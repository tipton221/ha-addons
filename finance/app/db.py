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

    @staticmethod
    def _add_columns(conn: sqlite3.Connection) -> None:
        """Columns added after the first release (0.4.0)."""
        wanted = {
            "transactions": [("note", "TEXT"), ("category_id", "INTEGER"), ("pending_amount", "REAL")],
            "accounts": [("display_name", "TEXT")],
            "bills": [("color", "TEXT NOT NULL DEFAULT 'purple'"), ("varies", "INTEGER NOT NULL DEFAULT 0")],
        }
        for table, cols in wanted.items():
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

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
