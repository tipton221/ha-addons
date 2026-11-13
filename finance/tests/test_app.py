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


# ---------------------------------------------------------------- YNAB and comparison

from app.compare import compare_all, compare_lists, suggest_links  # noqa: E402
from app.sync import sync_ynab  # noqa: E402


class FakeYNAB:
    configured = True

    def __init__(self, accounts, batches):
        self._accounts = accounts
        self.batches = list(batches)
        self.knowledge_seen = []

    async def accounts(self):
        return self._accounts

    async def transactions(self, knowledge):
        self.knowledge_seen.append(knowledge)
        return self.batches.pop(0)


def ytxn(tid, milli, d="2026-11-10", acct="y1", deleted=False, payee="Shop"):
    return {"id": tid, "account_id": acct, "date": d, "amount": milli, "payee_name": payee,
            "category_name": "Spending", "deleted": deleted}


def test_ynab_import_signs_and_delta(tmp_path):
    db = Database(tmp_path / "f.db")
    ynab = FakeYNAB([{"id": "y1", "name": "AMEX Gold Card", "type": "creditCard", "balance": -187990,
                      "closed": False, "deleted": False}],
                    [([ytxn("a", -12500), ytxn("b", 500000)], 10),
                     ([ytxn("a", -12500, deleted=True)], 11)])
    asyncio.run(sync_ynab(db, ynab))
    with db.connect() as conn:
        amounts = {r["id"]: r["amount"] for r in conn.execute("SELECT * FROM transactions WHERE source='ynab'")}
    assert amounts == {"a": 12.5, "b": -500.0}  # outflow positive, like Plaid
    asyncio.run(sync_ynab(db, ynab))
    assert ynab.knowledge_seen == [None, 10]
    with db.connect() as conn:
        assert [r["id"] for r in conn.execute("SELECT id FROM transactions WHERE source='ynab'")] == ["b"]
        acct = dict(conn.execute("SELECT * FROM accounts WHERE source='ynab'").fetchone())
    assert acct["account_id"] == "ynab:y1" and acct["current_balance"] == -187.99


def test_suggest_links():
    plaid = [{"account_id": "p1", "name": "American Express Gold Card", "mask": "1003"},
             {"account_id": "p2", "name": "Blue Business Cash(TM)", "mask": "1008"},
             {"account_id": "p3", "name": "Blue Cash Preferred®", "mask": "5003"},
             {"account_id": "p4", "name": "Hilton Honors Card", "mask": "1004"},
             {"account_id": "p5", "name": "Platinum Card®", "mask": "1000"},
             {"account_id": "p6", "name": "Some Other Card", "mask": "9"}]
    ynab = [{"account_id": "ynab:g", "name": "AMEX Gold Card", "subtype": "open"},
            {"account_id": "ynab:gold", "name": "Amex Gold Card old", "subtype": "closed"},
            {"account_id": "ynab:bbc", "name": "AMEX Business Cash", "subtype": "open"},
            {"account_id": "ynab:bcp", "name": "AMEX Blue Cash Preferred", "subtype": "open"},
            {"account_id": "ynab:h", "name": "AMEX Hilton Honors", "subtype": "open"},
            {"account_id": "ynab:p", "name": "AMEX Platinum Card", "subtype": "open"},
            {"account_id": "ynab:wf", "name": "Wealthfront", "subtype": "open"}]
    assert suggest_links(plaid, ynab) == {"p1": "ynab:g", "p2": "ynab:bbc", "p3": "ynab:bcp",
                                          "p4": "ynab:h", "p5": "ynab:p"}


def ct(src, tid, amount, d, pending=0):
    return {"source": src, "id": tid, "date": d, "name": tid, "merchant": tid, "amount": amount,
            "pending": pending, "category": None}


def test_compare_lists():
    plaid = [ct("plaid", "same", 10.0, "2026-11-01"),
             ct("plaid", "shifted", 20.0, "2026-11-02"),
             ct("plaid", "tip", 50.0, "2026-11-03"),
             ct("plaid", "missing", 7.0, "2026-11-04"),
             ct("plaid", "pend", 3.0, "2026-11-05", pending=1),
             ct("plaid", "late", 9.0, "2026-11-20")]
    ynab = [ct("ynab", "same", 10.0, "2026-11-01"),
            ct("ynab", "shifted", 20.0, "2026-11-05"),
            ct("ynab", "tip", 58.0, "2026-11-03"),
            ct("ynab", "extra", 4.0, "2026-11-06"),
            ct("ynab", "before", 1.0, "2026-10-01")]
    r = compare_lists(plaid, ynab, "2026-11-01", "2026-11-10")
    assert r.matched == 2
    assert [t["id"] for t in r.only_plaid] == ["missing"]
    assert [t["id"] for t in r.only_ynab] == ["extra"]
    assert [(p["id"], y["id"]) for p, y in r.amount_differs] == [("tip", "tip")]


def test_links_api_and_compare_page(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    app = create_app(settings, background_sync=False)
    db = Database(tmp_path / "finance.db")
    with db.connect() as conn:
        conn.execute("INSERT INTO plaid_items (item_id, access_token, institution) VALUES ('i1','t','Amex')")
        conn.execute("INSERT INTO accounts (account_id, source, item_id, name) VALUES ('p1','plaid','i1','Gold Card')")
        conn.execute("INSERT INTO accounts (account_id, source, name, subtype) VALUES ('ynab:y1','ynab','AMEX Gold Card','open')")
        conn.execute("INSERT INTO sessions VALUES (?, '2099-01-01T00:00:00+00:00')",
                     (__import__("hashlib").sha256(b"tok").hexdigest(),))
        for i, (src, acct, d) in enumerate([("plaid", "p1", "2026-11-01"), ("ynab", "ynab:y1", "2026-11-02")]):
            conn.execute("INSERT INTO transactions (source, id, account_id, date, name, amount) VALUES (?,?,?,?,?,?)",
                         (src, f"t{i}", acct, d, "Shop", 5.0))
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        page = c.get("/compare").text
        assert "Gold Card" in page and "selected" in page  # suggestion preselected
        assert c.post("/api/links", json={"links": {"p1": "ynab:y1", "bogus": "ynab:y1"}}, headers=H).json() == {"ok": True}
        with db.connect() as conn:
            assert [tuple(r) for r in conn.execute("SELECT * FROM account_links")] == [("p1", "ynab:y1")]
        result = compare_all(db, today=__import__("datetime").date(2026, 11, 13))
        assert result["pairs"][0].matched == 1
        assert c.post("/api/links", json={"links": {"p1": ""}}, headers=H).status_code == 200
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM account_links").fetchone()[0] == 0
