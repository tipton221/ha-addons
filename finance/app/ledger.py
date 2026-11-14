"""Display layer for transactions: categories, merchant rules, account names, notes, and bill tags.

What a transaction shows is resolved in this order:
- Name: the merchant rule's display name, else the bank's merchant name, else the description.
- Category: the transaction's own override, else the merchant rule, else Plaid's guess mapped
  into the user's categories.
"""

from __future__ import annotations

from datetime import date, timedelta

from .bills import bill_matches, bill_series, bill_status, normalize
from .db import Database

import re

COLORS = ["blue", "green", "orange", "red", "purple", "pink", "teal", "yellow", "indigo", "gray"]
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def valid_color(value, default: str = "gray") -> str:
    """A named palette color or a custom #rrggbb hex code."""
    value = str(value or "").strip()
    if value in COLORS:
        return value
    if HEX.match(value):
        return value.lower()
    if re.match(r"^[0-9a-fA-F]{6}$", value):
        return "#" + value.lower()
    return default


def css_color(value: str) -> str:
    return f"var(--{value})" if value in COLORS else value


def colors_css(db: Database) -> str:
    """Per-category and per-bill color classes, so templates need no inline styles (blocked by the CSP)."""
    with db.connect() as conn:
        cats = conn.execute("SELECT id, color FROM categories").fetchall()
        bills = conn.execute("SELECT id, color FROM bills").fetchall()
    lines = [f".cat-{r['id']} {{ --c: {css_color(valid_color(r['color']))}; }}" for r in cats]
    lines += [f".bill-{r['id']} {{ --c: {css_color(valid_color(r['color'], 'purple'))}; }}" for r in bills]
    return "\n".join(lines) + "\n"

STARTER_CATEGORIES = [
    ("Rent", "blue"), ("Utilities", "teal"), ("Insurance", "indigo"), ("Loans", "indigo"),
    ("Groceries", "green"), ("Restaurants", "orange"), ("Shopping", "pink"),
    ("Subscriptions", "purple"), ("Transportation", "yellow"), ("Travel", "teal"),
    ("Health", "red"), ("Personal care", "pink"), ("Entertainment", "purple"),
    ("Home", "orange"), ("Gifts", "red"), ("Fees", "gray"), ("Income", "green"),
    ("Transfers and payments", "gray"), ("Other", "gray"),
]

# Plaid detailed category -> starter category. Checked first.
DETAIL_MAP = {
    "FOOD_AND_DRINK_GROCERIES": "Groceries",
    "RENT_AND_UTILITIES_RENT": "Rent",
    "GENERAL_SERVICES_INSURANCE": "Insurance",
    "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT": "Transfers and payments",
    "ENTERTAINMENT_TV_AND_MOVIES": "Subscriptions",
    "ENTERTAINMENT_MUSIC_AND_AUDIO": "Subscriptions",
    "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES": "Shopping",
    "PERSONAL_CARE_GYMS_AND_FITNESS_CENTERS": "Health",
}
# Plaid primary category -> starter category.
PRIMARY_MAP = {
    "FOOD_AND_DRINK": "Restaurants",
    "RENT_AND_UTILITIES": "Utilities",
    "LOAN_PAYMENTS": "Loans",
    "GENERAL_MERCHANDISE": "Shopping",
    "TRANSPORTATION": "Transportation",
    "TRAVEL": "Travel",
    "MEDICAL": "Health",
    "PERSONAL_CARE": "Personal care",
    "ENTERTAINMENT": "Entertainment",
    "HOME_IMPROVEMENT": "Home",
    "BANK_FEES": "Fees",
    "INCOME": "Income",
    "TRANSFER_IN": "Transfers and payments",
    "TRANSFER_OUT": "Transfers and payments",
    "GOVERNMENT_AND_NON_PROFIT": "Other",
    "GENERAL_SERVICES": "Other",
}


def seed_categories(db: Database) -> None:
    with db.connect() as conn:
        if conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]:
            return
        conn.executemany("INSERT INTO categories (name, color, sort) VALUES (?, ?, ?)",
                         [(n, c, i) for i, (n, c) in enumerate(STARTER_CATEGORIES)])


def categories(db: Database) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM categories ORDER BY name COLLATE NOCASE")]


def merchant_key(t: dict) -> str:
    return normalize(t.get("merchant") or t.get("name") or "")


