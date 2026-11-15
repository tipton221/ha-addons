"""Money in transit: a card payment or transfer that one account shows and the other doesn't yet.

When a card payment posts on the card before the bank charge posts (or the other way round),
balances briefly count the money twice or not at all. Each such transaction is matched to the
account its past transfers paired with, and until the other side shows up (or 7 days pass) that
account's balance is adjusted by the expected amount, the way it will be once it posts.

Coverage reads cash from available balances (which include pending charges) and adds pending
card charges, so for coverage a pending row already counts. Net worth reads current balances,
which leave pending rows out, so there a pending side is still in transit.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .bills import normalize
from .db import Database
from .ledger import _transfer_like

WAIT_DAYS = 7      # adjust balances this long while the other side hasn't posted
REVIEW_DAYS = 30   # after that, list it as unmatched (no adjustment) this long, unless dismissed
TRIGGER_TYPES = ("depository", "credit")


def pacific_today() -> date:
    return datetime.now(ZoneInfo("America/Los_Angeles")).date()


def _cents(v: float) -> int:
    return round(v * 100)


def in_transit(db: Database, today: date) -> dict:
    """Open in-transit items and the balance adjustments they imply.

    Returns {"items": [...], "nw": {account_id: signed change}, "cash": change, "cards": change}.
    A signed change is in net worth terms: assets positive, debts negative.
    """
    since = (today - timedelta(days=REVIEW_DAYS + WAIT_DAYS)).isoformat()
    with db.connect() as conn:
        accts = {r["account_id"]: dict(r) for r in conn.execute(
            "SELECT account_id, name, display_name, type, in_net_worth FROM accounts WHERE source='plaid'")}
        rows = [dict(r) for r in conn.execute(
            "SELECT id, account_id, date, name, merchant, amount, pending, category, category_detail "
            "FROM transactions WHERE source='plaid' AND transfer_peer IS NULL AND amount != 0 AND date >= ? "
            "ORDER BY date, rowid", (since,))]
        # Where each account's past transfers went: (account, direction, merchant key) -> peer accounts.
        history = [dict(r) for r in conn.execute(
            "SELECT t.account_id, t.name, t.merchant, t.amount, p.account_id AS peer FROM transactions t "
            "JOIN transactions p ON p.source='plaid' AND p.id = t.transfer_peer "
            "WHERE t.source='plaid' AND p.account_id != t.account_id")]
        choices = {r["txn_id"]: r["account_id"] for r in conn.execute("SELECT txn_id, account_id FROM transit_choices")}

    # Only transfers with the same bank description as past paired ones: an account's other
    # transfers (fees, interest, contributions to accounts that never show them) are left alone.
    by_key: dict[tuple, Counter] = defaultdict(Counter)
    for h in history:
        by_key[(h["account_id"], h["amount"] > 0, normalize(h["merchant"] or h["name"]))][h["peer"]] += 1

    def guess(r: dict) -> str | None:
        counts = by_key.get((r["account_id"], r["amount"] > 0, normalize(r["merchant"] or r["name"])))
        return counts.most_common(1)[0][0] if counts else None

    def name(aid: str) -> str:
        a = accts.get(aid) or {}
        return a.get("display_name") or a.get("name") or "another account"

    by_account: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_account[r["account_id"]].append(r)

    used: set[str] = set()
    items, nw = [], defaultdict(float)
    cash = cards = 0.0
    for r in rows:
        if r["id"] in used or not _transfer_like(r):
            continue
        a = accts.get(r["account_id"])
        if not a or a["type"] not in TRIGGER_TYPES:
            continue
        choice = choices.get(r["id"])
        if choice == "":
            continue  # dismissed: not a transfer between my accounts
        other = choice or guess(r)
        if not other or other == r["account_id"] or other not in accts:
            continue  # nothing to go on: transfers to accounts that never show the other side
        day = date.fromisoformat(r["date"])
        match = None
        for m in by_account.get(other, []):
            if m["id"] in used or _cents(m["amount"]) != -_cents(r["amount"]):
                continue
            if abs((date.fromisoformat(m["date"]) - day).days) <= WAIT_DAYS:
                match = m
                break
        if match:
            used.add(match["id"])
        used.add(r["id"])
        if match and not match["pending"] and not r["pending"]:
            continue  # both posted: the next sync pairs them
        age = (today - day).days
        if match is None and age > WAIT_DAYS + REVIEW_DAYS:
            continue
        late = match is None and age > WAIT_DAYS
        item = {"id": r["id"], "date": r["date"], "amount": abs(r["amount"]), "out": r["amount"] > 0,
                "account": r["account_id"], "account_name": name(r["account_id"]),
                "other": other, "other_name": name(other), "chosen": choice is not None,
                "pending": bool(r["pending"]), "other_pending": bool(match and match["pending"]),
                "late": late, "waiting": match is None}
        items.append(item)
        if late:
            continue
        # Net worth (current balances leave pending rows out): add whichever side isn't posted yet.
        if match is None:
            if not r["pending"]:
                nw[other] += r["amount"]
        elif match["pending"] and not r["pending"]:
            nw[other] += r["amount"]
        elif r["pending"] and not match["pending"]:
            nw[r["account_id"]] -= r["amount"]
        # Coverage (pending rows already count): only a side that hasn't shown up at all.
        if match is None:
            otype = accts[other]["type"]
            if otype == "depository":
                cash += r["amount"]
            elif otype == "credit":
                cards -= r["amount"]
    nw = {k: round(v, 2) for k, v in nw.items()
          if abs(v) >= 0.005 and (accts.get(k) or {}).get("in_net_worth") != 0}
    items.sort(key=lambda i: i["date"], reverse=True)
    return {"items": items, "nw": nw, "cash": round(cash, 2), "cards": round(cards, 2)}


def transit_options(db: Database) -> list[dict]:
    """Accounts the other side of a transfer could be in."""
    with db.connect() as conn:
        return [{"id": r["account_id"], "name": r["display_name"] or r["name"]} for r in conn.execute(
            "SELECT account_id, name, display_name FROM accounts WHERE source='plaid' "
            "AND type IN ('depository', 'credit', 'investment') ORDER BY COALESCE(display_name, name)")]
