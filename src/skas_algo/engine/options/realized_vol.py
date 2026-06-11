"""Realized-volatility estimator for synthetic option pricing.

For an underlying with no traded options (e.g. MCX GOLD), implied volatility is not
observable, so we estimate volatility as **annualized rolling realized volatility** of
the underlying's daily log-returns. This is the vol input to a Black-Scholes synthetic
chain — the conceptual partner to ``black_scholes.implied_vol`` (which backs IV out of a
*real* premium). Realized ≠ implied, so synthetic premiums are model estimates, not
market prices.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Callable

import numpy as np
import pandas as pd


def realized_vol_series(closes: pd.Series, window: int = 20, min_periods: int = 10,
                        trading_days: int = 252, floor: float = 0.05) -> pd.Series:
    """Annualized rolling realized vol of a close series (forward-filled, floored)."""
    s = pd.Series(closes).astype(float)
    rets = np.log(s / s.shift(1))
    rv = rets.rolling(window, min_periods=min_periods).std() * math.sqrt(trading_days)
    return rv.ffill().clip(lower=floor)


def realized_vol_provider(closes_by_date: pd.Series, window: int = 20, min_periods: int = 10,
                          trading_days: int = 252, floor: float = 0.05) -> Callable[[date], float]:
    """Return ``vol_on(on_date) -> float``: the realized vol as of a date (forward-filled).

    ``closes_by_date`` is a close series indexed by date/Timestamp. Never returns NaN —
    falls back to the first available value or ``floor`` for dates before the series.
    """
    s = pd.Series(closes_by_date).sort_index()
    rv = realized_vol_series(s, window, min_periods, trading_days, floor)
    rv.index = s.index

    def vol_on(on_date: date) -> float:
        if len(rv) == 0:
            return floor
        ts = pd.Timestamp(on_date)
        upto = rv.loc[:ts]
        if len(upto):
            v = float(upto.iloc[-1])
            return v if not math.isnan(v) else floor
        valid = rv.dropna()
        return float(valid.iloc[0]) if len(valid) else floor

    return vol_on