def _default_category(t: dict, by_name: dict[str, int]) -> int | None:
    name = DETAIL_MAP.get(t.get("category_detail") or "") or PRIMARY_MAP.get(t.get("category") or "") or "Other"
    return by_name.get(name) or by_name.get("Other")


def resolve(db: Database, txns: list[dict], today: date | None = None) -> list[dict]:
    """Add display_name, category (dict), account_label, merchant_key, and bill (dict) to each row."""
    if not txns:
        return txns
    with db.connect() as conn:
        cats = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM categories")}
        rules = {r["merchant_key"]: dict(r) for r in conn.execute("SELECT * FROM merchant_rules")}
    # Bills with their schedules (next date, average amount for varying ones) for automatic tags.
    # Ended (stale) bills still tag their old charges.
    all_bills = {b["id"]: b for b in bill_status(db)}
    bills = [b for b in all_bills.values() if b["active"]]
    series = bill_series(db, bills) if bills else {}
    with db.connect() as conn:
        peer_ids = [t["transfer_peer"] for t in txns if t.get("transfer_peer")]
        peers = {r["id"]: r["label"] for r in conn.execute(
            "SELECT t.id, COALESCE(a.display_name, a.name) AS label FROM transactions t "
            "JOIN accounts a ON a.account_id = t.account_id WHERE t.source='plaid' AND t.id IN "
            f"({','.join('?' * len(peer_ids))})", peer_ids)} if peer_ids else {}
    by_name = {c["name"]: cid for cid, c in cats.items()}
    for t in txns:
        key = merchant_key(t)
        rule = rules.get(key) or {}
        t["merchant_key"] = key
        t["display_name"] = (t.get("name_override") or rule.get("display_name") or t.get("merchant")
                             or t.get("name") or "(no name)")
        cid = t.get("category_id") or rule.get("category_id") or _default_category(t, by_name)
        t["category_info"] = cats.get(cid)
        t["account_label"] = t.get("account_display_name") or t.get("account_name") or ""
        t["bill"] = None
        t["transfer"] = None
        if t.get("transfer_peer"):
            peer = peers.get(t["transfer_peer"])
            t["transfer"] = {"account": peer or "another account", "out": t["amount"] > 0}
        if t.get("bill_id"):
            t["bill"] = all_bills.get(t["bill_id"])  # chosen in the editor
        elif t.get("bill_id") is None and not t["transfer"] and t["amount"] != 0:
            amount, income = abs(t["amount"]), t["amount"] < 0
            d = date.fromisoformat(t["date"])
            for b in bills:
                if b.get("archived_on") and t["date"] > b["archived_on"]:
                    continue  # archived: keeps tagging old charges, not new ones
                if (b["kind"] == "income") == income and bill_matches(b, key, amount, d, series.get(b["id"])):
                    t["bill"] = b
                    break
    return txns


