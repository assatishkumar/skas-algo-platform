"""Helpers shared by options strategies (premium sanity, strike snap, expiry pick)."""

from __future__ import annotations

from datetime import date


def bad_close(x) -> bool:
    return x is None or x != x or x <= 0  # None / NaN / non-positive premium


def snap(strikes: list[float], target: float) -> float | None:
    """Nearest listed strike to ``target`` (None on an empty chain)."""
    return min(strikes, key=lambda k: abs(k - target)) if strikes else None


def legs_mtm_pnl(legs, closes: dict) -> float | None:
    """The DECISION-basis MTM the %-of-margin exit checks compare: Σ dir × (mark − entry)
    × units over the strategy's OWN legs. Leg entries are the decision-time premiums, not
    the actual fills, so live this can differ from the book P&L by the fill slippage
    (run-7 2026-07-17: ~₹276 on the short leg — the UI said "target achieved" while the
    strategy's own measure was still below it). Surfaced in the snapshot as
    ``strategy_pnl`` so the screen shows the number the strategy ACTS on. None when flat
    or any leg lacks a mark (matching the strategies' own bail-outs on missing prints)."""
    if not legs:
        return None
    total = 0.0
    for leg in legs:
        cur = closes.get(leg["symbol"])
        if cur is None:
            return None
        total += (float(cur) - float(leg["entry"])) * leg["units"] * leg["dir"]
    return total


def next_monthly_expiry(chain, underlying: str, today: date, min_dte: int,
                        right: str = "CE") -> date | None:
    """The nearest monthly expiry at least ``min_dte`` out.

    "Monthly" = the most LIQUID expiry of its calendar month (highest total open
    interest on today's chain), not simply the latest date — exchanges sometimes list
    odd late-month expiries whose contracts never trade but still carry frozen
    bhavcopy closes (e.g. NIFTY 2025-04-30 vs the real 2025-04-24 monthly); picking
    by date would enter phantom, un-executable positions.
    """
    exps = chain.expiries(underlying, today)
    if not exps:
        return None
    by_month: dict[tuple[int, int], list[date]] = {}
    for e in exps:
        if (e - today).days >= min_dte:
            by_month.setdefault((e.year, e.month), []).append(e)
    if not by_month:
        return None
    month = min(by_month)  # nearest qualifying month
    cands = by_month[month]
    if len(cands) == 1:
        return cands[0]

    def total_oi(exp: date) -> int:
        return sum(r.oi for r in chain.chain(underlying, today, exp) if r.right == right)

    return max(cands, key=total_oi)
