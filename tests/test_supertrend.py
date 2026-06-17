"""SuperTrend indicator: flips green in an uptrend and red on a sharp drop; D/W/M mapping."""

from __future__ import annotations

import pandas as pd

from skas_algo.engine.indicators.supertrend import atr, supertrend_bands, supertrend_direction


def _ohlc(closes: list[float], start="2024-01-01") -> pd.DataFrame:
    dates = pd.bdate_range(start=start, periods=len(closes))
    return pd.DataFrame({
        "date": dates,
        "open": [closes[max(0, i - 1)] for i in range(len(closes))],
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
    })


# Steady rise (→ green) then a sharp crash (→ red).
CLOSES = [100 + 2 * i for i in range(12)] + [122 - 5 * i for i in range(1, 13)]


def test_atr_is_positive_after_warmup():
    a = atr(_ohlc(CLOSES).set_index("date"), period=3)
    assert a.iloc[:2].isna().all()  # warmup (period-1 NaNs from ewm min_periods)
    assert (a.dropna() > 0).all()


def test_supertrend_flips_green_then_red():
    d = supertrend_direction(_ohlc(CLOSES), period=3, multiplier=2.0, timeframe="daily")
    vals = d.dropna()
    assert (vals == 1).any() and (vals == -1).any()  # both regimes occur
    assert vals.iloc[-1] == -1                        # ends red after the crash
    # A green flip (−1→+1) happens before the red flip (+1→−1).
    seq = vals.tolist()
    green = next(i for i in range(1, len(seq)) if seq[i - 1] == -1 and seq[i] == 1)
    red = next(i for i in range(green + 1, len(seq)) if seq[i - 1] == 1 and seq[i] == -1)
    assert green < red


def test_supertrend_bands_line_sits_on_correct_side():
    b = supertrend_bands(_ohlc(CLOSES), period=3, multiplier=2.0, timeframe="daily")
    assert list(b.columns) == ["close", "supertrend", "direction"]
    valid = b.dropna(subset=["direction", "supertrend"])
    assert len(valid) > 0
    # In an uptrend the SuperTrend line is support BELOW price; in a downtrend it's above.
    up = valid[valid["direction"] == 1]
    down = valid[valid["direction"] == -1]
    assert (up["supertrend"] <= up["close"] + 1e-6).all()
    assert (down["supertrend"] >= down["close"] - 1e-6).all()
    # Direction agrees with supertrend_direction().
    d = supertrend_direction(_ohlc(CLOSES), period=3, multiplier=2.0, timeframe="daily")
    assert (b["direction"].dropna() == d.dropna()).all()


def test_weekly_direction_maps_to_daily_index_forward_filled():
    df = _ohlc(CLOSES)
    d = supertrend_direction(df, period=2, multiplier=2.0, timeframe="weekly")
    # One value per daily row, forward-filled (no gaps once weekly bars start printing).
    assert len(d) == len(df)
    assert d.dropna().isin([1.0, -1.0]).all()


def test_monthly_direction_runs():
    # Longer daily series so a couple of monthly bars form.
    closes = [100 + i for i in range(80)] + [180 - 3 * i for i in range(1, 40)]
    d = supertrend_direction(_ohlc(closes), period=2, multiplier=2.0, timeframe="monthly")
    assert len(d) == len(closes)
    assert d.dropna().isin([1.0, -1.0]).all()
