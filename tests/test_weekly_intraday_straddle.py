"""weekly_intraday_straddle: cycle anchor (09:20 lock / mid-cycle force-start / weekly roll),
the daily x<y & x<VWAP SHORT entry, VWAP-cross-up + 15:25 exits, the 3/day cap + re-entry,
daily rollover, the optional MTM stop, the data-health alerts (bars unfetchable → error
surfaced + ALL entries incl. forced disabled), and a state round-trip that keeps the locked
strike — fake market/chain/option-bars, no network."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from skas_algo.strategies.weekly_intraday_straddle import (
    _ALERT_FETCH_FAILED,
    _ALERT_NO_BARS,
    _ALERT_NO_PRIOR,
    _ALERT_NO_SOURCE,
    WeeklyIntradayStraddle,
)

UNDER = "NIFTY"
WEEKLY = date(2026, 7, 21)          # nearest weekly ≥ today — the cycle's expiry
TODAY = date(2026, 7, 15)           # Wednesday = expiry+1 (prior trading day 07-14 is a Tue expiry)
PREV = date(2026, 7, 14)            # previous trading day → the source of y
ATM = 24000.0
CE_SYM = f"{UNDER}|{WEEKLY.isoformat()}|24000|CE"
PE_SYM = f"{UNDER}|{WEEKLY.isoformat()}|24000|PE"


def chain(atm=ATM, spot=24010.0, lot=65):
    rows = [{"strike": atm, "ce": {"ltp": 60.0, "oi": 5000}, "pe": {"ltp": 90.0, "oi": 5000}}]
    return {"spot": spot, "atm_strike": atm, "lot_size": lot, "rows": rows}


def _bars(day, ce_closes, pe_closes, vol=1000):
    """Aligned CE/PE 5-min bars from 09:15 (o=h=l=c so VWAP is a clean volume-weighted mean)."""
    ce, pe = [], []
    t = datetime.combine(day, time(9, 15))
    for c, p in zip(ce_closes, pe_closes, strict=True):
        s = t.isoformat()
        ce.append({"start": s, "o": c, "h": c, "l": c, "c": c, "volume": float(vol)})
        pe.append({"start": s, "o": p, "h": p, "l": p, "c": p, "volume": float(vol)})
        t += timedelta(minutes=5)
    return ce, pe


class FakeBars:
    def __init__(self):
        self.by_day: dict[str, tuple[list, list]] = {}
        self.raise_it = False

    def set(self, day, ce_closes, pe_closes, **kw):
        self.by_day[day.isoformat()] = _bars(day, ce_closes, pe_closes, **kw)

    def __call__(self, u, expiry_iso, strike, right, from_dt, to_dt, minutes):
        if self.raise_it:
            raise RuntimeError("kite historical down")
        ce, pe = self.by_day.get(from_dt.date().isoformat(), ([], []))
        return list(ce if right == "CE" else pe)


class FakeCacheChain:
    def __init__(self, expiries):
        self._e = expiries

    def expiries(self, _u, today):
        return [e for e in self._e if e >= today]


class FakeMarket:
    def __init__(self, chain_dict):
        self.chain_dict = chain_dict
        self.prices: dict[str, float] = {}
        self.current_date = None

    def live_chain(self, _u, _e):
        return self.chain_dict

    def index_spot(self, _u):
        return (self.chain_dict or {}).get("spot")

    def has_print(self, s):
        return s in self.prices


class FakeCtx:
    def __init__(self, market, cache_chain):
        self.market = market
        self.cache_chain = cache_chain
        self._now = None
        self.positions: dict[str, float] = {}

    def now(self):
        return self._now

    def today(self):
        return self._now.date()

    def option_chain(self):
        return self.cache_chain

    def lots(self, s):
        return self.positions.get(s, 0)

    def close(self, s):
        if s in self.market.prices:
            return self.market.prices[s]
        raise KeyError(s)


def setup(**kw):
    st = WeeklyIntradayStraddle(underlying="NIFTY", lots=1, **kw)
    fb = FakeBars()
    st.set_option_bars_fn(fb)
    ctx = FakeCtx(FakeMarket(chain()), FakeCacheChain([WEEKLY]))
    ctx.market.prices[CE_SYM] = 60.0     # live marks for the entry legs
    ctx.market.prices[PE_SYM] = 90.0
    return st, ctx, fb


def tick(st, ctx, dt):
    ctx._now = dt
    return st.on_slice(ctx)


def _fill(st, ctx):
    for leg in st.legs:
        ctx.positions[leg["symbol"]] = leg["units"]


# yesterday all 200 (y=200); today [240,220,150] → last closed (09:25) x=150 < y and < VWAP
def _good_signal_bars(fb):
    fb.set(PREV, [100, 100, 100], [100, 100, 100])
    fb.set(TODAY, [120, 110, 60], [120, 110, 90])


def test_locks_cycle_at_0920_and_enters_short_on_signal():
    st, ctx, fb = setup()
    _good_signal_bars(fb)
    sigs = tick(st, ctx, datetime(2026, 7, 15, 9, 30, 5))
    assert st.cycle is not None
    assert st.cycle["expiry_iso"] == WEEKLY.isoformat()
    assert st.cycle["strike"] == ATM and st.cycle["strike"] % 100 == 0  # NIFTY 100-multiple
    assert len(sigs) == 2 and all(s.action.name == "ENTER_SHORT" for s in sigs)
    assert {s.symbol.split("|")[3] for s in sigs} == {"CE", "PE"}
    assert all(s.quantity == 65 for s in sigs)          # 1 lot × 65
    assert st.entries_today == 1 and st.margin_source == "pending"
    assert abs(st.y_today - 200.0) < 1e-9


def test_no_entry_when_x_not_below_prior_low():
    st, ctx, fb = setup()
    fb.set(PREV, [50, 50, 50], [50, 50, 50])            # y = 100
    fb.set(TODAY, [120, 110, 60], [120, 110, 90])       # x(09:25)=150 > y=100 → skip
    sigs = tick(st, ctx, datetime(2026, 7, 15, 9, 30, 5))
    assert sigs == [] and st.cycle is not None and not st.legs


def test_vwap_cross_up_exit():
    st, ctx, fb = setup()
    fb.set(PREV, [100, 100, 100], [100, 100, 100])
    fb.set(TODAY, [120, 110, 60, 200], [120, 110, 90, 200])   # 4th bar spikes combined to 400
    sigs = tick(st, ctx, datetime(2026, 7, 15, 9, 30, 5))     # enter on 09:25 (x=150)
    assert len(sigs) == 2
    _fill(st, ctx)
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = leg["entry"]
    # next boundary: last closed = 09:30 bar, combined 400 > VWAP(252.5) → exit
    sigs2 = tick(st, ctx, datetime(2026, 7, 15, 9, 35, 5))
    assert sigs2 and all(s.reason == "vwap_cross" for s in sigs2) and len(sigs2) == 2
    assert not st.legs


def test_eod_exit_fires_even_when_fetch_raises():
    st, ctx, fb = setup()
    _good_signal_bars(fb)
    tick(st, ctx, datetime(2026, 7, 15, 9, 30, 5))            # enter
    _fill(st, ctx)
    fb.raise_it = True                                        # broker historical down
    sigs = tick(st, ctx, datetime(2026, 7, 15, 15, 25, 0))
    assert sigs and all(s.reason == "eod" for s in sigs) and len(sigs) == 2
    assert not st.legs


def test_daily_entry_cap_blocks_fourth():
    st, ctx, fb = setup(max_entries_per_day=3)
    _good_signal_bars(fb)
    tick(st, ctx, datetime(2026, 7, 15, 9, 30, 5))            # entry #1
    assert st.entries_today == 1 and st.legs
    st.entries_today = 3                                      # pretend 3 taken; go flat
    st.legs = []
    st.evaluated_bar = None
    ctx.positions.clear()
    assert tick(st, ctx, datetime(2026, 7, 15, 9, 35, 5)) == []   # cap → no re-entry


def test_reentry_under_cap():
    st, ctx, fb = setup()
    _good_signal_bars(fb)
    tick(st, ctx, datetime(2026, 7, 15, 9, 30, 5))            # entry #1
    assert st.entries_today == 1
    st.legs = []                                             # exited, flat again
    ctx.positions.clear()
    st.evaluated_bar = None                                  # a fresh 5-min close
    sigs = tick(st, ctx, datetime(2026, 7, 15, 9, 35, 5))
    assert len(sigs) == 2 and st.entries_today == 2          # re-entered under the cap


def test_daily_rollover_resets_counters():
    st, ctx, fb = setup()
    st.day_iso = TODAY.isoformat()
    st.entries_today = 3
    st.y_today, st.y_day = 200.0, TODAY.isoformat()
    st.evaluated_bar = "stale"
    tick(st, ctx, datetime(2026, 7, 16, 9, 30, 5))           # a new day → rollover
    assert st.entries_today == 0 and st.y_today is None and st.evaluated_bar is None


def test_force_entry_anchors_and_sells_bypassing_gates():
    st, ctx, fb = setup()
    fb.set(PREV, [100, 100, 100], [100, 100, 100])  # bars fetchable → the force probe passes
    assert tick(st, ctx, datetime(2026, 7, 15, 8, 30, 0)) == []   # pre-09:20, no signal → dormant
    assert st.cycle is None
    st.request_force_entry()
    sigs = tick(st, ctx, datetime(2026, 7, 15, 8, 30, 0))
    assert st.cycle is not None and st.cycle["strike"] == ATM
    assert len(sigs) == 2 and all(s.action.name == "ENTER_SHORT" for s in sigs)
    assert not st.force_pending and st.strategy_alert is None


def test_force_entry_blocked_while_bars_unfetchable():
    """The data-health gate beats even the owner's force button: with no fetchable option bars
    the forced book would have no working VWAP exit — the flag stays armed and retries."""
    st, ctx, fb = setup()                                        # fb has NO data at all
    st.request_force_entry()
    assert tick(st, ctx, datetime(2026, 7, 15, 9, 40, 0)) == []
    assert st.force_pending and not st.legs                      # still armed, nothing opened
    assert st.strategy_alert == _ALERT_NO_PRIOR                  # surfaced as an error
    fb.set(PREV, [100, 100, 100], [100, 100, 100])               # bars recover → next tick enters
    sigs = tick(st, ctx, datetime(2026, 7, 15, 9, 40, 15))
    assert len(sigs) == 2 and not st.force_pending and st.strategy_alert is None


def test_midcycle_autostart_locks_without_entry():
    st, ctx, fb = setup()
    fb.set(PREV, [10, 10], [10, 10])                         # y = 20
    fb.set(TODAY, [120, 110], [120, 110])                    # x ≫ y → no entry
    sigs = tick(st, ctx, datetime(2026, 7, 15, 11, 0, 5))    # deployed mid-cycle after 09:20
    assert st.cycle is not None and st.cycle["strike"] == ATM
    assert sigs == [] and not st.legs


def test_cycle_roll_reanchors_new_strike():
    st, ctx, fb = setup()
    st.cycle = {"expiry_iso": "2026-07-14", "strike": 23000.0,
                "ce_symbol": "NIFTY|2026-07-14|23000|CE", "pe_symbol": "NIFTY|2026-07-14|23000|PE",
                "start_day": "2026-07-08", "lot_size": 65}
    _good_signal_bars(fb)
    tick(st, ctx, datetime(2026, 7, 15, 9, 30, 5))
    assert st.cycle["expiry_iso"] == WEEKLY.isoformat()      # rolled to the new nearest weekly
    assert st.cycle["strike"] == ATM


def test_optional_stop_waits_for_broker_margin():
    st, ctx, fb = setup(stop_loss_pct=2.0)
    _good_signal_bars(fb)
    tick(st, ctx, datetime(2026, 7, 15, 9, 30, 5))           # enter
    _fill(st, ctx)
    for leg in st.legs:                                      # short loss: mark up 100
        ctx.market.prices[leg["symbol"]] = leg["entry"] + 100.0
    assert tick(st, ctx, datetime(2026, 7, 15, 10, 0, 5)) == []   # margin pending → stop waits
    st.set_broker_margin(100_000)
    sigs = tick(st, ctx, datetime(2026, 7, 15, 10, 5, 5))
    assert sigs and all(s.reason == "stop" for s in sigs) and len(sigs) == 2


def test_no_bars_fn_gates_entries_and_surfaces_error():
    st = WeeklyIntradayStraddle(underlying="NIFTY", lots=1)
    st.set_option_bars_fn(None)                             # cache source → no option bars
    ctx = FakeCtx(FakeMarket(chain()), FakeCacheChain([WEEKLY]))
    ctx._now = datetime(2026, 7, 15, 9, 30, 5)
    sigs = st.on_slice(ctx)
    assert sigs == [] and not st.legs
    assert st.strategy_alert == _ALERT_NO_SOURCE            # error shown; entries disabled
    st.request_force_entry()                                # even a forced entry is refused
    ctx._now = datetime(2026, 7, 15, 9, 30, 20)
    assert st.on_slice(ctx) == [] and not st.legs and st.force_pending


def test_empty_today_bars_set_alert_after_first_close():
    st, ctx, fb = setup()                                   # fb: no TODAY data at all
    fb.set(PREV, [100, 100, 100], [100, 100, 100])
    sigs = tick(st, ctx, datetime(2026, 7, 15, 9, 30, 5))   # past 09:25 grace, Kite served nothing
    assert sigs == [] and not st.legs
    assert st.strategy_alert == _ALERT_NO_BARS


def test_missing_prior_day_bars_set_alert_and_gate_entry():
    st, ctx, fb = setup()
    fb.set(TODAY, [120, 110, 60], [120, 110, 90])           # today's bars fine, yesterday missing
    sigs = tick(st, ctx, datetime(2026, 7, 15, 9, 30, 5))
    assert sigs == [] and not st.legs                       # x<VWAP holds but y is unknowable
    assert st.strategy_alert == _ALERT_NO_PRIOR


def test_fetch_exception_sets_alert_no_entry():
    st, ctx, fb = setup()
    _good_signal_bars(fb)
    fb.raise_it = True                                      # broker historical down from the start
    sigs = tick(st, ctx, datetime(2026, 7, 15, 9, 30, 5))
    assert sigs == [] and not st.legs
    assert st.strategy_alert == _ALERT_FETCH_FAILED
    fb.raise_it = False                                     # recovers → alert clears, entry fires
    st.evaluated_bar = None
    sigs = tick(st, ctx, datetime(2026, 7, 15, 9, 35, 5))
    assert len(sigs) == 2 and st.strategy_alert is None


def test_basket_status_carries_signal_series_and_prev_levels():
    """The Live signal chart's payload: today's 5-min combined closes + running VWAP, the
    prior day's low (y) AND close, per-leg VWAPs — refreshed even while holding."""
    st, ctx, fb = setup()
    _good_signal_bars(fb)
    tick(st, ctx, datetime(2026, 7, 15, 9, 30, 5))            # locks + enters at 09:25 close
    _fill(st, ctx)
    b = st.basket_status(ctx.market, None)["names"][0]
    assert b["y"] == 200.0 and b["prev_close"] == 200.0 and b["prev_day"] == PREV.isoformat()
    assert b["x"] == 150.0                                    # 60 + 90 on the 09:25 close
    assert [r["cc"] for r in b["series"]] == [240.0, 220.0, 150.0]
    assert b["series"][-1]["vwap"] == b["vwap"]               # running VWAP ends at the signal VWAP
    assert abs(b["vwap"] - (b["vwap_ce"] + b["vwap_pe"])) < 1e-9
    # Next boundary WHILE HOLDING: the series keeps refreshing for the chart.
    fb.set(TODAY, [120, 110, 60, 55], [120, 110, 90, 85])
    tick(st, ctx, datetime(2026, 7, 15, 9, 35, 5))
    b2 = st.basket_status(ctx.market, None)["names"][0]
    assert len(b2["series"]) == 4 and b2["x"] == 140.0


def test_state_round_trip_keeps_cycle_and_legs():
    st, ctx, fb = setup()
    _good_signal_bars(fb)
    tick(st, ctx, datetime(2026, 7, 15, 9, 30, 5))
    assert st.cycle is not None and st.legs
    st2 = WeeklyIntradayStraddle(underlying="NIFTY", lots=1)
    st2.load_state(st.export_state())
    assert st2.cycle == st.cycle
    assert st2.legs == st.legs
    assert st2.entries_today == st.entries_today
    assert st2.y_today == st.y_today and st2.y_day == st.y_day
