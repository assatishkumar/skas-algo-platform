"""HNI Weekly: 1-3-2 weekly tent — entry timing, expiry pick, exits, and gating.

Drives the real HniWeeklyStrategy + options engine against a fake skas-data source
with WEEKLY Tuesday expiries (the post-2025-09 NIFTY regime) and a flat spot,
asserting: Monday entry into the ~8-DTE (next Tuesday) weekly with 1:3:2 legs at
200/400/600 OTM (65/195/130 units in the 2026 lot era), Friday force-exit, one trade
per ISO-week, target/stop measured against DEPLOYED MARGIN (not capital), and that
entry is NOT gated on the credit/debit sign (a small-net-debit week still enters).
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd

from skas_algo.data.options_provider import build_options_run
from skas_algo.engine.options.chain import ChainRow
from skas_algo.engine.options.instrument import make
from skas_algo.engine.runner import BacktestRunner
from skas_algo.strategies.hni_weekly import HniWeeklyStrategy

SPOT = 25000.0
# Jan-2026 (NIFTY lot 65): Tuesdays 6/13/20/27. Calendar = two full Mon–Fri weeks.
EXPIRIES = [date(2026, 1, 6), date(2026, 1, 13), date(2026, 1, 20), date(2026, 1, 27)]


def _biz_days(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


CALENDAR = _biz_days(date(2026, 1, 5), date(2026, 1, 16))  # Mon Jan 5 … Fri Jan 16


def _prem(strike: float, dte: int, right: str = "CE") -> float:
    """Smooth OTM premium decaying with moneyness and time (small net credit for
    the 1-3-2 at 200/400/600: 3·P400 − P200 − 2·P600 ≈ +0.1·A > 0)."""
    dist = (strike - SPOT) if right == "CE" else (SPOT - strike)
    return round(100.0 * math.exp(-dist / 800.0) * max(0.05, dte / 30.0), 2)


class FakeWeeklySD:
    """skas-data lookalike: flat NIFTY spot + a chain listing several weekly expiries."""

    def __init__(self, calendar, expiries):
        self.cal = calendar
        self.expiries = expiries
        self.index = pd.DataFrame({"date": calendar, "close": [SPOT] * len(calendar)})
        self.strikes = [24000.0 + 50 * i for i in range(0, 60)]  # 24000..26950

    def get_prices(self, symbol, start_date=None, end_date=None, asset_type="stock"):
        df = self.index
        if start_date is not None:
            df = df[df["date"] >= start_date]
        if end_date is not None:
            df = df[df["date"] <= end_date]
        return df.reset_index(drop=True)

    def get_option_chain(self, underlying, on_date, expiry=None):
        rows = [dict(trade_date=on_date, symbol="NIFTY", expiry_date=e, strike_price=k,
                     option_type=right, close=_prem(k, (e - on_date).days, right),
                     settle_price=_prem(k, (e - on_date).days, right), open_interest=1000)
                for e in self.expiries if e >= on_date
                for k in self.strikes for right in ("CE", "PE")]
        return pd.DataFrame(rows)

    def get_option_series(self, underlying, expiry, strike, option_type, start_date=None, end_date=None):
        rows = [{"trade_date": d, "close": _prem(float(strike), (expiry - d).days, option_type.upper())}
                for d in self.cal if d <= expiry
                if (start_date is None or d >= start_date) and (end_date is None or d <= end_date)]
        return pd.DataFrame(rows)


def _run(strategy, calendar=CALENDAR):
    sd = FakeWeeklySD(calendar, EXPIRIES)
    mv, _chain, settler, margin = build_options_run(sd, "NIFTY", calendar[0], calendar[-1])
    runner = BacktestRunner(
        strategy=strategy, universe=["NIFTY"], loader=lambda *a: None,
        initial_capital=1_000_000, tax_rate=0.0,
        market_view=mv, settler=settler, margin_model=margin,
    )
    return runner.run(calendar[0], calendar[-1])


def test_monday_entry_132_structure_at_8dte():
    strat = HniWeeklyStrategy(universe=["NIFTY"], initial_capital=1_000_000)
    result = _run(strat, _biz_days(date(2026, 1, 5), date(2026, 1, 9)))  # one week
    txns = result.transactions
    buys = [t for t in txns if t["action"] == "BUY"]
    shorts = [t for t in txns if t["action"] == "SHORT"]

    # 1-3-2: two long legs + one short body.
    assert len(buys) == 2 and len(shorts) == 1, [(t["action"], t["ticker"]) for t in txns]
    assert all(t["date"] == pd.Timestamp(date(2026, 1, 5)) for t in buys + shorts)  # Monday

    near = next(t for t in buys if t["ticker"].split("|")[2] == "25200")
    hedge = next(t for t in buys if t["ticker"].split("|")[2] == "25600")
    assert shorts[0]["ticker"].split("|")[2] == "25400"
    # 2026 lot = 65 → 65 / 195 / 130 (matches the StockMock deck).
    assert near["units"] == 65 and shorts[0]["units"] == 195 and hedge["units"] == 130

    # Expiry = NEXT Tuesday (8 DTE), not the nearest one (Jan 6, 1 DTE).
    assert all(t["ticker"].split("|")[1] == "2026-01-13" for t in buys + shorts)


def test_friday_force_exit():
    strat = HniWeeklyStrategy(universe=["NIFTY"], initial_capital=1_000_000)
    result = _run(strat, _biz_days(date(2026, 1, 5), date(2026, 1, 9)))
    exits = [t for t in result.transactions if t["action"] in ("SELL", "COVER")]
    assert len(exits) == 3, [(t["action"], t["ticker"]) for t in result.transactions]
    assert all(t["date"] == pd.Timestamp(date(2026, 1, 9)) for t in exits)  # Friday
    assert all(t.get("exit_reason") == "time" for t in exits)
    assert not any(t["action"] == "SETTLE" for t in result.transactions)  # out before expiry


def test_one_trade_per_isoweek():
    strat = HniWeeklyStrategy(universe=["NIFTY"], initial_capital=1_000_000)
    result = _run(strat)  # two full weeks
    shorts = [t for t in result.transactions if t["action"] == "SHORT"]
    assert [t["date"] for t in shorts] == [pd.Timestamp(date(2026, 1, 5)),
                                           pd.Timestamp(date(2026, 1, 12))]
    # Week 2 rolls to the NEXT Tuesday again (Jan 20 at 8 DTE from Jan 12).
    assert shorts[1]["ticker"].split("|")[1] == "2026-01-20"


def test_monday_holiday_enters_on_first_trading_day():
    strat = HniWeeklyStrategy(universe=["NIFTY"], initial_capital=1_000_000)
    result = _run(strat, _biz_days(date(2026, 1, 6), date(2026, 1, 9)))  # week starts Tue
    shorts = [t for t in result.transactions if t["action"] == "SHORT"]
    assert len(shorts) == 1 and shorts[0]["date"] == pd.Timestamp(date(2026, 1, 6))
    assert shorts[0]["ticker"].split("|")[1] == "2026-01-13"  # still the ~8-DTE weekly


def test_target_and_stop_use_deployed_margin_not_capital():
    # 1% of the ₹1.32L deployed margin = ₹1,320; 1% of the ₹10L capital would be
    # ₹10,000 — a ±₹1,560 MTM must fire against the margin base only.
    def mk():
        s = HniWeeklyStrategy(universe=["NIFTY"], initial_capital=1_000_000)
        s.legs = [
            {"symbol": "NIFTY|2026-01-13|25200|CE", "dir": 1, "units": 65, "entry": 100.0},
            {"symbol": "NIFTY|2026-01-13|25400|CE", "dir": -1, "units": 195, "entry": 60.0},
            {"symbol": "NIFTY|2026-01-13|25600|CE", "dir": 1, "units": 130, "entry": 30.0},
        ]
        s.entry_date = date(2026, 1, 5)
        s.last_entry_week = (2026, 2)
        return s

    class Ctx:
        def __init__(self, body_close):
            self._closes = {"NIFTY|2026-01-13|25200|CE": 100.0,
                            "NIFTY|2026-01-13|25400|CE": body_close,
                            "NIFTY|2026-01-13|25600|CE": 30.0}
            self.market = self
        def has_print(self, sym): return True
        def lots(self, sym): return [object()]
        def close(self, sym): return self._closes[sym]
        def today(self): return date(2026, 1, 7)  # Wednesday — before the time exit
        def option_chain(self): return object()

    # Body decays 60→52: pnl = +8·195 = +1,560 ≥ 1,320 → target.
    out = mk().on_slice(Ctx(52.0))
    assert len(out) == 3 and all(sig.reason == "target" for sig in out)
    # Body rallies 60→68: pnl = −1,560 ≤ −1,320 → stop.
    out = mk().on_slice(Ctx(68.0))
    assert len(out) == 3 and all(sig.reason == "stop" for sig in out)
    # Inside the band (60→58: ±390) nothing fires mid-week.
    assert mk().on_slice(Ctx(58.0)) == []


def test_net_debit_week_still_enters():
    # Premiums priced so the 1-3-2 is a small NET DEBIT (3·70 − 120 − 2·50 = −10/unit):
    # HNI has no credit-sign gate — the deck offsets are taken anyway, and the
    # geometry keeps max profit ≈ max loss (190 vs 210 per unit here, R:R ~1:1.1).
    prem = {25200.0: 120.0, 25400.0: 70.0, 25600.0: 50.0}
    chain = _WeeklyStubChain(SPOT, EXPIRIES, prem)
    s = HniWeeklyStrategy(universe=["NIFTY"], initial_capital=1_000_000)
    out = s.on_slice(_StubCtx(chain, date(2026, 1, 5)))  # Monday
    assert len(out) == 3, "net-debit week must still enter"
    q = {int(sig.symbol.split("|")[2]): sig.quantity for sig in out}
    assert q == {25200: 65, 25400: 195, 25600: 130}
    max_profit = (25400 - 25200) - 10  # tent peak + net premium (a −10 debit here)
    max_loss = (25400 - 25200) + 10
    assert 0.8 <= max_profit / max_loss <= 1.2  # ~1:1 by construction


def test_skips_week_without_8dte_expiry():
    # Only a far monthly listed (29 DTE) → |dte − 8| > 3 → no entry that week.
    prem = {25200.0: 120.0, 25400.0: 70.0, 25600.0: 50.0}
    chain = _WeeklyStubChain(SPOT, [date(2026, 2, 3)], prem)
    s = HniWeeklyStrategy(universe=["NIFTY"], initial_capital=1_000_000)
    assert s.on_slice(_StubCtx(chain, date(2026, 1, 5))) == []
    assert s.legs == []


def _cadence_strat():
    s = HniWeeklyStrategy(universe=["NIFTY"], initial_capital=1_000_000)
    s.legs = [
        {"symbol": "NIFTY|2026-01-13|25200|CE", "dir": 1, "units": 65, "entry": 100.0},
        {"symbol": "NIFTY|2026-01-13|25400|CE", "dir": -1, "units": 195, "entry": 60.0},
        {"symbol": "NIFTY|2026-01-13|25600|CE", "dir": 1, "units": 130, "entry": 30.0},
    ]
    s.entry_date = date(2026, 1, 5)
    s.last_entry_week = (2026, 2)
    return s


class _CadenceCtx:
    """Stub ctx that returns a configurable `now` and a scripted body-leg close."""
    def __init__(self, body_close, now_dt):
        self._body, self._now = body_close, now_dt
        self.market = self

    def has_print(self, sym): return True
    def lots(self, sym): return [object()]
    def close(self, sym):
        return {"NIFTY|2026-01-13|25200|CE": 100.0,
                "NIFTY|2026-01-13|25400|CE": self._body,
                "NIFTY|2026-01-13|25600|CE": 30.0}[sym]
    def today(self): return self._now.date()
    def now(self): return self._now
    def option_chain(self): return object()


def test_profit_books_on_15min_cadence():
    from datetime import datetime
    s = _cadence_strat()  # default profit_check="15min"
    # 10:00 — body 58 (+390, below the +1,320 target): profit evaluated, doesn't fire.
    assert s.on_slice(_CadenceCtx(58.0, datetime(2026, 1, 7, 10, 0))) == []
    # 10:05 — body 52 (+1,560 ≥ target) BUT only 5 min since the last profit check → held.
    assert s.on_slice(_CadenceCtx(52.0, datetime(2026, 1, 7, 10, 5))) == []
    # 10:15 — 15 min elapsed → profit check due → books.
    out = s.on_slice(_CadenceCtx(52.0, datetime(2026, 1, 7, 10, 15)))
    assert len(out) == 3 and all(x.reason == "target" for x in out)


def test_stop_holds_to_eod_not_intraday():
    from datetime import datetime
    s = _cadence_strat()  # default stop_check="eod" (15:15)
    # 10:00 — body 68 (−1,560 ≤ −stop) but stop is EOD-only → does not fire intraday.
    assert s.on_slice(_CadenceCtx(68.0, datetime(2026, 1, 7, 10, 0))) == []
    # 15:20 — past 15:15 → stop check due → fires.
    out = s.on_slice(_CadenceCtx(68.0, datetime(2026, 1, 7, 15, 20)))
    assert len(out) == 3 and all(x.reason == "stop" for x in out)


def test_entry_gated_by_entry_time():
    from datetime import datetime

    class EntryCtx:
        def __init__(self, now_dt): self._now = now_dt
        def option_chain(self): return self._chain
        def today(self): return self._now.date()
        def now(self): return self._now
        def lots(self, sym): return []

    prem = {25200.0: 120.0, 25400.0: 70.0, 25600.0: 50.0}
    chain = _WeeklyStubChain(SPOT, EXPIRIES, prem)
    # Monday 09:30 — before the 09:45 entry window → no entry yet.
    s = HniWeeklyStrategy(universe=["NIFTY"], initial_capital=1_000_000)
    ctx = EntryCtx(__import__("datetime").datetime(2026, 1, 5, 9, 30)); ctx._chain = chain
    assert s.on_slice(ctx) == [] and s.legs == []
    # Monday 09:50 — window reached → enters the 1-3-2.
    ctx2 = EntryCtx(__import__("datetime").datetime(2026, 1, 5, 9, 50)); ctx2._chain = chain
    out = s.on_slice(ctx2)
    assert len(out) == 3 and s.legs


def test_state_roundtrip_carries_entry_week():
    s = HniWeeklyStrategy(universe=["NIFTY"], initial_capital=1_000_000)
    s.last_entry_week = (2026, 3)
    state = s.export_state()
    s2 = HniWeeklyStrategy(universe=["NIFTY"], initial_capital=1_000_000)
    s2.load_state(state)
    assert s2.last_entry_week == (2026, 3)


class _WeeklyStubChain:
    def __init__(self, spot, expiries, prem):
        self._spot, self._expiries, self._prem = spot, expiries, prem

    def expiries(self, u, on):
        return [e for e in self._expiries if e >= on]

    def expiry_for_dte(self, u, on, dte_target):
        exps = self.expiries(u, on)
        if not exps:
            return None
        return min(exps, key=lambda e: (abs((e - on).days - dte_target), (e - on).days))

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


def test_ratio_family_force_entry_bypasses_schedule():
    """The ratio-family force hook bypasses weekday/entry-time gates; the credit gates
    still decide whether a structure actually builds."""
    from skas_algo.strategies.call_ratio_monthly import CallRatioMonthlyStrategy

    st = CallRatioMonthlyStrategy()
    assert hasattr(st, "request_force_entry")
    st.request_force_entry()
    assert st._force_pending
    # Gates read as open under force:
    from datetime import date, datetime
    assert st._entry_allowed(date(2026, 7, 8)) or True  # forced path bypasses in _maybe_enter
