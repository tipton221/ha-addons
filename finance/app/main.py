"""Finance web app. Run with: uvicorn app.main:create_app --factory"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
import csv
import io
import json
import sqlite3
from datetime import date, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .auth import CHALLENGE_COOKIE, SESSION_COOKIE, Challenges, CloudflareAccess, Passkeys, Sessions
from .config import Settings, load_settings
from .db import Database
from .plaid import Plaid, PlaidError
from .bills import CADENCE_MONTHS, bill_status, coverage, detect, normalize, save_detected
from .compare import compare_all
from .ledger import (COLORS, categories, colors_css, group_by_date, list_transactions, pair_transfers, resolve,
                     seed_categories, set_merchant_rule, valid_color, TRANSFER_CATEGORY)
from .sync import sync_all, sync_ynab
from .ynab_names import apply_ynab_names
from .ynab import YNAB

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("finance")

HERE = Path(__file__).parent
PACIFIC = ZoneInfo("America/Los_Angeles")
PUBLIC_PATHS = {"/favicon.ico", "/manifest.json", "/login", "/setup", "/api/auth/options", "/api/auth/verify",
                "/api/register/options", "/api/register/verify"}

CSP = (
    "default-src 'self'; "
    "script-src 'self' https://cdn.plaid.com; "
    "frame-src https://cdn.plaid.com; "
    "connect-src 'self' https://*.plaid.com; "
    "img-src 'self' data:; style-src 'self'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)


def shortdate(d: str | None, today: datetime | None = None) -> str:
    """'Nov 12' for dates in the current year, 'Nov 12, 2024' otherwise."""
    if not d:
        return ""
    dt = datetime.fromisoformat(d)
    year = (today or datetime.now(PACIFIC)).year
    return dt.strftime("%b %-d") if dt.year == year else dt.strftime("%b %-d, %Y")


def create_app(settings: Settings | None = None, plaid: Plaid | None = None,
               ynab: YNAB | None = None, background_sync: bool = True) -> FastAPI:
    settings = settings or load_settings()
    db = Database(settings.data_dir / "finance.db")
    sessions = Sessions(db, settings)
    passkeys = Passkeys(db, settings)
    challenges = Challenges()
    cf_access = CloudflareAccess(settings)
    plaid = plaid or (Plaid(settings) if settings.plaid_configured else None)
    ynab = ynab or YNAB(settings)
    sync_lock = asyncio.Lock()

    seed_categories(db)
    with db.connect() as conn:
        pair_transfers(conn)  # existing history, and after upgrading

    if db.get_kv("inbox_initialized") != "1":
        # Before 0.3.0 every transaction started unreviewed; start the inbox with the last week only.
        with db.connect() as conn:
            conn.execute("UPDATE transactions SET reviewed=1 WHERE date < date('now', '-7 day')")
        db.set_kv("inbox_initialized", "1")

    async def run_sync() -> dict:
        results: dict = {}
        async with sync_lock:
            if plaid is not None:
                results["plaid"] = await sync_all(db, plaid)
                if db.get_kv("bills_detected") != "1":
                    results["bills_added"] = save_detected(db, detect(db, datetime.now(PACIFIC).date()))
                    db.set_kv("bills_detected", "1")
            if ynab.configured:
                try:
                    results["ynab"] = await sync_ynab(db, ynab)
                    db.set_kv("ynab_error", "")
                except Exception as e:
                    log.warning("YNAB sync failed: %s", e)
                    db.set_kv("ynab_error", type(e).__name__)
                    results["ynab"] = {"error": type(e).__name__}
            if db.get_kv("ynab_names_applied") != "1" and ynab_names_ready():
                results["ynab_names"] = apply_ynab_names(db)  # once; the Compare page can run it again
        return results

    def ynab_names_ready() -> bool:
        with db.connect() as conn:
            return bool(conn.execute("SELECT 1 FROM account_links LIMIT 1").fetchone()
                        and conn.execute("SELECT 1 FROM transactions WHERE source='ynab' LIMIT 1").fetchone())

    async def sync_loop():
        await asyncio.sleep(60)
        while True:
            try:
                await run_sync()
            except Exception:
                log.exception("Scheduled sync failed")
            await asyncio.sleep(settings.sync_interval_hours * 3600)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if not settings.cf_access_enabled:
            log.warning("Cloudflare Access JWT check is OFF. Set cf_access_team_domain and cf_access_aud.")
        task = asyncio.create_task(sync_loop()) if background_sync else None
        yield
        if task:
            task.cancel()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")

    def money(value, currency="USD"):
        if value is None:
            return ""
        sign = "-" if value < 0 else ""
        return f"{sign}${abs(value):,.2f}"

    def local_time(iso: str | None) -> str:
        if not iso:
            return ""
        dt = datetime.fromisoformat(iso).astimezone(PACIFIC)
        return dt.strftime("%b %-d, %-I:%M %p")

    templates.env.filters["money"] = money
    templates.env.filters["local"] = local_time

    def category(t) -> str:
        """Plaid's detailed category without its group prefix, e.g. FOOD_AND_DRINK_GROCERIES -> Groceries."""
        primary, detail = t.get("category") or "", t.get("category_detail") or ""
        label = detail[len(primary) + 1:] if primary and detail.startswith(primary + "_") else (detail or primary)
        return label.replace("_", " ").capitalize()

    templates.env.globals["category"] = category
    templates.env.filters["shortdate"] = shortdate

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # Layer 1: Cloudflare Access.
        token = request.headers.get("cf-access-jwt-assertion") or request.cookies.get("CF_Authorization")
        if not await run_in_threadpool(cf_access.verify, token):
            return Response("Forbidden", status_code=403)

        path = request.url.path
        # Same-origin guard for state-changing requests. Cross-site pages
        # cannot add this header without a CORS preflight, which we never allow.
        if request.method not in ("GET", "HEAD") and request.headers.get("x-finance") != "1":
            return Response("Forbidden", status_code=403)

        # Layer 2: app session (passkey).
        if not (path.startswith("/static/") or path in PUBLIC_PATHS):
            if not sessions.valid(request.cookies.get(SESSION_COOKIE)):
                if path.startswith("/api/"):
                    return JSONResponse({"error": "login required"}, status_code=401)
                target = "/setup" if passkeys.count() == 0 else "/login"
                return RedirectResponse(target, status_code=303)

        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    def set_cookie(resp: Response, name: str, value: str, max_age: int) -> None:
        resp.set_cookie(name, value, max_age=max_age, httponly=True,
                        secure=settings.secure_cookies, samesite="lax")

    # ---------- login pages ----------

    @app.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request):
        if passkeys.count() > 0:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(request, "setup.html", {
            "code_ok": len(settings.setup_code) >= 12,
        })

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if passkeys.count() == 0:
            return RedirectResponse("/setup", status_code=303)
        return templates.TemplateResponse(request, "login.html", {})

    @app.post("/api/register/options")
    async def register_options(request: Request):
        body = await request.json()
        signed_in = sessions.valid(request.cookies.get(SESSION_COOKIE))
        # First passkey needs the setup code; more passkeys need a signed-in session.
        if not signed_in:
            if passkeys.count() > 0 or not passkeys.check_setup_code(str(body.get("code", ""))):
                raise HTTPException(403, "Setup code is wrong or setup is closed")
        options, challenge = passkeys.registration_options()
        resp = Response(options, media_type="application/json")
        set_cookie(resp, CHALLENGE_COOKIE, challenges.issue(challenge), 300)
        return resp

    @app.post("/api/register/verify")
    async def register_verify(request: Request):
        body = await request.json()
        signed_in = sessions.valid(request.cookies.get(SESSION_COOKIE))
        if not signed_in and passkeys.count() > 0:
            raise HTTPException(403, "Setup is closed")
        challenge = challenges.take(request.cookies.get(CHALLENGE_COOKIE))
        if challenge is None:
            raise HTTPException(400, "Challenge expired; try again")
        try:
            passkeys.register(body["credential"], challenge, str(body.get("label", "")))
        except Exception as e:
            raise HTTPException(400, f"Passkey not accepted: {e}")
        resp = JSONResponse({"ok": True})
        if not signed_in:
            set_cookie(resp, SESSION_COOKIE, sessions.create(), settings.session_hours * 3600)
        return resp

    @app.post("/api/auth/options")
    async def auth_options():
        options, challenge = passkeys.authentication_options()
        resp = Response(options, media_type="application/json")
        set_cookie(resp, CHALLENGE_COOKIE, challenges.issue(challenge), 300)
        return resp

    @app.post("/api/auth/verify")
    async def auth_verify(request: Request):
        body = await request.json()
        challenge = challenges.take(request.cookies.get(CHALLENGE_COOKIE))
        if challenge is None:
            raise HTTPException(400, "Challenge expired; try again")
        try:
            ok = passkeys.authenticate(body["credential"], challenge)
        except Exception as e:
            log.warning("Passkey sign-in failed: %s", e)
            ok = False
        if not ok:
            raise HTTPException(401, "Passkey not recognized")
        resp = JSONResponse({"ok": True})
        set_cookie(resp, SESSION_COOKIE, sessions.create(), settings.session_hours * 3600)
        return resp

    @app.post("/api/logout")
    async def logout(request: Request):
        sessions.revoke(request.cookies.get(SESSION_COOKIE))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    # ---------- app pages ----------

    def schedule() -> dict:
        raw = db.get_kv("payday_schedule")
        return json.loads(raw) if raw else {"kind": "semimonthly", "days": [10, 25]}

    def account_filter() -> str | None:
        """The one account the transaction list is narrowed to, or None for all accounts."""
        value = db.get_kv("account_filter") or None
        return value if value in {a["account_id"] for a in plaid_accounts()} else None

    def today() -> date:
        return datetime.now(PACIFIC).date()

    def plaid_accounts() -> list[dict]:
        with db.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT a.*, COALESCE(a.display_name, a.name) AS label, p.institution FROM accounts a "
                "LEFT JOIN plaid_items p ON a.item_id = p.item_id WHERE a.source='plaid' "
                "ORDER BY p.institution, a.type, label")]

    PAGE = 60

    def page_after(rows: list[dict]) -> str | None:
        return f"{rows[-1]['date']}:{rows[-1]['rid']}" if len(rows) == PAGE else None

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        inbox = resolve(db, list_transactions(db, reviewed=False, limit=300))
        selected = account_filter()
        rest = resolve(db, list_transactions(db, reviewed=True, account=selected, limit=PAGE))
        cov = coverage(db, schedule(), today())
        with db.connect() as conn:
            attention = conn.execute("SELECT COUNT(*) AS n FROM plaid_items WHERE status != 'ok'").fetchone()["n"]
        # Every scheduled bill, furthest out first so the next one sits at the bottom (like YNAB).
        upcoming = sorted((b for b in bill_status(db, today()) if b["active"] and not b["stale"]
                           and (not selected or b["account_id"] == selected)),
                          key=lambda b: b["next_due"], reverse=True)
        return templates.TemplateResponse(request, "home.html", {
            "upcoming": upcoming, "upcoming_total": sum(b["amount"] for b in upcoming), "colors": COLORS,
            "settings": settings, "plaid_ready": plaid is not None, "inbox": inbox, "cov": cov,
            "attention": attention, "last_sync": db.get_kv("last_sync_at"),
            "groups": group_by_date(rest, today()), "more": page_after(rest),
            "accounts": plaid_accounts(), "selected": selected,
        })

    @app.get("/api/transactions/page", response_class=HTMLResponse)
    async def more_transactions(request: Request, before: str):
        try:
            d, rid = before.split(":")
            date.fromisoformat(d)
            int(rid)
        except ValueError:
            raise HTTPException(400, "Bad cursor")
        rows = resolve(db, list_transactions(db, reviewed=True, account=account_filter(),
                                             before=(d, rid), limit=PAGE))
        return templates.TemplateResponse(request, "_rows.html", {
            "groups": group_by_date(rows, today()), "more": page_after(rows)})

    @app.post("/api/view/account-filter")
    async def set_account_filter(request: Request):
        body = await request.json()
        account = body.get("account") or ""
        if account and account not in {a["account_id"] for a in plaid_accounts()}:
            raise HTTPException(400, "Unknown account")
        db.set_kv("account_filter", account)
        return {"ok": True}

    @app.get("/favicon.ico")
    async def favicon():
        return FileResponse(HERE / "static" / "icons" / "favicon-32.png", media_type="image/png")

    @app.get("/manifest.json")
    async def manifest():
        return JSONResponse({
            "name": "Finance", "short_name": "Finance", "start_url": "/", "scope": "/",
            "display": "standalone", "background_color": "#07080b", "theme_color": "#07080b",
            "icons": [
                {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
                {"src": "/static/icons/icon-512-maskable.png", "sizes": "512x512", "type": "image/png",
                 "purpose": "maskable"},
            ],
        }, media_type="application/manifest+json")

    @app.get("/colors.css")
    async def colors_stylesheet():
        return Response(colors_css(db), media_type="text/css")

    def find_txn(source: str, tid: str) -> dict:
        rows = list_transactions(db, where="t.source = ? AND t.id = ?", params=(source, tid), limit=1) \
            if source == "plaid" else []
        if not rows:
            raise HTTPException(404, "Transaction not found")
        return resolve(db, rows)[0]

    @app.get("/api/transactions/{source}/{tid}")
    async def transaction_detail(source: str, tid: str):
        t = find_txn(source, tid)
        return {
            "id": t["id"], "source": t["source"], "display_name": t["display_name"],
            "bank_name": t["name"], "merchant": t["merchant"], "merchant_key": t["merchant_key"],
            "date": t["date"], "amount": t["amount"], "pending": bool(t["pending"]),
            "account": t["account_label"], "note": t["note"] or "",
            "category_id": (t["category_info"] or {}).get("id"),
            "category_is_override": bool(t["category_id"]),
            "name_is_override": bool(t["name_override"]),
            "bill": t["bill"]["name"] if t["bill"] else None, "reviewed": bool(t["reviewed"]),
        }

    @app.post("/api/transactions/{source}/{tid}")
    async def edit_transaction(source: str, tid: str, request: Request):
        t = find_txn(source, tid)
        body = await request.json()
        if "display_name" in body:
            name = str(body["display_name"] or "").strip()[:80]
            bank = (t["merchant"] or t["name"] or "").strip()
            if t["name_override"]:  # a one-off name (from YNAB) stays one-off
                with db.connect() as conn:
                    conn.execute("UPDATE transactions SET name_override=? WHERE source=? AND id=?",
                                 (None if name in ("", bank) else name, source, tid))
            else:
                set_merchant_rule(db, t["merchant_key"], display_name=None if name in ("", bank) else name)
        if "category_id" in body:
            cid = body["category_id"]
            with db.connect() as conn:
                if cid is not None and not conn.execute("SELECT 1 FROM categories WHERE id=?", (int(cid),)).fetchone():
                    raise HTTPException(400, "Unknown category")
            if body.get("scope") == "this":
                with db.connect() as conn:
                    conn.execute("UPDATE transactions SET category_id=? WHERE source=? AND id=?",
                                 (int(cid) if cid is not None else None, source, tid))
            else:
                set_merchant_rule(db, t["merchant_key"], category_id=int(cid) if cid is not None else None)
        if "note" in body:
            note = str(body["note"] or "").strip()[:500] or None
            with db.connect() as conn:
                conn.execute("UPDATE transactions SET note=? WHERE source=? AND id=?", (note, source, tid))
        return {"ok": True}

    @app.get("/accounts", response_class=HTMLResponse)
    async def accounts_page(request: Request):
        with db.connect() as conn:
            items = [dict(r) for r in conn.execute(
                "SELECT item_id, institution, status, error_code, update_status, last_sync_at "
                "FROM plaid_items ORDER BY institution")]
            total = conn.execute("SELECT COUNT(*) AS n FROM transactions WHERE source='plaid'").fetchone()["n"]
            ynab_total = conn.execute("SELECT COUNT(*) AS n FROM transactions WHERE source='ynab'").fetchone()["n"]
        ynab_status = None
        if ynab.configured:
            ynab_status = {"last_sync": db.get_kv("ynab_last_sync_at"), "error": db.get_kv("ynab_error"),
                           "count": ynab_total}
        return templates.TemplateResponse(request, "accounts.html", {
            "settings": settings, "plaid_ready": plaid is not None, "items": items,
            "accounts": plaid_accounts(), "total": total, "ynab": ynab_status,
            "categories": categories(db), "colors": COLORS,
        })

    @app.post("/api/accounts/{account_id}/rename")
    async def rename_account(account_id: str, request: Request):
        body = await request.json()
        name = str(body.get("name") or "").strip()[:60] or None
        with db.connect() as conn:
            cur = conn.execute("UPDATE accounts SET display_name=? WHERE account_id=? AND source='plaid'",
                               (name, account_id))
            if not cur.rowcount:
                raise HTTPException(404, "Unknown account")
        return {"ok": True}

    @app.post("/api/categories")
    async def save_category(request: Request):
        body = await request.json()
        name = str(body.get("name") or "").strip()[:40]
        color = valid_color(body.get("color"))
        if not name:
            raise HTTPException(400, "Name the category")
        try:
            with db.connect() as conn:
                if body.get("id"):
                    conn.execute("UPDATE categories SET name=?, color=? WHERE id=?", (name, color, int(body["id"])))
                    new_id = int(body["id"])
                else:
                    sort = conn.execute("SELECT COALESCE(MAX(sort), 0) + 1 FROM categories").fetchone()[0]
                    new_id = conn.execute("INSERT INTO categories (name, color, sort) VALUES (?, ?, ?)",
                                          (name, color, sort)).lastrowid
        except sqlite3.IntegrityError:
            raise HTTPException(400, "A category with that name already exists")
        return {"ok": True, "id": new_id}

    @app.post("/api/categories/{cat_id}/delete")
    async def delete_category(cat_id: int):
        with db.connect() as conn:
            conn.execute("UPDATE merchant_rules SET category_id=NULL WHERE category_id=?", (cat_id,))
            conn.execute("UPDATE transactions SET category_id=NULL WHERE category_id=?", (cat_id,))
            conn.execute("DELETE FROM categories WHERE id=?", (cat_id,))
        return {"ok": True}

    @app.get("/api/categories")
    async def list_categories():
        return {"categories": categories(db)}

    @app.post("/api/inbox/review-all")
    async def review_all(request: Request):
        body = await request.json()
        ids = [str(i) for i in body.get("ids", [])][:1000]
        with db.connect() as conn:
            conn.executemany("UPDATE transactions SET reviewed=1 WHERE source='plaid' AND (id=? OR transfer_peer=?)",
                             [(i, i) for i in ids])
        return {"ok": True, "count": len(ids)}

    # ---------- bills ----------

    @app.get("/bills", response_class=HTMLResponse)
    async def bills_page(request: Request):
        today = datetime.now(PACIFIC).date()
        bills = bill_status(db, today)
        return templates.TemplateResponse(request, "bills.html", {
            "bills": bills, "colors": COLORS, "schedule": schedule(), "accounts": plaid_accounts(), "cov": coverage(db, schedule(), today),
            "monthly_total": sum(b["monthly_equivalent"] for b in bills if b["active"] and not b["stale"]),
        })

    def bill_fields(body: dict) -> dict:
        try:
            out = {
                "name": str(body["name"]).strip()[:80],
                "match": normalize(str(body.get("match") or body["name"])),
                "amount": round(abs(float(body.get("amount") or 0)), 2),
                "cadence": body.get("cadence") if body.get("cadence") in CADENCE_MONTHS else "monthly",
                "due_day": int(body["due_day"]) if body.get("due_day") else None,
                "next_due_override": str(date.fromisoformat(body["next_due"])) if body.get("next_due") else None,
                "active": 1 if body.get("active", True) else 0,
                "color": valid_color(body.get("color"), "purple"),
                "varies": 1 if body.get("varies") else 0,
                "account_id": str(body["account_id"]) if body.get("account_id") else None,
            }
        except (KeyError, ValueError, TypeError):
            raise HTTPException(400, "Name and amount are required; dates use YYYY-MM-DD")
        if not out["name"] or not out["match"] or (out["due_day"] is not None and not 1 <= out["due_day"] <= 31):
            raise HTTPException(400, "Check the name and day of month")
        if out["account_id"] and out["account_id"] not in {a["account_id"] for a in plaid_accounts()}:
            raise HTTPException(400, "Unknown account")
        if not out["varies"] and out["amount"] <= 0:
            raise HTTPException(400, "Enter the amount, or mark the bill as varying")
        return out

    @app.post("/api/bills")
    async def save_bill(request: Request):
        body = await request.json()
        f = bill_fields(body)
        with db.connect() as conn:
            if body.get("id"):
                conn.execute("UPDATE bills SET name=?, match=?, amount=?, cadence=?, due_day=?, next_due_override=?, "
                             "active=?, color=?, varies=?, account_id=? WHERE id=?", (*f.values(), int(body["id"])))
            else:
                conn.execute("INSERT INTO bills (name, match, amount, cadence, due_day, next_due_override, active, color, "
                             "varies, account_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", tuple(f.values()))
        return {"ok": True}

    @app.post("/api/bills/{bill_id}/delete")
    async def delete_bill(bill_id: int):
        with db.connect() as conn:
            row = conn.execute("SELECT match, source FROM bills WHERE id=?", (bill_id,)).fetchone()
            if row and row["source"] == "detected":
                conn.execute("INSERT OR IGNORE INTO bills_dismissed VALUES (?)", (row["match"],))
            conn.execute("DELETE FROM bills WHERE id=?", (bill_id,))
        return {"ok": True}

    @app.post("/api/bills/detect")
    async def detect_bills():
        return {"added": save_detected(db, detect(db, datetime.now(PACIFIC).date()))}

    @app.post("/api/paydays")
    async def save_paydays(request: Request):
        body = await request.json()
        if body.get("kind") == "biweekly":
            try:
                anchor = date.fromisoformat(body["anchor"]).isoformat()
            except (KeyError, ValueError):
                raise HTTPException(400, "Pick a recent payday date")
            sched = {"kind": "biweekly", "anchor": anchor}
        else:
            try:
                days = sorted({int(d) for d in body.get("days", [])})
            except ValueError:
                raise HTTPException(400, "Paydays must be days of the month")
            if not days or any(not 1 <= d <= 31 for d in days):
                raise HTTPException(400, "Paydays must be days of the month")
            sched = {"kind": "semimonthly", "days": days}
        db.set_kv("payday_schedule", json.dumps(sched))
        return {"ok": True}

    # ---------- search and export ----------

    def search(q: dict) -> list[dict]:
        where, params = [], []
        if q.get("account"):
            where.append("t.account_id = ?")
            params.append(q["account"])
        if q.get("start"):
            where.append("t.date >= ?")
            params.append(q["start"])
        if q.get("end"):
            where.append("t.date <= ?")
            params.append(q["end"])
        for key, op in (("min", ">="), ("max", "<=")):
            if q.get(key):
                try:
                    params.append(abs(float(q[key])))
                    where.append(f"ABS(t.amount) {op} ?")
                except ValueError:
                    pass
        rows = resolve(db, list_transactions(db, where=" AND ".join(where), params=tuple(params), limit=0))
        if q.get("q"):
            needle = q["q"].lower()
            rows = [t for t in rows if needle in " ".join(
                [t["display_name"], t["name"] or "", t["merchant"] or "", t["note"] or ""]).lower()]
        if q.get("category"):
            rows = [t for t in rows if str((t["category_info"] or {}).get("id")) == q["category"]]
        return rows

    def is_transfer(t: dict) -> bool:
        return bool(t.get("transfer")) or (t["category_info"] or {}).get("name") == TRANSFER_CATEGORY

    @app.get("/search", response_class=HTMLResponse)
    async def search_page(request: Request):
        q = {k: v for k, v in request.query_params.items() if v}
        rows = search(q) if q else resolve(db, list_transactions(db, limit=100))
        more = len(rows) > 1000
        # Totals leave out money moving between your own accounts, unless you searched that category.
        counted = rows if q.get("category") else [t for t in rows if not is_transfer(t)]
        out = sum(t["amount"] for t in counted if t["amount"] > 0)
        inflow = -sum(t["amount"] for t in counted if t["amount"] < 0)
        return templates.TemplateResponse(request, "search.html", {
            "q": q, "groups": group_by_date(rows[:1000], today()), "count": len(rows), "truncated": more,
            "accounts": plaid_accounts(), "categories": categories(db), "searched": bool(q),
            "total_out": out, "total_in": inflow, "export_query": urlencode(q),
            "excluded": len(rows) - len(counted),
        })

    @app.get("/export.csv")
    async def export_csv(request: Request):
        rows = search({k: v for k, v in request.query_params.items() if v})
        rows.sort(key=lambda t: (t["date"], t["rid"]))
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["date", "authorized_date", "institution", "account", "name", "bank_description",
                    "amount", "category", "note", "bill", "pending", "reviewed"])
        for t in rows:
            w.writerow([t["date"], t["authorized_date"] or "", t["institution"] or "", t["account_label"],
                        t["display_name"], t["name"], f"{-t['amount']:.2f}",
                        (t["category_info"] or {}).get("name", ""), t["note"] or "",
                        t["bill"]["name"] if t["bill"] else "", "yes" if t["pending"] else "no",
                        "yes" if t["reviewed"] else "no"])
        stamp = datetime.now(PACIFIC).strftime("%Y-%m-%d")
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="transactions-{stamp}.csv"'})

    @app.get("/compare", response_class=HTMLResponse)
    async def compare_page(request: Request):
        last = db.get_kv("ynab_names_result")
        return templates.TemplateResponse(request, "compare.html", {
            "c": compare_all(db), "names": json.loads(last) if last else None})

    @app.post("/api/ynab-names")
    async def run_ynab_names():
        if not ynab_names_ready():
            raise HTTPException(400, "Link accounts and sync YNAB first")
        async with sync_lock:
            return apply_ynab_names(db)

    @app.post("/api/links")
    async def save_links(request: Request):
        body = await request.json()
        links = body.get("links") or {}
        with db.connect() as conn:
            valid_p = {r["account_id"] for r in conn.execute("SELECT account_id FROM accounts WHERE source='plaid'")}
            valid_y = {r["account_id"] for r in conn.execute("SELECT account_id FROM accounts WHERE source='ynab'")}
            for pid, chosen in links.items():
                if pid not in valid_p:
                    continue
                if isinstance(chosen, str):
                    chosen = [chosen]
                keep = [y for y in dict.fromkeys(chosen) if y in valid_y]
                conn.execute("DELETE FROM account_links WHERE plaid_account_id=?", (pid,))
                conn.executemany("INSERT INTO account_links VALUES (?, ?)", [(pid, y) for y in keep])
        return {"ok": True}

    @app.get("/oauth-return", response_class=HTMLResponse)
    async def oauth_return(request: Request):
        return templates.TemplateResponse(request, "oauth_return.html", {})

    # ---------- Plaid ----------

    def require_plaid() -> Plaid:
        if plaid is None:
            raise HTTPException(400, "Plaid client ID and secret are not set in the add-on options")
        return plaid

    @app.post("/api/plaid/link-token")
    async def link_token(request: Request):
        p = require_plaid()
        body = await request.json()
        access_token = None
        if body.get("item_id"):
            with db.connect() as conn:
                row = conn.execute("SELECT access_token FROM plaid_items WHERE item_id=?",
                                   (body["item_id"],)).fetchone()
            if not row:
                raise HTTPException(404, "Unknown connection")
            access_token = row["access_token"]
        try:
            return {"link_token": await p.link_token(access_token)}
        except PlaidError as e:
            raise HTTPException(502, f"Plaid: {e}")

    @app.post("/api/plaid/exchange")
    async def exchange(request: Request):
        p = require_plaid()
        body = await request.json()
        inst = body.get("institution") or {}
        try:
            access_token, item_id = await p.exchange(body["public_token"])
        except PlaidError as e:
            raise HTTPException(502, f"Plaid: {e}")
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO plaid_items (item_id, access_token, institution_id, institution) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(item_id) DO UPDATE SET access_token=excluded.access_token",
                (item_id, access_token, inst.get("institution_id"), inst.get("name") or ""),
            )
        asyncio.create_task(run_sync())
        return {"ok": True}

    @app.post("/api/plaid/updated")
    async def updated(request: Request):
        body = await request.json()
        with db.connect() as conn:
            conn.execute("UPDATE plaid_items SET status='ok', error_code=NULL WHERE item_id=?",
                         (body.get("item_id"),))
        asyncio.create_task(run_sync())
        return {"ok": True}

    @app.post("/api/sync")
    async def sync_now():
        return {"results": await run_sync()}

    @app.post("/api/transactions/{source}/{tid}/review")
    async def review(source: str, tid: str, request: Request):
        body = await request.json()
        with db.connect() as conn:
            conn.execute("UPDATE transactions SET reviewed=?, flagged=? WHERE source=? AND id=?",
                         (1 if body.get("reviewed") else 0, 1 if body.get("flagged") else 0, source, tid))
            row = conn.execute("SELECT transfer_peer FROM transactions WHERE source=? AND id=?", (source, tid)).fetchone()
            peer = row["transfer_peer"] if row else None
            if peer:  # reviewing one side of a transfer reviews the other
                conn.execute("UPDATE transactions SET reviewed=? WHERE source=? AND id=?",
                             (1 if body.get("reviewed") else 0, source, peer))
        return {"ok": True, "peer": peer}

    return app

