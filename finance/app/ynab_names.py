"""Bring YNAB payee names and memos onto matching Plaid transactions.

Old bank descriptions are often cryptic, while the same purchases in YNAB carry the payee you chose.
Each posted Plaid transaction on a linked account is matched to a YNAB transaction on its linked YNAB
account(s) with the same amount within a few days. Then, per bank merchant:

- at least two matches, all with the same YNAB payee: the merchant is renamed (past and future, like a rename you
  make yourself), unless you already renamed it;
- otherwise: each transaction gets its own one-off name.

YNAB memos become notes on transactions that have no note yet.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date

from .db import Database
from .ledger import merchant_key

DATE_TOLERANCE = 4
MIN_FOR_MERCHANT = 2    # one match is not enough to rename a merchant for the future (think PayPal)
SKIP_PAYEES = ("transfer :", "starting balance", "reconciliation balance adjustment", "manual balance adjustment")


def _gap(p: dict, y: dict) -> int:
    yd = date.fromisoformat(y["date"])
    return min(abs((date.fromisoformat(d) - yd).days) for d in (p["date"], p["authorized_date"] or p["date"]))


def match_ynab(conn) -> list[tuple[dict, dict]]:
    """One-to-one (plaid, ynab) pairs: same amount to the cent, closest date within the tolerance."""
    links: dict[str, list[str]] = defaultdict(list)
    for r in conn.execute("SELECT plaid_account_id, ynab_account_id FROM account_links"):
        links[r["plaid_account_id"]].append(r["ynab_account_id"])
    out = []
    for pid, yids in links.items():
        plaid = [dict(r) for r in conn.execute(
            "SELECT id, date, authorized_date, amount, merchant, name, note, name_override FROM transactions "
            "WHERE source='plaid' AND pending=0 AND account_id=? ORDER BY date", (pid,))]
        marks = ",".join("?" * len(yids))
        by_amount: dict[int, list[dict]] = defaultdict(list)
        for r in conn.execute(f"SELECT id, date, amount, name, raw FROM transactions WHERE source='ynab' "
                              f"AND account_id IN ({marks})", yids):
            by_amount[round(r["amount"] * 100)].append(dict(r))
        taken: set[str] = set()
        for p in plaid:
            best = None
            for y in by_amount.get(round(p["amount"] * 100), []):
                if y["id"] in taken:
                    continue
                g = _gap(p, y)
                if g <= DATE_TOLERANCE and (best is None or g < best[0]):
                    best = (g, y)
            if best:
                taken.add(best[1]["id"])
                out.append((p, best[1]))
    return out


def apply_ynab_names(db: Database) -> dict:
    with db.connect() as conn:
        pairs = match_ynab(conn)
        renamed = {r["merchant_key"] for r in conn.execute(
            "SELECT merchant_key FROM merchant_rules WHERE display_name IS NOT NULL")}
        groups: dict[str, list[tuple[dict, str]]] = defaultdict(list)
        notes = 0
        for p, y in pairs:
            memo = (json.loads(y["raw"] or "{}").get("memo") or "").strip()[:500]
            if memo and not (p["note"] or "").strip():
                conn.execute("UPDATE transactions SET note=? WHERE source='plaid' AND id=?", (memo, p["id"]))
                notes += 1
            payee = (y["name"] or "").strip()[:80]
            if payee and not payee.lower().startswith(SKIP_PAYEES):
                groups[merchant_key(p)].append((p, payee))
        merchants = one_off = 0
        for key, items in groups.items():
            if not key or key in renamed:
                continue  # a name you chose wins
            payees = {payee for _, payee in items}
            bank = {(p["merchant"] or p["name"] or "").strip() for p, _ in items}
            if len(payees) == 1 and len(items) >= MIN_FOR_MERCHANT:
                payee = payees.pop()
                if bank != {payee}:
                    conn.execute(
                        "INSERT INTO merchant_rules (merchant_key, display_name) VALUES (?, ?) "
                        "ON CONFLICT(merchant_key) DO UPDATE SET display_name=excluded.display_name", (key, payee))
                    merchants += 1
            else:
                for p, payee in items:
                    if payee != (p["merchant"] or p["name"] or "").strip() and not p["name_override"]:
                        conn.execute("UPDATE transactions SET name_override=? WHERE source='plaid' AND id=?",
                                     (payee, p["id"]))
                        one_off += 1
    result = {"matched": len(pairs), "merchants": merchants, "one_off": one_off, "notes": notes}
    db.set_kv("ynab_names_result", json.dumps(result))
    db.set_kv("ynab_names_applied", "1")
    return result
