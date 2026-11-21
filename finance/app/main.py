"""Finance web app. Run with: uvicorn app.main:create_app --factory"""

from __future__ import annotations

import asyncio
import calendar
import logging
from contextlib import asynccontextmanager
import csv
import io
import json
import os
import re
import secrets
import sqlite3
import tempfile
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .auth import (CHALLENGE_COOKIE, SESSION_COOKIE, SIGNIN_CODE_TTL, Challenges, CloudflareAccess, Passkeys, Sessions,
                   SignInCodes)
from .config import Settings, load_settings
from .db import Database
from .plaid import Plaid, PlaidError
from .transit import transit_options
from .investments import PLANS, limits, returns
from .loans import loans
from .apns import APNs
from .push import after_sync_message, send as push_send, vapid_keys
from .bills import CADENCES, CadenceLabels, cadence_text, make_cadence, step_months, bill_status, coverage, detect, new_candidates, normalize, save_detected
from .compare import compare_all
from .insights import (KIND_LABELS, KIND_PILLS, KINDS, MANUAL_DEBTS, MANUAL_KINDS, monthly, net_worth, observations,
                       spending_rows)
from .ledger import (COLORS, categories, colors_css, group_by_date, list_transactions, merchant_key, pair_transfers, resolve,
                     seed_categories, set_merchant_rule, valid_color, TRANSFER_CATEGORY)
from .sync import refresh_accounts, sync_all, sync_ynab
from .ynab_names import apply_ynab_names
from .webhooks import Verifier
from .ynab import YNAB

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("finance")

HERE = Path(__file__).parent
PACIFIC = ZoneInfo("America/Los_Angeles")
BADGE_CHECK_SECONDS = 10  # how often the app badge is compared with the count of things to review
AASA_PATH = "/.well-known/apple-app-site-association"
WEBHOOK_PATH = "/plaid/webhook"
WEBHOOK_DELAY = 10   # seconds: a burst of webhooks (one per connection or product) runs one sync
PUBLIC_PATHS = {"/favicon.ico", "/manifest.json", "/sw.js", "/login", "/setup", "/api/auth/options", "/api/auth/verify",
                "/api/register/options", "/api/register/verify", "/api/signin-code/use"}

def num(value) -> float:
    """A number typed in a field, which may have a $ and thousands commas (1,234.50)."""
    text = str(value if value is not None else "").replace(",", "").replace("$", "").strip()
    return float(text) if text else 0.0


