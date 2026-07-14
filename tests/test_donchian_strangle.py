"""Donchian Strangle Monthly — screener service (pure functions) + the strategy driven through a
real LiveSession (paper, scripted quotes, multi-underlying). No DB / async / network."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

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
    assert row["ce"]["premium"] == 24.8 and row["margin"] > 0  # bid (ltp 25 − 0.2), not last-traded


def test_excluded_event_still_carries_rule_strikes():
    # An event-excluded name keeps its rule-based CE/PE legs so the UI can default a manual override.
    row = _analyze(event="2025-12-15")  # inside the cycle window → excluded:event
    assert row["status"] == "excluded:event"
    assert row["ce"] is not None and row["pe"] is not None
    assert row["ce"]["strike"] == 1100 and row["pe"]["strike"] == 900


def test_breakout_up_skips_itm_ce_sells_atm_pe():
    # Spot (1200) above the Donchian high (1100) → the CE would be ITM. Breakout rule: skip the CE
    # and sell the ATM PE instead.
    row = _analyze(chain=_chain(spot=1200.0))
    assert row["breakout"] == "up" and row["status"] == "PE-only"
    assert row["ce"] is None and row["pe"] is not None
    assert "breakout" in (row.get("reason") or "")


def test_breakout_rule_off_keeps_donchian_ce():
    row = _analyze(chain=_chain(spot=1200.0),
                   params=DonchianParams(ivp_min=0, require_iv_gt_hv=False, breakout_atm=False))
    assert row.get("breakout") is None and row["ce"] is not None  # rule off → ITM CE kept


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


def test_enter_skips_unpriceable_leg_not_whole_basket():
    from types import SimpleNamespace

    from skas_algo.strategies.donchian_strangle_monthly import DonchianStrangleMonthlyStrategy

    strat = DonchianStrangleMonthlyStrategy(
        universe=["NIFTY"], initial_capital=1_000_000, expiry="2026-07-28",
        legs=[
            {"underlying": "RELIANCE", "right": "CE", "strike": 1370, "side": "sell", "lots": 1, "lot_size": 500, "spot": 1300},
            {"underlying": "RELIANCE", "right": "PE", "strike": 1250, "side": "sell", "lots": 1, "lot_size": 500, "spot": 1300},
            {"underlying": "JSWSTEEL", "right": "CE", "strike": 1330, "side": "sell", "lots": 1, "lot_size": 875, "spot": 1280},
        ],
    )
    market = SimpleNamespace(index_spot=lambda u: {"RELIANCE": 1300.0, "JSWSTEEL": 1280.0}.get(u))
    ctx = SimpleNamespace(market=market, close=lambda sym: 0.0 if "JSWSTEEL" in sym else 50.0)  # JSWSTEEL leg dead
    syms = [s.symbol for s in strat._enter(ctx)]
    assert any("RELIANCE" in s and s.endswith("CE") for s in syms)   # priceable legs entered
    assert any("RELIANCE" in s and s.endswith("PE") for s in syms)
    assert not any("JSWSTEEL" in s for s in syms)                    # one dead leg skipped, basket NOT blocked
    assert strat.entered is True


def test_resolve_cycle_anchors():
    today = date(2026, 1, 20)
    cyc = resolve_cycle(today, [date(2026, 1, 27), date(2026, 2, 24)])
    assert cyc["sell_expiry"] == date(2026, 1, 27)           # nearest listed monthly ≥ today
    assert cyc["range_end"] == today                          # cycle-to-date → ends today
    assert cyc["entry_date"] == today                         # enter today
    assert cyc["range_start"] < cyc["range_end"]              # starts after last month's expiry


def test_resolve_cycle_snaps_to_trading_days():
    today = date(2026, 1, 20)
    # Pretend the Dec-2025 monthly (calendar Dec 30) fell on a holiday → trading days exclude it.
    tds = [date(2025, 12, 29), date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 2)]
    cyc = resolve_cycle(today, [date(2026, 1, 27)], trading_days=tds)
    assert cyc["range_start"] == date(2025, 12, 31)  # day after the holiday-snapped Dec 29 expiry
    assert cyc["range_end"] == today                  # cycle-to-date ends today
    assert cyc["entry_date"] == today


def test_resolve_cycle_rejects_inverted_override():
    # A stale UI override with range_start AFTER range_end must not invert the window (→ all rows
    # would error). resolve_cycle falls back to the auto-resolved anchors.
    today = date(2026, 6, 29)
    cyc = resolve_cycle(today, [date(2026, 7, 28)],
                        range_start=date(2026, 5, 27), range_end=date(2026, 5, 26))
    assert cyc["range_start"] < cyc["range_end"]   # never inverted


def test_resolve_cycle_rolls_past_imminent_expiry():
    # On the expiry day itself the sell must roll to the next monthly (≥ min_dte out), not sell the
    # ~0-DTE contract that's expiring today.
    today = date(2026, 6, 30)  # the June 2026 monthly expiry (last Tuesday)
    cyc = resolve_cycle(today, [date(2026, 6, 30), date(2026, 7, 28)])
    assert cyc["sell_expiry"] == date(2026, 7, 28)   # rolled past today's 0-DTE expiry


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
    # ~4.5% OTM, rounded out to the nearest ELIGIBLE strike — NIFTY trades round 100s only (owner
    # rule), so the 50-step targets (26125/23875) snap to 26200/23800, not the listed 26150/23850.
    assert h["ce_strike"] == 26200 and h["pe_strike"] == 23800


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


def test_portfolio_stop_margin_basis_flattens():
    # basis="margin": SL is % of basket margin. Force a known margin so the threshold is exact.
    strat = _strat(portfolio_basis="margin", portfolio_sl_pct=4.0)
    sess, _mv = _session(strat)
    sess.set_margin_override(100_000.0)             # 4% → a ₹4,000 stop
    sess.update_quotes(ENTRY_Q)
    sess.run_decision(datetime(2026, 1, 5, 9, 50))  # enter
    # Shorts richen → combined loss ~₹12,200, well past −₹4,000 → flatten everything.
    sess.update_quotes({SYMS["aaa_ce"]: 80.0, SYMS["aaa_pe"]: 80.0,
                        SYMS["nf_ce"]: 50.0, SYMS["nf_pe"]: 50.0})
    out = sess.run_decision(datetime(2026, 1, 5, 10, 0))
    assert out and all(e.get("exit_reason") == "portfolio_stop"
                       for e in out if e["action"] in ("COVER", "SELL"))
    assert not sess.portfolio.lot_symbols()


def test_portfolio_target_margin_basis_flattens():
    # basis="margin": the profit target is % of basket margin too.
    strat = _strat(portfolio_basis="margin", portfolio_target_enabled=True, portfolio_target_pct=6.0)
    sess, _mv = _session(strat)
    sess.set_margin_override(50_000.0)              # 6% → a ₹3,000 target
    sess.update_quotes(ENTRY_Q)
    sess.run_decision(datetime(2026, 1, 5, 9, 50))  # enter
    # Shorts decay toward zero → combined profit ~₹3,600 ≥ ₹3,000 → flatten.
    sess.update_quotes({SYMS["aaa_ce"]: 1.0, SYMS["aaa_pe"]: 1.0,
                        SYMS["nf_ce"]: 50.0, SYMS["nf_pe"]: 50.0})
    out = sess.run_decision(datetime(2026, 1, 5, 10, 0))
    assert out and all(e.get("exit_reason") == "portfolio_target"
                       for e in out if e["action"] in ("COVER", "SELL"))
    assert not sess.portfolio.lot_symbols()


def test_leg_target_closes_single_leg_on_premium_capture():
    # Leg target = 80% of each leg's OWN premium. One leg decays past it, the other doesn't.
    strat = _strat(leg_target_enabled=True, leg_target_pct=80.0)  # notional basis (default); pf target off
    sess, _mv = _session(strat)
    sess.update_quotes(ENTRY_Q)
    sess.run_decision(datetime(2026, 1, 5, 9, 50))  # enter
    # aaa_ce 20 → 3 = captured 85% (≥80) → close; aaa_pe 18 → 18 = 0% → stays open.
    sess.update_quotes({SYMS["aaa_ce"]: 3.0, SYMS["aaa_pe"]: 18.0,
                        SYMS["nf_ce"]: 50.0, SYMS["nf_pe"]: 50.0})
    out = sess.run_decision(datetime(2026, 1, 5, 10, 0))
    book = set(sess.portfolio.lot_symbols())
    assert SYMS["aaa_ce"] not in book                       # leg taken off for profit
    assert SYMS["aaa_pe"] in book                            # opposite leg untouched
    assert SYMS["nf_ce"] in book and SYMS["nf_pe"] in book   # hedge untouched
    assert any(e.get("exit_reason") == "leg_target" for e in out if e["action"] in ("COVER", "SELL"))
    assert strat.realized_pnl > 0                            # booked the captured premium


def test_history_persists_across_restart_and_builds_report():
    """The live report (equity curve / yearly / monthly booked) needs the equity history to survive a
    restart. export_state persists it (daily) + the flush log; load_state revives both as datetimes so
    build_report runs."""
    from skas_algo.engine.report import build_report
    from skas_algo.engine.runner import RunResult

    strat = _strat()
    sess, _mv = _session(strat)
    sess.history = [
        {"date": datetime(2026, 1, 5, 15, 30), "cash": 1_000_000.0, "holdings_value": 0.0,
         "invested_capital": 0.0, "total_equity": 1_000_000.0},
        {"date": datetime(2026, 1, 6, 15, 30), "cash": 1_010_000.0, "holdings_value": 0.0,
         "invested_capital": 0.0, "total_equity": 1_010_000.0},
    ]
    sess.transactions = [{"date": datetime(2026, 1, 6, 15, 30), "ticker": "X", "action": "SELL", "profit": 10_000.0}]
    sess.monthly_flush_log = {(2026, 1): {"tax": 2_000.0, "withdrawal": 0.0, "date": datetime(2026, 1, 31)}}

    sess2, _ = _session(_strat())
    sess2.load_state(sess.export_state())
    assert len(sess2.history) == 2 and isinstance(sess2.history[0]["date"], datetime)
    assert sess2.monthly_flush_log[(2026, 1)]["tax"] == 2_000.0

    rep = build_report(RunResult(history=sess2.history, transactions=sess2.transactions,
                                 monthly_flush_log=sess2.monthly_flush_log, portfolio=sess2.portfolio),
                       1_000_000.0)
    assert rep["equity_curve"] and rep["monthly_profit"][2026][1] == 10_000.0  # booked from the trade


def test_entry_books_at_bid_not_ltp():
    """With a two-sided book, the strategy records each short's entry at the BID (the price the broker
    actually fills) — not the LTP — so the basket MTM/premium match the portfolio's unrealized."""
    strat = _strat()
    sess, mv = _session(strat)

    def chain_fn(u, _e):
        if u == "AAA":  # shorts: LTP is 20/18 (ENTRY_Q) but the bid is lower
            return {"spot": 1000.0, "rows": [
                {"strike": 1050, "ce": {"bid": 18.0, "ask": 22.0}, "pe": {}},
                {"strike": 950, "ce": {}, "pe": {"bid": 16.0, "ask": 20.0}},
            ]}
        return {"spot": 25000.0, "rows": [  # NIFTY hedge longs fill at the ask
            {"strike": 26000, "ce": {"bid": 48.0, "ask": 52.0}, "pe": {}},
            {"strike": 24000, "ce": {}, "pe": {"bid": 48.0, "ask": 52.0}},
        ]}

    mv.set_chain_fn(chain_fn)
    sess.update_quotes(ENTRY_Q)  # LTP feed
    sess.run_decision(datetime(2026, 1, 5, 9, 50))
    assert strat.entry_close[SYMS["aaa_ce"]] == 18.0          # bid, not the LTP 20
    assert strat.entry_close[SYMS["aaa_pe"]] == 16.0          # bid, not the LTP 18
    assert strat.premium_collected == (18 + 16) * 100         # real credit received


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


