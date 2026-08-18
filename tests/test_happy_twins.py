"""happy_twins: the dual-SuperTrend precompute kind, transition entries, state exits."""

from __future__ import annotations

import pandas as pd

from skas_algo.engine.market import MarketView
from skas_algo.strategies.happy_twins import HappyTwinsStrategy


def _frame(closes):
    dates = pd.bdate_range("2024-01-01", periods=len(closes))
    return pd.DataFrame({"date": dates, "open": closes, "high": [c * 1.01 for c in closes],
                         "low": [c * 0.99 for c in closes], "close": closes})


def test_marketview_computes_named_supertrend_twins():
    """Two 'kind: supertrend' entries in one indicator config → two independent daily
    series, readable by name — the plumbing Happy Twins rides."""
    closes = [100 + i for i in range(30)] + [130 - 2 * i for i in range(15)]
    view = MarketView(lookback=5, indicators={
        "st_fast": {"kind": "supertrend", "period": 2, "multiplier": 1.0, "timeframe": "daily"},
        "st_slow": {"kind": "supertrend", "period": 10, "multiplier": 3.0, "timeframe": "daily"},
    })
    view.add_symbol("AAA", _frame(closes))
    view.finalize()
    view.set_date(view.unified_dates[-1])
    fast, slow = view.indicator("AAA", "st_fast"), view.indicator("AAA", "st_slow")
    assert fast == -1.0                      # sharp downtrend flipped the tight ST
    assert slow in (1.0, -1.0) and slow != fast or slow == fast  # both exist
    view.set_date(view.unified_dates[25])
    assert view.indicator("AAA", "st_fast") == 1.0    # mid-uptrend: green


class _Ctx:
    def __init__(self, universe):
        self.fast: dict[str, float | None] = {}
        self.slow: dict[str, float | None] = {}
        self.price = 100.0
        self.cash = 1_000_000.0
        self.positions: set[str] = set()
        self._u = universe

    def present_symbols(self):
        return list(self._u)

    def lot_symbols(self):
        return sorted(self.positions)

    def indicator(self, sym, name):
        return (self.fast if name == "st_fast" else self.slow).get(sym)

    def equity(self):
        return 1_000_000.0

    def close(self, sym):
        return self.price


def _tick(st, ctx):
    sigs = st.on_slice(ctx)
    for s in sigs:
        if s.action.name == "ENTER_LONG":
            ctx.positions.add(s.symbol)
        else:
            ctx.positions.discard(s.symbol)
    return sigs


def test_enters_on_the_green_FLIP_not_on_green_state():
    st = HappyTwinsStrategy(universe=["AAA"])
    ctx = _Ctx(["AAA"])
    ctx.fast["AAA"], ctx.slow["AAA"] = 1.0, 1.0
    assert _tick(st, ctx) == []              # green from the start: state, not a flip
    ctx.fast["AAA"] = -1.0
    assert _tick(st, ctx) == []              # now red — arms the transition
    ctx.fast["AAA"] = 1.0
    sigs = _tick(st, ctx)                    # red → green: THE flip
    assert len(sigs) == 1 and sigs[0].action.name == "ENTER_LONG"
    assert sigs[0].quantity == 10_000        # equity/1 symbol // price


def test_exits_while_slow_is_red_even_without_seeing_the_flip():
    """State-based exit: a restart that missed the red flip still closes next slice."""
    st = HappyTwinsStrategy(universe=["AAA"])
    ctx = _Ctx(["AAA"])
    ctx.positions.add("AAA")                 # recovered holding, no prev state at all
    ctx.fast["AAA"], ctx.slow["AAA"] = -1.0, -1.0
    sigs = _tick(st, ctx)
    assert [s.action.name for s in sigs] == ["EXIT_ALL"]


def test_reentry_after_exit_needs_a_fresh_flip_and_gets_one():
    st = HappyTwinsStrategy(universe=["AAA"])
    ctx = _Ctx(["AAA"])
    ctx.positions.add("AAA")
    ctx.fast["AAA"], ctx.slow["AAA"] = -1.0, -1.0
    _tick(st, ctx)                            # exited; prev_fast = -1 recorded
    ctx.fast["AAA"], ctx.slow["AAA"] = 1.0, 1.0
    sigs = _tick(st, ctx)                     # red → green next slice = a real flip
    assert [s.action.name for s in sigs] == ["ENTER_LONG"]


def test_state_round_trip_preserves_the_transition_memory():
    st = HappyTwinsStrategy(universe=["AAA"])
    ctx = _Ctx(["AAA"])
    ctx.fast["AAA"], ctx.slow["AAA"] = -1.0, 1.0
    _tick(st, ctx)                            # records prev_fast = -1
    fresh = HappyTwinsStrategy(universe=["AAA"])
    fresh.load_state(st.export_state())
    ctx.fast["AAA"] = 1.0
    sigs = fresh.on_slice(ctx)                # flip visible ACROSS the restart
    assert [s.action.name for s in sigs] == ["ENTER_LONG"]
