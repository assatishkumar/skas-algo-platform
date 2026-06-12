"""Synthetic GOLD option chain: BS round-trip + an end-to-end synthetic backtest.

Validates (a) the synthetic chain reprices to its input vol, and (b) a short straddle
runs through the SAME engine on the synthetic GOLD market (entries + expiry settlement),
using a fake skas-data source (no MCX session, no network).
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd

from skas_algo.data.synthetic_options import (
    GOLD_STRIKE_STEP,
    build_synthetic_options_run,
    gold_monthly_expiries,
    synthetic_chain_df,
)
from skas_algo.engine.options.black_scholes import black76_implied_vol
from skas_algo.engine.options.contract_specs import lot_size_for
from skas_algo.engine.runner import BacktestRunner
from skas_algo.strategies.short_premium import ShortPremiumStrategy


def _biz_days(start: date, n: int) -> list[date]:
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


CALENDAR = _biz_days(date(2024, 1, 15), 21)  # through mid-Feb; expiry 2024-01-26 lands inside


class FakeGoldSD:
    """Minimal SkasData stand-in: only the cached GOLD futures series is requested."""

    def __init__(self, calendar: list[date]):
        # gently varying GOLD price → non-trivial realized vol
        closes = [62000 + 200 * math.sin(i / 2.0) + 15 * i for i in range(len(calendar))]
        self.gold = pd.DataFrame({"date": calendar, "close": closes})

    def get_prices(self, symbol, start_date=None, end_date=None, asset_type="stock"):
        df = self.gold
        if start_date is not None:
            df = df[df["date"] >= start_date]
        if end_date is not None:
            df = df[df["date"] <= end_date]
        return df.reset_index(drop=True)


def test_synthetic_chain_round_trips_iv():
    on, exp, spot, vol = date(2024, 1, 15), date(2024, 2, 5), 62000.0, 0.18
    df = synthetic_chain_df("GOLD", on, exp, spot, vol)
    atm = round(spot / GOLD_STRIKE_STEP) * GOLD_STRIKE_STEP
    ce = df[(df.option_type == "CE") & (df.strike_price == atm)].iloc[0]
    t = (exp - on).days / 365.0
    iv = black76_implied_vol(float(ce.close), spot, atm, t, 0.065, "CE")
    assert iv is not None and abs(iv - vol) < 0.01  # Black-76 price → IV recovers the input vol


def test_synthetic_chain_black76_parity_and_oi():
    """Options on futures: C - P = e^{-rt}(F - K) (no carry drift), and rows are tradable."""
    on, exp, spot, vol, r = date(2024, 1, 15), date(2024, 2, 5), 62000.0, 0.18, 0.065
    df = synthetic_chain_df("GOLD", on, exp, spot, vol)
    k = 62500.0  # F < K → calls must be CHEAPER than puts
    ce = float(df[(df.option_type == "CE") & (df.strike_price == k)].close.iloc[0])
    pe = float(df[(df.option_type == "PE") & (df.strike_price == k)].close.iloc[0])
    t = (exp - on).days / 365.0
    assert abs((ce - pe) - math.exp(-r * t) * (spot - k)) < 1e-6
    assert (df.open_interest > 0).all()  # nominal OI so oi>0 liquidity guards don't skip


def test_gold_expiry_calendar():
    # GOLDM options expire in the LAST week of the month (e.g. 26 Jun 2026), not the 5th.
    assert gold_monthly_expiries(date(2026, 6, 12), ahead=1) == [date(2026, 6, 26)]
    # Weekend rollback: 26 Jul 2026 is a Sunday → prior business day (Fri 24 Jul).
    assert gold_monthly_expiries(date(2026, 7, 1), ahead=1) == [date(2026, 7, 24)]


def test_gold_lot_size_is_goldm():
    assert lot_size_for("GOLD", date(2026, 6, 12)) == 10  # GOLDM: 100 g at ₹/10g quote


def test_synthetic_gold_backtest_enters_and_settles():
    sd = FakeGoldSD(CALENDAR)
    strategy = ShortPremiumStrategy(
        universe=["GOLD"], underlying="GOLD", structure="straddle",
        dte_target=7, lots=1, stop_loss_pct=10.0, profit_target_pct=10.0,  # no early exit
    )
    mv, _chain, settler, margin = build_synthetic_options_run(sd, "GOLD", CALENDAR[0], CALENDAR[-1])
    runner = BacktestRunner(
        strategy=strategy, universe=["GOLD"], loader=lambda *a: None,
        initial_capital=2_000_000, tax_rate=0.0,
        market_view=mv, settler=settler, margin_model=margin,
    )
    result = runner.run(CALENDAR[0], CALENDAR[-1])

    shorts = [t for t in result.transactions if t["action"] == "SHORT"]
    settles = [t for t in result.transactions if t["action"] == "SETTLE"]
    assert len(shorts) == 2, [(t["action"], t["ticker"]) for t in result.transactions]
    assert len(settles) == 2  # both legs settled at expiry
    assert all(t["ticker"].startswith("GOLD|") for t in shorts)
    assert margin.max_margin_used > 0
