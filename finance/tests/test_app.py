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

from datetime import date as D  # noqa: E402

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
        assert c.post("/api/view/account-filter", json={"account": "bogus"}, headers=H).status_code == 400
        c.post("/api/view/account-filter", json={"account": "bank"}, headers=H)
        all_html = c.get("/").text.split('id="all"')[1]
        assert "Shop Two" in all_html and "Shop One" not in all_html
        c.post("/api/view/account-filter", json={"account": ""}, headers=H)
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
                           "amount": 320.0, "category": None, "category_detail": None, "category_id": None}])
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
    pend = dict(txn("p1", 18.94, pending=True), merchant_name="Taco Spot",
                personal_finance_category={"primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_RESTAURANT"})
    plaid = FakePlaid([page(added=[pend], cursor="c1")])
    asyncio.run(sync_all(db, plaid))
    with db.connect() as conn:
        conn.execute("UPDATE transactions SET reviewed=1, note='Dinner with friends', category_id=7 WHERE id='p1'")
    posted = dict(txn("t1", 21.78, pending_id="p1"), merchant_name="Taco Spot",
                  personal_finance_category=pend["personal_finance_category"])
    big = dict(txn("p2", 10.0, pending=True))
    plaid.pages = [page(added=[posted, big], removed=["p1"], cursor="c2")]
    asyncio.run(sync_all(db, plaid))
    plaid.pages = [page(added=[txn("t2", 40.0, pending_id="p2")], removed=["p2"], cursor="c3")]
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
        # The yearly bill is ~65 days out (beyond 30) and listed first; the next one is last.
        assert "Stream Box" in sched and sched.index("Stream Box") < sched.index("Phone Co")
        c.post("/api/view/account-filter", json={"account": "card"}, headers=H)
        sched = c.get("/").text.split('id="scheduled-wrap"')[1].split("</details>")[0]
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
        c.post("/api/view/account-filter", json={"account": "card"}, headers=H)
        assert "Phone Co" not in c.get("/").text.split('id="scheduled-wrap"')[1].split('id="all"')[0]
        c.post("/api/view/account-filter", json={"account": "card2"}, headers=H)
        assert "Phone Co" in c.get("/").text.split('id="scheduled-wrap"')[1].split('id="all"')[0]
        assert c.post("/api/bills", json={**body, "account_id": "nope"}, headers=H).status_code == 400
        assert c.post("/api/bills", json={**body, "account_id": ""}, headers=H).status_code == 200  # back to automatic
        c.post("/api/view/account-filter", json={"account": "card"}, headers=H)
        assert "Phone Co" in c.get("/").text.split('id="scheduled-wrap"')[1].split('id="all"')[0]


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
        c.post("/api/view/pending", json={"on": True}, headers=H)
        all_list = c.get("/").text.split('id="all"')[1]
        assert "Corner Cafe" in all_list and "Book Barn" not in all_list
        c.post("/api/view/pending", json={"on": False}, headers=H)
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
