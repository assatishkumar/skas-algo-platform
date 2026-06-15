"""Backtest-then-forward seeding: replay to a past 'today' and carry the open book forward.

Drives ``seed_state_from_backtest`` against a synthetic skas-data source so a short straddle
is entered during the replay and is still open at the seed cutoff — the produced LiveSession
state must therefore carry the open option legs (the live PAPER starting book).
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from skas_algo.engine.portfolio import Portfolio
from skas_algo.live.manager import LiveConfig, manager
from skas_algo.live.seed import seed_state_from_backtest

CALENDAR = [date(2024, 1, 22), date(2024, 1, 23), date(2024, 1, 24), date(2024, 1, 25)]
EXPIRY = date(2024, 1, 25)
STRIKES = [20900, 21000, 21100]


def _chain_for(on_date: date) -> pd.DataFrame:
    prem = {20900: (160, 70), 21000: (120, 110), 21100: (70, 160)}
    rows = []
    for k in STRIKES:
        ce, pe = prem[k]
        for r, px in (("CE", ce), ("PE", pe)):
            rows.append(dict(trade_date=on_date, symbol="NIFTY", expiry_date=EXPIRY,
                             strike_price=float(k), option_type=r, close=float(px),
                             settle_price=float(px), open_interest=1000))
    return pd.DataFrame(rows)


class FakeOptionsSD:
    def __init__(self):
        self.index = pd.DataFrame({"date": CALENDAR, "close": [21000.0] * len(CALENDAR)})
        self.chains = {d: _chain_for(d) for d in CALENDAR}

    def get_prices(self, symbol, start_date=None, end_date=None, asset_type="stock"):
        df = self.index
        if start_date is not None:
            df = df[df["date"] >= start_date]
        if end_date is not None:
            df = df[df["date"] <= end_date]  # honor the seed cutoff (don't replay past it)
        return df.reset_index(drop=True)

    def get_option_chain(self, underlying, on_date, expiry=None):
        return self.chains.get(on_date, pd.DataFrame())

    def get_option_series(self, underlying, expiry, strike, option_type, start_date=None, end_date=None):
        rows = []
        for d, df in self.chains.items():
            m = df[(df.expiry_date == expiry) & (df.strike_price == float(strike))
                   & (df.option_type == option_type.upper())]
            if len(m):
                rows.append({"trade_date": d, "close": float(m.iloc[0]["close"])})
        return pd.DataFrame(rows)


def test_seed_carries_open_book_forward(monkeypatch):
    sd = FakeOptionsSD()
    monkeypatch.setattr("skas_algo.data.provider.get_data_cache", lambda: sd)

    config = LiveConfig(
        name="seed", strategy_id="short_premium", symbols=["NIFTY"],
        instrument_class="DERIV", underlying="NIFTY", capital=2_000_000,
        params={"structure": "straddle", "dte_target": 3, "lots": 1,
                "stop_loss_pct": 10.0, "profit_target_pct": 10.0},
        warm_from_date=date(2024, 1, 22),
    )
    # Cut off BEFORE expiry so the straddle is still open → must carry forward.
    result = seed_state_from_backtest(config, loader=lambda *a: None, end_date=date(2024, 1, 24))
    state = result["state"]

    assert state["current_month"] == [2024, 1]
    assert result["transactions"]  # the replay's trades are carried for realized-P&L display
    p = Portfolio(cash=0)
    p.load_state(state["portfolio"])
    syms = p.lot_symbols()
    assert syms, "seed should carry the open straddle legs forward"
    assert all("|" in s for s in syms)  # they're option contracts
    assert any(s.split("|")[2] == "21000" for s in syms)  # the ATM straddle


def test_deploy_margin_guard_blocks_undercapitalized():
    # Batman ~₹2L margin per lot-set → 10 lot-sets needs ~₹20L; ₹1L must be rejected with
    # a suggested capital (the guard runs before any DB/data access).
    cfg = LiveConfig(
        name="x", strategy_id="batman_ratio_monthly", symbols=["NIFTY"],
        instrument_class="DERIV", underlying="NIFTY", capital=100_000, params={"lots": 10},
    )
    with pytest.raises(ValueError, match="margin"):
        manager.start(cfg, loader=lambda *a: None, quote_source=None)