def test_flip_30delta_uses_live_chain():
    from skas_algo.engine.options import black_scholes as bs

    strat = _strat(portfolio_sl_pct=2.0, breach_basis="touch", flip_delta="30delta")
    sess, mv = _session(strat)
    spot, r, sig = 1100.0, 0.065, 0.30
    t = (EXP_D - date(2026, 1, 5)).days / 365.0
    strikes = [900.0 + 10 * i for i in range(0, 41)]  # 900..1300

    def chain_fn(_u, _e):  # BS-priced chain at σ=0.30 so deltas are well-defined
        rows = [{"strike": k, "ce": {"ltp": bs.price(spot, k, t, r, sig, "CE")},
                 "pe": {"ltp": bs.price(spot, k, t, r, sig, "PE")}} for k in strikes]
        return {"spot": spot, "rows": rows}

    mv.set_chain_fn(chain_fn)
    sess.update_quotes(ENTRY_Q)
    sess.run_decision(datetime(2026, 1, 5, 9, 50))  # enter
    sess.update_quotes(ENTRY_Q)
    mv.set_index_spot("AAA", spot)                   # CE 1050 breached → roll to a 30Δ PE
    out = sess.run_decision(datetime(2026, 1, 5, 10, 0))
    expk = int(min(strikes, key=lambda k: abs(abs(bs.delta(spot, k, t, r, sig, "PE")) - 0.30)))
    assert expk != 1100  # a 30Δ PE is OTM, not the ATM strike
    assert any(e["action"] == "SHORT" and e["ticker"] == f"AAA|2026-01-13|{expk}|PE" for e in out)


