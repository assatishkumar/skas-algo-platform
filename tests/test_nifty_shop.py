"""Nifty_Shop: 20-DMA selection, Case-1 new buys, +target exit, Case-2 average-down."""

from __future__ import annotations

import pandas as pd

from skas_algo.engine.context import AlgoContext
from skas_algo.engine.market import MarketView
from skas_algo.engine.portfolio import Portfolio
from skas_algo.engine.types import SignalAction
from skas_algo.strategies.nifty_shop import NiftyShopStrategy

DATES = pd.to_datetime(["2020-01-01", "2020-01-02", "2020-01-03", "2020-01-06"])


def _view(closes_today: dict[str, float], base: float = 100.0) -> MarketView:
    """MarketView (3-DMA) where each symbol sits flat at ``base`` for 3 days, then prints
    ``closes_today`` on the 4th — so the 3-DMA = base and belowness = (base-close)/base."""
    view = MarketView(lookback=3)
    for sym, last in closes_today.items():
        view.add_symbol(sym, pd.DataFrame({"date": DATES, "close": [base, base, base, last]}))
    view.finalize()
    view.set_date(DATES[-1])
    return view


def _ctx(view: MarketView, cash: float) -> tuple[AlgoContext, Portfolio]:
    pf = Portfolio(cash=cash)
    return AlgoContext(None, {}, pf, view), pf


def test_case1_buys_two_most_below_dma():
    view = _view({"AAA": 90, "BBB": 92, "CCC": 94, "DDD": 96, "EEE": 98, "FFF": 110})
    ctx, _pf = _ctx(view, cash=1_000_000)
    strat = NiftyShopStrategy(universe=list("ABCDEF"), initial_capital=1_000_000,
                              allocation_pct=0.04, new_buys_per_day=2)
    buys = [(s.symbol, s.quantity) for s in strat.on_slice(ctx)
            if s.action is SignalAction.ENTER_LONG]
    # Buys the two MOST-below-DMA names (AAA, BBB); FFF is above its DMA → never a candidate.
    assert [b[0] for b in buys] == ["AAA", "BBB"]
    assert buys[0][1] == 40_000 // 90 and buys[1][1] == 40_000 // 92  # same ₹ (4%×10L), not qty


def test_exit_at_profit_target():
    view = _view({"AAA": 106})  # +6% vs the 100 entry, above the 5% target
    ctx, pf = _ctx(view, cash=1_000_000)
    pf.buy("AAA", 100, 100.0, DATES[-1])
    strat = NiftyShopStrategy(universe=["AAA"], initial_capital=1_000_000, profit_target=0.05)
    sigs = strat.on_slice(ctx)
    assert any(s.symbol == "AAA" and s.action is SignalAction.EXIT_ALL for s in sigs)


def test_case2_averages_worst_performer_only():
    # All 5 candidates already held; AAA (−6%) and BBB (−4%) are >3% below their last entry,
    # so one averaging trade fires on the worst (AAA). EEE at exactly −3% does NOT qualify.
    view = _view({"AAA": 94, "BBB": 96, "CCC": 98, "DDD": 99, "EEE": 97})
    ctx, pf = _ctx(view, cash=1_000_000)
    for sym in ("AAA", "BBB", "CCC", "DDD", "EEE"):
        pf.buy(sym, 100, 100.0, DATES[-1])  # last entry = 100 for each
    strat = NiftyShopStrategy(universe=list("ABCDE"), initial_capital=1_000_000,
                              avg_down_pct=0.03, max_avg_per_day=1)
    buys = [s for s in strat.on_slice(ctx) if s.action is SignalAction.ENTER_LONG]
    assert len(buys) == 1 and buys[0].symbol == "AAA" and buys[0].quantity > 0
