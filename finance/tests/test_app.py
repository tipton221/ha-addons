import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.main import create_app  # noqa: E402
from app.plaid import PlaidError  # noqa: E402
from app.sync import sync_all  # noqa: E402

H = {"X-Finance": "1"}


def make_settings(tmp_path, monkeypatch, **opts):
    base = {"public_url": "http://localhost:8080", "setup_code": "correct-horse-battery"}
    base.update(opts)
    (tmp_path / "options.json").write_text(json.dumps(base))
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FINANCE_OPTIONS", str(tmp_path / "options.json"))
    return load_settings()


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    with TestClient(create_app(settings, background_sync=False)) as c:
        yield c


def test_redirects_to_setup_when_no_passkey(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"


def test_api_requires_session(client):
    assert client.post("/api/sync", headers=H).status_code == 401


def test_post_without_header_is_rejected(client):
    assert client.post("/api/register/options", json={"code": "correct-horse-battery"}).status_code == 403


def test_setup_code_checked(client):
    assert client.post("/api/register/options", json={"code": "wrong"}, headers=H).status_code == 403
    r = client.post("/api/register/options", json={"code": "correct-horse-battery"}, headers=H)
    assert r.status_code == 200 and "challenge" in r.json()


def test_short_setup_code_locks_setup(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch, setup_code="short")
    with TestClient(create_app(settings, background_sync=False)) as c:
        assert "Setup is locked" in c.get("/setup").text
        assert c.post("/api/register/options", json={"code": "short"}, headers=H).status_code == 403


def test_cloudflare_access_enforced(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch, cf_access_team_domain="team.cloudflareaccess.com",
                             cf_access_aud="abc")
    with TestClient(create_app(settings, background_sync=False)) as c:
        assert c.get("/login").status_code == 403
        assert c.get("/static/style.css").status_code == 403
        assert c.get("/login", headers={"Cf-Access-Jwt-Assertion": "not-a-jwt"}).status_code == 403


def test_security_headers(client):
    r = client.get("/setup")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"


class FakePlaid:
    def __init__(self, pages, accounts=None, fail_first_with=None):
        self.pages = list(pages)
        self.cursors = []
        self.fail_first_with = fail_first_with
        self._accounts = accounts or []

    async def transactions_sync(self, access_token, cursor):
        self.cursors.append(cursor)
        if self.fail_first_with:
            code, self.fail_first_with = self.fail_first_with, None
            raise PlaidError(400, {"error_code": code})
        return self.pages.pop(0)

    async def accounts(self, access_token):
        return self._accounts


def txn(tid, amount, pending=False, pending_id=None):
    return {"transaction_id": tid, "account_id": "acct1", "date": "2026-11-12", "name": f"Shop {tid}",
            "merchant_name": "Shop", "amount": amount, "iso_currency_code": "USD", "pending": pending,
            "pending_transaction_id": pending_id,
            "personal_finance_category": {"primary": "GENERAL_MERCHANDISE", "detailed": "X"}}


def page(added=(), modified=(), removed=(), cursor="c1", more=False):
    return {"added": list(added), "modified": list(modified),
            "removed": [{"transaction_id": r} for r in removed],
            "next_cursor": cursor, "has_more": more, "transactions_update_status": "INITIAL_UPDATE_COMPLETE"}


def seed_item(db):
    with db.connect() as conn:
        conn.execute("INSERT INTO plaid_items (item_id, access_token, institution) VALUES ('i1', 'tok', 'Amex')")


def test_sync_pages_pending_to_posted_and_cursor(tmp_path):
    db = Database(tmp_path / "f.db")
    seed_item(db)
    plaid = FakePlaid([page(added=[txn("p1", 12.5, pending=True)], cursor="c1", more=True),
                       page(added=[txn("t2", 40.0)], cursor="c2")],
                      accounts=[{"account_id": "acct1", "name": "Gold", "mask": "1001", "type": "credit",
                                 "balances": {"current": 52.5, "iso_currency_code": "USD"}}])
    asyncio.run(sync_all(db, plaid))
    with db.connect() as conn:
        conn.execute("UPDATE transactions SET reviewed=1 WHERE id='p1'")
    # The pending charge posts: Plaid adds the posted one and removes the pending one.
    plaid.pages = [page(added=[txn("t1", 12.5, pending_id="p1")], removed=["p1"], cursor="c3")]
    asyncio.run(sync_all(db, plaid))
    with db.connect() as conn:
        rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM transactions")}
        item = dict(conn.execute("SELECT * FROM plaid_items").fetchone())
        acct = dict(conn.execute("SELECT * FROM accounts").fetchone())
    assert set(rows) == {"t1", "t2"}
    assert rows["t1"]["reviewed"] == 1  # review carried over from the pending charge
    assert item["cursor"] == "c3"
    assert plaid.cursors == [None, "c1", "c2"]
    assert acct["current_balance"] == 52.5


def test_sync_restarts_after_mutation_error(tmp_path):
    db = Database(tmp_path / "f.db")
    seed_item(db)
    plaid = FakePlaid([page(added=[txn("t1", 5)], cursor="c1")],
                      fail_first_with="TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION")
    asyncio.run(sync_all(db, plaid))
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"] == 1


def test_sync_marks_login_required(tmp_path):
    db = Database(tmp_path / "f.db")
    seed_item(db)
    plaid = FakePlaid([], fail_first_with="ITEM_LOGIN_REQUIRED")
    result = asyncio.run(sync_all(db, plaid))
    with db.connect() as conn:
        item = dict(conn.execute("SELECT * FROM plaid_items").fetchone())
    assert item["status"] == "login_required"
    assert result["Amex"] == {"error": "ITEM_LOGIN_REQUIRED"}
