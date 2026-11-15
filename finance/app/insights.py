"""Insights: net worth over time, monthly spending by category, cash flow, and short observations.

Net worth for each account on a given day comes from the best source available that day:
- Plaid accounts: the balance saved by a sync on or before that day. Before the first saved
  balance, the linked YNAB accounts' balance (YNAB keeps reconciled history back to its start),
  else the balance worked backward from today through Plaid's transactions.
- YNAB accounts with no Plaid account: YNAB's running balance, or a balance entered by hand
  if that is more recent.
- Manual accounts (added in the app, for example a 403(b) Plaid can't reach): the most recent
  balance entered on or before that day.

Spending and cash flow use Plaid transactions only. Each category counts as one kind:
spending, income, saving, debt (loan payments), or transfer (money moving between your own
accounts, left out). A transfer paired with an investment account counts as saving, and a
deposit tagged as recurring income (a paycheck) counts as income whatever its category.
"""

from __future__ import annotations

import bisect
import calendar
from collections import defaultdict
from datetime import date

from .db import Database

KINDS = ("spending", "income", "saving", "debt", "transfer")
KIND_LABELS = {"spending": "Spending", "income": "Income", "saving": "Saving or investing",
               "debt": "Debt payment", "transfer": "Transfer"}
# Short names for the pill on each category in Accounts › Categories.
KIND_PILLS = {"spending": "Spending", "income": "Income", "saving": "Saving", "debt": "Debt", "transfer": "Transfer"}
DEFAULT_KINDS = {"Income": "income", "Transfers and payments": "transfer", "Loans": "debt"}

# Account types that are owed money (shown as debts). Plaid, YNAB, and manual names.
LIABILITY_TYPES = {"credit", "loan", "creditCard", "lineOfCredit", "otherLiability", "mortgage",
                   "autoLoan", "studentLoan", "personalLoan", "medicalDebt", "otherDebt", "liability"}
YNAB_PREFIX = "ynab:"
MANUAL_KINDS = {"retirement": "Retirement", "investment": "Investment", "cash": "Cash",
                "property": "Property", "other_asset": "Other asset", "loan": "Loan",
                "other_debt": "Other debt"}
MANUAL_DEBTS = {"loan", "other_debt"}


