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
from skas_algo.strategies.call_ratio_monthly import CallRatioMonthlyStrategy

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


def _ce(strike: float, dte: int) -> float:
    """A smooth OTM call premium that decays with moneyness and time (gives a small net credit)."""
    return round(100.0 * math.exp(-(strike - SPOT) / 800.0) * max(0.05, dte / 30.0), 2)


class FakeCRSD:
    def __init__(self, calendar):
        self.cal = calendar
        self.index = pd.DataFrame({"date": calendar, "close": [SPOT] * len(calendar)})
        self.strikes = [20000.0 + 50 * i for i in range(0, 120)]  # 20000..25950

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
                     option_type="CE", close=_ce(k, dte), settle_price=_ce(k, dte), open_interest=1000)
                for k in self.strikes]
        return pd.DataFrame(rows)

    def get_option_series(self, underlying, expiry, strike, option_type, start_date=None, end_date=None):
        rows = [{"trade_date": d, "close": _ce(float(strike), (EXPIRY - d).days)}
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
    assert shorts[0]["units"] == 2 * 75  # 2 lots short, NIFTY lot 75

    # It closed (time exit before expiry → SELL on the longs, COVER on the short; not SETTLE).
    assert any(t["action"] == "COVER" for t in txns)
    assert any(t["action"] == "SELL" for t in txns)


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


def test_debit_structure_shifts_closer_to_find_credit():
    # Base strikes (21300/21600/22600) price to a net DEBIT (−18/unit); the +100 shift is
    # also a debit; the −100 shift (21200/21500/22500) yields a small credit within the
    # 1% cap → the strategy should adjust CLOSER and enter there.
    prem = {21000.0: 130.0, 21100.0: 115.0, 21200.0: 100.0, 21300.0: 90.0, 21400.0: 75.0,
            21500.0: 58.0, 21600.0: 40.0, 21700.0: 30.0, 22300.0: 12.0, 22400.0: 10.0,
            22500.0: 9.0, 22600.0: 8.0, 22700.0: 6.5}
    chain = _StubChain(21000.0, EXPIRY, prem)
    s = CallRatioMonthlyStrategy(universe=["NIFTY"], initial_capital=100_000)
    out = s.on_slice(_StubCtx(chain, date(2024, 1, 30)))
    assert out, "expected an entry via the closer shift"
    ks = sorted(int(sig.symbol.split("|")[2]) for sig in out)
    assert ks == [21200, 21500, 22500]  # shifted −100 from base
    net = (2 * 58.0 - 100.0 - 9.0) * 75  # = +525, within the ₹1,000 (1%) cap
    assert 0 <= net <= 1000

    # With adjust_for_credit=False the same debit month is skipped instead.
    s2 = CallRatioMonthlyStrategy(universe=["NIFTY"], initial_capital=100_000, adjust_for_credit=False)
    assert s2.on_slice(_StubCtx(chain, date(2024, 1, 30))) == []


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
        net = (2 * sl - b - h) * 75
        assert 0 <= net <= 1000, f"credit {net} exceeds 1% cap"


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
