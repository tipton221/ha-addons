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


def test_apple_app_site_association_skips_access_and_login(tmp_path, monkeypatch):
    """Apple's servers fetch this without a Cloudflare login or a session; nothing else is exempt."""
    settings = make_settings(tmp_path, monkeypatch, cf_access_team_domain="team.cloudflareaccess.com",
                             cf_access_aud="abc", apple_app_id="ABCDE12345.com.example.finance")
    with TestClient(create_app(settings, background_sync=False)) as c:
        r = c.get("/.well-known/apple-app-site-association", follow_redirects=False)
        assert r.status_code == 200 and r.headers["content-type"] == "application/json"
        assert r.json() == {"webcredentials": {"apps": ["ABCDE12345.com.example.finance"]}}
        assert c.get("/.well-known/other").status_code == 403
        assert c.post("/.well-known/apple-app-site-association").status_code == 403
    settings = make_settings(tmp_path, monkeypatch)  # no app ID set: nothing to serve
    with TestClient(create_app(settings, background_sync=False)) as c:
        assert c.get("/.well-known/apple-app-site-association").status_code == 404


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
        conn.execute("INSERT INTO plaid_items (item_id, access_token, institution) VALUES ('i1', 'tok', 'Acme')")


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
    assert result["Acme"] == {"error": "ITEM_LOGIN_REQUIRED"}


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
    ynab = FakeYNAB([{"id": "y1", "name": "ACME Gold Card", "type": "creditCard", "balance": -50000,
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
    assert acct["account_id"] == "ynab:y1" and acct["current_balance"] == -50.0


def test_suggest_links():
    plaid = [{"account_id": "p1", "name": "Acme Gold Card", "mask": "1111"},
             {"account_id": "p2", "name": "Acme Business Cash(TM)", "mask": "2222"},
             {"account_id": "p3", "name": "Acme Cash Preferred®", "mask": "3333"},
             {"account_id": "p4", "name": "Acme Hotel Card", "mask": "4444"},
             {"account_id": "p5", "name": "Acme Platinum Card®", "mask": "5555"},
             {"account_id": "p6", "name": "Some Other Card", "mask": "9"}]
    ynab = [{"account_id": "ynab:g", "name": "ACME Gold Card", "subtype": "open"},
            {"account_id": "ynab:gold", "name": "Acme Gold Card old", "subtype": "closed"},
            {"account_id": "ynab:bbc", "name": "ACME Business Cash", "subtype": "open"},
            {"account_id": "ynab:bcp", "name": "ACME Cash Preferred", "subtype": "open"},
            {"account_id": "ynab:h", "name": "ACME Hotel", "subtype": "open"},
            {"account_id": "ynab:p", "name": "ACME Platinum Card", "subtype": "open"},
            {"account_id": "ynab:wf", "name": "Savings Bank", "subtype": "open"}]
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
        conn.execute("INSERT INTO plaid_items (item_id, access_token, institution) VALUES ('i1','t','Acme')")
        conn.execute("INSERT INTO accounts (account_id, source, item_id, name) VALUES ('p1','plaid','i1','Gold Card')")
        conn.execute("INSERT INTO accounts (account_id, source, name, subtype) VALUES ('ynab:y1','ynab','ACME Gold Card','open')")
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


def test_shortdate_shows_year_outside_current_year():
    from datetime import datetime as dt
    from app.main import shortdate
    now = dt(2026, 11, 13)
    assert shortdate("2026-11-12", now) == "Nov 12"
    assert shortdate("2024-11-15", now) == "Nov 15, 2024"
    assert shortdate(None, now) == ""


def test_compare_flags_transactions_in_other_ynab_account(tmp_path):
    from datetime import date as _date
    db = Database(tmp_path / "f.db")
    with db.connect() as conn:
        conn.execute("INSERT INTO plaid_items (item_id, access_token, institution) VALUES ('i1','t','Acme')")
        conn.execute("INSERT INTO accounts (account_id, source, item_id, name) VALUES ('p1','plaid','i1','Card Two')")
        conn.execute("INSERT INTO accounts (account_id, source, name, subtype) VALUES "
                     "('ynab:new','ynab','BCP','open'), ('ynab:old','ynab','BCP old','closed')")
        conn.execute("INSERT INTO account_links VALUES ('p1', 'ynab:new')")
        rows = [("plaid", "a", "p1", "2026-01-05", 10.0), ("plaid", "b", "p1", "2026-03-05", 20.0),
                ("ynab", "y1", "ynab:old", "2026-01-06", 10.0), ("ynab", "y2", "ynab:new", "2026-03-05", 20.0)]
        conn.executemany("INSERT INTO transactions (source, id, account_id, date, name, amount) "
                         "VALUES (?, ?, ?, ?, 'Shop', ?)", rows)
    r = compare_all(db, today=_date(2026, 11, 13))["pairs"][0]
    assert r.matched == 1 and [t["id"] for t in r.only_plaid] == ["a"]
    assert r.elsewhere == {"a": "BCP old"}
    assert r.ynab_count == 1 and r.ynab_first == "2026-03-05"


def test_ynab_full_history_reimport_once(tmp_path):
    db = Database(tmp_path / "f.db")
    db.set_kv("ynab_server_knowledge", "99")  # left over from an install that imported only a year
    ynab = FakeYNAB([], [([], 100), ([], 101)])
    asyncio.run(sync_ynab(db, ynab))
    asyncio.run(sync_ynab(db, ynab))
    assert ynab.knowledge_seen == [None, 100]


def test_ynab_client_requests_full_history_first(tmp_path, monkeypatch):
    import httpx
    from app.ynab import YNAB
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"data": {"transactions": [], "server_knowledge": 5}})

    settings = make_settings(tmp_path, monkeypatch, ynab_token="t")
    client = httpx.AsyncClient(base_url="https://api.ynab.com/v1", transport=httpx.MockTransport(handler))
    y = YNAB(settings, client)
    asyncio.run(y.transactions(None))
    asyncio.run(y.transactions(5))
    assert seen == [{"since_date": "2000-01-01"}, {"last_knowledge_of_server": "5"}]


def test_clean_account_names():
    from app.sync import _clean
    assert _clean("EXAMPLE BANK VISA�� CARD ...0000") == "EXAMPLE BANK VISA CARD ...0000"


def test_compare_uses_purchase_date():
    plaid = [dict(ct("plaid", "apple", 1694.94, "2026-09-19"), authorized_date="2026-09-14"),
             dict(ct("plaid", "late", 50.0, "2026-09-19"), authorized_date=None)]
    ynab = [ct("ynab", "apple", 1694.94, "2026-09-14"), ct("ynab", "late", 50.0, "2026-09-13")]
    r = compare_lists(plaid, ynab, "2026-09-01", "2026-09-30")
    assert r.matched == 1 and [t["id"] for t in r.only_plaid] == ["late"]


def test_multiple_ynab_accounts_per_plaid_account(tmp_path, monkeypatch):
    from datetime import date as _date
    import hashlib
    settings = make_settings(tmp_path, monkeypatch)
    app = create_app(settings, background_sync=False)
    db = Database(tmp_path / "finance.db")
    with db.connect() as conn:
        conn.execute("INSERT INTO plaid_items (item_id, access_token, institution) VALUES ('i1','t','Example Bank')")
        conn.execute("INSERT INTO accounts (account_id, source, item_id, name) VALUES ('p1','plaid','i1','New Card')")
        conn.execute("INSERT INTO accounts (account_id, source, name, subtype) VALUES "
                     "('ynab:old','ynab','Old Card Rewards','open'), ('ynab:new','ynab','New Card Visa','open')")
        conn.execute("INSERT INTO sessions VALUES (?, '2099-01-01T00:00:00+00:00')", (hashlib.sha256(b"tok").hexdigest(),))
        rows = [("plaid", "a", "p1", "2025-01-05", 10.0), ("plaid", "b", "p1", "2026-10-26", 20.0),
                ("ynab", "y1", "ynab:old", "2025-01-05", 10.0), ("ynab", "y2", "ynab:new", "2026-10-26", 20.0)]
        conn.executemany("INSERT INTO transactions (source, id, account_id, date, name, amount) "
                         "VALUES (?, ?, ?, ?, 'Shop', ?)", rows)
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        c.post("/api/links", json={"links": {"p1": ["ynab:new", "ynab:old", "ynab:new", "bogus"]}}, headers=H)
        page = c.get("/compare").text
    assert "New Card Visa + Old Card Rewards" in page
    r = compare_all(db, today=_date(2026, 11, 13))["pairs"][0]
    assert r.matched == 2 and not r.only_plaid and r.ynab_count == 2


def test_links_table_migrates_from_single_link(tmp_path):
    import sqlite3
    path = tmp_path / "f.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE account_links (plaid_account_id TEXT PRIMARY KEY, ynab_account_id TEXT NOT NULL)")
    conn.execute("INSERT INTO account_links VALUES ('p1', 'ynab:y1')")
    conn.commit()
    conn.close()
    db = Database(path)
    with db.connect() as conn:
        conn.execute("INSERT INTO account_links VALUES ('p1', 'ynab:y2')")
        rows = sorted(tuple(r) for r in conn.execute("SELECT * FROM account_links"))
    assert rows == [("p1", "ynab:y1"), ("p1", "ynab:y2")]


# ---------------------------------------------------------------- 0.3.0: inbox, bills, search

from datetime import date as D, timedelta  # noqa: E402

from app.bills import add_months, bill_status, coverage, detect, next_payday, save_detected  # noqa: E402


def seed(db, rows, accounts=None):
    import hashlib
    with db.connect() as conn:
        conn.execute("INSERT OR IGNORE INTO plaid_items (item_id, access_token, institution) VALUES ('i1','t','Bank')")
        for a in accounts or []:
            conn.execute("INSERT INTO accounts (account_id, source, item_id, name, type, current_balance, available_balance) "
                         "VALUES (?, 'plaid', 'i1', ?, ?, ?, ?)", a)
        for r in rows:
            conn.execute("INSERT INTO transactions (source, id, account_id, date, name, merchant, amount, pending, "
                         "category, category_detail, reviewed) VALUES ('plaid', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", r)
        conn.execute("INSERT OR IGNORE INTO sessions VALUES (?, '2099-01-01T00:00:00+00:00')",
                     (hashlib.sha256(b"tok").hexdigest(),))


def monthly(prefix, merchant, amount, day, months, category="RENT_AND_UTILITIES", acct="card"):
    out = []
    for i, m in enumerate(months):
        out.append((f"{prefix}{i}", acct, f"2026-{m:02d}-{day:02d}", merchant.upper(), merchant, amount, 0,
                    category, category + "_X", 1))
    return out


def test_detect_recurring_bills(tmp_path):
    db = Database(tmp_path / "f.db")
    rows = (monthly("tm", "Phone Co", 99.10, 11, [7, 8, 9, 10, 11])
            + monthly("rent", "Rent Co", 2000.0, 1, [8, 9, 10, 11])
            + monthly("qfc", "QFC", 40.0, 3, [8, 9, 10, 11], category="FOOD_AND_DRINK")
            + monthly("pay", "Card Payment", 900.0, 20, [8, 9, 10], category="LOAN_PAYMENTS")
            + monthly("old", "Gym", 30.0, 5, [1, 2, 3, 4]))
    rows = [r if r[0][:3] != "pay" else r[:7] + ("LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT", 1) for r in rows]
    rows += [("ins1", "card", "2025-10-19", "INSURER", "Insurer", 500.0, 0, "GENERAL_SERVICES", "X", 1),
             ("ins2", "card", "2026-04-18", "INSURER", "Insurer", 500.0, 0, "GENERAL_SERVICES", "X", 1),
             ("ins3", "card", "2026-10-19", "INSURER", "Insurer", 525.0, 0, "GENERAL_SERVICES", "X", 1)]
    seed(db, rows)
    found = {c.name: c for c in detect(db, D(2026, 11, 13))}
    assert set(found) == {"Phone Co", "Rent Co", "Insurer"}  # not groceries, card payments, or the old gym
    assert found["Phone Co"].cadence == "monthly" and found["Phone Co"].amount == 99.10
    assert found["Insurer"].cadence == "semiannual"
    assert save_detected(db, list(found.values())) == 3
    assert save_detected(db, list(found.values())) == 0  # not added twice
    bills = {b["name"]: b for b in bill_status(db, D(2026, 11, 13))}
    assert bills["Phone Co"]["next_due"] == D(2026, 12, 11)
    assert bills["Rent Co"]["next_due"] == D(2026, 12, 1)
    assert bills["Insurer"]["next_due"] == D(2027, 4, 19)


def test_paydays():
    semi = {"kind": "semimonthly", "days": [10, 25]}
    assert next_payday(semi, D(2026, 11, 13)) == D(2026, 11, 25)
    assert next_payday(semi, D(2026, 11, 26)) == D(2026, 12, 10)
    assert next_payday(semi, D(2027, 4, 1)) == D(2027, 4, 9)  # April 10, 2027 is a Saturday
    assert next_payday({"kind": "biweekly", "anchor": "2026-11-20"}, D(2026, 11, 21)) == D(2026, 12, 4)
    assert next_payday({"kind": "biweekly", "anchor": "2026-11-20"}, D(2026, 11, 13)) == D(2026, 11, 20)
    assert add_months(D(2026, 1, 31), 1) == D(2026, 2, 28)
    # A new job: no payday before the first one.
    new_job = dict(semi, start="2026-12-10")
    assert next_payday(new_job, D(2026, 11, 16)) == D(2026, 12, 10)
    assert next_payday(new_job, D(2026, 12, 10)) == D(2026, 12, 25)
    assert next_payday(new_job, D(2027, 1, 1)) == D(2027, 1, 11)  # Jan 10, 2027 is a Sunday: the Monday after


def test_coverage(tmp_path):
    db = Database(tmp_path / "f.db")
    seed(db, [("p1", "card", "2026-11-12", "X", "X", 50.0, 1, "FOOD_AND_DRINK", "X", 0)],
         accounts=[("cash", "Cash", "depository", 6000.0, 5900.0), ("card", "Gold", "credit", 1200.0, None)])
    with db.connect() as conn:
        conn.execute("INSERT INTO bills (name, match, amount, cadence, due_day) VALUES "
                     "('Phone Co', 'phone co', 200, 'monthly', 20), ('Rent', 'rent co', 2000, 'monthly', 1), "
                     "('Gold fee', 'membership fee', 325, 'yearly', 25)")
        conn.execute("UPDATE bills SET next_due_override='2027-06-25' WHERE name='Gold fee'")
    c = coverage(db, {"kind": "semimonthly", "days": [10, 25]}, D(2026, 11, 13))
    assert c["payday"] == D(2026, 11, 25)
    assert [b["name"] for b in c["due"]] == ["Phone Co"]
    assert c["left"] == 5900 - 1200 - 50 - 200
    assert round(c["annual_reserve"], 2) == round(325 / 12, 2)