def month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def months_between(first: date, last: date) -> list[str]:
    out, y, m = [], first.year, first.month
    while (y, m) <= (last.year, last.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def is_liability(acct: dict) -> bool:
    if acct["source"] == "manual":
        return acct.get("subtype") in MANUAL_DEBTS
    return (acct.get("type") or "") in LIABILITY_TYPES


class Series:
    """A step function of (day, value) points: the value on a day is the last point on or before it."""

    def __init__(self, points: list[tuple[str, float]]):
        points = sorted(points)
        self.days = [p[0] for p in points]
        self.values = [p[1] for p in points]

    def at(self, day: str) -> tuple[str, float] | None:
        i = bisect.bisect_right(self.days, day) - 1
        return (self.days[i], self.values[i]) if i >= 0 else None

    @property
    def first(self) -> str | None:
        return self.days[0] if self.days else None


def _running(points: list[tuple[str, float]]) -> Series:
    """Daily sums turned into a running balance."""
    total, out = 0.0, []
    for day, delta in sorted(points):
        total += delta
        out.append((day, round(total, 2)))
    return Series(out)


def account_series(db: Database, today: date) -> list[dict]:
    """Every account that counts in net worth, with a function giving its signed value on a day
    (assets positive, debts negative) and where that value comes from."""
    with db.connect() as conn:
        accts = [dict(r) for r in conn.execute("SELECT * FROM accounts")]
        links = defaultdict(list)
        for r in conn.execute("SELECT plaid_account_id, ynab_account_id FROM account_links"):
            links[r["plaid_account_id"]].append(r["ynab_account_id"])
        # YNAB: one signed change per account per day (stored amounts are positive for money out).
        ynab_days = defaultdict(list)
        for r in conn.execute("SELECT account_id, date, -SUM(amount) AS v FROM transactions "
                              "WHERE source='ynab' GROUP BY account_id, date"):
            ynab_days[r["account_id"]].append((r["date"], r["v"]))
        plaid_days = defaultdict(list)
        for r in conn.execute("SELECT account_id, date, SUM(amount) AS v FROM transactions "
                              "WHERE source='plaid' AND pending=0 GROUP BY account_id, date"):
            plaid_days[r["account_id"]].append((r["date"], r["v"]))
        history = defaultdict(list)
        for r in conn.execute("SELECT account_id, day, current_balance FROM balance_history "
                              "WHERE current_balance IS NOT NULL"):
            history[r["account_id"]].append((r["day"], r["current_balance"]))
        manual = defaultdict(list)
        for r in conn.execute("SELECT account_id, day, balance FROM manual_balances"):
            manual[r["account_id"]].append((r["day"], r["balance"]))
        loan_terms = {r["account_id"]: (r["rate"] or 0.0, r["payment"] or 0.0)
                      for r in conn.execute("SELECT account_id, rate, payment FROM loans")}

    ynab = {a["account_id"]: _running(ynab_days[a["account_id"]]) for a in accts if a["source"] == "ynab"}
    pays = None   # loan payments since each manual loan's latest balance, loaded when needed
    linked = {y for ys in links.values() for y in ys}
    today_s = today.isoformat()
    out = []
    for a in accts:
        aid, debt = a["account_id"], is_liability(a)
        label = a.get("display_name") or a["name"]
        if a["source"] == "plaid":
            sign = -1 if debt else 1
            hist = Series([(d, sign * v) for d, v in history[aid]])
            yl = [ynab[y] for y in links.get(aid, []) if y in ynab]
            # Worked backward from today's balance: each later transaction is undone.
            now = sign * (a["current_balance"] or 0)
            later = sorted(plaid_days[aid])
            back_days = [d for d, _ in later]
            suffix, acc = [], 0.0
            for _, v in reversed(later):
                acc += v
                suffix.append(acc)
            suffix.reverse()  # suffix[i] = sum of transactions on back_days[i:] (money out positive)
            first_txn = back_days[0] if back_days else None

            def value(day, hist=hist, yl=yl, now=now, back_days=back_days, suffix=suffix, first_txn=first_txn):
                h = hist.at(day)
                if h:
                    return h[1], "plaid"
                if hist.first is None and day >= today_s:
                    return now, "plaid"
                ys = [s.at(day) for s in yl]
                if any(ys):
                    return round(sum(v for _, v in filter(None, ys)), 2), "ynab"
                if first_txn and day >= first_txn and a["type"] in ("depository", "credit"):
                    i = bisect.bisect_right(back_days, day)
                    return round(now + (suffix[i] if i < len(suffix) else 0.0), 2), "estimate"
                return None, None
        elif a["source"] == "ynab":
            if aid in linked:
                continue  # counted through its Plaid account
            ys, ms = ynab[aid], Series(manual[aid])

            def value(day, ys=ys, ms=ms):
                y, m = ys.at(day), ms.at(day)
                if m and (not y or m[0] >= y[0]):
                    return m[1], "manual"
                return (y[1], "ynab") if y else (None, None)
        elif a["source"] == "manual":
            ms, sign = Series(manual[aid]), (-1 if debt else 1)
            if debt and pays is None:
                from .loans import loan_payments
                pays = loan_payments(db)
            after = pays.get(aid) if debt else None
            # A debt entered by hand existed before its first balance: earlier months carry it back,
            # undoing the loan's payments (less interest) month by month when its terms are known.
            back = loan_terms.get(aid, (0.0, 0.0)) if debt and ms.first else None

            def value(day, ms=ms, sign=sign, back=back, after=after, terms=loan_terms.get(aid, (0.0, 0.0))):
                m = ms.at(day)
                if m and after and m[0] == ms.days[-1] and day > m[0]:
                    # After the latest balance: its payments since come off, interest goes on.
                    from .loans import roll_forward
                    return sign * roll_forward(abs(m[1]), m[0], terms[0], after, day), "estimate"
                if m:
                    return sign * abs(m[1]), "manual"
                if back is None:
                    return None, None
                first_day, bal = ms.days[0], abs(ms.values[0])
                months = round((date.fromisoformat(first_day) - date.fromisoformat(day)).days / 30.44)
                rate, payment = back
                for _ in range(months):
                    if payment <= 0:
                        break
                    bal = (bal + payment) / (1 + rate / 100 / 12)
                return sign * round(bal, 2), "estimate"
        else:
            continue
        out.append({"id": aid, "name": label, "source": a["source"], "type": a.get("type"),
                    "subtype": a.get("subtype"), "debt": debt, "value": value,
                    "included": a.get("in_net_worth") != 0,
                    "closed": a.get("subtype") == "closed" or a.get("visibility") == "archived"})
    return out


def net_worth(db: Database, today: date) -> dict:
    """Month-end net worth from the first YNAB or Plaid record to today, plus today's accounts."""
    series = account_series(db, today)
    with db.connect() as conn:
        first = conn.execute("SELECT MIN(date) FROM transactions").fetchone()[0]
        first_manual = conn.execute("SELECT MIN(day) FROM manual_balances").fetchone()[0]
    starts = [d for d in (first, first_manual) if d]
    if not starts or not series:
        return {"points": [], "accounts": [], "excluded": [], "now": None}
    start = date.fromisoformat(min(starts))
    from .transit import in_transit
    transit = in_transit(db, today)["nw"]  # today's balances only: a payment one side shows yet
    today_s = today.isoformat()
    points = []
    for m in months_between(start, today):
        y, mo = int(m[:4]), int(m[5:])
        day = min(month_end(y, mo), today).isoformat()
        assets = debts = 0.0
        for s in series:
            if not s["included"]:
                continue
            v, _ = s["value"](day)
            if v is None:
                continue
            if day == today_s:
                v += transit.get(s["id"], 0.0)
            if s["debt"]:
                debts += v
            else:
                assets += v
        points.append({"m": m, "day": day, "assets": round(assets, 2), "debts": round(debts, 2),
                       "net": round(assets + debts, 2)})
    accounts, excluded = [], []
    for s in series:
        v, src = s["value"](today_s)
        shift = transit.get(s["id"], 0.0) if s["included"] and v is not None else 0.0
        if shift:
            v = round(v + shift, 2)
        if not s["included"]:
            excluded.append({"id": s["id"], "name": s["name"], "value": v, "debt": s["debt"]})
            continue
        if v is None or (s["closed"] and abs(v) < 0.005):
            continue
        accounts.append({"id": s["id"], "name": s["name"], "value": v, "source": src, "debt": s["debt"],
                         "editable": s["source"] in ("manual", "ynab"), "manual": s["source"] == "manual",
                         "subtype": s["subtype"], "transit": shift,
                         "kind": MANUAL_KINDS.get(s["subtype"] or "") if s["source"] == "manual" else None})
    accounts.sort(key=lambda x: (x["debt"], -abs(x["value"])))
    excluded.sort(key=lambda x: x["name"].lower())
    return {"points": points, "accounts": accounts, "excluded": excluded, "now": points[-1] if points else None}


# ---------------------------------------------------------------- spending and cash flow

def category_kinds(db: Database) -> dict[int, str]:
    with db.connect() as conn:
        return {r["id"]: (r["kind"] if r["kind"] in KINDS else "spending")
                for r in conn.execute("SELECT id, kind FROM categories")}


INVESTMENT_TRANSFERS = {"TRANSFER_OUT_INVESTMENT_AND_RETIREMENT_FUNDS", "TRANSFER_IN_INVESTMENT_AND_RETIREMENT_FUNDS"}


def classify(t: dict, kinds: dict[int, str], acct_types: dict[str, str]) -> tuple[str, float] | None:
    """(bucket, amount) for one Plaid transaction, with money out positive for every bucket but
    income, where money in is positive. None when it doesn't count (transfers, investment accounts)."""
    if acct_types.get(t["account_id"]) in ("investment", "loan"):
        return None
    cat = t.get("category_info") or {}
    kind = kinds.get(cat.get("id"), "spending")
    amount = t["amount"]
    if t.get("transfer") or kind == "transfer":
        peer_type = acct_types.get(t.get("transfer_peer_account") or "")
        if t.get("transfer") and peer_type == "investment":
            return "saving", amount
        # Plaid marks money sent to (or taken from) a retirement or investment account, even when that
        # account isn't connected here: a Roth IRA contribution, for example.
        if not t.get("transfer") and (t.get("category_detail") or "") in INVESTMENT_TRANSFERS:
            return "saving", amount
        return None
    if kind == "income" or ((t.get("bill") or {}).get("kind") == "income" and amount < 0):
        return "income", -amount  # an Income category, or a deposit tagged as recurring income
    if kind in ("saving", "debt"):
        return kind, amount
    return "spending", amount


def classified(db: Database, rows: list[dict]):
    """Each counted transaction with its (bucket, amount)."""
    kinds = category_kinds(db)
    with db.connect() as conn:
        acct_types = {r["account_id"]: r["type"] for r in conn.execute("SELECT account_id, type FROM accounts")}
        peers = {r["id"]: r["account_id"] for r in conn.execute(
            "SELECT id, account_id FROM transactions WHERE source='plaid' AND id IN "
            "(SELECT transfer_peer FROM transactions WHERE transfer_peer IS NOT NULL)")}
    for t in rows:
        t["transfer_peer_account"] = peers.get(t.get("transfer_peer") or "")
        c = classify(t, kinds, acct_types)
        if c:
            yield t, c[0], c[1]


def spending_rows(db: Database, rows: list[dict]) -> list[dict]:
    """The transactions counted as spending, largest first (refunds last)."""
    return sorted((t for t, bucket, _ in classified(db, rows) if bucket == "spending"), key=lambda t: -t["amount"])


def monthly(db: Database, rows: list[dict], today: date) -> dict:
    """Per month: income, spending (with categories), saving, debt payments, and what was kept."""
    months: dict[str, dict] = {}
    for t, bucket, amount in classified(db, rows):
        m = months.setdefault(t["date"][:7], {"income": 0.0, "spending": 0.0, "saving": 0.0, "debt": 0.0,
                                              "cats": defaultdict(float), "count": 0, "biggest": []})
        m[bucket] += amount
        if bucket == "spending":
            cid = (t.get("category_info") or {}).get("id")
            m["cats"][cid] += amount
            m["count"] += 1
            if amount > 0:
                m["biggest"].append((amount, t))
    if not months:
        return {"months": []}
    keys = months_between(date.fromisoformat(min(months) + "-01"), today)
    out = []
    for k in keys:
        m = months.get(k) or {"income": 0.0, "spending": 0.0, "saving": 0.0, "debt": 0.0, "cats": {},
                              "count": 0, "biggest": []}
        kept = m["income"] - m["spending"] - m["debt"]
        big = sorted(m["biggest"], key=lambda x: -x[0])[:3]
        out.append({
            "m": k, "income": round(m["income"], 2), "spending": round(m["spending"], 2),
            "saving": round(m["saving"], 2), "debt": round(m["debt"], 2), "kept": round(kept, 2),
            "rate": round(100 * kept / m["income"]) if m["income"] > 0 else None,
            "cats": {str(c): round(v, 2) for c, v in m["cats"].items() if abs(v) >= 0.005},
            "biggest": [{"name": t["display_name"], "amount": round(a, 2), "date": t["date"],
                         "cat": (t.get("category_info") or {}).get("id")} for a, t in big],
        })
    return {"months": out}


def observations(flow: list[dict], cats: dict[int, dict], today: date) -> list[dict]:
    """Tiles at the top of Insights: this month's spending so far and where it's heading, categories
    well above their usual last month, and how much of the last three months' income was left over.

    Each tile: tone (good, bad, or neutral), badge (what the color means, in words), label, value,
    detail, and optionally a category id (for its color dot) and a Search link."""
    tiles = []
    if len(flow) < 2:
        return tiles
    current, done = flow[-1], flow[:-1]
    full = [m for m in done[-6:] if m["spending"] > 0]
    this_month = calendar.month_name[today.month]
    if full and today.day >= 5:
        avg = sum(m["spending"] for m in full) / len(full)
        days = calendar.monthrange(today.year, today.month)[1]
        projected = current["spending"] / today.day * days
        diff = projected - avg
        tone = "bad" if diff > 0.1 * avg else "good" if diff < -0.1 * avg else "neutral"
        tiles.append({"tone": tone, "badge": {"bad": "Above usual", "good": "Below usual", "neutral": "Near usual"}[tone],
                      "label": f"{this_month} spending so far", "value": fmt(current["spending"]),
                      "detail": f"At this rate, about {fmt(projected)} by {calendar.month_abbr[today.month]} {days}. "
                                f"Your {len(full)}-month average is {fmt(avg)}."})
    last, before = done[-1], done[-7:-1]
    if before:
        y, mo = int(last["m"][:4]), int(last["m"][5:])
        end = calendar.monthrange(y, mo)[1]
        spikes = []
        for cid, spent in last["cats"].items():
            usual = sum(m["cats"].get(cid, 0) for m in before) / len(before)
            if spent >= 50 and spent > 1.4 * usual + 25:
                spikes.append((spent - usual, cid, spent, usual))
        for diff, cid, spent, usual in sorted(spikes, reverse=True)[:3]:
            cat = cats.get(int(cid)) if cid != "None" else None
            href = f"/search?start={last['m']}-01&end={last['m']}-{end:02d}" + (f"&category={cid}" if cat else "")
            tiles.append({"tone": "bad", "badge": f"+{fmt(diff)} vs usual",
                          "label": f"{(cat or {}).get('name', 'Other')} in {calendar.month_name[mo]}",
                          "value": fmt(spent), "detail": f"Usually about {fmt(usual)} a month. Tap to see the transactions.",
                          "cat": int(cid) if cat else None, "href": href})
    recent = done[-3:]
    income = sum(m["income"] for m in recent)
    out = sum(m["spending"] + m["debt"] for m in recent)
    kept = income - out
    span = f"{calendar.month_abbr[int(recent[0]['m'][5:])]} to {calendar.month_abbr[int(recent[-1]['m'][5:])]}"
    label = f"Income left over, {span}"
    if income <= 0:
        tiles.append({"tone": "neutral", "badge": "No income found", "label": label, "value": "Not available", "text": True,
                      "detail": "Set your paycheck's category to count as Income (Accounts › Categories)"})
    else:
        rate = round(100 * kept / income)
        tone = "good" if rate >= 20 else "neutral" if rate >= 0 else "bad"
        tiles.append({"tone": tone, "badge": f"Kept {rate}%" if kept >= 0 else "Spent more than came in",
                      "label": label, "value": ("-" if kept < 0 else "") + fmt(kept),
                      "detail": f"{fmt(income)} came in. Spending and debt payments were {fmt(out)}."})
    if len(tiles) % 2:
        extra = _extra_tile(done, cats)
        if extra:
            tiles.append(extra)
    return tiles


def _extra_tile(done: list[dict], cats: dict[int, dict]) -> dict | None:
    """One more tile so they pair up two per row: money saved or invested lately, else a category
    well below its usual last month, else last month's top category."""
    recent = done[-3:]
    saved = sum(m["saving"] for m in recent)
    span = f"{calendar.month_abbr[int(recent[0]['m'][5:])]} to {calendar.month_abbr[int(recent[-1]['m'][5:])]}"
    if saved >= 50:
        return {"tone": "good", "badge": "Invested", "label": f"Saved or invested, {span}", "value": fmt(saved),
                "detail": f"About {fmt(saved / len(recent))} a month into retirement, investment, or savings."}
    last, before = done[-1], done[-7:-1]
    y, mo = int(last["m"][:4]), int(last["m"][5:])
    end = calendar.monthrange(y, mo)[1]

    def link(cid):
        return f"/search?start={last['m']}-01&end={last['m']}-{end:02d}&category={cid}"

    if before:
        drops = []
        for cid in {c for m in before for c in m["cats"]}:
            usual = sum(m["cats"].get(cid, 0) for m in before) / len(before)
            spent = last["cats"].get(cid, 0)
            cat = cats.get(int(cid)) if cid != "None" else None
            if cat and usual >= 100 and spent < 0.6 * usual:
                drops.append((usual - spent, cid, spent, usual, cat))
        if drops:
            diff, cid, spent, usual, cat = max(drops)
            return {"tone": "good", "badge": f"-{fmt(diff)} vs usual", "label": f"{cat['name']} in {calendar.month_name[mo]}",
                    "value": fmt(spent), "detail": f"Usually about {fmt(usual)} a month. Tap to see the transactions.",
                    "cat": int(cid), "href": link(cid)}
    named = [(v, cid) for cid, v in last["cats"].items() if cid != "None" and cats.get(int(cid)) and v > 0]
    if named and last["spending"] > 0:
        v, cid = max(named)
        cat = cats[int(cid)]
        return {"tone": "neutral", "badge": "Top category", "label": f"{cat['name']} in {calendar.month_name[mo]}",
                "value": fmt(v), "detail": f"{round(100 * v / last['spending'])}% of {calendar.month_name[mo]}'s spending. "
                                           "Tap to see the transactions.", "cat": int(cid), "href": link(cid)}
    return None


def fmt(v: float) -> str:
    return f"${abs(v):,.0f}" if abs(v) >= 100 else f"${abs(v):,.2f}"


def month_name(k: str) -> str:
    return f"{calendar.month_name[int(k[5:])]} {k[:4]}"
