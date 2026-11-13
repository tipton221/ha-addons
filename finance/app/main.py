"""Finance web app. Run with: uvicorn app.main:create_app --factory"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .auth import CHALLENGE_COOKIE, SESSION_COOKIE, Challenges, CloudflareAccess, Passkeys, Sessions
from .config import Settings, load_settings
from .db import Database
from .plaid import Plaid, PlaidError
from .sync import sync_all
from .ynab import YNAB

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("finance")

HERE = Path(__file__).parent
PACIFIC = ZoneInfo("America/Los_Angeles")
PUBLIC_PATHS = {"/login", "/setup", "/api/auth/options", "/api/auth/verify",
                "/api/register/options", "/api/register/verify"}

CSP = (
    "default-src 'self'; "
    "script-src 'self' https://cdn.plaid.com; "
    "frame-src https://cdn.plaid.com; "
    "connect-src 'self' https://*.plaid.com; "
    "img-src 'self' data:; style-src 'self'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)


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

    async def run_sync() -> dict:
        if plaid is None:
            return {}
        async with sync_lock:
            return await sync_all(db, plaid)

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
    templates.env.filters["shortdate"] = lambda d: datetime.fromisoformat(d).strftime("%b %-d") if d else ""

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

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        with db.connect() as conn:
            items = [dict(r) for r in conn.execute(
                "SELECT item_id, institution, status, error_code, update_status, last_sync_at "
                "FROM plaid_items ORDER BY institution")]
            accounts = [dict(r) for r in conn.execute(
                "SELECT a.*, p.institution FROM accounts a LEFT JOIN plaid_items p "
                "ON a.item_id = p.item_id ORDER BY p.institution, a.type, a.name")]
            txns = [dict(r) for r in conn.execute(
                "SELECT t.*, a.name AS account_name, a.mask FROM transactions t "
                "LEFT JOIN accounts a ON a.account_id = t.account_id "
                "ORDER BY t.date DESC, t.first_seen_at DESC LIMIT 50")]
            total = conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"]
        ynab_status = None
        if ynab.configured:
            try:
                budgets = await ynab.budgets()
                ynab_status = {"ok": True, "budgets": [b["name"] for b in budgets]}
            except Exception as e:
                ynab_status = {"ok": False, "error": type(e).__name__}
        return templates.TemplateResponse(request, "dashboard.html", {
            "settings": settings, "plaid_ready": plaid is not None, "items": items,
            "accounts": accounts, "txns": txns, "total": total, "ynab": ynab_status,
            "last_sync": db.get_kv("last_sync_at"),
        })

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
        return {"ok": True}

    return app

