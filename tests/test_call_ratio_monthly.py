"""Call Ratio Monthly: entry structure, exits, and a long+short options report.

Drives the real CallRatioMonthlyStrategy + options engine against a fake skas-data source
(monthly CE chain, no DB/network), asserting the 3-leg structure is entered on the last
Tuesday, exits on the time rule, and the options report reconstructs all legs (long + short).
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd

from skas_algo.data.options_provider import build_options_run
from skas_algo.engine.options.chain import ChainRow
from skas_algo.engine.options.instrument import make
from skas_algo.engine.options.report import build_options_report
from skas_algo.engine.runner import BacktestRunner
from skas_algo.strategies.call_ratio_monthly import (
    BatmanRatioMonthlyStrategy,
    CallRatioMonthlyStrategy,
    PutRatioMonthlyStrategy,
)

SPOT = 21000.0
EXPIRY = date(2024, 2, 29)  # Feb 2024 monthly (last Thursday)


def _biz_days(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


CALENDAR = _biz_days(date(2024, 1, 25), date(2024, 2, 23))  # entry Jan 30 (last Tue), time-exit mid-Feb


def _prem(strike: float, dte: int, right: str = "CE") -> float:
    """A smooth OTM premium decaying with moneyness and time (small net credit at base).
    Distance is above spot for CE, below spot for PE — symmetric curves."""
    dist = (strike - SPOT) if right == "CE" else (SPOT - strike)
    return round(100.0 * math.exp(-dist / 800.0) * max(0.05, dte / 30.0), 2)


def _ce(strike: float, dte: int) -> float:
    return _prem(strike, dte, "CE")


class FakeCRSD:
    def __init__(self, calendar):
        self.cal = calendar
        self.index = pd.DataFrame({"date": calendar, "close": [SPOT] * len(calendar)})
        self.strikes = [20000.0 + 50 * i for i in range(-40, 120)]  # 18000..25950

    def get_prices(self, symbol, start_date=None, end_date=None, asset_type="stock"):
        df = self.index
        if start_date is not None:
            df = df[df["date"] >= start_date]
        if end_date is not None:
            df = df[df["date"] <= end_date]
        return df.reset_index(drop=True)

    def get_option_chain(self, underlying, on_date, expiry=None):
        dte = (EXPIRY - on_date).days
        rows = [dict(trade_date=on_date, symbol="NIFTY", expiry_date=EXPIRY, strike_price=k,
                     option_type=right, close=_prem(k, dte, right),
                     settle_price=_prem(k, dte, right), open_interest=1000)
                for k in self.strikes for right in ("CE", "PE")]
        return pd.DataFrame(rows)

    def get_option_series(self, underlying, expiry, strike, option_type, start_date=None, end_date=None):
        rows = [{"trade_date": d, "close": _prem(float(strike), (EXPIRY - d).days, option_type.upper())}
                for d in self.cal
                if (start_date is None or d >= start_date) and (end_date is None or d <= end_date)]
        return pd.DataFrame(rows)


def _run(strategy):
    sd = FakeCRSD(CALENDAR)
    mv, _chain, settler, margin = build_options_run(sd, "NIFTY", CALENDAR[0], CALENDAR[-1])
    runner = BacktestRunner(
        strategy=strategy, universe=["NIFTY"], loader=lambda *a: None,
        initial_capital=100_000, tax_rate=0.0,
        market_view=mv, settler=settler, margin_model=margin,
    )
    return runner.run(CALENDAR[0], CALENDAR[-1])


def test_entry_structure_and_time_exit():
    strat = CallRatioMonthlyStrategy(
        universe=["NIFTY"], initial_capital=100_000, max_holding_days=15, min_dte=18,
    )
    result = _run(strat)
    txns = result.transactions
    buys = [t for t in txns if t["action"] == "BUY"]
    shorts = [t for t in txns if t["action"] == "SHORT"]

    # One entry: 2 long legs (near + hedge) + 1 short leg (the body, 2× units).
    assert len(buys) == 2, [(t["action"], t["ticker"]) for t in txns]
    assert len(shorts) == 1
    near_k = min(int(t["ticker"].split("|")[2]) for t in buys)
    hedge_k = max(int(t["ticker"].split("|")[2]) for t in buys)
    sell_k = int(shorts[0]["ticker"].split("|")[2])
    assert near_k == 21300 and sell_k == 21600 and hedge_k == 22600  # spot+300/+600/+1600
    assert shorts[0]["units"] == 2 * 50  # 2 lots short; NIFTY lot was 50 for a Feb-2024 expiry

    # It closed (time exit before expiry → SELL on the longs, COVER on the short; not SETTLE).
    assert any(t["action"] == "COVER" for t in txns)
    assert any(t["action"] == "SELL" for t in txns)


def test_put_ratio_mirrors_below_spot():
    strat = PutRatioMonthlyStrategy(
        universe=["NIFTY"], initial_capital=100_000, max_holding_days=15, min_dte=18,
    )
    result = _run(strat)
    txns = result.transactions
    buys = [t for t in txns if t["action"] == "BUY"]
    shorts = [t for t in txns if t["action"] == "SHORT"]

    assert len(buys) == 2 and len(shorts) == 1, [(t["action"], t["ticker"]) for t in txns]
    assert all(t["ticker"].endswith("|PE") for t in buys + shorts)
    near_k = max(int(t["ticker"].split("|")[2]) for t in buys)
    hedge_k = min(int(t["ticker"].split("|")[2]) for t in buys)
    sell_k = int(shorts[0]["ticker"].split("|")[2])
    # spot−300 / −600 / −1600: the downside mirror of the call structure.
    assert near_k == 20700 and sell_k == 20400 and hedge_k == 19400
    assert shorts[0]["units"] == 2 * 50

    # Closed via time exit (spot flat → puts decayed; SELL longs + COVER short).
    assert any(t["action"] == "COVER" for t in txns)
    assert any(t["action"] == "SELL" for t in txns)


def test_batman_enters_both_wings():
    strat = BatmanRatioMonthlyStrategy(
        universe=["NIFTY"], initial_capital=200_000, max_holding_days=15, min_dte=18,
        tail_hedge_offset=0,  # un-tailed wing mechanics (tail default covered separately)
    )
    result = _run(strat)
    txns = result.transactions
    buys = [t for t in txns if t["action"] == "BUY"]
    shorts = [t for t in txns if t["action"] == "SHORT"]

    # 6 legs: 2 longs + 1 short (×2 units) per wing.
    assert len(buys) == 4 and len(shorts) == 2, [(t["action"], t["ticker"]) for t in txns]
    ce = sorted(int(t["ticker"].split("|")[2]) for t in buys + shorts if t["ticker"].endswith("|CE"))
    pe = sorted(int(t["ticker"].split("|")[2]) for t in buys + shorts if t["ticker"].endswith("|PE"))
    assert ce == [21300, 21600, 22600]  # call wing above spot
    assert pe == [19400, 20400, 20700]  # put wing below spot
    assert all(t["units"] == 2 * 50 for t in shorts)
    # One combined exit closes everything (time exit; spot flat → both wings decay).
    assert any(t["action"] == "COVER" for t in txns) and any(t["action"] == "SELL" for t in txns)


def test_batman_combined_credit_cap_reshifts_both_wings():
    # Base wings: credit ≈ ₹610 each (fits the 1% per-wing cap of ₹2,000) → combined
    # ₹1,220. A combined cap of 0.5% (₹1,000) must rebuild both wings further OTM
    # (half-cap per wing = ₹500 → 2 extra shifts each) so the sum fits.
    strat = BatmanRatioMonthlyStrategy(
        universe=["NIFTY"], initial_capital=200_000, max_holding_days=15, min_dte=18,
        combined_credit_limit_pct=0.005, tail_hedge_offset=0,
    )
    result = _run(strat)
    entries = [t for t in result.transactions if t["action"] in ("BUY", "SHORT")]
    ce = sorted(int(t["ticker"].split("|")[2]) for t in entries if t["ticker"].endswith("|CE"))
    pe = sorted(int(t["ticker"].split("|")[2]) for t in entries if t["ticker"].endswith("|PE"))
    assert ce == [21500, 21800, 22800]  # base +200 OTM
    assert pe == [19200, 20200, 20500]  # base −200 OTM
    # Combined entry credit respects the cap: Σ over wings of (2S−B−H)·units ≤ ₹1,000.
    credit = sum(-t["amount"] if t["action"] == "BUY" else t["amount"] for t in entries)
    assert 0 <= credit <= 1000, credit


def test_batman_requires_both_wings():
    # A CE-only chain (no PE rows) must skip — a single qualifying wing is not a Batman.
    prem = {21000.0 + 50 * i: 50.0 for i in range(-40, 80)}
    chain = _StubChain(21000.0, EXPIRY, prem)  # emits CE rows only
    s = BatmanRatioMonthlyStrategy(universe=["NIFTY"], initial_capital=200_000,
                                   credit_debit_limit_pct=99)
    assert s.on_slice(_StubCtx(chain, date(2024, 1, 30))) == []
    assert s.legs == []


def test_report_reconstructs_all_three_legs():
    strat = CallRatioMonthlyStrategy(
        universe=["NIFTY"], initial_capital=100_000, max_holding_days=15, min_dte=18,
    )
    result = _run(strat)
    rep = build_options_report(result, 100_000, {"Max Margin Used": 1.0})
    assert rep is not None
    assert len(rep["cycles"]) == 1
    legs = rep["cycles"][0]["legs_detail"]
    assert len(legs) == 3
    sides = sorted(leg["side"] for leg in legs)
    assert sides == ["long", "long", "short"]


def test_percent_mode_scales_strikes_with_spot():
    # % of spot keeps moneyness constant: the buy→sell strike gap scales with the index level.
    def legs_at(spot):
        chain = _StubChain(spot, EXPIRY, {spot + 50 * i: 50.0 for i in range(-40, 80)})
        s = CallRatioMonthlyStrategy(
            universe=["NIFTY"], initial_capital=100_000, strike_mode="percent",
            buy_offset=1.3, sell_offset=2.6, hedge_offset=7.0,
            credit_debit_limit_pct=99,  # premiums flat → net≈0, never gated
        )
        out = s.on_slice(_StubCtx(chain, date(2024, 1, 30)))
        assert out, f"no entry at spot {spot}"
        ks = sorted({int(sig.symbol.split("|")[2]) for sig in out})
        return ks[0], ks[-1]  # (buy, hedge)

    lo_buy, lo_hedge = legs_at(10000)
    hi_buy, hi_hedge = legs_at(24000)
    # buy ≈ +1.3%: 10130→snap & 24312→snap; the buy→hedge span scales ~2.4x with the level.
    assert (hi_hedge - hi_buy) > 1.8 * (lo_hedge - lo_buy)
    assert hi_buy > lo_buy and hi_hedge > lo_hedge


def test_delta_mode_picks_near_target_deltas():
    import math
    from skas_algo.engine.options import black_scholes as bs
    spot, t, r = 21000.0, 30 / 365.0, 0.065
    # Build a chain priced at a known IV so deltas are well-defined.
    prem = {}
    for i in range(-20, 60):
        k = spot + 50 * i
        prem[k] = max(bs.price(spot, k, t, r, 0.15, "CE"), 0.05)
    chain = _StubChain(spot, EXPIRY, prem)
    s = CallRatioMonthlyStrategy(
        universe=["NIFTY"], initial_capital=100_000, strike_mode="delta",
        buy_offset=0.36, sell_offset=0.25, hedge_offset=0.05, credit_debit_limit_pct=99,
    )
    out = s.on_slice(_StubCtx(chain, date(2024, 1, 30)))
    assert out and len(out) == 3
    ks = sorted(int(sig.symbol.split("|")[2]) for sig in out)
    deltas = [abs(bs.delta(spot, k, t, r, 0.15, "CE")) for k in ks]
    # hedge (highest strike) ~0.05, buy (lowest) ~0.36 — monotonic & in the ballpark.
    assert deltas[0] > deltas[1] > deltas[2]
    assert 0.25 <= deltas[0] <= 0.5 and deltas[2] < 0.15


def test_skips_month_on_large_net_debit():
    # Premiums where the body (sells) is far cheaper than buy+hedge → big net debit → skip.
    prem = {21300.0: 120.0, 21600.0: 8.0, 22600.0: 4.0}
    chain = _StubChain(SPOT, EXPIRY, prem)
    strat = CallRatioMonthlyStrategy(universe=["NIFTY"], initial_capital=100_000, min_dte=18)
    out = strat.on_slice(_StubCtx(chain, date(2024, 1, 30)))  # last Tuesday of Jan
    assert out == [] and strat.legs == []  # net debit (~6,300) > 1% of 1L → no trade


def test_stale_mark_guard_blocks_exit_until_all_legs_print():
    s = CallRatioMonthlyStrategy(universe=["NIFTY"], initial_capital=100_000)
    s.legs = [
        {"symbol": "NIFTY|2024-02-29|21300|CE", "dir": 1, "units": 75, "entry": 100.0},
        {"symbol": "NIFTY|2024-02-29|21600|CE", "dir": -1, "units": 150, "entry": 60.0},
        {"symbol": "NIFTY|2024-02-29|22600|CE", "dir": 1, "units": 75, "entry": 20.0},
    ]
    s.entry_date = date(2024, 1, 30)
    # Marks that would massively breach the stop (long leg collapsed).
    closes = {s.legs[0]["symbol"]: 10.0, s.legs[1]["symbol"]: 60.0, s.legs[2]["symbol"]: 20.0}

    class Ctx:
        def __init__(self, stale_symbol=None):
            self._stale = stale_symbol
            self.market = self
        def has_print(self, sym): return sym != self._stale
        def lots(self, sym): return [object()]
        def close(self, sym): return closes[sym]
        def today(self): return date(2024, 2, 5)
        def option_chain(self): return object()

    # One leg unprinted (the short stuck at entry) → no exit, manage next slice.
    assert s.on_slice(Ctx(stale_symbol=s.legs[1]["symbol"])) == []
    assert s.legs  # still holding
    # All legs printed → the stop fires.
    out = s.on_slice(Ctx())
    assert len(out) == 3 and all(sig.reason == "stop" for sig in out)


def test_min_vix_filter_skips_low_iv_months():
    from skas_algo.engine.options import black_scholes as bs
    spot, t, r = 21000.0, 30 / 365.0, 0.065
    prem = {spot + 50 * i: max(bs.price(spot, spot + 50 * i, t, r, 0.12, "CE"), 0.05)
            for i in range(-20, 60)}  # chain priced at 12% IV
    chain = _StubChain(spot, EXPIRY, prem)
    kw = dict(universe=["NIFTY"], initial_capital=100_000, credit_debit_limit_pct=99)
    assert CallRatioMonthlyStrategy(min_vix=15, **kw).on_slice(_StubCtx(chain, date(2024, 1, 30))) == []
    assert CallRatioMonthlyStrategy(min_vix=10, **kw).on_slice(_StubCtx(chain, date(2024, 1, 30)))


def test_debit_month_is_skipped():
    # Base strikes (21300/21600/22600) price to a net DEBIT (−18/unit) — a low-IV month.
    # The strategy must SKIP (no entry), even though a closer structure would yield credit.
    prem = {21000.0: 130.0, 21100.0: 115.0, 21200.0: 100.0, 21300.0: 90.0, 21400.0: 75.0,
            21500.0: 58.0, 21600.0: 40.0, 21700.0: 30.0, 22300.0: 12.0, 22400.0: 10.0,
            22500.0: 9.0, 22600.0: 8.0, 22700.0: 6.5}
    chain = _StubChain(21000.0, EXPIRY, prem)
    s = CallRatioMonthlyStrategy(universe=["NIFTY"], initial_capital=100_000)
    assert s.on_slice(_StubCtx(chain, date(2024, 1, 30))) == []
    assert s.legs == []


def test_credit_cap_never_exceeded():
    # Rich credit at base (high IV): must shift further OTM until credit ≤ 1% of capital.
    # Premiums decay slowly → base credit is way over the cap; far shifts thin it out.
    prem = {21000.0 + 100 * i: max(400.0 - 28.0 * i, 1.0) for i in range(0, 30)}
    chain = _StubChain(21000.0, EXPIRY, prem)
    s = CallRatioMonthlyStrategy(universe=["NIFTY"], initial_capital=100_000)
    out = s.on_slice(_StubCtx(chain, date(2024, 1, 30)))
    if out:  # if any structure qualified, its net credit must respect the cap
        ks = {int(sig.symbol.split("|")[2]): sig for sig in out}
        strikes = sorted(ks)
        b, sl, h = (prem[float(k)] for k in strikes)
        net = (2 * sl - b - h) * 50  # NIFTY lot = 50 for a Feb-2024 expiry
        assert 0 <= net <= 1000, f"credit {net} exceeds 1% cap"


def test_generalized_132_ratio_units_and_net():
    # buy_lots/sell_lots/hedge_lots generalize the leg ratio: 1:3:2 at 200/400/600
    # must emit 50/150/100 units (Feb-2024 lot 50) with the body short.
    prem = {21200.0: 90.0, 21400.0: 70.0, 21600.0: 55.0}
    chain = _StubChain(SPOT, EXPIRY, prem)
    s = CallRatioMonthlyStrategy(
        universe=["NIFTY"], initial_capital=100_000,
        buy_lots=1, sell_lots=3, hedge_lots=2,
        buy_offset=200, sell_offset=400, hedge_offset=600, credit_debit_limit_pct=99,
    )
    out = s.on_slice(_StubCtx(chain, date(2024, 1, 30)))
    assert len(out) == 3
    q = {int(sig.symbol.split("|")[2]): sig.quantity for sig in out}
    assert q == {21200: 50, 21400: 150, 21600: 100}
    body = next(sig for sig in out if sig.symbol.split("|")[2] == "21400")
    assert body.action.value == "ENTER_SHORT"
    # Generalized net: (3·70 − 1·90 − 2·55)·50 = ₹500 credit, recorded in the legs.
    assert {leg["units"] for leg in s.legs} == {50, 150, 100}


class _StubChain:
    def __init__(self, spot, expiry, prem):
        self._spot, self._expiry, self._prem = spot, expiry, prem

    def expiries(self, u, on):
        return [self._expiry]

    def spot(self, u, on):
        return self._spot

    def strikes(self, u, on, e):
        return sorted(self._prem)

    def chain(self, u, on, e):
        return [ChainRow(u, e, k, "CE", p, p, 1000, make(u, e, k, "CE").symbol)
                for k, p in sorted(self._prem.items())]


class _StubCtx:
    def __init__(self, chain, today):
        self._c, self._t = chain, today

    def option_chain(self):
        return self._c

    def today(self):
        return self._t

    def lots(self, symbol):
        return []


# ---------------------------------------------------------------- tail hedge
def test_tail_hedge_adds_fourth_leg():
    """tail_hedge_offset adds one extra far long per wing, cost counted in the credit."""
    strat = CallRatioMonthlyStrategy(
        universe=["NIFTY"], initial_capital=100_000, max_holding_days=15, min_dte=18,
        tail_hedge_offset=2100,
    )
    result = _run(strat)
    buys = [t for t in result.transactions if t["action"] == "BUY"]
    assert len(buys) == 3, [(t["action"], t["ticker"]) for t in result.transactions]
    strikes = sorted(int(t["ticker"].split("|")[2]) for t in buys)
    assert strikes == [21300, 22600, 23100]  # near, hedge, tail (spot+2100)
    tail = next(t for t in buys if t["ticker"].split("|")[2] == "23100")
    assert tail["units"] == 50  # tail_hedge_lots=1.0 → same lots as the wing


def test_tail_hedge_on_hedge_strike_doubles_hedge():
    """A tail AT the hedge strike merges into one doubled hedge leg (no duplicate
    symbol legs); the extra cost makes the wing a debit, allowed via min_credit_pct."""
    strat = CallRatioMonthlyStrategy(
        universe=["NIFTY"], initial_capital=100_000, max_holding_days=15, min_dte=18,
        tail_hedge_offset=1600, min_credit_pct=-0.01,
    )
    result = _run(strat)
    buys = [t for t in result.transactions if t["action"] == "BUY"]
    assert len(buys) == 2, [(t["action"], t["ticker"]) for t in result.transactions]
    hedge = next(t for t in buys if t["ticker"].split("|")[2] == "22600")
    assert hedge["units"] == 100  # 1 hedge lot + 1 tail lot merged


def test_tail_hedge_put_side_only_on_batman():
    """tail_hedge_side='put' tails only the PE wing: 7 legs total (CE 3 + PE 4)."""
    strat = BatmanRatioMonthlyStrategy(
        universe=["NIFTY"], initial_capital=100_000, max_holding_days=15, min_dte=18,
        tail_hedge_offset=2100, tail_hedge_side="put",
    )
    result = _run(strat)
    entries = [t for t in result.transactions if t["action"] in ("BUY", "SHORT")]
    assert len(entries) == 7, [(t["action"], t["ticker"]) for t in entries]
    pe_buys = sorted(int(t["ticker"].split("|")[2]) for t in entries
                     if t["action"] == "BUY" and t["ticker"].endswith("|PE"))
    ce_buys = sorted(int(t["ticker"].split("|")[2]) for t in entries
                     if t["action"] == "BUY" and t["ticker"].endswith("|CE"))
    assert pe_buys == [18900, 19400, 20700]  # tail spot−2100, hedge −1600, near −300
    assert ce_buys == [21300, 22600]  # call wing untailed


def test_tail_hedge_snaps_to_last_listed_strike():
    """A tail beyond the listed chain (offset 6000 → 27000 > max 25950) snaps to the
    farthest listed strike rather than skipping the month."""
    strat = CallRatioMonthlyStrategy(
        universe=["NIFTY"], initial_capital=100_000, max_holding_days=15, min_dte=18,
        tail_hedge_offset=6000,
    )
    result = _run(strat)
    buys = [t for t in result.transactions if t["action"] == "BUY"]
    strikes = sorted(int(t["ticker"].split("|")[2]) for t in buys)
    assert strikes == [21300, 22600, 25950], strikes


def test_batman_defaults_to_half_put_tail():
    """Batman ships with the run-92 config: half-size put-wing tail at 2100 pts —
    7 legs (CE 3 + PE 4), tail at spot−2100 with half the wing's units."""
    strat = BatmanRatioMonthlyStrategy(
        universe=["NIFTY"], initial_capital=200_000, max_holding_days=15, min_dte=18,
        lots=2,  # 2 lots → half-tail = 1 whole lot
    )
    assert (strat.tail_hedge_offset, strat.tail_hedge_lots, strat.tail_hedge_side) == (2100.0, 0.5, "put")
    result = _run(strat)
    entries = [t for t in result.transactions if t["action"] in ("BUY", "SHORT")]
    assert len(entries) == 7, [(t["action"], t["ticker"]) for t in entries]
    tail = next(t for t in entries
                if t["action"] == "BUY" and t["ticker"].split("|")[2] == "18900")
    assert tail["ticker"].endswith("|PE") and tail["units"] == 50  # 1 lot vs wings' 2


