"""Synthetic GOLD option chain: BS round-trip + an end-to-end synthetic backtest.

Validates (a) the synthetic chain reprices to its input vol, and (b) a short straddle
runs through the SAME engine on the synthetic GOLD market (entries + expiry settlement),
using a fake skas-data source (no MCX session, no network).
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd

from skas_algo.data.synthetic_options import build_synthetic_options_run, synthetic_chain_df
from skas_algo.engine.options.black_scholes import implied_vol
from skas_algo.engine.runner import BacktestRunner
from skas_algo.strategies.short_premium import ShortPremiumStrategy


def _biz_days(start: date, n: int) -> list[date]:
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


CALENDAR = _biz_days(date(2024, 1, 15), 21)  # through mid-Feb; expiry 2024-02-05 lands inside


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
    atm = round(spot / 100) * 100
    ce = df[(df.option_type == "CE") & (df.strike_price == atm)].iloc[0]
    t = (exp - on).days / 365.0
    iv = implied_vol(float(ce.close), spot, atm, t, 0.065, "CE")
    assert iv is not None and abs(iv - vol) < 0.01  # BS price → IV recovers the input vol


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