def test_inbox_pages_and_review_all(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    db = Database(tmp_path / "finance.db")
    db.set_kv("inbox_initialized", "1")
    seed(db, [("n1", "card", D.today().isoformat(), "NEW", "New Shop", 12.0, 1, "FOOD_AND_DRINK", "X", 0),
              ("n2", "card", D.today().isoformat(), "OTHER", "Other", 3.0, 0, "FOOD_AND_DRINK", "X", 0),
              ("o1", "card", "2025-01-01", "OLD", "Old Shop", 9.0, 0, "FOOD_AND_DRINK", "X", 1)],
         accounts=[("card", "Gold", "credit", 100.0, None)])
    with TestClient(create_app(settings, background_sync=False)) as c:
        c.cookies.set("finance_session", "tok")
        page = c.get("/").text
        inbox_html = page.split('id="inbox"')[1].split("</ul>")[0]
        assert "New Shop" in inbox_html and "Pending" in inbox_html and "Old Shop" not in inbox_html
        assert "Old Shop" in page.split('id="all"')[1]  # reviewed ones list below
        assert c.post("/api/inbox/review-all", json={"ids": ["n1", "n2"]}, headers=H).json()["count"] == 2
        assert "All caught up" in c.get("/").text
        for path in ("/bills", "/search", "/accounts", "/search?q=old&min=1"):
            assert c.get(path).status_code == 200, path
        assert "Old Shop" in c.get("/search?q=old").text


def test_inbox_migration_marks_old_history_reviewed(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    db = Database(tmp_path / "finance.db")
    seed(db, [("old", "card", "2025-01-01", "OLD", "Old", 9.0, 0, None, None, 0),
              ("new", "card", D.today().isoformat(), "NEW", "New", 9.0, 0, None, None, 0)])
    create_app(settings, background_sync=False)
    with db.connect() as conn:
        assert {r["id"]: r["reviewed"] for r in conn.execute("SELECT id, reviewed FROM transactions")} == {"old": 1, "new": 0}


def test_bills_api_and_export(tmp_path, monkeypatch):
    import csv as _csv
    import io as _io
    settings = make_settings(tmp_path, monkeypatch)
    db = Database(tmp_path / "finance.db")
    seed(db, [("a", "card", "2026-10-01", "AMAZON MKTPLACE", "Amazon", 25.5, 0, "GENERAL_MERCHANDISE", "X", 1),
              ("b", "card", "2026-10-02", "PAYMENT THANK YOU", None, -100.0, 0, "LOAN_PAYMENTS", "X", 1)],
         accounts=[("card", "Gold", "credit", 100.0, None)])
    with TestClient(create_app(settings, background_sync=False)) as c:
        c.cookies.set("finance_session", "tok")
        assert c.post("/api/bills", json={"name": "City Power", "amount": "85", "cadence": "monthly",
                                          "due_day": "18"}, headers=H).status_code == 200
        assert c.post("/api/bills", json={"name": "", "amount": "x"}, headers=H).status_code == 400
        with db.connect() as conn:
            bill = dict(conn.execute("SELECT * FROM bills").fetchone())
        assert bill["match"] == "city power" and bill["due_day"] == 18
        c.post(f"/api/bills/{bill['id']}/delete", headers=H)
        assert c.post("/api/paydays", json={"kind": "biweekly", "anchor": "2026-11-20"}, headers=H).status_code == 200
        assert c.post("/api/paydays", json={"kind": "semimonthly", "days": [40]}, headers=H).status_code == 400
        assert c.post("/api/paydays", json={"kind": "semimonthly", "days": [10, 25], "start": "nope"}, headers=H).status_code == 400
        assert c.post("/api/paydays", json={"kind": "semimonthly", "days": [10, 25], "start": "2099-01-10"}, headers=H).status_code == 200
        assert json.loads(db.get_kv("payday_schedule"))["start"] == "2099-01-10"
        assert "starting Jan 10" in c.get("/bills").text
        c.post("/api/paydays", json={"kind": "semimonthly", "days": [10, 25], "start": ""}, headers=H)
        assert "start" not in json.loads(db.get_kv("payday_schedule"))
        r = c.get("/export.csv?q=amazon")
        assert r.headers["content-type"].startswith("text/csv")
        rows = list(_csv.reader(_io.StringIO(r.text)))
        assert rows[0][0] == "date" and len(rows) == 2 and rows[1][6] == "-25.50" and rows[1][7] == "Shopping"


def test_stale_bill_is_flagged_and_not_counted(tmp_path):
    db = Database(tmp_path / "f.db")
    seed(db, monthly("ag", "Old Lease Co", 1500.0, 1, [6, 7, 8, 9]),
         accounts=[("cash", "Cash", "depository", 5000.0, 5000.0)])
    with db.connect() as conn:
        conn.execute("INSERT INTO bills (name, match, amount, cadence) VALUES ('Old Lease Co', 'old lease co', 1500, 'monthly')")
    b = bill_status(db, D(2026, 11, 13))[0]
    assert b["stale"]
    c = coverage(db, {"kind": "semimonthly", "days": [10, 25]}, D(2026, 11, 28))
    assert c["due"] == [] and c["left"] == 5000


def test_bill_matches_similar_amount_only(tmp_path):
    db = Database(tmp_path / "f.db")
    seed(db, [("p1", "card", "2026-03-18", "AMAZON PRIME", "Amazon", 147.34, 0, "GENERAL_SERVICES", "X", 1),
              ("o1", "card", "2026-11-10", "AMAZON MKTPLACE", "Amazon", 13.22, 0, "GENERAL_MERCHANDISE", "X", 1)])
    with db.connect() as conn:
        conn.execute("INSERT INTO bills (name, match, amount, cadence) VALUES ('Amazon Prime', 'amazon', 147.34, 'yearly')")
    b = bill_status(db, D(2026, 11, 13))[0]
    assert b["last_date"] == "2026-03-18" and b["next_due"] == D(2027, 3, 18) and not b["stale"]


# ---------------------------------------------------------------- 0.4.0: merchants, categories, notes

def client_with(tmp_path, monkeypatch, rows, accounts):
    settings = make_settings(tmp_path, monkeypatch)
    db = Database(tmp_path / "finance.db")
    db.set_kv("inbox_initialized", "1")
    seed(db, rows, accounts=accounts)
    return db, create_app(settings, background_sync=False)


def test_default_categories_from_plaid(tmp_path, monkeypatch):
    from app.ledger import resolve
    db, app = client_with(tmp_path, monkeypatch, [
        ("g", "card", "2026-11-01", "CORNER GROCER", "Corner Grocer", 20.0, 0, "FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES", 1),
        ("r", "card", "2026-11-01", "TACO PLACE", "Taco Place", 12.0, 0, "FOOD_AND_DRINK", "FOOD_AND_DRINK_FAST_FOOD", 1),
        ("u", "card", "2026-11-01", "MYSTERY", None, 5.0, 0, None, None, 1)], [("card", "Card A", "credit", 0, None)])
    with db.connect() as conn:
        raw = [dict(r) for r in conn.execute("SELECT * FROM transactions ORDER BY id")]
    rows = resolve(db, raw)
    assert [r["category_info"]["name"] for r in rows] == ["Groceries", "Restaurants", "Other"]


def test_rename_merchant_and_category_scopes(tmp_path, monkeypatch):
    rows = [("a1", "card", "2026-11-01", "SQ *BEAN CAFE 123", "Bean Cafe", 5.0, 0, "FOOD_AND_DRINK", "X", 1),
            ("a2", "card", "2026-11-05", "SQ *BEAN CAFE 456", "Bean Cafe", 6.0, 0, "FOOD_AND_DRINK", "X", 1),
            ("b1", "card", "2026-11-03", "OTHER STORE", "Other Store", 9.0, 0, "GENERAL_MERCHANDISE", "X", 1)]
    db, app = client_with(tmp_path, monkeypatch, rows, [("card", "Card A", "credit", 0, None)])
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        cats = {x["name"]: x["id"] for x in c.get("/api/categories").json()["categories"]}
        # Rename and recategorize the merchant: applies to both past transactions.
        assert c.post("/api/transactions/plaid/a1", json={"display_name": "Morning Coffee",
                      "category_id": cats["Groceries"], "scope": "merchant"}, headers=H).status_code == 200
        for tid in ("a1", "a2"):
            d = c.get(f"/api/transactions/plaid/{tid}").json()
            assert d["display_name"] == "Morning Coffee" and d["category_id"] == cats["Groceries"]
        # A future transaction from the same merchant follows the rule.
        seed(db, [("a3", "card", "2026-11-09", "SQ *BEAN CAFE 789", "Bean Cafe", 7.0, 0, "FOOD_AND_DRINK", "X", 0)])
        d = c.get("/api/transactions/plaid/a3").json()
        assert d["display_name"] == "Morning Coffee" and d["category_id"] == cats["Groceries"]
        # A one-off override changes only that transaction.
        c.post("/api/transactions/plaid/a2", json={"category_id": cats["Gifts"], "scope": "this"}, headers=H)
        assert c.get("/api/transactions/plaid/a2").json()["category_id"] == cats["Gifts"]
        assert c.get("/api/transactions/plaid/a1").json()["category_id"] == cats["Groceries"]
        # Other merchants are untouched.
        assert c.get("/api/transactions/plaid/b1").json()["display_name"] == "Other Store"
        # Renaming back to the bank's name clears the custom name.
        c.post("/api/transactions/plaid/a1", json={"display_name": "Bean Cafe"}, headers=H)
        assert c.get("/api/transactions/plaid/a1").json()["display_name"] == "Bean Cafe"
        assert c.post("/api/transactions/plaid/a1", json={"category_id": 99999}, headers=H).status_code == 400


def test_notes_account_names_and_filters(tmp_path, monkeypatch):
    rows = [("a1", "card", "2026-11-01", "SHOP ONE", "Shop One", 5.0, 0, None, None, 1),
            ("b1", "bank", "2026-11-02", "SHOP TWO", "Shop Two", 6.0, 0, None, None, 1)]
    db, app = client_with(tmp_path, monkeypatch, rows, [("card", "Card A", "credit", 0, None),
                                                         ("bank", "Bank B", "depository", 100, 100)])
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        c.post("/api/transactions/plaid/a1", json={"note": "Birthday gift for a friend"}, headers=H)
        assert c.post("/api/accounts/card/rename", json={"name": "Everyday Card"}, headers=H).status_code == 200
        assert c.post("/api/accounts/nope/rename", json={"name": "X"}, headers=H).status_code == 404
        page = c.get("/").text
        assert "Birthday gift for a friend" in page and "Everyday Card" in page
        # Narrow the list to one account, then back to all.
        all_html = c.get("/?account=bank").text.split('id="all"')[1]
        assert "Shop Two" in all_html and "Shop One" not in all_html
        all_html = c.get("/").text.split('id="all"')[1]
        assert "Shop One" in all_html and "Shop Two" in all_html
        assert "Shop One" in c.get("/search?q=birthday").text


def test_categories_crud(tmp_path, monkeypatch):
    rows = [("a1", "card", "2026-11-01", "SHOP ONE", "Shop One", 5.0, 0, None, None, 1)]
    db, app = client_with(tmp_path, monkeypatch, rows, [("card", "Card A", "credit", 0, None)])
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        new = c.post("/api/categories", json={"name": "Hobbies", "color": "teal"}, headers=H).json()["id"]
        assert c.post("/api/categories", json={"name": "Hobbies"}, headers=H).status_code == 400
        c.post("/api/transactions/plaid/a1", json={"category_id": new, "scope": "merchant"}, headers=H)
        c.post("/api/categories", json={"id": new, "name": "Hobby gear", "color": "pink"}, headers=H)
        assert "Hobby gear" in c.get("/").text
        c.post(f"/api/categories/{new}/delete", headers=H)
        assert c.get("/api/transactions/plaid/a1").json()["category_id"] is not None  # back to the default


def test_paging_and_bill_tag(tmp_path, monkeypatch):
    rows = [(f"t{i:03d}", "card", f"2026-{(i % 9) + 1:02d}-{(i % 27) + 1:02d}", "SHOP", f"Shop {i}", 1.0 + i, 0, None, None, 1)
            for i in range(130)]
    rows.append(("bill1", "card", "2026-11-11", "PHONE CO", "Phone Co", 99.0, 0, "RENT_AND_UTILITIES", "X", 1))
    db, app = client_with(tmp_path, monkeypatch, rows, [("card", "Card A", "credit", 0, None)])
    with db.connect() as conn:
        conn.execute("INSERT INTO bills (name, match, amount, cadence, color) VALUES ('Phone', 'phone co', 99, 'monthly', 'teal')")
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        page = c.get("/").text
        assert page.count('class="txn-item"') == 60 and 'class="pill tag bill-1">↻ Phone' in page
        cursor = page.split('data-before="')[1].split('"')[0]
        more = c.get(f"/api/transactions/page?before={cursor}").text
        assert more.count('class="txn-item"') == 60 and 'data-before="' in more
        cursor2 = more.split('data-before="')[1].split('"')[0]
        last = c.get(f"/api/transactions/page?before={cursor2}").text
        assert last.count('class="txn-item"') == 11 and "data-before" not in last
        assert c.get("/api/transactions/page?before=garbage").status_code == 400


def test_new_columns_added_to_old_database(tmp_path):
    import sqlite3
    path = tmp_path / "f.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE transactions (source TEXT, id TEXT, account_id TEXT, date TEXT, name TEXT, amount REAL, "
                 "PRIMARY KEY (source, id))")
    conn.commit()
    conn.close()
    db = Database(path)
    with db.connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(transactions)")}
    assert {"note", "category_id"} <= cols


# ---------------------------------------------------------------- 0.4.1: colors and varying bills

def test_custom_hex_colors_and_stylesheet(tmp_path, monkeypatch):
    from app.ledger import valid_color
    assert valid_color("teal") == "teal" and valid_color("#A1B2C3") == "#a1b2c3"
    assert valid_color("a1b2c3") == "#a1b2c3" and valid_color("red; }") == "gray"
    db, app = client_with(tmp_path, monkeypatch, [], [("card", "Card A", "credit", 0, None)])
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        cid = c.post("/api/categories", json={"name": "Hobbies", "color": "#12AB34"}, headers=H).json()["id"]
        css = c.get("/colors.css")
        assert css.headers["content-type"].startswith("text/css")
        assert f".cat-{cid} {{ --c: #12ab34; }}" in css.text
        assert ".cat-1 { --c: var(--blue); }" in css.text


def test_varying_bill(tmp_path):
    db = Database(tmp_path / "f.db")
    seed(db, [("e1", "card", "2026-08-16", "CITY POWER", "City Power", 164.25, 0, "RENT_AND_UTILITIES", "X", 1),
              ("e2", "card", "2026-09-16", "CITY POWER", "City Power", 174.77, 0, "RENT_AND_UTILITIES", "X", 1),
              ("e3", "card", "2026-10-16", "CITY POWER", "City Power", 217.01, 0, "RENT_AND_UTILITIES", "X", 1)],
         accounts=[("card", "Card A", "credit", 0, None)])
    found = {x.name: x for x in detect(db, D(2026, 11, 13))}
    assert found["City Power"].varies and found["City Power"].amount == round((164.25 + 174.77 + 217.01) / 3, 2)
    save_detected(db, list(found.values()))
    b = bill_status(db, D(2026, 11, 13))[0]
    assert b["varies"] == 1 and b["amount"] == 185.34 and b["next_due"] == D(2026, 11, 16)
    # A much higher winter bill still gets the bill's tag, because the amount varies.
    from app.ledger import resolve
    tagged = resolve(db, [{"source": "plaid", "id": "e4", "merchant": "City Power", "name": "CITY POWER",
                           "date": "2026-11-17", "amount": 320.0, "category": None, "category_detail": None, "category_id": None}])
    assert tagged[0]["bill"]["name"] == "City Power"


def test_bill_amount_required_unless_varies(tmp_path, monkeypatch):
    db, app = client_with(tmp_path, monkeypatch, [], [("card", "Card A", "credit", 0, None)])
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        assert c.post("/api/bills", json={"name": "Power", "amount": ""}, headers=H).status_code == 400
        assert c.post("/api/bills", json={"name": "Power", "varies": True, "color": "#ff00aa"}, headers=H).status_code == 200
        assert "≈" in c.get("/bills").text


# ---------------------------------------------------------------- 0.4.3: pending to posted, scheduled, icons

def test_posted_charge_keeps_edits_and_records_tip(tmp_path):
    db = Database(tmp_path / "f.db")
    seed_item(db)
    # Recent dates: anything over a week old arrives as already-reviewed history.
    recent = (D.today() - timedelta(days=1)).isoformat()

    def rtxn(*a, **kw):
        return dict(txn(*a, **kw), date=recent)

    pend = dict(rtxn("p1", 18.94, pending=True), merchant_name="Taco Spot",
                personal_finance_category={"primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_RESTAURANT"})
    plaid = FakePlaid([page(added=[pend], cursor="c1")])
    asyncio.run(sync_all(db, plaid))
    with db.connect() as conn:
        conn.execute("UPDATE transactions SET reviewed=1, note='Dinner with friends', category_id=7 WHERE id='p1'")
    posted = dict(rtxn("t1", 21.78, pending_id="p1"), merchant_name="Taco Spot",
                  personal_finance_category=pend["personal_finance_category"])
    big = dict(rtxn("p2", 10.0, pending=True))
    plaid.pages = [page(added=[posted, big], removed=["p1"], cursor="c2")]
    asyncio.run(sync_all(db, plaid))
    plaid.pages = [page(added=[rtxn("t2", 40.0, pending_id="p2")], removed=["p2"], cursor="c3")]
    with db.connect() as conn:
        conn.execute("UPDATE transactions SET reviewed=1 WHERE id='p2'")
    asyncio.run(sync_all(db, plaid))
    with db.connect() as conn:
        t1 = dict(conn.execute("SELECT * FROM transactions WHERE id='t1'").fetchone())
        t2 = dict(conn.execute("SELECT * FROM transactions WHERE id='t2'").fetchone())
    assert (t1["reviewed"], t1["note"], t1["category_id"], t1["pending_amount"]) == (1, "Dinner with friends", 7, 18.94)
    assert t2["reviewed"] == 0 and t2["pending_amount"] == 10.0  # 4x the pending amount: back to the inbox


def test_tip_badge_scheduled_section_and_icons(tmp_path, monkeypatch):
    rows = [("t1", "card", D.today().isoformat(), "TACO SPOT", "Taco Spot", 21.78, 0, "FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT", 1)]
    db, app = client_with(tmp_path, monkeypatch, rows, [("card", "Card A", "credit", 0, None)])
    with db.connect() as conn:
        conn.execute("UPDATE transactions SET pending_amount=18.94 WHERE id='t1'")
        conn.execute("INSERT INTO bills (name, match, amount, cadence, due_day) VALUES ('Phone Co', 'phone co', 99, 'monthly', ?)",
                     ((D.today() + __import__("datetime").timedelta(days=3)).day,))
    with TestClient(app) as c:
        assert c.get("/", follow_redirects=False).status_code == 303  # signed out: redirected
        c.cookies.set("finance_session", "tok")
        page_html = c.get("/").text
        assert "Tip +$2.84 · 15%" in page_html
        assert 'id="scheduled"' in page_html and "Phone Co" in page_html.split('id="scheduled"')[1]
        c.cookies.clear()
        assert c.get("/manifest.json").json()["display"] == "standalone"  # reachable before signing in
        assert c.get("/favicon.ico").headers["content-type"] == "image/png"
        assert c.get("/static/icons/apple-touch-icon.png").status_code == 200


# ---------------------------------------------------------------- 0.4.4: scheduled list follows the account filter

def test_scheduled_all_bills_reverse_order_and_filtered(tmp_path, monkeypatch):
    from datetime import timedelta
    t = D.today()
    rows = [("p1", "card", (t - timedelta(days=20)).isoformat(), "PHONE CO", "Phone Co", 50.0, 0, "X", "X", 1),
            ("s1", "card2", (t - timedelta(days=300)).isoformat(), "STREAM BOX", "Stream Box", 120.0, 0, "X", "X", 1)]
    db, app = client_with(tmp_path, monkeypatch, rows, [("card", "Card A", "credit", 0, None),
                                                       ("card2", "Card B", "credit", 0, None)])
    with db.connect() as conn:
        conn.execute("INSERT INTO bills (name, match, amount, cadence) VALUES ('Phone Co', 'phone co', 50, 'monthly')")
        conn.execute("INSERT INTO bills (name, match, amount, cadence) VALUES ('Stream Box', 'stream box', 120, 'yearly')")
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        sched = c.get("/").text.split('id="scheduled"')[1].split("</details>")[0]
        head, listed = sched.split("</summary>", 1)
        assert "Next" in head and "Phone Co" in head  # the panel names the next bill
        # The yearly bill is ~65 days out (beyond 30) and listed first; the next one is last.
        assert "Stream Box" in listed and listed.index("Stream Box") < listed.index("Phone Co")
        sched = c.get("/?account=card").text.split('id="scheduled-wrap"')[1].split("</details>")[0]
        assert "Phone Co" in sched and "Stream Box" not in sched


def test_bill_account_choice_overrides_last_charge(tmp_path, monkeypatch):
    from datetime import timedelta
    t = D.today()
    rows = [("p1", "card", (t - timedelta(days=20)).isoformat(), "PHONE CO", "Phone Co", 50.0, 0, "X", "X", 1)]
    db, app = client_with(tmp_path, monkeypatch, rows, [("card", "Card A", "credit", 0, None),
                                                       ("card2", "Card B", "credit", 0, None)])
    with db.connect() as conn:
        conn.execute("INSERT INTO bills (name, match, amount, cadence) VALUES ('Phone Co', 'phone co', 50, 'monthly')")
        bid = conn.execute("SELECT id FROM bills").fetchone()["id"]
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        assert "Automatic (Card A)" in c.get("/bills").text
        body = {"id": bid, "name": "Phone Co", "amount": "50", "cadence": "monthly", "account_id": "card2"}
        assert c.post("/api/bills", json=body, headers=H).status_code == 200
        assert "Phone Co" not in c.get("/?account=card").text.split('id="scheduled-wrap"')[1].split('id="all"')[0]
        assert "Phone Co" in c.get("/?account=card2").text.split('id="scheduled-wrap"')[1].split('id="all"')[0]
        assert c.post("/api/bills", json={**body, "account_id": "nope"}, headers=H).status_code == 400
        assert c.post("/api/bills", json={**body, "account_id": ""}, headers=H).status_code == 200  # back to automatic
        assert "Phone Co" in c.get("/?account=card").text.split('id="scheduled-wrap"')[1].split('id="all"')[0]


# ---------------------------------------------------------------- 1.1.0: transfers and card payments

def test_card_payment_pairs_reviews_together_and_skips_search_totals(tmp_path, monkeypatch):
    rows = [("pay", "chk", "2026-11-03", "CARD A AUTOPAY", None, 500.0, 0, "LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT", 0),
            ("rcv", "card", "2026-11-04", "PAYMENT THANK YOU", None, -500.0, 0, "TRANSFER_IN", "TRANSFER_IN_ACCOUNT_TRANSFER", 0),
            ("buy", "card", "2026-11-04", "HARDWARE BARN", "Hardware Barn", 40.0, 0, "GENERAL_MERCHANDISE", "X", 0),
            # Same amount as a purchase refund: neither side is a transfer, so no pair.
            ("ref", "card", "2026-11-05", "HARDWARE BARN", "Hardware Barn", -40.0, 0, "GENERAL_MERCHANDISE", "X", 0)]
    db, app = client_with(tmp_path, monkeypatch, rows, [("chk", "Checking", "depository", 1000, 1000),
                                                       ("card", "Card A", "credit", 0, None)])
    with TestClient(app) as c:  # startup pairs existing history
        c.cookies.set("finance_session", "tok")
        with db.connect() as conn:
            peer = {r["id"]: r["transfer_peer"] for r in conn.execute("SELECT id, transfer_peer FROM transactions")}
        assert peer == {"pay": "rcv", "rcv": "pay", "buy": None, "ref": None}
        page_html = c.get("/").text
        assert "⇄ To Card A" in page_html and "⇄ From Checking" in page_html
        assert c.post("/api/transactions/plaid/pay/review", json={"reviewed": True}, headers=H).json()["peer"] == "rcv"
        with db.connect() as conn:
            assert conn.execute("SELECT reviewed FROM transactions WHERE id='rcv'").fetchone()[0] == 1
        s = c.get("/search?start=2026-11-01").text
        assert "out $40.00 · in $40.00 · not counting 2 transfers" in s


# ---------------------------------------------------------------- 1.2.0: YNAB names

def test_ynab_names_rename_merchants_one_offs_and_memos(tmp_path, monkeypatch):
    import json as _json
    rows = [("c1", "card", "2026-10-01", "SQ *BEAN CFE 0042", None, 4.5, 0, "FOOD_AND_DRINK", "X", 1),
            ("c2", "card", "2026-10-08", "SQ *BEAN CFE 0042", None, 5.25, 0, "FOOD_AND_DRINK", "X", 1),
            ("m1", "card", "2026-10-03", "MKTPLACE PMTS", None, 20.0, 0, "GENERAL_MERCHANDISE", "X", 1),
            ("m2", "card", "2026-10-04", "MKTPLACE PMTS", None, 31.0, 0, "GENERAL_MERCHANDISE", "X", 1),
            ("k1", "card", "2026-10-05", "KEPT NAME CO", None, 9.0, 0, "GENERAL_MERCHANDISE", "X", 1),
            ("s1", "card", "2026-10-06", "PAYPAL *XYZ", None, 12.0, 0, "GENERAL_MERCHANDISE", "X", 1)]
    db, app = client_with(tmp_path, monkeypatch, rows, [("card", "Card A", "credit", 0, None)])
    ynab = [("y1", "2026-10-02", 4.5, "Bean Cafe", "oat latte"), ("y2", "2026-10-08", 5.25, "Bean Cafe", ""),
            ("y3", "2026-10-03", 20.0, "Book Shop", ""), ("y4", "2026-10-05", 31.0, "Toy Store", "gift"),
            ("y5", "2026-10-05", 9.0, "Other Name", ""), ("y6", "2026-10-07", 12.0, "Music App", "")]
    with db.connect() as conn:
        conn.execute("INSERT INTO accounts (account_id, source, name, type) VALUES ('ynab:a', 'ynab', 'Card A', 'creditCard')")
        conn.execute("INSERT INTO account_links VALUES ('card', 'ynab:a')")
        for i, d, amt, payee, memo in ynab:
            conn.execute("INSERT INTO transactions (source, id, account_id, date, name, merchant, amount, pending, raw, reviewed) "
                         "VALUES ('ynab', ?, 'ynab:a', ?, ?, ?, ?, 0, ?, 1)", (i, d, payee, payee, amt, _json.dumps({"memo": memo})))
        conn.execute("INSERT INTO merchant_rules (merchant_key, display_name) VALUES ('kept name co', 'My Name')")
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        r = c.post("/api/ynab-names", json={}, headers=H).json()
        assert r == {"matched": 6, "merchants": 1, "one_off": 3, "notes": 2}
        assert c.get("/api/transactions/plaid/s1").json()["name_is_override"]  # single match: one-off only
        names = {i: c.get(f"/api/transactions/plaid/{i}").json() for i in ("c1", "c2", "m1", "m2", "k1")}
        assert [names[i]["display_name"] for i in ("c1", "c2", "m1", "m2", "k1")] == \
            ["Bean Cafe", "Bean Cafe", "Book Shop", "Toy Store", "My Name"]
        assert names["c1"]["note"] == "oat latte" and names["m2"]["note"] == "gift"
        assert names["m1"]["name_is_override"] and not names["c1"]["name_is_override"]
        # "Just this one" renames only that transaction.
        c.post("/api/transactions/plaid/m1", json={"display_name": "Used Books", "scope": "this"}, headers=H)
        assert c.get("/api/transactions/plaid/m2").json()["display_name"] == "Toy Store"
        assert "Used Books" in c.get("/search?q=used").text
        # "All from this merchant" replaces the one-off names on every transaction from it.
        c.post("/api/transactions/plaid/m1", json={"display_name": "Marketplace", "scope": "merchant"}, headers=H)
        assert [c.get(f"/api/transactions/plaid/{i}").json()["display_name"] for i in ("m1", "m2")] == ["Marketplace"] * 2



# ---------------------------------------------------------------- 1.3.0: editor, bills from transactions, income

def test_similar_merchants_bill_choice_income_and_rows(tmp_path, monkeypatch):
    from datetime import timedelta
    t = D.today()
    day = lambda n: (t - timedelta(days=n)).isoformat()
    rows = [("a1", "card", day(3), "AMAZON MKTPL*AB12CD", None, 20.0, 0, "GENERAL_MERCHANDISE", "X", 1),
            ("a2", "card", day(4), "AMAZON MKTPL*ZZ98YX", None, 30.0, 0, "GENERAL_MERCHANDISE", "X", 1),
            ("a3", "card", day(5), "AMAZON PRIME", None, 14.99, 0, "GENERAL_MERCHANDISE", "X", 1),
            ("g1", "card", day(6), "GYM CLUB", None, 45.0, 0, "PERSONAL_CARE", "X", 1),
            ("p1", "chk", day(9), "ACME HOSPITAL PAYROLL", None, -2000.0, 0, "INCOME", "INCOME_WAGES", 1),
            ("p2", "chk", day(23), "ACME HOSPITAL PAYROLL", None, -2100.0, 0, "INCOME", "INCOME_WAGES", 1)]
    db, app = client_with(tmp_path, monkeypatch, rows, [("card", "Card A", "credit", 0, None),
                                                       ("chk", "Checking", "depository", 5000, 5000)])
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        d = c.get("/api/transactions/plaid/a1").json()
        assert {s["key"] for s in d["similar"]} == {"amazon mktpl zz yx", "amazon prime"}
        # Rename with the similar merchants included: all three become Amazon.
        c.post("/api/transactions/plaid/a1", json={"display_name": "Amazon", "scope": "merchant",
                                                    "similar": [s["key"] for s in d["similar"]] + ["not a real key"]}, headers=H)
        assert {c.get(f"/api/transactions/plaid/{i}").json()["display_name"] for i in ("a1", "a2", "a3")} == {"Amazon"}
        # Make the gym charge a bill from the transaction; the form is filled in.
        form = c.get("/api/bills/form?source=plaid&id=g1").text
        assert 'value="Gym Club"' in form.replace("GYM CLUB", "Gym Club") or 'value="GYM CLUB"' in form
        assert 'data-from-txn="g1"' in form and 'value="45.00"' in form
        bid = c.post("/api/bills", json={"name": "Gym", "match": "gym club", "amount": "45", "from_txn": "g1"},
                     headers=H).json()["id"]
        assert c.get("/api/transactions/plaid/g1").json()["bill"] == "Gym"
        # Mark it "not a bill": the tag goes away, and the bill no longer sees that charge.
        c.post("/api/transactions/plaid/g1", json={"bill_id": "0"}, headers=H)
        assert c.get("/api/transactions/plaid/g1").json()["bill"] is None
        assert "no charge found" in c.get("/bills").text
        # Recurring income: biweekly paycheck averaging the deposits, shown apart from bills.
        c.post("/api/bills", json={"name": "Paycheck", "match": "acme hospital payroll", "kind": "income",
                                   "cadence": "biweekly", "varies": True}, headers=H)
        page = c.get("/bills").text
        assert "Recurring income" in page and "+$2,050.00" in page
        assert c.get("/api/transactions/plaid/p1").json()["bill"] == "Paycheck"
        home = c.get("/").text
        assert "Paycheck" in home.split('id="scheduled-wrap"')[1].split('id="all"')[0]
        # Rows render for in-place refresh.
        r = c.post("/api/rows", json={"rows": [{"id": "a1"}, {"id": "p1", "review": True}]}, headers=H).json()["rows"]
        assert "Amazon" in r["a1"] and 'class="tick"' in r["p1"]
        names = c.get("/api/merchant-names").json()["names"]
        assert names[0] == "Amazon"
        # Categories are alphabetical.
        cats = [x["name"] for x in c.get("/api/categories").json()["categories"]]
        assert cats == sorted(cats, key=str.lower)


def test_paycheck_sets_payday_and_pending_filter(tmp_path, monkeypatch):
    from datetime import timedelta
    t = D.today()
    rows = [("p1", "chk", (t - timedelta(days=4)).isoformat(), "ACME PAYROLL", None, -2000.0, 0, "INCOME", "X", 1),
            ("q1", "card", t.isoformat(), "CORNER CAFE", "Corner Cafe", 6.0, 1, "FOOD_AND_DRINK", "X", 1),
            ("q2", "card", t.isoformat(), "BOOK BARN", "Book Barn", 12.0, 0, "GENERAL_MERCHANDISE", "X", 1)]
    db, app = client_with(tmp_path, monkeypatch, rows, [("card", "Card A", "credit", 0, None),
                                                       ("chk", "Checking", "depository", 1000, 1000)])
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        c.post("/api/bills", json={"name": "Paycheck", "match": "acme payroll", "kind": "income", "amount": "2000",
                                   "cadence": "biweekly", "is_payday": True}, headers=H)
        expected = (t + timedelta(days=10)).isoformat()
        from app.bills import coverage
        assert coverage(db, {"kind": "semimonthly", "days": [1, 15]}, t)["payday"].isoformat() == expected
        assert "From your paycheck" in c.get("/bills").text
        # Pending only: just the pending transaction in the list.
        all_list = c.get("/?pending=1").text.split('id="all"')[1]
        assert "Corner Cafe" in all_list and "Book Barn" not in all_list
        all_list = c.get("/").text.split('id="all"')[1]
        assert "Book Barn" in all_list



def test_similar_merchants_are_chosen_one_by_one_and_renames_can_be_undone(tmp_path, monkeypatch):
    rows = [("d1", "card", "2026-11-01", "WASHINGTON DOL", "Washington DOL", 50.0, 0, "GOVERNMENT_AND_NON_PROFIT", "X", 1),
            ("f1", "card", "2026-11-02", "WASHINGTON STATE FERRIES", "Washington State Ferries", 18.0, 0, "TRAVEL", "X", 1),
            ("p1", "card", "2026-11-03", "WASHINGTON STATE PARKS", "Washington State Parks", 10.0, 0, "TRAVEL", "X", 1)]
    db, app = client_with(tmp_path, monkeypatch, rows, [("card", "Card A", "credit", 0, None)])
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        d = c.get("/api/transactions/plaid/d1").json()
        assert {s["name"] for s in d["similar"]} == {"Washington State Ferries", "Washington State Parks"}
        # Rename DOL and include only the parks.
        c.post("/api/transactions/plaid/d1", json={"display_name": "Washington Department of Licensing",
                                                    "scope": "merchant", "similar": ["washington state parks"]}, headers=H)
        names = {i: c.get(f"/api/transactions/plaid/{i}").json()["display_name"] for i in ("d1", "f1", "p1")}
        assert names == {"d1": "Washington Department of Licensing", "f1": "Washington State Ferries",
                         "p1": "Washington Department of Licensing"}
        # The rename shows up in the list on Accounts, and undoing it restores the bank's name.
        page = c.get("/accounts").text
        assert "Renamed merchants" in page and 'data-key="washington state parks"' in page
        c.post("/api/rules/delete", json={"key": "washington state parks"}, headers=H)
        assert c.get("/api/transactions/plaid/p1").json()["display_name"] == "Washington State Parks"
        assert c.get("/api/transactions/plaid/d1").json()["display_name"] == "Washington Department of Licensing"



def test_bill_tags_need_amount_and_date(tmp_path, monkeypatch):
    rows = [("v1", "card", "2026-09-23", "AMAZON PRIME VIDEO", "Amazon", 8.99, 0, "ENTERTAINMENT", "X", 1),
            ("v2", "card", "2026-10-23", "AMAZON PRIME VIDEO", "Amazon", 8.99, 0, "ENTERTAINMENT", "X", 1),
            ("y1", "card", "2026-03-02", "AMAZON PRIME", "Amazon", 139.0, 0, "GENERAL_MERCHANDISE", "X", 1),
            ("o1", "card", "2026-10-05", "AMAZON MKTPL", "Amazon", 9.20, 0, "GENERAL_MERCHANDISE", "X", 1),
            ("o2", "card", "2026-10-22", "AMAZON MKTPL", "Amazon", 24.00, 0, "GENERAL_MERCHANDISE", "X", 1),
            ("o3", "card", "2026-08-14", "AMAZON MKTPL", "Amazon", 118.0, 0, "GENERAL_MERCHANDISE", "X", 1)]
    db, app = client_with(tmp_path, monkeypatch, rows, [("card", "Card A", "credit", 0, None)])
    with db.connect() as conn:
        conn.execute("INSERT INTO bills (name, match, amount, cadence) VALUES ('Prime Video', 'amazon', 8.99, 'monthly')")
        conn.execute("INSERT INTO bills (name, match, amount, cadence) VALUES ('Prime', 'amazon', 139, 'yearly')")
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        bill = {i: c.get(f"/api/transactions/plaid/{i}").json()["bill"] for i in ("v1", "v2", "y1", "o1", "o2", "o3")}
    # Right amount and date: tagged. Similar amount on the wrong day, wrong amount on the right day,
    # or an order near the annual fee months off schedule: not tagged.
    assert bill == {"v1": "Prime Video", "v2": "Prime Video", "y1": "Prime", "o1": None, "o2": None, "o3": None}


def test_archived_bill_keeps_old_tags_and_leaves_coverage(tmp_path, monkeypatch):
    from datetime import timedelta
    t = D.today()
    months = [t - timedelta(days=30 * n) for n in (3, 2, 1)]
    rows = [(f"r{i}", "chk", d.isoformat(), "AVENUE APTS RENT", None, 1900.0, 0, "RENT_AND_UTILITIES", "X", 1)
            for i, d in enumerate(months)]
    db, app = client_with(tmp_path, monkeypatch, rows, [("chk", "Checking", "depository", 5000, 5000)])
    with db.connect() as conn:
        bid = conn.execute("INSERT INTO bills (name, match, amount, cadence, due_day) VALUES "
                           "('Old Rent', 'avenue apts rent', 1900, 'monthly', ?)", (months[-1].day,)).lastrowid
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        assert c.post(f"/api/bills/{bid}/archive", json={}, headers=H).status_code == 200
        # Old charges keep the tag; the bill is listed under Archived and not in Scheduled.
        assert all(c.get(f"/api/transactions/plaid/r{i}").json()["bill"] == "Old Rent" for i in range(3))
        assert "Archived" in c.get("/bills").text
        assert "Old Rent" not in c.get("/").text.split('id="scheduled-wrap"')[1].split('id="all"')[0]
        # A charge after the archive date is not tagged.
        with db.connect() as conn:
            conn.execute("UPDATE bills SET archived_on=? WHERE id=?", ((months[1] + timedelta(days=1)).isoformat(), bid))
        assert c.get("/api/transactions/plaid/r2").json()["bill"] is None
        assert c.get("/api/transactions/plaid/r1").json()["bill"] == "Old Rent"
        # Restore brings it back.
        c.post(f"/api/bills/{bid}/archive", json={"restore": True}, headers=H)
        assert c.get("/api/transactions/plaid/r2").json()["bill"] == "Old Rent"


def test_tips_manual_automatic_and_cleared(tmp_path, monkeypatch):
    rows = [("r1", "card", "2026-11-01", "CORNER BISTRO", "Corner Bistro", 47.20, 0, "FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT", 1),
            ("g1", "card", "2026-11-01", "CORNER GROCER", "Corner Grocer", 52.00, 0, "FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES", 1),
            ("h1", "card", "2026-11-02", "CLIPPER SHOP", "Clipper Shop", 40.00, 0, "PERSONAL_CARE", "X", 1)]
    db, app = client_with(tmp_path, monkeypatch, rows, [("card", "Card A", "credit", 0, None)])
    with db.connect() as conn:
        conn.execute("UPDATE transactions SET pending_amount=40.00 WHERE id IN ('r1', 'g1')")
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        page = c.get("/").text
        # Restaurant: automatic tip. Groceries: only a difference, not a tip.
        assert "Tip +$7.20 · 18%" in page and "+$12.00 since pending" in page
        # Haircut: a tip entered by hand, percent of the price before the tip.
        c.post("/api/transactions/plaid/h1", json={"tip": "10.00"}, headers=H)
        d = c.get("/api/transactions/plaid/h1").json()
        assert d["tip"] == 10.0 and d["tip_is_manual"] and "Tip +$10.00 · 33%" in c.get("/").text
        assert c.post("/api/transactions/plaid/h1", json={"tip": "40"}, headers=H).status_code == 400
        # Clearing the automatic restaurant tip keeps it off; the change shows as a difference.
        c.post("/api/transactions/plaid/r1", json={"tip": None}, headers=H)
        page = c.get("/").text
        assert "Tip +$7.20" not in page and "+$7.20 since pending" in page
        assert "tip" in c.get("/export.csv").text.splitlines()[0]



def test_filters_reset_accounts_short_names_visibility_and_details(tmp_path, monkeypatch):
    import json as _json
    rows = [("a1", "card", "2026-11-01", "SQ *CORNER CAFE", "Corner Cafe", 6.5, 0, "FOOD_AND_DRINK", "X", 1),
            ("b1", "old", "2026-10-01", "BOOK BARN", "Book Barn", 12.0, 0, "GENERAL_MERCHANDISE", "X", 1)]
    db, app = client_with(tmp_path, monkeypatch, rows, [("card", "Acme Rewards Visa Signature", "credit", 0, None),
                                                       ("old", "Old Store Card", "credit", 25.0, None),
                                                       ("ira", "Retirement IRA", "investment", 0, None)])
    raw = {"transaction_id": "a1", "authorized_datetime": "2026-11-01T18:42:00Z", "payment_channel": "in store",
           "location": {"address": "100 Main St", "city": "Springfield", "region": "WA", "postal_code": "98000",
                        "lat": 47.1, "lon": -122.2, "store_number": "42"},
           "website": "cornercafe.example", "logo_url": "https://plaid-merchant-logos.plaid.com/cafe.png",
           "counterparties": [{"name": "Square", "type": "payment_terminal"}],
           "original_description": "SQ *CORNER CAFE 0042 SPRINGFIELD WA"}
    with db.connect() as conn:
        conn.execute("UPDATE transactions SET raw=? WHERE id='a1'", (_json.dumps(raw),))
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        # Short name on the pill, full name elsewhere; less used goes behind More; archived is hidden.
        c.post("/api/accounts/card/rename", json={"name": "", "short_name": "Rewards", "visibility": "everyday"}, headers=H)
        c.post("/api/accounts/ira/rename", json={"name": "", "visibility": "less"}, headers=H)
        c.post("/api/accounts/old/rename", json={"name": "", "visibility": "archived"}, headers=H)
        home = c.get("/").text
        pills = home.split('id="filters"')[1].split("</div>")[0]
        assert ">Rewards<" in pills and "less-used" in pills and "Retirement IRA" in pills
        assert "Old Store Card" not in pills and "More (1)" in pills
        assert "Acme Rewards Visa Signature" in home.split('id="all"')[1]
        # Filters come from the request only, so a plain reload shows everything.
        assert "Book Barn" not in c.get("/?account=card").text.split('id="all"')[1]
        assert "Book Barn" in c.get("/").text.split('id="all"')[1]
        # The archived account with a balance is flagged on Accounts.
        assert "still counts in coverage" in c.get("/accounts").text
        # Details from Plaid's record.
        d = c.get("/api/transactions/plaid/a1").json()["details"]
        assert d["time"] == "10:42\u202fam" and "channel" not in d and d["store"] == "42"
        assert d["street"] == "100 Main St" and d["place"] == "Springfield, WA 98000"
        assert d["maps"].startswith("https://maps.apple.com/?ll=47.1,-122.2")
        assert d["logo"].endswith("cafe.png") and d["counterparties"] == [{"name": "Square", "type": "payment terminal"}]
        assert d["original"].startswith("SQ *CORNER CAFE 0042")
        assert "https://*.plaid.com" in c.get("/").headers["content-security-policy"]
        assert d["shop"] is None and "mcc" not in d and "confidence" not in d



def test_store_links_for_amazon_and_apple(tmp_path, monkeypatch):
    rows = [("z1", "card", "2026-11-01", "AMZN Mktp US*AB12", "AMZN Mktp US", 15.06, 0, "GENERAL_MERCHANDISE", "X", 1),
            ("z2", "card", "2026-11-01", "Amazon.com*CD34", "Amazon.com", 2.97, 0, "GENERAL_MERCHANDISE", "X", 1),
            ("n1", "card", "2026-11-01", "CORNER CAFE", "Corner Cafe", 6.0, 0, "FOOD_AND_DRINK", "X", 1),
            ("p1", "card", "2026-11-01", "APPLE.COM/BILL", "Apple", 13.99, 0, "ENTERTAINMENT", "X", 1),
            ("p2", "card", "2026-11-01", "APPLECARD GSBANK PAYMENT", "Apple Card", 50.0, 0, "LOAN_PAYMENTS", "X", 1),
            ("p3", "card", "2026-11-01", "APPLE STORE R123", "Apple Store", 99.0, 0, "GENERAL_MERCHANDISE", "X", 1)]
    db, app = client_with(tmp_path, monkeypatch, rows, [("card", "Card A", "credit", 0, None)])
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        links = {i: (c.get(f"/api/transactions/plaid/{i}").json()["details"]["shop"] or {}).get("url")
                 for i in ("z1", "z2", "n1", "p1", "p2", "p3")}
    assert links["z1"] == links["z2"] == "https://www.amazon.com/cpe/yourpayments/transactions" and links["n1"] is None
    assert links["p1"] == "https://reportaproblem.apple.com/?s=6"  # App Store and subscriptions
    assert links["p2"] is None and links["p3"] is None  # Apple Card payment, Apple Store retail


def test_plaid_link_gets_a_per_page_style_nonce(tmp_path, monkeypatch):
    import re
    settings = make_settings(tmp_path, monkeypatch, plaid_client_id="cid", plaid_secret="sec")
    db = Database(tmp_path / "finance.db")
    seed(db, [], accounts=[])
    with TestClient(create_app(settings, background_sync=False)) as c:
        c.cookies.set("finance_session", "tok")
        pages = [c.get("/accounts"), c.get("/accounts")]
    nonces = []
    for r in pages:
        tag = re.search(r'link-initialize\.js" nonce="([^"]+)"', r.text)
        assert tag, "Plaid script tag has a nonce"
        policy = r.headers["content-security-policy"]
        assert f"style-src 'self' 'nonce-{tag.group(1)}'" in policy and "unsafe-inline" not in policy
        nonces.append(tag.group(1))
    assert nonces[0] != nonces[1]  # a new nonce for every page


def test_search_multi_select_flow_and_quick_dates(tmp_path, monkeypatch):
    rows = [("s1", "card", "2026-11-03", "BOOK BARN", "Book Barn", 12.0, 0, "GENERAL_MERCHANDISE", "X", 1),
            ("s2", "card2", "2026-11-04", "CORNER CAFE", "Corner Cafe", 6.0, 0, "FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT", 1),
            ("s3", "card3", "2026-11-05", "TOY TOWN", "Toy Town", 30.0, 0, "GENERAL_MERCHANDISE", "X", 1),
            ("s4", "card", "2026-11-06", "REFUND BOOK BARN", "Book Barn", -5.0, 0, "GENERAL_MERCHANDISE", "X", 1)]
    db, app = client_with(tmp_path, monkeypatch, rows, [("card", "Card A", "credit", 0, None),
                                                       ("card2", "Card B", "credit", 0, None),
                                                       ("card3", "Card C", "credit", 0, None)])
    with db.connect() as conn:
        conn.execute("UPDATE transactions SET note='gift' WHERE id='s3'")
        conn.execute("UPDATE transactions SET tip=1.5 WHERE id='s2'")
        conn.execute("UPDATE transactions SET pending=1 WHERE id='s4'")
        conn.execute("UPDATE transactions SET pending_amount=25 WHERE id='s3'")  # posted at 30
        conn.execute("UPDATE transactions SET category_id=(SELECT id FROM categories WHERE name='Transfers and payments') WHERE id='s1'")
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        def found(qs):
            page = c.get("/search?" + qs).text.split("result")[1]
            return {n for n in ("Book Barn", "Corner Cafe", "Toy Town") if n in page}
        assert found("account=card&account=card2") == {"Book Barn", "Corner Cafe"}
        cats = {x["name"]: x["id"] for x in c.get("/api/categories").json()["categories"]}
        assert found(f"category={cats['Restaurants']}&category={cats['Shopping']}") == {"Book Barn", "Corner Cafe", "Toy Town"}
        assert found("flow=in") == {"Book Barn"} and found("noted=1") == {"Toy Town"}
        assert found("tipped=1") == {"Corner Cafe"}
        assert found("pending=1") == {"Book Barn"} and found("changed=1") == {"Toy Town"}
        assert found("transfers=1") == {"Book Barn"} and found("transfers=1&pending=1") == set()
        page = c.get("/search").text
        assert page.count('class="filter quick-month') == 12 and page.count('class="filter quick-year') == 5
        assert 'type="text" class="lazy-date"' in page
        # A month and a year together read back as both chosen; a whole year as just the year.
        both = c.get("/search?start=2024-01-01&end=2024-01-31").text
        assert 'quick-month on" data-month="1"' in both and 'quick-year on" data-year="2024"' in both
        year = c.get("/search?start=2025-01-01&end=2025-12-31").text
        assert 'quick-year on" data-year="2025"' in year and "quick-month on" not in year
        assert "account=card&amp;account=card2" in c.get("/search?account=card&account=card2").text  # export keeps both
        assert c.get("/export.csv?account=card2").text.count("Corner Cafe") == 1


def test_new_connection_lists_accounts_right_away_and_can_choose_accounts(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch, plaid_client_id="cid", plaid_secret="sec")
    seen = {}

    class LinkPlaid(FakePlaid):
        async def exchange(self, public_token):
            return "access-1", "item-1"

        async def link_token(self, access_token=None, choose_accounts=False, **kw):
            seen["args"] = (access_token, choose_accounts)
            return "link-token"

    fake = LinkPlaid([page(cursor="c1")], accounts=[{"account_id": "new1", "name": "Spare Card", "mask": "0001",
                                                    "type": "credit", "balances": {"current": 0}}])
    db = Database(tmp_path / "finance.db")
    seed(db, [], accounts=[])
    app = create_app(settings, plaid=fake, background_sync=False)
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        assert c.post("/api/plaid/exchange", json={"public_token": "p", "institution": {"name": "Acme Bank"}},
                      headers=H).status_code == 200
        with db.connect() as conn:
            assert conn.execute("SELECT name FROM accounts WHERE account_id='new1'").fetchone()[0] == "Spare Card"
        c.post("/api/plaid/link-token", json={"item_id": "item-1", "accounts": True}, headers=H)
        assert seen["args"] == ("access-1", True)
        assert 'class="ghost choose-accounts"' in c.get("/accounts").text
        # Choosing accounts again: the app reports how many new accounts Plaid now shares.
        assert c.post("/api/plaid/updated", json={"item_id": "item-1", "selected": 2}, headers=H).json()["added"] == 0
        fake._accounts.append({"account_id": "new2", "name": "Other Card", "mask": "0002", "type": "credit",
                               "balances": {"current": 0}})
        assert c.post("/api/plaid/updated", json={"item_id": "item-1", "selected": 2}, headers=H).json()["added"] == 1



def test_each_sync_keeps_one_balance_per_account_per_day(tmp_path):
    db = Database(tmp_path / "f.db")
    seed_item(db)
    acct = {"account_id": "acct1", "name": "Gold", "mask": "1001", "type": "credit",
            "balances": {"current": 52.5, "available": 900.0, "iso_currency_code": "USD"}}
    plaid = FakePlaid([page(cursor="c1"), page(cursor="c2")], accounts=[acct])
    asyncio.run(sync_all(db, plaid))
    acct["balances"]["current"] = 60.0
    asyncio.run(sync_all(db, plaid))
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM balance_history")]
    assert len(rows) == 1 and rows[0]["current_balance"] == 60.0 and rows[0]["available_balance"] == 900.0


def test_net_worth_joins_ynab_history_plaid_and_manual(tmp_path, monkeypatch):
    from datetime import date
    from app.insights import net_worth
    db, app = client_with(tmp_path, monkeypatch, [], [("chk", "Everyday Checking", "depository", 1500.0, 1500.0),
                                                       ("card", "Card A", "credit", 200.0, None)])
    with db.connect() as conn:
        for aid, name, t in (("ynab:chk", "Old Checking", "checking"), ("ynab:ret", "Retirement Plan", "otherAsset")):
            conn.execute("INSERT INTO accounts (account_id, source, name, type, subtype) VALUES (?, 'ynab', ?, ?, 'open')",
                         (aid, name, t))
        for i, (aid, d, amt) in enumerate((("ynab:chk", "2026-09-02", -1000.0), ("ynab:chk", "2026-10-05", -300.0),
                                           ("ynab:ret", "2026-09-02", -5000.0), ("ynab:ret", "2026-10-20", -250.0))):
            conn.execute("INSERT INTO transactions (source, id, account_id, date, name, amount) VALUES "
                         "('ynab', ?, ?, ?, 'x', ?)", (f"y{i}", aid, d, amt))
        conn.execute("INSERT INTO account_links VALUES ('chk', 'ynab:chk')")
        # Plaid balances start on Nov 13; before that the checking account's history comes from YNAB.
        conn.execute("INSERT INTO balance_history (account_id, day, current_balance) VALUES ('chk', '2026-11-13', 1500)")
        conn.execute("INSERT INTO balance_history (account_id, day, current_balance) VALUES ('card', '2026-11-13', 200)")
        conn.execute("INSERT INTO manual_balances (account_id, day, balance) VALUES ('ynab:ret', '2026-11-10', 6000)")
        conn.execute("INSERT INTO accounts (account_id, source, name, type, subtype) VALUES "
                     "('manual:car', 'manual', 'Car loan', 'liability', 'loan')")
        conn.execute("INSERT INTO manual_balances (account_id, day, balance) VALUES ('manual:car', '2026-11-01', 4000)")
    nw = net_worth(db, date(2026, 11, 15))
    by_month = {p["m"]: p for p in nw["points"]}
    # YNAB 1000 + 5000, less the car loan carried back from its first balance (no terms: flat 4000).
    assert by_month["2026-09"]["net"] == 2000.0
    assert by_month["2026-10"]["net"] == 2550.0           # 1300 + 5250 - 4000
    nov = by_month["2026-11"]
    assert nov["assets"] == 7500.0 and nov["debts"] == -4200.0  # Plaid 1500, manual 6000; card 200 and loan 4000
    names = {a["name"]: a for a in nw["accounts"]}
    assert "Old Checking" not in names                     # counted through its Plaid account
    assert names["Retirement Plan"]["source"] == "manual" and names["Car loan"]["value"] == -4000


def test_monthly_cash_flow_buckets(tmp_path, monkeypatch):
    from datetime import date
    from app.insights import monthly
    from app.ledger import list_transactions, resolve
    rows = [("pay", "chk", "2026-10-01", "ACME PAYROLL", "Acme", -3000.0, 0, "INCOME", "INCOME_WAGES", 1),
            ("food", "card", "2026-10-03", "CORNER CAFE", "Corner Cafe", 40.0, 0, "FOOD_AND_DRINK", "X", 1),
            ("back", "card", "2026-10-04", "CORNER CAFE", "Corner Cafe", -10.0, 0, "FOOD_AND_DRINK", "X", 1),
            ("loan", "chk", "2026-10-05", "LOAN SERVICER", "Loan Servicer", 200.0, 0, "LOAN_PAYMENTS", "LOAN_PAYMENTS_STUDENT_LOAN_PAYMENT", 1),
            ("ccpay", "chk", "2026-10-06", "CARD PAYMENT", None, 30.0, 0, "LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT", 1),
            ("inv", "chk", "2026-10-07", "TO BROKERAGE", None, 500.0, 0, "TRANSFER_OUT", "X", 1),
            ("inv2", "brk", "2026-10-07", "FROM CHECKING", None, -500.0, 0, "TRANSFER_IN", "X", 1),
            ("roth", "chk", "2026-10-08", "TO ROTH IRA", None, 250.0, 0, "TRANSFER_OUT", "TRANSFER_OUT_INVESTMENT_AND_RETIREMENT_FUNDS", 1)]
    db, app = client_with(tmp_path, monkeypatch, rows, [("chk", "Checking", "depository", 0, 0),
                                                       ("card", "Card", "credit", 0, None),
                                                       ("brk", "Brokerage", "investment", 0, None)])
    with db.connect() as conn:
        conn.execute("UPDATE transactions SET transfer_peer='inv2' WHERE id='inv'")
        conn.execute("UPDATE transactions SET transfer_peer='inv' WHERE id='inv2'")
    flow = monthly(db, resolve(db, list_transactions(db, limit=0)), date(2026, 10, 31))["months"]
    oct_ = flow[-1]
    assert (oct_["income"], oct_["spending"], oct_["debt"], oct_["saving"]) == (3000.0, 30.0, 200.0, 750.0)
    # A paycheck the bank calls "Other" still counts as income once it's tagged as recurring income.
    with db.connect() as conn:
        conn.execute("UPDATE transactions SET category=NULL, category_detail=NULL WHERE id='pay'")
        conn.execute("INSERT INTO bills (name, match, amount, cadence, kind) VALUES ('Paycheck', 'acme', 3000, 'biweekly', 'income')")
        bill_id = conn.execute("SELECT id FROM bills").fetchone()[0]
        conn.execute("UPDATE transactions SET bill_id=? WHERE id='pay'", (bill_id,))
    assert monthly(db, resolve(db, list_transactions(db, limit=0)), date(2026, 10, 31))["months"][-1]["income"] == 3000.0
    assert oct_["kept"] == 2770.0 and oct_["rate"] == 92
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        page = c.get("/insights")
        assert page.status_code == 200 and 'id="insights-data"' in page.text
        # A category set to count as saving moves its spending into Saved.
        cats = {x["name"]: x for x in c.get("/api/categories").json()["categories"]}
        r = c.post("/api/categories", json={"id": cats["Restaurants"]["id"], "name": "Restaurants",
                                           "color": "orange", "kind": "saving"}, headers=H)
        assert r.status_code == 200
    flow = monthly(db, resolve(db, list_transactions(db, limit=0)), date(2026, 10, 31))["months"]
    assert flow[-1]["spending"] == 0 and flow[-1]["saving"] == 780.0


def test_manual_accounts_and_balances_api(tmp_path, monkeypatch):
    db, app = client_with(tmp_path, monkeypatch, [], [("chk", "Checking", "depository", 0, 0)])
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        r = c.post("/api/manual-accounts", json={"name": "Retirement Plan", "kind": "retirement",
                                                 "balance": "12,500.50", "day": "2026-11-01"}, headers=H)
        aid = r.json()["id"]
        assert aid.startswith("manual:")
        assert c.post("/api/balances", json={"account_id": aid, "balance": "abc"}, headers=H).status_code == 400
        assert c.post("/api/balances", json={"account_id": aid, "balance": "1", "day": "2999-01-01"}, headers=H).status_code == 400
        assert c.post("/api/balances", json={"account_id": "chk", "balance": "1"}, headers=H).status_code == 400  # Plaid
        assert c.post("/api/balances", json={"account_id": aid, "balance": "13000"}, headers=H).status_code == 200
        assert "Retirement Plan" in c.get("/insights").text
        assert c.post("/api/manual-accounts/delete", json={"id": "chk"}, headers=H).status_code == 400
        assert c.post("/api/manual-accounts/delete", json={"id": aid}, headers=H).status_code == 200
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM manual_balances").fetchone()[0] == 0


def test_month_spending_list_and_net_worth_toggle(tmp_path, monkeypatch):
    from datetime import date
    from app.insights import net_worth
    rows = [("a", "card", "2026-10-03", "BOOK BARN", "Book Barn", 40.0, 0, "GENERAL_MERCHANDISE", "X", 1),
            ("b", "card", "2026-10-04", "TOY TOWN", "Toy Town", 90.0, 0, "GENERAL_MERCHANDISE", "X", 1),
            ("c", "card", "2026-10-05", "CARD PAYMENT", None, -130.0, 0, "LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT", 1),
            ("d", "card", "2026-09-05", "TOY TOWN", "Toy Town", 15.0, 0, "GENERAL_MERCHANDISE", "X", 1)]
    db, app = client_with(tmp_path, monkeypatch, rows, [("card", "Card A", "credit", 100.0, None),
                                                       ("chk", "Checking", "depository", 900.0, 900.0)])
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        html = c.get("/api/insights/spending?m=2026-10").text
        assert html.index("Toy Town") < html.index("Book Barn") and "CARD PAYMENT" not in html
        assert "Oct 4" in html  # rows show their date
        assert c.get("/api/insights/spending?m=bad").status_code == 400
        assert net_worth(db, date(2026, 11, 15))["now"]["net"] == 800.0
        assert c.post("/api/accounts/card/net-worth", json={"include": False}, headers=H).status_code == 200
        nw = net_worth(db, date(2026, 11, 15))
        assert nw["now"]["net"] == 900.0 and [a["name"] for a in nw["excluded"]] == ["Card A"]
        assert "Left out" in c.get("/insights").text
        assert c.post("/api/accounts/nope/net-worth", json={"include": True}, headers=H).status_code == 404


def test_amounts_accept_thousands_commas(tmp_path, monkeypatch):
    db, app = client_with(tmp_path, monkeypatch, [], [("card", "Card A", "credit", 0, None)])
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        r = c.post("/api/bills", json={"name": "Big Insurance", "amount": "1,234.50", "cadence": "yearly"}, headers=H)
        assert r.status_code == 200
    with db.connect() as conn:
        assert conn.execute("SELECT amount FROM bills").fetchone()[0] == 1234.5


def test_search_sort_orders(tmp_path, monkeypatch):
    rows = [("s1", "card", "2026-11-03", "BOOK BARN", "Book Barn", 12.0, 0, "GENERAL_MERCHANDISE", "X", 1),
            ("s2", "card", "2026-11-04", "CORNER CAFE", "Corner Cafe", 6.0, 0, "FOOD_AND_DRINK", "X", 1),
            ("s3", "card", "2026-11-05", "ACORN TOYS", "Acorn Toys", 30.0, 0, "GENERAL_MERCHANDISE", "X", 1)]
    db, app = client_with(tmp_path, monkeypatch, rows, [("card", "Card A", "credit", 0, None)])
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        def order(qs):
            page = c.get("/search?" + qs).text.split("result")[1]
            return sorted(("Book Barn", "Corner Cafe", "Acorn Toys"), key=page.index)
        assert order("min=1") == ["Acorn Toys", "Corner Cafe", "Book Barn"]          # newest first
        assert order("min=1&sort=oldest") == ["Book Barn", "Corner Cafe", "Acorn Toys"]
        assert order("min=1&sort=name") == ["Acorn Toys", "Book Barn", "Corner Cafe"]
        assert order("min=1&sort=largest") == ["Acorn Toys", "Book Barn", "Corner Cafe"]
        assert order("min=1&sort=smallest") == ["Corner Cafe", "Book Barn", "Acorn Toys"]
        assert "Nothing found" in c.get("/search?q=zzz&sort=name").text


def test_bill_learns_merchant_from_hand_tagged_charge(tmp_path, monkeypatch):
    from app.ledger import list_transactions, resolve
    # The bank calls these "ACME INVEST XFER"; the bill's own match text ("brokerage deposit") never appears.
    rows = [(f"x{m}", "chk", f"2026-{m:02d}-{15 + (m % 3)}", "ACME INVEST XFER", None, 250.0, 0, "TRANSFER_OUT", "X", 1)
            for m in range(5, 11)]
    rows.append(("other", "chk", "2026-10-02", "ACME INVEST XFER", None, 1900.0, 0, "TRANSFER_OUT", "X", 1))
    db, app = client_with(tmp_path, monkeypatch, rows, [("chk", "Checking", "depository", 0, 0)])
    with db.connect() as conn:
        conn.execute("INSERT INTO bills (name, match, amount, cadence, due_day) "
                     "VALUES ('Brokerage deposit', 'brokerage deposit', 250, 'monthly', 15)")
        bill_id = conn.execute("SELECT id FROM bills").fetchone()[0]
    tagged = lambda: {t["id"] for t in resolve(db, list_transactions(db, limit=0)) if t["bill"]}
    assert tagged() == set()
    with db.connect() as conn:
        conn.execute("UPDATE transactions SET bill_id=? WHERE id='x10'", (bill_id,))
    assert tagged() == {f"x{m}" for m in range(5, 11)}  # every monthly $250, a day or two off the 15th too


def test_find_bills_asks_before_adding_and_search_finds_bill_names(tmp_path, monkeypatch):
    rows = (monthly("tm", "Phone Co", 99.10, 11, [7, 8, 9, 10, 11])
            + monthly("st", "Photo App", 22.99, 4, [7, 8, 9, 10, 11])
            + monthly("rent", "Rent Co", 2000.0, 1, [8, 9, 10, 11]))
    db, app = client_with(tmp_path, monkeypatch, rows, [("card", "Card A", "credit", 0, None)])
    H = {"X-Finance": "1"}
    def count():
        with db.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM bills").fetchone()[0]
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        r = c.post("/api/bills/detect", json={}, headers=H).json()
        assert r["count"] == 3 and "Phone Co" in r["html"] and count() == 0  # shown, not added
        ok = c.post("/api/bills/add-detected", json={"add": ["phone co"], "ignore": ["rent co"]}, headers=H).json()
        assert ok["added"] == 1 and count() == 1
        r = c.post("/api/bills/detect", json={}, headers=H).json()
        assert r["count"] == 1 and "Photo App" in r["html"]  # rent set aside for good, phone already added
        with db.connect() as conn:
            conn.execute("UPDATE bills SET name='Creative Suite Plan', match='photo app', amount=22.99, due_day=4 WHERE match='phone co'")
        page = c.get("/search?q=suite").text
        assert "Photo App" in page and "Phone Co" not in page


def test_payment_in_transit_both_directions(tmp_path):
    from app.bills import coverage
    from app.insights import net_worth
    from app.ledger import pair_transfers
    from app.ledger import seed_categories
    from app.transit import in_transit
    today = D(2026, 11, 15)
    PAY = ("LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
    rows = []
    for m in (8, 9, 10):  # past payments from savings to the card, both sides posted
        rows += [(f"o{m}", "sav", f"2026-{m:02d}-10", "CARD CO PAYMENT", None, 400.0, 0, *PAY, 1),
                 (f"i{m}", "card", f"2026-{m:02d}-11", "PAYMENT RECEIVED", None, -400.0, 0, *PAY, 1)]
    db = Database(tmp_path / "f.db")
    seed(db, rows, accounts=[("sav", "Savings Co", "depository", 5000.0, 5000.0),
                             ("card", "Card A", "credit", 1000.0, None)])
    seed_categories(db)  # as the app does at startup
    with db.connect() as conn:
        assert pair_transfers(conn) == 3
    sched = {"kind": "semimonthly", "days": [1, 15]}

    def add(*r):
        seed(db, [r])

    def state():
        t, cov = in_transit(db, today), coverage(db, sched, today)
        acc = {a["id"]: a["value"] for a in net_worth(db, today)["accounts"]}
        return t, cov, acc

    # The card shows the payment first: savings still has the money, so it is taken off there.
    add("p1", "card", "2026-11-14", "PAYMENT RECEIVED", None, -3000.0, 0, *PAY, 0)
    t, cov, acc = state()
    assert [(i["other"], i["waiting"]) for i in t["items"]] == [("sav", True)]
    assert cov["cash"] == 2000.0 and cov["transit_cash"] == -3000.0
    assert acc["sav"] == 2000.0
    # Savings shows it pending: available balance already counts it, current balance doesn't yet.
    add("p1s", "sav", "2026-11-15", "CARD CO PAYMENT", None, 3000.0, 1, None, None, 0)
    t, cov, acc = state()
    assert cov["transit_cash"] == 0 and acc["sav"] == 2000.0 and t["items"][0]["other_pending"]
    # It posts: the two sides pair and nothing is in transit.
    with db.connect() as conn:
        conn.execute("UPDATE transactions SET pending=0 WHERE id='p1s'")
        pair_transfers(conn)
    t, cov, acc = state()
    assert t["items"] == [] and t["nw"] == {}

    # The other way: savings sends a payment the card hasn't shown yet, so the card owes less.
    add("p2", "sav", "2026-11-13", "CARD CO PAYMENT", None, 700.0, 0, *PAY, 0)
    t, cov, acc = state()
    assert cov["cards"] == 300.0 and cov["transit_cards"] == -700.0
    assert acc["card"] == -300.0

    # After 7 days with no match it is listed for review but no longer counted.
    add("p3", "card", "2026-11-03", "PAYMENT RECEIVED", None, -50.0, 0, *PAY, 0)
    late = [i for i in in_transit(db, today)["items"] if i["id"] == "p3"]
    assert late and late[0]["late"] and in_transit(db, today)["cards"] == -700.0
    # "Not a transfer between my accounts" drops it.
    with db.connect() as conn:
        conn.execute("INSERT INTO transit_choices VALUES ('p2', '')")
    assert in_transit(db, today)["items"][0]["id"] != "p2" and in_transit(db, today)["cards"] == 0
    # A different transfer from the same account (a description its paired transfers never had) is left alone.
    add("fee", "sav", "2026-11-14", "SWEEP TO BROKERAGE", None, 25.0, 0, "TRANSFER_OUT", "TRANSFER_OUT_X", 0)
    assert "fee" not in {i["id"] for i in in_transit(db, today)["items"]}
    # Rows carry the in-transit item, for the pill in transaction lists.
    from app.ledger import list_transactions, resolve
    with db.connect() as conn:
        conn.execute("DELETE FROM transit_choices")
    flagged = {t["id"] for t in resolve(db, list_transactions(db, limit=0), today) if t["in_transit"]}
    assert flagged == {"p2"}  # p3 is past 7 days, so no pill
    # Only rows in a category that counts as a transfer: one moved to another category is left alone.
    with db.connect() as conn:
        food = conn.execute("SELECT id FROM categories WHERE kind='spending' LIMIT 1").fetchone()[0]
        conn.execute("UPDATE transactions SET category_id=? WHERE id='p2'", (food,))
    assert "p2" not in {i["id"] for i in in_transit(db, today)["items"]}


def test_investment_returns_leave_out_money_put_in(tmp_path, monkeypatch):
    from app.investments import returns
    db, app = client_with(tmp_path, monkeypatch, [], [("roth", "Test Roth IRA", "investment", 5600.0, None)])
    today = D(2026, 11, 15)
    with db.connect() as conn:
        conn.execute("UPDATE accounts SET subtype='roth' WHERE account_id='roth'")
        # A plan entered by hand: $10,000 in December, $1,000 put in on July 1, $13,000 now.
        conn.execute("INSERT INTO accounts (account_id, source, name, type, subtype) VALUES "
                     "('manual:plan', 'manual', 'Old 403b', 'asset', 'retirement')")
        for day, bal, flow in (("2025-12-01", 10000, None), ("2026-07-01", 12000, 1000), ("2026-11-15", 13000, None)):
            conn.execute("INSERT INTO manual_balances (account_id, day, balance, flow) VALUES ('manual:plan', ?, ?, ?)",
                         (day, bal, flow))
        # The Roth: Plaid balance history since Sep 30, and $250 contributions from Plaid Investments.
        conn.execute("INSERT INTO balance_history (account_id, day, current_balance) VALUES ('roth', '2026-09-30', 5000)")
        conn.execute("INSERT INTO balance_history (account_id, day, current_balance) VALUES ('roth', '2026-11-15', 5600)")
        for i, m in enumerate((10, 11)):
            conn.execute("INSERT INTO investment_txns (id, account_id, date, name, amount, type, subtype) "
                         "VALUES (?, 'roth', ?, 'Contribution', -250, 'cash', 'contribution')", (f"c{i}", f"2026-{m:02d}-01"))
        conn.execute("INSERT INTO investment_txns (id, account_id, date, name, amount, type, subtype) "
                     "VALUES ('buy', 'roth', '2026-10-02', 'Buy fund', 250, 'buy', 'buy')")  # not a flow
    r = returns(db, today)
    assert r["chart"][0]["d"] == "2025-12-01" and r["chart"][-1] == {"d": "2026-11-15", "v": 18600.0}
    plan = next(a for a in r["accounts"] if a["id"] == "manual:plan")
    ytd = plan["periods"]["ytd"]
    # The $1,000 is spread over the 30 weeks from Dec 1 to Jul 1: 4 of them fall in December, before YTD.
    assert ytd["put_in"] == round(1000 * 26 / 30, 2) and ytd["gain"] == round(2000 + 1000 * 4 / 30, 2)
    assert ytd["since"] is None and 19.5 < ytd["pct"] < 21.0
    roth = next(a for a in r["accounts"] if a["id"] == "roth")
    three = roth["periods"]["3m"]  # history starts inside the period: counts from its first balance
    assert three["since"] == "2026-09-30" and three["put_in"] == 500.0 and three["gain"] == 100.0
    assert r["value"] == 18600.0 and r["totals"]["ytd"]["value"] == 18600.0


def test_contribution_limits_and_tax_year(tmp_path, monkeypatch):
    from app.investments import limits
    db, app = client_with(tmp_path, monkeypatch, [], [("roth", "Test Roth IRA", "investment", 0.0, None)])
    with db.connect() as conn:
        conn.execute("UPDATE accounts SET subtype='roth' WHERE account_id='roth'")
        for tid, day, amt in (("a", "2026-01-10", 500), ("b", "2026-03-01", 250), ("c", "2026-06-01", 250)):
            conn.execute("INSERT INTO investment_txns (id, account_id, date, amount, type, subtype) "
                         "VALUES (?, 'roth', ?, ?, 'cash', 'contribution')", (tid, day, -amt))
    H = {"X-Finance": "1"}
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        y = limits(db, D(2026, 11, 15))["years"]
        assert [x["year"] for x in y] == [2026] and y[0]["ira"] == 1000.0 and y[0]["ira_limit"] == 7500
        assert c.post("/api/investments/tax-year", json={"id": "a", "year": 2025}, headers=H).status_code == 200
        assert c.post("/api/investments/tax-year", json={"id": "a", "year": 2024}, headers=H).status_code == 400
        assert limits(db, D(2026, 11, 15))["years"][0]["ira"] == 500.0
        early = limits(db, D(2026, 3, 2))["years"]  # before April 15 last year shows too
        assert [(x["year"], x["ira"]) for x in early] == [(2026, 500.0), (2025, 500.0)]
        assert c.post("/api/investments/plan", json={"year": 2026, "plan": "403b", "amount": "6,120.50"},
                      headers=H).status_code == 200
        plan = limits(db, D(2026, 11, 15))["years"][0]["plans"][0]
        assert plan["amount"] == 6120.5 and plan["limit"] == 24500
        page = c.get("/insights").text
        assert "Contribution limits" in page and "Investments" in page


def test_sync_investments_and_consent(tmp_path):
    from app.sync import sync_investments
    db = Database(tmp_path / "f.db")
    seed_item(db)

    class InvPlaid:
        def __init__(self, fail=None):
            self.fail, self.ranges = fail, []

        async def investments_holdings(self, token):
            if self.fail:
                raise PlaidError(400, {"error_code": self.fail})
            return {"accounts": [{"account_id": "roth"}],
                    "holdings": [{"account_id": "roth", "security_id": "s1", "quantity": 10,
                                  "institution_value": 1200.0, "cost_basis": 1000.0}],
                    "securities": [{"security_id": "s1", "name": "Total Market Fund", "ticker_symbol": "TMF", "type": "etf"}]}

        async def investments_transactions(self, token, start, end):
            self.ranges.append((start, end))
            return {"investment_transactions": [
                {"investment_transaction_id": "t1", "account_id": "roth", "date": "2026-11-01", "name": "Deposit",
                 "amount": -250.0, "type": "cash", "subtype": "contribution"}], "securities": []}

    item = {"item_id": "i1", "access_token": "tok", "investments_synced": None}
    p = InvPlaid()
    assert asyncio.run(sync_investments(db, p, item)) == {"holdings": 1, "activity": 1}
    with db.connect() as conn:
        assert conn.execute("SELECT investments FROM plaid_items").fetchone()[0] == "on"
        assert conn.execute("SELECT ticker, value FROM holdings").fetchone()[:] == ("TMF", 1200.0)
        assert conn.execute("SELECT amount FROM investment_txns WHERE id='t1'").fetchone()[0] == -250.0
    first_start = D.fromisoformat(p.ranges[0][0])
    assert (D.fromisoformat(p.ranges[0][1]) - first_start).days == 730  # full history the first time
    item["investments_synced"] = "2026-11-15"
    asyncio.run(sync_investments(db, p, item))
    assert (D.fromisoformat(p.ranges[1][1]) - D.fromisoformat(p.ranges[1][0])).days == 730  # every time
    assert asyncio.run(sync_investments(db, InvPlaid("ADDITIONAL_CONSENT_REQUIRED"), item)) == {"investments": "consent"}
    with db.connect() as conn:
        assert conn.execute("SELECT investments FROM plaid_items").fetchone()[0] == "consent"



def test_manual_flow_spread_matches_record_keeper(tmp_path, monkeypatch):
    """A year of payroll contributions entered once lands near the record keeper's own rate of return."""
    from app.investments import returns
    db, app = client_with(tmp_path, monkeypatch, [], [])
    with db.connect() as conn:
        conn.execute("INSERT INTO accounts (account_id, source, name, type, subtype) VALUES "
                     "('manual:p', 'manual', 'Plan', 'asset', 'retirement')")
        conn.execute("INSERT INTO manual_balances (account_id, day, balance) VALUES ('manual:p', '2025-12-31', 50000)")
        conn.execute("INSERT INTO manual_balances (account_id, day, balance, flow) VALUES ('manual:p', '2026-11-14', 67000, 12500)")
    ytd = returns(db, D(2026, 11, 15))["accounts"][0]["periods"]["ytd"]
    assert ytd["gain"] == 4500.0 and 7.5 < ytd["pct"] < 8.5  # lumped on Nov 14 it would be about 9%


def test_entered_balances_anchor_gains_and_ira_transfers_count(tmp_path, monkeypatch):
    from app.investments import limits, returns
    rows = [("y0", "ynab:plan", "2025-06-01", "Starting balance", None, -30000.0, 0, None, None, 1),
            ("y1", "ynab:plan", "2025-12-15", "Balance adjustment", None, -15000.0, 0, None, None, 1)]
    db, app = client_with(tmp_path, monkeypatch, [], [("roth", "Test Roth IRA", "investment", 0.0, None)])
    with db.connect() as conn:
        conn.execute("INSERT INTO accounts (account_id, source, name, type) VALUES ('ynab:plan', 'ynab', 'Plan', 'otherAsset')")
        for r in rows:
            conn.execute("INSERT INTO transactions (source, id, account_id, date, name, merchant, amount, pending, "
                         "category, category_detail, reviewed) VALUES ('ynab', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", r)
        conn.execute("UPDATE accounts SET track_returns=1 WHERE account_id='ynab:plan'")
        conn.execute("INSERT INTO manual_balances (account_id, day, balance) VALUES ('ynab:plan', '2025-12-31', 50000)")
        conn.execute("INSERT INTO manual_balances (account_id, day, balance, flow) VALUES ('ynab:plan', '2026-11-14', 67000, 12500)")
        conn.execute("UPDATE accounts SET subtype='roth' WHERE account_id='roth'")
        conn.execute("INSERT INTO investment_txns (id, account_id, date, name, amount, type, subtype) VALUES "
                     "('t1', 'roth', '2026-10-15', 'Transfer in', -250, 'transfer', 'transfer')")
        conn.execute("INSERT INTO investment_txns (id, account_id, date, name, amount, type, subtype) VALUES "
                     "('t2', 'roth', '2026-11-05', 'Promotion', -3.01, 'cash', 'deposit')")
    plan = next(a for a in returns(db, D(2026, 11, 15))["accounts"] if a["id"] == "ynab:plan")
    # YNAB's balances (contributions mixed with growth) are left out: history starts at the first entry.
    assert plan["first"] == "2025-12-31" and plan["periods"]["all"]["since"] == "2025-12-31"
    assert plan["periods"]["ytd"]["gain"] == 4500.0
    assert plan["periods"]["3m"]["gain"] is None and plan["periods"]["3m"]["needs"] == "2026-08-15"
    assert limits(db, D(2026, 11, 15))["years"][0]["ira"] == 253.01
    H = {"X-Finance": "1"}
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        assert c.post("/api/investments/tax-year", json={"id": "t2", "year": 0}, headers=H).status_code == 200
        assert c.post("/api/investments/tax-year", json={"id": "t1", "year": 2025}, headers=H).status_code == 400
        assert "?v=" in c.get("/insights").text
    y = limits(db, D(2026, 11, 15))["years"][0]
    assert y["ira"] == 250.0 and [c["id"] for c in y["ira_ignored"]] == ["t2"]



def test_ira_contributions_from_ynab_before_plaid_activity(tmp_path, monkeypatch):
    import json as _json
    from app.investments import limits
    db, app = client_with(tmp_path, monkeypatch, [], [("roth", "Test Roth IRA", "investment", 0.0, None)])
    with db.connect() as conn:
        conn.execute("UPDATE accounts SET subtype='roth' WHERE account_id='roth'")
        conn.execute("INSERT INTO account_links VALUES ('roth', 'ynab:r')")
        for i, m in enumerate(range(1, 11)):
            conn.execute("INSERT INTO transactions (source, id, account_id, date, name, amount, raw, reviewed) "
                         "VALUES ('ynab', ?, 'ynab:r', ?, 'Transfer : Savings', -250, ?, 1)",
                         (f"y{i}", f"2026-{m:02d}-15", _json.dumps({"transfer_account_id": "x"})))
        conn.execute("INSERT INTO transactions (source, id, account_id, date, name, amount, raw, reviewed) "
                     "VALUES ('ynab', 'adj', 'ynab:r', '2026-06-30', 'Market change', -400, '{}', 1)")  # not a transfer
        conn.execute("INSERT INTO investment_txns (id, account_id, date, name, amount, type, subtype) VALUES "
                     "('p1', 'roth', '2026-10-01', 'Deposit', -250, 'cash', 'deposit')")  # Plaid from Oct 1 on
    y = limits(db, D(2026, 11, 15))["years"][0]
    # Jan to Sep from YNAB (Oct's YNAB transfer overlaps Plaid's activity), plus Plaid's Oct 1 deposit.
    assert y["ira"] == 2500.0 and {c["source"] for c in y["ira_items"]} == {"YNAB", "Plaid"}
    H = {"X-Finance": "1"}
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        assert c.post("/api/investments/tax-year", json={"id": "ynab:y5", "year": 2025}, headers=H).status_code == 400
        assert c.post("/api/investments/tax-year", json={"id": "ynab:y0", "year": 0}, headers=H).status_code == 200
    assert limits(db, D(2026, 11, 15))["years"][0]["ira"] == 2250.0


def test_ira_contributions_from_a_recurring_bill(tmp_path, monkeypatch):
    """Transfers tagged with the IRA's bill count, once each, alongside what YNAB or Plaid reports."""
    import json as _json
    from app.investments import limits
    rows = [(f"c{m}", "sav", f"2026-{m:02d}-15", "TO ROTH IRA", "Roth Transfer", 250.0, 0, "TRANSFER_OUT", "X", 1)
            for m in (9, 10, 11)]
    rows.append(("cp", "sav", "2026-11-17", "TO ROTH IRA", "Roth Transfer", 250.0, 1, "TRANSFER_OUT", "X", 0))  # pending
    db, app = client_with(tmp_path, monkeypatch, rows, [("sav", "Savings Co", "depository", 0, 0),
                                                        ("roth", "Test Roth IRA", "investment", 0.0, None)])
    with db.connect() as conn:
        conn.execute("UPDATE accounts SET subtype='roth' WHERE account_id='roth'")
        conn.execute("INSERT INTO account_links VALUES ('roth', 'ynab:r')")
        for m in (9, 10):  # YNAB already has September and October
            conn.execute("INSERT INTO transactions (source, id, account_id, date, name, amount, raw, reviewed) "
                         "VALUES ('ynab', ?, 'ynab:r', ?, 'Transfer : Savings', -250, ?, 1)",
                         (f"y{m}", f"2026-{m:02d}-16", _json.dumps({"transfer_account_id": "x"})))
        conn.execute("INSERT INTO bills (name, match, amount, cadence, due_day) VALUES ('Roth transfer', 'roth transfer', 250, 'monthly', 15)")
        bill = conn.execute("SELECT id FROM bills").fetchone()[0]
    assert limits(db, D(2026, 11, 20))["years"][0]["ira"] == 500.0
    H = {"X-Finance": "1"}
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        assert c.post("/api/investments/contribution-bill", json={"account_id": "roth", "bill_id": "999"}, headers=H).status_code == 400
        assert c.post("/api/investments/contribution-bill", json={"account_id": "roth", "bill_id": str(bill)}, headers=H).status_code == 200
        y = limits(db, D(2026, 11, 20))["years"][0]
        # Sep and Oct from YNAB (the tagged transfers a day earlier match them), Nov from the bill; pending waits.
        assert y["ira"] == 750.0
        assert sorted(c_["source"] for c_ in y["ira_items"]) == ["YNAB", "YNAB", "the recurring bill"]
        assert c.post("/api/investments/tax-year", json={"id": "bill:c11", "year": 0}, headers=H).status_code == 200
        assert limits(db, D(2026, 11, 20))["years"][0]["ira"] == 500.0
        assert "contributions are the recurring bill" in c.get("/insights").text
        assert c.post("/api/investments/contribution-bill", json={"account_id": "roth", "bill_id": ""}, headers=H).status_code == 200
    assert limits(db, D(2026, 11, 20))["years"][0]["ira"] == 500.0


def test_loans_pslf_count_and_projection(tmp_path, monkeypatch):
    from app.loans import loans, project
    # Monthly payments to a loan servicer, tagged with a bill.
    rows = [(f"m{m}", "chk", f"2026-{m:02d}-08", "LOAN SERVICER", "Loan Servicer", 250.0, 0, "LOAN_PAYMENTS",
             "LOAN_PAYMENTS_STUDENT_LOAN_PAYMENT", 1) for m in range(6, 12)]
    rows.append(("extra", "chk", "2026-09-20", "LOAN SERVICER", "Loan Servicer", 250.0, 0, "LOAN_PAYMENTS", "X", 1))
    db, app = client_with(tmp_path, monkeypatch, rows, [("chk", "Checking", "depository", 0, 0)])
    with db.connect() as conn:
        conn.execute("INSERT INTO bills (name, match, amount, cadence, due_day) VALUES ('Loan servicer', 'loan servicer', 250, 'monthly', 8)")
        bill = conn.execute("SELECT id FROM bills").fetchone()[0]
    H = {"X-Finance": "1"}
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        r = c.post("/api/loans", json={"name": "Student loans", "balance": "24,000.00", "day": "2026-11-15",
                                       "rate": "3.4", "payment": "250.00", "bill_id": str(bill), "pslf": True,
                                       "pslf_count": "94", "pslf_as_of": "2026-09-06", "recert": "2027-04-08"}, headers=H)
        assert r.status_code == 200
        aid = r.json()["id"]
        today = D(2026, 11, 15)
        (loan,) = loans(db, today)
        p = loan["pslf_info"]
        # Sep (two payments: one month), Oct, Nov after Sep 6: 94 + 3.
        assert loan["balance"] == 24000.0 and p["count"] == 97 and [m["month"] for m in p["months"]] == ["2026-09", "2026-10", "2026-11"]
        # October and November were between jobs.
        for m in ("2026-10", "2026-11"):
            assert c.post("/api/loans/month", json={"account_id": aid, "month": m, "counts": False}, headers=H).status_code == 200
        p = loans(db, today)[0]["pslf_info"]
        assert p["count"] == 95 and p["left"] == 25
        # The form saves every month's choice at once.
        assert c.post("/api/loans", json={"account_id": aid, "rate": "3.4", "payment": "250", "bill_id": str(bill),
                                          "pslf": True, "pslf_count": "94", "pslf_as_of": "2026-09-06",
                                          "recert": "2027-04-08", "months": {"2026-10": True, "2026-11": False}},
                      headers=H).status_code == 200
        assert loans(db, today)[0]["pslf_info"]["count"] == 96
        c.post("/api/loans", json={"account_id": aid, "rate": "3.4", "payment": "250", "bill_id": str(bill),
                                   "pslf": True, "pslf_count": "94", "pslf_as_of": "2026-09-06", "recert": "2027-04-08",
                                   "months": {"2026-10": False}}, headers=H)
        # November already has a payment, so the next is December; 25 more ends in December 2028.
        assert p["last_month"] == "2028-12" and p["to_pay"] == 6250.0
        assert p["forgiven"] == project(24000, 3.4, 250, 25)["balance"] and 18000 < p["forgiven"] < 19500
        assert loan["recert_days"] == (D(2027, 4, 8) - today).days
        assert "Loans" in c.get("/insights").text
        # It counts in net worth as a debt.
        from app.insights import net_worth
        nw = net_worth(db, today)
        assert any(a["id"] == aid and a["value"] == -24000.0 for a in nw["accounts"])
        # Before its first balance the loan still counts, a little higher each month further back.
        from app.insights import account_series
        loan_value = next(a for a in account_series(db, today) if a["id"] == aid)["value"]
        oct_end, jun_end = loan_value("2026-10-15")[0], loan_value("2026-06-15")[0]
        assert loan_value("2026-10-15")[1] == "estimate"
        assert -24250 < oct_end < -24150 and jun_end < oct_end
        expected = 24000.0
        for _ in range(5):
            expected = (expected + 250) / (1 + 3.4 / 100 / 12)
        assert jun_end == -round(expected, 2)
    assert project(10000, 6, 40)["months"] is None          # 40 a month never covers $50 of interest
    assert project(1200, 0, 100)["months"] == 12


def test_any_loan_shows_progress_and_payoff(tmp_path, monkeypatch):
    from app.loans import loans
    db, app = client_with(tmp_path, monkeypatch, [], [])
    H = {"X-Finance": "1"}
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        r = c.post("/api/loans", json={"name": "Car loan", "balance": "20,000.00", "day": "2026-11-15", "rate": "5",
                                       "payment": "400", "original": "28,000", "pslf": False,
                                       "pslf_count": "12", "pslf_as_of": "2026-01-01", "recert": "2027-01-01"}, headers=H)
        assert r.status_code == 200
    (car,) = loans(db, D(2026, 11, 15))
    assert car["paid_pct"] == round(100 * 8000 / 28000, 1) and car["paid"] == 8000.0
    assert 50 < car["payoff"]["months"] < 60 and car["payoff"]["month"] > "2031-01"
    # Without PSLF the count, its date, and the recertification aren't kept.
    assert car["pslf_info"] if car["pslf"] else car["recert"] is None
    with db.connect() as conn:
        assert conn.execute("SELECT pslf_count, pslf_as_of, recert FROM loans").fetchone()[:] == (None, None, None)


def test_insight_tiles_pair_up(tmp_path):
    from app.insights import observations
    cats = {1: {"name": "Groceries"}, 2: {"name": "Shopping"}}

    def month(m, spend, income, saving=0.0, cats_=None):
        return {"m": m, "spending": spend, "income": income, "saving": saving, "debt": 0.0, "cats": cats_ or {}}
    base = [month(f"2026-{m:02d}", 1000, 3000, cats_={"1": 400, "2": 600}) for m in range(4, 10)]
    today = D(2026, 11, 3)                       # before the 5th: no "so far" tile
    # Income left over alone (1 tile) gets a partner: last month's top category.
    flow = base + [month("2026-10", 1000, 3000, cats_={"1": 400, "2": 600}), month("2026-11", 100, 0)]
    tiles = observations(flow, cats, today)
    assert len(tiles) == 2 and tiles[1]["badge"] == "Top category" and tiles[1]["label"] == "Shopping in October"
    # With saving, the partner is the money saved or invested.
    flow[-2]["saving"] = 750.0
    tiles = observations(flow, cats, today)
    assert len(tiles) == 2 and tiles[1]["label"].startswith("Saved or invested")
    # A category well below usual comes before the top category.
    flow[-2]["saving"] = 0.0
    flow[-2]["cats"] = {"1": 100, "2": 600}
    tiles = observations(flow, cats, today)
    assert tiles[1]["label"] == "Groceries in October" and tiles[1]["tone"] == "good"
    # An even count gets nothing extra.
    tiles = observations(flow, cats, D(2026, 11, 10))
    assert len(tiles) % 2 == 0


def test_loan_balance_rolls_forward_with_payments(tmp_path, monkeypatch):
    from app.insights import net_worth
    from app.loans import loans
    rows = [("d8", "chk", "2026-12-08", "LOAN SERVICER", "Loan Servicer", 250.0, 0, "LOAN_PAYMENTS", "X", 1)]
    db, app = client_with(tmp_path, monkeypatch, rows, [("chk", "Checking", "depository", 0, 0)])
    with db.connect() as conn:
        conn.execute("INSERT INTO bills (name, match, amount, cadence, due_day) VALUES ('Loan servicer', 'loan servicer', 250, 'monthly', 8)")
        bill = conn.execute("SELECT id FROM bills").fetchone()[0]
        conn.execute("INSERT INTO accounts (account_id, source, name, type, subtype) VALUES ('manual:l', 'manual', 'Loan', 'liability', 'loan')")
        conn.execute("INSERT INTO manual_balances (account_id, day, balance) VALUES ('manual:l', '2026-11-15', 24000)")
        conn.execute("INSERT INTO loans (account_id, rate, payment, bill_id, pslf) VALUES ('manual:l', 3.65, 250, ?, 0)", (bill,))
    today = D(2026, 12, 10)
    (loan,) = loans(db, today)
    # 23 days of interest to Dec 8 (3.65% of 24,000 is $2.40 a day), the payment, then 2 more days.
    after_payment = 24000 + 2.4 * 23 - 250
    expected = round(after_payment + after_payment * 0.0365 / 365 * 2, 2)
    assert loan["balance"] == expected and loan["estimated"] and loan["paid_since"] == 1
    assert any(a["id"] == "manual:l" and a["value"] == -expected for a in net_worth(db, today)["accounts"])


def test_every_n_years_and_payment_window(tmp_path, monkeypatch):
    from app.bills import bill_status, coverage, make_cadence, cadence_text
    from app.ledger import list_transactions, resolve
    assert make_cadence("years", "2") == "years:2" and make_cadence("months", "12") == "yearly"
    assert make_cadence("months", "3") == "quarterly" and make_cadence("months", "18") == "months:18"
    assert cadence_text("years:2") == "Every 2 years" and cadence_text("months:18") == "Every 18 months"
    # A license renewed every two years, paid in the months before it expires.
    rows = [("r1", "card", "2025-01-20", "STATE LICENSING", "State Licensing", 120.0, 0, "GOVERNMENT_AND_NON_PROFIT", "X", 1)]
    db, app = client_with(tmp_path, monkeypatch, rows, [("card", "Card A", "credit", 0, None)])
    H = {"X-Finance": "1"}
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        bad = c.post("/api/bills", json={"name": "License", "match": "state licensing", "amount": "120", "cadence": "years",
                                         "every": "2", "date_varies": True, "window_months": "30"}, headers=H)
        assert bad.status_code == 400  # window as long as the cycle, and no due date
        ok = c.post("/api/bills", json={"name": "License", "match": "state licensing", "amount": "120", "cadence": "years",
                                        "every": "2", "date_varies": True, "window_months": "3", "next_due": "2027-03-31"}, headers=H)
        assert ok.status_code == 200
    today = D(2026, 11, 16)
    (b,) = bill_status(db, today)
    assert b["cadence"] == "years:2" and b["next_due"] == D(2027, 3, 31) and b["window_opens"] == D(2026, 12, 31)
    assert not b["stale"]  # last paid Jan 2025: a whole cycle plus the window hasn't passed
    sched = {"kind": "semimonthly", "days": [1, 15]}
    assert not coverage(db, sched, today)["due"]                       # window not open yet
    assert [x["name"] for x in coverage(db, sched, D(2027, 1, 10))["due"]] == ["License"]  # open: set aside
    # Paid early, on Feb 5: tagged, and the next due date stays on schedule (Mar 31, 2029).
    seed(db, [("r2", "card", "2027-02-05", "STATE LICENSING", "State Licensing", 120.0, 0, "GOVERNMENT_AND_NON_PROFIT", "X", 1)])
    later = D(2027, 2, 10)
    (b,) = bill_status(db, later)
    assert b["next_due"] == D(2029, 3, 31) and not coverage(db, sched, later)["due"]
    tagged = {t["id"] for t in resolve(db, list_transactions(db, limit=0), later) if t["bill"]}
    assert "r2" in tagged


def test_inbox_tab_badge(tmp_path, monkeypatch):
    rows = [("a", "card", "2026-11-15", "SHOP", "Shop", 5.0, 0, "GENERAL_MERCHANDISE", "X", 0),
            ("b", "card", "2026-11-15", "SHOP", "Shop", 6.0, 0, "GENERAL_MERCHANDISE", "X", 0),
            ("c", "card", "2026-11-14", "SHOP", "Shop", 7.0, 0, "GENERAL_MERCHANDISE", "X", 1)]
    db, app = client_with(tmp_path, monkeypatch, rows, [("card", "Card A", "credit", 0, None)])
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        page = c.get("/bills").text
        assert 'id="tab-badge" aria-label="2 to review">2</span>' in page
        with db.connect() as conn:
            conn.execute("UPDATE transactions SET reviewed=1")
        assert 'aria-label="0 to review" hidden>0</span>' in c.get("/bills").text
        assert 'id="tab-badge-accounts" aria-label="0 to fix" hidden>0</span>' in c.get("/bills").text
        with db.connect() as conn:
            conn.execute("UPDATE plaid_items SET status='login_required'")
        assert 'id="tab-badge-accounts" aria-label="1 to fix">1</span>' in c.get("/bills").text


def test_web_push_encryption_and_messages(tmp_path, monkeypatch):
    import httpx as _httpx
    import json as _json
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from app.push import after_sync_message, b64u, decrypt, encrypt, send, unb64u, vapid_keys
    from app.ledger import list_transactions, resolve
    # A browser's keys; what the app encrypts, the browser can read.
    ua = ec.generate_private_key(ec.SECP256R1())
    p256dh = b64u(ua.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    auth = b64u(b"0123456789abcdef")
    msg = encrypt(b'{"title":"Hi"}', p256dh, auth)
    assert msg[20] == 65 and decrypt(msg, ua, auth) == b'{"title":"Hi"}'

    rows = [("a", "card", "2026-11-15", "CORNER CAFE", "Corner Cafe", 6.5, 0, "FOOD_AND_DRINK", "X", 0)]
    db, app = client_with(tmp_path, monkeypatch, rows, [("card", "Card A", "credit", 0, None)])
    H = {"X-Finance": "1"}
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        key = c.get("/api/push/key").json()["key"]
        assert len(unb64u(key)) == 65 and key == vapid_keys(db)[1]  # stable across calls
        assert c.get("/sw.js").headers["service-worker-allowed"] == "/"
        sub = {"endpoint": "https://push.example.test/abc", "keys": {"p256dh": p256dh, "auth": auth}}
        assert c.post("/api/push/subscribe", json={"subscription": sub}, headers=H).status_code == 200
    # What was waiting when notifications were turned on isn't announced; a new charge is, once.
    seed(db, [("b", "card", "2026-11-16", "BOOK BARN", "Book Barn", 24.0, 0, "GENERAL_MERCHANDISE", "X", 0)])
    unreviewed = lambda: resolve(db, list_transactions(db, reviewed=False, limit=300))
    m = after_sync_message(db, unreviewed(), [], 2)
    assert m["title"] == "Book Barn" and m["body"].startswith("$24.00 · Card A") and m["badge"] == 2
    assert after_sync_message(db, unreviewed(), [], 2) is None
    # Several at once, and a connection that broke.
    seed(db, [("c", "card", "2026-11-16", "SHOP ONE", "Shop One", 3.0, 0, "X", "X", 0),
              ("d", "card", "2026-11-16", "SHOP TWO", "Shop Two", 4.0, 0, "X", "X", 0)])
    m = after_sync_message(db, unreviewed(), [{"item_id": "i1", "institution": "Bank"}], 5)
    assert m["title"] == "Connection needs attention" and "2 new transactions" in m["body"] and m["url"] == "/accounts"
    assert after_sync_message(db, unreviewed(), [{"item_id": "i1", "institution": "Bank"}], 5) is None  # told once

    seen = []

    def handler(request):
        seen.append(request)
        return _httpx.Response(201 if "abc" in str(request.url) else 410)
    with db.connect() as conn:
        conn.execute("INSERT INTO push_subs (endpoint, p256dh, auth) VALUES ('https://push.example.test/gone', ?, ?)", (p256dh, auth))
    client = _httpx.AsyncClient(transport=_httpx.MockTransport(handler))
    assert asyncio.run(send(db, {"title": "T", "badge": 1}, "https://finance.example.test", client)) == 1
    r = seen[0]
    assert r.headers["content-encoding"] == "aes128gcm" and r.headers["authorization"].startswith("vapid t=")
    assert _json.loads(decrypt(r.content, ua, auth)) == {"title": "T", "badge": 1}
    with db.connect() as conn:  # the gone subscription is dropped
        assert [x[0] for x in conn.execute("SELECT endpoint FROM push_subs")] == ["https://push.example.test/abc"]


def test_all_caught_up_clears_other_devices(tmp_path, monkeypatch):
    import app.main as main_mod
    sent = []

    async def fake_send(db, message, subject, client=None, skip=""):
        sent.append((message, skip))
        return 1
    monkeypatch.setattr(main_mod, "push_send", fake_send)
    rows = [("a", "card", "2026-11-15", "SHOP", "Shop", 5.0, 0, "GENERAL_MERCHANDISE", "X", 0)]
    db, app = client_with(tmp_path, monkeypatch, rows, [("card", "Card A", "credit", 0, None)])
    H = {"X-Finance": "1"}
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        db.set_kv("push_last_badge", "1")
        clear = lambda: c.post("/api/push/all-clear", json={"endpoint": "https://push.example.test/me"}, headers=H).json()
        assert clear() == {"sent": 0}                          # setting off
        c.post("/api/push/settings", json={"clear_others": True}, headers=H)
        assert clear() == {"sent": 0}                          # one still waiting
        with db.connect() as conn:
            conn.execute("UPDATE transactions SET reviewed=1")
        assert clear() == {"sent": 1}
        msg, skip = sent[-1]
        assert msg["title"] == "All caught up" and msg["quiet"] and msg["badge"] == 0 and skip.endswith("/me")
        assert clear() == {"sent": 0}                          # only once until something new is pushed


def test_bills_transit_panel_counts_only_what_is_counted(tmp_path, monkeypatch):
    """The In transit panel's total leaves out an item with no match after 7 days, and opens for it."""
    import app.main as main_mod
    db, app = client_with(tmp_path, monkeypatch, [], [("sav", "Savings Co", "depository", 5000.0, 5000.0),
                                                      ("card", "Card A", "credit", 0, None)])
    base = {"date": "2026-11-14", "out": True, "account": "sav", "account_name": "Savings Co", "other": "card",
            "other_name": "Card A", "chosen": False, "pending": False, "other_pending": False, "waiting": True}
    items = [dict(base, id="x1", amount=300.0, late=False), dict(base, id="x2", amount=50.0, late=True)]
    real = main_mod.coverage
    monkeypatch.setattr(main_mod, "coverage", lambda *a, **kw: dict(real(*a, **kw), transit=items))
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        html = c.get("/bills").text
        head = html.split('id="transit"')[1].split("</summary>")[0]
        assert html.count('id="transit" open') == 1
        assert "$300.00" in head and "$350.00" not in head
        assert "1 with no match after 7 days" in head
        items[1]["late"] = False
        html = c.get("/bills").text
        head = html.split('id="transit"')[1].split("</summary>")[0]
        assert 'id="transit" open' not in html and "$350.00" in head


def test_full_backup_download(tmp_path, monkeypatch):
    """A signed-in, same-origin POST gets a working copy of the whole database; nothing else does."""
    import sqlite3 as _sqlite3
    db, app = client_with(tmp_path, monkeypatch, [
        ("b1", "card", "2026-11-01", "CORNER GROCER", "Corner Grocer", 20.0, 0, "FOOD_AND_DRINK", "X", 1)],
        [("card", "Card A", "credit", 0, None)])
    with TestClient(app) as c:
        assert c.post("/api/backup", headers=H).status_code == 401  # not signed in
        c.cookies.set("finance_session", "tok")
        assert c.post("/api/backup").status_code == 403             # no app header
        assert c.get("/api/backup").status_code == 405
        r = c.post("/api/backup", headers=H)
    assert r.status_code == 200 and "finance-backup-" in r.headers["content-disposition"]
    out = tmp_path / "copy.db"
    out.write_bytes(r.content)
    conn = _sqlite3.connect(out)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT name FROM transactions WHERE id='b1'").fetchone()[0] == "CORNER GROCER"
    assert not [f for f in tmp_path.iterdir() if f.name.startswith("tmp") and f.suffix == ".db"]  # temp file removed


def test_apns_payload_token_and_send(tmp_path):
    """Apple push: the message format, a reused signing token, sandbox routing, skip, and dropping
    devices Apple says are gone."""
    import httpx as _httpx
    import jwt as _jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from app.apns import APNs
    key = ec.generate_private_key(ec.SECP256R1())
    p8 = tmp_path / "apns.p8"
    p8.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
    apns = APNs(p8, "KEY1234567", "TEAM123456.com.example.finance")
    assert apns.configured and apns.bundle_id == "com.example.finance"
    assert not APNs(tmp_path / "missing.p8", "KEY1234567", "TEAM123456.com.example.finance").configured
    t1 = apns.auth_token(now=1000)
    assert apns.auth_token(now=1000 + 60) == t1                   # reused, not made per push
    assert apns.auth_token(now=1000 + 41 * 60) != t1               # refreshed before Apple's hour runs out
    assert _jwt.get_unverified_header(t1)["kid"] == "KEY1234567"
    claims = _jwt.decode(t1, key.public_key(), algorithms=["ES256"])
    assert claims["iss"] == "TEAM123456"

    loud = APNs.payload({"title": "Corner Grocer", "body": "$20.00", "badge": 3, "url": "/"})
    assert loud["aps"]["badge"] == 3 and loud["aps"]["sound"] == "default" and loud["url"] == "/"
    quiet = APNs.payload({"title": "All caught up", "body": "", "badge": 0, "quiet": True})
    assert "sound" not in quiet["aps"] and quiet["aps"]["interruption-level"] == "passive"

    db = Database(tmp_path / "f.db")
    with db.connect() as conn:
        conn.executemany("INSERT INTO apns_devices (token, env) VALUES (?, ?)",
                         [("aa" * 32, "production"), ("bb" * 32, "sandbox"), ("cc" * 32, "production"), ("dd" * 32, "production")])
    seen = []

    def handler(request):
        seen.append((request.url.host, request.url.path.rsplit("/", 1)[1], request.headers["apns-topic"],
                     request.headers["apns-collapse-id"]))
        token = request.url.path.rsplit("/", 1)[1]
        if token == "cc" * 32:
            return _httpx.Response(410, json={"reason": "Unregistered"})
        return _httpx.Response(200)

    client = _httpx.AsyncClient(transport=_httpx.MockTransport(handler))
    sent = asyncio.run(apns.send(db, {"title": "x", "body": "y", "badge": 1}, skip="dd" * 32, client=client))
    assert sent == 2
    hosts = {tok: host for host, tok, _, _ in seen}
    assert hosts["aa" * 32] == "api.push.apple.com" and hosts["bb" * 32] == "api.sandbox.push.apple.com"
    assert "dd" * 32 not in hosts                                  # skipped
    assert {topic for _, _, topic, _ in seen} == {"com.example.finance"} and {c for *_, c in seen} == {"finance"}
    with db.connect() as conn:
        left = {r[0] for r in conn.execute("SELECT token FROM apns_devices")}
    assert "cc" * 32 not in left and "aa" * 32 in left              # gone device dropped


def test_apns_register_and_combined_test_push(tmp_path, monkeypatch):
    import app.main as main_mod
    from app.apns import APNs
    db, app = client_with(tmp_path, monkeypatch, [
        ("w1", "card", D.today().isoformat(), "CORNER GROCER", "Corner Grocer", 20.0, 0, "FOOD_AND_DRINK", "X", 0)],
        [("card", "Card A", "credit", 0, None)])
    calls = []

    async def fake_web(db_, message, subject, client=None, skip=""):
        calls.append(("web", message["title"], skip))
        return 0

    async def fake_apns(self, db_, message, skip="", client=None):
        calls.append(("apns", message["title"], skip))
        return 1
    monkeypatch.setattr(main_mod, "push_send", fake_web)
    monkeypatch.setattr(APNs, "send", fake_apns)
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        assert c.post("/api/apns/register", json={"token": "not hex!"}, headers=H).status_code == 400
        r = c.post("/api/apns/register", json={"token": "AB" * 32, "env": "sandbox"}, headers=H)
        assert r.status_code == 200 and r.json()["ready"] is False   # no key on the server yet
        with db.connect() as conn:
            assert conn.execute("SELECT env FROM apns_devices WHERE token=?", ("ab" * 32,)).fetchone()[0] == "sandbox"
            assert conn.execute("SELECT 1 FROM push_told WHERE id='w1'").fetchone()  # waiting isn't news
        r = c.post("/api/push/test", headers=H)
        assert r.status_code == 200 and r.json()["sent"] == 1          # counted from the app device
        assert ("web", "Notifications are on", "") in calls and ("apns", "Notifications are on", "") in calls
        assert c.post("/api/apns/unregister", json={"token": "ab" * 32}, headers=H).status_code == 200
    with db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM apns_devices").fetchone()


def test_server_update_line(tmp_path, monkeypatch):
    """The standalone server's update status shows at the bottom of Accounts; nothing without it."""
    db, app = client_with(tmp_path, monkeypatch, [], [("card", "Card A", "credit", 0, None)])
    with TestClient(app) as c:
        c.cookies.set("finance_session", "tok")
        assert 'id="server-update"' not in c.get("/accounts").text
        assert c.get("/api/server-update").json() == {}
        (tmp_path / "update.json").write_text(json.dumps({
            "state": "updating", "version": "2.2.16", "rev": "aaaaaaa", "at": "2026-11-20T03:00:00+00:00",
            "target_version": "2.2.17", "target_rev": "bbbbbbb"}))
        page = c.get("/accounts").text
        assert 'data-state="updating"' in page and "installing 2.2.17 (bbbbbbb)" in page and "7:00 PM" in page
        (tmp_path / "update.json").write_text(json.dumps({
            "state": "current", "version": "2.2.17", "rev": "bbbbbbb", "at": "2026-11-20T03:05:00+00:00"}))
        assert c.get("/api/server-update").json()["state"] == "current"
        assert "Version 2.2.17 (bbbbbbb), installed" in c.get("/accounts").text


def signed_in_client(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, monkeypatch)
    app = create_app(settings, background_sync=False)
    with Database(tmp_path / "finance.db").connect() as conn:
        conn.execute("INSERT INTO sessions VALUES (?, '2099-01-01T00:00:00+00:00')",
                     (__import__("hashlib").sha256(b"tok").hexdigest(),))
    return app


def test_signin_code_signs_in_another_device_once(tmp_path, monkeypatch):
    app = signed_in_client(tmp_path, monkeypatch)
    with TestClient(app) as phone, TestClient(app) as other:
        assert phone.post("/api/signin-code", headers=H).status_code == 401  # needs a session
        phone.cookies.set("finance_session", "tok")
        code = phone.post("/api/signin-code", headers=H).json()["code"]
        assert len(code) == 9 and code[4] == "-"
        assert other.post("/api/sync", headers=H).status_code == 401
        r = other.post("/api/signin-code/use", json={"code": code.lower().replace("-", " ")}, headers=H)
        assert r.status_code == 200 and "finance_session" in r.cookies
        assert other.get("/accounts").status_code == 200
        assert "Sign in another device" in other.get("/accounts").text
        # Works once.
        other.cookies.clear()
        assert other.post("/api/signin-code/use", json={"code": code}, headers=H).status_code == 401


def test_signin_code_rules():
    from app.auth import SignInCodes

    codes = SignInCodes()
    code = codes.issue(now=1000)
    assert not codes.redeem(code, now=1000 + 301)  # expired after five minutes
    code = codes.issue(now=2000)
    for _ in range(5):
        assert not codes.redeem("AAAA-AAAA" if code != "AAAA-AAAA" else "BBBB-BBBB", now=2001)
    assert not codes.redeem(code, now=2002)  # five wrong tries cancel it
    codes.issue(now=3000)
    second = codes.issue(now=3001)  # a new code replaces the last
    assert codes.redeem(second, now=3002)


def test_signin_code_on_login_page_and_needs_app_header(tmp_path, monkeypatch):
    app = signed_in_client(tmp_path, monkeypatch)
    with Database(tmp_path / "finance.db").connect() as conn:
        conn.execute("INSERT INTO passkeys (credential_id, public_key, sign_count, label) VALUES (x'01', x'02', 0, 'Phone')")
    with TestClient(app) as c:
        assert "Use a sign-in code" in c.get("/login").text
        assert c.post("/api/signin-code/use", json={"code": "ABCD-EFGH"}).status_code == 403


def test_sidebar_summary_for_the_mac_app(tmp_path, monkeypatch):
    app = signed_in_client(tmp_path, monkeypatch)
    with Database(tmp_path / "finance.db").connect() as conn:
        conn.execute("INSERT INTO plaid_items (item_id, access_token, institution) VALUES ('i1','t','Acme')")
        conn.execute("INSERT INTO accounts (account_id, source, item_id, name, type, current_balance) "
                     "VALUES ('c1','plaid','i1','Acme Checking','depository',1234.5)")
        conn.execute("INSERT INTO accounts (account_id, source, item_id, name, type, current_balance, visibility) "
                     "VALUES ('c2','plaid','i1','Old Savings','depository',0,'archived')")
    with TestClient(app) as c:
        assert c.get("/api/sidebar").status_code == 401
        c.cookies.set("finance_session", "tok")
        d = c.get("/api/sidebar").json()
        assert d["left"]["label"].startswith("Left through ") and d["left"]["amount"].startswith(("$", "-$"))
        assert d["synced_at"] is None
        assert d["accounts"] == [{"id": "c1", "name": "Acme Checking", "balance": "$1,234.50", "type": "depository"}]


def test_inbox_json_matches_the_page(tmp_path, monkeypatch):
    import re
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    app = signed_in_client(tmp_path, monkeypatch)
    today = datetime.now(ZoneInfo("America/Los_Angeles")).date()  # the app's "today" is Pacific
    with Database(tmp_path / "finance.db").connect() as conn:
        conn.execute("INSERT INTO plaid_items (item_id, access_token, institution) VALUES ('i1','t','Acme')")
        conn.execute("INSERT INTO accounts (account_id, source, item_id, name, type, current_balance) "
                     "VALUES ('c1','plaid','i1','Acme Checking','depository',500)")
        conn.execute("INSERT INTO accounts (account_id, source, item_id, name, type, current_balance) "
                     "VALUES ('k1','plaid','i1','Acme Card','credit',40)")
        rows = [("t1", "c1", 0, 0, 12.5, "Corner Bakery"), ("t2", "k1", 1, 1, 30.0, "Fuel Stop"),
                ("t3", "c1", 2, 1, -200.0, "Payroll Inc"), ("t4", "k1", 0, 1, 8.25, "Tea House")]
        for tid, acct, days, reviewed, amount, name in rows:
            conn.execute("INSERT INTO transactions (source, id, account_id, date, name, amount, reviewed, pending) "
                         "VALUES ('plaid', ?, ?, ?, ?, ?, ?, ?)",
                         (tid, acct, (today - timedelta(days=days)).isoformat(), name, amount, reviewed,
                          1 if tid == "t4" else 0))
    with TestClient(app) as c:
        assert c.get("/api/inbox").status_code == 401
        c.cookies.set("finance_session", "tok")
        d = c.get("/api/inbox").json()
        page = c.get("/").text
        review_html = re.findall(r'data-id="([^"]+)"', page.split('id="inbox"')[1].split("</ul>")[0])
        all_html = re.findall(r'data-id="([^"]+)"', page.split('id="all"')[1])
        assert [t["id"] for t in d["to_review"]] == review_html == ["t1"]
        assert [t["id"] for g in d["groups"] for t in g["items"]] == all_html == ["t4", "t2", "t3"]
        assert d["groups"][0]["label"] == "Today" and d["next"] is None
        t3 = next(t for g in d["groups"] for t in g["items"] if t["id"] == "t3")
        assert t3["amount"] == "$200.00" and t3["inflow"] is True
        assert d["to_review"][0]["amount"] == "-$12.50" and d["to_review"][0]["date_label"]
        assert d["summary"]["label"].startswith("Left through ") and d["pending_count"] == 1
        assert d["badges"] == {"inbox": 1, "accounts": 0}
        assert {a["id"] for a in d["accounts"]} == {"c1", "k1"}
        assert d["groups"][0]["items"][0]["category"]["color"].startswith("#")
        # Filters and the next-page form.
        assert [t["id"] for g in c.get("/api/inbox?account=k1").json()["groups"] for t in g["items"]] == ["t4", "t2"]
        assert [t["id"] for g in c.get("/api/inbox?pending=1").json()["groups"] for t in g["items"]] == ["t4"]
        nxt = c.get(f"/api/inbox?before={today.isoformat()}:999999").json()
        assert set(nxt) == {"groups", "next"}
        assert c.get("/api/inbox?before=nope").status_code == 400
