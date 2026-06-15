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

    # --- open option legs still live at the run end (expiry beyond end_date): marked to
    #     the last close so a combined strategy net reconciles with the equity curve.
    final_marks = getattr(result, "final_marks", {}) or {}
    option_open_pnl = 0.0
    for sym, queue in open_legs.items():
        inst = parse(sym)
        mark = final_marks.get(sym)
        if inst is None or mark is None:
            continue
        for side, entry in queue:
            units, mult = entry["units"], inst.multiplier
            option_open_pnl += ((entry["price"] - mark) if side == "short"
                                else (mark - entry["price"])) * units * mult

    # --- cycles: group the CE+PE legs entered together (a straddle/strangle) -----
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for p in positions:
        groups[(p["underlying"], p["entry_date"], p["expiry"])].append(p)
    cycles: list[dict] = [_cycle(key, legs) for key, legs in groups.items()]
    cycles.sort(key=lambda c: (c["entry_date"], c["expiry"]))

    # --- equity (covered-leg) round trips: a strategy may BUY an underlying inside an
    #     options run (the staggered covered call accumulates an ETF against the sold
    #     call). Those legs are where the bulk of the P&L books, but they're not options
    #     — pair BUY/AVG_BUY → SELL per non-option symbol so the report shows them. Empty
    #     for pure-options runs, so nothing changes there.
    equity_open: dict[str, list] = defaultdict(list)
    equity_legs: list[dict] = []
    for t in txns:
        if parse(t["ticker"]) is not None:
            continue  # options handled above
        act = t["action"]
        if act in ("BUY", "AVG_BUY"):
            equity_open[t["ticker"]].append(t)
        elif act == "SELL":
            buys = equity_open.get(t["ticker"])
            if buys:
                equity_legs.append(_equity_round_trip(t["ticker"], list(buys), t))
                buys.clear()
    equity_held: list[dict] = [
        _equity_held(sym, list(buys), final_marks.get(sym))
        for sym, buys in equity_open.items() if buys
    ]
    equity_legs.sort(key=lambda l: l["entry_date"])

    # --- F&O transaction charges across all option legs (same schedule the engine
    #     deducted from cash at execution, so the totals reconcile with the equity curve).
    from .charges import charges_for_txn

    charges = {"brokerage": 0.0, "stt": 0.0, "exchange": 0.0, "sebi": 0.0,
               "stamp": 0.0, "gst": 0.0, "total": 0.0}
    for t in txns:
        if parse(t["ticker"]) is None:
            continue
        for k, v in charges_for_txn(t).items():
            charges[k] += v

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

    summary = _summary(positions, cycles, metrics, margin_series, charges["total"])
    _add_equity_summary(summary, equity_legs, equity_held, option_open_pnl)

    out = {
        "summary": summary,
        "charges": charges,
        "exit_reasons": _exit_reasons(positions),
        "per_expiry_cycle": _per_expiry(positions),
        "positions": sorted(positions, key=lambda p: (p["entry_date"], p["symbol"])),
        "cycles": cycles,
        "margin_series": margin_series,
        "premium_curve": premium_curve,
    }
    if equity_legs or equity_held:
        out["equity_legs"] = equity_legs
        out["equity_held"] = equity_held
        out["campaigns"] = _build_campaigns(equity_legs, equity_held, positions)
    return out


