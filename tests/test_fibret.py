"""FibRet screener math: swing detection, fib levels, and a row build against a faked chain."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from skas_algo.services.fibret import (
    FibParams,
    Swing,
    analyze_symbol,
    detect_swing,
    fib_levels,
)


def _df(rows: list[tuple[str, float, float, float]]) -> pd.DataFrame:
    # rows: (date, high, low, close)
    return pd.DataFrame(
        [{"date": d, "open": c, "high": h, "low": lo, "close": c} for (d, h, lo, c) in rows]
    )


def _ramp(highs_lows: list[tuple[float, float]], start="2026-01-01") -> pd.DataFrame:
    d0 = date.fromisoformat(start)
    return _df([
        ((d0 + timedelta(days=i)).isoformat(), h, lo, (h + lo) / 2)
        for i, (h, lo) in enumerate(highs_lows)
    ])


def test_detect_swing_down_leg_sells_call():
    # High (1000) early, low (900) most recent → down-leg → sell CALL.
    df = _ramp([(960, 940), (1000, 970), (980, 950), (950, 920), (940, 900)])
    sw = detect_swing(df, lookback=60)
    assert sw is not None
    assert sw.side == "CE"
    assert sw.high == 1000 and sw.low == 900
    assert sw.range == 100


def test_detect_swing_up_leg_sells_put():
    # Low (900) early, high (1000) most recent → up-leg → sell PUT.
    df = _ramp([(940, 900), (950, 920), (980, 950), (1000, 970), (995, 960)])
    sw = detect_swing(df, lookback=60)
    assert sw is not None
    assert sw.side == "PE"
    assert sw.high == 1000 and sw.low == 900


def test_fib_levels_match_confirmed_example():
    call = Swing(1000, "d", 900, "d", "CE")
    entry, stop = fib_levels(call, 1.618, 0.786)
    assert round(entry, 1) == 1061.8  # L + 1.618·R, above the high
    assert round(stop, 1) == 978.6    # L + 0.786·R, between spot and strike

    put = Swing(1000, "d", 900, "d", "PE")
    entry, stop = fib_levels(put, 1.618, 0.786)
    assert round(entry, 1) == 838.2   # H − 1.618·R, below the low
    assert round(stop, 1) == 921.4


def test_analyze_symbol_builds_short_call_row():
    df = _ramp([(960, 940), (1000, 970), (980, 950), (950, 920), (940, 900)])
    chain = {
        "spot": 910.0,
        "lot_size": 400,
        "rows": [
            {"strike": 1040.0, "ce": {"ltp": 8.0, "oi": 1200}, "pe": {"ltp": 130.0, "oi": 500}},
            {"strike": 1060.0, "ce": {"ltp": 5.0, "oi": 900}, "pe": {"ltp": 150.0, "oi": 400}},
            {"strike": 1080.0, "ce": {"ltp": 3.0, "oi": 300}, "pe": {"ltp": 170.0, "oi": 200}},
        ],
    }
    on = date(2026, 6, 22)
    row = analyze_symbol(
        symbol="ACME", df=df, chain=chain, expiry=on + timedelta(days=30), on_date=on,
        params=FibParams(min_oi=500),
    )
    assert row["error"] is None
    assert row["side"] == "CE"
    assert row["strike"] == 1060.0          # nearest to entry level 1061.8
    assert row["premium"] == 5.0
    assert row["breakeven"] == 1065.0       # strike + premium (short call)
    assert row["max_profit"] == 5.0 * 400   # premium · lot · lots
    assert row["est_stop_loss"] > 0         # option richens as spot rises toward the stop
    assert row["reward_risk"] is not None and row["reward_risk"] > 0
    assert row["margin"] is not None
    assert row["liquid"] is True            # oi 900 ≥ min_oi 500
    assert round(row["stop_level"], 1) == 978.6


def test_analyze_symbol_no_swing():
    flat = _df([("2026-01-01", 100, 100, 100)])
    row = analyze_symbol(
        symbol="FLAT", df=flat, chain={"spot": 100, "lot_size": 50, "rows": []},
        expiry=date(2026, 7, 1), on_date=date(2026, 6, 22), params=FibParams(),
    )
    assert row["error"] == "no swing in price history"