def test_flip_capped_once_per_day():
    strat = _strat(portfolio_sl_pct=2.0, breach_basis="touch", max_flips=5)  # high cap so it doesn't close
    sess, mv = _session(strat)
    sess.update_quotes(ENTRY_Q)
    sess.run_decision(datetime(2026, 1, 5, 9, 50))      # enter
    sess.update_quotes(ENTRY_Q)
    mv.set_index_spot("AAA", 1100.0)                      # CE 1050 breached → roll to ATM PE 1100
    sess.run_decision(datetime(2026, 1, 5, 10, 0))
    assert strat.flip_count["AAA"] == 1
    # Same day, the rolled ATM PE 1100 is now breached (spot well below it) — but the daily cap blocks it.
    sess.update_quotes(ENTRY_Q)
    mv.set_index_spot("AAA", 1050.0)
    sess.run_decision(datetime(2026, 1, 5, 14, 0))
    assert strat.flip_count["AAA"] == 1                   # no second flip the same day
    # Next trading day, the breach flips again.
    sess.update_quotes(ENTRY_Q)
    mv.set_index_spot("AAA", 1050.0)
    sess.run_decision(datetime(2026, 1, 6, 10, 0))
    assert strat.flip_count["AAA"] == 2


def test_breach_buffer_skips_marginal_touch():
    strat = _strat(portfolio_sl_pct=2.0, breach_basis="touch", breach_buffer_pct=1.0)
    sess, mv = _session(strat)
    sess.update_quotes(ENTRY_Q)
    sess.run_decision(datetime(2026, 1, 5, 9, 50))
    sess.update_quotes(ENTRY_Q)
    mv.set_index_spot("AAA", 1055.0)                      # CE 1050: 1055 < 1050×1.01 (=1060.5) → within buffer
    sess.run_decision(datetime(2026, 1, 5, 10, 0))
    assert strat.flip_count.get("AAA", 0) == 0           # marginal touch → no flip
    sess.update_quotes(ENTRY_Q)
    mv.set_index_spot("AAA", 1065.0)                      # clears 1050×1.01 → real breach → flip
    sess.run_decision(datetime(2026, 1, 5, 11, 0))
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


