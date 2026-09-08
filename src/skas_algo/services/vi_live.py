"""value_investing on the Live tile: what the run OWNS, per watchlist name, and what it will
do today (owner ask, 2026-09-08).

The generic tile describes a run by realized/unrealized P&L over its open positions, and
counts the fund-source ETF as a position. For an accumulation strategy that reads wrong on
every line: the ETF is the money waiting to be invested, not an investment; nothing is
ever realized; and the number that matters is invested rupees against market value, per
name, with a money-weighted return — the backtest's ``holdings_report`` view. This builds
exactly that from the run's OWN transactions and marks, then adds what a live run has that
a finished backtest does not: every watchlist name whether or not it has been bought, the
pooled rupees each name is saving, and the buys today's decision would make.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from skas_algo.services.holdings import holdings_report

MIN_XIRR_DAYS = 90   # younger than this, an annualised return is noise


def _marks(market, symbols: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for sym in symbols:
        px = None
        for name in ("last_close", "close"):
            fn = getattr(market, name, None)
            if fn is None:
                continue
            try:
                px = fn(sym)
            except Exception:
                px = None
            if px:
                break
        if px:
            out[sym] = float(px)
    return out


def _change_pct(market, sym: str, mark: float | None) -> float | None:
    fn = getattr(market, "prev_close", None)
    if fn is None or mark is None:
        return None
    try:
        prev = fn(sym)
    except Exception:
        return None
    if not prev or prev <= 0:
        return None
    return round((mark / float(prev) - 1.0) * 100.0, 2)


def value_investing_report(live, today: date | None = None) -> dict[str, Any]:
    """The tile's view of a value_investing deployment (``live`` = a LiveRun)."""
    session = live.session
    strategy = session.strategy
    market = getattr(session, "market", None)
    portfolio = session.portfolio
    today = today or datetime.now().date()
    fund = str(getattr(strategy, "fund_source", "") or "").upper() or None
    watch = [s for s in (getattr(strategy, "watchlist", None) or []) if s and s != fund]

    # --- transactions: the running session's own book, serialized like /trades ---
    from skas_algo.live.manager import _serialize_event

    txns = [_serialize_event(t) for t in (session.transactions or [])]
    traded = sorted({t["ticker"] for t in txns if t.get("ticker")})
    symbols = sorted(set(watch) | set(traded) | ({fund} if fund else set()))
    marks = _marks(market, symbols) if market is not None else {}
    # a name with no live mark: the last price it traded at, so its row still prices
    for t in txns:
        marks.setdefault(t["ticker"], float(t.get("price") or 0.0))

    report = holdings_report(txns, marks, as_of=today, fund_source=fund,
                             fund_yield_pct=float(getattr(strategy, "fund_yield_pct", 0.0) or 0.0))
    held = {r["symbol"]: r for r in report["rows"]}
    # A money-weighted CAGR annualises whatever happened since the first buy: on a name
    # held eight days a −2% dip reads −72%. Blank it until a holding is MIN_XIRR_DAYS old
    # (the backtest panel never faces this — its runs span years).
    for r in held.values():
        fb = r.get("first_buy")
        if fb and (today - date.fromisoformat(fb)).days < MIN_XIRR_DAYS:
            r["xirr_pct"] = None
    firsts = [date.fromisoformat(r["first_buy"]) for r in held.values() if r.get("first_buy")]
    if firsts and (today - min(firsts)).days < MIN_XIRR_DAYS:
        report["totals"]["xirr_pct"] = None

    # --- today: the real ranking and planner on copies (nothing is credited or spent) ---
    preview: dict[str, Any] = {}
    if market is not None and hasattr(strategy, "preview_plan"):
        try:
            preview = strategy.preview_plan(market, today)
        except Exception:  # pragma: no cover - a preview must never break the tile
            preview = {}
    plan = {sym: (px, units, cost) for sym, px, units, cost in preview.get("plan", [])}
    afford = {sym: (px, units, cost) for sym, px, units, cost in preview.get("affordable", [])}
    ranked = {sym: (chg, px) for sym, chg, px in preview.get("ranked", [])}
    pots = preview.get("pots") or {}

    # --- every watchlist name: held, exited, or not yet bought ---
    names = list(watch) + [s for s in traded if s not in watch and s != fund]
    rows: list[dict[str, Any]] = []
    for sym in names:
        h = held.get(sym)
        mark = marks.get(sym)
        bought_ever = any(t["ticker"] == sym for t in txns)
        status = "held" if h else ("exited" if bought_ever else "pending")
        chg, _px = ranked.get(sym, (None, None))
        row = {
            "symbol": sym,
            "in_watchlist": sym in watch,
            "status": status,
            "change_pct": chg if chg is not None else _change_pct(market, sym, mark),
            "rank": (list(ranked).index(sym) + 1) if sym in ranked else None,
            "units": h["units"] if h else 0,
            "avg_cost": h["avg_cost"] if h else None,
            "last_price": mark,
            "invested": h["invested"] if h else 0.0,
            "value": h["value"] if h else 0.0,
            "pnl": h["pnl"] if h else None,
            "pnl_pct": h["pnl_pct"] if h else None,
            "xirr_pct": h["xirr_pct"] if h else None,
            "weight_pct": h["weight_pct"] if h else None,
            "first_buy": h["first_buy"] if h else None,
            "buys": h["buys"] if h else 0,
            "pot": round(float(pots.get(sym, 0.0)), 2),
            "buys_today": (
                {"units": plan[sym][1], "price": plan[sym][0], "cost": plan[sym][2]}
                if sym in plan else None
            ),
            # what its pot could buy at today's price if cash were no object
            "affordable": (
                {"units": afford[sym][1], "price": afford[sym][0], "cost": afford[sym][2]}
                if sym in afford else None
            ),
        }
        rows.append(row)
    # sort: today's buys first, then held by value, then pending by rank
    rows.sort(key=lambda r: (0 if r["buys_today"] else 1 if r["affordable"] else 2,
                             0 if r["status"] == "held" else 1,
                             -(r["value"] or 0.0), r["rank"] or 10_000))

    basket = {}
    fn = getattr(strategy, "basket_status", None)
    if fn is not None:
        try:
            basket = fn(market, portfolio)
        except Exception:  # pragma: no cover
            basket = {}

    fund_block = report.get("fund") or {}
    if fund:
        fund_block = {
            **fund_block,
            "symbol": fund,
            "units": int(sum(lot.units for lot in portfolio.lots(fund))),
            "value": round(sum(lot.units for lot in portfolio.lots(fund)) * marks.get(fund, 0.0), 2),
            "runway_days": basket.get("runway_days"),
            "checked": basket.get("fund_checked"),
        }

    return {
        "run_id": live.run_id,
        "as_of": report["as_of"],
        "rows": rows,
        "totals": report["totals"],
        "fund": fund_block or None,
        "today": {
            "spendable": preview.get("spendable", basket.get("settled_cash")),
            "projected": preview.get("projected", basket.get("settled_projected")),
            "settling": basket.get("pending_total", 0.0),
            "daily_budget": getattr(strategy, "daily_budget", None),
            "pots_total": round(sum(float(v) for v in pots.values()), 2),
            "plan": [{"symbol": s, "price": px, "units": u, "cost": c}
                     for s, px, u, c in preview.get("plan", [])],
            "plan_total": round(sum(c for _s, _p, _u, c in preview.get("plan", [])), 2),
            "affordable": [{"symbol": s, "price": px, "units": u, "cost": c}
                           for s, px, u, c in preview.get("affordable", [])],
            "affordable_total": round(sum(c for _s, _p, _u, c in preview.get("affordable", [])), 2),
            # why an empty plan is empty: no cash to pay, or no pot that affords a share
            "blocked_by": (
                None if preview.get("plan")
                else "cash" if preview.get("affordable") else "pots"
            ),
            "shopped_today": getattr(strategy, "last_shop_day", None) == today.isoformat(),
            "sizing": getattr(strategy, "sizing", None),
        },
        "cash": round(float(portfolio.cash), 2),
    }
