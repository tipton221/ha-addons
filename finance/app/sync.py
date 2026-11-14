"""Pull new transactions and balances from Plaid into the database."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone

from .db import Database
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


def carry_from_pending(conn, pending_id: str, posted_id: str, posted_amount: float) -> None:
    """When a pending charge posts, keep what the user did to it (review, note, one-off category)
    and remember the pending amount, so a tip or final total can be shown."""
    row = conn.execute("SELECT amount, reviewed, note, category_id FROM transactions "
                       "WHERE source='plaid' AND id=?", (pending_id,)).fetchone()
    if not row:
        return
    reviewed = row["reviewed"]
    if row["amount"] > 0 and posted_amount - row["amount"] > RECHECK_CHANGE * row["amount"]:
        reviewed = 0  # far more than a tip: worth a second look
    conn.execute(
        "UPDATE transactions SET reviewed = MAX(reviewed, ?), note = COALESCE(note, ?), "
        "category_id = COALESCE(category_id, ?), pending_amount = ? WHERE source='plaid' AND id=?",
        (reviewed, row["note"], row["category_id"],
         row["amount"] if abs(row["amount"] - posted_amount) >= 0.005 else None, posted_id))


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
        for t in added + modified:
            conn.execute(UPSERT, _txn_row(t))
            # When a pending charge posts, Plaid sends the posted one with a
            # link back to the pending one, and removes the pending one.
            if t.get("pending_transaction_id"):
                carry_from_pending(conn, t["pending_transaction_id"], t["transaction_id"], t["amount"])
        for tid in removed:
            conn.execute("DELETE FROM transactions WHERE source='plaid' AND id=?", (tid,))
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


async def sync_all(db: Database, plaid: Plaid) -> dict:
    with db.connect() as conn:
        items = [dict(r) for r in conn.execute("SELECT * FROM plaid_items")]
    results = {}
    for item in items:
        name = item["institution"] or item["item_id"]
        try:
            results[name] = await sync_item(db, plaid, item)
            await refresh_accounts(db, plaid, item)
        except PlaidError as e:
            log.warning("Plaid sync failed for %s: %s", name, e)
            status = "login_required" if e.error_code in LOGIN_ERRORS else "error"
            with db.connect() as conn:
                conn.execute(
                    "UPDATE plaid_items SET status=?, error_code=? WHERE item_id=?",
                    (status, e.error_code, item["item_id"]),
                )
            results[name] = {"error": e.error_code}
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
