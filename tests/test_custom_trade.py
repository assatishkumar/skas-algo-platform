"""Custom Trade strategies: a user-built multi-leg option position and a managed equity trade,
driven through the real LiveSession (paper, scripted quotes). No DB / async / network."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pandas as pd

from skas_algo.data.options_provider import build_live_options_run
from skas_algo.engine.live import LiveSession
from skas_algo.engine.options.charges import ChargeModel
from skas_algo.strategies.custom_equity import CustomEquityStrategy
from skas_algo.strategies.custom_options import CustomOptionsStrategy
from skas_algo.strategies.registry import available

SPOT = 25000.0
EXPIRIES = [date(2026, 1, 13), date(2026, 1, 20)]
EXP = "2026-01-13"


def _biz(start, end):
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _prem(strike, dte, spot, right="CE"):
    dist = (strike - spot) if right == "CE" else (spot - strike)
    return round(100.0 * math.exp(-dist / 800.0) * max(0.05, dte / 30.0), 2)


class FakeLiveSD:
    def __init__(self, calendar, spot=SPOT):
        self.cal = calendar
        self.spot = spot
        self.strikes = [24000.0 + 50 * i for i in range(0, 60)]

    def get_prices(self, symbol, start_date=None, end_date=None, asset_type="stock"):
        df = pd.DataFrame({"date": self.cal, "close": [self.spot] * len(self.cal)})
        if start_date is not None:
            df = df[df["date"] >= start_date]
        if end_date is not None:
            df = df[df["date"] <= end_date]
        return df.reset_index(drop=True)

    def get_option_chain(self, underlying, on_date, expiry=None):
        rows = [dict(trade_date=on_date, symbol="NIFTY", expiry_date=e, strike_price=k,
                     option_type=r, close=_prem(k, (e - on_date).days, self.spot, r),
                     settle_price=_prem(k, (e - on_date).days, self.spot, r), open_interest=1000)
                for e in EXPIRIES if e >= on_date for k in self.strikes for r in ("CE", "PE")]
        return pd.DataFrame(rows)

    def get_option_series(self, underlying, expiry, strike, option_type, start_date=None, end_date=None):
        rows = [{"trade_date": d,
                 "close": _prem(float(strike), (expiry - d).days, self.spot, option_type.upper())}
                for d in self.cal if d <= expiry
                if (start_date is None or d >= start_date) and (end_date is None or d <= end_date)]
        return pd.DataFrame(rows)


def _opt_session(strat, now=datetime(2026, 1, 5, 9, 50)):
    sd = FakeLiveSD(_biz(date(2026, 1, 1), date(2026, 1, 20)))
    mv, _chain, settler, margin = build_live_options_run(sd, "NIFTY", now=now)
    sess = LiveSession(strat, initial_capital=1_000_000, market_view=mv, settler=settler,
                       margin_model=margin, charge_model=ChargeModel())
    return sess, mv


def _call_spread(**kw):
    return CustomOptionsStrategy(
        universe=["NIFTY"], underlying="NIFTY", initial_capital=1_000_000, expiry=EXP,
        legs=[{"right": "CE", "strike": 25000, "side": "sell", "lots": 1},
              {"right": "CE", "strike": 25200, "side": "buy", "lots": 1}],
        **kw,
    )


# ---------------------------------------------------------------- registry
def test_custom_strategies_registered():
    assert "custom_options" in available()
    assert "custom_equity" in available()


# ------------------------------------------------------------ custom_options
def test_custom_options_enters_selected_legs():
    strat = _call_spread()
    sess, _mv = _opt_session(strat)
    events = sess.run_decision(datetime(2026, 1, 5, 9, 50))
    shorts = [e for e in events if e["action"] == "SHORT"]
    buys = [e for e in events if e["action"] == "BUY"]
    assert len(shorts) == 1 and len(buys) == 1
    assert shorts[0]["ticker"] == "NIFTY|2026-01-13|25000|CE" and shorts[0]["units"] == 65
    assert buys[0]["ticker"] == "NIFTY|2026-01-13|25200|CE" and buys[0]["units"] == 65
    # Net entry premium is a credit (short ATM richer than the long OTM wing).
    assert strat.entered and strat._risk_base() > 0


def test_custom_options_combined_pnl_target_exits_all():
    strat = _call_spread(target_pct=0.5)
    sess, _mv = _opt_session(strat)
    sess.run_decision(datetime(2026, 1, 5, 9, 50))  # enter
    # Both premiums collapse → the spread is deep in profit → combined target fires, all legs exit.
    sess.update_quotes({leg: 0.05 for leg in strat.legs})
    out = sess.run_decision(datetime(2026, 1, 5, 10, 0))
    assert out and all(e.get("exit_reason") == "target" for e in out if e["action"] in ("COVER", "SELL"))
    assert not sess.portfolio.lot_symbols()


def test_custom_options_leg_premium_stop_exits_that_leg():
    strat = _call_spread(leg_stops={0: 0.5})  # short leg (index 0) stops if its premium +50%
    sess, _mv = _opt_session(strat)
    sess.run_decision(datetime(2026, 1, 5, 9, 50))
    short = strat.legs[0]
    entry = strat.entry_close[short]
    sess.update_quotes({short: entry * 2.0, strat.legs[1]: strat.entry_close[strat.legs[1]]})
    out = sess.run_decision(datetime(2026, 1, 5, 10, 0))
    assert out and any(e.get("exit_reason") == "leg_stop" for e in out)
    # Only the short leg closed; the long wing is still held.
    book = set(sess.portfolio.lot_symbols())
    assert short not in book and strat.legs[1] in book


def test_custom_options_spot_band_exits_all():
    strat = _call_spread(spot_upper=25400)
    sess, mv = _opt_session(strat)
    sess.run_decision(datetime(2026, 1, 5, 9, 50))
    mv.set_index_spot("NIFTY", 25500.0)  # live spot breaches the upper band
    sess.update_quotes({leg: strat.entry_close[leg] for leg in strat.legs})
    out = sess.run_decision(datetime(2026, 1, 5, 10, 0))
    assert out and all(e.get("exit_reason") == "spot_upper" for e in out if e["action"] in ("COVER", "SELL"))
    assert not sess.portfolio.lot_symbols()


def test_custom_options_settles_at_expiry():
    strat = _call_spread()
    sess, _mv = _opt_session(strat)
    sess.run_decision(datetime(2026, 1, 5, 9, 50))
    assert sess.portfolio.lot_symbols()
    settle = sess.run_decision(datetime(2026, 1, 13, 15, 20))  # expiry day
    assert any(e["action"] == "SETTLE" for e in settle)
    assert not sess.portfolio.lot_symbols() and strat.done


# ------------------------------------------------------------- custom_equity
def _equity_session(strat):
    sess = LiveSession(strat, initial_capital=100_000, lookback=5, tax_rate=0.0)
    sess.warmup({"AAA": [100.0] * 25})
    return sess


def _enter_immediate(strat):
    sess = _equity_session(strat)
    sess.update_quotes({"AAA": 100.0})
    ev = sess.run_decision(date(2024, 1, 2))
    sess.end_day()
    return sess, ev


def test_custom_equity_immediate_entry_then_target():
    strat = CustomEquityStrategy(universe=["AAA"], symbol="AAA", initial_capital=100_000,
                                 qty=10, entry_mode="immediate", target_pct=0.06, stop_pct=0.05)
    sess, ev = _enter_immediate(strat)
    assert any(e["action"] == "BUY" and e["units"] == 10 for e in ev)
    sess.update_quotes({"AAA": 107.0})  # +7% > 6% target
    out = sess.run_decision(date(2024, 1, 3))
    assert any(e["action"] == "SELL" for e in out)
    assert not sess.portfolio.lot_symbols() and strat.done


def test_custom_equity_trigger_waits_for_cross():
    strat = CustomEquityStrategy(universe=["AAA"], symbol="AAA", initial_capital=100_000,
                                 qty=10, entry_mode="trigger", trigger_price=110.0)
    sess = _equity_session(strat)
    sess.update_quotes({"AAA": 100.0})
    assert sess.run_decision(date(2024, 1, 2)) == []   # below trigger → no entry
    sess.end_day()
    sess.update_quotes({"AAA": 105.0})
    assert sess.run_decision(date(2024, 1, 3)) == []   # still below → no entry
    sess.end_day()
    sess.update_quotes({"AAA": 112.0})                 # crosses 110 → enter
    out = sess.run_decision(date(2024, 1, 4))
    assert any(e["action"] == "BUY" for e in out) and strat.entered


def test_custom_equity_hard_stop():
    strat = CustomEquityStrategy(universe=["AAA"], symbol="AAA", initial_capital=100_000,
                                 qty=10, entry_mode="immediate", stop_pct=0.05)
    sess, _ev = _enter_immediate(strat)
    sess.update_quotes({"AAA": 94.0})  # −6% < −5% stop
    out = sess.run_decision(date(2024, 1, 3))
    assert any(e["action"] == "SELL" for e in out)
    assert not sess.portfolio.lot_symbols()


def test_custom_equity_trailing_stop():
    strat = CustomEquityStrategy(universe=["AAA"], symbol="AAA", initial_capital=100_000,
                                 qty=10, entry_mode="immediate", trailing=True, trail_pct=0.05)
    sess, _ev = _enter_immediate(strat)
    sess.update_quotes({"AAA": 120.0})  # new high-water mark, no stop
    assert sess.run_decision(date(2024, 1, 3)) == []
    sess.end_day()
    sess.update_quotes({"AAA": 113.0})  # 113 <= 120*0.95=114 → trailing stop
    out = sess.run_decision(date(2024, 1, 4))
    assert any(e["action"] == "SELL" for e in out)
    assert not sess.portfolio.lot_symbols()
