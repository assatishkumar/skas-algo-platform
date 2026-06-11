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
