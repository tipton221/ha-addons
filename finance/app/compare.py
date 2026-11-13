"""Compare Plaid and YNAB transactions for linked accounts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from .db import Database

DATE_TOLERANCE = 4      # days the two sources may disagree on a date
SETTLE_DAYS = 3         # ignore the last few days: one source may not have them yet
NEAR_AMOUNT = 0.25      # "same purchase, different amount" if within 25% (tips, holds)

NOISE = {"american", "express", "amex", "card", "the", "account", "credit", "visa", "checking",
         "savings", "tm"}


def _tokens(name: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", name.lower().replace("®", " ").replace("(tm)", " "))
    return {w for w in words if w not in NOISE}


def suggest_links(plaid_accounts: list[dict], ynab_accounts: list[dict]) -> dict[str, str]:
    """Best-guess YNAB account for each Plaid account, by name similarity."""
    open_ynab = [y for y in ynab_accounts if y.get("subtype") != "closed"]
    out: dict[str, str] = {}
    taken: set[str] = set()
    scored = []
    for p in plaid_accounts:
        pt = _tokens(p["name"]) or _tokens(p.get("official_name") or "")
        for y in open_ynab:
            yt = _tokens(y["name"])
            if not pt or not yt:
                continue
            score = len(pt & yt) / len(pt | yt)
            if p.get("mask") and p["mask"] in y["name"]:
                score += 0.5
            if score >= 0.4:
                scored.append((score, p["account_id"], y["account_id"]))
    for score, pid, yid in sorted(scored, reverse=True):
        if pid not in out and yid not in taken:
            out[pid] = yid
            taken.add(yid)
    return out


@dataclass
class PairResult:
    plaid_account: dict
    ynab_account: dict
    start: str
    end: str
    matched: int = 0
    only_plaid: list[dict] = field(default_factory=list)
    only_ynab: list[dict] = field(default_factory=list)
    amount_differs: list[tuple[dict, dict]] = field(default_factory=list)
    ynab_count: int = 0
    ynab_first: str | None = None
    # Plaid-only transactions that do appear in YNAB, but under a different YNAB account.
    elsewhere: dict[str, str] = field(default_factory=dict)


def _d(s: str) -> date:
    return date.fromisoformat(s)


def compare_lists(plaid: list[dict], ynab: list[dict], start: str, end: str) -> PairResult:
    """Match posted Plaid transactions to YNAB transactions within [start, end]."""
    res = PairResult({}, {}, start, end)
    lo, hi = _d(start), _d(end)
    tol = timedelta(days=DATE_TOLERANCE)
    p_in = [t for t in plaid if not t["pending"] and lo <= _d(t["date"]) <= hi]
    y_pool = [t for t in ynab if lo - tol <= _d(t["date"]) <= hi + tol]
    used: set[str] = set()

    def best(t, accept):
        cands = [y for y in y_pool if y["id"] not in used and accept(t, y)
                 and abs(_d(y["date"]) - _d(t["date"])) <= tol]
        return min(cands, key=lambda y: abs(_d(y["date"]) - _d(t["date"])), default=None)

    exact = lambda t, y: abs(t["amount"] - y["amount"]) < 0.005  # noqa: E731
    leftovers = []
    for t in sorted(p_in, key=lambda t: t["date"]):
        y = best(t, exact)
        if y:
            used.add(y["id"])
            res.matched += 1
        else:
            leftovers.append(t)

    def near(t, y):
        a, b = t["amount"], y["amount"]
        return a * b > 0 and abs(a - b) <= NEAR_AMOUNT * max(abs(a), abs(b))

    for t in leftovers:
        y = best(t, near)
        if y:
            used.add(y["id"])
            res.amount_differs.append((t, y))
        else:
            res.only_plaid.append(t)
    res.only_ynab = [y for y in y_pool if y["id"] not in used and lo <= _d(y["date"]) <= hi]
    return res


def compare_all(db: Database, today: date | None = None) -> dict:
    today = today or date.today()
    end = (today - timedelta(days=SETTLE_DAYS)).isoformat()
    with db.connect() as conn:
        plaid_accounts = [dict(r) for r in conn.execute(
            "SELECT a.*, p.institution FROM accounts a LEFT JOIN plaid_items p ON a.item_id = p.item_id "
            "WHERE a.source='plaid' ORDER BY p.institution, a.name")]
        ynab_accounts = [dict(r) for r in conn.execute(
            "SELECT * FROM accounts WHERE source='ynab' ORDER BY subtype DESC, name")]
        links = {r["plaid_account_id"]: r["ynab_account_id"]
                 for r in conn.execute("SELECT * FROM account_links")}
    ynab_by_id = {y["account_id"]: y for y in ynab_accounts}
    pairs = []
    with db.connect() as conn:
        for p in plaid_accounts:
            yid = links.get(p["account_id"])
            if not yid or yid not in ynab_by_id:
                continue
            first = conn.execute(
                "SELECT MIN(date) AS d FROM transactions WHERE source='plaid' AND account_id=?",
                (p["account_id"],)).fetchone()["d"]
            if not first or first > end:
                continue
            cols = "source, id, date, name, merchant, amount, pending, category"
            pt = [dict(r) for r in conn.execute(
                f"SELECT {cols} FROM transactions WHERE source='plaid' AND account_id=?", (p["account_id"],))]
            yt = [dict(r) for r in conn.execute(
                f"SELECT {cols} FROM transactions WHERE source='ynab' AND account_id=?", (yid,))]
            r = compare_lists(pt, yt, first, end)
            r.plaid_account, r.ynab_account = p, ynab_by_id[yid]
            r.ynab_count = len(yt)
            r.ynab_first = min((t["date"] for t in yt), default=None)
            tol = timedelta(days=DATE_TOLERANCE)
            for t in r.only_plaid:
                d0 = _d(t["date"])
                hit = conn.execute(
                    "SELECT a.name FROM transactions t JOIN accounts a ON a.account_id = t.account_id "
                    "WHERE t.source='ynab' AND t.account_id != ? AND ABS(t.amount - ?) < 0.005 "
                    "AND t.date BETWEEN ? AND ? LIMIT 1",
                    (yid, t["amount"], (d0 - tol).isoformat(), (d0 + tol).isoformat())).fetchone()
                if hit:
                    r.elsewhere[t["id"]] = hit["name"]
            pairs.append(r)
    return {
        "plaid_accounts": plaid_accounts,
        "ynab_accounts": ynab_accounts,
        "links": links,
        "suggested": suggest_links(plaid_accounts, ynab_accounts),
        "pairs": pairs,
        "end": end,
    }
