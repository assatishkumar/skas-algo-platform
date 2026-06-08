"""Deterministic engine tests on synthetic data (no external cache needed)."""

from __future__ import annotations

from datetime import date

import pandas as pd

from skas_algo.engine.metrics import compute_metrics
from skas_algo.engine.portfolio import Portfolio
from skas_algo.engine.runner import BacktestRunner
from skas_algo.strategies.sst_lifo import SSTLifoStrategy


def test_mark_prices_forward_fill():
    """A held symbol that doesn't print today is marked at its last known close,
    not dropped to zero (fixes the Muhurat/special-session drawdown artifact)."""
    from skas_algo.engine.market import MarketView

    view = MarketView(lookback=2)
    d = pd.to_datetime(["2020-01-01", "2020-01-02", "2020-01-03"])
    view.add_symbol(
        "AAA",
        pd.DataFrame(
            {
                "date": d,
                "open": [10, 11, 12],
                "high": [10, 11, 12],
                "low": [10, 11, 12],
                "close": [10.0, 11.0, 12.0],
                "volume": [1, 1, 1],
            }
        ),
    )
    # BBB is missing the middle day (a sparse/special session).
    view.add_symbol(
        "BBB",
        pd.DataFrame(
            {
                "date": [d[0], d[2]],
                "open": [20, 22],
                "high": [20, 22],
                "low": [20, 22],
                "close": [20.0, 22.0],
                "volume": [1, 1],
            }
        ),
    )
    view.finalize()

    view.set_date(d[0])
    assert view.mark_prices() == {"AAA": 10.0, "BBB": 20.0}
    view.set_date(d[1])  # BBB absent today
    assert view.mark_prices() == {"AAA": 11.0, "BBB": 20.0}  # BBB carried forward
    assert "BBB" not in view.closes_today()  # but no real print today
    view.set_date(d[2])
    assert view.mark_prices() == {"AAA": 12.0, "BBB": 22.0}


def test_equity_scaled_allocation_tracks_equity():
    """equity_scaled sizes each lot off current equity; fixed stays at initial/parts."""
    from skas_algo.engine.context import AlgoContext
    from skas_algo.engine.market import MarketView

    pf = Portfolio(cash=200_000)  # account has grown from 100k to 200k
    ctx = AlgoContext(None, {}, pf, MarketView(lookback=1))
    fixed = SSTLifoStrategy(
        ["AAA"], initial_capital=100_000, capital_parts=10, allocation_mode="fixed"
    )
    scaled = SSTLifoStrategy(
        ["AAA"], initial_capital=100_000, capital_parts=10, allocation_mode="equity_scaled"
    )
    assert fixed._allocation(ctx) == 10_000  # initial_capital / parts (constant)
    assert scaled._allocation(ctx) == 20_000  # current equity / parts (grows)


def test_portfolio_buy_close_and_flush():
    p = Portfolio(cash=1000.0)
    lot = p.buy("X", units=10, price=10.0, when=date(2020, 1, 1))
    assert p.cash == 900.0
    assert p.units("X") == 10

    profit = p.close_lot("X", lot.id, price=12.0)
    assert profit == 20.0  # 10 * (12 - 10)
    assert p.cash == 1020.0
    assert p.month_realized == 20.0
    assert p.lots("X") == []

    flush = p.flush_month(tax_rate=0.20, withdrawal_rate=0.0)
    assert flush is not None
    assert flush.tax == 4.0
    assert p.cash == 1016.0
    assert p.total_taxes == 4.0


def _ramp(start: str, n: int, base: float, step: float) -> pd.DataFrame:
    dates = pd.bdate_range(start=start, periods=n)
    closes = [base + step * i for i in range(n)]
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1000] * n,
        }
    )


def test_sst_lifo_enters_on_breakout_and_exits_on_target():
    # A dip below the 20-day low then a steady ramp up -> one entry, later an exit.
    frames = {"AAA": _build_dip_then_ramp()}

    def loader(sym, s, e):
        return frames[sym]

    strat = SSTLifoStrategy(
        universe=["AAA"], initial_capital=100_000, capital_parts=10, profit_target=0.06
    )
    runner = BacktestRunner(
        strategy=strat,
        universe=["AAA"],
        loader=loader,
        initial_capital=100_000,
        lookback=20,
        tax_rate=0.0,
    )
    result = runner.run(date(2020, 1, 1), date(2021, 12, 31))

    actions = [t["action"] for t in result.transactions]
    assert "BUY" in actions
    assert "SELL" in actions

    metrics = compute_metrics(result, 100_000)
    assert metrics["Total Trades"] >= 1
    # Profit target is 6%; every realized trade should be a win in this monotone ramp.
    assert metrics["Win Rate %"] == 100.0


def _build_dip_then_ramp() -> pd.DataFrame:
    # 25 flat days at 100, one dip to 90 (new 20-day low), then a ramp that breaks
    # the 20-day high and keeps climbing past +6%.
    dates = pd.bdate_range(start="2020-01-01", periods=80)
    closes = [100.0] * 25 + [90.0] + [100.0] * 5
    price = 101.0
    while len(closes) < len(dates):
        closes.append(price)
        price += 1.5
    closes = closes[: len(dates)]
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1000] * len(dates),
        }
    )