def csp(nonce: str) -> str:
    """Content Security Policy. Plaid Link adds one <style> element when it opens and copies the
    nonce from its own <script> tag onto it, so that nonce is allowed for styles (nothing else is)."""
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}' https://cdn.plaid.com; "
        "frame-src https://cdn.plaid.com; "
        "connect-src 'self' https://*.plaid.com; "
        f"img-src 'self' data: https://*.plaid.com; style-src 'self' 'nonce-{nonce}'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
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
    signin_codes = SignInCodes()
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

    async def ensure_webhooks() -> None:
        """Give every connection the webhook address (new ones get it from Link). Failures only log:
        the scheduled sync still runs."""
        url = settings.plaid_webhook_url
        if plaid is None or not url:
            return
        with db.connect() as conn:
            items = [dict(r) for r in conn.execute(
                "SELECT item_id, access_token FROM plaid_items WHERE webhook IS NULL OR webhook != ?", (url,))]
        for item in items:
            try:
                await plaid.set_webhook(item["access_token"], url)
            except Exception as e:
                log.warning("Couldn't set the webhook for a connection: %s", e)
                continue
            with db.connect() as conn:
                conn.execute("UPDATE plaid_items SET webhook=? WHERE item_id=?", (url, item["item_id"]))

    async def run_sync() -> dict:
        results: dict = {}
        async with sync_lock:
            if plaid is not None:
                await ensure_webhooks()
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
        try:
            results["push"] = await notify_after_sync()
        except Exception:
            log.exception("Push notification failed")
        return results

    apns = APNs(settings.data_dir / "apns.p8", settings.apns_key_id, settings.apple_app_id)

    async def notify(message: dict, skip: str = "") -> int:
        """Send a notification to every device: Web Push subscriptions and the native app's devices.
        skip is one device to leave out (a Web Push endpoint or an app device token)."""
        return (await push_send(db, message, push_subject(), skip=skip)
                + await apns.send(db, message, skip=skip))

    def push_subject() -> str:
        """Who sends the pushes (required by push services): the app's own address."""
        return settings.public_url if settings.public_url.startswith("https://") else "mailto:finance@localhost.invalid"

    def broken_items() -> list[dict]:
        with db.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT item_id, institution FROM plaid_items WHERE status != 'ok'")]

    async def notify_after_sync() -> int:
        """A notification for new transactions to review or a connection that broke, with the badge."""
        with db.connect() as conn:
            if not (conn.execute("SELECT 1 FROM push_subs LIMIT 1").fetchone()
                    or conn.execute("SELECT 1 FROM apns_devices LIMIT 1").fetchone()):
                return 0
        rows = resolve(db, list_transactions(db, reviewed=False, limit=300))
        message = after_sync_message(db, rows, broken_items(), inbox_count() + account_issues())
        if not message:
            return 0
        db.set_kv("push_last_badge", str(message["badge"]))
        db.set_kv("apns_badge", str(message["badge"]))  # the notification sets the app's badge
        return await notify(message)

    async def badge_loop():
        """Keep the app's badge the same on every device, as Parcel does: when the count of things
        to review or fix changes (reviewed on one device, a connection fixed), set it everywhere,
        and at zero clear the notifications shown."""
        while True:
            await asyncio.sleep(BADGE_CHECK_SECONDS)
            try:
                n = inbox_count() + account_issues()
                if str(n) != (db.get_kv("apns_badge") or "0"):
                    db.set_kv("apns_badge", str(n))
                    await apns.badge(db, n)
                    await apns.refresh(db, {"badge": n, "clear": n == 0})
            except Exception:
                log.exception("Badge update failed")

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
            # With webhooks, Plaid says when there's new data; this is the hourly backup.
            hours = 1 if settings.plaid_webhook_url else settings.sync_interval_hours
            await asyncio.sleep(hours * 3600)

    verifier = Verifier(plaid) if plaid is not None else None
    webhook_task: asyncio.Task | None = None

    async def plaid_webhook(request: Request) -> Response:
        """Plaid has new data (or news about a connection): sync soon. One sync covers a burst."""
        nonlocal webhook_task
        body = await request.body()
        if verifier is None or not await verifier.verify(request.headers.get("plaid-verification"), body):
            return JSONResponse({"error": "not verified"}, status_code=401)
        try:
            item_id = str(json.loads(body).get("item_id") or "")
        except ValueError:
            return JSONResponse({"error": "bad body"}, status_code=400)
        with db.connect() as conn:
            known = conn.execute("SELECT 1 FROM plaid_items WHERE item_id=?", (item_id,)).fetchone()
        if known:
            db.set_kv("plaid_webhook_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
            if webhook_task is None or webhook_task.done():
                async def later():
                    await asyncio.sleep(WEBHOOK_DELAY)
                    try:
                        await run_sync()
                    except Exception:
                        log.exception("Sync after a webhook failed")
                webhook_task = asyncio.create_task(later())
        return JSONResponse({"ok": True})

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if not settings.cf_access_enabled:
            log.warning("Cloudflare Access JWT check is OFF. Set cf_access_team_domain and cf_access_aud.")
        tasks = [asyncio.create_task(sync_loop()), asyncio.create_task(badge_loop())] if background_sync else []
        yield
        for task in tasks:
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

    # Static files carry a version in their URL, so an update never runs with an old cached script.
    templates.env.globals["asset_v"] = str(int(max(f.stat().st_mtime for f in (HERE / "static").rglob("*") if f.is_file())))
    # Keep a short trailing part with the word before it: "403 (b)" never breaks before "(b)".
    templates.env.filters["keep"] = lambda text: re.sub(r" (\(?\w{1,3}\)?)$", "\u00a0\\1", str(text or ""))
    templates.env.filters["month_label"] = lambda m: date(int(m[:4]), int(m[5:7]), 1).strftime("%b %Y") if m else ""
    def inbox_count() -> int:
        with db.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM transactions WHERE source='plaid' AND reviewed=0").fetchone()[0]
    templates.env.globals["inbox_count"] = inbox_count  # the badge on the Inbox tab

    def account_issues() -> int:
        """Things to fix on the Accounts tab: a bank connection that needs signing in again or failed,
        one waiting for investment access, and a failed YNAB sync."""
        with db.connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM plaid_items WHERE status != 'ok' OR investments = 'consent'").fetchone()[0]
        if ynab.configured and db.get_kv("ynab_error"):
            n += 1
        return n
    templates.env.globals["account_issues"] = account_issues
    templates.env.globals["category"] = category
    templates.env.filters["shortdate"] = shortdate
    templates.env.globals["cadence_label"] = CadenceLabels()
    # For the form: a stored schedule as (choice, N) — a named one, or every N months or years.
    def cadence_parts(c):
        return ((c, "") if c in CADENCES or not c else
                ("years", step_months(c) // 12) if step_months(c) % 12 == 0 else ("months", step_months(c)))
    templates.env.globals["cadence_parts"] = cadence_parts

    @app.middleware("http")
    async def guard(request: Request, call_next):
        request.state.nonce = secrets.token_urlsafe(16)
        # Apple fetches this file from its own servers, without a Cloudflare login, to confirm the
        # native app may use passkeys for this site. It holds only the app's ID, so it is served
        # before the login checks (Cloudflare Access needs a matching bypass for this one path).
        if request.url.path == AASA_PATH and request.method in ("GET", "HEAD"):
            if not settings.apple_app_id:
                return Response("Not found", status_code=404)
            return JSONResponse({"webcredentials": {"apps": [settings.apple_app_id]}},
                                headers={"Cache-Control": "max-age=3600"})
        # Plaid's webhooks come from Plaid, without a Cloudflare login or a session; each is
        # checked against Plaid's signature instead (Cloudflare Access needs a bypass for this path).
        if request.url.path == WEBHOOK_PATH and request.method == "POST":
            return await plaid_webhook(request)
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
        response.headers["Content-Security-Policy"] = csp(request.state.nonce)
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

    @app.post("/api/signin-code")
    async def signin_code_new():
        """A one-time code (signed-in only) that signs in another device: Accounts shows it, and the
        sign-in page on the other device takes it. For a device without the passkey, such as the
        iPhone simulator. A new code replaces the last one."""
        return {"code": signin_codes.issue(), "expires_in": SIGNIN_CODE_TTL}

    @app.post("/api/signin-code/use")
    async def signin_code_use(request: Request):
        body = await request.json()
        if not signin_codes.redeem(str(body.get("code", ""))):
            log.warning("Sign-in code rejected")
            raise HTTPException(401, "That code didn't work. Codes last five minutes and work once; make a new one on a signed-in device.")
        log.info("Signed in with a one-time code")
        resp = JSONResponse({"ok": True})
        set_cookie(resp, SESSION_COOKIE, sessions.create(), settings.session_hours * 3600)
        return resp

    @app.post("/api/backup")
    async def download_backup():
        """A consistent copy of the whole database (SQLite's online backup, safe while the app is
        running), for moving the app to another server. It holds the bank connections, so it is only
        given to a signed-in session, and only to a same-origin request (POST with the app header)."""
        def snapshot() -> bytes:
            fd, tmp = tempfile.mkstemp(suffix=".db", dir=settings.data_dir)
            os.close(fd)
            try:
                src = sqlite3.connect(db.path)
                dst = sqlite3.connect(tmp)
                with dst:
                    src.backup(dst)
                src.close()
                dst.close()
                return Path(tmp).read_bytes()
            finally:
                os.unlink(tmp)
        data = await run_in_threadpool(snapshot)
        name = f"finance-backup-{today().isoformat()}.db"
        return Response(data, media_type="application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

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

    def account_filter(value: str | None) -> str | None:
        """The one account the list is narrowed to (from the request), or None for all accounts.
        Filters live in the page, not the database, so a reload starts from All accounts."""
        return value if value and value in {a["account_id"] for a in plaid_accounts()} else None

    def all_list(account: str | None, before=None, pending: bool = False) -> list[dict]:
        """The All transactions list: reviewed transactions, or every pending one when that filter is on."""
        if pending:
            return list_transactions(db, account=account, before=before, limit=PAGE, where="t.pending = 1")
        return list_transactions(db, reviewed=True, account=account, before=before, limit=PAGE)

    def today() -> date:
        return datetime.now(PACIFIC).date()

    VISIBILITY = ("everyday", "less", "archived")

    def plaid_accounts() -> list[dict]:
        """Plaid accounts with their full label, short pill label, and visibility (everyday first)."""
        with db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT a.*, COALESCE(a.display_name, a.name) AS label, p.institution FROM accounts a "
                "LEFT JOIN plaid_items p ON a.item_id = p.item_id WHERE a.source='plaid' "
                "ORDER BY p.institution, a.type, label")]
        for a in rows:
            a["pill"] = a.get("short_name") or a["label"]
            a["visibility"] = a.get("visibility") if a.get("visibility") in VISIBILITY else "everyday"
        return sorted(rows, key=lambda a: VISIBILITY.index(a["visibility"]))

    PAGE = 60

    def page_after(rows: list[dict]) -> str | None:
        return f"{rows[-1]['date']}:{rows[-1]['rid']}" if len(rows) == PAGE else None

    def inbox_context(account: str = "", pending: int = 0) -> dict:
        """Everything the Inbox shows, for the page and for the app's JSON (/api/inbox), so the two agree."""
        inbox = resolve(db, list_transactions(db, reviewed=False, limit=300))
        selected = account_filter(account)
        pending_only = bool(pending)
        rest = resolve(db, all_list(selected, pending=pending_only))
        cov = coverage(db, schedule(), today())
        with db.connect() as conn:
            attention = conn.execute("SELECT COUNT(*) AS n FROM plaid_items WHERE status != 'ok'").fetchone()["n"]
            pending_count = conn.execute("SELECT COUNT(*) AS n FROM transactions WHERE source='plaid' AND pending=1 "
                                         + ("AND account_id=?" if selected else ""),
                                         (selected,) if selected else ()).fetchone()["n"]
        # Every scheduled bill, furthest out first so the next one sits at the bottom (like YNAB).
        upcoming = sorted((b for b in bill_status(db, today()) if b["active"] and not b["stale"] and not b["archived_on"]
                           and (not selected or b["account_id"] == selected)),
                          key=lambda b: b["next_due"], reverse=True)
        return {
            "upcoming": upcoming, "colors": COLORS,
            "upcoming_out": sum(b["amount"] for b in upcoming if b["kind"] != "income"),
            "upcoming_in": sum(b["amount"] for b in upcoming if b["kind"] == "income"),
            "settings": settings, "plaid_ready": plaid is not None, "inbox": inbox, "cov": cov,
            "attention": attention, "last_sync": db.get_kv("last_sync_at"),
            "groups": group_by_date(rest, today()), "more": page_after(rest),
            "accounts": plaid_accounts(), "selected": selected, "pending_only": pending_only,
            "pending_count": pending_count,
        }

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request, account: str = "", pending: int = 0):
        return templates.TemplateResponse(request, "home.html", inbox_context(account, pending))

    # ---------- JSON for the native app ----------

    HEX_COLORS = {"blue": "#0a84ff", "green": "#30d158", "orange": "#ff9f0a", "red": "#ff453a", "purple": "#bf5af2",
                  "pink": "#ff375f", "teal": "#40c8e0", "yellow": "#ffd60a", "indigo": "#5e5ce6", "gray": "#8e8e93"}

    def hex_color(value: str | None, default: str = "gray") -> str:
        """A category or bill color as #rrggbb (the page uses CSS variables for the named ones)."""
        v = valid_color(value, default)
        return HEX_COLORS.get(v, v)

    def txn_json(t: dict, dated: bool = False) -> dict:
        """One transaction row as the app draws it, mirroring _txn.html. Amounts come formatted the way
        the page shows them (money out without a sign, money in with a minus, as YNAB does)."""
        tip, cat, bill = t["tip_info"], t["category_info"], t["bill"]
        return {
            "source": t["source"], "id": t["id"], "peer": t.get("transfer_peer"),
            "name": t["display_name"], "pending": bool(t["pending"]),
            "in_transit": t["in_transit"]["other_name"] if t.get("in_transit") else None,
            "category": {"id": cat["id"], "name": cat["name"], "color": hex_color(cat.get("color"))} if cat else None,
            "tip": (f"Tip +{money(tip['amount'])}" + (f" · {tip['pct']}%" if tip.get("pct") is not None else "")) if tip else None,
            "change": None if tip or not t["change"] else f"{'+' if t['change'] > 0 else ''}{money(t['change'])} since pending",
            "transfer": f"{'To' if t['transfer']['out'] else 'From'} {t['transfer']['account']}" if t["transfer"] else None,
            "bill": {"id": bill["id"], "name": bill["name"], "color": hex_color(bill.get("color"), "purple")} if bill else None,
            "account": t["account_label"], "date": t["date"],
            "date_label": shortdate(t["date"]) if dated else None,
            "note": t["note"] or None,
            "amount": money(0 - t["amount"]), "inflow": t["amount"] < 0,
        }

    def groups_json(groups: list) -> list[dict]:
        return [{"label": label, "items": [txn_json(t) for t in items]} for label, items in groups]

    @app.get("/api/inbox")
    async def inbox_json(account: str = "", pending: int = 0, before: str = ""):
        """The Inbox for the native app. With before (the next cursor from a previous answer), just the
        next page of All transactions; the app joins a day split across two pages."""
        if before:
            try:
                d, rid = before.split(":")
                date.fromisoformat(d)
                int(rid)
            except ValueError:
                raise HTTPException(400, "Bad cursor")
            rows = resolve(db, all_list(account_filter(account), before=(d, rid), pending=bool(pending)))
            return {"groups": groups_json(group_by_date(rows, today())), "next": page_after(rows)}
        c = inbox_context(account, pending)
        cov, up = c["cov"], c["upcoming"]
        waiting = [t for t in cov["transit"] if not t["late"]]
        synced = c["last_sync"]
        return {
            "summary": {
                "label": f"Left through {shortdate(cov['payday'].isoformat())}", "amount": money(cov["left"]),
                "ok": bool(cov["ok"]), "cash": money(cov["cash"]), "cards": money(cov["cards"] + cov["pending"]),
                "bills_due": money(cov["due_total"]),
                "transit": (f"Counts {len(waiting)} payment{'' if len(waiting) == 1 else 's'} in transit as posted"
                            if waiting else None),
            },
            "attention": c["attention"],
            "badges": {"inbox": inbox_count(), "accounts": account_issues()},  # the tab badges
            "synced_at": datetime.fromisoformat(synced).timestamp() if synced else None,
            "to_review": [txn_json(t, dated=True) for t in c["inbox"]],
            "scheduled": {
                "count": len(up), "next": f"Next {shortdate(up[-1]['next_due'].isoformat())} · {up[-1]['name']}",
                "out": f"-{money(c['upcoming_out'])}", "in": f"+{money(c['upcoming_in'])}" if c["upcoming_in"] else None,
            } if up else None,
            "accounts": [{"id": a["account_id"], "name": a["pill"], "label": a["label"], "visibility": a["visibility"]}
                         for a in c["accounts"] if a["visibility"] != "archived" or a["account_id"] == c["selected"]],
            "account": c["selected"], "pending_only": c["pending_only"], "pending_count": c["pending_count"],
            "groups": groups_json(c["groups"]), "next": c["more"],
            "posted": [txn_json(t, dated=True) for t in just_posted()],
        }

    def just_posted() -> list[dict]:
        """Pending charges that posted in the last week since the Inbox's Just posted list was last
        dismissed, newest first. Ones back in To review (a much larger amount) are listed there."""
        since = max((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds"),
                    db.get_kv("posted_seen_at") or "")
        return resolve(db, list_transactions(db, where="t.posted_at > ? AND t.reviewed = 1", params=(since,), limit=50))

    @app.post("/api/posted/seen")
    async def posted_seen():
        """The Inbox's Just posted list was dismissed."""
        db.set_kv("posted_seen_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        return {"ok": True}

    @app.get("/api/sidebar")
    async def sidebar():
        """What the Mac app's sidebar shows: the Inbox summary, the last sync, and the everyday
        accounts with their balances. Amounts come formatted, so the app shows them as they are."""
        cov = coverage(db, schedule(), today())
        synced = db.get_kv("last_sync_at")
        return {
            "left": {"label": f"Left through {shortdate(cov['payday'].isoformat())}", "amount": money(cov["left"]),
                     "ok": bool(cov["ok"])},
            "synced_at": datetime.fromisoformat(synced).timestamp() if synced else None,
            "accounts": [{"id": a["account_id"], "name": a["pill"], "balance": money(a["current_balance"]),
                          "type": a.get("type") or ""}
                         for a in plaid_accounts() if a["visibility"] == "everyday"],
        }

    @app.get("/api/transactions/page", response_class=HTMLResponse)
    async def more_transactions(request: Request, before: str, account: str = "", pending: int = 0):
        try:
            d, rid = before.split(":")
            date.fromisoformat(d)
            int(rid)
        except ValueError:
            raise HTTPException(400, "Bad cursor")
        rows = resolve(db, all_list(account_filter(account), before=(d, rid), pending=bool(pending)))
        return templates.TemplateResponse(request, "_rows.html", {
            "groups": group_by_date(rows, today()), "more": page_after(rows)})

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
        keys = merchant_keys()
        return {
            "id": t["id"], "source": t["source"], "display_name": t["display_name"],
            "bank_name": t["name"], "merchant": t["merchant"], "merchant_key": t["merchant_key"],
            "date": t["date"], "amount": t["amount"], "pending": bool(t["pending"]),
            "account": t["account_label"], "note": t["note"] or "",
            "category_id": (t["category_info"] or {}).get("id"),
            "category_is_override": bool(t["category_id"]),
            "name_is_override": bool(t["name_override"]),
            "tip": t["tip_info"]["amount"] if t["tip_info"] else None,
            "tip_is_manual": bool(t["tip"]),  # 0 means "no tip" and counts as not manual
            "bill": t["bill"]["name"] if t["bill"] else None, "reviewed": bool(t["reviewed"]),
            "bill_id": t["bill"]["id"] if t["bill"] else None, "bill_choice": t["bill_id"],
            "details": transaction_details(t),
            "bills": bill_choices(t["amount"] < 0),
            "merchant_count": len(keys.get(t["merchant_key"], [])),
            "similar": similar_merchants(t["merchant_key"], keys),
        }

    # Prefixes that say how someone paid, not who they paid, so they do not make merchants "similar".
    GENERIC_FIRST_WORDS = {"transfer", "sq", "tst", "pos", "paypal", "debit", "purchase", "check", "online",
                           "ach", "deposit", "withdrawal", "web", "card", "recurring", "aplpay", "applepay",
                           "venmo", "zelle", "atm", "fee", "interest", "mobile", "point", "sale", "pp"}

    AMAZON_TRANSACTIONS = "https://www.amazon.com/cpe/yourpayments/transactions"
    APPLE_PURCHASES = "https://reportaproblem.apple.com/?s=6"

    def store_link(t: dict) -> dict | None:
        """A button to the store's own purchase history (Amazon orders, Apple subscriptions and apps)."""
        words = t["merchant_key"].split(" ")
        if t.get("transfer_peer"):
            return None
        if words[0] in ("amazon", "amzn"):
            return {"label": "Open Amazon transactions", "url": AMAZON_TRANSACTIONS}
        # App Store and subscription charges, not Apple Card payments, Apple Cash, or Apple Store retail.
        if words[0] == "apple" and not set(words[1:2]) & {"card", "cash", "store"}:
            return {"label": "Open Apple purchases", "url": APPLE_PURCHASES}
        return None

    def local_time(value: str | None) -> str | None:
        """A Plaid timestamp as Pacific time, or None when missing or a placeholder midnight."""
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None or (dt.hour, dt.minute, dt.second) == (0, 0, 0):
            return None
        return dt.astimezone(PACIFIC).strftime("%-I:%M %p").lower().replace(" ", "\u202f")

    def transaction_details(t: dict) -> dict:
        """Extra facts from Plaid's original record, only the ones that are filled in."""
        try:
            raw = json.loads(t.get("raw") or "{}")
        except ValueError:
            raw = {}
        loc = raw.get("location") or {}
        street = loc.get("address")
        place = ", ".join(x for x in [loc.get("city"), " ".join(x for x in [loc.get("region"), loc.get("postal_code")] if x)] if x)
        maps = None
        name = t["display_name"]
        if loc.get("lat") is not None and loc.get("lon") is not None:
            maps = f"https://maps.apple.com/?ll={loc['lat']},{loc['lon']}&q={quote(name)}"
        elif street or place:
            maps = f"https://maps.apple.com/?q={quote(', '.join(x for x in [name, street, place] if x))}"
        meta = raw.get("payment_meta") or {}
        counterparties = [{"name": c.get("name"), "type": (c.get("type") or "").replace("_", " ")}
                          for c in raw.get("counterparties") or []
                          if c.get("name") and c.get("name") != name]
        def logo_ok(url):
            return url if isinstance(url, str) and url.startswith("https://") and ".plaid.com/" in url else None
        return {
            "time": local_time(raw.get("authorized_datetime")) or local_time(raw.get("datetime")),
            "authorized_date": t.get("authorized_date") if t.get("authorized_date") != t["date"] else None,
            "posted_date": t["date"],
            "street": street, "place": place or None, "store": loc.get("store_number"),
            "country": loc.get("country") if loc.get("country") not in (None, "US") else None,
            "maps": maps,
            "website": raw.get("website"),
            "logo": logo_ok(raw.get("logo_url")),
            "counterparties": counterparties,
            "shop": store_link(t),
            "payee": meta.get("payee"), "payer": meta.get("payer"),
            "reference": meta.get("reference_number"), "processor": meta.get("payment_processor"),
            "check": raw.get("check_number"),
            "original": raw.get("original_description") if raw.get("original_description") != t.get("name") else None,
        }

    def merchant_keys() -> dict[str, list[dict]]:
        """Plaid transactions grouped by merchant key."""
        with db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT rowid, merchant, name FROM transactions WHERE source='plaid'")]
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(merchant_key(r), []).append(r)
        return out

    def similar_merchants(key: str, keys: dict[str, list[dict]]) -> list[dict]:
        """Other merchant keys that start with the same word ("amazon mktpl ab", "amazon prime"),
        so one rename can cover bank descriptions that carry order numbers."""
        first = key.split(" ")[0] if key else ""
        if len(first) < 3 or first in GENERIC_FIRST_WORDS:
            return []
        with db.connect() as conn:
            names = {r["merchant_key"]: r["display_name"] for r in conn.execute(
                "SELECT merchant_key, display_name FROM merchant_rules WHERE display_name IS NOT NULL")}
        out = []
        for k, v in keys.items():
            if k == key or k.split(" ")[0] != first:
                continue
            bank = v[0]["merchant"] or v[0]["name"] or ""
            out.append({"key": k, "count": len(v), "sample": bank, "name": names.get(k) or bank})
        return sorted(out, key=lambda x: -x["count"])[:30]

    def bill_choices(income: bool) -> list[dict]:
        with db.connect() as conn:
            return [{"id": r["id"], "name": r["name"] + (" (archived)" if r["archived_on"] else "")}
                    for r in conn.execute("SELECT id, name, archived_on FROM bills WHERE kind=? "
                                          "ORDER BY archived_on IS NOT NULL, name COLLATE NOCASE",
                                          ("income" if income else "bill",))]

    @app.post("/api/transactions/{source}/{tid}")
    async def edit_transaction(source: str, tid: str, request: Request):
        t = find_txn(source, tid)
        body = await request.json()
        scope = "this" if body.get("scope") == "this" else "merchant"
        # Similar merchant keys the user chose to include (checked against the real list).
        also = []
        if scope == "merchant" and body.get("similar"):
            allowed = {s["key"] for s in similar_merchants(t["merchant_key"], merchant_keys())}
            also = [k for k in body["similar"] if k in allowed]
        if "display_name" in body:
            name = str(body["display_name"] or "").strip()[:80]
            bank = (t["merchant"] or t["name"] or "").strip()
            new = None if name in ("", bank) else name
            with db.connect() as conn:
                if scope == "this":
                    conn.execute("UPDATE transactions SET name_override=? WHERE source=? AND id=?", (new, source, tid))
            if scope == "merchant":
                for key in [t["merchant_key"], *also]:
                    set_merchant_rule(db, key, display_name=new)
                    clear_name_overrides(key)
        if "category_id" in body:
            cid = body["category_id"]
            with db.connect() as conn:
                if cid is not None and not conn.execute("SELECT 1 FROM categories WHERE id=?", (int(cid),)).fetchone():
                    raise HTTPException(400, "Unknown category")
            if scope == "this":
                with db.connect() as conn:
                    conn.execute("UPDATE transactions SET category_id=? WHERE source=? AND id=?",
                                 (int(cid) if cid is not None else None, source, tid))
            else:
                for key in [t["merchant_key"], *also]:
                    set_merchant_rule(db, key, category_id=int(cid) if cid is not None else None)
        if "tip" in body:  # a tip entered by hand; empty or 0 goes back to automatic
            try:
                tip = round(abs(num(body["tip"])), 2) or None
            except (TypeError, ValueError):
                raise HTTPException(400, "Enter the tip as an amount")
            if tip is not None and tip >= abs(t["amount"]):
                raise HTTPException(400, "The tip must be less than the total")
            if tip is None and t["tip_info"] and not t["tip_info"]["manual"]:
                tip = 0  # clearing an automatic tip: remember "no tip" so it does not come back
            with db.connect() as conn:
                conn.execute("UPDATE transactions SET tip=? WHERE source=? AND id=?", (tip, source, tid))
        if "bill_id" in body:  # "" or None: automatic; 0: not a bill; a bill id: that bill
            raw = body["bill_id"]
            bid = None if raw in (None, "", "auto") else int(raw)
            with db.connect() as conn:
                if bid and not conn.execute("SELECT 1 FROM bills WHERE id=?", (bid,)).fetchone():
                    raise HTTPException(400, "Unknown bill")
                conn.execute("UPDATE transactions SET bill_id=? WHERE source=? AND id=?", (bid, source, tid))
        if "note" in body:
            note = str(body["note"] or "").strip()[:500] or None
            with db.connect() as conn:
                conn.execute("UPDATE transactions SET note=? WHERE source=? AND id=?", (note, source, tid))
        return {"ok": True}

    def clear_name_overrides(key: str) -> None:
        """A merchant-wide name replaces one-off names on that merchant's transactions."""
        with db.connect() as conn:
            rows = conn.execute("SELECT rowid, merchant, name FROM transactions "
                                "WHERE source='plaid' AND name_override IS NOT NULL").fetchall()
            ids = [(r["rowid"],) for r in rows if merchant_key(dict(r)) == key]
            conn.executemany("UPDATE transactions SET name_override=NULL WHERE rowid=?", ids)

    @app.post("/api/rows")
    async def render_rows(request: Request):
        """Fresh HTML for transaction rows already on the page, so a save does not reload the page."""
        body = await request.json()
        wanted = {str(r["id"]): bool(r.get("review")) for r in body.get("rows", [])[:900]}
        dated = {str(r["id"]) for r in body.get("rows", [])[:900] if r.get("dated")}
        if not wanted:
            return {"rows": {}}
        marks = ",".join("?" * len(wanted))
        rows = resolve(db, list_transactions(db, where=f"t.id IN ({marks})", params=tuple(wanted), limit=len(wanted)))
        macro = templates.env.get_template("_txn.html").make_module({}).txn_row
        return {"rows": {t["id"]: str(macro(t, review=wanted[t["id"]], dated=t["id"] in dated)) for t in rows}}

    @app.get("/api/merchant-names")
    async def merchant_names():
        """Names in use, most common first, for suggestions while typing a name."""
        rows = resolve(db, list_transactions(db, limit=100000))
        counts: dict[str, int] = {}
        for t in rows:
            counts[t["display_name"]] = counts.get(t["display_name"], 0) + 1
        return {"names": [n for n, _ in sorted(counts.items(), key=lambda x: (-x[1], x[0].lower()))][:2000]}

    @app.get("/accounts", response_class=HTMLResponse)
    async def accounts_page(request: Request):
        with db.connect() as conn:
            items = [dict(r) for r in conn.execute(
                "SELECT item_id, institution, status, error_code, update_status, last_sync_at, products, investments, "
                "(SELECT COUNT(*) FROM accounts a WHERE a.item_id = plaid_items.item_id AND a.type='investment') AS invest_accounts "
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
            "push_clear_others": db.get_kv("push_clear_others") == "1",
            "categories": categories(db), "colors": COLORS, "rules": merchant_rule_list(),
            "kind_labels": KIND_LABELS, "kind_pills": KIND_PILLS, "server_update": server_update(),
        })

    def plaid_updates_line() -> str | None:
        """How new Plaid data arrives, for Accounts › Connections."""
        if plaid is None or not settings.plaid_webhook_url:
            return None
        at = db.get_kv("plaid_webhook_at")
        if at:
            return f"Plaid tells the app when it has new transactions (last {templates.env.filters['local'](at)}), and it checks every hour too."
        return "Plaid tells the app when it has new transactions (none received yet), and it checks every hour too."

    @app.get("/api/accounts")
    async def accounts_json():
        """The Accounts page for the native app: accounts, bank connections, categories, renamed
        merchants, settings, and the server's version, with the lines the page shows."""
        with db.connect() as conn:
            items = [dict(r) for r in conn.execute(
                "SELECT item_id, institution, status, error_code, update_status, last_sync_at, products, investments, "
                "(SELECT COUNT(*) FROM accounts a WHERE a.item_id = plaid_items.item_id AND a.type='investment') AS invest_accounts "
                "FROM plaid_items ORDER BY institution")]
            total = conn.execute("SELECT COUNT(*) AS n FROM transactions WHERE source='plaid'").fetchone()["n"]
            ynab_total = conn.execute("SELECT COUNT(*) AS n FROM transactions WHERE source='ynab'").fetchone()["n"]
        local = templates.env.filters["local"]
        accounts = []
        for a in plaid_accounts():
            parts = [a.get("institution") or "", (a.get("subtype") or a.get("type") or "").capitalize()]
            if a.get("short_name"):
                parts.append(f"pill: {a['short_name']}")
            if a.get("display_name"):
                parts.append(f"bank name: {a['name']}")
            accounts.append({
                "id": a["account_id"], "label": a["label"], "name": a["name"], "title": a.get("display_name") or "",
                "short": a.get("short_name") or "", "visibility": a["visibility"],
                "subtitle": " · ".join(p for p in parts if p), "balance": money(a.get("current_balance")),
                "warn": "Archived but the balance is not zero, so it still counts in coverage."
                        if a["visibility"] == "archived" and abs(a.get("current_balance") or 0) >= 0.005 else None,
            })
        connections = []
        for i in items:
            parts = [f"Synced {local(i['last_sync_at'])}" if i["last_sync_at"] else "Not synced yet"]
            if i["update_status"] and i["update_status"] != "HISTORICAL_UPDATE_COMPLETE":
                parts.append("history loading")
            if i["products"] == "investments":
                parts.append("investments only")
            elif i["investments"] == "on":
                parts.append("with investments")
            elif i["investments"] == "unsupported":
                parts.append("investments not available here")
            ok = i["status"] == "ok"
            connections.append({
                "id": i["item_id"], "name": i["institution"] or "Unnamed connection", "ok": ok,
                "subtitle": " · ".join(parts),
                "add_investments": ("Allow investments" if i["investments"] == "consent" else "Add investments")
                    if ok and i["invest_accounts"] and i["investments"] not in ("on", "unsupported") and i["products"] != "investments" else None,
            })
        if not ynab.configured:
            ynab_line = "YNAB: not set up"
        elif db.get_kv("ynab_error"):
            ynab_line = f"YNAB: last sync failed ({db.get_kv('ynab_error')})"
        elif db.get_kv("ynab_last_sync_at"):
            ynab_line = f"YNAB: {ynab_total} transactions imported, synced {local(db.get_kv('ynab_last_sync_at'))}"
        else:
            ynab_line = "YNAB: waiting for the first sync"
        u = server_update()
        version = None
        if u:
            if u["state"] == "updating":
                version = (f"Version {u.get('version')} · installing {u.get('target_version')} ({u.get('target_rev')}), "
                           f"started {local(u.get('at'))}")
            elif u["state"] == "failed":
                version = (f"Version {u.get('version')} ({u.get('rev')}) · {u.get('target_version')} "
                           f"({u.get('target_rev')}) didn't install, so this version keeps running.")
            else:
                version = f"Version {u.get('version')} ({u.get('rev')}), installed {local(u.get('at'))}"
        banners = []
        if not settings.cf_access_enabled:
            banners.append("The Cloudflare Access check inside the app is off. Set cf_access_team_domain and cf_access_aud in the options.")
        if plaid is None:
            banners.append("Plaid is not set up. Add the client ID and secret in the options.")
        return {
            "banners": banners, "plaid_ready": plaid is not None,
            "accounts": accounts, "connections": connections, "ynab": ynab_line,
            "plaid_updates": plaid_updates_line(),
            "categories": [{"id": c["id"], "name": c["name"], "color": hex_color(c.get("color")),
                            "color_name": valid_color(c.get("color")),
                            "kind": c.get("kind") if c.get("kind") in KIND_PILLS else "spending",
                            "kind_pill": KIND_PILLS.get(c.get("kind") or "spending", "Spending")} for c in categories(db)],
            "kinds": [{"id": k, "label": v} for k, v in KIND_LABELS.items()],
            "rules": [{"key": r["merchant_key"], "name": r["display_name"] or r["bank"], "bank": r["bank"],
                       "count": r["count"],
                       "category": {"id": r["category"]["id"], "name": r["category"]["name"],
                                    "color": hex_color(r["category"].get("color"))} if r["category"] else None}
                      for r in merchant_rule_list()],
            "push_clear_others": db.get_kv("push_clear_others") == "1",
            "total": f"{total} transactions stored", "version": version, "updating": bool(u and u["state"] == "updating"),
            "colors": [{"name": c, "hex": HEX_COLORS[c]} for c in COLORS],
        }

    def server_update() -> dict | None:
        """On the standalone server: the running version and any update in progress, as the updater
        last wrote them (data/update.json). None under Home Assistant."""
        try:
            info = json.loads((settings.data_dir / "update.json").read_text())
        except (OSError, ValueError):
            return None
        return info if isinstance(info, dict) and info.get("state") else None

    @app.get("/api/server-update")
    async def server_update_status():
        return server_update() or {}

    def merchant_rule_list() -> list[dict]:
        """Merchant renames and categories, with how many transactions each covers."""
        keys = merchant_keys()
        cats = {c["id"]: c for c in categories(db)}
        with db.connect() as conn:
            rules = [dict(r) for r in conn.execute(
                "SELECT * FROM merchant_rules WHERE display_name IS NOT NULL OR category_id IS NOT NULL")]
        out = []
        for r in rules:
            rows = keys.get(r["merchant_key"], [])
            bank = (rows[0]["merchant"] or rows[0]["name"]) if rows else r["merchant_key"]
            out.append({**r, "count": len(rows), "bank": bank, "category": cats.get(r["category_id"])})
        return sorted(out, key=lambda r: (r["display_name"] or r["bank"] or "").lower())

    @app.post("/api/rules/delete")
    async def delete_rule(request: Request):
        """Undo a merchant rename (and its category): transactions go back to the bank's name."""
        body = await request.json()
        with db.connect() as conn:
            conn.execute("DELETE FROM merchant_rules WHERE merchant_key=?", (str(body.get("key") or ""),))
        return {"ok": True}

    @app.post("/api/accounts/{account_id}/rename")
    async def rename_account(account_id: str, request: Request):
        body = await request.json()
        name = str(body.get("name") or "").strip()[:60] or None
        short = str(body.get("short_name") or "").strip()[:24] or None
        visibility = body.get("visibility") if body.get("visibility") in VISIBILITY else "everyday"
        with db.connect() as conn:
            cur = conn.execute("UPDATE accounts SET display_name=?, short_name=?, visibility=? "
                               "WHERE account_id=? AND source='plaid'", (name, short, visibility, account_id))
            if not cur.rowcount:
                raise HTTPException(404, "Unknown account")
        return {"ok": True}

    @app.post("/api/categories")
    async def save_category(request: Request):
        body = await request.json()
        name = str(body.get("name") or "").strip()[:40]
        color = valid_color(body.get("color"))
        kind = body.get("kind") if body.get("kind") in KINDS else "spending"
        if not name:
            raise HTTPException(400, "Name the category")
        try:
            with db.connect() as conn:
                if body.get("id"):
                    conn.execute("UPDATE categories SET name=?, color=?, kind=? WHERE id=?",
                                 (name, color, kind, int(body["id"])))
                    new_id = int(body["id"])
                else:
                    sort = conn.execute("SELECT COALESCE(MAX(sort), 0) + 1 FROM categories").fetchone()[0]
                    new_id = conn.execute("INSERT INTO categories (name, color, sort, kind) VALUES (?, ?, ?, ?)",
                                          (name, color, sort, kind)).lastrowid
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
        everything = bill_status(db, today)
        archived = [b for b in everything if b["archived_on"]]
        everything = [b for b in everything if not b["archived_on"]]
        bills = [b for b in everything if b["kind"] != "income"]
        income = [b for b in everything if b["kind"] == "income"]
        cov = coverage(db, schedule(), today)
        ids = [i["id"] for i in cov["transit"]]
        transit_txns = {t["id"]: t for t in resolve(db, list_transactions(
            db, where=f"t.id IN ({','.join('?' * len(ids))})", params=tuple(ids), limit=0))} if ids else {}
        return templates.TemplateResponse(request, "bills.html", {
            "bills": bills, "income": income, "archived": archived, "colors": COLORS, "schedule": schedule(), "accounts": plaid_accounts(),
            "cov": cov, "transit_accounts": transit_options(db), "transit_txns": transit_txns, "today_iso": today.isoformat(),
            "monthly_total": sum(b["monthly_equivalent"] for b in bills if b["active"] and not b["stale"]),
            "monthly_income": sum(b["monthly_equivalent"] for b in income if not b["stale"]),
        })

    # ---------- Bills JSON for the native app ----------

    def bill_json(b: dict, labels: dict[str, dict]) -> dict:
        """One bill or income, with the line the page shows under its name and the form's values."""
        income = b["kind"] == "income"
        cadence = cadence_text(b["cadence"])
        parts = [cadence]
        if not b["archived_on"]:
            if b.get("window_opens"):
                parts.append(f"due by {shortdate(b['next_due'].isoformat())} (pay from {shortdate(b['window_opens'].isoformat())})")
            elif b.get("next_due"):
                parts.append(f"next {shortdate(b['next_due'].isoformat())}")
        if b.get("last_date"):
            parts.append(f"last {shortdate(b['last_date'])} {money(b['last_amount'])}")
        else:
            parts.append("none found yet" if income else "no charge found")
        if not b["active"] and not income and not b["archived_on"]:
            parts.append("not counted")
        note = None
        if b["archived_on"]:
            note = f"Archived {shortdate(b['archived_on'])}"
        warn = None
        if not b["archived_on"] and b.get("stale"):
            warn = f"None since {shortdate(b['last_date'])}. " + ("Archive it if it ended." if income else "Not counted; archive it if it ended.")
        account = labels.get(b.get("account_id") or "")
        choice, every = cadence_parts(b["cadence"])
        return {
            "id": b["id"], "name": b["name"], "kind": b["kind"], "color": hex_color(b.get("color"), "green" if income else "purple"),
            "color_name": valid_color(b.get("color"), "green" if income else "purple"),
            "amount": f"{'≈ ' if b.get('varies') else ''}{'+' if income else ''}{money(b['amount'])}",
            "subtitle": " · ".join(parts), "account": account["label"] if account else None, "note": note, "warn": warn,
            "inactive": not ((b["active"] or income) and not b.get("stale") and not b["archived_on"]),
            "archived": bool(b["archived_on"]),
            "form": {  # what the editor starts with
                "kind": b["kind"], "name": b["name"], "amount": round(b["amount"] or 0, 2), "cadence": choice,
                "every": every or None, "due_day": b.get("due_day"), "match": b.get("match") or "",
                "next_due": b.get("next_due_override"), "account_id": b.get("account_choice") or "",
                "auto_account": (labels.get(b.get("auto_account_id") or "") or {}).get("label"),
                "varies": bool(b.get("varies")), "window_months": b.get("window_months"),
                "is_payday": bool(b.get("is_payday")), "active": bool(b["active"]),
            },
        }

    @app.get("/api/bills")
    async def bills_json():
        """The Bills page for the native app: coverage, payments in transit, what's due before payday,
        paydays, bills, income, and archived ones, with each one's editor values."""
        today_ = today()
        everything = bill_status(db, today_)
        archived = [b for b in everything if b["archived_on"]]
        everything = [b for b in everything if not b["archived_on"]]
        bills = [b for b in everything if b["kind"] != "income"]
        income = [b for b in everything if b["kind"] == "income"]
        cov = coverage(db, schedule(), today_)
        accts = plaid_accounts()
        labels = {a["account_id"]: a for a in accts}
        ids = [i["id"] for i in cov["transit"]]
        txns = {t["id"]: t for t in resolve(db, list_transactions(
            db, where=f"t.id IN ({','.join('?' * len(ids))})", params=tuple(ids), limit=0))} if ids else {}
        options = transit_options(db)
        transit = []
        for t in cov["transit"]:
            tx = txns.get(t["id"])
            status = ("No match after 7 days · not counted" if t["late"] else
                      f"Pending in {t['other_name']}" if t["other_pending"] else f"Waiting on {t['other_name']}")
            transit.append({
                "id": t["id"], "amount": money(t["amount"]), "late": t["late"], "status": status,
                "title": f"{t['account_name']} to {t['other_name']}" if t["out"] else f"{t['other_name']} to {t['account_name']}",
                "subtitle": shortdate(t["date"]) + (f" · {tx['display_name']}" if tx else ""),
                "sheet_title": f"{money(t['amount'])} {'to' if t['out'] else 'from'} {t['other_name']}",
                "hint": (f"{t['account_name']} shows this payment or transfer on {shortdate(t['date'])}"
                         f"{' (pending)' if t['pending'] else ''}. Until the matching {'charge' if t['out'] else 'deposit'} "
                         "shows up in the other account, balances count it as if it had, for up to 7 days."
                         + ("" if t["chosen"] else " The other account is worked out from where past transfers like this one went.")),
                "other": t["other"], "options": [o for o in options if o["id"] != t["account"]],
                "txn": txn_json(tx, dated=True) if tx else None,
            })
        sched = schedule()
        if cov["paycheck"]:
            p = cov["paycheck"]
            paydays = {"paycheck": {"id": p["id"], "name": p["name"], "color": hex_color(p.get("color"), "green"),
                                    "text": f"{cadence_text(p['cadence'])} · next {shortdate(cov['payday'].isoformat())}"}}
        else:
            days = sched.get("days") or [10, 25]
            if sched.get("kind") == "biweekly":
                title, text = "Every two weeks", f"Including {shortdate(sched.get('anchor'))}"
            else:
                title = "Twice a month"
                text = "On the " + " and ".join(str(d) for d in days)
                if sched.get("start") and sched["start"] > today_.isoformat():
                    text += f" · starting {shortdate(sched['start'])}"
            paydays = {"title": title, "text": f"{text} · next {shortdate(cov['payday'].isoformat())}",
                       "schedule": {"kind": sched.get("kind") or "semimonthly", "days": days,
                                    "start": sched.get("start"), "anchor": sched.get("anchor")}}
        def due_line(b):
            parts = [cadence_text(b["cadence"])]
            if b.get("window_opens"):
                parts.append(f"due by {shortdate(b['next_due'].isoformat())}")
            if b.get("varies"):
                parts.append(f"average of the last {b['samples']}" if b.get("samples") else "no charges found yet")
            if b.get("account_id") in labels:
                parts.append(labels[b["account_id"]]["pill"])
            return " · ".join(parts)
        late = [t for t in cov["transit"] if t["late"]]
        counted = [t for t in cov["transit"] if not t["late"]]
        monthly_total = sum(b["monthly_equivalent"] for b in bills if b["active"] and not b["stale"])
        monthly_income = sum(b["monthly_equivalent"] for b in income if not b["stale"])
        return {
            "coverage": {
                "title": f"Coverage through {shortdate(cov['payday'].isoformat())}",
                "rows": [
                    {"label": "Cash", "note": f"incl. {money(cov['transit_cash'])} in transit" if cov.get("transit_cash") else None,
                     "value": money(cov["cash"])},
                    {"label": "Card balances", "note": f"incl. {money(cov['transit_cards'])} in transit" if cov.get("transit_cards") else None,
                     "value": f"-{money(cov['cards'])}"},
                    {"label": "Pending card charges", "note": None, "value": f"-{money(cov['pending'])}"},
                    {"label": "Bills due before payday", "note": None, "value": f"-{money(cov['due_total'])}"},
                ],
                "left": money(cov["left"]), "ok": bool(cov["ok"]),
                "footer": f"Non-monthly bills average {money(cov['annual_reserve'])} a month. "
                          f"All bills together average {money(monthly_total)} a month.",
            },
            "transit": {"items": transit, "late": f"{len(late)} with no match after 7 days" if late else None,
                        "total": money(sum(t["amount"] for t in counted)) if counted else None} if transit else None,
            "due": {"summary": f"{len(cov['due'])} bill{'' if len(cov['due']) == 1 else 's'} · {money(cov['due_total'])}",
                    "items": [{"id": b["id"], "name": b["name"], "color": hex_color(b.get("color"), "purple"),
                               "month": b["next_due"].strftime("%b"), "day": b["next_due"].day,
                               "subtitle": due_line(b),
                               "amount": f"{'≈ ' if b.get('varies') else ''}{money(b['amount'])}"} for b in cov["due"]]}
                   if cov["due"] else None,
            "paydays": paydays,
            "bills": [bill_json(b, labels) for b in bills],
            "income": [bill_json(b, labels) for b in income],
            "income_monthly": f"≈ +{money(monthly_income)} a month" if income else None,
            "archived": [bill_json(b, labels) for b in archived],
            "accounts": [{"id": a["account_id"], "label": a["label"], "visibility": a["visibility"]}
                         for a in accts if a.get("type") in ("credit", "depository")],
            "colors": [{"name": c, "hex": HEX_COLORS[c]} for c in COLORS],
        }

    @app.get("/api/bills/prefill")
    async def bill_prefill(source: str, id: str):
        """A new bill's (or income's) starting values from a transaction, for the native editor."""
        t = find_txn(source, id)
        d = date.fromisoformat(t["date"])
        return {"kind": "income" if t["amount"] < 0 else "bill", "name": t["display_name"], "amount": round(abs(t["amount"]), 2),
                "cadence": "monthly", "every": None, "due_day": d.day, "match": t["merchant_key"], "next_due": None,
                "account_id": t["account_id"], "auto_account": None, "varies": False, "window_months": None,
                "is_payday": False, "active": True, "from_txn": t["id"]}

    @app.get("/api/bills/form", response_class=HTMLResponse)
    async def bill_form_from_transaction(request: Request, source: str, id: str):
        """A new bill (or income) form filled in from a transaction."""
        t = find_txn(source, id)
        d = date.fromisoformat(t["date"])
        prefill = {"name": t["display_name"], "match": t["merchant_key"], "amount": abs(t["amount"]),
                   "kind": "income" if t["amount"] < 0 else "bill", "cadence": "monthly", "due_day": d.day,
                   "account_choice": t["account_id"], "from_txn": t["id"]}
        macro = templates.env.get_template("_bill_form.html").make_module(
            {"accounts": plaid_accounts(), "colors": COLORS}).bill_form
        return HTMLResponse(str(macro(None, prefill)))

    def bill_fields(body: dict) -> dict:
        try:
            out = {
                "name": str(body["name"]).strip()[:80],
                "match": normalize(str(body.get("match") or body["name"])),
                "amount": round(abs(num(body.get("amount"))), 2),
                "cadence": make_cadence(str(body.get("cadence") or ""), body.get("every")),
                "due_day": int(body["due_day"]) if body.get("due_day") else None,
                "next_due_override": str(date.fromisoformat(body["next_due"])) if body.get("next_due") else None,
                "active": 1 if body.get("active", True) else 0,
                "color": valid_color(body.get("color"), "purple"),
                "varies": 1 if body.get("varies") else 0,
                "account_id": str(body["account_id"]) if body.get("account_id") else None,
                "kind": "income" if body.get("kind") == "income" else "bill",
                "is_payday": 1 if body.get("is_payday") and body.get("kind") == "income" else 0,
                "window_months": int(body["window_months"]) if body.get("date_varies") and str(body.get("window_months") or "").strip() else None,
            }
        except (KeyError, ValueError, TypeError):
            raise HTTPException(400, "Name and amount are required; dates use YYYY-MM-DD")
        if not out["name"] or not out["match"] or (out["due_day"] is not None and not 1 <= out["due_day"] <= 31):
            raise HTTPException(400, "Check the name and day of month")
        if out["account_id"] and out["account_id"] not in {a["account_id"] for a in plaid_accounts()}:
            raise HTTPException(400, "Unknown account")
        if not out["varies"] and out["amount"] <= 0:
            raise HTTPException(400, "Enter the amount, or mark it as varying")
        if out["window_months"] is not None:
            if out["cadence"] == "biweekly" or not 1 <= out["window_months"] < step_months(out["cadence"]):
                raise HTTPException(400, "The payment window must be at least a month and shorter than the time between payments")
            if not out["next_due_override"]:
                raise HTTPException(400, "Enter the due date for a bill whose date varies")
        if out["kind"] == "income" and body.get("color") in (None, ""):
            out["color"] = "green"
        return out

    @app.post("/api/bills")
    async def save_bill(request: Request):
        body = await request.json()
        f = bill_fields(body)
        with db.connect() as conn:
            if body.get("id"):
                bill_id = int(body["id"])
                conn.execute("UPDATE bills SET name=?, match=?, amount=?, cadence=?, due_day=?, next_due_override=?, "
                             "active=?, color=?, varies=?, account_id=?, kind=?, is_payday=?, window_months=? WHERE id=?",
                             (*f.values(), bill_id))
            else:
                bill_id = conn.execute(
                    "INSERT INTO bills (name, match, amount, cadence, due_day, next_due_override, active, color, "
                    "varies, account_id, kind, is_payday, window_months) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    tuple(f.values())).lastrowid
            if f["is_payday"]:  # only one paycheck sets paydays
                conn.execute("UPDATE bills SET is_payday=0 WHERE id != ?", (bill_id,))
            if body.get("from_txn"):  # made from a transaction: that transaction belongs to it
                conn.execute("UPDATE transactions SET bill_id=? WHERE source='plaid' AND id=?",
                             (bill_id, str(body["from_txn"])))
        return {"ok": True, "id": bill_id}

    @app.post("/api/bills/{bill_id}/archive")
    async def archive_bill(bill_id: int, request: Request):
        """Archive (keep tagging charges up to today, drop from coverage and Scheduled) or restore."""
        body = await request.json()
        with db.connect() as conn:
            if body.get("restore"):
                conn.execute("UPDATE bills SET archived_on=NULL WHERE id=?", (bill_id,))
            else:
                conn.execute("UPDATE bills SET archived_on=?, is_payday=0 WHERE id=?", (today().isoformat(), bill_id))
        return {"ok": True}

    @app.post("/api/bills/{bill_id}/delete")
    async def delete_bill(bill_id: int):
        with db.connect() as conn:
            row = conn.execute("SELECT match, source FROM bills WHERE id=?", (bill_id,)).fetchone()
            if row and row["source"] == "detected":
                conn.execute("INSERT OR IGNORE INTO bills_dismissed VALUES (?)", (row["match"],))
            conn.execute("DELETE FROM bills WHERE id=?", (bill_id,))
            conn.execute("UPDATE transactions SET bill_id=NULL WHERE bill_id=?", (bill_id,))
        return {"ok": True}

    @app.post("/api/transit")
    async def save_transit(request: Request):
        """The other account of a payment in transit, or "" when it isn't a transfer between my accounts."""
        body = await request.json()
        txn = str(body.get("id") or "")
        account = body.get("account")
        with db.connect() as conn:
            if not conn.execute("SELECT 1 FROM transactions WHERE source='plaid' AND id=?", (txn,)).fetchone():
                raise HTTPException(404, "Transaction not found")
            if account and not conn.execute("SELECT 1 FROM accounts WHERE source='plaid' AND account_id=?",
                                            (account,)).fetchone():
                raise HTTPException(400, "Pick an account")
            conn.execute("INSERT INTO transit_choices VALUES (?, ?) ON CONFLICT(txn_id) DO UPDATE SET "
                         "account_id=excluded.account_id", (txn, account or ""))
        return {"ok": True}

    @app.post("/api/bills/detect")
    async def detect_bills():
        """Recurring charges not on the list yet, shown for confirmation. Nothing is added here."""
        found = new_candidates(db, detect(db, datetime.now(PACIFIC).date()))
        html = templates.env.get_template("_bill_candidates.html").render(found=found)
        items = [{"match": c.match, "name": c.name, "amount": f"{'≈ ' if c.varies else ''}{money(c.amount)}",
                  "subtitle": f"{cadence_text(c.cadence)} · {c.count} charges · last {shortdate(c.last_seen.isoformat())}"}
                 for c in found]
        return {"count": len(found), "html": html, "items": items}

    @app.post("/api/bills/add-detected")
    async def add_detected(request: Request):
        """Add the recurring charges ticked in the confirmation sheet; "Not a bill" ones are never suggested again."""
        body = await request.json()
        add = set(body.get("add") or [])
        found = new_candidates(db, detect(db, datetime.now(PACIFIC).date()))
        added = save_detected(db, [c for c in found if c.match in add])
        ignore = set(body.get("ignore") or [])
        with db.connect() as conn:
            for c in found:
                if c.match in ignore and c.match not in add:
                    conn.execute("INSERT OR IGNORE INTO bills_dismissed VALUES (?)", (c.match,))
        return {"added": added}

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
            if body.get("start"):
                try:
                    sched["start"] = date.fromisoformat(body["start"]).isoformat()
                except ValueError:
                    raise HTTPException(400, "Pick a valid first payday")
        db.set_kv("payday_schedule", json.dumps(sched))
        return {"ok": True}

    # ---------- search and export ----------

    def search_query(request: Request) -> dict:
        """Search filters from the query string. Accounts and categories can be chosen several at a time."""
        qp = request.query_params
        q = {k: v.strip() for k, v in qp.items() if k not in ("account", "category") and v and v.strip()}
        accounts = [a for a in qp.getlist("account") if a]
        cats = [c for c in qp.getlist("category") if c]
        if accounts:
            q["account"] = accounts
        if cats:
            q["category"] = cats
        return q

    def search(q: dict) -> list[dict]:
        where, params = [], []
        accounts = q.get("account") or []
        if isinstance(accounts, str):
            accounts = [accounts]
        if accounts:
            where.append(f"t.account_id IN ({','.join('?' * len(accounts))})")
            params += accounts
        if q.get("start"):
            where.append("t.date >= ?")
            params.append(q["start"])
        if q.get("end"):
            where.append("t.date <= ?")
            params.append(q["end"])
        for key, op in (("min", ">="), ("max", "<=")):
            if q.get(key):
                try:
                    params.append(abs(num(q[key])))
                    where.append(f"ABS(t.amount) {op} ?")
                except ValueError:
                    pass
        rows = resolve(db, list_transactions(db, where=" AND ".join(where), params=tuple(params), limit=0))
        if q.get("q"):
            needle = q["q"].lower()
            rows = [t for t in rows if needle in " ".join(
                [t["display_name"], t["name"] or "", t["merchant"] or "", t["note"] or "",
                 t["bill"]["name"] if t["bill"] else ""]).lower()]
        cats = q.get("category") or []
        if isinstance(cats, str):
            cats = [cats]
        if cats:
            rows = [t for t in rows if str((t["category_info"] or {}).get("id")) in cats]
        if q.get("flow") == "out":
            rows = [t for t in rows if t["amount"] > 0]
        elif q.get("flow") == "in":
            rows = [t for t in rows if t["amount"] < 0]
        if q.get("noted"):
            rows = [t for t in rows if (t["note"] or "").strip()]
        if q.get("billed"):
            rows = [t for t in rows if t["bill"]]
        if q.get("tipped"):
            rows = [t for t in rows if t["tip_info"]]
        if q.get("pending"):
            rows = [t for t in rows if t["pending"]]
        if q.get("changed"):  # final amount differs from pending, not counting tips
            rows = [t for t in rows if t["change"]]
        if q.get("transfers"):
            rows = [t for t in rows if is_transfer(t)]
        return rows

    def quick_ranges(today_: date, q: dict) -> dict:
        """Month (Jan to Dec) and year (last five) picks. A month and a year together mean that
        month of that year; the active picks are read back from the From and To dates."""
        active_month = active_year = None
        try:
            start = date.fromisoformat(q.get("start", ""))
            end = date.fromisoformat(q.get("end", ""))
        except ValueError:
            start = end = None
        if start and end and start.day == 1:
            if start.year == end.year and start.month == end.month and \
                    end.day == calendar.monthrange(end.year, end.month)[1]:
                active_month, active_year = start.month, start.year
            elif (start.month, end.month, end.day, start.year) == (1, 12, 31, end.year):
                active_year = start.year
        months = [{"n": m, "label": calendar.month_abbr[m], "on": m == active_month} for m in range(1, 13)]
        years = [{"n": y, "label": str(y), "on": y == active_year} for y in range(today_.year, today_.year - 5, -1)]
        return {"months": months, "years": years}

    def is_transfer(t: dict) -> bool:
        return bool(t.get("transfer")) or (t["category_info"] or {}).get("name") == TRANSFER_CATEGORY

    SORTS = {"date": "Newest first", "oldest": "Oldest first", "name": "Merchant, A to Z",
             "largest": "Largest amount", "smallest": "Smallest amount"}

    def search_context(request: Request) -> dict:
        """Search results and filters, for the page and for the app's JSON (/api/search)."""
        q = search_query(request)
        sort = q.pop("sort", "date")
        sort = sort if sort in SORTS else "date"
        rows = search(q) if q else resolve(db, list_transactions(db, limit=100))
        if sort == "oldest":
            rows.reverse()
        elif sort == "name":
            rows.sort(key=lambda t: (t["display_name"].casefold(), t["date"]))
        elif sort in ("largest", "smallest"):
            rows.sort(key=lambda t: abs(t["amount"]), reverse=sort == "largest")
        more = len(rows) > 1000
        # Totals leave out money moving between your own accounts, unless you searched by category
        # or asked for transfers.
        counted = rows if q.get("category") or q.get("transfers") else [t for t in rows if not is_transfer(t)]
        out = sum(t["amount"] for t in counted if t["amount"] > 0)
        inflow = -sum(t["amount"] for t in counted if t["amount"] < 0)
        return {
            "q": q, "count": len(rows), "truncated": more, "sort": sort, "sorts": SORTS,
            # By date the list is grouped by day; any other order is one list with dates on the rows.
            "groups": group_by_date(rows[:1000], today()) if sort in ("date", "oldest") else [("", rows[:1000])],
            "dated": sort not in ("date", "oldest"),
            "accounts": plaid_accounts(), "categories": categories(db), "searched": bool(q),
            "total_out": out, "total_in": inflow, "export_query": urlencode(q, doseq=True),
            "excluded": len(rows) - len(counted), "quick": quick_ranges(today(), q),
        }

    @app.get("/search", response_class=HTMLResponse)
    async def search_page(request: Request):
        return templates.TemplateResponse(request, "search.html", search_context(request))

    @app.get("/api/search")
    async def search_json(request: Request):
        """Search for the native app: the same filters as the page (in the address), the results by
        day (or one dated list for other orders), the totals line, and the filter choices."""
        c = search_context(request)
        excluded = c["excluded"]
        return {
            "searched": c["searched"],
            "title": (f"{c['count']} result{'' if c['count'] == 1 else 's'}" if c["searched"] else "Latest transactions"),
            "sums": (f"out {money(c['total_out'])} · in {money(c['total_in'])}"
                     + (f" · not counting {excluded} transfer{'' if excluded == 1 else 's'}" if excluded else ""))
                    if c["searched"] else None,
            "truncated": c["truncated"], "sort": c["sort"],
            "sorts": [{"id": k, "label": v} for k, v in SORTS.items()],
            "groups": [{"label": label, "items": [txn_json(t, dated=c["dated"]) for t in items]} for label, items in c["groups"]],
            "accounts": [{"id": a["account_id"], "name": a["pill"], "label": a["label"], "visibility": a["visibility"]}
                         for a in c["accounts"] if a["visibility"] != "archived"],
            "categories": [{"id": cat["id"], "name": cat["name"], "color": hex_color(cat.get("color"))} for cat in c["categories"]],
            "years": [y["n"] for y in c["quick"]["years"]],
            "export_query": c["export_query"],
        }

    @app.get("/export.csv")
    async def export_csv(request: Request):
        rows = search(search_query(request))
        rows.sort(key=lambda t: (t["date"], t["rid"]))
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["date", "authorized_date", "institution", "account", "name", "bank_description",
                    "amount", "category", "note", "bill", "pending", "reviewed", "tip"])
        for t in rows:
            w.writerow([t["date"], t["authorized_date"] or "", t["institution"] or "", t["account_label"],
                        t["display_name"], t["name"], f"{-t['amount']:.2f}",
                        (t["category_info"] or {}).get("name", ""), t["note"] or "",
                        t["bill"]["name"] if t["bill"] else "", "yes" if t["pending"] else "no",
                        "yes" if t["reviewed"] else "no",
                        f"{t['tip_info']['amount']:.2f}" if t["tip_info"] else ""])
        stamp = datetime.now(PACIFIC).strftime("%Y-%m-%d")
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="transactions-{stamp}.csv"'})

    # ---------- insights ----------

    async def insights_data(now: date) -> dict:
        """Everything Insights shows, for the page and the app."""
        nw = await run_in_threadpool(net_worth, db, now)
        rows = resolve(db, list_transactions(db, limit=0))
        flow = monthly(db, rows, now)["months"]
        cats = {c["id"]: c for c in categories(db)}
        inv = await run_in_threadpool(returns, db, now)
        with db.connect() as conn:
            balances = {}
            for r in conn.execute("SELECT account_id, day, balance, flow FROM manual_balances ORDER BY day DESC"):
                balances.setdefault(r["account_id"], []).append(dict(r))
        return {"nw": nw, "flow": flow, "cats": cats, "inv": inv, "balances": balances, "limits": limits(db, now),
                "loans": loans(db, now),
                "loan_bills": [b for b in bill_status(db, now) if b.get("kind") != "income" and not b.get("archived_on")]}

    @app.get("/insights", response_class=HTMLResponse)
    async def insights_page(request: Request):
        now = today()
        d = await insights_data(now)
        nw, flow, cats, inv = d["nw"], d["flow"], d["cats"], d["inv"]
        data = {
            "networth": nw["points"],
            "months": flow,
            "categories": {str(i): {"name": c["name"], "color": c["color"]} for i, c in cats.items()},
            "today": now.isoformat(),
            "investments": {"points": inv["chart"], "starts": inv["starts"]},
        }
        return templates.TemplateResponse(request, "insights.html", {
            "nw": nw, "flow": flow, "notes": observations(flow, cats, now), "data": data,
            "loans": d["loans"], "loan_bills": d["loan_bills"],
            "inv": inv, "limits": d["limits"], "plans": PLANS, "tracked_ids": {a["id"] for a in inv["accounts"]},
            "manual_kinds": MANUAL_KINDS, "manual_debts": MANUAL_DEBTS, "balances": d["balances"],
            "today": now.isoformat(), "kind_labels": KIND_LABELS,
        })

    def signed_money(v: float) -> str:
        return f"{'+' if v >= 0 else ''}{money(v)}"

    def pct_text(p) -> str:
        return f"{'+' if p >= 0 else ''}{p}%"

    def month_short(m: str) -> str:
        return date(int(m[:4]), int(m[5:7]), 1).strftime("%b %Y")

    def search_path(m: str, cid) -> str:
        last = calendar.monthrange(int(m[:4]), int(m[5:7]))[1]
        path = f"/search?start={m}-01&end={m}-{last:02d}"
        return path + (f"&category={cid}" if cid not in (None, "None") else "")

    def investments_json(inv: dict, today_s: str, balances: dict) -> dict | None:
        if not inv["accounts"]:
            return None
        periods = [{"id": k, "label": label} for k, label in inv["periods"]]
        labels = dict(inv["periods"])
        totals = []
        for k, _ in inv["periods"]:
            t = inv["totals"].get(k)
            if t:
                line = signed_money(t["gain"]) + (f" · {pct_text(t['pct'])}" if t["pct"] is not None else "")
                totals.append({"period": k, "line": line, "up": t["gain"] >= 0, "partial": t["partial"]})
        accounts = []
        for a in inv["accounts"]:
            new = a["first"] >= today_s
            lines, table = [], []
            for k, _ in inv["periods"]:
                p = a["periods"].get(k)
                if not p:
                    continue
                if p["gain"] is None:
                    gain = None
                    note = f"Needs a balance near {shortdate(p['needs'])}"
                    table.append({"period": labels[k], "needs": note})
                else:
                    gain = signed_money(p["gain"]) + (f" · {pct_text(p['pct'])}" if p["pct"] is not None else "")
                    note = f"{money(p['put_in'])} put in" if p["put_in"] else "Nothing put in"
                    table.append({"period": labels[k], "put_in": money(p["put_in"]), "gain": signed_money(p["gain"]),
                                  "up": p["gain"] >= 0, "pct": pct_text(p["pct"]) if p["pct"] is not None else "—"})
                if p.get("since"):
                    note += f" · since {shortdate(p['since'])}"
                lines.append({"period": k, "gain": None if new else gain, "up": (p["gain"] or 0) >= 0, "note": note})
            hint = ("Gain is the change in balance minus money put in or taken out. Return weights each deposit by how "
                    f"long it was invested. History starts {shortdate(a['first'])}.")
            if a["id"] in balances:
                hint += (" Gains use the balances you entered (with the money put in), not other history. A period needs "
                         "a balance within about a month before it starts; your record keeper can show the balance on a past date.")
            holdings = []
            for h in a["holdings"]:
                cash = h.get("kind") == "cash" or (h.get("ticker") or "").startswith("CUR:")
                vs = None
                if h.get("cost_basis"):
                    g = (h.get("value") or 0) - h["cost_basis"]
                    vs = {"text": f"{signed_money(g)} vs cost", "up": g >= 0}
                holdings.append({"name": "Cash" if cash else (h.get("ticker") or h.get("name") or "Holding"),
                                 "detail": h.get("name") if h.get("ticker") and h.get("name") and not cash else None,
                                 "value": money(h.get("value") or 0), "vs_cost": vs})
            activity = []
            for t in a["activity"]:
                kind = t.get("type") or ""
                if t.get("subtype") and t["subtype"] != kind:
                    kind += f" / {t['subtype']}"
                activity.append({"name": t.get("name") or (t.get("subtype") or t.get("type") or "").capitalize(),
                                 "detail": f"{shortdate(t['date'])} · {kind}", "amount": money(0 - t["amount"]),
                                 "inflow": t["amount"] < 0})
            accounts.append({"id": a["id"], "name": a["name"], "value": money(a["value"]),
                             "note": "History starts today; gains show from tomorrow" if new else None,
                             "periods": lines, "table": table, "hint": hint, "holdings": holdings, "activity": activity})
        return {"value": money(inv["value"]), "periods": periods, "totals": totals,
                "chart": inv["chart"], "starts": [{"period": k, "day": v} for k, v in inv["starts"].items()],
                "accounts": accounts}

    def limits_json(lim: dict, loan_bills: list) -> dict:
        def item(c):
            d = c["own_year"]
            years = [{"year": d, "label": f"Counts for {d}"}]
            if c["can_switch"]:
                years.append({"year": d - 1, "label": f"Counts for {d - 1}"})
            years.append({"year": 0, "label": "Not a contribution"})
            return {"id": c["id"], "amount": money(c["amount"]), "year": c["year"], "years": years,
                    "detail": f"{shortdate(c['date'])} · {c['name'] or (c['subtype'] or '').capitalize()} · "
                              f"{c['account']} · from {c['source']}"}
        years = []
        for y in lim["years"]:
            left = money(y["ira_limit"] - y["ira"]) if y["ira"] < y["ira_limit"] else "$0.00"
            plans = []
            for p in y["plans"]:
                entered = p["amount"] is not None
                plans.append({"plan": p["plan"], "label": p["label"], "amount": p["amount"],
                              "pct": min(100.0, round(100 * p["amount"] / p["limit"], 1)) if entered else None,
                              "line": (f"{money(p['amount'])} of {money(p['limit'])} · your own contributions, all employers"
                                       if entered else f"Not entered · limit {money(p['limit'])}")})
            years.append({"year": y["year"],
                          "title": f"{y['year']}" + ("" if y["known"] else " (limits not yet published, using the latest)"),
                          "ira_pct": min(100.0, round(100 * y["ira"] / y["ira_limit"], 1)),
                          "ira_line": f"{money(y['ira'])} of {money(y['ira_limit'])} · {left} left",
                          "ira_editable": bool(y["ira_items"] or y["ira_ignored"] or lim["ira_accounts"]),
                          "ira_items": [item(c) for c in y["ira_items"]],
                          "ira_ignored": [item(c) for c in y["ira_ignored"]], "plans": plans})
        footer = "IRS limits for anyone under 50."
        if lim["ira_needs_investments"]:
            footer += (" IRA contributions are counted from Plaid Investments activity: add investments to that "
                       "connection in Accounts › Connections.")
        elif not lim["has_ira"]:
            footer += " No IRA is connected."
        return {"years": years, "footer": footer,
                "ira_accounts": [{"id": a["id"], "name": a["name"], "bill": a["bill"]} for a in lim["ira_accounts"]]}

    def loans_json(items: list) -> list:
        month_label = templates.env.filters["month_label"]
        out = []
        for l in items:
            p = l.get("pslf_info")
            meta = [f"{l['rate']}%", f"{money(l['payment'] or 0)}/mo"] if l["has_details"] else ["Add the rate and payment"]
            if l["estimated"]:
                meta.append(f"Estimated: {l['paid_since']} payment{'' if l['paid_since'] == 1 else 's'} since {shortdate(l['as_of'])}")
            elif l["as_of"]:
                meta.append(f"Balance as of {shortdate(l['as_of'])}")
            progress = None   # PSLF count, or how much is paid off
            stats, summary, note, hint = [], [], None, None
            if p:
                progress = {"pslf": True, "left": f"{p['count']}", "right": "of 120", "pct": p["pct"]}
                if p["left"] and p.get("last_month"):
                    stats = [{"label": "Payments left", "value": str(p["left"])},
                             {"label": "Last payment", "value": month_label(p["last_month"])},
                             {"label": "Forgiven", "value": f"≈ ${p['forgiven']:,.0f}", "up": True}]
                    summary = [{"label": "Payments left", "value": str(p["left"])},
                               {"label": "120th payment", "value": month_label(p["last_month"])},
                               {"label": "Still to pay", "value": money(p["to_pay"])},
                               {"label": "Forgiven", "value": f"≈ {money(p['forgiven'])}", "up": True}]
                    hint = (f"{p['base']} as of {shortdate(p['as_of'])}" + (f", plus {p['added']} since" if p["added"] else "")
                            + f". Assumes {money(l['payment'] or 0)} every month at {l['rate']}%; the forgiven amount is the "
                            "balance left after the 120th payment. Recalculate when your payment changes at recertification.")
                elif not p["left"]:
                    note = {"text": "All 120 done: apply for forgiveness", "up": True}
            elif l.get("payoff") or "paid_pct" in l:
                if "paid_pct" in l:
                    progress = {"pslf": False, "left": f"{round(l['paid_pct'])}%", "right": "paid off",
                                "detail": f"${l['paid']:,.0f} of ${l['original']:,.0f}", "pct": l["paid_pct"]}
                po = l.get("payoff")
                if po:
                    if po["months"] is not None:
                        stats = [{"label": "Payments left", "value": str(po["months"])},
                                 {"label": "Paid off", "value": month_label(po["month"])},
                                 {"label": "Total to pay", "value": f"${l['balance'] + po['interest']:,.0f}",
                                  "detail": f"${po['interest']:,.0f} interest"}]
                        hint = (f"At {money(l['payment'] or 0)} a month it's paid off about {month_label(po['month'])}, "
                                f"with about {money(po['interest'])} more interest.")
                    else:
                        stats = [{"label": "Paid off", "value": "Never at this payment"}]
                elif not l["has_details"]:
                    note = {"text": "Add the rate and payment for a payoff date", "up": False}
            recert = None
            if l.get("recert"):
                days = l["recert_days"]
                recert = {"text": f"Recertify by {shortdate(l['recert'])}" + (f" · {days} days" if days >= 0 else ""),
                          "soon": days < 60}
            out.append({
                "id": l["id"], "name": l["name"], "balance": money(l["balance"]), "meta": meta,
                "progress": progress, "stats": stats, "note": note, "recert": recert, "summary": summary, "hint": hint,
                "form": {"plaid": l["source"] == "plaid", "balance": l["balance"], "rate": l["rate"],
                         "payment": l["payment"], "original": l["original"], "bill_id": l["bill_id"],
                         "pslf": l["pslf"], "pslf_count": p["base"] if p else None,
                         "pslf_as_of": p["as_of"] if p else None, "recert": l.get("recert")},
                "months": [{"month": m["month"], "label": m["label"], "counts": m["counts"],
                            "detail": f"{money(m['amount'])} · " + ", ".join(shortdate(d) for d in m["days"])}
                           for m in (p["months"] if p else [])],
            })
        return out

    def networth_accounts_json(nw: dict, balances: dict, tracked: set) -> list:
        out = []
        for a in nw["accounts"]:
            hist = balances.get(a["id"])
            if a["manual"]:
                sub = f"{a['kind']} · entered by hand" + (f" {shortdate(hist[0]['day'])}" if hist else "")
            elif a["source"] == "manual":
                sub = f"From YNAB · entered by hand {shortdate(hist[0]['day'])}"
            elif a["source"] == "ynab":
                sub = "From YNAB"
            elif a["source"] == "estimate":
                sub = "Worked out from transactions"
            else:
                sub = "Plaid"
            if a["transit"]:
                sub += f" · incl. {'+' if a['transit'] > 0 else ''}{money(a['transit'])} in transit"
            if a["editable"]:
                hint = ("Enter the balance whenever you check it. Net worth uses the latest one up to each month."
                        if a["manual"] else "This account comes from YNAB with no Plaid connection. A balance entered "
                        "here is used from its date on, in place of YNAB's.")
            else:
                hint = (("Plaid hasn't saved a balance for this account yet, so today's value is worked out from its "
                         "transactions." if a["source"] == "estimate" else "Plaid updates this balance on every sync.")
                        + " Leave it out if it shouldn't count in net worth, for example an account you closed or don't own.")
            out.append({"id": a["id"], "name": a["name"], "value": money(a["value"]), "debt": a["debt"], "subtitle": sub,
                        "editable": a["editable"], "manual": a["manual"], "kind": a["subtype"],
                        "tracked": a["id"] in tracked, "hint": hint,
                        "history": [{"day": h["day"], "label": shortdate(h["day"]), "balance": money(h["balance"]),
                                     "flow": f"{money(h['flow'])} in" if h["flow"] else None} for h in (hist or [])[:24]]})
        return out

    @app.get("/api/insights")
    async def insights_json():
        """Insights for the native app. Chart series and form values are numbers; the lines the page
        shows come formatted."""
        now = today()
        d = await insights_data(now)
        nw, flow, cats, inv = d["nw"], d["flow"], d["cats"], d["inv"]
        today_s = now.isoformat()
        tiles = [{"tone": n["tone"], "badge": n["badge"], "label": n["label"], "value": n["value"], "detail": n["detail"],
                  "text": bool(n.get("text")),
                  "color": hex_color(cats[n["cat"]]["color"]) if n.get("cat") in cats else None, "search": n.get("href")}
                 for n in observations(flow, cats, now)]
        def cat_rows(m):
            rows = []
            for cid, v in m["cats"].items():
                c = cats.get(int(cid)) if cid != "None" else None
                rows.append({"id": int(cid) if c else None, "name": c["name"] if c else "Other",
                             "color": hex_color(c["color"] if c else None), "value": v, "search": search_path(m["m"], cid)})
            return sorted(rows, key=lambda r: -r["value"])
        months = [{"m": m["m"], "label": month_short(m["m"]), "income": m["income"], "spending": m["spending"],
                   "saving": m["saving"], "debt": m["debt"], "kept": m["kept"], "rate": m["rate"],
                   "search": search_path(m["m"], None), "categories": cat_rows(m)} for m in flow[-12:]]
        tracked = {a["id"] for a in inv["accounts"]}
        return {
            "today": today_s, "tiles": tiles,
            "networth": {
                "now": nw["now"], "points": [{"m": p["m"], "net": p["net"], "assets": p["assets"], "debts": p["debts"]}
                                             for p in nw["points"]],
                "accounts": networth_accounts_json(nw, d["balances"], tracked),
                "excluded": [{"id": a["id"], "name": a["name"]} for a in nw["excluded"]],
                "has_ynab": any(a["source"] == "ynab" for a in nw["accounts"]),
            },
            "investments": investments_json(inv, today_s, d["balances"]),
            "limits": limits_json(d["limits"], d["loan_bills"]),
            "loans": loans_json(d["loans"]),
            "loan_bills": [{"id": b["id"], "name": b["name"]} for b in d["loan_bills"]],
            "months": months,
            "manual_kinds": [{"id": k, "label": label + (" (debt)" if k in MANUAL_DEBTS else "")}
                             for k, label in MANUAL_KINDS.items()],
        }

    @app.get("/api/insights/spending", response_class=HTMLResponse)
    async def month_spending(request: Request, m: str, format: str = ""):
        """Every transaction counted as spending in a month, largest first, as editable rows."""
        try:
            first = date.fromisoformat(m + "-01")
        except ValueError:
            raise HTTPException(400, "Month must look like 2026-11")
        last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
        rows = resolve(db, list_transactions(db, where="t.date >= ? AND t.date <= ?",
                                             params=(first.isoformat(), last.isoformat()), limit=0))
        items = spending_rows(db, rows)
        if format == "json":  # the native app
            return JSONResponse({"items": [txn_json(t, dated=True) for t in items]})
        macro = templates.env.get_template("_txn.html").make_module({}).txn_row
        return HTMLResponse("".join(str(macro(t, dated=True)) for t in items))

    @app.post("/api/accounts/{account_id}/net-worth")
    async def set_in_net_worth(account_id: str, request: Request):
        include = bool((await request.json()).get("include"))
        with db.connect() as conn:
            n = conn.execute("UPDATE accounts SET in_net_worth=? WHERE account_id=?",
                             (1 if include else 0, account_id)).rowcount
        if not n:
            raise HTTPException(404, "No such account")
        return {"ok": True}

    @app.post("/api/manual-accounts")
    async def save_manual_account(request: Request):
        body = await request.json()
        name = str(body.get("name") or "").strip()[:60]
        kind = body.get("kind")
        if not name:
            raise HTTPException(400, "Name the account")
        if kind not in MANUAL_KINDS:
            raise HTTPException(400, "Choose what kind of account it is")
        aid = str(body.get("id") or "")
        with db.connect() as conn:
            if aid:
                if not aid.startswith("manual:"):
                    raise HTTPException(400, "Only accounts added here can be renamed here")
                conn.execute("UPDATE accounts SET name=?, subtype=?, type=? WHERE account_id=? AND source='manual'",
                             (name, kind, "liability" if kind in MANUAL_DEBTS else "asset", aid))
            else:
                aid = "manual:" + secrets.token_hex(8)
                conn.execute("INSERT INTO accounts (account_id, source, name, type, subtype, currency) "
                             "VALUES (?, 'manual', ?, ?, ?, 'USD')",
                             (aid, name, "liability" if kind in MANUAL_DEBTS else "asset", kind))
        if body.get("balance") not in (None, ""):
            save_balance(aid, body.get("balance"), body.get("day"))
        return {"ok": True, "id": aid}

    def save_balance(aid: str, value, day, flow=None) -> None:
        try:
            amount = round(num(value), 2)
        except ValueError:
            raise HTTPException(400, "Enter the balance as a number")
        day = str(day or "") or today().isoformat()
        try:
            date.fromisoformat(day)
        except ValueError:
            raise HTTPException(400, "Enter a valid date")
        if day > today().isoformat():
            raise HTTPException(400, "The date can't be in the future")
        with db.connect() as conn:
            src = conn.execute("SELECT source FROM accounts WHERE account_id=?", (aid,)).fetchone()
            if not src or src["source"] not in ("manual", "ynab"):
                raise HTTPException(400, "Balances can only be entered for manual or YNAB accounts")
            try:
                put_in = round(num(flow), 2) if str(flow or "").strip() else None
            except ValueError:
                raise HTTPException(400, "Enter the money put in as a number")
            conn.execute("INSERT INTO manual_balances (account_id, day, balance, flow) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(account_id, day) DO UPDATE SET balance=excluded.balance, flow=excluded.flow",
                         (aid, day, amount, put_in))

    @app.post("/api/balances")
    async def add_balance(request: Request):
        body = await request.json()
        save_balance(str(body.get("account_id") or ""), body.get("balance"), body.get("day"), body.get("flow"))
        return {"ok": True}

    @app.post("/api/accounts/{account_id}/track")
    async def set_track_returns(account_id: str, request: Request):
        """Show (or hide) an account in Investments."""
        track = 1 if (await request.json()).get("track") else 0
        with db.connect() as conn:
            if not conn.execute("UPDATE accounts SET track_returns=? WHERE account_id=?", (track, account_id)).rowcount:
                raise HTTPException(404, "Account not found")
        return {"ok": True}

    @app.post("/api/loans")
    async def save_loan(request: Request):
        """Loan details, and a new loan account when there is no account_id yet."""
        body = await request.json()
        aid = str(body.get("account_id") or "")

        def opt_num(key, label):
            raw = str(body.get(key) or "").strip().rstrip("%")
            if not raw:
                return None
            try:
                return round(num(raw), 4)
            except ValueError:
                raise HTTPException(400, f"Enter the {label} as a number")

        def opt_day(key):
            raw = str(body.get(key) or "").strip()
            if not raw:
                return None
            try:
                return date.fromisoformat(raw).isoformat()
            except ValueError:
                raise HTTPException(400, "Enter a valid date")

        rate, payment = opt_num("rate", "interest rate"), opt_num("payment", "monthly payment")
        original = opt_num("original", "original amount")
        pslf = 1 if body.get("pslf") else 0
        count = opt_num("pslf_count", "qualifying payment count")
        if count is not None and not 0 <= count <= 120:
            raise HTTPException(400, "Qualifying payments are between 0 and 120")
        bill = body.get("bill_id")
        bill = int(bill) if str(bill or "").isdigit() else None
        with db.connect() as conn:
            if not aid:
                name = str(body.get("name") or "").strip()[:60]
                if not name:
                    raise HTTPException(400, "Name the loan")
                aid = "manual:" + secrets.token_hex(8)
                conn.execute("INSERT INTO accounts (account_id, source, name, type, subtype, currency) "
                             "VALUES (?, 'manual', ?, 'liability', 'loan', 'USD')", (aid, name))
            elif not conn.execute("SELECT 1 FROM accounts WHERE account_id=?", (aid,)).fetchone():
                raise HTTPException(404, "Account not found")
            conn.execute(
                "INSERT INTO loans (account_id, rate, payment, bill_id, pslf, pslf_count, pslf_as_of, recert, original) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(account_id) DO UPDATE SET rate=excluded.rate, "
                "payment=excluded.payment, bill_id=excluded.bill_id, pslf=excluded.pslf, pslf_count=excluded.pslf_count, "
                "pslf_as_of=excluded.pslf_as_of, recert=excluded.recert, original=excluded.original",
                (aid, rate, payment, bill, pslf, int(count) if count is not None and pslf else None,
                 opt_day("pslf_as_of") if pslf else None, opt_day("recert") if pslf else None, original))
        months = body.get("months") or {}
        with db.connect() as conn:
            for m, counts in months.items():
                if len(str(m)) != 7:
                    continue
                if counts:
                    conn.execute("DELETE FROM loan_months WHERE account_id=? AND month=?", (aid, m))
                else:
                    conn.execute("INSERT OR REPLACE INTO loan_months VALUES (?, ?, 0)", (aid, m))
        if str(body.get("balance") or "").strip():
            save_balance(aid, body.get("balance"), body.get("day"))
        return {"ok": True, "id": aid}

    @app.post("/api/loans/month")
    async def set_loan_month(request: Request):
        """Whether a month's payment counts toward PSLF."""
        body = await request.json()
        month = str(body.get("month") or "")
        if len(month) != 7 or month[4] != "-":
            raise HTTPException(400, "Month must look like 2026-11")
        with db.connect() as conn:
            if body.get("counts"):
                conn.execute("DELETE FROM loan_months WHERE account_id=? AND month=?", (str(body.get("account_id")), month))
            else:
                conn.execute("INSERT OR REPLACE INTO loan_months VALUES (?, ?, 0)", (str(body.get("account_id")), month))
        return {"ok": True}

    @app.post("/api/investments/plan")
    async def save_plan_contributions(request: Request):
        """Workplace plan contributions so far in a year (empty removes the entry)."""
        body = await request.json()
        try:
            year = int(body.get("year"))
        except (TypeError, ValueError):
            raise HTTPException(400, "Pick a year")
        plan = str(body.get("plan") or "")
        if plan not in PLANS:
            raise HTTPException(400, "Unknown plan")
        with db.connect() as conn:
            if not str(body.get("amount") or "").strip():
                conn.execute("DELETE FROM plan_contributions WHERE year=? AND plan=?", (year, plan))
            else:
                try:
                    amount = round(num(body["amount"]), 2)
                except ValueError:
                    raise HTTPException(400, "Enter the amount as a number")
                conn.execute("INSERT INTO plan_contributions VALUES (?, ?, ?) ON CONFLICT(year, plan) "
                             "DO UPDATE SET amount=excluded.amount", (year, plan, amount))
        return {"ok": True}

    @app.post("/api/investments/contribution-bill")
    async def set_contribution_bill(request: Request):
        """The recurring bill (a monthly transfer) whose tagged transactions are an IRA's contributions."""
        body = await request.json()
        aid = str(body.get("account_id") or "")
        try:
            bill = int(body.get("bill_id") or 0) or None
        except (TypeError, ValueError):
            raise HTTPException(400, "Pick a recurring bill")
        with db.connect() as conn:
            if bill and not conn.execute("SELECT 1 FROM bills WHERE id=?", (bill,)).fetchone():
                raise HTTPException(400, "Pick a recurring bill")
            if not conn.execute("UPDATE accounts SET contribution_bill=? WHERE account_id=?", (bill, aid)).rowcount:
                raise HTTPException(404, "Account not found")
        return {"ok": True}

    @app.post("/api/investments/tax-year")
    async def set_contribution_year(request: Request):
        """Count an early-year IRA contribution for the year before (or back for its own year)."""
        body = await request.json()
        cid = str(body.get("id") or "")
        with db.connect() as conn:
            if cid.startswith("ynab:"):
                row = conn.execute("SELECT date FROM transactions WHERE source='ynab' AND id=?", (cid[5:],)).fetchone()
            elif cid.startswith("bill:"):
                row = conn.execute("SELECT date FROM transactions WHERE source='plaid' AND id=?", (cid[5:],)).fetchone()
            else:
                row = conn.execute("SELECT date FROM investment_txns WHERE id=?", (cid,)).fetchone()
            if not row:
                raise HTTPException(404, "Contribution not found")
            d = date.fromisoformat(row["date"])
            y = d.year
            year = int(body.get("year") if body.get("year") is not None else y)
            early = (d.month, d.day) <= (4, 15)
            if year not in ((y, y - 1, 0) if early else (y, 0)):
                raise HTTPException(400, "It can count for its own year, the year before (through April 15), or not at all")
            if year == y:
                conn.execute("DELETE FROM contribution_years WHERE id=?", (cid,))
            else:
                conn.execute("INSERT INTO contribution_years VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET year=excluded.year",
                             (cid, year))
            conn.execute("UPDATE investment_txns SET tax_year=NULL WHERE id=?", (cid,))
        return {"ok": True}

    @app.post("/api/balances/delete")
    async def delete_balance(request: Request):
        body = await request.json()
        with db.connect() as conn:
            conn.execute("DELETE FROM manual_balances WHERE account_id=? AND day=?",
                         (str(body.get("account_id") or ""), str(body.get("day") or "")))
        return {"ok": True}

    @app.post("/api/manual-accounts/delete")
    async def delete_manual_account(request: Request):
        aid = str((await request.json()).get("id") or "")
        if not aid.startswith("manual:"):
            raise HTTPException(400, "Only accounts added here can be removed here")
        with db.connect() as conn:
            conn.execute("DELETE FROM manual_balances WHERE account_id=?", (aid,))
            conn.execute("DELETE FROM accounts WHERE account_id=? AND source='manual'", (aid,))
        return {"ok": True}

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

    @app.get("/link", response_class=HTMLResponse)
    async def link_page(request: Request):
        """Plaid Link alone (no page behind it), for the native app's bank connection sheet."""
        return templates.TemplateResponse(request, "link.html", {"plaid_ready": plaid is not None})

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
            return {"link_token": await p.link_token(access_token, choose_accounts=bool(body.get("accounts")),
                                                     add_investments=bool(access_token and body.get("investments")),
                                                     investments_only=bool(not access_token and body.get("investments")))}
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
            only = bool(body.get("investments"))
            conn.execute(
                "INSERT INTO plaid_items (item_id, access_token, institution_id, institution, products, investments) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(item_id) DO UPDATE SET access_token=excluded.access_token",
                (item_id, access_token, inst.get("institution_id"), inst.get("name") or "",
                 "investments" if only else None, "on" if only else None),
            )
        await load_accounts(item_id)  # accounts show up right away; transactions follow in the background
        asyncio.create_task(run_sync())
        return {"ok": True}

    async def load_accounts(item_id: str) -> None:
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM plaid_items WHERE item_id=?", (item_id,)).fetchone()
        if not row:
            return
        try:
            await refresh_accounts(db, require_plaid(), dict(row))
        except PlaidError as e:
            log.warning("Could not load accounts for a new connection: %s", e)

    @app.post("/api/plaid/updated")
    async def updated(request: Request):
        body = await request.json()
        with db.connect() as conn:
            conn.execute("UPDATE plaid_items SET status='ok', error_code=NULL WHERE item_id=?",
                         (body.get("item_id"),))
            if body.get("investments"):  # consent given in Link: start pulling holdings and activity
                conn.execute("UPDATE plaid_items SET investments='on' WHERE item_id=?", (body.get("item_id"),))
        item_id = str(body.get("item_id") or "")
        def count() -> int:
            with db.connect() as conn:
                return conn.execute("SELECT COUNT(*) FROM accounts WHERE source='plaid' AND item_id=?",
                                    (item_id,)).fetchone()[0]
        before = count()
        await load_accounts(item_id)  # picks up accounts added in Plaid
        added = count() - before
        log.info("Connection updated: Link reported %s selected account(s); Plaid now shares %s new account(s)",
                 body.get("selected", "?"), added)
        asyncio.create_task(run_sync())
        return {"ok": True, "added": added}

    @app.get("/sw.js")
    async def service_worker():
        """The service worker that shows notifications and sets the icon badge. Served from the root
        so it covers the whole app."""
        return Response((HERE / "static" / "sw.js").read_text(), media_type="text/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})

    @app.get("/api/push/key")
    async def push_key():
        return {"key": vapid_keys(db)[1]}

    @app.post("/api/push/subscribe")
    async def push_subscribe(request: Request):
        sub_ = (await request.json()).get("subscription") or {}
        keys = sub_.get("keys") or {}
        endpoint = str(sub_.get("endpoint") or "")
        if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
            raise HTTPException(400, "Not a push subscription")
        with db.connect() as conn:
            conn.execute("INSERT INTO push_subs (endpoint, p256dh, auth) VALUES (?, ?, ?) ON CONFLICT(endpoint) "
                         "DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth", (endpoint, keys["p256dh"], keys["auth"]))
            # What's already waiting isn't news: only transactions after this are announced.
            conn.execute("INSERT OR IGNORE INTO push_told SELECT id FROM transactions WHERE source='plaid' AND reviewed=0")
        db.set_kv("push_broken", json.dumps(sorted(i["item_id"] for i in broken_items())))
        return {"ok": True}

    @app.post("/api/apns/register")
    async def apns_register(request: Request):
        """The native app's device token (sent by the app through this signed-in page)."""
        body = await request.json()
        token = str(body.get("token") or "").strip().lower()
        env = "sandbox" if body.get("env") == "sandbox" else "production"
        if not re.fullmatch(r"[0-9a-f]{32,200}", token):
            raise HTTPException(400, "Not a device token")
        with db.connect() as conn:
            new = not conn.execute("SELECT 1 FROM apns_devices WHERE token=?", (token,)).fetchone()
            conn.execute("INSERT INTO apns_devices (token, env) VALUES (?, ?) ON CONFLICT(token) DO UPDATE SET env=excluded.env",
                         (token, env))
            if new:  # what's already waiting isn't news to a new device
                conn.execute("INSERT OR IGNORE INTO push_told SELECT id FROM transactions WHERE source='plaid' AND reviewed=0")
        if new:
            db.set_kv("push_broken", json.dumps(sorted(i["item_id"] for i in broken_items())))
        return {"ok": True, "ready": apns.configured}

    @app.post("/api/apns/unregister")
    async def apns_unregister(request: Request):
        token = str((await request.json()).get("token") or "").strip().lower()
        with db.connect() as conn:
            conn.execute("DELETE FROM apns_devices WHERE token=?", (token,))
        return {"ok": True}

    @app.post("/api/push/unsubscribe")
    async def push_unsubscribe(request: Request):
        endpoint = str((await request.json()).get("endpoint") or "")
        with db.connect() as conn:
            conn.execute("DELETE FROM push_subs WHERE endpoint=?", (endpoint,))
        return {"ok": True}

    @app.post("/api/push/settings")
    async def push_settings(request: Request):
        db.set_kv("push_clear_others", "1" if (await request.json()).get("clear_others") else "0")
        return {"ok": True}

    @app.post("/api/push/opened")
    async def push_opened(request: Request):
        """The app's notification was opened (or the app opened with it showing) on one device:
        clear it from the others."""
        token = str((await request.json()).get("token") or "").strip().lower()
        return {"sent": await apns.refresh(db, {"clear": True}, skip=token)}

    @app.post("/api/push/all-clear")
    async def push_all_clear(request: Request):
        """Sent by a device that just reached zero: if the others were last told about something,
        replace that notification on them with a quiet "All caught up" (only with the setting on)."""
        body = await request.json()
        endpoint = str(body.get("endpoint") or body.get("device") or "")
        if db.get_kv("push_clear_others") != "1" or (db.get_kv("push_last_badge") or "0") == "0":
            return {"sent": 0}
        if inbox_count() + account_issues():
            return {"sent": 0}
        db.set_kv("push_last_badge", "0")
        # Web Push only: the app's devices clear themselves (badge_loop).
        sent = await push_send(db, {"title": "All caught up", "body": "Nothing to review or fix.", "badge": 0,
                                    "url": "/", "quiet": True}, push_subject(), skip=endpoint)
        return {"sent": sent}

    @app.post("/api/push/test")
    async def push_test():
        n = inbox_count() + account_issues()
        db.set_kv("push_last_badge", str(n))
        sent = await notify({"title": "Notifications are on",
                             "body": f"{n} to review or fix. New transactions will show up here.",
                             "badge": n, "url": "/"})
        if not sent:
            raise HTTPException(400, "No device accepted the test. Turn notifications on from the installed app.")
        return {"ok": True, "sent": sent}

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