def test_post_expiry_entry_rule_gates_by_cycle():
    from datetime import date
    from skas_algo.strategies.call_ratio_monthly import CallRatioMonthlyStrategy

    s = CallRatioMonthlyStrategy(underlying="NIFTY", entry_rule="post_expiry")
    # June 2024 monthly expiry (Thursday era) = Thu 27 Jun. Mid-cycle (outside the entry
    # window) nothing happens — even on a fresh strategy — and the expiry day itself
    # doesn't count; the window opens the day AFTER and stays open entry_window_days.
    assert not s._entry_allowed(date(2024, 6, 20))
    assert not s._entry_allowed(date(2024, 6, 27))
    assert s._entry_allowed(date(2024, 6, 28))      # first day after → new cycle
    assert s._entry_allowed(date(2024, 7, 3))       # still inside the 7-day retry window
    s._mark_entered(date(2024, 6, 28))
    assert not s._entry_allowed(date(2024, 7, 10))  # mid-cycle: same anchor → locked
    assert not s._entry_allowed(date(2024, 7, 25))  # July expiry day itself — still locked
    assert s._entry_allowed(date(2024, 7, 26))      # new cycle (day after July expiry)
    # State round-trip keeps the cycle lock (live restart must not double-enter).
    st = s.export_state()
    s2 = CallRatioMonthlyStrategy(underlying="NIFTY", entry_rule="post_expiry")
    s2.load_state(st)
    assert not s2._entry_allowed(date(2024, 7, 10))
    # Legacy rule untouched: last Tuesday of June 2024 = 25th.
    legacy = CallRatioMonthlyStrategy(underlying="NIFTY")
    assert not legacy._entry_allowed(date(2024, 6, 24))
    assert legacy._entry_allowed(date(2024, 6, 25))


