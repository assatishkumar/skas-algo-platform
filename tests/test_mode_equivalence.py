"""Mode equivalence: replaying history through the LiveSession (PAPER) reproduces the
BacktestRunner trade-for-trade. This is the core guarantee of the platform — the same
SliceExecutor drives both, so "what you backtest is what you forward-test".
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from skas_algo.engine.live import LiveSession
from skas_algo.engine.overrides import OverrideRule
from skas_algo.engine.runner import BacktestRunner
from skas_algo.strategies.sst_fifo import SSTFifoStrategy
from skas_algo.strategies.sst_lifo import SSTLifoStrategy


def _make_frames(seed: int = 7, n: int = 320, syms=("AAA", "BBB", "CCC")) -> dict:
    """Deterministic pseudo-random price frames that generate plenty of trades."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2019-01-01", periods=n)
    frames = {}
    for i, s in enumerate(syms):
        steps = rng.normal(0.0006, 0.02, n)
        price = 100 * (1 + 0.15 * i) * np.cumprod(1 + steps)
        price = np.round(price, 2).astype("float64")
        frames[s] = pd.DataFrame(
            {
                "date": dates,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 1000,
            }
        )
    return frames


def _replay_live(session: LiveSession, frames: dict) -> None:
    series = {}
    all_dates: set[pd.Timestamp] = set()
    for sym, df in frames.items():
        d = pd.to_datetime(df["date"])
        series[sym] = dict(zip(d, df["close"], strict=True))
        all_dates.update(d)
    dates = sorted(all_dates)
    session.warmup({sym: [] for sym in frames})  # establish symbol order; history grows via end_day
    for ts in dates:
        session.update_quotes({s: series[s][ts] for s in frames if ts in series[s]})
        session.run_decision(ts)
        session.end_day()
    session.finalize(dates[-1])


def _backtest(strategy, frames, **kw):
    runner = BacktestRunner(
        strategy=strategy,
        universe=list(frames),
        loader=lambda sym, a, b: frames[sym],
        lookback=20,
        tax_rate=0.20,
        **kw,
    )
    return runner.run(date(2019, 1, 1), date(2030, 1, 1))


def _assert_same_trades(bt_txns, live_txns):
    assert len(bt_txns) > 5, "test data should produce trades"
    assert len(bt_txns) == len(live_txns)
    for b, v in zip(bt_txns, live_txns, strict=True):
        assert b["date"] == v["date"]
        assert b["ticker"] == v["ticker"]
        assert b["action"] == v["action"]
        assert b["units"] == v["units"]
        assert b["price"] == v["price"]
        assert b["profit"] == v["profit"]
        assert b["tag"] == v["tag"]


def test_lifo_live_matches_backtest():
    frames = _make_frames()
    bt = _backtest(SSTLifoStrategy(list(frames), capital_parts=10, profit_target=0.06), frames)
    live = LiveSession(
        SSTLifoStrategy(list(frames), capital_parts=10, profit_target=0.06),
        initial_capital=2_500_000,
        lookback=20,
        tax_rate=0.20,
    )
    _replay_live(live, frames)
    _assert_same_trades(bt.transactions, live.transactions)
    assert bt.history[-1]["total_equity"] == live.history[-1]["total_equity"]


def test_fifo_live_matches_backtest():
    frames = _make_frames(seed=11)
    bt = _backtest(SSTFifoStrategy(list(frames), capital_parts=10), frames)
    live = LiveSession(
        SSTFifoStrategy(list(frames), capital_parts=10),
        initial_capital=2_500_000,
        lookback=20,
        tax_rate=0.20,
    )
    _replay_live(live, frames)
    _assert_same_trades(bt.transactions, live.transactions)


def test_live_matches_backtest_with_override():
    frames = _make_frames(seed=3)
    rule = {
        "exit": [
            {"at_pct": 6, "action": "book", "qty_pct": 50},
            {"action": "trail_sl", "trail_pct": 3},
        ]
    }
    ov = [OverrideRule(scope="ALGO", target=None, rule=rule)]
    bt = _backtest(
        SSTLifoStrategy(list(frames), capital_parts=10, profit_target=0.06), frames, overrides=ov
    )
    live = LiveSession(
        SSTLifoStrategy(list(frames), capital_parts=10, profit_target=0.06),
        initial_capital=2_500_000,
        lookback=20,
        tax_rate=0.20,
        overrides=ov,
    )
    _replay_live(live, frames)
    _assert_same_trades(bt.transactions, live.transactions)
    # The override path actually fired (booked partials + trailing exits).
    assert any(t["tag"] == "BOOK" for t in live.transactions)
    assert any(t["tag"] == "TRAIL" for t in live.transactions)
