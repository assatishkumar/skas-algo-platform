"""SuperTrend Momentum: green flip → buy, +target → book 50%, red flip → exit the remainder."""

from __future__ import annotations

from datetime import date

import pandas as pd

from skas_algo.engine.runner import BacktestRunner
from skas_algo.strategies.supertrend_momentum import SuperTrendMomentumStrategy

# Flat (red) → strong rise (green flip + >5% run) → sharp crash (red flip).
CLOSES = [100, 100, 100, 103, 107, 112, 118, 125, 126, 124, 116, 104, 92, 80]


def _loader(symbol, start_date, end_date):
    dates = pd.bdate_range(start="2024-01-01", periods=len(CLOSES))
    df = pd.DataFrame({
        "date": dates,
        "open": [CLOSES[max(0, i - 1)] for i in range(len(CLOSES))],
        "high": [c + 0.5 for c in CLOSES],
        "low": [c - 0.5 for c in CLOSES],
        "close": [float(c) for c in CLOSES],
    })
    return df[(df["date"] >= pd.Timestamp(start_date)) & (df["date"] <= pd.Timestamp(end_date))].reset_index(drop=True)


def _run(partial_book_pct: float):
    strat = SuperTrendMomentumStrategy(
        universe=["AAA"], initial_capital=1_000_000, capital_parts=10,
        timeframe="daily", supertrend_period=3, supertrend_multiplier=2.0,
        profit_target=0.05, partial_book_pct=partial_book_pct,
    )
    runner = BacktestRunner(
        strategy=strat, universe=["AAA"], loader=_loader, initial_capital=1_000_000,
        lookback=2, tax_rate=0.0,
        supertrend={"period": 3, "multiplier": 2.0, "timeframe": "daily"},
    )
    return runner.run(date(2024, 1, 1), pd.bdate_range("2024-01-01", periods=len(CLOSES))[-1].date())


def test_green_entry_partial_book_then_red_exit():
    result = _run(partial_book_pct=0.5)
    buys = [t for t in result.transactions if t["action"] in ("BUY", "AVG_BUY")]
    books = [t for t in result.transactions if t["action"] == "SELL" and t["tag"] == "BOOK"]
    reds = [t for t in result.transactions if t["action"] == "SELL" and t["tag"] != "BOOK"]
    assert len(buys) == 1, [(t["action"], t["tag"]) for t in result.transactions]
    bought = buys[0]["units"]
    # Booked ~half at the +5% target, before the red exit.
    assert len(books) == 1 and books[0]["units"] == round(bought * 0.5)
    assert any(t.get("exit_reason") == "supertrend_red" for t in reds)
    # The whole position is eventually closed: partial + remainder == bought.
    assert books[0]["units"] + sum(t["units"] for t in reds) == bought
    assert books[0]["date"] < reds[0]["date"]


def test_full_exit_at_target_when_no_partial():
    # partial_book_pct = 1.0 → exit the WHOLE position at the % target (no riding to red).
    result = _run(partial_book_pct=1.0)
    buys = [t for t in result.transactions if t["action"] in ("BUY", "AVG_BUY")]
    sells = [t for t in result.transactions if t["action"] == "SELL"]
    assert len(buys) == 1
    assert any(t.get("exit_reason") == "target" for t in sells)
    assert sum(t["units"] for t in sells) == buys[0]["units"]


# Flat (red) → rise (green flip, peak 120) → dip (pullback) → breakout above 120 → later crash.
PB_CLOSES = [100, 100, 100, 103, 108, 114, 120, 116, 112, 122, 124, 118, 100, 90]


def _pb_loader(symbol, start_date, end_date):
    dates = pd.bdate_range(start="2024-01-01", periods=len(PB_CLOSES))
    df = pd.DataFrame({
        "date": dates, "open": [PB_CLOSES[max(0, i - 1)] for i in range(len(PB_CLOSES))],
        "high": [c + 0.5 for c in PB_CLOSES], "low": [c - 0.5 for c in PB_CLOSES],
        "close": [float(c) for c in PB_CLOSES],
    })
    return df[(df["date"] >= pd.Timestamp(start_date)) & (df["date"] <= pd.Timestamp(end_date))].reset_index(drop=True)


def _pb_run(entry_mode: str):
    strat = SuperTrendMomentumStrategy(
        universe=["AAA"], initial_capital=1_000_000, capital_parts=10,
        timeframe="daily", supertrend_period=3, supertrend_multiplier=2.0,
        profit_target=0.50, partial_book_pct=1.0, entry_mode=entry_mode, pullback_pct=0.0,
    )
    runner = BacktestRunner(
        strategy=strat, universe=["AAA"], loader=_pb_loader, initial_capital=1_000_000,
        lookback=2, tax_rate=0.0, supertrend={"period": 3, "multiplier": 2.0, "timeframe": "daily"},
    )
    return runner.run(date(2024, 1, 1), pd.bdate_range("2024-01-01", periods=len(PB_CLOSES))[-1].date())


def test_pullback_entry_delays_buy_until_breakout():
    flip_buy = [t for t in _pb_run("flip").transactions if t["action"] == "BUY"]
    pb_buy = [t for t in _pb_run("pullback").transactions if t["action"] == "BUY"]
    assert len(flip_buy) == 1 and len(pb_buy) == 1
    # Pullback mode waits for the dip + breakout, so it buys strictly later — and at the
    # breakout above the post-flip high (120), i.e. at the 122 close.
    assert pb_buy[0]["date"] > flip_buy[0]["date"]
    assert round(pb_buy[0]["price"]) == 122