def _build_campaigns(equity_legs: list[dict], equity_held: list[dict],
                     positions: list[dict]) -> list[dict]:
    """Group a covered-call run into CAMPAIGNS: one per accumulation→called-away round
    trip (plus the still-open holding). Each campaign carries its tranche buys and the
    calls sold/rolled while it was live, with a combined net (equity + option). A call is
    assigned to the latest campaign whose start is on/before the call's entry date."""
    import bisect

    camps: list[dict] = []
    for l in equity_legs:
        camps.append({
            "start": l["entry_date"], "end": l["exit_date"], "status": "called_away",
            "units": l["units"], "avg_cost": l["entry_price"], "exit_price": l["exit_price"],
            "exit_reason": l["exit_reason"], "holding_days": l["holding_days"],
            "equity_realized": l["realized_pnl"], "equity_open": 0.0,
            "tranches": l["tranches"], "calls": [],
        })
    for h in equity_held:
        camps.append({
            "start": h["entry_date"], "end": None, "status": "open",
            "units": h["units"], "avg_cost": h["entry_price"], "exit_price": None,
            "mark": h.get("mark"), "exit_reason": "open", "holding_days": None,
            "equity_realized": 0.0, "equity_open": h.get("unrealized_pnl") or 0.0,
            "tranches": h["tranches"], "calls": [],
        })
    if not camps:
        return []
    camps.sort(key=lambda c: c["start"])
    starts = [c["start"] for c in camps]
    for p in positions:
        if p.get("right") != "CE":
            continue
        i = max(0, bisect.bisect_right(starts, p["entry_date"]) - 1)
        camps[i]["calls"].append({
            "entry_date": p["entry_date"], "strike": p["strike"],
            "entry_premium": p["entry_premium"], "exit_date": p["exit_date"],
            "exit_price": p["exit_price"], "exit_reason": p["exit_reason"],
            "premium_collected": p["premium_collected"], "realized_pnl": p["realized_pnl"],
            "net_pnl": p.get("net_pnl", p["realized_pnl"]),
        })
    for c in camps:
        c["calls"].sort(key=lambda x: x["entry_date"])
        c["n_calls"] = len(c["calls"])
        c["option_net"] = sum(x["net_pnl"] for x in c["calls"])
        c["premium_collected"] = sum(x["premium_collected"] for x in c["calls"])
        c["combined_net"] = c["equity_realized"] + c["equity_open"] + c["option_net"]
    return camps


def _equity_round_trip(symbol: str, buys: list[dict], sell: dict) -> dict:
    """One closed equity round-trip: a run of accumulating BUY/AVG_BUY legs realized by
    a single SELL (the covered call's tranches → called-away liquidation). Realized P&L
    is the SELL event's own pooled profit; entry is the size-weighted average cost."""
    from skas_algo.engine.execution import _as_date

    bought_units = sum(b["units"] for b in buys) or 1
    avg_cost = sum(b["units"] * b["price"] for b in buys) / bought_units
    return {
        "symbol": symbol,
        "side": "equity",
        "entry_date": _iso(buys[0]["date"]),
        "entry_price": avg_cost,
        "exit_date": _iso(sell["date"]),
        "exit_price": sell["price"],
        "exit_reason": sell.get("exit_reason") or "sold",
        "units": sell["units"],
        "realized_pnl": sell["profit"],
        "holding_days": (_as_date(sell["date"]) - _as_date(buys[0]["date"])).days,
        "tranches": [
            {"date": _iso(b["date"]), "units": b["units"], "price": b["price"],
             "tag": b.get("tag", "")} for b in buys
        ],
    }


def _equity_held(symbol: str, buys: list[dict], mark: float | None) -> dict:
    """Still-open equity at the end of the run: held tranche units marked to the last
    known close (``mark``), with unrealized P&L vs the average cost."""
    units = sum(b["units"] for b in buys) or 1
    avg_cost = sum(b["units"] * b["price"] for b in buys) / units
    unreal = ((mark - avg_cost) * units) if mark is not None else None
    return {
        "symbol": symbol,
        "side": "equity_open",
        "entry_date": _iso(buys[0]["date"]),
        "entry_price": avg_cost,
        "units": units,
        "mark": mark,
        "unrealized_pnl": unreal,
        "tranches": [
            {"date": _iso(b["date"]), "units": b["units"], "price": b["price"],
             "tag": b.get("tag", "")} for b in buys
        ],
    }


