"""End-to-end options backtest: short straddle through the full options engine stack.

Drives the real ShortPremiumStrategy + OptionMarketView + ExpirySettler + BacktestRunner
against a synthetic skas-data-like source (no DB, no network), asserting that the
straddle is sold at the target DTE and settled to intrinsic at expiry.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from skas_algo.data.options_provider import build_options_run
from skas_algo.engine.runner import BacktestRunner
from skas_algo.strategies.short_premium import ShortPremiumStrategy

CALENDAR = [date(2024, 1, 22), date(2024, 1, 23), date(2024, 1, 24),
            date(2024, 1, 25), date(2024, 1, 26)]
EXPIRY = date(2024, 1, 25)
STRIKES = [20900, 21000, 21100]


def _chain_for(on_date: date) -> pd.DataFrame:
    # ATM=21000; flat-ish premiums that decay toward expiry (not used for settlement).
    prem = {20900: (160, 70), 21000: (120, 110), 21100: (70, 160)}  # (CE, PE)
    rows = []
    for k in STRIKES:
        ce, pe = prem[k]
        rows.append(dict(trade_date=on_date, symbol="NIFTY", expiry_date=EXPIRY,
                         strike_price=float(k), option_type="CE", close=float(ce),
                         settle_price=float(ce), open_interest=1000))
        rows.append(dict(trade_date=on_date, symbol="NIFTY", expiry_date=EXPIRY,
                         strike_price=float(k), option_type="PE", close=float(pe),
                         settle_price=float(pe), open_interest=1000))
    return pd.DataFrame(rows)


class FakeOptionsSD:
    """Minimal stand-in for SkasData: an index series + per-date option chains."""

    def __init__(self):
        self.index = pd.DataFrame({"date": CALENDAR, "close": [21000.0] * len(CALENDAR)})
        self.chains = {d: _chain_for(d) for d in CALENDAR}

    def get_prices(self, symbol, start_date=None, end_date=None, asset_type="stock"):
        return self.index  # only the NIFTY 50 index series is requested

    def get_option_chain(self, underlying, on_date, expiry=None):
        return self.chains.get(on_date, pd.DataFrame())

    def get_option_series(self, underlying, expiry, strike, option_type,
                          start_date=None, end_date=None):
        rows = []
        for d, df in self.chains.items():
            m = df[(df.expiry_date == expiry) & (df.strike_price == float(strike))
                   & (df.option_type == option_type.upper())]
            if len(m):
                rows.append({"trade_date": d, "close": float(m.iloc[0]["close"])})
        return pd.DataFrame(rows)


def test_short_straddle_enters_and_settles():
    sd = FakeOptionsSD()
    strategy = ShortPremiumStrategy(
        universe=["NIFTY"], underlying="NIFTY", structure="straddle",
        dte_target=1, lots=1, stop_loss_pct=10.0, profit_target_pct=10.0,  # no early exit
    )
    market_view, _chain, settler, margin_model = build_options_run(
        sd, "NIFTY", CALENDAR[0], CALENDAR[-1])

    runner = BacktestRunner(
        strategy=strategy, universe=["NIFTY"], loader=lambda *a: None,
        initial_capital=2_000_000, tax_rate=0.0,
        market_view=market_view, settler=settler, margin_model=margin_model,
    )
    result = runner.run(CALENDAR[0], CALENDAR[-1])

    actions = [(t["action"], t["ticker"]) for t in result.transactions]
    shorts = [t for t in result.transactions if t["action"] == "SHORT"]
    settles = [t for t in result.transactions if t["action"] == "SETTLE"]

    # Sold a 2-leg straddle on Wed (dte=1), both legs settled at expiry.
    assert len(shorts) == 2, actions
    assert {inst_strike(t["ticker"]) for t in shorts} == {21000}
    assert len(settles) == 2, actions

    # ATM straddle entry premium 120+110=230/unit; spot pins at 21000 -> both expire
    # worthless -> short keeps full premium = 230 * 75 lot.
    realized = sum(t["profit"] for t in result.transactions)
    assert realized == (120 + 110) * 75

    # Margin was tracked while the position was open.
    assert margin_model.max_margin_used > 0
    # Final equity = initial + premium captured (tax_rate=0).
    assert result.history[-1]["total_equity"] == 2_000_000 + (120 + 110) * 75


def inst_strike(symbol: str) -> int:
    return int(symbol.split("|")[2])
