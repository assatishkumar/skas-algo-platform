"""Options-strategy-aware analytics, reconstructed from a RunResult.

The equity report (``engine/report.py``) is position-agnostic. For an options run we
also want a *position-lifecycle* view: each short leg's entry premium, how long it was
held, why it closed (target / stop / expiry), and the realized P&L — plus portfolio-level
premium decay and margin usage over time.

This module is fully **additive and options-only**: ``build_options_report`` returns
``None`` when the run has no option symbols (``instrument.parse`` is the seam), so equity
reports are byte-identical and never gain an ``"options"`` key.

Round-trips are reconstructed by pairing ``SHORT`` entries with ``COVER``/``SETTLE`` exits
FIFO per option symbol. Option legs always close as a whole lot (``buy_to_close`` /
settlement pop the entire lot), so each SHORT pairs 1:1 with one exit.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from .instrument import parse


def _iso(d) -> str | None:
    if d is None:
        return None
    if hasattr(d, "strftime"):
        return d.strftime("%Y-%m-%d")
    return str(d)[:10]


def _lots_of(units: int, lot_size: int) -> int:
    return max(1, round(units / lot_size)) if lot_size else 1


def build_options_report(
    result, initial_capital: float, metrics: dict
) -> dict[str, Any] | None:
    """Reconstruct per-position options analytics, or ``None`` for an equity run."""
    txns = result.transactions
    if not any(parse(t["ticker"]) is not None for t in txns):
        return None

    # --- pair entries with exits FIFO per symbol. SHORT→COVER/SETTLE (short legs) and
    #     BUY/AVG_BUY→SELL/SETTLE (long legs, e.g. a ratio spread's hedge). A symbol is
    #     exclusively long or short at a time, so the entry's side labels the round-trip.
    open_legs: dict[str, deque] = defaultdict(deque)
    positions: list[dict] = []
    for t in txns:
        inst = parse(t["ticker"])
        if inst is None:
            continue  # equities never enter the options report
        act = t["action"]
        if act in ("SHORT", "BUY", "AVG_BUY"):
            open_legs[t["ticker"]].append(("short" if act == "SHORT" else "long", t))
        elif act in ("COVER", "SELL", "SETTLE"):
            queue = open_legs.get(t["ticker"])
            if not queue:
                continue  # unmatched exit (shouldn't happen) — skip
            side, entry = queue.popleft()
            positions.append(_round_trip(inst, entry, t, side))

    # --- cycles: group the CE+PE legs entered together (a straddle/strangle) -----
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for p in positions:
        groups[(p["underlying"], p["entry_date"], p["expiry"])].append(p)
    cycles: list[dict] = [_cycle(key, legs) for key, legs in groups.items()]
    cycles.sort(key=lambda c: (c["entry_date"], c["expiry"]))

    # --- portfolio-level series from daily history -------------------------------
    margin_series = [
        {"date": _iso(r["date"]), "margin": float(r["margin_used"])}
        for r in result.history
        if "margin_used" in r
    ]
    premium_curve = [
        {"date": _iso(r["date"]), "premium": float(r["open_premium"])}
        for r in result.history
        if "open_premium" in r
    ]

    return {
        "summary": _summary(positions, cycles, metrics, margin_series),
        "exit_reasons": _exit_reasons(positions),
        "per_expiry_cycle": _per_expiry(positions),
        "positions": sorted(positions, key=lambda p: (p["entry_date"], p["symbol"])),
        "cycles": cycles,
        "margin_series": margin_series,
        "premium_curve": premium_curve,
    }


def _round_trip(inst, entry: dict, exit_ev: dict, side: str = "short") -> dict:
    units = entry["units"]
    entry_premium = entry["price"]
    mult = inst.multiplier
    reason = exit_ev.get("exit_reason") or (
        "expiry" if exit_ev["action"] == "SETTLE" else "manual"
    )
    holding = exit_ev.get("holding_days")
    if holding is None:
        from skas_algo.engine.execution import _as_date

        holding = (_as_date(exit_ev["date"]) - _as_date(entry["date"])).days
    # premium_collected is the entry cashflow: + for a short (received), − for a long (paid).
    sign = 1 if side == "short" else -1
    return {
        "symbol": entry["ticker"],
        "underlying": inst.underlying,
        "strike": inst.strike,
        "right": inst.right,
        "side": side,
        "expiry": inst.expiry.isoformat(),
        "entry_date": _iso(entry["date"]),
        "entry_premium": entry_premium,
        "exit_date": _iso(exit_ev["date"]),
        "exit_price": exit_ev["price"],
        "exit_action": exit_ev["action"],
        "exit_reason": reason,
        "units": units,
        "lots": _lots_of(units, inst.lot_size),
        "multiplier": mult,
        "holding_days": holding,
        "realized_pnl": exit_ev["profit"],
        "pnl_pct": exit_ev["pnl_pct"] * 100.0,
        "premium_collected": sign * entry_premium * units * mult,
    }


def _cycle(key: tuple, legs: list[dict]) -> dict:
    underlying, entry_date, expiry = key
    reasons = {leg["exit_reason"] for leg in legs}
    # ce/pe kept for the 2-leg straddle/strangle view; legs_detail carries ALL legs so the
    # UI can render arbitrary multi-leg structures (e.g. a 3-leg call ratio spread).
    ce = next((leg for leg in legs if leg["right"] == "CE"), None)
    pe = next((leg for leg in legs if leg["right"] == "PE"), None)
    ordered = sorted(legs, key=lambda leg: (leg["right"], leg["strike"]))
    return {
        "underlying": underlying,
        "entry_date": entry_date,
        "expiry": expiry,
        "legs": [leg["symbol"] for leg in legs],
        "legs_detail": ordered,
        "premium_collected": sum(leg["premium_collected"] for leg in legs),
        "realized_pnl": sum(leg["realized_pnl"] for leg in legs),
        "holding_days": max(leg["holding_days"] for leg in legs),
        "exit_reason": next(iter(reasons)) if len(reasons) == 1 else "mixed",
        "ce": ce,
        "pe": pe,
    }


def _summary(positions, cycles, metrics, margin_series) -> dict:
    n = len(positions)
    collected = sum(p["premium_collected"] for p in positions)
    captured = sum(p["realized_pnl"] for p in positions)
    wins = sum(1 for p in positions if p["realized_pnl"] > 0)
    max_margin = float(metrics.get("Max Margin Used", 0.0) or 0.0)
    margins = [m["margin"] for m in margin_series if m["margin"] > 0]
    return {
        "total_premium_collected": collected,
        "total_premium_captured": captured,
        "premium_capture_pct": (captured / collected * 100.0) if collected else 0.0,
        "avg_holding_days": (sum(p["holding_days"] for p in positions) / n) if n else 0.0,
        "num_positions": n,
        "num_cycles": len(cycles),
        "win_rate_pct": (wins / n * 100.0) if n else 0.0,
        "max_margin_used": max_margin,
        "avg_margin_used": (sum(margins) / len(margins)) if margins else 0.0,
        "capital_efficiency": (collected / max_margin) if max_margin else 0.0,
        "avg_premium_per_cycle": (collected / len(cycles)) if cycles else 0.0,
    }


def _exit_reasons(positions) -> dict:
    out: dict[str, dict] = {}
    for p in positions:
        bucket = out.setdefault(
            p["exit_reason"], {"count": 0, "pnl": 0.0, "wins": 0, "losses": 0}
        )
        bucket["count"] += 1
        bucket["pnl"] += p["realized_pnl"]
        if p["realized_pnl"] > 0:
            bucket["wins"] += 1
        else:
            bucket["losses"] += 1
    return out


def _per_expiry(positions) -> list[dict]:
    groups: dict[str, dict] = {}
    for p in positions:
        g = groups.setdefault(
            p["expiry"], {"expiry": p["expiry"], "entries": 0, "premium_collected": 0.0, "realized_pnl": 0.0}
        )
        g["entries"] += 1
        g["premium_collected"] += p["premium_collected"]
        g["realized_pnl"] += p["realized_pnl"]
    rows = sorted(groups.values(), key=lambda r: r["expiry"])
    for r in rows:
        r["win"] = r["realized_pnl"] > 0
    return rows