def _add_equity_summary(summary: dict, equity_legs: list[dict], equity_held: list[dict],
                        option_open_pnl: float = 0.0) -> None:
    """Fold the covered leg into the summary: realized + open equity P&L and a combined
    strategy net (option net-after-charges + open option MTM + equity). Added only when
    equity legs exist so pure-options summaries are unchanged. The combined net then
    reconciles to the run's Final Equity − capital regardless of the end-of-run state."""
    if not (equity_legs or equity_held):
        return
    realized = sum(l["realized_pnl"] for l in equity_legs)
    open_pnl = sum(l["unrealized_pnl"] for l in equity_held
                   if l.get("unrealized_pnl") is not None)
    summary["equity_realized_pnl"] = realized
    summary["equity_open_pnl"] = open_pnl
    summary["equity_units_held"] = sum(l["units"] for l in equity_held)
    summary["option_open_pnl"] = option_open_pnl
    summary["strategy_net_pnl"] = (summary["net_after_charges"] + option_open_pnl
                                   + realized + open_pnl)


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
    from .charges import charges_for_txn

    charges = charges_for_txn(entry)["total"] + charges_for_txn(exit_ev)["total"]
    realized = exit_ev["profit"]
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
        "realized_pnl": realized,
        "pnl_pct": exit_ev["pnl_pct"] * 100.0,
        "premium_collected": sign * entry_premium * units * mult,
        "charges": charges,
        "net_pnl": realized - charges,
    }


def _cycle(key: tuple, legs: list[dict]) -> dict:
    underlying, entry_date, expiry = key
    reasons = {leg["exit_reason"] for leg in legs}
    # ce/pe kept for the 2-leg straddle/strangle view; legs_detail carries ALL legs so the
    # UI can render arbitrary multi-leg structures (e.g. a 3-leg call ratio spread).
    ce = next((leg for leg in legs if leg["right"] == "CE"), None)
    pe = next((leg for leg in legs if leg["right"] == "PE"), None)
    ordered = sorted(legs, key=lambda leg: (leg["right"], leg["strike"]))
    realized = sum(leg["realized_pnl"] for leg in legs)
    charges = sum(leg["charges"] for leg in legs)
    return {
        "underlying": underlying,
        "entry_date": entry_date,
        "exit_date": max(leg["exit_date"] for leg in legs),
        "expiry": expiry,
        "legs": [leg["symbol"] for leg in legs],
        "legs_detail": ordered,
        "premium_collected": sum(leg["premium_collected"] for leg in legs),
        "realized_pnl": realized,
        "charges": charges,
        "net_pnl": realized - charges,
        "holding_days": max(leg["holding_days"] for leg in legs),
        "exit_reason": next(iter(reasons)) if len(reasons) == 1 else "mixed",
        "ce": ce,
        "pe": pe,
    }


def _summary(positions, cycles, metrics, margin_series, total_charges: float = 0.0) -> dict:
    n = len(positions)
    nc = len(cycles)
    collected = sum(p["premium_collected"] for p in positions)
    captured = sum(p["realized_pnl"] for p in positions)
    # Win rate is per POSITION (cycle/structure), not per leg: a cycle is a win if it hit
    # its profit target or otherwise closed in net profit (time-exit in the green). Stops
    # are losses. (Leg-level P&L is misleading — the long legs of a ratio spread usually
    # lose while the short body wins.)
    cyc_wins = sum(1 for c in cycles if c.get("exit_reason") == "target" or c.get("net_pnl", 0.0) > 0)
    max_margin = float(metrics.get("Max Margin Used", 0.0) or 0.0)
    margins = [m["margin"] for m in margin_series if m["margin"] > 0]
    return {
        "total_premium_collected": collected,
        "total_premium_captured": captured,
        "premium_capture_pct": (captured / collected * 100.0) if collected else 0.0,
        "avg_holding_days": (sum(p["holding_days"] for p in positions) / n) if n else 0.0,
        "num_positions": n,
        "num_cycles": nc,
        "winning_cycles": cyc_wins,
        "win_rate_pct": (cyc_wins / nc * 100.0) if nc else 0.0,
        "max_margin_used": max_margin,
        "avg_margin_used": (sum(margins) / len(margins)) if margins else 0.0,
        "capital_efficiency": (collected / max_margin) if max_margin else 0.0,
        "avg_premium_per_cycle": (collected / len(cycles)) if cycles else 0.0,
        "total_charges": total_charges,
        "net_after_charges": captured - total_charges,
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
