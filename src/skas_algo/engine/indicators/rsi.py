"""RSI (Wilder) + EMA — the daily close-series indicators for equity strategies.

Same contract as ``supertrend.py``: pure functions over a price Series, returning a
Series aligned to the input index, NaN until warmed up. Wilder smoothing == EWMA with
``alpha = 1/period`` (the exact convention ``atr()`` already uses), which matches the
TradingView RSI the gap_reversal spec was written against.
"""

from __future__ import annotations

import pandas as pd


def ema(close: pd.Series, span: int) -> pd.Series:
    """Standard EMA of a close series (``span`` bars, TradingView convention)."""
    return close.ewm(span=int(span), adjust=False, min_periods=int(span)).mean()


def rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder RSI of a close series: 0-100, NaN for the first ``period`` bars.

    RSI = 100 − 100/(1+RS), RS = RMA(gains)/RMA(losses). A flat/all-gain window divides
    by zero → RS=inf → RSI 100 (pandas handles this without raising); all-loss → 0.
    """
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=int(period)).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=int(period)).mean()
    rs = avg_gain / avg_loss
    out = 100.0 - 100.0 / (1.0 + rs)
    # avg_loss == 0 → rs = inf → the formula already yields 100.0; keep NaN warmup intact.
    return out