# ─────────────────────────── capital-based auto sizing (sizing="margin")

def _run_cap(strategy, capital: float):
    sd = FakeCRSD(CALENDAR)
    mv, _chain, settler, margin = build_options_run(sd, "NIFTY", CALENDAR[0], CALENDAR[-1])
    runner = BacktestRunner(
        strategy=strategy, universe=["NIFTY"], loader=lambda *a: None,
        initial_capital=capital, tax_rate=0.0,
        market_view=mv, settler=settler, margin_model=margin,
    )
    return runner.run(CALENDAR[0], CALENDAR[-1])


def test_auto_sizing_fits_lots_to_capital():
    # Era-true divisor at the fixture: 0.13 × 21000 × (2 lots × 50) = ₹273,000 per lot-set.
    # ₹10L × 95% → 3 sets; ₹1L → floors to 0 → min 1 set (same as fixed lots=1).
    big = CallRatioMonthlyStrategy(universe=["NIFTY"], initial_capital=1_000_000,
                                   sizing="margin", min_dte=18)
    r = _run_cap(big, 1_000_000)
    shorts = [t for t in r.transactions if t["action"] == "SHORT"]
    assert big.lots == 3 and shorts[0]["units"] == 3 * 2 * 50
    assert big._entry_capital_base == 1_000_000  # credit gates scaled with the same base

    small = CallRatioMonthlyStrategy(universe=["NIFTY"], initial_capital=100_000,
                                     sizing="margin", min_dte=18)
    r2 = _run_cap(small, 100_000)
    shorts2 = [t for t in r2.transactions if t["action"] == "SHORT"]
    assert small.lots == 1 and shorts2[0]["units"] == 2 * 50
    # Strike geometry identical in both — the rupee credit gate scaled with capital, so
    # bigger capital does NOT shift strikes.
    def k(t):
        return int(t["ticker"].split("|")[2])
    assert k(shorts[0]) == k(shorts2[0]) == 21600


