"""Pull new transactions and balances from Plaid into the database."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")

from .db import Database
from .ledger import normalize, pair_transfers
from .plaid import Plaid, PlaidError

log = logging.getLogger("finance.sync")

INBOX_DAYS = 7

LOGIN_ERRORS = {"ITEM_LOGIN_REQUIRED", "PENDING_EXPIRATION", "PENDING_DISCONNECT"}


def _clean(text: str | None) -> str:
    """Drop Unicode replacement characters some banks send in names, and tidy spaces."""
    return " ".join((text or "").replace("\ufffd", " ").split())


def _txn_row(t: dict) -> tuple:
    pfc = t.get("personal_finance_category") or {}
    return (
        "plaid",
        t["transaction_id"],
        t["account_id"],
        t["date"],
        t.get("authorized_date"),
        t.get("name") or "",
        t.get("merchant_name"),
        t["amount"],
        t.get("iso_currency_code"),
        1 if t.get("pending") else 0,
        t.get("pending_transaction_id"),
        pfc.get("primary"),
        pfc.get("detailed"),
        json.dumps(t),
        # History older than a week (first sync of a new connection) arrives already reviewed,
        # so the inbox only holds recent activity.
        1 if t["date"] < (date.today() - timedelta(days=INBOX_DAYS)).isoformat() else 0,
    )


UPSERT = """
INSERT INTO transactions (source, id, account_id, date, authorized_date, name, merchant,
    amount, currency, pending, pending_id, category, category_detail, raw, reviewed)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(source, id) DO UPDATE SET
    account_id = excluded.account_id, date = excluded.date,
    authorized_date = excluded.authorized_date, name = excluded.name,
    merchant = excluded.merchant, amount = excluded.amount, currency = excluded.currency,
    pending = excluded.pending, pending_id = excluded.pending_id,
    category = excluded.category, category_detail = excluded.category_detail, raw = excluded.raw
