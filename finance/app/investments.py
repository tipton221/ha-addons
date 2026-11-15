"""Investments: gain and return for retirement and investment accounts, and contribution limits.

Balances come from the same per-day values as net worth (insights.account_series). Money put in
or taken out ("flows") is not gain, so it is taken out of the change in balance:
- Plaid Investments activity: contributions, deposits, withdrawals, distributions, transfers.
- YNAB: transfers into or out of the account (YNAB's own transfer transactions).
- Accounts entered by hand: the "money put in since the last entry" saved with a balance, spread
  evenly over the weeks since the previous entry (payroll contributions arrive all along).

Return % is the Modified Dietz return: gain divided by the starting balance plus each flow
weighted by the share of the period it was invested. A deposit late in the period barely counts,
so it can't look like growth.

An account whose history starts inside a period counts from its first balance, as if that
balance had been put in that day, and is marked "since" that date.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, timedelta

from .db import Database
from .insights import account_series

PERIODS = (("3m", "3M"), ("6m", "6M"), ("ytd", "YTD"), ("1y", "1Y"), ("all", "All"))
FLOW_SUBTYPES = {"contribution", "deposit", "withdrawal", "distribution", "transfer"}
FLOW_TYPES = {"cash", "transfer"}
CONTRIBUTION_SUBTYPES = {"contribution", "deposit"}
IRA_SUBTYPES = {"ira", "roth"}          # Plaid subtypes under the personal IRA limit
# Employee deferral limits (IRS). 403(b), 401(k), and 457(b) each have the plan limit; a 457(b)
# has its own, separate from a 403(b) or 401(k). Catch-up amounts (age 50+) are not included.
LIMITS = {
    2025: {"ira": 7000, "plan": 23500},
    2026: {"ira": 7500, "plan": 24500},
    2027: {"ira": 7500, "plan": 25500},
}
PLANS = {"403b": "403(b) or 401(k)", "457b": "457(b)"}
IRA_PRIOR_YEAR_UNTIL = (4, 15)          # contributions through April 15 can count for the year before


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m, day)
        except ValueError:
            continue
    raise ValueError(d)


def period_start(key: str, today: date) -> date | None:
    return {"3m": add_months(today, -3), "6m": add_months(today, -6),
            "ytd": date(today.year - 1, 12, 31), "1y": add_months(today, -12)}.get(key)


def tracked(db: Database, today: date) -> list[dict]:
    """Accounts whose gains are tracked: Plaid investment accounts and accounts added by hand as
    retirement or investment, unless switched off; plus any other account switched on."""
    with db.connect() as conn:
        choice = {r["account_id"]: r["track_returns"] for r in conn.execute(
            "SELECT account_id, track_returns FROM accounts WHERE track_returns IS NOT NULL")}
    out = []
    for s in account_series(db, today):
        default = (s["source"] == "plaid" and s["type"] == "investment") or \
                  (s["source"] == "manual" and s["subtype"] in ("retirement", "investment"))
        on = choice.get(s["id"], 1 if default else 0)
        if on and not s["debt"]:
            out.append(s)
    return out


def flows(db: Database, ids: list[str]) -> dict[str, list[tuple[str, float]]]:
    """Money put in (+) or taken out (-) of each account, by day."""
    out: dict[str, list[tuple[str, float]]] = defaultdict(list)
    if not ids:
        return out
    marks = ",".join("?" * len(ids))
    with db.connect() as conn:
        plaid_from: dict[str, str] = {}
        for r in conn.execute(f"SELECT account_id, date, amount, type, subtype FROM investment_txns "
                              f"WHERE account_id IN ({marks}) ORDER BY date", tuple(ids)):
            plaid_from.setdefault(r["account_id"], r["date"])
            if r["type"] in FLOW_TYPES and r["subtype"] in FLOW_SUBTYPES and r["amount"]:
                out[r["account_id"]].append((r["date"], -r["amount"]))
        # Money put in by hand is spread evenly, week by week, over the time since the previous
        # entry: payroll contributions arrive all through it, not on the day the balance was checked.
        prev: dict[str, str] = {}
        for r in conn.execute(f"SELECT account_id, day, flow FROM manual_balances "
                              f"WHERE account_id IN ({marks}) ORDER BY account_id, day", tuple(ids)):
            before = prev.get(r["account_id"])
            prev[r["account_id"]] = r["day"]
            if not r["flow"]:
                continue
            if not before:
                out[r["account_id"]].append((r["day"], r["flow"]))
                continue
            d0, d1 = date.fromisoformat(before), date.fromisoformat(r["day"])
            n = max(1, (d1 - d0).days // 7)
            for k in range(1, n + 1):
                out[r["account_id"]].append(((d0 + timedelta(days=round(k * (d1 - d0).days / n))).isoformat(),
                                             r["flow"] / n))
        # YNAB transfers into the account (its own, or the YNAB accounts linked to a Plaid account),
        # before Plaid's investment activity starts.
        links = defaultdict(list)
        for r in conn.execute("SELECT plaid_account_id, ynab_account_id FROM account_links"):
            links[r["plaid_account_id"]].append(r["ynab_account_id"])
        for aid in ids:
            ynab_ids = [aid] if aid.startswith("ynab:") else links.get(aid, [])
            if not ynab_ids:
                continue
            for r in conn.execute(f"SELECT date, amount, raw FROM transactions WHERE source='ynab' AND account_id IN "
                                  f"({','.join('?' * len(ynab_ids))})", tuple(ynab_ids)):
                if aid in plaid_from and r["date"] >= plaid_from[aid]:
                    continue
                try:
                    raw = json.loads(r["raw"] or "{}")
                except ValueError:
                    raw = {}
                if raw.get("transfer_account_id") and r["amount"]:
                    out[aid].append((r["date"], -r["amount"]))
    for v in out.values():
        v.sort()
    return out


def dietz(v0: float, v1: float, cash: list[tuple[str, float]], start: date, end: date) -> tuple[float, float | None]:
    """Gain and Modified Dietz return over (start, end]."""
    days = max((end - start).days, 1)
    net = sum(f for _, f in cash)
    weighted = sum(f * (end - date.fromisoformat(d)).days / days for d, f in cash)
    gain = v1 - v0 - net
    base = v0 + weighted
    return round(gain, 2), (round(100 * gain / base, 2) if base > 0.005 else None)


def _first_day(value, start: date, end: date) -> date | None:
    """First day in (start, end] with a balance (monthly steps, then daily within the month)."""
    d = start + timedelta(days=1)
    while d <= end:
        if value(d.isoformat())[0] is not None:
            # back up day by day to the first one
            back = d - timedelta(days=1)
            while back > start and value(back.isoformat())[0] is not None:
                back -= timedelta(days=1)
            return back + timedelta(days=1)
        d += timedelta(days=28)
    return end if value(end.isoformat())[0] is not None else None


def returns(db: Database, today: date) -> dict:
    """Per account and in total, for every period: start and end balance, flows, gain, return %."""
    accts = tracked(db, today)
    cash = flows(db, [a["id"] for a in accts])
    with db.connect() as conn:
        holdings = defaultdict(list)
        for r in conn.execute("SELECT * FROM holdings ORDER BY value DESC"):
            holdings[r["account_id"]].append(dict(r))
    end_s = today.isoformat()
    rows = []
    totals = {}
    first_any = None
    for a in accts:
        now = a["value"](end_s)[0]
        if now is None:
            continue
        first = _first_day(a["value"], date(1990, 1, 1), today)
        first_any = min(first_any or first, first)
        a_out = {"id": a["id"], "name": a["name"], "value": round(now, 2), "source": a["source"],
                 "periods": {}, "holdings": holdings.get(a["id"], []), "first": first.isoformat()}
        rows.append((a, a_out))
    for key, _label in PERIODS:
        start = period_start(key, today) if key != "all" else (first_any - timedelta(days=1) if first_any else None)
        if start is None:
            continue
        t_v0 = t_v1 = 0.0
        t_cash: list[tuple[str, float]] = []
        for a, a_out in rows:
            s = start.isoformat()
            v0 = a["value"](s)[0]
            since = None
            in_period = [(d, f) for d, f in cash.get(a["id"], []) if s < d <= end_s]
            if v0 is None:
                first = date.fromisoformat(a_out["first"])
                since = first.isoformat()
                # Its first balance counts as money put in that day (already including any flow then).
                start_val = a["value"](since)[0] or 0.0
                in_period = [(d, f) for d, f in in_period if d > since] + [(since, start_val)]
                v0 = 0.0
            v1 = a_out["value"]
            gain, pct = dietz(v0, v1, in_period, start, today)
            a_out["periods"][key] = {"gain": gain, "pct": pct, "since": since,
                                     "put_in": round(sum(f for d, f in in_period if d != since), 2)}
            t_v0 += v0
            t_v1 += v1
            t_cash += in_period
        gain, pct = dietz(t_v0, t_v1, t_cash, start, today)
        totals[key] = {"gain": gain, "pct": pct, "value": round(t_v1, 2)}
    return {"accounts": [r for _, r in rows], "totals": totals, "periods": PERIODS,
            "value": round(sum(r["value"] for _, r in rows), 2)}


# ---------------------------------------------------------------- contribution limits

def ira_contributions(db: Database) -> list[dict]:
    """Contributions into IRA accounts (Plaid Investments activity), with the year each counts for."""
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT t.id, t.account_id, t.date, t.name, t.amount, t.subtype, t.tax_year, "
            "COALESCE(a.display_name, a.name) AS account FROM investment_txns t JOIN accounts a ON a.account_id = t.account_id "
            f"WHERE a.subtype IN ({','.join('?' * len(IRA_SUBTYPES))}) AND t.type='cash' AND t.amount < 0 "
            f"AND t.subtype IN ({','.join('?' * len(CONTRIBUTION_SUBTYPES))}) ORDER BY t.date DESC",
            tuple(IRA_SUBTYPES) + tuple(CONTRIBUTION_SUBTYPES))]
    for r in rows:
        d = date.fromisoformat(r["date"])
        r["amount"] = -r["amount"]
        r["year"] = r["tax_year"] or d.year
        r["can_switch"] = (d.month, d.day) <= IRA_PRIOR_YEAR_UNTIL
    return rows


def limits(db: Database, today: date) -> dict:
    """This year's progress (and last year's while IRA contributions can still count for it)."""
    years = [today.year]
    if (today.month, today.day) <= IRA_PRIOR_YEAR_UNTIL:
        years.append(today.year - 1)
    contribs = ira_contributions(db)
    with db.connect() as conn:
        plan = {(r["year"], r["plan"]): r["amount"] for r in conn.execute("SELECT * FROM plan_contributions")}
        has_ira = conn.execute(f"SELECT COUNT(*) FROM accounts WHERE source='plaid' AND subtype IN "
                               f"({','.join('?' * len(IRA_SUBTYPES))})", tuple(IRA_SUBTYPES)).fetchone()[0]
        ira_without_activity = conn.execute(
            f"SELECT COUNT(*) FROM accounts a WHERE source='plaid' AND subtype IN ({','.join('?' * len(IRA_SUBTYPES))}) "
            "AND NOT EXISTS (SELECT 1 FROM investment_txns t WHERE t.account_id = a.account_id)",
            tuple(IRA_SUBTYPES)).fetchone()[0]
    out = []
    for y in years:
        lim = LIMITS.get(y) or LIMITS[max(LIMITS)]
        ira = [c for c in contribs if c["year"] == y]
        plans = [{"plan": p, "label": label, "amount": plan.get((y, p)), "limit": lim["plan"]}
                 for p, label in PLANS.items() if p == "403b" or plan.get((y, p)) is not None]
        out.append({"year": y, "ira": round(sum(c["amount"] for c in ira), 2), "ira_limit": lim["ira"],
                    "ira_items": ira, "plans": plans, "known": y in LIMITS})
    return {"years": out, "has_ira": bool(has_ira), "ira_needs_investments": bool(ira_without_activity)}
