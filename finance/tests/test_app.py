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
