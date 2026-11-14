"""Recurring bills: detection from Plaid history, next due dates, and coverage.

Coverage answers one question: after paying every card balance and every bill
due before the next paycheck, is there still cash left?
"""

from __future__ import annotations

import bisect
import calendar
import functools
import re
import statistics
from dataclasses import dataclass
from datetime import date, timedelta

from .db import Database

AMOUNT_TOLERANCE = 0.35  # finding a bill's last charge: within 35% of the expected amount
# Tagging a transaction with a bill is stricter: the amount must be close (unless the bill varies)
# and the date must fall near one of the bill's scheduled dates, so a $9 Amazon order on the
# 23rd is not "Prime Video".
TAG_TOLERANCE = 0.10     # within 10% ...
TAG_MIN_DIFF = 1.00      # ... or $1, whichever is more
TAG_DATE_WINDOW = {"biweekly": 3}  # days either side of a scheduled date
TAG_DATE_WINDOW_DEFAULT = 6
MIN_BILL = 5.00         # ignore tiny recurring charges
STALE_GRACE_DAYS = 10  # a bill this late with no charge has probably ended

CADENCE_MONTHS = {"monthly": 1, "quarterly": 3, "semiannual": 6, "yearly": 12}
CADENCES = ("monthly", "biweekly", "quarterly", "semiannual", "yearly")  # biweekly is for paychecks
PER_MONTH = {"monthly": 1, "biweekly": 26 / 12, "quarterly": 1 / 3, "semiannual": 1 / 6, "yearly": 1 / 12}


def advance(d: date, cadence: str, day: int | None = None) -> date:
    """The next occurrence after d."""
    if cadence == "biweekly":
        return d + timedelta(days=14)
    return add_months(d, CADENCE_MONTHS.get(cadence, 1), day)

# Plaid categories that recur but are not bills: card payments, transfers, income.
SKIP_PRIMARY = {"TRANSFER_IN", "TRANSFER_OUT", "INCOME", "BANK_FEES"}
SKIP_DETAIL = {"LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"}
# Merchants that recur but are everyday spending, not bills.
SKIP_PRIMARY_SPENDING = {"FOOD_AND_DRINK", "TRANSPORTATION", "GENERAL_MERCHANDISE", "TRAVEL"}


_WORDS = re.compile(r"[a-z]+")


@functools.lru_cache(maxsize=50000)
def normalize(name: str) -> str:
    """Merchant key: lowercase words without digits or punctuation."""
    words = _WORDS.findall((name or "").lower())
    return " ".join(w for w in words if w not in {"inc", "llc", "com", "www", "the", "payment", "autopay"})[:60]


def add_months(d: date, months: int, day: int | None = None) -> date:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(day or d.day, last))


def next_due(last_seen: date, cadence: str, today: date) -> date:
    step = CADENCE_MONTHS.get(cadence, 1)
    due = add_months(last_seen, step)
    while due < today:
        due = add_months(due, step, last_seen.day)
    return due


@dataclass
class Candidate:
    name: str
    match: str
    amount: float
    cadence: str
    last_seen: date
    count: int
    varies: bool = False

VARIES_SPREAD = 0.08    # recent charges more than 8% apart: treat the amount as varying
VARIES_SAMPLES = 6      # average the last six charges for a varying bill


def _cadence(dates: list[date]) -> str | None:
    if len(dates) < 2:
        return None
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    g = statistics.median(gaps)
    for cadence, lo, hi in (("monthly", 26, 35), ("quarterly", 85, 97), ("semiannual", 170, 195),
                            ("yearly", 350, 380)):
        if lo <= g <= hi:
            need = 3 if cadence == "monthly" else 2
            return cadence if len(dates) >= need else None
    return None


