"""SuperTrend indicator (and ATR) from OHLC, with optional weekly/monthly resampling.

SuperTrend is an ATR-banded trend filter: when price closes above the trailing band it is in
an uptrend ("green", direction +1); below, a downtrend ("red", direction −1). The band only
flips on a *bar close*, so a daily SuperTrend changes on the daily bar, a weekly one on the
weekly bar, etc.

``supertrend_direction`` returns the direction of the most-recent COMPLETED bar at the chosen
timeframe, forward-filled onto the daily date index — so a weekly/monthly flip becomes visible
on the first trading day after the bar closes (no lookahead). Strategies read it via
``ctx.supertrend_dir(symbol)``; the timeframe is baked into the precomputed series.
"""

from __future__ import annotations

import pandas as pd

# Resample rules per timeframe (None = keep daily bars). W-FRI = weeks ending Friday.
_RESAMPLE = {"daily": None, "weekly": "W-FRI", "monthly": "ME"}


def _as_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    """Return a date-indexed OHLC frame (sorted), accepting a 'date' column or a date index."""
    out = df.copy()
    if "date" in out.columns:
        out["date"] = pd.to_datetime(out["date"])
        out = out.set_index("date")
    else:
        out.index = pd.to_datetime(out.index)
    return out.sort_index()


def atr(ohlc: pd.DataFrame, period: int) -> pd.Series:
    """Wilder's ATR over ``period`` bars (RMA of the true range)."""
    high, low, close = ohlc["high"], ohlc["low"], ohlc["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    # Wilder smoothing == EWMA with alpha = 1/period.
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def _supertrend_bars(bars: pd.DataFrame, period: int, multiplier: float) -> pd.DataFrame:
    """Per-bar SuperTrend: ``direction`` (+1 up / −1 down) and the trailing ``supertrend`` line
    (the active band — support below price in an uptrend, resistance above in a downtrend).
    Both NaN until ATR is defined."""
    out = pd.DataFrame(index=bars.index, columns=["direction", "supertrend"], dtype=float)
    if bars.empty:
        return out
    hl2 = (bars["high"] + bars["low"]) / 2.0
    band = multiplier * atr(bars, period)
    fu = (hl2 + band).to_numpy(copy=True)
    fl = (hl2 - band).to_numpy(copy=True)
    c = bars["close"].to_numpy()
    n = len(bars)
    direction = [float("nan")] * n
    line = [float("nan")] * n
    started = False
    prev_dir = -1  # seeded when the first valid ATR bar appears
    for i in range(n):
        if i > 0:
            # Carry-forward the final bands (the classic SuperTrend rule).
            if not (pd.isna(fu[i])) and not (pd.isna(fu[i - 1])):
                if not (fu[i] < fu[i - 1] or c[i - 1] > fu[i - 1]):
                    fu[i] = fu[i - 1]
                if not (fl[i] > fl[i - 1] or c[i - 1] < fl[i - 1]):
                    fl[i] = fl[i - 1]
        if pd.isna(band.iloc[i]):
            continue  # ATR not warmed up yet
        if not started:
            prev_dir = 1 if c[i] > fu[i] else -1
            started = True
        else:
            if prev_dir == -1 and c[i] > fu[i]:
                prev_dir = 1
            elif prev_dir == 1 and c[i] < fl[i]:
                prev_dir = -1
        direction[i] = float(prev_dir)
        line[i] = fl[i] if prev_dir == 1 else fu[i]
    out["direction"] = direction
    out["supertrend"] = line
    return out


def _bars_for(daily: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Resample daily OHLC to the timeframe (daily = unchanged)."""
    rule = _RESAMPLE.get(str(timeframe).lower())
    if rule is None:
        return daily
    return daily.resample(rule).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna(subset=["high", "low", "close"])


def supertrend_direction(
    df: pd.DataFrame, period: int = 10, multiplier: float = 3.0, timeframe: str = "daily"
) -> pd.Series:
    """SuperTrend direction (+1 green / −1 red) of the latest COMPLETED bar at ``timeframe``,
    forward-filled onto the daily date index of ``df``.

    ``df`` is daily OHLC ('date'/'open'/'high'/'low'/'close'). For weekly/monthly it is resampled
    (OHLC) before computing, then mapped back to the daily dates so a strategy reading it daily
    sees the flip the first trading day on/after the bar closes.
    """
    daily = _as_ohlc(df)
    bars = _bars_for(daily, timeframe)
    bar_dir = _supertrend_bars(bars, period, multiplier)["direction"]
    if bars is daily:
        return bar_dir
    return bar_dir.reindex(daily.index, method="ffill")


def supertrend_bands(
    df: pd.DataFrame, period: int = 10, multiplier: float = 3.0, timeframe: str = "daily"
) -> pd.DataFrame:
    """For charting: a daily-indexed frame with ``close`` (daily), the ``supertrend`` line and
    ``direction`` at ``timeframe`` (weekly/monthly forward-filled to daily, so the line is a step
    that flips on the bar close — no lookahead). Mirrors ``supertrend_direction``'s mapping."""
    daily = _as_ohlc(df)
    bars = _supertrend_bars(_bars_for(daily, timeframe), period, multiplier)
    if str(timeframe).lower() not in _RESAMPLE or _RESAMPLE[str(timeframe).lower()] is None:
        out = bars.copy()
    else:
        out = bars.reindex(daily.index, method="ffill")
    out["close"] = daily["close"]
    return out[["close", "supertrend", "direction"]]