def list_transactions(db: Database, *, reviewed: bool | None = None, account: str | None = None,
                      before: tuple[str, str] | None = None, limit: int = 60,
                      where: str = "", params: tuple = ()) -> list[dict]:
    """Plaid transactions, newest first, with account and institution columns."""
    clauses, args = ["t.source = 'plaid'"], []
    if reviewed is not None:
        clauses.append("t.reviewed = ?")
        args.append(1 if reviewed else 0)
    if account:
        clauses.append("t.account_id = ?")
        args.append(account)
    if before:
        clauses.append("(t.date < ? OR (t.date = ? AND t.rowid < ?))")
        args += [before[0], before[0], int(before[1])]
    if where:
        clauses.append(where)
        args += list(params)
    sql = ("SELECT t.rowid AS rid, t.*, a.name AS account_name, a.display_name AS account_display_name, "
           "a.mask, a.type AS account_type, p.institution FROM transactions t "
           "LEFT JOIN accounts a ON a.account_id = t.account_id "
           "LEFT JOIN plaid_items p ON a.item_id = p.item_id "
           f"WHERE {' AND '.join(clauses)} ORDER BY t.date DESC, t.rowid DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def group_by_date(txns: list[dict], today: date) -> list[tuple[str, list[dict]]]:
    groups: list[tuple[str, list[dict]]] = []
    for t in txns:
        d = date.fromisoformat(t["date"])
        if d == today:
            label = "Today"
        elif d == today - timedelta(days=1):
            label = "Yesterday"
        elif d.year == today.year:
            label = d.strftime("%A, %B %-d")
        else:
            label = d.strftime("%B %-d, %Y")
        if groups and groups[-1][0] == label:
            groups[-1][1].append(t)
        else:
            groups.append((label, [t]))
    return groups


def set_merchant_rule(db: Database, key: str, *, display_name: str | None = ..., category_id: int | None = ...) -> None:
    """Create or update the rule for a merchant. Pass a value to change it; leave as ... to keep it."""
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM merchant_rules WHERE merchant_key=?", (key,)).fetchone()
        current = dict(row) if row else {"display_name": None, "category_id": None}
        name = current["display_name"] if display_name is ... else (display_name or None)
        cat = current["category_id"] if category_id is ... else category_id
        conn.execute(
            "INSERT INTO merchant_rules (merchant_key, display_name, category_id) VALUES (?, ?, ?) "
            "ON CONFLICT(merchant_key) DO UPDATE SET display_name=excluded.display_name, "
            "category_id=excluded.category_id", (key, name, cat))
        if category_id is not ...:
            # A merchant-wide choice replaces one-off overrides on that merchant's transactions.
            keys = [r for r in conn.execute(
                "SELECT rowid, merchant, name FROM transactions WHERE source='plaid' AND category_id IS NOT NULL")]
            ids = [r["rowid"] for r in keys if normalize(r["merchant"] or r["name"]) == key]
            conn.executemany("UPDATE transactions SET category_id=NULL WHERE rowid=?", [(i,) for i in ids])


# ---------------------------------------------------------------- transfers

TRANSFER_PRIMARY = {"TRANSFER_IN", "TRANSFER_OUT"}
TRANSFER_CATEGORY = "Transfers and payments"
TRANSFER_WINDOW_DAYS = 5


def _transfer_like(t) -> bool:
    return t["category"] in TRANSFER_PRIMARY or (t["category_detail"] or "").startswith("LOAN_PAYMENTS_CREDIT_CARD")


def pair_transfers(conn) -> int:
    """Link the two sides of a card payment or account to account transfer.

    A pair is one posted charge and one posted credit of the same amount, on two different
    checking, card, or investment accounts (at least one of them checking), within a few days, where Plaid
    calls at least one side a transfer or payment. Returns the number of new pairs.
    """
    # The other side was removed (for example, a pending row replaced when it posted).
    conn.execute("UPDATE transactions SET transfer_peer=NULL WHERE source='plaid' AND transfer_peer IS NOT NULL "
                 "AND transfer_peer NOT IN (SELECT id FROM transactions WHERE source='plaid')")
    rows = [dict(r) for r in conn.execute(
        "SELECT t.id, t.account_id, t.date, t.amount, t.category, t.category_detail, a.type FROM transactions t "
        "JOIN accounts a ON a.account_id = t.account_id WHERE t.source='plaid' AND t.pending=0 "
        "AND t.transfer_peer IS NULL AND a.type IN ('depository', 'credit', 'investment') AND t.amount != 0 "
        "ORDER BY t.date, t.rowid")]
    credits: dict[int, list[dict]] = {}
    for r in rows:
        if r["amount"] < 0:
            credits.setdefault(round(-r["amount"] * 100), []).append(r)
    taken: set[str] = set()
    pairs = []
    for out in (r for r in rows if r["amount"] > 0):
        best, gap = None, None
        for inn in credits.get(round(out["amount"] * 100), []):
            if inn["id"] in taken or inn["account_id"] == out["account_id"]:
                continue
            if "depository" not in (out["type"], inn["type"]):
                continue
            if not (_transfer_like(out) or _transfer_like(inn)):
                continue
            g = abs((date.fromisoformat(out["date"]) - date.fromisoformat(inn["date"])).days)
            if g <= TRANSFER_WINDOW_DAYS and (gap is None or g < gap):
                best, gap = inn, g
        if best:
            taken.add(best["id"])
            pairs.append((out["id"], best["id"]))
    for a, b in pairs:
        conn.execute("UPDATE transactions SET transfer_peer=? WHERE source='plaid' AND id=?", (b, a))
        conn.execute("UPDATE transactions SET transfer_peer=? WHERE source='plaid' AND id=?", (a, b))
    return len(pairs)