def detect(db: Database, today: date | None = None) -> list[Candidate]:
    """Find recurring outflows in Plaid history that are still active."""
    today = today or date.today()
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT date, name, merchant, amount, category, category_detail FROM transactions "
            "WHERE source='plaid' AND pending=0 AND amount > 0 ORDER BY date")]
    groups: dict[str, list[dict]] = {}
    for t in rows:
        if t["category"] in SKIP_PRIMARY or t["category"] in SKIP_PRIMARY_SPENDING or t["category_detail"] in SKIP_DETAIL:
            continue
        key = normalize(t["merchant"] or t["name"])
        if key:
            groups.setdefault(key, []).append(t)
    out = []
    for key, txns in groups.items():
        # One charge per month at most, so refunds and extra purchases do not hide the pattern.
        by_period: dict[str, dict] = {}
        for t in txns:
            by_period[t["date"][:7]] = t
        series = sorted(by_period.values(), key=lambda t: t["date"])
        dates = [date.fromisoformat(t["date"]) for t in series]
        cadence = _cadence(dates)
        if not cadence:
            continue
        last = dates[-1]
        # Still active: seen within 1.5 cycles.
        if (today - last).days > CADENCE_MONTHS[cadence] * 31 * 1.5:
            continue
        recent = [t["amount"] for t in series[-3:]]
        if statistics.median(recent) < MIN_BILL:
            continue
        name = series[-1]["merchant"] or series[-1]["name"]
        varies = (max(recent) - min(recent)) > VARIES_SPREAD * max(recent)
        amount = statistics.mean([t["amount"] for t in series[-VARIES_SAMPLES:]]) if varies else statistics.median(recent)
        out.append(Candidate(name=name, match=key, amount=round(amount, 2),
                             cadence=cadence, last_seen=last, count=len(series), varies=varies))
    return sorted(out, key=lambda c: (-c.amount))


def save_detected(db: Database, candidates: list[Candidate]) -> int:
    """Add detected bills that are not already on the list (by match key). Returns how many were added."""
    added = 0
    with db.connect() as conn:
        existing = {r["match"] for r in conn.execute("SELECT match FROM bills")}
        dismissed = {r["match"] for r in conn.execute("SELECT match FROM bills_dismissed")}
        for c in candidates:
            if c.match in existing or c.match in dismissed:
                continue
            conn.execute(
                "INSERT INTO bills (name, match, amount, cadence, due_day, source, varies) "
                "VALUES (?, ?, ?, ?, ?, 'detected', ?)",
                (c.name, c.match, c.amount, c.cadence, c.last_seen.day, 1 if c.varies else 0))
            added += 1
    return added


_status_cache: dict = {}


def bill_status(db: Database, today: date | None = None) -> list[dict]:
    """Each bill with its last matching charge and next due date. Reused until transactions,
    bills, or merchant rules change (the database revision) or the day changes."""
    today = today or date.today()
    key = (str(db.path), db.revision(), today)
    if key not in _status_cache:
        _status_cache.clear()
        _status_cache[key] = _bill_status(db, today)
    return [dict(b) for b in _status_cache[key]]


