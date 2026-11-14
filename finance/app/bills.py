"""Recurring bills: detection from Plaid history, next due dates, and coverage.

Coverage answers one question: after paying every card balance and every bill
due before the next paycheck, is there still cash left?
"""

from __future__ import annotations

import calendar
import re
import statistics
from dataclasses import dataclass
from datetime import date, timedelta

from .db import Database

AMOUNT_TOLERANCE = 0.35  # a charge within 35% of the expected amount counts (utilities vary)
MIN_BILL = 5.00         # ignore tiny recurring charges
STALE_GRACE_DAYS = 10  # a bill this late with no charge has probably ended

CADENCE_MONTHS = {"monthly": 1, "quarterly": 3, "semiannual": 6, "yearly": 12}

# Plaid categories that recur but are not bills: card payments, transfers, income.
SKIP_PRIMARY = {"TRANSFER_IN", "TRANSFER_OUT", "INCOME", "BANK_FEES"}
SKIP_DETAIL = {"LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"}
# Merchants that recur but are everyday spending, not bills.
SKIP_PRIMARY_SPENDING = {"FOOD_AND_DRINK", "TRANSPORTATION", "GENERAL_MERCHANDISE", "TRAVEL"}


def normalize(name: str) -> str:
    """Merchant key: lowercase words without digits or punctuation."""
    words = re.findall(r"[a-z]+", (name or "").lower())
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


def bill_status(db: Database, today: date | None = None) -> list[dict]:
    """Each bill with its last matching charge and next due date."""
    today = today or date.today()
    with db.connect() as conn:
        bills = [dict(r) for r in conn.execute("SELECT * FROM bills ORDER BY name COLLATE NOCASE")]
        charges = [(normalize(r["merchant"] or r["name"]), r["date"], r["amount"]) for r in conn.execute(
            "SELECT merchant, name, date, amount FROM transactions WHERE source='plaid' AND amount > 0 "
            "ORDER BY date DESC")]
    for b in bills:
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
            hit = next((c for c in charges if b["match"] and b["match"] in c[0]
                        and abs(c[2] - b["amount"]) <= AMOUNT_TOLERANCE * max(b["amount"], c[2])), None)
        row = {"date": hit[1], "amount": hit[2]} if hit else None
        b["last_date"] = row["date"] if row else None
        b["last_amount"] = row["amount"] if row else None
        step = CADENCE_MONTHS.get(b["cadence"], 1)
        if b["next_due_override"]:
            due = date.fromisoformat(b["next_due_override"])
            while due < today:
                due = add_months(due, step)
        elif row:
            last = date.fromisoformat(row["date"])
            if b["due_day"]:
                last = date(last.year, last.month, min(b["due_day"], calendar.monthrange(last.year, last.month)[1]))
            due = next_due(last, b["cadence"], today)
        else:
            day = b["due_day"] or 1
            due = date(today.year, today.month, min(day, calendar.monthrange(today.year, today.month)[1]))
            while due < today:
                due = add_months(due, step, day)
        b["next_due"] = due
        # A bill that skipped its last expected charge has probably ended (for example, an old lease).
        b["stale"] = bool(row) and not b["next_due_override"] and (
            add_months(date.fromisoformat(row["date"]), step) + timedelta(days=STALE_GRACE_DAYS) < today)
        b["monthly_equivalent"] = b["amount"] / CADENCE_MONTHS.get(b["cadence"], 1)
    return bills


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
    bills = bill_status(db, today)
    due = [b for b in bills if b["active"] and not b["stale"] and today <= b["next_due"] < payday]
    due_total = sum(b["amount"] for b in due)
    reserve = sum(b["monthly_equivalent"] for b in bills if b["active"] and not b["stale"] and b["cadence"] != "monthly")
    left = cash - cards - pending - due_total
    return {
        "today": today, "payday": payday, "cash": cash, "cash_accounts": cash_accts,
        "cards": cards, "pending": pending, "due": sorted(due, key=lambda b: b["next_due"]),
        "due_total": due_total, "left": left, "ok": left >= 0, "annual_reserve": reserve,
    }
