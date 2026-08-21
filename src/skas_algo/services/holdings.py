"""Portfolio breakdown for ACCUMULATION runs — the report an investor reads.

A buy-and-hold strategy has no round trips, so the trading report describes almost nothing
about it: "win rate" and "net realized P&L" measure the ETF sweeps that funded the buying,
and every rupee of actual return sits unrealized in open positions. This builds the view that
does describe it — what you own, what it cost, what it is worth, and how that compares to
having bought the index with the same money on the same days.

Three deliberate choices:

* **XIRR, not CAGR.** Units are bought across years, so a simple start/end CAGR is
  meaningless — it has no single start. XIRR (money-weighted) is the CAGR of a staggered
  purchase and is what a broker statement would call the return.
* **The benchmark is an index SIP, not a lump sum.** The comparison that answers "should I
  have just bought the index?" is the SAME rupees on the SAME days into the index. A lump-sum
  index line would flatter or damn the strategy purely on when money happened to arrive.
* **Fund yield is stated, never folded in.** A dividend liquid ETF (LIQUIDBEES) holds NAV
  flat and pays out as units, so a price-only backtest credits parked money 0%. That is a
  modelling gap, not a result — so the yield it would really have earned is reported as its
  own line at a stated rate, and the engine's equity curve is left exactly as the engine
  computed it.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

_ENTRY = {"BUY", "AVG_BUY"}


def _as_date(v: Any) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def xirr(flows: list[tuple[date, float]], *, lo: float = -0.95, hi: float = 10.0) -> float | None:
    """Money-weighted annual return. ``flows`` are (date, amount) with money IN negative and
    the closing value positive. Bisection — Newton diverges on the spiky cashflow of a daily
    drip. None when the flows are degenerate (no sign change / single day)."""
    if len(flows) < 2:
        return None
    if not (any(a < 0 for _, a in flows) and any(a > 0 for _, a in flows)):
        return None
    d0 = min(d for d, _ in flows)
    if all(d == d0 for d, _ in flows):
        return None

    def npv(r: float) -> float:
        return sum(a / (1.0 + r) ** ((d - d0).days / 365.0) for d, a in flows)

    if npv(lo) * npv(hi) > 0:
        return None  # no root in a sane range (a total wipeout, or absurd growth)
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if npv(mid) > 0:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2.0, 6)


def holdings_report(
    transactions: list[dict],
    marks: dict[str, float],
    *,
    as_of: date | None = None,
    fund_source: str | None = None,
    fund_yield_pct: float = 0.0,
    equity_curve: list[dict] | None = None,
    benchmark: str | None = None,
    benchmark_prices: dict[str, float] | None = None,
) -> dict:
    """The end-of-run portfolio, per holding and in total.

    ``marks`` is the last-known close per symbol (``RunResult.final_marks``);
    ``benchmark_prices`` is {date_iso: close} for the index the SIP comparison buys.
    """
    as_of = as_of or date.today()
    fund = (fund_source or "").upper()

    per: dict[str, dict] = {}
    flows: list[tuple[date, float]] = []
    fund_units = 0.0
    for t in transactions:
        sym = t.get("ticker")
        if not sym:
            continue
        units = float(t.get("units") or 0)
        amount = float(t.get("amount") or 0)
        signed = units if t.get("action") in _ENTRY else -units
        if sym.upper() == fund:
            fund_units += signed
            continue
        row = per.setdefault(sym, {"symbol": sym, "units": 0.0, "invested": 0.0,
                                   "buys": 0, "first_buy": None, "flows": []})
        row["units"] += signed
        if t.get("action") in _ENTRY:
            row["invested"] += amount
            row["buys"] += 1
            d = _as_date(t["date"])
            row["first_buy"] = min(row["first_buy"] or d, d)
            row["flows"].append((d, -amount))
            flows.append((d, -amount))
        else:
            # A sale reduces the cost basis proportionally — this strategy never sells, but
            # the breakdown must stay correct if one ever does (or a leg is manually closed).
            d = _as_date(t["date"])
            row["invested"] -= amount
            row["flows"].append((d, amount))
            flows.append((d, amount))

    rows = []
    total_value = total_invested = 0.0
    for sym, row in per.items():
        if row["units"] <= 1e-9:
            continue  # fully exited — it belongs to realized P&L, not the holdings table
        price = float(marks.get(sym) or 0.0)
        value = row["units"] * price
        invested = round(row["invested"], 2)
        pnl = value - invested
        total_value += value
        total_invested += invested
        rows.append({
            "symbol": sym,
            "units": int(row["units"]) if float(row["units"]).is_integer() else row["units"],
            "invested": round(invested, 2),
            "avg_cost": round(invested / row["units"], 2) if row["units"] else None,
            "last_price": round(price, 2),
            "value": round(value, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / invested * 100.0, 2) if invested else None,
            # per-holding money-weighted CAGR: its own buys, plus its value today
            "xirr_pct": _pct(xirr([*row["flows"], (as_of, value)])),
            "first_buy": row["first_buy"].isoformat() if row["first_buy"] else None,
            "buys": row["buys"],
            "weight_pct": None,  # filled once the total is known
        })
    for r in rows:
        r["weight_pct"] = round(r["value"] / total_value * 100.0, 2) if total_value else None
    rows.sort(key=lambda r: -(r["value"] or 0))

    sleeve_xirr = _pct(xirr([*flows, (as_of, total_value)])) if flows else None
    out: dict[str, Any] = {
        "as_of": as_of.isoformat(),
        "rows": rows,
        "totals": {
            "invested": round(total_invested, 2),
            "value": round(total_value, 2),
            "pnl": round(total_value - total_invested, 2),
            "pnl_pct": (round((total_value - total_invested) / total_invested * 100.0, 2)
                        if total_invested else None),
            "xirr_pct": sleeve_xirr,
            "names": len(rows),
        },
    }

    if fund:
        fund_price = float(marks.get(fund) or 0.0)
        out["fund"] = {
            "symbol": fund,
            "units": round(fund_units, 4),
            "value": round(fund_units * fund_price, 2),
            "yield_pct": fund_yield_pct,
            # What the parked balance would really have earned. Reported, never folded into
            # the engine's equity curve — see the module docstring.
            "yield_credited": round(_parked_yield(equity_curve, transactions, marks,
                                                  fund, fund_yield_pct), 2),
        }

    if benchmark and benchmark_prices:
        out["benchmark"] = _index_sip(flows, benchmark, benchmark_prices, as_of,
                                      total_value, sleeve_xirr)
    return out


def _pct(x: float | None) -> float | None:
    return None if x is None else round(x * 100.0, 2)


def _parked_yield(equity_curve, transactions, marks, fund: str, yield_pct: float) -> float:
    """Simple interest on the fund holding's value, day by day along the equity curve."""
    if yield_pct <= 0 or not equity_curve:
        return 0.0
    price = float(marks.get(fund) or 0.0)
    if price <= 0:
        return 0.0
    # fund units held on each date, from the trade tape
    moves: list[tuple[date, float]] = []
    for t in transactions:
        if str(t.get("ticker", "")).upper() != fund:
            continue
        u = float(t.get("units") or 0)
        moves.append((_as_date(t["date"]), u if t.get("action") in _ENTRY else -u))
    moves.sort()
    total = 0.0
    units = 0.0
    mi = 0
    prev: date | None = None
    for p in equity_curve:
        d = _as_date(p.get("date"))
        while mi < len(moves) and moves[mi][0] <= d:
            units += moves[mi][1]
            mi += 1
        if prev is not None and units > 0:
            total += units * price * (yield_pct / 100.0) * (d - prev).days / 365.0
        prev = d
    return total


def _index_sip(flows, index: str, prices: dict[str, float], as_of: date,
               strategy_value: float, strategy_xirr: float | None) -> dict:
    """The same rupees on the same days, into ``index``: the only fair yardstick for a drip."""
    days = sorted(prices)
    if not days:
        return {}

    def price_on(d: date) -> float | None:
        iso = d.isoformat()
        hit = [x for x in days if x <= iso]
        return prices[hit[-1]] if hit else None

    units = 0.0
    for d, amount in flows:
        if amount >= 0:
            continue  # only money going IN buys index units
        px = price_on(d)
        if px:
            units += (-amount) / px
    value = units * prices[days[-1]]
    bench_xirr = _pct(xirr([*flows, (as_of, value)]))
    return {
        "index": index,
        "value": round(value, 2),
        "xirr_pct": bench_xirr,
        "vs_value": round(strategy_value - value, 2),
        "vs_xirr_pts": (None if (bench_xirr is None or strategy_xirr is None)
                        else round(strategy_xirr - bench_xirr, 2)),
    }