def _bill_status(db: Database, today: date) -> list[dict]:
    with db.connect() as conn:
        bills = [dict(r) for r in conn.execute("SELECT * FROM bills ORDER BY name COLLATE NOCASE")]
        rows = [dict(r) for r in conn.execute(
            "SELECT merchant, name, date, amount, account_id, bill_id FROM transactions "
            "WHERE source='plaid' AND amount != 0 ORDER BY date DESC")]
    for r in rows:
        r["key"] = normalize(r["merchant"] or r["name"])  # once per row, not once per bill
    for b in bills:
        income = b.get("kind") == "income"
        # (merchant key, date, amount as a positive number, account). Income bills look at money in.
        # A transaction marked "not a bill" or set to another bill is skipped; one set to this bill always counts.
        charges = [(b["match"] if r["bill_id"] == b["id"] else r["key"],
                    r["date"], abs(r["amount"]), r["account_id"]) for r in rows
                   if (r["amount"] < 0) == income and (r["bill_id"] is None or r["bill_id"] == b["id"])]
        if b.get("varies"):
            # Varying bills (utilities): any charge from the merchant counts, and the expected
            # amount is the average of the most recent ones.
            hits = [c for c in charges if b["match"] and b["match"] in c[0]][:VARIES_SAMPLES]
            hit = hits[0] if hits else None
            if hits:
                b["amount"] = round(statistics.mean(h[2] for h in hits), 2)
            b["samples"] = len(hits)
        else:
            # Same merchant and a similar amount, so one Amazon Prime bill does not match every Amazon order.
            # A close amount (or a charge assigned to this bill) wins over a merely similar one.
            near = [c for c in charges if b["match"] and b["match"] in c[0]
                    and abs(c[2] - b["amount"]) <= AMOUNT_TOLERANCE * max(b["amount"], c[2])]
            hit = next((c for c in near if amount_matches(b, c[2])), near[0] if near else None)
        row = {"date": hit[1], "amount": hit[2]} if hit else None
        b["last_date"] = row["date"] if row else None
        b["last_amount"] = row["amount"] if row else None
        # The card it is paid with: the one chosen in the editor, else the card of the last charge.
        b["auto_account_id"] = hit[3] if hit else None
        b["account_choice"] = b.get("account_id")
        b["account_id"] = b["account_choice"] or b["auto_account_id"]
        cadence = b["cadence"]
        if b["next_due_override"]:
            due = date.fromisoformat(b["next_due_override"])
            while due < today:
                due = advance(due, cadence)
        elif row and cadence == "biweekly":
            due = advance(date.fromisoformat(row["date"]), cadence)
            while due < today:
                due = advance(due, cadence)
        elif row:
            last = date.fromisoformat(row["date"])
            if b["due_day"]:
                last = date(last.year, last.month, min(b["due_day"], calendar.monthrange(last.year, last.month)[1]))
            due = next_due(last, cadence, today)
        else:
            day = b["due_day"] or 1
            due = date(today.year, today.month, min(day, calendar.monthrange(today.year, today.month)[1]))
            while due < today:
                due = advance(due, cadence, day)
        b["next_due"] = due
        # A bill that skipped its last expected charge has probably ended (for example, an old lease).
        b["stale"] = bool(row) and not b["next_due_override"] and (
            advance(date.fromisoformat(row["date"]), cadence) + timedelta(days=STALE_GRACE_DAYS) < today)
        b["monthly_equivalent"] = b["amount"] * PER_MONTH.get(cadence, 1)
    return bills


def amount_matches(bill: dict, amount: float) -> bool:
    if bill.get("varies"):
        return True
    expected = bill["amount"] or 0
    return abs(amount - expected) <= max(TAG_MIN_DIFF, TAG_TOLERANCE * max(expected, amount))


def on_schedule(bill: dict, d: date) -> bool:
    """Whether d is within a few days of one of the bill's scheduled dates (past or future)."""
    anchor = bill.get("next_due")
    if not anchor:
        return True
    cadence = bill.get("cadence") or "monthly"
    if cadence == "biweekly":
        off = (d - anchor).days % 14
        return min(off, 14 - off) <= TAG_DATE_WINDOW["biweekly"]
    step = CADENCE_MONTHS.get(cadence, 1)
    day = bill.get("due_day") or anchor.day
    k = round(((d.year - anchor.year) * 12 + d.month - anchor.month) / step)
    gap = min(abs((add_months(anchor, (k + j) * step, day) - d).days) for j in (-1, 0, 1))
    return gap <= TAG_DATE_WINDOW.get(cadence, TAG_DATE_WINDOW_DEFAULT)


# Days between charges for each cadence, and how far off a repeat may be.
PERIOD_DAYS = {"biweekly": (14, 2), "monthly": (30.4, 5), "quarterly": (91.3, 9), "semiannual": (182.6, 14),
               "yearly": (365.2, 16)}
REPEAT_SAME = 0.02       # a repeat is the same amount within 2% (or 50 cents)
REPEAT_OLD_PRICE = 0.35  # a repeating amount within 35% counts too (the bill's price before a change)


