"""Donchian Strangle Monthly — screener service (pure functions) + the strategy driven through a
real LiveSession (paper, scripted quotes, multi-underlying). No DB / async / network."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

from skas_algo.data.options_provider import build_live_options_run
from skas_algo.engine.live import LiveSession
from skas_algo.engine.options.charges import ChargeModel
from skas_algo.services.donchian_strangle import (
    DonchianParams,
    analyze_name,
    annualized_hv,
    beta_from_frames,
    donchian_range,
    pick_strike,
    portfolio_panel,
    resolve_cycle,
    strike_step,
)
from skas_algo.strategies.donchian_strangle_monthly import DonchianStrangleMonthlyStrategy
from skas_algo.strategies.registry import available

EXP = "2026-01-13"
EXP_D = date(2026, 1, 13)


# ─────────────────────────────────────────────────────────── service: pure math

def _ohlc(n=40, close=1000.0, high=1100.0, low=900.0, start=date(2025, 11, 1)):
    days = [start + timedelta(days=i) for i in range(n)]
    return pd.DataFrame({"date": days, "open": [close] * n, "high": [high] * n,
                         "low": [low] * n, "close": [close] * n})


def _chain(spot=1000.0, lot=100, lo=850, hi=1150, step=10, ce_ltp=25.0, pe_ltp=22.0):
    strikes = list(range(lo, hi + 1, step))
    rows = [{"strike": float(k), "ce": {"ltp": ce_ltp, "oi": 1000, "bid": ce_ltp - 0.2, "ask": ce_ltp + 0.2},
             "pe": {"ltp": pe_ltp, "oi": 1000, "bid": pe_ltp - 0.2, "ask": pe_ltp + 0.2}} for k in strikes]
    return {"spot": spot, "lot_size": lot, "rows": rows}


def test_annualized_hv_floored_and_percent():
    hv = annualized_hv([1000.0] * 40, 20)  # flat series → floored realized vol, as a percent
    assert hv is not None and hv == 5.0  # 0.05 floor × 100


def test_donchian_range_window():
    df = pd.DataFrame({"date": [date(2025, 12, d) for d in (1, 2, 3, 4, 5)],
                       "high": [10, 20, 99, 15, 12], "low": [5, 4, 3, 8, 7]})
    hi, lo = donchian_range(df, date(2025, 12, 2), date(2025, 12, 4))
    assert hi == 99 and lo == 3


def test_pick_strike_nearest_and_round_out():
    s = [100.0, 110.0, 120.0, 130.0]
    assert pick_strike(s, 113, "CE", round_out=False) == 110  # nearest
    assert pick_strike(s, 113, "CE", round_out=True) == 120   # CE rounds up (out)
    assert pick_strike(s, 113, "PE", round_out=True) == 110   # PE rounds down (out)


def _analyze(**kw):
    base = dict(symbol="AAA", df=_ohlc(), chain=_chain(), sell_expiry=EXP_D,
                range_start=date(2025, 11, 1), range_end=date(2025, 12, 1),
                entry_date=date(2025, 12, 2), atm_iv=40.0, ivp=70.0, event=None,
                params=DonchianParams())
    base.update(kw)
    return analyze_name(**base)


def test_analyze_name_strangle():
    row = _analyze()
    assert row["status"] == "strangle"
    assert row["ce"]["strike"] == 1100 and row["pe"]["strike"] == 900
    assert row["ce"]["premium"] == 25 and row["margin"] > 0


def test_analyze_name_skip_thin_leg_single_sided():
    chain = _chain(ce_ltp=2.0)  # CE premium 2 < 0.5% of 1000 (=5) → skipped → PE-only
    row = _analyze(chain=chain)
    assert row["status"] == "PE-only" and row["ce"]["skip"] is True


def test_analyze_name_excluded_event():
    row = _analyze(event="2026-01-05")  # event inside [entry_date, sell_expiry]
    assert row["status"] == "excluded:event"


def test_analyze_name_excluded_ivp_filter():
    row = _analyze(ivp=10.0)  # below ivp_min 50
    assert row["status"] == "excluded:filter"


def test_resolve_cycle_anchors():
    today = date(2026, 1, 20)
    cyc = resolve_cycle(today, [date(2026, 1, 27), date(2026, 2, 24)])
    assert cyc["sell_expiry"] == date(2026, 1, 27)           # nearest listed monthly ≥ today
    assert cyc["range_end"] <= today                          # last monthly on/before today
    assert cyc["range_start"] < cyc["range_end"]              # the prior monthly
    assert cyc["entry_date"] > cyc["range_end"]


def test_resolve_cycle_snaps_to_trading_days():
    today = date(2026, 1, 20)
    # Pretend the Dec-2025 monthly (calendar Dec 30) fell on a holiday → trading days exclude it.
    tds = [date(2025, 12, 29), date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 2)]
    cyc = resolve_cycle(today, [date(2026, 1, 27)], trading_days=tds)
    assert cyc["range_end"] == date(2025, 12, 29)   # snapped back from the holiday
    assert cyc["entry_date"] == date(2025, 12, 31)  # next actual trading day


def test_portfolio_panel_hedge_and_stop():
    selected = [{"symbol": "AAA", "spot": 1000.0, "lot_size": 100, "lots": 20,
                 "ce": {"strike": 1100, "premium": 25.0}, "pe": {"strike": 900, "premium": 22.0}}]
    nrows = [{"strike": float(k), "ce": {"ltp": 100.0}, "pe": {"ltp": 100.0}}
             for k in range(23000, 27001, 50)]
    panel = portfolio_panel(selected, nifty_spot=25000.0, nifty_lot_size=50,
                            nifty_chain={"rows": nrows}, params=DonchianParams())
    assert panel["agg_notional"] == 2_000_000          # 1000 × 100 × 20 (once per name)
    assert panel["premium_collected"] == (25 + 22) * 100 * 20
    assert panel["portfolio_sl_amount"] == 40_000      # 2% of notional
    h = panel["hedge"]
    assert h["nifty_lots"] == 2                          # round(2e6 / (25000×50))
    assert h["ce_strike"] == 26150 and h["pe_strike"] == 23850  # ~4.5% OTM, rounded out


def test_strike_step():
    assert strike_step([100.0, 110.0, 120.0, 130.0]) == 10
    assert strike_step([24000.0, 24050.0, 24100.0]) == 50


def test_beta_from_frames_exact():
    import math
    rets = [0.01, -0.02, 0.03, -0.01, 0.02, 0.005, -0.015] * 5
    days = [date(2025, 11, 1) + timedelta(days=i) for i in range(len(rets) + 1)]
    nclose, xclose = [100.0], [1000.0]
    for r in rets:
        nclose.append(nclose[-1] * math.exp(r))
        xclose.append(xclose[-1] * math.exp(2 * r))  # the name moves 2× NIFTY → beta 2.0
    b = beta_from_frames(pd.DataFrame({"date": days, "close": xclose}),
                         pd.DataFrame({"date": days, "close": nclose}))
    assert b is not None and abs(b - 2.0) < 1e-6


def test_portfolio_panel_beta_weighted():
    sel = [{"symbol": "AAA", "spot": 1000.0, "lot_size": 100, "lots": 20, "beta": 2.0,
            "ce": {"strike": 1100, "premium": 25.0}, "pe": {"strike": 900, "premium": 22.0}}]
    nrows = [{"strike": float(k), "ce": {"ltp": 100.0}, "pe": {"ltp": 100.0}} for k in range(23000, 27001, 50)]
    base = dict(nifty_spot=25000.0, nifty_lot_size=50, nifty_chain={"rows": nrows})
    plain = portfolio_panel(sel, params=DonchianParams(hedge_beta_weight=False), **base)
    weighted = portfolio_panel(sel, params=DonchianParams(hedge_beta_weight=True), **base)
    assert plain["hedge"]["nifty_lots"] == 2     # round(2e6 / 1.25e6)
    assert weighted["hedge"]["nifty_lots"] == 3  # round(4e6 / 1.25e6) — beta doubles the hedge notional


# ──────────────────────────────────────────────── strategy: driven via LiveSession

def _prem(strike, dte, spot, right):
    import math
    dist = (strike - spot) if right == "CE" else (spot - strike)
    return round(50.0 * math.exp(-dist / max(spot, 1) / 0.1) * max(0.05, dte / 30.0), 2)


class FakeBasketSD:
    """Serves prices/chains for multiple underlyings (a stock 'AAA' + 'NIFTY')."""

    SPOT = {"AAA": 1000.0, "NIFTY": 25000.0, "NIFTY 50": 25000.0}

    def __init__(self, calendar):
        self.cal = calendar

    def _spot(self, sym):
        return self.SPOT.get(sym.upper(), 1000.0)

    def get_prices(self, symbol, start_date=None, end_date=None, asset_type="stock"):
        spot = self._spot(symbol)
        df = pd.DataFrame({"date": self.cal, "open": [spot] * len(self.cal),
                           "high": [spot * 1.1] * len(self.cal), "low": [spot * 0.9] * len(self.cal),
                           "close": [spot] * len(self.cal)})
        if start_date is not None:
            df = df[df["date"] >= start_date]
        if end_date is not None:
            df = df[df["date"] <= end_date]
        return df.reset_index(drop=True)

    def get_option_chain(self, underlying, on_date, expiry=None):
        spot = self._spot(underlying)
        strikes = [round(spot * 0.8) + i * (50 if underlying.upper() == "NIFTY" else 10) for i in range(80)]
        rows = [dict(trade_date=on_date, symbol=underlying.upper(), expiry_date=EXP_D, strike_price=k,
                     option_type=r, close=_prem(k, (EXP_D - on_date).days, spot, r),
                     settle_price=_prem(k, (EXP_D - on_date).days, spot, r), open_interest=1000)
                for k in strikes for r in ("CE", "PE") if EXP_D >= on_date]
        return pd.DataFrame(rows)

    def get_option_series(self, underlying, expiry, strike, option_type, start_date=None, end_date=None):
        spot = self._spot(underlying)
        rows = [{"trade_date": d, "close": _prem(float(strike), (expiry - d).days, spot, option_type.upper())}
                for d in self.cal if d <= expiry
                if (start_date is None or d >= start_date) and (end_date is None or d <= end_date)]
        return pd.DataFrame(rows)


def _biz(start, end):
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


LEGS = [
    {"underlying": "AAA", "right": "CE", "strike": 1050, "side": "sell", "lots": 1, "spot": 1000, "lot_size": 100, "strike_step": 10},
    {"underlying": "AAA", "right": "PE", "strike": 950, "side": "sell", "lots": 1, "spot": 1000, "lot_size": 100, "strike_step": 10},
    {"underlying": "NIFTY", "right": "CE", "strike": 26000, "side": "buy", "lots": 1, "spot": 25000, "lot_size": 50, "strike_step": 50},
    {"underlying": "NIFTY", "right": "PE", "strike": 24000, "side": "buy", "lots": 1, "spot": 25000, "lot_size": 50, "strike_step": 50},
]
SYMS = {
    "aaa_ce": "AAA|2026-01-13|1050|CE", "aaa_pe": "AAA|2026-01-13|950|PE",
    "nf_ce": "NIFTY|2026-01-13|26000|CE", "nf_pe": "NIFTY|2026-01-13|24000|PE",
}
ENTRY_Q = {SYMS["aaa_ce"]: 20.0, SYMS["aaa_pe"]: 18.0, SYMS["nf_ce"]: 50.0, SYMS["nf_pe"]: 50.0}


def _session(strat, now=datetime(2026, 1, 5, 9, 50)):
    sd = FakeBasketSD(_biz(date(2026, 1, 1), date(2026, 1, 20)))
    mv, _chain, settler, margin = build_live_options_run(sd, "NIFTY", now=now)
    sess = LiveSession(strat, initial_capital=5_000_000, market_view=mv, settler=settler,
                       margin_model=margin, charge_model=ChargeModel())
    return sess, mv


def _strat(**kw):
    return DonchianStrangleMonthlyStrategy(universe=["NIFTY"], initial_capital=5_000_000,
                                           expiry=EXP, legs=LEGS, **kw)


def test_registered():
    assert "donchian_strangle_monthly" in available()


def test_enters_all_legs_and_records_aggregates():
    strat = _strat()
    sess, _mv = _session(strat)
    sess.update_quotes(ENTRY_Q)
    events = sess.run_decision(datetime(2026, 1, 5, 9, 50))
    shorts = {e["ticker"] for e in events if e["action"] == "SHORT"}
    buys = {e["ticker"] for e in events if e["action"] == "BUY"}
    assert shorts == {SYMS["aaa_ce"], SYMS["aaa_pe"]}
    assert buys == {SYMS["nf_ce"], SYMS["nf_pe"]}
    assert strat.agg_notional == 1000.0 * 100  # AAA notional counted once
    assert strat.premium_collected == (20 + 18) * 100


def test_portfolio_stop_flattens_book():
    strat = _strat(portfolio_sl_pct=2.0)
    sess, _mv = _session(strat)
    sess.update_quotes(ENTRY_Q)
    sess.run_decision(datetime(2026, 1, 5, 9, 50))  # enter
    # Short legs richen sharply → combined MTM well past −2% of notional → flatten everything.
    sess.update_quotes({SYMS["aaa_ce"]: 100.0, SYMS["aaa_pe"]: 100.0,
                        SYMS["nf_ce"]: 50.0, SYMS["nf_pe"]: 50.0})
    out = sess.run_decision(datetime(2026, 1, 5, 10, 0))
    assert out and all(e.get("exit_reason") == "portfolio_stop"
                       for e in out if e["action"] in ("COVER", "SELL"))
    assert not sess.portfolio.lot_symbols()


def test_breach_rolls_to_atm_opposite():
    strat = _strat(portfolio_sl_pct=2.0, breach_basis="touch")
    sess, mv = _session(strat)
    sess.update_quotes(ENTRY_Q)
    sess.run_decision(datetime(2026, 1, 5, 9, 50))  # enter
    sess.update_quotes(ENTRY_Q)                       # marks unchanged → portfolio stop won't fire
    mv.set_index_spot("AAA", 1100.0)                  # AAA spot > short CE 1050 → CE breach → roll to ATM PE
    mv.set_index_spot("NIFTY", 25000.0)
    out = sess.run_decision(datetime(2026, 1, 5, 10, 0))
    assert any(e["action"] == "SHORT" and e["ticker"] == "AAA|2026-01-13|1100|PE" for e in out)  # rolled
    book = set(sess.portfolio.lot_symbols())
    assert SYMS["aaa_ce"] not in book and SYMS["aaa_pe"] not in book  # original AAA strangle closed
    assert "AAA|2026-01-13|1100|PE" in book                          # single fresh ATM short
    assert SYMS["nf_ce"] in book and SYMS["nf_pe"] in book           # NIFTY hedge untouched
    assert strat.flip_count["AAA"] == 1


def test_max_flips_closes_the_name():
    strat = _strat(portfolio_sl_pct=2.0, breach_basis="touch", max_flips=1)
    sess, mv = _session(strat)
    sess.update_quotes(ENTRY_Q)
    sess.run_decision(datetime(2026, 1, 5, 9, 50))
    sess.update_quotes(ENTRY_Q)
    mv.set_index_spot("AAA", 1100.0)
    sess.run_decision(datetime(2026, 1, 5, 10, 0))  # 1st breach == max_flips → close the name, no re-roll
    book = set(sess.portfolio.lot_symbols())
    assert not any(s.startswith("AAA|") for s in book) and "AAA" in strat.closed_names
    assert SYMS["nf_ce"] in book and SYMS["nf_pe"] in book


def test_breach_basis_close_gates_to_eod():
    strat = _strat(portfolio_sl_pct=2.0, breach_basis="close")
    sess, mv = _session(strat)
    sess.update_quotes(ENTRY_Q)
    sess.run_decision(datetime(2026, 1, 5, 9, 50))
    sess.update_quotes(ENTRY_Q)
    mv.set_index_spot("AAA", 1100.0)
    sess.run_decision(datetime(2026, 1, 5, 10, 0))   # intraday — close-basis must NOT flip yet
    assert SYMS["aaa_ce"] in set(sess.portfolio.lot_symbols())
    sess.update_quotes(ENTRY_Q)
    out = sess.run_decision(datetime(2026, 1, 5, 15, 20))  # at/after EOD → flip fires
    assert any(e["action"] == "SHORT" and e["ticker"] == "AAA|2026-01-13|1100|PE" for e in out)


def test_flip_loss_books_realized_pnl():
    strat = _strat(portfolio_sl_pct=2.0, breach_basis="touch")
    sess, mv = _session(strat)
    sess.update_quotes(ENTRY_Q)
    sess.run_decision(datetime(2026, 1, 5, 9, 50))
    # Short CE richens to 35 (entry 20) — a loss, but not enough to trip the −2% portfolio stop,
    # so the breach roll runs and books the realized loss on the closed legs.
    sess.update_quotes({**ENTRY_Q, SYMS["aaa_ce"]: 35.0})
    mv.set_index_spot("AAA", 1100.0)
    sess.run_decision(datetime(2026, 1, 5, 10, 0))
    assert strat.realized_pnl < 0  # the flip booked the loss so the portfolio stop stays honest


def test_settles_at_expiry():
    strat = _strat()
    sess, _mv = _session(strat)
    sess.update_quotes(ENTRY_Q)
    sess.run_decision(datetime(2026, 1, 5, 9, 50))
    assert sess.portfolio.lot_symbols()
    settle = sess.run_decision(datetime(2026, 1, 13, 15, 20))  # expiry day
    assert any(e["action"] == "SETTLE" for e in settle)
    assert not sess.portfolio.lot_symbols() and strat.done
