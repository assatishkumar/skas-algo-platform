"""Staggered Covered Call: tranche accumulation, roll-down, and expiry handling.

Drives the real StaggeredCoveredCallStrategy + options engine against a fake GOLD
chain (monthly expiries, premium smooth in moneyness × time) plus a GOLDBEES series
served through the new ``equity_loader`` seam. Scenarios: entry (short CE ~6% OTM +
T1 ≈ ⅓ of the notional-matched ETF units), rising spot firing T2/T3 at the GTT
levels, ITM expiry (cash-settle → ETF liquidated → fresh cycle), falling spot
rolling the CE down (~80% premium captured) with triggers re-anchored, and OTM
expiry keeping the tranches into the next cycle.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd

from skas_algo.data.options_provider import build_options_run
from skas_algo.engine.options.charges import ChargeModel
from skas_algo.engine.options.chain import ChainRow
from skas_algo.engine.options.instrument import make
from skas_algo.engine.options.report import build_options_report
from skas_algo.engine.runner import BacktestRunner
from skas_algo.strategies.staggered_covered_call import StaggeredCoveredCallStrategy

EXPIRY1 = date(2026, 2, 26)
EXPIRY2 = date(2026, 3, 26)
BASE = 100000.0  # GOLD futures ₹/10g; GOLDBEES tracks at spot/1000


def _biz_days(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _prem(strike: float, dte: int, spot: float, right: str = "CE") -> float:
    """Premium smooth in moneyness and time: ~419 at entry for a 6% OTM call, 31 DTE.
    Symmetric for puts (distance below spot)."""
    dist = (strike - spot) if right == "CE" else (spot - strike)
    return round(3000.0 * math.exp(-dist / 3000.0) * max(0.05, dte / 30.0), 2)


class FakeGoldSD:
    """skas-data lookalike: scripted GOLD spot path + monthly CE chain + GOLDBEES ETF."""

    def __init__(self, calendar, spot_fn, expiries=(EXPIRY1, EXPIRY2)):
        self.cal = calendar
        self.spot_fn = spot_fn
        self.expiries = list(expiries)
        self.strikes = [90000.0 + 500 * i for i in range(0, 53)]  # 90000..116000

    def _frame(self, closes):
        return pd.DataFrame({"date": self.cal, "close": closes})

    def get_prices(self, symbol, start_date=None, end_date=None, asset_type="stock"):
        if symbol == "GOLD":
            df = self._frame([self.spot_fn(d) for d in self.cal])
        elif symbol == "GOLDBEES":
            df = self._frame([self.spot_fn(d) / 1000.0 for d in self.cal])
        else:
            return None
        if start_date is not None:
            df = df[df["date"] >= start_date]
        if end_date is not None:
            df = df[df["date"] <= end_date]
        return df.reset_index(drop=True)

    def get_option_chain(self, underlying, on_date, expiry=None):
        spot = self.spot_fn(on_date)
        rows = [dict(trade_date=on_date, symbol="GOLD", expiry_date=e, strike_price=k,
                     option_type=right, close=_prem(k, (e - on_date).days, spot, right),
                     settle_price=_prem(k, (e - on_date).days, spot, right), open_interest=1000)
                for e in self.expiries if e >= on_date for k in self.strikes for right in ("CE", "PE")]
        return pd.DataFrame(rows)

    def get_option_series(self, underlying, expiry, strike, option_type, start_date=None, end_date=None):
        rows = [{"trade_date": d,
                 "close": _prem(float(strike), (expiry - d).days, self.spot_fn(d), option_type.upper())}
                for d in self.cal if d <= expiry
                if (start_date is None or d >= start_date) and (end_date is None or d <= end_date)]
        return pd.DataFrame(rows)


def _run(strategy, calendar, spot_fn):
    sd = FakeGoldSD(calendar, spot_fn)
    equity_loader = lambda sym, lo, hi: sd.get_prices(symbol=sym, start_date=lo, end_date=hi)
    mv, _chain, settler, margin = build_options_run(
        sd, "GOLD", calendar[0], calendar[-1], equity_loader=equity_loader)
    runner = BacktestRunner(
        strategy=strategy, universe=["GOLD"], loader=lambda *a: None,
        initial_capital=2_000_000, tax_rate=0.0,
        market_view=mv, settler=settler, margin_model=margin,
        charge_model=ChargeModel(),  # mirror the API path so net_after_charges reconciles
    )
    return runner.run(calendar[0], calendar[-1])


def _interp(d: date, d0: date, v0: float, d1: date, v1: float) -> float:
    f = (d - d0).days / (d1 - d0).days
    return v0 + (v1 - v0) * f


# --------------------------------------------------------------- rising → ITM
def _spot_rising(d: date) -> float:
    pts = {date(2026, 1, 26): 100000.0, date(2026, 1, 27): 100500.0,
           date(2026, 1, 28): 102100.0, date(2026, 1, 29): 103000.0,
           date(2026, 1, 30): 104200.0}
    if d in pts:
        return pts[d]
    if d <= date(2026, 1, 30):
        return 100000.0
    if d <= EXPIRY1:
        return round(_interp(d, date(2026, 1, 30), 104200.0, EXPIRY1, 107000.0), 2)
    return 107000.0  # past expiry: stays ITM


def test_entry_short_ce_and_t1_third_of_notional():
    cal = _biz_days(date(2026, 1, 26), date(2026, 1, 27))
    strat = StaggeredCoveredCallStrategy(universe=["GOLD"], initial_capital=2_000_000)
    result = _run(strat, cal, _spot_rising)
    txns = result.transactions
    shorts = [t for t in txns if t["action"] == "SHORT"]
    buys = [t for t in txns if t["action"] in ("BUY", "AVG_BUY")]

    assert len(shorts) == 1 and shorts[0]["ticker"] == "GOLD|2026-02-26|106000|CE"
    assert shorts[0]["units"] == 10  # 1 GOLDM lot, multiplier 10
    assert len(buys) == 1 and buys[0]["ticker"] == "GOLDBEES"
    # full = round(10 × 100000 / 100) = 10,000 units; T1 = 3,334 (⅓ + remainder).
    assert buys[0]["units"] == 3334 and buys[0]["tag"] == "cc_t1"
    # ~67% naked initially — stamped in state, never claimed covered.
    state = strat.export_state()
    assert abs(state["naked_fraction"] - (1 - 3334 / 10000)) < 1e-6
    assert len(strat.triggers) == 2  # T2 at S+(K−S)/3, T3 at S+2(K−S)/3
    assert [round(t["level"]) for t in strat.triggers] == [102000, 104000]


def test_rising_path_fires_t2_t3_then_itm_calls_away_and_restarts():
    cal = _biz_days(date(2026, 1, 26), date(2026, 3, 2))
    strat = StaggeredCoveredCallStrategy(universe=["GOLD"], initial_capital=2_000_000)
    result = _run(strat, cal, _spot_rising)
    txns = result.transactions

    t2 = next(t for t in txns if t["tag"] == "cc_t2")
    t3 = next(t for t in txns if t["tag"] == "cc_t3")
    assert t2["date"] == pd.Timestamp(date(2026, 1, 28)) and t2["units"] == 3333
    assert t3["date"] == pd.Timestamp(date(2026, 1, 30)) and t3["units"] == 3333

    # Expiry: CE cash-settles to intrinsic 107000−106000 = 1000.
    settle = next(t for t in txns if t["action"] == "SETTLE")
    assert settle["date"] == pd.Timestamp(EXPIRY1) and settle["price"] == 1000.0

    # Called away: the full 10,000 ETF units liquidated, then a fresh cycle (new
    # monthly CE + a new T1) starts the same day.
    sell = next(t for t in txns if t["action"] == "SELL" and t["ticker"] == "GOLDBEES")
    assert sell["date"] == pd.Timestamp(EXPIRY1) and sell["units"] == 10000
    assert sell["exit_reason"] == "cc_called_away"
    shorts = [t for t in txns if t["action"] == "SHORT"]
    assert len(shorts) == 2 and shorts[1]["ticker"].split("|")[1] == "2026-03-26"
    rebuy = [t for t in txns if t["tag"] == "cc_t1"]
    assert len(rebuy) == 2 and rebuy[1]["date"] == pd.Timestamp(EXPIRY1)


# --------------------------------------------------------------- falling → roll-down
def _spot_falling(d: date) -> float:
    pts = {date(2026, 1, 26): 100000.0, date(2026, 1, 27): 99500.0,
           date(2026, 1, 28): 99000.0, date(2026, 1, 29): 98000.0,
           date(2026, 1, 30): 97000.0, date(2026, 2, 2): 96200.0}
    if d in pts:
        return pts[d]
    return 95500.0  # Feb 3 onward


def test_falling_path_rolls_the_ce_down_and_reanchors_triggers():
    cal = _biz_days(date(2026, 1, 26), date(2026, 2, 6))
    strat = StaggeredCoveredCallStrategy(universe=["GOLD"], initial_capital=2_000_000)
    result = _run(strat, cal, _spot_falling)
    txns = result.transactions

    # ~80% of the ₹419 entry premium captured on Feb 3 (price ≤ 20% of entry) → roll.
    cover = next(t for t in txns if t["action"] == "COVER")
    assert cover["date"] == pd.Timestamp(date(2026, 2, 3))
    assert cover["exit_reason"] == "cc_rolldown_close"
    assert cover["ticker"] == "GOLD|2026-02-26|106000|CE"

    shorts = [t for t in txns if t["action"] == "SHORT"]
    assert len(shorts) == 2
    # New strike snaps to ~6% OTM of the NEW spot (1.06 × 95500 → 101000), same expiry.
    assert shorts[1]["ticker"] == "GOLD|2026-02-26|101000|CE"
    assert shorts[1]["date"] == pd.Timestamp(date(2026, 2, 3))

    # Unfired T2/T3 re-anchor to (S_now, K_new): 95500 + i/3 × 5500.
    assert [round(t["level"]) for t in strat.triggers] == [97333, 99167]
    # Spot never rose — no tranche beyond T1 fired.
    assert not [t for t in txns if t["tag"] in ("cc_t2", "cc_t3")]


# --------------------------------------------------------------- OTM expiry keeps tranches
def test_otm_expiry_keeps_tranches_and_recycles():
    cal = _biz_days(date(2026, 1, 26), date(2026, 3, 2))
    strat = StaggeredCoveredCallStrategy(
        universe=["GOLD"], initial_capital=2_000_000,
        rolldown_min_dte=10,  # flat-spot theta decay would fake a roll-down at low DTE
    )
    result = _run(strat, cal, lambda d: BASE)
    txns = result.transactions

    settle = next(t for t in txns if t["action"] == "SETTLE")
    assert settle["date"] == pd.Timestamp(EXPIRY1) and settle["price"] == 0.0  # OTM

    # Tranches KEPT: no ETF sell anywhere; held units roll into the next cycle.
    assert not [t for t in txns if t["action"] == "SELL" and t["ticker"] == "GOLDBEES"]
    shorts = [t for t in txns if t["action"] == "SHORT"]
    assert len(shorts) == 2 and shorts[1]["ticker"] == "GOLD|2026-03-26|106000|CE"
    # Held T1 counts as a fired tranche → no second cc_t1 buy.
    assert len([t for t in txns if t["tag"] == "cc_t1"]) == 1
    assert strat.held_units == 3334
    assert [t["ordinal"] for t in strat.triggers] == [1, 2]


# --------------------------------------------------------------- premium floor
def test_premium_floor_walks_to_a_nearer_strike():
    # A chain where the 6%-OTM call is worthless (~₹2) but nearer strikes pay premium.
    spot, exp = 40000.0, EXPIRY1
    prices = {40000.0 + 500 * i: max(800.0 - 180.0 * i, 2.0) for i in range(0, 9)}  # 40000:800 … 44000:2
    rows = {k: ChainRow("GOLD", exp, k, "CE", p, p, 1000, make("GOLD", exp, k, "CE").symbol)
            for k, p in prices.items()}
    s = StaggeredCoveredCallStrategy(universe=["GOLD"], min_premium_pct=0.005, min_ce_otm_pct=2.0)
    k, row, met = s._select_ce_strike(rows, spot)
    # 6% OTM (42500) prices ₹2 < floor ₹200; walk down 42000(₹80)<200 → 41500(₹260)≥200.
    assert k == 41500 and row.close >= 200 and met is True
    # Never nearer than min_ce_otm_pct (2% ⇒ ≥ 40800) even if the floor isn't met.
    assert k >= spot * 1.02
    # Floor OFF reproduces the plain ce_otm_pct strike (legacy behaviour).
    s0 = StaggeredCoveredCallStrategy(universe=["GOLD"], min_premium_pct=0.0)
    assert s0._select_ce_strike(rows, spot)[0] == 42500


def test_strike_floored_at_equity_cost_basis():
    # Underwater: spot 80000 but the held ETF cost basis is at index ~100000. A call sold
    # at the 6%-OTM target (~84800) would, on a recovery, be assigned BELOW cost → a loss.
    spot, exp = 80000.0, EXPIRY1
    prices = {80000.0 + 500 * i: max(3000.0 * math.exp(-(500 * i) / 3000.0), 1.0) for i in range(0, 60)}
    rows = {k: ChainRow("GOLD", exp, k, "CE", p, p, 1000, make("GOLD", exp, k, "CE").symbol)
            for k, p in prices.items()}
    s = StaggeredCoveredCallStrategy(universe=["GOLD"])
    # With the cost floor at 100000, no strike below cost is allowed → nearest ≥ cost.
    k, _row, _met = s._select_ce_strike(rows, spot, floor_strike=100000.0)
    assert k >= 100000.0
    # No floor → the plain ~6%-OTM strike (≈ 84800 → snaps to 85000).
    k0, _r0, _m0 = s._select_ce_strike(rows, spot, floor_strike=0.0)
    assert abs(k0 - 84800) <= 500


def test_guard_keeps_equity_held_instead_of_called_away_at_a_loss():
    # The 2020-NIFTY pattern: rally (all 3 tranches fill HIGH, avg cost ≈ index 102000)
    # → crash to 80000 → partial recovery to 95000. Unguarded, the call rolls DOWN below
    # cost (~85000) and the recovery assigns it at 95000 < cost → a called-away LOSS.
    # The cost guard must DECLINE those rolls, keep the original 106000 call (expires OTM
    # at 95000), and keep the ETF — no loss is locked in.
    def spot_crash_recover(d):
        if d <= date(2026, 1, 26):
            return 100000.0
        if d <= date(2026, 1, 30):   # rally → fires T2 (102000) and T3 (104000)
            return round(_interp(d, date(2026, 1, 26), 100000.0, date(2026, 1, 30), 104500.0), 2)
        if d <= date(2026, 2, 13):   # crash
            return round(_interp(d, date(2026, 1, 30), 104500.0, date(2026, 2, 13), 80000.0), 2)
        if d <= EXPIRY1:             # partial recovery, stays under the 106000 strike
            return round(_interp(d, date(2026, 2, 13), 80000.0, EXPIRY1, 95000.0), 2)
        return 95000.0

    cal = _biz_days(date(2026, 1, 26), date(2026, 3, 2))
    guarded = StaggeredCoveredCallStrategy(universe=["GOLD"], initial_capital=2_000_000)
    res_on = _run(guarded, cal, spot_crash_recover)
    called_away_on = [t for t in res_on.transactions
                      if t.get("exit_reason") == "cc_called_away"]
    assert not called_away_on, "cost guard must not let the ETF be called away below cost"

    unguarded = StaggeredCoveredCallStrategy(universe=["GOLD"], initial_capital=2_000_000,
                                             keep_strike_above_cost=False)
    res_off = _run(unguarded, cal, spot_crash_recover)
    rep_off = build_options_report(res_off, 2_000_000, {})
    # Without the guard the call rolls down below cost and the recovery assigns it at a loss.
    loss_legs = [l for l in rep_off.get("equity_legs", [])
                 if l["exit_reason"] == "cc_called_away" and l["realized_pnl"] < 0]
    assert loss_legs, "without the guard, a below-cost roll-down books a called-away loss"


def test_premium_floor_unmet_reports_false_so_rolldown_declines():
    # Whole chain near-worthless (e.g. days to expiry): no strike clears the floor →
    # met=False, so a roll-down won't churn into another ~0 call (it keeps riding).
    spot, exp = 40000.0, EXPIRY1
    rows = {40000.0 + 500 * i: ChainRow("GOLD", exp, 40000.0 + 500 * i, "CE", 1.0, 1.0, 1000,
            make("GOLD", exp, 40000.0 + 500 * i, "CE").symbol) for i in range(0, 9)}
    s = StaggeredCoveredCallStrategy(universe=["GOLD"], min_premium_pct=0.005)
    k, row, met = s._select_ce_strike(rows, spot)
    assert k is not None and met is False  # best-effort strike returned, but floor unmet


# --------------------------------------------------------------- equity legs in report
def test_report_includes_equity_round_trip_and_combined_net():
    cal = _biz_days(date(2026, 1, 26), date(2026, 3, 2))
    strat = StaggeredCoveredCallStrategy(universe=["GOLD"], initial_capital=2_000_000)
    result = _run(strat, cal, _spot_rising)
    rep = build_options_report(result, 2_000_000, {})
    assert rep is not None

    legs = rep.get("equity_legs")
    assert legs, "the covered leg (ETF) must appear in the options report"
    rt = legs[0]
    assert rt["symbol"] == "GOLDBEES" and rt["units"] == 10000
    assert rt["exit_reason"] == "cc_called_away" and rt["realized_pnl"] > 0  # rising → equity gain booked
    assert sum(tr["units"] for tr in rt["tranches"]) == 10000  # accumulation history shown
    assert {tr["tag"] for tr in rt["tranches"]} == {"cc_t1", "cc_t2", "cc_t3"}

    # The new cycle's tranche is still held at the run end → marked to the last close.
    held = rep.get("equity_held")
    assert held and held[0]["symbol"] == "GOLDBEES" and held[0]["units"] == 3334
    assert held[0]["mark"] is not None

    # Summary folds the covered leg into a combined net (option realized net + open
    # option MTM + equity realized + equity open) that reconciles to the equity curve.
    s = rep["summary"]
    assert s["equity_realized_pnl"] == sum(l["realized_pnl"] for l in legs)
    assert abs(s["strategy_net_pnl"]
               - (s["net_after_charges"] + s["option_open_pnl"]
                  + s["equity_realized_pnl"] + s["equity_open_pnl"])) < 1e-6
    # Combined net == Final Equity − capital (full reconciliation with the run).
    final_equity = result.portfolio.cash + result.portfolio.holdings_value(result.final_marks)
    assert abs(s["strategy_net_pnl"] - (final_equity - 2_000_000)) < 1.0


def test_report_groups_into_campaigns_with_calls_and_combined_net():
    cal = _biz_days(date(2026, 1, 26), date(2026, 3, 2))
    strat = StaggeredCoveredCallStrategy(universe=["GOLD"], initial_capital=2_000_000)
    result = _run(strat, cal, _spot_rising)
    rep = build_options_report(result, 2_000_000, {})
    camps = rep.get("campaigns")
    assert camps, "covered-call report must group into campaigns"

    # First campaign = the accumulation that gets called away; it owns the calls sold
    # while it was live and reports a combined net (equity + option).
    c0 = camps[0]
    assert c0["status"] == "called_away" and c0["start"] == "2026-01-26"
    assert {tr["tag"] for tr in c0["tranches"]} == {"cc_t1", "cc_t2", "cc_t3"}
    assert c0["n_calls"] >= 1 and all(call["strike"] > 0 for call in c0["calls"])
    assert abs(c0["combined_net"]
               - (c0["equity_realized"] + c0["equity_open"] + c0["option_net"])) < 1e-6
    # Every call in a campaign was sold within its [start, end] window.
    for c in camps:
        for call in c["calls"]:
            assert call["entry_date"] >= c["start"]
            if c["end"]:
                assert call["entry_date"] <= c["end"]
    # The last campaign is the still-open holding (rising path re-entered after called away).
    assert camps[-1]["status"] == "open"


def test_report_marks_held_tranches_when_otm_expiry_keeps_them():
    cal = _biz_days(date(2026, 1, 26), date(2026, 3, 2))
    strat = StaggeredCoveredCallStrategy(universe=["GOLD"], initial_capital=2_000_000,
                                         rolldown_min_dte=10)
    result = _run(strat, cal, lambda d: BASE)
    rep = build_options_report(result, 2_000_000, {})
    assert not rep.get("equity_legs")  # OTM expiry kept the tranches — nothing sold
    held = rep.get("equity_held")
    assert held and held[0]["units"] == 3334
    assert held[0]["mark"] == 100.0  # BASE/1000, flat spot
    assert abs(held[0]["unrealized_pnl"]) < 1e-6


# --------------------------------------------------------------- profit levers
def test_min_return_floors_strike_above_cost_plus_margin():
    # min_return_pct lifts the cost floor: a 3% min return on a cost basis at index 100000
    # requires the strike ≥ 103000.
    s = StaggeredCoveredCallStrategy(universe=["GOLD"], min_return_pct=3.0)
    s.held_units, s.held_cost = 1000, 100_000.0  # avg cost ₹100 (ETF)
    # spot 100000, etf 100 → ratio 1000 → cost-index 100000 ×1.03 = 103000.
    assert abs(s._cost_floor_strike(100000.0, 100.0) - 103000.0) < 1e-6
    # guard off → no floor
    s.keep_strike_above_cost = False
    assert s._cost_floor_strike(100000.0, 100.0) == 0.0


def test_covered_call_delta_targets_a_closer_strike_when_fully_covered():
    import math
    from skas_algo.engine.options import black_scholes as bs
    spot, t, exp = 40000.0, 30 / 365.0, EXPIRY1
    # Chain priced at a known IV so deltas are well-defined.
    prices = {40000.0 + 500 * i: max(bs.price(spot, 40000.0 + 500 * i, t, 0.065, 0.18, "CE"), 0.05)
              for i in range(0, 40)}
    rows = {k: ChainRow("GOLD", exp, k, "CE", p, p, 1000, make("GOLD", exp, k, "CE").symbol)
            for k, p in prices.items()}
    s = StaggeredCoveredCallStrategy(universe=["GOLD"], covered_call_delta=0.30, ce_otm_pct=6.0)
    # Not yet fully covered → fixed 6%-OTM (~42400 → 42500).
    s.full_units, s.held_units = 1000, 333
    assert abs(s._ce_target_strike(rows, spot, t) - 42500) <= 500
    # Fully covered → the ~0.30Δ strike (closer to ATM than 6% OTM → lower strike).
    s.held_units = 1000
    kd = s._ce_target_strike(rows, spot, t)
    d = abs(bs.delta(spot, kd, t, 0.065, 0.18, "CE"))
    assert 0.22 <= d <= 0.38 and kd < 42500  # richer (closer) strike


def test_wheel_sells_puts_and_accumulates_on_assignment():
    # Spot drifts DOWN so the sold puts finish ITM → the wheel accumulates via assignment
    # (cc_put_assigned) rather than GTT up-buys, and books put premium.
    cal = _biz_days(date(2026, 1, 26), date(2026, 4, 30))
    strat = StaggeredCoveredCallStrategy(universe=["GOLD"], initial_capital=2_000_000,
                                         sell_puts=True, put_otm_pct=2.0, rolldown_min_dte=10)
    # gentle downward drift across the cycles
    def spot_drift(d):
        base = 100000.0 - 12000.0 * ((d - date(2026, 1, 26)).days / 94.0)
        return round(max(base, 80000.0), 2)
    result = _run(strat, cal, spot_drift)
    txns = result.transactions
    assert any(t["action"] == "SHORT" and t["ticker"].endswith("|PE") for t in txns), "wheel must sell puts"
    assert any(t.get("tag") == "cc_put_assigned" for t in txns), "ITM puts must accumulate ETF"
    # No GTT up-buys in wheel mode (accumulation is via puts).
    assert not any(t.get("tag") in ("cc_t2", "cc_t3") for t in txns)


# --------------------------------------------------------------- unit bits
def test_etf_auto_mapping_and_state_roundtrip():
    s = StaggeredCoveredCallStrategy(universe=["NIFTY"], initial_capital=1_000_000)
    assert s.etf_symbol == "NIFTYBEES"
    s.ce = {"symbol": "NIFTY|2026-02-24|26500|CE", "units": 65, "entry": 120.0,
            "strike": 26500.0, "expiry": date(2026, 2, 24)}
    s.held_units, s.full_units, s.tranche_units = 3334, 10000, 3333
    s.triggers = [{"level": 102000.0, "ordinal": 1}]
    state = s.export_state()
    s2 = StaggeredCoveredCallStrategy(universe=["NIFTY"], initial_capital=1_000_000)
    s2.load_state(state)
    assert s2.ce == s.ce and s2.held_units == 3334
    assert s2.triggers == s.triggers and abs(state["naked_fraction"] - 0.6666) < 1e-3