class Series:
    """A bill's charges, indexed by amount and by date so repeat checks only look at nearby ones
    (a busy merchant like Amazon has hundreds of charges)."""

    def __init__(self, items: list[tuple[date, float]]):
        self.by_amount = sorted((a, d.toordinal()) for d, a in items)
        self.amounts = [a for a, _ in self.by_amount]
        self.days = sorted(d.toordinal() for d, _ in items)

    def near_amount(self, lo: float, hi: float):
        start = bisect.bisect_left(self.amounts, lo)
        end = bisect.bisect_right(self.amounts, hi)
        return self.by_amount[start:end]

    def days_between(self, lo: int, hi: int):
        return self.days[bisect.bisect_left(self.days, lo):bisect.bisect_right(self.days, hi)]


def repeats(series: "Series | list", d: date, amount: float, cadence: str, same_amount: bool = True,
            exact: bool = False, cycles: tuple[int, ...] = (1, 2, 3)) -> bool:
    """Whether another charge in the series is one to three cycles before or after this one
    (with the same amount, unless same_amount is False, as for varying bills)."""
    if not isinstance(series, Series):
        series = Series(series)
    period, slack = PERIOD_DAYS.get(cadence, PERIOD_DAYS["monthly"])
    day = d.toordinal()

    def cycle_away(other: int) -> bool:
        gap = abs(day - other)
        # One to three cycles away (a skipped month, or a yearly repeat of a twice-a-year bill).
        return gap != 0 and any(abs(gap - n * period) <= slack + 3 * (n - 1) for n in cycles)

    if not same_amount:
        reach = int(max(cycles) * period + slack + 3 * (max(cycles) - 1)) + 1
        return any(cycle_away(o) for o in series.days_between(day - reach, day + reach))
    if exact:
        lo, hi = amount - 0.005, amount + 0.005
    else:
        lo = min(amount - 0.50, amount * (1 - REPEAT_SAME)) if amount > 0 else amount - 0.50
        hi = max(amount + 0.50, amount / (1 - REPEAT_SAME))
    for a2, other in series.near_amount(lo, hi):
        if not exact and abs(a2 - amount) > max(0.50, REPEAT_SAME * max(amount, a2)):
            continue
        if cycle_away(other):
            return True
    return False


def bill_matches(bill: dict, key: str, amount: float, d: date, series: "Series | list | None" = None) -> bool:
    """Automatic tagging. The merchant must match, then:
    - fixed bills: the amount is close and the charge either repeats a cycle away or falls on the
      bill's schedule; or the exact amount repeats one cycle away and is within 35% (an older price).
    - varying bills: the charge falls on the schedule or another charge comes about a cycle away.
    One-off purchases from the same merchant (an Amazon order next to Prime Video) do neither."""
    if not bill["match"] or bill["match"] not in key:
        return False
    series = series or []
    cadence = bill.get("cadence") or "monthly"
    if bill.get("varies"):
        return on_schedule(bill, d) or repeats(series, d, amount, cadence, same_amount=False)
    expected = bill["amount"] or 0
    again = repeats(series, d, amount, cadence)
    if amount_matches(bill, amount):
        return again or on_schedule(bill, d)
    # An older price: the exact same amount one cycle away (subscriptions repeat to the cent;
    # orders from a busy merchant only come close).
    return (abs(amount - expected) <= REPEAT_OLD_PRICE * max(expected, amount)
            and repeats(series, d, amount, cadence, exact=True, cycles=(1,)))


_series_cache: dict = {}


def bill_series(db: Database, bills: list[dict]) -> dict[int, list[tuple[date, float]]]:
    """Posted charges (deposits for income) from each bill's merchant, for repeat checks."""
    key = (str(db.path), db.revision(), tuple((b["id"], b["match"], b.get("kind")) for b in bills))
    if key not in _series_cache:
        _series_cache.clear()
        _series_cache[key] = {bid: Series(items) for bid, items in _bill_series(db, bills).items()}
    return _series_cache[key]