"""


RECHECK_CHANGE = 0.30   # a posted charge this much above its pending amount goes back to the inbox
CARRIED = "reviewed, note, category_id, name_override, bill_id, tip"
MATCH_DAYS_BEFORE = 3   # a posted charge can be dated this many days before its pending one
MATCH_DAYS_AFTER = 14   # or this many after
KEEP_GONE_DAYS = 14     # how long a removed pending charge waits to be matched


def _carry(conn, row, posted_id: str, posted_amount: float, pending_id: str) -> None:
    """Give the posted charge what was done to its pending one (review, note, one-off name and
    category, bill, tip) and remember the pending amount, so a tip or final total can be shown."""
    reviewed = row["reviewed"]
    if row["amount"] > 0 and posted_amount - row["amount"] > RECHECK_CHANGE * row["amount"]:
        reviewed = 0  # far more than a tip: worth a second look
    conn.execute(
        "UPDATE transactions SET reviewed = MAX(reviewed, ?), note = COALESCE(note, ?), "
        "category_id = COALESCE(category_id, ?), name_override = COALESCE(name_override, ?), "
        "bill_id = COALESCE(bill_id, ?), tip = COALESCE(tip, ?), pending_amount = ?, "
        "pending_id = COALESCE(pending_id, ?), posted_at = COALESCE(posted_at, ?) WHERE source='plaid' AND id=?",
        (reviewed, row["note"], row["category_id"], row["name_override"], row["bill_id"], row["tip"],
         row["amount"] if abs(row["amount"] - posted_amount) >= 0.005 else None, pending_id,
         datetime.now(timezone.utc).isoformat(timespec="seconds"), posted_id))


def carry_from_pending(conn, pending_id: str, posted_id: str, posted_amount: float) -> bool:
    """When a pending charge posts and Plaid links the two, keep what was done to the pending one."""
    row = conn.execute(f"SELECT amount, {CARRIED} FROM transactions WHERE source='plaid' AND id=?",
                       (pending_id,)).fetchone()
    if not row:
        return False
    _carry(conn, row, posted_id, posted_amount, pending_id)
    return True


def _words(t) -> set[str]:
    return {w for w in normalize(t["merchant"] or t["name"] or "").split() if len(w) > 2}


def match_gone(conn) -> int:
    """Some banks don't tell Plaid which posted charge replaced a pending one, so it's removed with
    no link and what was done to it would be lost. Match each such pending charge to a posted one
    on the same account: the same amount (or more, up to a tip), dated from a few days before to two
    weeks after, not already matched; the closest amount, then a shared word in the name, then the
    nearest date wins. Returns how many were matched."""
    conn.execute(f"DELETE FROM pending_gone WHERE gone_at < datetime('now', '-{KEEP_GONE_DAYS} days')")
    matched = 0
    for g in conn.execute("SELECT * FROM pending_gone ORDER BY date").fetchall():
        d = date.fromisoformat(g["date"])
        lo, hi = sorted((g["amount"], g["amount"] * (1 + RECHECK_CHANGE)))
        candidates = conn.execute(
            "SELECT id, name, merchant, amount, date FROM transactions WHERE source='plaid' AND pending=0 "
            "AND account_id=? AND pending_id IS NULL AND posted_at IS NULL AND date BETWEEN ? AND ? "
            "AND amount BETWEEN ? AND ?",
            (g["account_id"], (d - timedelta(days=MATCH_DAYS_BEFORE)).isoformat(),
             (d + timedelta(days=MATCH_DAYS_AFTER)).isoformat(), lo - 0.005, hi + 0.005)).fetchall()
        if not candidates:
            continue
        words = _words(g)
        best = min(candidates, key=lambda c: (round(abs(c["amount"] - g["amount"]), 2), not (words & _words(c)),
                                              abs((date.fromisoformat(c["date"]) - d).days)))
        _carry(conn, g, best["id"], best["amount"], g["id"])
        conn.execute("DELETE FROM pending_gone WHERE id=?", (g["id"],))
        matched += 1
    return matched


async def sync_item(db: Database, plaid: Plaid, item: dict) -> dict:
    """Run /transactions/sync to completion for one item. Returns counts."""
    start_cursor = item["cursor"]
    for _attempt in range(3):
        cursor = start_cursor
        added: list[dict] = []
        modified: list[dict] = []
        removed: list[str] = []
        status = None
        try:
            while True:
                page = await plaid.transactions_sync(item["access_token"], cursor)
                added += page["added"]
                modified += page["modified"]
                removed += [r["transaction_id"] for r in page["removed"]]
                cursor = page["next_cursor"]
                status = page.get("transactions_update_status")
                if not page["has_more"]:
                    break
        except PlaidError as e:
            if e.error_code == "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION":
                continue  # Plaid says to restart from the original cursor.
            raise
        break
    else:
        raise RuntimeError("transactions sync kept changing during pagination")

    with db.connect() as conn:
        linked = set()
        for t in added + modified:
            conn.execute(UPSERT, _txn_row(t))
            # When a pending charge posts, Plaid sends the posted one with a
            # link back to the pending one, and removes the pending one.
            if t.get("pending_transaction_id") and carry_from_pending(
                    conn, t["pending_transaction_id"], t["transaction_id"], t["amount"]):
                linked.add(t["pending_transaction_id"])
        for tid in removed:
            if tid not in linked:
                # Removed with no posted charge pointing to it: keep it a while to match by hand.
                conn.execute(f"INSERT OR REPLACE INTO pending_gone (id, account_id, date, name, merchant, amount, {CARRIED}) "
                             f"SELECT id, account_id, date, name, merchant, amount, {CARRIED} FROM transactions "
                             "WHERE source='plaid' AND id=? AND pending=1", (tid,))
            conn.execute("DELETE FROM transactions WHERE source='plaid' AND id=?", (tid,))
        match_gone(conn)
        conn.execute(
            "UPDATE plaid_items SET cursor=?, update_status=?, status='ok', error_code=NULL, "
            "last_sync_at=? WHERE item_id=?",
            (cursor, status, datetime.now(timezone.utc).isoformat(), item["item_id"]),
        )
    return {"added": len(added), "modified": len(modified), "removed": len(removed)}


async def refresh_accounts(db: Database, plaid: Plaid, item: dict) -> None:
    accounts = await plaid.accounts(item["access_token"])
    with db.connect() as conn:
        for a in accounts:
            b = a.get("balances") or {}
            conn.execute(
                """INSERT INTO accounts (account_id, source, item_id, name, official_name, mask,
                       type, subtype, current_balance, available_balance, currency, updated_at)
                   VALUES (?, 'plaid', ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(account_id) DO UPDATE SET name=excluded.name,
                       official_name=excluded.official_name, mask=excluded.mask,
                       type=excluded.type, subtype=excluded.subtype,
                       current_balance=excluded.current_balance,
                       available_balance=excluded.available_balance,
                       currency=excluded.currency, updated_at=excluded.updated_at""",
                (a["account_id"], item["item_id"], _clean(a.get("name")), _clean(a.get("official_name")) or None,
                 a.get("mask"), a.get("type"), a.get("subtype"), b.get("current"),
                 b.get("available"), b.get("iso_currency_code")),
            )
            # Today's balance for the history (the last sync of the day wins).
            conn.execute(
                "INSERT INTO balance_history (account_id, day, current_balance, available_balance) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(account_id, day) DO UPDATE SET current_balance=excluded.current_balance, "
                "available_balance=excluded.available_balance",
                (a["account_id"], datetime.now(PACIFIC).date().isoformat(), b.get("current"), b.get("available")))


# Plaid keeps up to 24 months of investment transactions. Every sync asks for all of it: right after
# Investments is added, Plaid may still be loading the history, and a shorter window would never
# pick it up later.
INVESTMENT_HISTORY_DAYS = 730
INVESTMENT_UNAVAILABLE = {"PRODUCTS_NOT_SUPPORTED", "NO_INVESTMENT_ACCOUNTS", "PRODUCT_NOT_ENABLED",
                          "INVALID_PRODUCT", "PRODUCTS_NOT_SUPPORTED_FOR_INSTITUTION"}


async def sync_investments(db: Database, plaid: Plaid, item: dict) -> dict:
    """Holdings and investment transactions for a connection with Investments added."""
    try:
        holdings = await plaid.investments_holdings(item["access_token"])
        today = datetime.now(PACIFIC).date()
        days = INVESTMENT_HISTORY_DAYS
        activity = await plaid.investments_transactions(
            item["access_token"], (today - timedelta(days=days)).isoformat(), today.isoformat())
    except PlaidError as e:
        state = ("consent" if e.error_code == "ADDITIONAL_CONSENT_REQUIRED"
                 else "unsupported" if e.error_code in INVESTMENT_UNAVAILABLE else None)
        if state:
            with db.connect() as conn:
                conn.execute("UPDATE plaid_items SET investments=? WHERE item_id=?", (state, item["item_id"]))
            return {"investments": state}
        raise
    securities = {sec["security_id"]: sec for sec in holdings.get("securities") or []}
    securities.update({sec["security_id"]: sec for sec in activity.get("securities") or []})
    accounts = {a["account_id"] for a in holdings.get("accounts") or []}
    with db.connect() as conn:
        if accounts:
            conn.execute(f"DELETE FROM holdings WHERE account_id IN ({','.join('?' * len(accounts))})", tuple(accounts))
        for h in holdings.get("holdings") or []:
            sec = securities.get(h["security_id"]) or {}
            conn.execute(
                "INSERT OR REPLACE INTO holdings (account_id, security_id, name, ticker, kind, quantity, value, cost_basis) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (h["account_id"], h["security_id"], _clean(sec.get("name")) or None, sec.get("ticker_symbol"),
                 sec.get("type"), h.get("quantity"), h.get("institution_value"), h.get("cost_basis")))
        for t in activity["investment_transactions"]:
            conn.execute(
                "INSERT INTO investment_txns (id, account_id, date, name, amount, type, subtype, quantity, price, fees, "
                "security_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "account_id=excluded.account_id, date=excluded.date, name=excluded.name, amount=excluded.amount, "
                "type=excluded.type, subtype=excluded.subtype, quantity=excluded.quantity, price=excluded.price, "
                "fees=excluded.fees, security_id=excluded.security_id",
                (t["investment_transaction_id"], t["account_id"], t["date"], _clean(t.get("name")), t["amount"],
                 t.get("type"), t.get("subtype"), t.get("quantity"), t.get("price"), t.get("fees"),
                 t.get("security_id")))
        conn.execute("UPDATE plaid_items SET investments='on', investments_synced=COALESCE(investments_synced, ?) "
                     "WHERE item_id=?", (today.isoformat(), item["item_id"]))
    return {"holdings": len(holdings.get("holdings") or []), "activity": len(activity["investment_transactions"])}


async def sync_all(db: Database, plaid: Plaid) -> dict:
    with db.connect() as conn:
        items = [dict(r) for r in conn.execute("SELECT * FROM plaid_items")]
    results = {}
    for item in items:
        name = item["institution"] or item["item_id"]
        try:
            await refresh_accounts(db, plaid, item)  # first, so a new account appears even if transactions fail
            if item.get("products") != "investments":
                results[name] = await sync_item(db, plaid, item)
            else:
                with db.connect() as conn:
                    conn.execute("UPDATE plaid_items SET status='ok', error_code=NULL, last_sync_at=? WHERE item_id=?",
                                 (datetime.now(timezone.utc).isoformat(), item["item_id"]))
                results[name] = {}
            if item.get("investments") == "on" or item.get("products") == "investments":
                try:
                    results[name]["investments"] = await sync_investments(db, plaid, item)
                except PlaidError as e:
                    # A bank whose transactions synced stays connected; only its investments wait.
                    if e.error_code in LOGIN_ERRORS or item.get("products") == "investments":
                        raise
                    log.warning("Plaid investments failed for %s: %s", name, e)
                    results[name]["investments"] = {"error": e.error_code}
        except PlaidError as e:
            log.warning("Plaid sync failed for %s: %s", name, e)
            status = "login_required" if e.error_code in LOGIN_ERRORS else "error"
            with db.connect() as conn:
                conn.execute(
                    "UPDATE plaid_items SET status=?, error_code=? WHERE item_id=?",
                    (status, e.error_code, item["item_id"]),
                )
            results[name] = {"error": e.error_code}
    with db.connect() as conn:
        pair_transfers(conn)
    db.set_kv("last_sync_at", datetime.now(timezone.utc).isoformat())
    return results


# ---------------------------------------------------------------- YNAB

YNAB_PREFIX = "ynab:"


async def sync_ynab(db: Database, ynab) -> dict:
    """Import YNAB accounts and transactions. Uses YNAB's delta requests after the first run.

    Amounts are stored with Plaid's sign convention (positive = money out),
    so the two sources compare directly. YNAB stores milliunits.
    """
    accounts = await ynab.accounts()
    knowledge = db.get_kv("ynab_server_knowledge")
    if db.get_kv("ynab_full_history") != "1":
        # Installs from 0.2.0/0.2.1 imported only the last year; start over once.
        knowledge = None
    txns, new_knowledge = await ynab.transactions(int(knowledge) if knowledge else None)
    changed = deleted = 0
    with db.connect() as conn:
        for a in accounts:
            if a.get("deleted"):
                continue
            conn.execute(
                """INSERT INTO accounts (account_id, source, name, type, subtype,
                       current_balance, currency, updated_at)
                   VALUES (?, 'ynab', ?, ?, ?, ?, 'USD', datetime('now'))
                   ON CONFLICT(account_id) DO UPDATE SET name=excluded.name, type=excluded.type,
                       subtype=excluded.subtype, current_balance=excluded.current_balance,
                       updated_at=excluded.updated_at""",
                (YNAB_PREFIX + a["id"], a["name"], a.get("type"),
                 "closed" if a.get("closed") else "open", a["balance"] / 1000),
            )
        for t in txns:
            if t.get("deleted"):
                conn.execute("DELETE FROM transactions WHERE source='ynab' AND id=?", (t["id"],))
                deleted += 1
                continue
            payee = t.get("payee_name") or ""
            conn.execute(UPSERT, (
                "ynab", t["id"], YNAB_PREFIX + t["account_id"], t["date"], None, payee, payee or None,
                -t["amount"] / 1000, "USD", 0, None, t.get("category_name"), None, json.dumps(t), 1,
            ))
            changed += 1
    db.set_kv("ynab_server_knowledge", str(new_knowledge))
    db.set_kv("ynab_full_history", "1")
    db.set_kv("ynab_last_sync_at", datetime.now(timezone.utc).isoformat())
    return {"changed": changed, "deleted": deleted}
