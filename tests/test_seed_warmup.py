"""Seed-replay indicator warmup: replaying from a recent `warm_from_date` must warm Donchian/
SuperTrend over prior history (driving the strategy's state through the buffer) so entries match a
full-history backtest — not cold-start. `warmup_days=0` must be byte-identical to before (parity)."""

from __future__ import annotations

from datetime import date

import pandas as pd

from skas_algo.engine.runner import BacktestRunner
from skas_algo.strategies.sst_lifo import SSTLifoStrategy

# A 5-day low at idx5 (arms tracking), then a breakout above the 5-day high (=100) at idx9
# (close 103) → BUY. The idx5 low is the key: a seed that starts after it but before the
# breakout must still observe it (via warmup) to arm tracking.
#  idx: 0   1   2   3   4    5   6   7    8    9    10 …
_CLOSES = [100, 100, 100, 100, 100, 90, 96, 98, 100, 103, 112, 112, 112, 112, 112, 112, 112, 112]
_DATES = pd.bdate_range("2024-01-01", periods=len(_CLOSES))
_FRAME = pd.DataFrame({"date": _DATES, "open": _CLOSES, "high": _CLOSES, "low": _CLOSES,
                       "close": _CLOSES, "volume": 1})
_BREAKOUT = _DATES[9].date().isoformat()  # 2024-01-12


def _loader(_sym, start, end):
    """Respects the requested window (so a cold seed really gets a truncated frame)."""
    d = _FRAME["date"].dt.date
    return _FRAME[(d >= start) & (d <= end)].reset_index(drop=True)


def _run(start: date, warmup: int):
    strat = SSTLifoStrategy(universe=["AAA"], initial_capital=100_000, capital_parts=1, profit_target=10.0)
    runner = BacktestRunner(strategy=strat, universe=["AAA"], loader=_loader,
                            initial_capital=100_000, lookback=5, tax_rate=0.0)
    return runner.run(start, date(2024, 12, 31), warmup_days=warmup)


def _buy_dates(res):
    return [str(t["date"])[:10] for t in res.transactions if t["action"] == "BUY"]


def test_warmup_seed_matches_full_history_and_beats_cold_start():
    # Full history (trades from idx0, fully warm) — the reference: enters on the breakout.
    full = _buy_dates(_run(date(2024, 1, 1), 0))
    assert full == [_BREAKOUT]

    # Seed from idx7 (AFTER the idx5 low, BEFORE the idx10 breakout):
    seed_start = _DATES[7].date()
    cold = _buy_dates(_run(seed_start, 0))        # no warmup → misses the low → no/late entry
    warm = _buy_dates(_run(seed_start, 30))       # warms over prior bars → sees the low → arms

    assert warm == full          # warmup reproduces the full-history entry
    assert cold != full          # cold-start does NOT (the bug)
    assert _BREAKOUT not in cold  # specifically, cold never makes the correct breakout entry


def test_warmup_zero_is_unchanged():
    # warmup_days=0 from a date with full prior data in-frame == the pre-fix behaviour.
    a = _buy_dates(_run(date(2024, 1, 1), 0))
    b = _buy_dates(_run(date(2024, 1, 1), 0))
    assert a == b == [_BREAKOUT]
