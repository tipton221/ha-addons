"""Loans: balance, rate, payment, payoff projection, and Public Service Loan Forgiveness progress.

A loan is a debt account (added by hand as a loan, or a Plaid loan account) with details saved in
the loans table. Its payments are the transactions tagged with the recurring bill chosen for it.

PSLF counts qualifying months, not payments: the count entered from studentaid.gov (as of its
"last updated" date) plus one for each later month with a payment, unless that month is marked as
not counting (for example a payment made between jobs). Paying twice in a month still adds one.

Projections step month by month: interest on the balance at the yearly rate / 12, minus the
payment. Under PSLF the balance left after the 120th qualifying payment is forgiven.
"""

from __future__ import annotations

from datetime import date, timedelta

from .db import Database
from .insights import MANUAL_DEBTS
from .ledger import list_transactions, resolve

PSLF_TARGET = 120
MAX_MONTHS = 600


def month_add(m: str, n: int) -> str:
    y, mo = int(m[:4]), int(m[5:7]) - 1 + n
    return f"{y + mo // 12:04d}-{mo % 12 + 1:02d}"


def month_label(m: str) -> str:
    return date(int(m[:4]), int(m[5:7]), 1).strftime("%b %Y")


def project(balance: float, rate: float, payment: float, months: int | None = None) -> dict:
    """Step the balance forward. With months: the balance after that many payments. Without: the
    number of payments until it is paid off (None if the payment doesn't cover the interest)."""
    bal, interest, n = balance, 0.0, 0
    limit = months if months is not None else MAX_MONTHS
    while n < limit and bal > 0.005:
        i = bal * rate / 100 / 12
        if months is None and payment <= i:
            return {"months": None, "balance": round(bal, 2), "interest": round(interest, 2)}
        interest += i
        bal = max(0.0, bal + i - payment)
        n += 1
    return {"months": n if bal <= 0.005 else None, "balance": round(bal, 2), "interest": round(interest, 2)}


def loan_accounts(db: Database) -> list[dict]:
    """Debt accounts that can carry loan details, with their saved details."""
    with db.connect() as conn:
        accts = [dict(r) for r in conn.execute(
            "SELECT a.account_id, COALESCE(a.display_name, a.name) AS name, a.source, a.type, a.subtype, "
            "a.current_balance, l.rate, l.payment, l.pslf, l.pslf_count, l.pslf_as_of, l.bill_id, l.recert, l.original, "
            "l.account_id AS detailed FROM accounts a LEFT JOIN loans l ON l.account_id = a.account_id "
            f"WHERE (a.source='manual' AND a.subtype IN ({','.join('?' * len(MANUAL_DEBTS))})) "
            "OR (a.source='plaid' AND a.type='loan') OR l.account_id IS NOT NULL",
            tuple(MANUAL_DEBTS))]
        latest = {r["account_id"]: (r["day"], r["balance"]) for r in conn.execute(
            "SELECT account_id, day, balance FROM manual_balances m WHERE day = "
            "(SELECT MAX(day) FROM manual_balances WHERE account_id = m.account_id)")}
    for a in accts:
        if a["source"] == "plaid":
            a["balance"], a["as_of"] = abs(a["current_balance"] or 0.0), None
        else:
            day, bal = latest.get(a["account_id"], (None, 0.0))
            a["balance"], a["as_of"] = abs(bal), day
    return accts


def payments(db: Database, bill_id: int | None, since: str) -> list[dict]:
    """Payments to the loan (transactions tagged with its bill) on or after a day."""
    if not bill_id:
        return []
    rows = resolve(db, list_transactions(db, where="t.date >= ? AND t.amount > 0", params=(since,), limit=0))
    return sorted((t for t in rows if t["bill"] and t["bill"]["id"] == bill_id and not t["pending"]),
                  key=lambda t: t["date"])


def loans(db: Database, today: date) -> list[dict]:
    out = []
    with db.connect() as conn:
        marks = {}
        for r in conn.execute("SELECT account_id, month, counts FROM loan_months"):
            marks[(r["account_id"], r["month"])] = r["counts"]
    this_month = today.isoformat()[:7]
    for a in loan_accounts(db):
        rate, payment = a.get("rate") or 0.0, a.get("payment") or 0.0
        loan = {"id": a["account_id"], "name": a["name"], "source": a["source"], "balance": round(a["balance"], 2),
                "as_of": a["as_of"], "rate": a.get("rate"), "payment": a.get("payment"), "bill_id": a.get("bill_id"),
                "recert": a.get("recert"), "pslf": bool(a.get("pslf")), "has_details": a.get("rate") is not None,
                "original": a.get("original")}
        if a.get("original") and a["original"] > 0:
            paid = max(0.0, min(1.0, (a["original"] - a["balance"]) / a["original"]))
            loan["paid_pct"] = round(100 * paid, 1)
            loan["paid"] = round(max(0.0, a["original"] - a["balance"]), 2)
        if a.get("recert"):
            loan["recert_days"] = (date.fromisoformat(a["recert"]) - today).days
        if payment > 0:
            loan["payoff"] = project(a["balance"], rate, payment)
            if loan["payoff"]["months"] is not None:
                loan["payoff"]["month"] = month_add(this_month, loan["payoff"]["months"])
        if loan["pslf"]:
            base = a.get("pslf_count") or 0
            as_of = a.get("pslf_as_of") or today.isoformat()
            months: dict[str, list[dict]] = {}
            for t in payments(db, a.get("bill_id"), (date.fromisoformat(as_of) + timedelta(days=1)).isoformat()):
                months.setdefault(t["date"][:7], []).append(t)
            month_rows = []
            for m in sorted(months):
                counts = marks.get((a["account_id"], m), 1)
                month_rows.append({"month": m, "label": month_label(m), "counts": bool(counts),
                                   "amount": round(sum(t["amount"] for t in months[m]), 2), "days": [t["date"] for t in months[m]]})
            added = sum(1 for r in month_rows if r["counts"])
            count = min(PSLF_TARGET, base + added)
            left = PSLF_TARGET - count
            # The next payment is this month's, unless this month already has one.
            first = month_add(this_month, 1) if this_month in months else this_month
            pslf = {"base": base, "as_of": as_of, "added": added, "count": count, "left": left, "months": month_rows,
                    "pct": round(100 * count / PSLF_TARGET, 1)}
            if left and payment > 0:
                after = project(a["balance"], rate, payment, left)
                pslf.update({"last_month": month_add(first, left - 1), "forgiven": after["balance"],
                             "to_pay": round(payment * left, 2), "paid_off_first": after["months"] is not None})
            loan["pslf_info"] = pslf
        out.append(loan)
    return out
