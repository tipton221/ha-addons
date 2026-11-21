"""SQLite storage. The database file lives in the add-on's /data folder,
which Home Assistant includes in its backups. It never goes into git."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS passkeys (
    credential_id   BLOB PRIMARY KEY,
    public_key      BLOB NOT NULL,
    sign_count      INTEGER NOT NULL DEFAULT 0,
    label           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash      TEXT PRIMARY KEY,
    expires_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plaid_items (
    item_id         TEXT PRIMARY KEY,
    access_token    TEXT NOT NULL,
    institution_id  TEXT,
    institution     TEXT NOT NULL DEFAULT '',
    cursor          TEXT,
    status          TEXT NOT NULL DEFAULT 'ok',
    error_code      TEXT,
    update_status   TEXT,
    last_sync_at    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One balance per account per day (Pacific), kept from every sync, for net worth trends.
CREATE TABLE IF NOT EXISTS balance_history (
    account_id      TEXT NOT NULL,
    day             TEXT NOT NULL,          -- YYYY-MM-DD, Pacific
    current_balance REAL,
    available_balance REAL,
    PRIMARY KEY (account_id, day)
);

CREATE TABLE IF NOT EXISTS accounts (
    account_id      TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    item_id         TEXT,
    name            TEXT NOT NULL,
    official_name   TEXT,
    mask            TEXT,
    type            TEXT,
    subtype         TEXT,
    current_balance REAL,
    available_balance REAL,
    currency        TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transactions (
    source          TEXT NOT NULL,
    id              TEXT NOT NULL,
    account_id      TEXT NOT NULL,
    date            TEXT NOT NULL,
    authorized_date TEXT,
    name            TEXT NOT NULL,
    merchant        TEXT,
    amount          REAL NOT NULL,
    currency        TEXT,
    pending         INTEGER NOT NULL DEFAULT 0,
    pending_id      TEXT,
    category        TEXT,
    category_detail TEXT,
    raw             TEXT,
    reviewed        INTEGER NOT NULL DEFAULT 0,
    flagged         INTEGER NOT NULL DEFAULT 0,
    first_seen_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source, id)
);

CREATE INDEX IF NOT EXISTS transactions_date ON transactions (date DESC);
CREATE INDEX IF NOT EXISTS transactions_account ON transactions (account_id, date);

CREATE TABLE IF NOT EXISTS bills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    match           TEXT NOT NULL,          -- lowercase text found in the merchant or name
    amount          REAL NOT NULL,          -- expected charge
    cadence         TEXT NOT NULL DEFAULT 'monthly',
    due_day         INTEGER,                -- day of month it usually charges
    next_due_override TEXT,                 -- for yearly bills with a known date
    active          INTEGER NOT NULL DEFAULT 1,
    source          TEXT NOT NULL DEFAULT 'manual',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Detected bills the user removed, so detection does not add them back.
CREATE TABLE IF NOT EXISTS bills_dismissed (
    match           TEXT PRIMARY KEY
);

-- The user's own categories (seeded with a starter list, then fully editable).
CREATE TABLE IF NOT EXISTS categories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    color           TEXT NOT NULL DEFAULT 'gray',
    sort            INTEGER NOT NULL DEFAULT 0
);

-- Per-merchant choices that apply to past and future transactions.
-- merchant_key is the normalized raw merchant (or description) from the bank.
CREATE TABLE IF NOT EXISTS merchant_rules (
    merchant_key    TEXT PRIMARY KEY,
    display_name    TEXT,
    category_id     INTEGER REFERENCES categories(id) ON DELETE SET NULL
);

-- Balances entered by hand: for accounts added in the app (source 'manual') and for YNAB
-- accounts with no Plaid connection. Debts are stored as the positive amount owed.
-- Investment activity from Plaid Investments: buys, sells, dividends, and the cash moved in and
-- out (contributions, withdrawals). amount follows Plaid: positive when cash leaves the account
-- (a buy), negative when cash comes in (a contribution or a sale).
CREATE TABLE IF NOT EXISTS investment_txns (
    id              TEXT PRIMARY KEY,
    account_id      TEXT NOT NULL,
    date            TEXT NOT NULL,
    name            TEXT NOT NULL DEFAULT '',
    amount          REAL NOT NULL,
    type            TEXT,
    subtype         TEXT,
    quantity        REAL,
    price           REAL,
    fees            REAL,
    security_id     TEXT,
    tax_year        INTEGER                 -- IRA contribution counted for this year instead of the date's
);
CREATE INDEX IF NOT EXISTS investment_txns_account ON investment_txns (account_id, date);

-- Which year an IRA contribution counts for (0: not a contribution). id is an investment_txns id,
-- or "ynab:" + a YNAB transaction id for contributions known only from YNAB.
CREATE TABLE IF NOT EXISTS contribution_years (
    id              TEXT PRIMARY KEY,
    year            INTEGER NOT NULL
);

-- Current holdings per investment account (replaced on every sync).
CREATE TABLE IF NOT EXISTS holdings (
    account_id      TEXT NOT NULL,
    security_id     TEXT NOT NULL,
    name            TEXT,
    ticker          TEXT,
    kind            TEXT,
    quantity        REAL,
    value           REAL,
    cost_basis      REAL,
    PRIMARY KEY (account_id, security_id)
);

-- Workplace plan (403(b), 401(k), 457(b)) contributions so far in a year, entered by hand.
CREATE TABLE IF NOT EXISTS plan_contributions (
    year            INTEGER NOT NULL,
    plan            TEXT NOT NULL,          -- '403b' (also 401(k)) or '457b'
    amount          REAL NOT NULL,
    PRIMARY KEY (year, plan)
);

-- Loan details for a debt account (a loan added by hand, or a Plaid loan account).
CREATE TABLE IF NOT EXISTS loans (
    account_id      TEXT PRIMARY KEY,
    rate            REAL,                   -- yearly interest rate, percent
    payment         REAL,                   -- monthly payment
    bill_id         INTEGER,                -- recurring bill whose tagged charges are the payments
    pslf            INTEGER NOT NULL DEFAULT 0,
    pslf_count      INTEGER,                -- qualifying payments shown on studentaid.gov
    pslf_as_of      TEXT,                   -- the date that count was last updated
    recert          TEXT                    -- next income-driven repayment recertification
);

-- A month whose loan payment doesn't count toward PSLF (counts = 0), for example between jobs.
CREATE TABLE IF NOT EXISTS loan_months (
    account_id      TEXT NOT NULL,
    month           TEXT NOT NULL,          -- YYYY-MM
    counts          INTEGER NOT NULL,
    PRIMARY KEY (account_id, month)
);

-- The native app's devices, for Apple push notifications (env: sandbox for Xcode builds,
-- production for TestFlight and App Store builds).
CREATE TABLE IF NOT EXISTS apns_devices (
    token           TEXT PRIMARY KEY,
    env             TEXT NOT NULL DEFAULT 'production',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Web Push subscriptions (one per device with the app installed and notifications allowed).
CREATE TABLE IF NOT EXISTS push_subs (
    endpoint        TEXT PRIMARY KEY,
    p256dh          TEXT NOT NULL,
    auth            TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Transactions already announced by a notification (each is told once).
CREATE TABLE IF NOT EXISTS push_told (
    id              TEXT PRIMARY KEY
);

-- Money in transit: the account chosen for the other side of a transfer ('' = not a transfer
-- between my accounts). Without a row, the account is worked out from past transfers.
CREATE TABLE IF NOT EXISTS transit_choices (
    txn_id          TEXT PRIMARY KEY,
    account_id      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manual_balances (
    account_id      TEXT NOT NULL,
    day             TEXT NOT NULL,          -- YYYY-MM-DD, Pacific
    balance         REAL NOT NULL,
    PRIMARY KEY (account_id, day)
);

-- Pending charges Plaid removed without saying which posted charge replaced them (some banks
-- don't), kept a while so the posted one can still be matched and keep what was done to it.
CREATE TABLE IF NOT EXISTS pending_gone (
    id              TEXT PRIMARY KEY,
    account_id      TEXT NOT NULL,
    date            TEXT NOT NULL,
    name            TEXT NOT NULL,
    merchant        TEXT,
    amount          REAL NOT NULL,
    reviewed        INTEGER NOT NULL,
    note            TEXT,
    category_id     INTEGER,
    name_override   TEXT,
    bill_id         INTEGER,
    tip             REAL,
    gone_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Which YNAB accounts track the same card or bank account as a Plaid account.
-- More than one is allowed, e.g. when a card was converted and YNAB kept the old account.
CREATE TABLE IF NOT EXISTS account_links (
    plaid_account_id TEXT NOT NULL,
    ynab_account_id  TEXT NOT NULL,
    PRIMARY KEY (plaid_account_id, ynab_account_id)
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            self._migrate(conn)
            conn.executescript(SCHEMA)
            self._add_columns(conn)
            self._revision_triggers(conn)

    @staticmethod
    def _revision_triggers(conn: sqlite3.Connection) -> None:
        """A counter that goes up whenever transactions, bills, or merchant rules change, so
        computed results (bill schedules) can be reused until something they depend on changes."""
        conn.execute("CREATE TABLE IF NOT EXISTS revision (id INTEGER PRIMARY KEY CHECK (id = 1), n INTEGER NOT NULL)")
        conn.execute("INSERT OR IGNORE INTO revision VALUES (1, 0)")
        for table in ("transactions", "bills", "merchant_rules"):
            for event in ("INSERT", "UPDATE", "DELETE"):
                conn.execute(f"CREATE TRIGGER IF NOT EXISTS rev_{table}_{event.lower()} AFTER {event} ON {table} "
                             "BEGIN UPDATE revision SET n = n + 1 WHERE id = 1; END")

    def revision(self) -> int:
        with self.connect() as conn:
            return conn.execute("SELECT n FROM revision WHERE id = 1").fetchone()[0]

    @staticmethod
    def _add_columns(conn: sqlite3.Connection) -> None:
        """Columns added after the first release (0.4.0)."""
        wanted = {
            "transactions": [
                ("note", "TEXT"), ("category_id", "INTEGER"), ("pending_amount", "REAL"),
                ("transfer_peer", "TEXT"),   # id of the other side of a transfer or card payment
                ("name_override", "TEXT"),   # one-off name for this transaction only
                ("bill_id", "INTEGER"),      # NULL: matched by rule; 0: not a bill; >0: this bill
                ("tip", "REAL"),             # tip entered by hand (part of the amount); NULL: automatic; 0: no tip
                ("posted_at", "TEXT"),       # when this posted charge replaced its pending one (UTC)
            ],
            "loans": [
                ("original", "REAL"),        # amount borrowed, for the paid-off progress bar
            ],
            "plaid_items": [
                ("products", "TEXT"),        # NULL: transactions; 'investments' for an investments-only connection
                ("investments", "TEXT"),     # NULL: not added; 'on'; 'consent' (needs Plaid consent); 'unsupported'
                ("investments_synced", "TEXT"),  # first full investment history pulled
                ("webhook", "TEXT"),         # the webhook address Plaid was given for this connection
            ],
            "manual_balances": [
                ("flow", "REAL"),            # money put in (+) or taken out (-) since the previous entry
            ],
            "accounts": [
                ("display_name", "TEXT"),
                ("short_name", "TEXT"),      # shown on filter pills
                ("visibility", "TEXT NOT NULL DEFAULT 'everyday'"),  # everyday, less (used), archived
                ("in_net_worth", "INTEGER NOT NULL DEFAULT 1"),  # 0: left out of Insights net worth
                ("track_returns", "INTEGER"),  # 1/0: show (or hide) in Investments; NULL: by account type
                ("contribution_bill", "INTEGER"),  # IRA: recurring bill whose tagged transfers are contributions
            ],
            "categories": [
                ("kind", "TEXT"),            # spending, income, saving, debt, or transfer (see insights.py)
            ],
            "bills": [
                ("color", "TEXT NOT NULL DEFAULT 'purple'"), ("varies", "INTEGER NOT NULL DEFAULT 0"),
                ("account_id", "TEXT"),                     # chosen card; NULL means the card of the last charge
                ("kind", "TEXT NOT NULL DEFAULT 'bill'"),   # 'bill' (money out) or 'income' (money in)
                ("is_payday", "INTEGER NOT NULL DEFAULT 0"),  # the paycheck that sets paydays
                ("archived_on", "TEXT"),                    # set when archived: tags charges up to this date only
                ("window_months", "INTEGER"),               # paid any time up to N months before the due date
            ],
        }
        for table, cols in wanted.items():
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        # Categories without a kind get one from their name (Income, Transfers and payments, Loans).
        conn.execute("UPDATE categories SET kind = CASE name WHEN 'Income' THEN 'income' "
                     "WHEN 'Transfers and payments' THEN 'transfer' WHEN 'Loans' THEN 'debt' "
                     "ELSE 'spending' END WHERE kind IS NULL")

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        # 0.2.x stored one YNAB account per Plaid account (plaid_account_id was the key).
        cols = conn.execute("PRAGMA table_info(account_links)").fetchall()
        pk = [c[1] for c in cols if c[5]]
        if pk == ["plaid_account_id"]:
            conn.execute("ALTER TABLE account_links RENAME TO account_links_old")
            conn.execute("""CREATE TABLE account_links (
                plaid_account_id TEXT NOT NULL, ynab_account_id TEXT NOT NULL,
                PRIMARY KEY (plaid_account_id, ynab_account_id))""")
            conn.execute("INSERT INTO account_links SELECT plaid_account_id, ynab_account_id FROM account_links_old")
            conn.execute("DROP TABLE account_links_old")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get_kv(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_kv(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