def test_basket_status_reports_per_name_and_payoff():
    strat = _strat(portfolio_sl_pct=2.0, breach_basis="touch")
    sess, mv = _session(strat)
    sess.update_quotes(ENTRY_Q)
    sess.run_decision(datetime(2026, 1, 5, 9, 50))
    mv.set_index_spot("AAA", 1010.0)   # inside the strikes → no breach
    mv.set_index_spot("NIFTY", 25000.0)
    sess.update_quotes(ENTRY_Q)
    st = strat.basket_status(mv, sess.portfolio)
    aaa = next(n for n in st["names"] if n["symbol"] == "AAA")
    assert aaa["status"] == "open" and len([leg for leg in aaa["legs"] if leg["open"]]) == 2
    # Name-level aggregate (CE+PE clubbed): units, entry credit collected, struct, lot size, realized.
    assert aaa["units"] == 100 and aaa["credit"] == (20 + 18) * 100
    assert aaa["struct"] == "strangle" and aaa["lot_size"] == 100 and aaa["lots"] == 1 and aaa["realized"] == 0.0
    assert all(leg["side"].startswith("SELL ") and "state" in leg for leg in aaa["legs"])
    # Aggregates + hedge enrichment used by the redesign.
    assert st["net_credit"] == aaa["credit"] and st["hedge"]["entry_notional"] == 100000.0 and st["hedge"]["legs"]
    assert st["combined_mtm"] == st["basket_mtm"] + st["hedge_mtm"] and "portfolio_stop_amount" in st
    assert st["hedge"]["lots"] == 1 and st["hedge"]["cost"] == (50 + 50) * 50  # NIFTY hedge debit paid
    assert len(st["payoff"]) == 31 and any(p["move_pct"] == 0 for p in st["payoff"])