def test_auto_sizing_cap_and_fixed_default_unchanged():
    capped = CallRatioMonthlyStrategy(universe=["NIFTY"], initial_capital=1_000_000,
                                      sizing="margin", max_auto_lots=2, min_dte=18)
    _run_cap(capped, 1_000_000)
    assert capped.lots == 2
    # Default (fixed) ignores capital entirely — lots param is exact, like before.
    fixed = CallRatioMonthlyStrategy(universe=["NIFTY"], initial_capital=1_000_000,
                                     lots=5, min_dte=18)
    r2 = _run_cap(fixed, 1_000_000)
    shorts = [t for t in r2.transactions if t["action"] == "SHORT"]
    assert fixed.lots == 5 and shorts[0]["units"] == 5 * 2 * 50


def test_capital_base_guarded_for_stub_ctx():
    s = CallRatioMonthlyStrategy(universe=["NIFTY"], initial_capital=250_000, sizing="margin")
    assert s._capital_base(None) == 250_000            # no ctx → fallback

    class NoEquity:                                     # stub ctx without an equity accessor
        pass

    assert s._capital_base(NoEquity()) == 250_000

    class WithEquity:
        def equity(self):
            return 800_000.0

    assert s._capital_base(WithEquity()) == 800_000.0
    # fixed mode never reads equity
    f = CallRatioMonthlyStrategy(universe=["NIFTY"], initial_capital=250_000)
    assert f._capital_base(WithEquity()) == 250_000
