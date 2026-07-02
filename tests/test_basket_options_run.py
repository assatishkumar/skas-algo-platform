"""Basket options run: per-symbol loader routing (BS stocks / real NIFTY), contract-life
clamp, strike-step bands, day_range + index_spot on the market view, stock lot table."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd
import pytest

from skas_algo.data.basket_options import (
    build_basket_options_run,
    stock_strike_step,
)
from skas_algo.engine.options import black_scholes as bs
from skas_algo.engine.options.contract_specs import lot_size_for
from skas_algo.engine.options.realized_vol import realized_vol_provider


def _weekdays(start: date, n: int) -> list[date]:
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


CAL = _weekdays(date(2024, 3, 1), 25)


class FakeSD:
    """Two stocks + the NIFTY 50 index + a stub real NIFTY option series."""

    def __init__(self):
        n = len(CAL)
        self.frames = {
            "RELIANCE": pd.DataFrame({
                "date": CAL,
                "high": [1010 + 20 * math.sin(i / 2) + 10 for i in range(n)],
                "low": [990 + 20 * math.sin(i / 2) - 10 for i in range(n)],
                "close": [1000 + 20 * math.sin(i / 2) for i in range(n)],
            }),
            "NIFTY 50": pd.DataFrame({
                "date": CAL,
                "high": [22050.0] * n, "low": [21950.0] * n, "close": [22000.0] * n,
            }),
        }
        self.option_series_calls: list[tuple] = []

    def get_prices(self, symbol, start_date=None, end_date=None, asset_type="stock"):
        df = self.frames.get(symbol)
        if df is None:
            return None
        if start_date is not None:
            df = df[df["date"] >= start_date]
        if end_date is not None:
            df = df[df["date"] <= end_date]
        return df.reset_index(drop=True)

    def get_option_series(self, underlying, expiry, strike, right, start_date=None, end_date=None):
        self.option_series_calls.append((underlying, expiry, strike, right))
        return pd.DataFrame({"trade_date": CAL[:5], "close": [100.0] * 5})

    def get_option_chain(self, underlying, on_date):
        return None  # not needed by these tests


def test_stock_strike_step_bands():
    assert stock_strike_step(40) == 1.0
    assert stock_strike_step(80) == 2.5
    assert stock_strike_step(200) == 5.0
    assert stock_strike_step(400) == 10.0
    assert stock_strike_step(800) == 20.0
    assert stock_strike_step(1800) == 50.0
    assert stock_strike_step(4000) == 100.0
    assert stock_strike_step(9000) == 250.0


def test_loader_routes_stock_to_bs_and_nifty_to_real():
    sd = FakeSD()
    mv, _chain, _settler, _margin = build_basket_options_run(
        sd, ["RELIANCE"], CAL[0], CAL[-1], vol_multiplier=1.2)
    mv.set_date(pd.Timestamp(CAL[10]))

    expiry = CAL[-1]
    sym = f"RELIANCE|{expiry.isoformat()}|1050|CE"
    got = mv.close(sym)
    # Must equal bs.price at that day's close with the SAME full-history HV × multiplier.
    closes = sd.frames["RELIANCE"].copy()
    closes["date"] = pd.to_datetime(closes["date"])
    vol = realized_vol_provider(closes.set_index("date")["close"], window=20)(CAL[10]) * 1.2
    spot = float(sd.frames["RELIANCE"]["close"].iloc[10])
    t = (expiry - CAL[10]).days / 365.0
    assert got == pytest.approx(bs.price(spot, 1050.0, t, 0.065, vol, "CE"), rel=1e-9)

    nifty_sym = f"NIFTY|{expiry.isoformat()}|23000|CE"
    assert mv.close(nifty_sym) == 100.0  # the stub REAL series, not a BS price
    assert sd.option_series_calls and sd.option_series_calls[0][0] == "NIFTY"


def test_loader_clamps_to_contract_life():
    sd = FakeSD()
    mv, _c, _s, _m = build_basket_options_run(sd, ["RELIANCE"], CAL[0], CAL[-1])
    far_expiry = CAL[-1] + timedelta(days=120)  # far future → life window starts after CAL ends
    mv.set_date(pd.Timestamp(CAL[3]))
    sym = f"RELIANCE|{far_expiry.isoformat()}|1000|CE"
    with pytest.raises(KeyError):
        mv.close(sym)  # no prices generated outside expiry−60d → expiry


def test_index_spot_and_day_range():
    sd = FakeSD()
    mv, _c, _s, _m = build_basket_options_run(sd, ["RELIANCE"], CAL[0], CAL[-1])
    mv.set_date(pd.Timestamp(CAL[5]))
    row = sd.frames["RELIANCE"].iloc[5]
    assert mv.index_spot("RELIANCE") == pytest.approx(float(row["close"]))
    assert mv.index_spot("NIFTY") == 22000.0
    hi, lo = mv.day_range("RELIANCE")
    assert hi == pytest.approx(float(row["high"])) and lo == pytest.approx(float(row["low"]))
    assert mv.day_range("NOSUCH") is None


def test_stock_lot_table_seeded():
    assert lot_size_for("RELIANCE", date(2024, 3, 1)) == 500
    assert lot_size_for("HDFCBANK", date(2024, 3, 1)) == 650
    with pytest.raises(KeyError):
        lot_size_for("LTIM", date(2024, 3, 1))  # no F&O listing — deliberately absent