def _bill_series(db: Database, bills: list[dict]) -> dict[int, list[tuple[date, float]]]:
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT merchant, name, date, amount, bill_id FROM transactions "
            "WHERE source='plaid' AND pending=0 AND amount != 0 AND transfer_peer IS NULL")]
    keyed = [(normalize(r["merchant"] or r["name"]), date.fromisoformat(r["date"]), r["amount"], r["bill_id"]) for r in rows]
    out = {}
    for b in bills:
        income = b.get("kind") == "income"
        out[b["id"]] = [(d, abs(a)) for k, d, a, bid in keyed
                        if (a < 0) == income and bid in (None, b["id"]) and b["match"] and b["match"] in k]
    return out


# ---------------------------------------------------------------- paydays

def next_payday(schedule: dict, today: date) -> date:
    """schedule: {"kind": "semimonthly", "days": [10, 25]} or {"kind": "biweekly", "anchor": "2026-11-20"}."""
    if schedule.get("kind") == "biweekly" and schedule.get("anchor"):
        d = date.fromisoformat(schedule["anchor"])
        while d <= today:
            d += timedelta(days=14)
        while d - timedelta(days=14) > today:
            d -= timedelta(days=14)
        return d
    days = sorted(schedule.get("days") or [10, 25])
    for month_offset in range(0, 3):
        base = add_months(date(today.year, today.month, 1), month_offset)
        for day in days:
            d = date(base.year, base.month, min(day, calendar.monthrange(base.year, base.month)[1]))
            # Payday moves to the previous Friday on Saturday, the following Monday on Sunday.
            if d.weekday() == 5:
                d -= timedelta(days=1)
            elif d.weekday() == 6:
                d += timedelta(days=1)
            if d > today:
                return d
    return today + timedelta(days=15)


def coverage(db: Database, schedule: dict, today: date | None = None) -> dict:
    today = today or date.today()
    all_bills = bill_status(db, today)
    # Paydays come from the recurring income marked as the paycheck, if there is one.
    all_bills = [b for b in all_bills if not b.get("archived_on")]  # archived bills are history only
    paycheck = next((b for b in all_bills if b.get("kind") == "income" and b.get("is_payday")), None)
    if paycheck:
        payday = paycheck["next_due"]
        while payday <= today:
            payday = advance(payday, paycheck["cadence"])
    else:
        payday = next_payday(schedule, today)
    with db.connect() as conn:
        cash_accts = [dict(r) for r in conn.execute(
            "SELECT name, mask, available_balance, current_balance FROM accounts "
            "WHERE source='plaid' AND type='depository'")]
        card_accts = [dict(r) for r in conn.execute(
            "SELECT account_id, name, mask, current_balance FROM accounts WHERE source='plaid' AND type='credit'")]
        pending = conn.execute(
            "SELECT COALESCE(SUM(t.amount), 0) AS s FROM transactions t JOIN accounts a ON a.account_id = t.account_id "
            "WHERE t.source='plaid' AND t.pending=1 AND a.type='credit'").fetchone()["s"]
    cash = sum((a["available_balance"] if a["available_balance"] is not None else a["current_balance"] or 0)
               for a in cash_accts)
    cards = sum(a["current_balance"] or 0 for a in card_accts)
    bills = [b for b in all_bills if b.get("kind") != "income"]  # coverage counts money going out
    due = [b for b in bills if b["active"] and not b["stale"] and today <= b["next_due"] < payday]
    due_total = sum(b["amount"] for b in due)
    reserve = sum(b["monthly_equivalent"] for b in bills if b["active"] and not b["stale"]
                  and b["cadence"] not in ("monthly", "biweekly"))
    left = cash - cards - pending - due_total
    return {
        "today": today, "payday": payday, "cash": cash, "cash_accounts": cash_accts,
        "cards": cards, "pending": pending, "due": sorted(due, key=lambda b: b["next_due"]),
        "due_total": due_total, "left": left, "ok": left >= 0, "annual_reserve": reserve,
        "paycheck": paycheck,
    }