def test_basket_status_marks_flipped_name():
    strat = _strat(portfolio_sl_pct=2.0, breach_basis="touch")
    sess, mv = _session(strat)
    sess.update_quotes(ENTRY_Q)
    sess.run_decision(datetime(2026, 1, 5, 9, 50))
    sess.update_quotes(ENTRY_Q)
    mv.set_index_spot("AAA", 1100.0)
    sess.run_decision(datetime(2026, 1, 5, 10, 0))  # roll
    aaa = next(n for n in strat.basket_status(mv, sess.portfolio)["names"] if n["symbol"] == "AAA")
    assert aaa["status"] == "flipped" and aaa["flip_count"] == 1


def test_marks_persist_across_restart():
    strat = _strat()
    sess, _mv = _session(strat)
    sess.update_quotes(ENTRY_Q)
    sess.run_decision(datetime(2026, 1, 5, 9, 50))   # enter
    sess.update_quotes({SYMS["aaa_ce"]: 30.0})        # a fresh live quote moves a leg's mark
    state = sess.export_state()
    assert state["marks"].get(SYMS["aaa_ce"]) == 30.0  # last live quote is persisted

    # Rebuild a fresh session (simulates a restart) with NO quotes + NO live chain, restore state.
    strat2 = _strat()
    sess2, mv2 = _session(strat2)
    sess2.load_state(state)
    assert mv2.close(SYMS["aaa_ce"]) == 30.0           # priced off the restored last live quote
    assert mv2.has_print(SYMS["aaa_ce"]) is False      # restored as a forward-filled mark, not a fresh tick


def test_settles_at_expiry():
    strat = _strat()
    sess, _mv = _session(strat)
    sess.update_quotes(ENTRY_Q)
    sess.run_decision(datetime(2026, 1, 5, 9, 50))
    assert sess.portfolio.lot_symbols()
    settle = sess.run_decision(datetime(2026, 1, 13, 15, 31))  # expiry day, past the 15:30 cutoff
    assert any(e["action"] == "SETTLE" for e in settle)
    assert not sess.portfolio.lot_symbols() and strat.done


# ────────────────────────────── entry gates ported from the backtest loss study

def test_analyze_name_tight_channel_gate():
    # Default _ohlc: range 900–1100 on spot 1000 → width 20%. A 25% floor excludes it;
    # legs are KEPT (excluded rows stay deployable via manual override, like IVP).
    row = _analyze(params=DonchianParams(min_channel_width_pct=25.0))
    assert row["status"] == "excluded:filter" and "channel 20.0%" in row["reason"]
    assert row["ce"] is not None and row["pe"] is not None
    assert row["width_pct"] == pytest.approx(20.0)
    # Below the floor it passes untouched.
    assert _analyze(params=DonchianParams(min_channel_width_pct=8.0))["status"] == "strangle"


def test_analyze_name_vol_compression_gate():
    # Closes: 60 lively bars (±2%) then 20 flat bars → HV20 collapses vs HV60 (squeeze).
    n = 80
    days = [date(2025, 9, 1) + timedelta(days=i) for i in range(n)]
    closes = [1000 * (1 + 0.02 * ((i % 2) * 2 - 1)) for i in range(60)] + [1000.0] * 20
    df = pd.DataFrame({"date": days, "open": closes, "high": [c + 5 for c in closes],
                       "low": [c - 5 for c in closes], "close": closes})
    row = _analyze(df=df, range_start=date(2025, 9, 1), range_end=date(2025, 11, 19),
                   params=DonchianParams(min_hv_ratio=0.85))
    assert row["hv_ratio"] is not None and row["hv_ratio"] < 0.85
    assert row["status"] == "excluded:filter" and "vol squeeze" in row["reason"]
    # The same name passes with the gate off (and the ratio is still reported).
    ungated = _analyze(df=df, range_start=date(2025, 9, 1), range_end=date(2025, 11, 19))
    assert ungated["status"] == "strangle" and ungated["hv_ratio"] < 0.85
