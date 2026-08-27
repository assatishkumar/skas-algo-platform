"""intraday_strangle_combo: OTM3 strike math on the LISTING grid, per-leg 40/70 exits,
LEG INDEPENDENCE (the deck's one non-negotiable), independent re-entry caps, the rupee
MTM stop, the weekday index schedule and the hard 15:25 exit — fake market, no network.
"""

from __future__ import annotations

from datetime import date, datetime

from skas_algo.strategies.intraday_strangle_combo import IntradayStrangleComboStrategy

# 2026-08-14 is a FRIDAY → NIFTY-only under the default schedule.
FRI = datetime(2026, 8, 14, 9, 20)


def at(h: int, m: int = 0) -> datetime:
    """A wall-clock time on FRI. Use this, never ``FRI.replace(hour=…)`` — that keeps
    minute=20, so 11:00 would land AFTER 11:01 and the cadence window (which stamps the
    last check time) silently refuses every later check."""
    return datetime(2026, 8, 14, h, m)
NIFTY_WEEKLY = date(2026, 8, 18)
SENSEX_WEEKLY = date(2026, 8, 20)


def chain(spot=25000.0, step=50, lot=75, prem=100.0, oi=5000, span=30):
    """A flat-premium chain on the given LISTING grid — premiums are uniform so a test can
    set an exact SL/target price without solving BS."""
    rows = []
    atm = round(spot / step) * step
    for i in range(-span, span + 1):
        k = float(atm + i * step)
        rows.append({"strike": k,
                     "ce": {"ltp": prem, "oi": oi},
                     "pe": {"ltp": prem, "oi": oi}})
    return {"spot": spot, "atm_strike": float(atm), "lot_size": lot, "rows": rows}


class FakeCacheChain:
    def __init__(self, expiries):
        self._e = expiries

    def expiries(self, _u, today):
        return [e for e in self._e if e >= today]


class FakeMarket:
    def __init__(self, chains: dict):
        self.chains = chains            # underlying -> chain dict
        self.prices: dict[str, float] = {}
        self.current_date = None

    def live_chain(self, u, _e):
        return self.chains.get(u)

    def index_spot(self, u):
        return (self.chains.get(u) or {}).get("spot")

    def has_print(self, s):
        return s in self.prices


class FakeCtx:
    def __init__(self, market, cache_chain=None):
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


# Per-leg mechanics have to be tested with the overall MTM stop OFF, because on NIFTY it
# usually fires first: a 40% stop on a Rs100 premium is -40 x 75 = -Rs3,000, twice the
# Rs1,500/lot budget. That interaction is real and gets its own test below.
NO_MTM = {"NIFTY": 0.0, "SENSEX": 0.0}


def setup(chains=None, expiries=None, **kw):
    st = IntradayStrangleComboStrategy(**kw)
    ctx = FakeCtx(FakeMarket(chains or {"NIFTY": chain()}),
                  FakeCacheChain(expiries or [NIFTY_WEEKLY, SENSEX_WEEKLY]))
    return st, ctx


def tick(st, ctx, dt):
    ctx._now = dt
    sigs = st.on_slice(ctx)
    # Mirror the engine: entries open a position, EXIT_ALL closes it.
    for s in sigs:
        if s.action.name in ("ENTER_SHORT", "ENTER_LONG"):
            ctx.positions[s.symbol] = s.quantity
        elif s.action.name == "EXIT_ALL":
            ctx.positions.pop(s.symbol, None)
    return sigs


def mark(ctx, symbol, price):
    ctx.market.prices[symbol] = price


def leg_of(st, u, right):
    return st.sides[u][right]["leg"]


def open_all(st, ctx, dt=FRI, prem=100.0):
    """Enter the day's strangle and give both legs a fresh print at entry premium."""
    sigs = tick(st, ctx, dt)
    for r in ("CE", "PE"):
        mark(ctx, leg_of(st, "NIFTY", r)["symbol"], prem)
    return sigs


# --------------------------------------------------------------- strike math
def test_otm3_uses_the_listing_grid_the_owners_worked_example():
    """Spot 25000 → PE 24850, CE 25150. Three FIFTY-point steps, not the platform's
    NIFTY-100 selection grid — the whole reason this strategy is backtest-only."""
    st, ctx = setup()
    sigs = tick(st, ctx, FRI)
    strikes = {s.symbol.split("|")[3]: s.symbol.split("|")[2] for s in sigs}
    assert strikes == {"PE": "24850", "CE": "25150"}
    assert all(s.action.name == "ENTER_SHORT" and s.quantity == 75 for s in sigs)


def test_sensex_counts_hundred_point_steps():
    """SENSEX lists 100s only → OTM3 is ±300. Wednesday is a SENSEX day."""
    st, ctx = setup(chains={"SENSEX": chain(spot=82000.0, step=100, lot=20)},
                    underlyings=["SENSEX"])
    sigs = tick(st, ctx, datetime(2026, 8, 19, 9, 20))   # Wednesday
    strikes = {s.symbol.split("|")[3]: s.symbol.split("|")[2] for s in sigs}
    assert strikes == {"PE": "81700", "CE": "82300"}


def test_a_missing_strike_skips_rather_than_substituting():
    """An absent OTM3 row must SKIP the entry (retried next tick), never silently sell a
    nearby strike the owner didn't ask for."""
    c = chain()
    c["rows"] = [r for r in c["rows"] if r["strike"] != 24850.0]   # drop the PE leg's strike
    st, ctx = setup(chains={"NIFTY": c})
    sigs = tick(st, ctx, FRI)
    assert [s.symbol.split("|")[3] for s in sigs] == ["CE"]        # CE only
    assert leg_of(st, "NIFTY", "PE") is None


# ------------------------------------------------------------ per-leg exits
def test_forty_percent_stop_and_seventy_percent_target_off_each_legs_own_entry():
    st, ctx = setup(mtm_stop_per_lot=NO_MTM)
    open_all(st, ctx)
    ce, pe = leg_of(st, "NIFTY", "CE"), leg_of(st, "NIFTY", "PE")
    assert ce["entry"] == 100.0

    mark(ctx, ce["symbol"], 139.9)          # +39.9% — not yet
    mark(ctx, pe["symbol"], 30.1)           # −69.9% — not yet
    assert tick(st, ctx, at(10)) == []

    mark(ctx, ce["symbol"], 140.0)          # +40% exactly → stop
    out = tick(st, ctx, at(10, 1))
    assert out[0].action.name == "EXIT_ALL" and out[0].reason == "isc_leg_sl"

    mark(ctx, pe["symbol"], 30.0)           # −70% exactly → target
    out = tick(st, ctx, at(10, 2))
    assert any(s.reason == "isc_leg_target" for s in out)


def test_one_leg_exiting_never_touches_the_other():
    """The deck's non-negotiable, asserted directly: the PE leg's record, symbol and entry
    are untouched by the CE leg stopping out and re-entering."""
    st, ctx = setup(mtm_stop_per_lot=NO_MTM)
    open_all(st, ctx)
    pe_before = dict(leg_of(st, "NIFTY", "PE"))
    pe_side_before = dict(st.sides["NIFTY"]["PE"], leg=None)

    mark(ctx, leg_of(st, "NIFTY", "CE")["symbol"], 140.0)
    out = tick(st, ctx, at(10))

    assert pe_before["symbol"] not in [s.symbol for s in out]       # PE never signalled
    assert leg_of(st, "NIFTY", "PE") == pe_before                   # record identical
    assert dict(st.sides["NIFTY"]["PE"], leg=None) == pe_side_before  # counters untouched
    assert ctx.positions.get(pe_before["symbol"]) == pe_before["units"]  # still held


def test_reentry_recomputes_otm3_from_the_current_spot():
    st, ctx = setup(mtm_stop_per_lot=NO_MTM)
    open_all(st, ctx)
    ce = leg_of(st, "NIFTY", "CE")
    assert ce["strike"] == 25150.0

    ctx.market.chains["NIFTY"] = chain(spot=25200.0)   # spot ran 200 points up
    mark(ctx, ce["symbol"], 140.0)                     # ...which is what stopped the CE
    out = tick(st, ctx, at(10))

    assert [s.action.name for s in out] == ["EXIT_ALL", "ENTER_SHORT"]
    # ORDER IS LOAD-BEARING (overrides.py resolves EXIT_ALL against the pre-action book):
    # entry-first would let the close swallow the freshly re-opened lot.
    assert out[0].symbol.split("|")[2] == "25150"      # the leg that stopped
    assert out[1].symbol.split("|")[2] == "25350"      # OTM3 off the NEW spot, not the old strike
    assert leg_of(st, "NIFTY", "CE")["strike"] == 25350.0


def test_same_strike_reentry_still_exits_before_it_re_enters():
    """Spot unmoved → the re-entry lands on the strike just closed. The exit signal must
    still come first or the close swallows the re-opened lot (the run-#203 merge bug)."""
    st, ctx = setup(mtm_stop_per_lot=NO_MTM)
    open_all(st, ctx)
    ce = leg_of(st, "NIFTY", "CE")
    mark(ctx, ce["symbol"], 140.0)
    out = tick(st, ctx, at(10))
    assert [s.symbol for s in out] == [ce["symbol"], ce["symbol"]]
    assert [s.action.name for s in out] == ["EXIT_ALL", "ENTER_SHORT"]


# --------------------------------------------------------------- re-entry caps
def test_sl_and_target_reentry_caps_are_independent_counters():
    """2 SL re-entries AND 2 target re-entries per leg — so a side can trade up to 5 times.
    The counters must not share a budget."""
    st, ctx = setup(mtm_stop_per_lot=NO_MTM)
    open_all(st, ctx)

    for i in range(2):                       # two SL re-entries
        leg = leg_of(st, "NIFTY", "CE")
        mark(ctx, leg["symbol"], leg["entry"] * 1.4)
        tick(st, ctx, at(10, i))
        mark(ctx, leg_of(st, "NIFTY", "CE")["symbol"], 100.0)
    assert st.sides["NIFTY"]["CE"]["sl_reentries"] == 2
    assert leg_of(st, "NIFTY", "CE") is not None        # still trading

    for i in range(2):                       # two TARGET re-entries, a separate budget
        leg = leg_of(st, "NIFTY", "CE")
        mark(ctx, leg["symbol"], leg["entry"] * 0.3)
        tick(st, ctx, at(10, 10 + i))
        mark(ctx, leg_of(st, "NIFTY", "CE")["symbol"], 100.0)
    assert st.sides["NIFTY"]["CE"]["tgt_reentries"] == 2
    assert leg_of(st, "NIFTY", "CE") is not None        # 5th entry of the day, still open

    # A third stop exhausts the SL budget → that side is done, the PE side is not.
    leg = leg_of(st, "NIFTY", "CE")
    mark(ctx, leg["symbol"], leg["entry"] * 1.4)
    out = tick(st, ctx, at(10, 30))
    assert [s.action.name for s in out] == ["EXIT_ALL"]   # exit, NO re-entry
    assert st.sides["NIFTY"]["CE"]["closed_for_day"] is True
    assert st.sides["NIFTY"]["PE"]["closed_for_day"] is False
    assert leg_of(st, "NIFTY", "PE") is not None


# ------------------------------------------------------------------- risk
def test_nifty_rupee_mtm_stop_closes_both_sides_and_stops_the_day():
    """₹1,500 per lot, day-cumulative. On breach: close everything, no more re-entries."""
    st, ctx = setup(lots=2)                              # → ₹3,000 budget
    open_all(st, ctx)
    ce, pe = leg_of(st, "NIFTY", "CE"), leg_of(st, "NIFTY", "PE")
    assert ce["units"] == 150                            # 2 lots × 75

    mark(ctx, ce["symbol"], 105.0)                       # −5 × 150 = −750
    mark(ctx, pe["symbol"], 109.0)                       # −9 × 150 = −1350 → −2,100 total
    assert tick(st, ctx, at(11)) == []     # inside the ₹3,000 budget

    mark(ctx, pe["symbol"], 116.0)                       # −16 × 150 = −2,400 → −3,150
    out = tick(st, ctx, at(11, 1))
    assert len(out) == 2 and all(s.reason == "isc_mtm_stop" for s in out)
    assert st.stopped_day["NIFTY"] == FRI.date().isoformat()
    assert tick(st, ctx, at(12)) == []     # stopped for the day


def test_the_mtm_stop_counts_realized_pnl_banked_earlier_in_the_day():
    """"Overall MTM" is realized + unrealized. A leg already booked at a loss must keep
    counting, or the day's real drawdown runs well past the budget."""
    st, ctx = setup(mtm_stop_per_lot={"NIFTY": 4000.0})   # above a single leg's 40% stop
    open_all(st, ctx)
    ce = leg_of(st, "NIFTY", "CE")
    mark(ctx, ce["symbol"], 140.0)                        # leg stop: −40 × 75 = −3,000
    out = tick(st, ctx, at(10))
    assert [s.reason for s in out] == ["isc_leg_sl", "isc_entry"]
    assert st.realized["NIFTY"] == -3000.0                # banked, and the day continues

    # Both legs now sit at entry, so UNREALIZED is zero — only the banked −3,000 is in play.
    for r in ("CE", "PE"):
        mark(ctx, leg_of(st, "NIFTY", r)["symbol"], 100.0)
    assert tick(st, ctx, at(10, 1)) == []                 # −3,000 is inside the −4,000 budget

    mark(ctx, leg_of(st, "NIFTY", "CE")["symbol"], 115.0)  # −15 × 75 = −1,125 → −4,125
    out = tick(st, ctx, at(10, 2))
    assert out and all(s.reason == "isc_mtm_stop" for s in out)
    assert st.stopped_day["NIFTY"] == FRI.date().isoformat()


def test_the_overall_mtm_stop_outranks_a_leg_stop_in_the_same_tick():
    """With the deck's numbers these collide constantly: a 40% stop on a ₹100 NIFTY premium
    is −₹3,000, twice the ₹1,500/lot budget. When both fire on one price the OVERALL stop
    must win — it closes the book and ends the day, which is the stricter guard."""
    st, ctx = setup()                                     # default ₹1,500/lot
    open_all(st, ctx)
    mark(ctx, leg_of(st, "NIFTY", "CE")["symbol"], 140.0)  # would also be a leg stop
    out = tick(st, ctx, at(10))
    assert len(out) == 2 and all(s.reason == "isc_mtm_stop" for s in out)
    assert st.stopped_day["NIFTY"] == FRI.date().isoformat()
    assert st.sides["NIFTY"]["CE"]["sl_reentries"] == 0    # no re-entry was counted


def test_sensex_has_no_mtm_stop_only_the_per_leg_forties():
    st, ctx = setup(chains={"SENSEX": chain(spot=82000.0, step=100, lot=20)},
                    underlyings=["SENSEX"])
    wed = datetime(2026, 8, 19, 9, 20)
    tick(st, ctx, wed)
    for r in ("CE", "PE"):
        mark(ctx, leg_of(st, "SENSEX", r)["symbol"], 100.0)
    for r in ("CE", "PE"):                               # −₹40,000 combined, no stop fires
        mark(ctx, leg_of(st, "SENSEX", r)["symbol"], 130.0)
    assert tick(st, ctx, wed.replace(hour=11, minute=0)) == []
    assert st.stopped_day["SENSEX"] is None


# ------------------------------------------------------------- session/schedule
def test_weekday_schedule_picks_the_index():
    chains = {"NIFTY": chain(), "SENSEX": chain(spot=82000.0, step=100, lot=20)}
    for day, expect in [(datetime(2026, 8, 17, 9, 20), {"NIFTY"}),        # Mon
                        (datetime(2026, 8, 18, 9, 20), {"NIFTY", "SENSEX"}),  # Tue: both
                        (datetime(2026, 8, 19, 9, 20), {"SENSEX"}),       # Wed
                        (datetime(2026, 8, 20, 9, 20), {"SENSEX"}),       # Thu
                        (datetime(2026, 8, 21, 9, 20), {"NIFTY"})]:       # Fri
        st, ctx = setup(chains=chains)
        sigs = tick(st, ctx, day)
        assert {s.symbol.split("|")[0] for s in sigs} == expect, day.strftime("%a")
        assert len(sigs) == 2 * len(expect)


def test_no_entry_before_0916_and_hard_exit_at_1525():
    st, ctx = setup()
    assert tick(st, ctx, at(9, 15)) == []   # pre-entry
    open_all(st, ctx, dt=at(9, 16))
    out = tick(st, ctx, at(15, 25))
    assert len(out) == 2 and all(s.reason == "isc_eod" for s in out)
    assert tick(st, ctx, at(15, 26)) == []  # and stays flat


def test_the_time_exit_does_not_wait_for_a_fresh_print():
    """15:25 is unconditional — a stale mark defers the SL/MTM checks, never the square-off."""
    st, ctx = setup()
    tick(st, ctx, FRI)                              # entered, but NO marks fed
    out = tick(st, ctx, at(15, 25))
    assert len(out) == 2 and all(s.reason == "isc_eod" for s in out)


def test_a_stale_mark_defers_the_stop_rather_than_judging_on_it():
    st, ctx = setup()
    tick(st, ctx, FRI)                              # entered; no prints yet
    assert tick(st, ctx, at(11)) == []
    assert leg_of(st, "NIFTY", "CE") is not None


def test_reentry_cutoff_stops_late_re_entries_but_not_the_open_leg():
    st, ctx = setup(mtm_stop_per_lot=NO_MTM)
    open_all(st, ctx)
    ce = leg_of(st, "NIFTY", "CE")
    mark(ctx, ce["symbol"], 140.0)
    out = tick(st, ctx, at(15, 1))   # past the 15:00 cutoff
    assert [s.action.name for s in out] == ["EXIT_ALL"]   # booked, not re-entered
    assert leg_of(st, "NIFTY", "PE") is not None          # the other leg runs to 15:25


def test_new_day_resets_counters_and_realized():
    st, ctx = setup(mtm_stop_per_lot=NO_MTM)
    open_all(st, ctx)
    mark(ctx, leg_of(st, "NIFTY", "CE")["symbol"], 140.0)
    tick(st, ctx, at(10))
    tick(st, ctx, at(15, 25))
    assert st.sides["NIFTY"]["CE"]["sl_reentries"] == 1 and st.realized["NIFTY"] != 0

    mon = datetime(2026, 8, 17, 9, 20)                    # next NIFTY day
    ctx.market.prices.clear()
    sigs = tick(st, ctx, mon)
    assert len(sigs) == 2                                 # fresh strangle
    assert st.sides["NIFTY"]["CE"]["sl_reentries"] == 0
    assert st.realized["NIFTY"] == 0.0


def test_state_round_trip():
    st, ctx = setup(mtm_stop_per_lot=NO_MTM)
    open_all(st, ctx)
    mark(ctx, leg_of(st, "NIFTY", "CE")["symbol"], 140.0)
    tick(st, ctx, at(10))
    exported = st.export_state()

    fresh = IntradayStrangleComboStrategy()
    fresh.load_state(exported)
    assert fresh.export_state() == exported
    assert fresh.sides["NIFTY"]["CE"]["sl_reentries"] == 1
    assert fresh.realized["NIFTY"] == st.realized["NIFTY"]
    assert fresh.sides["NIFTY"]["PE"]["leg"] == leg_of(st, "NIFTY", "PE")


def test_a_single_index_deploy_trades_only_that_index():
    """A DERIV deploy passes its index as ``universe=[…]`` (manager builds the strategy with
    universe=[config.underlying]) — NOT as ``underlyings``. Without honouring that, deploying
    "…_nifty" and "…_sensex" as two runs gave BOTH of them both indices, so on every shared
    day each would have doubled the other's book. Caught on the 2026-08-13 forward test,
    minutes before entry."""
    assert IntradayStrangleComboStrategy(universe=["SENSEX"]).underlyings == ["SENSEX"]
    assert IntradayStrangleComboStrategy(universe=["NIFTY"]).underlyings == ["NIFTY"]
    # Explicit underlyings still wins (the replay harness pins it that way)…
    assert IntradayStrangleComboStrategy(
        universe=["NIFTY"], underlyings=["SENSEX"]).underlyings == ["SENSEX"]
    # …and with neither, the whole schedule is the universe (a backtest of the full system).
    assert IntradayStrangleComboStrategy().underlyings == ["NIFTY", "SENSEX"]


def test_a_sensex_only_run_ignores_nifty_days_entirely():
    st, ctx = setup(chains={"NIFTY": chain(), "SENSEX": chain(spot=82000.0, step=100, lot=20)},
                    universe=["SENSEX"])
    assert tick(st, ctx, FRI) == []                          # Friday is NIFTY-only
    sigs = tick(st, ctx, datetime(2026, 8, 20, 9, 20))       # Thursday is SENSEX
    assert {s.symbol.split("|")[0] for s in sigs} == {"SENSEX"}


# --------------------------------------------------- reporting (cycle reconstruction)
def _tape():
    """Run 254's real 2026-08-13 tape: per-leg exits, same-instant re-entries, one side
    exhausting its budget while the other keeps trading."""
    rows = [
        ("09:16:19", "78200|CE", "SHORT", 72.90, None, None),
        ("09:16:19", "77600|PE", "SHORT", 74.45, None, None),
        ("10:24:02", "78200|CE", "COVER", 21.65, 3075.0, "isc_leg_target"),
        ("10:24:02", "78000|CE", "SHORT", 52.55, None, None),
        ("11:52:36", "78000|CE", "COVER", 75.00, -1347.0, "isc_leg_sl"),
        ("11:52:36", "78200|CE", "SHORT", 30.25, None, None),
        ("11:58:08", "77600|PE", "COVER", 22.35, 3126.0, "isc_leg_target"),
        ("11:58:08", "77600|PE", "SHORT", 22.30, None, None),
        ("11:59:38", "78200|CE", "COVER", 44.75, -870.0, "isc_leg_sl"),
        ("11:59:38", "78200|CE", "SHORT", 44.65, None, None),
        ("12:03:10", "78200|CE", "COVER", 64.55, -1194.0, "isc_leg_sl"),
        ("13:52:27", "77600|PE", "COVER", 6.55, 945.0, "isc_leg_target"),
        ("13:52:27", "77600|PE", "SHORT", 6.50, None, None),
        ("14:07:00", "77600|PE", "COVER", 10.00, -210.0, "isc_leg_sl"),
        ("14:07:00", "77600|PE", "SHORT", 9.80, None, None),
    ]
    return [{"date": f"2026-08-13T{t}+05:30", "ticker": f"SENSEX|2026-08-13|{leg}",
             "action": a, "units": 60, "price": p, "profit": pr, "exit_reason": r}
            for t, leg, a, p, pr, r in rows]


def test_a_days_per_leg_re_entries_are_ONE_cycle_not_several():
    """A leg that exits and re-enters in the SAME decision is never flat. Testing the book
    between those two rows reported run 254's single 2026-08-13 session as THREE cycles."""
    from skas_algo.services.cycle_detail import reconstruct_cycles

    cycles = reconstruct_cycles(_tape())
    assert len(cycles) == 1
    assert cycles[0]["entry_date"].startswith("2026-08-13T09:16")
    assert cycles[0]["exit_date"] is None                       # still open at 14:07
    assert round(cycles[0]["realized_pnl"]) == 3525             # matches the tape exactly


def test_each_re_entry_is_its_own_leg_not_a_fattened_one():
    """CE 78200 was sold three separate times at 3 lots. Collapsing them by symbol invented
    a 9-lot position at their average price and hid the intermediate exits — which is what
    made the narrator call a paired close+open a 'post-iron-fly ADD'."""
    from skas_algo.services.cycle_detail import reconstruct_cycles

    legs = reconstruct_cycles(_tape())[0]["legs_detail"]
    assert len(legs) == 8                                       # 8 episodes, not 3 symbols
    assert {lg["units"] for lg in legs} == {60}                 # every one is 3 lots
    ce78200 = sorted(lg["entry_premium"] for lg in legs
                     if lg["strike"] == 78200.0 and lg["right"] == "CE")
    assert ce78200 == [30.25, 44.65, 72.90]                     # true entries, not 49.27
    assert sum(1 for lg in legs if lg["exit_price"] is None) == 1   # only the last PE is open


def test_an_intraday_cycle_gets_a_real_spot_line():
    """A 0DTE cycle's window is one day, so daily closes give ONE point — and a one-point
    polyline draws nothing, which is why the SENSEX 2026-08-13 ladder showed a spot legend
    and no spot line. Intraday cycles use the trade rows' own minute-accurate spot."""
    from datetime import date as _d

    from skas_algo.services.cycle_detail import _spot_path

    rows = [dict(t, underlying_spot=s) for t, s in zip(
        _tape(), [77859, 77859, 77726, 77726, 77856, 77856, 77886,
                  77886, 77925, 77925, 78002, 77910, 77910, 77892, 77892], strict=True)]
    day = _d(2026, 8, 13)
    pts = _spot_path(lambda d: None, day, day, 77859, 77892, day, rows)
    assert len(pts) >= 2                              # an actual line, not a dot
    assert all("T" in p["date"] for p in pts)         # minute-accurate, not a bare date
    assert [p["spot"] for p in pts][:3] == [77859, 77726, 77856]
    assert len({p["date"] for p in pts}) == len(pts)  # one point per instant, deduped

    # A multi-day cycle is untouched — still daily closes at the session date.
    multi = _spot_path(lambda d: 78000, day, _d(2026, 8, 20), 77859, None, None, rows)
    assert len(multi) == 8 and all("T" not in p["date"] for p in multi)


# ------------------------------------------------- skip_same_strike_reentry
def test_skip_same_strike_reentry_waits_for_the_strike_to_actually_move():
    """When the recomputed OTM3 is the strike that just stopped, a "re-entry" repositions
    nothing — it books the loss, pays the round trip, and only re-bases the stop wider.
    With the flag on, the side stays flat and ARMED, and enters the moment spot moves
    enough to change the strike. The budget is charged when the re-entry lands, not when
    it is owed."""
    st, ctx = setup(mtm_stop_per_lot=NO_MTM, same_strike_action="skip")
    open_all(st, ctx)
    ce = leg_of(st, "NIFTY", "CE")
    assert ce["strike"] == 25150.0

    mark(ctx, ce["symbol"], 140.0)                    # stop, spot unmoved
    out = tick(st, ctx, at(10))
    assert [s.action.name for s in out] == ["EXIT_ALL"]        # booked, NOT re-sold
    side = st.sides["NIFTY"]["CE"]
    assert side["pending"] == "sl" and side["blocked_strike"] == 25150.0
    assert side["sl_reentries"] == 0                  # nothing landed → nothing charged
    assert side["closed_for_day"] is False            # still armed

    assert tick(st, ctx, at(10, 1)) == []             # strike still 25150 → keep waiting

    ctx.market.chains["NIFTY"] = chain(spot=25200.0)  # spot moves → OTM3 becomes 25350
    out = tick(st, ctx, at(10, 2))
    assert [s.action.name for s in out] == ["ENTER_SHORT"]
    assert out[0].symbol.split("|")[2] == "25350"
    assert side["sl_reentries"] == 1                  # charged only now
    assert side["pending"] is None and side["blocked_strike"] is None


def test_the_flag_is_off_by_default_so_a_recovered_deploy_is_unchanged():
    st, ctx = setup(mtm_stop_per_lot=NO_MTM)
    assert st.same_strike_action == "reenter"
    open_all(st, ctx)
    mark(ctx, leg_of(st, "NIFTY", "CE")["symbol"], 140.0)
    out = tick(st, ctx, at(10))
    assert [s.action.name for s in out] == ["EXIT_ALL", "ENTER_SHORT"]   # same-strike re-sell
    assert st.sides["NIFTY"]["CE"]["sl_reentries"] == 1


def test_an_owed_reentry_survives_a_restart():
    """Losing `pending` would leave the counters spent but the side looking untraded, so
    the flat path would enter again without charging a re-entry — free budget."""
    st, ctx = setup(mtm_stop_per_lot=NO_MTM, same_strike_action="skip")
    open_all(st, ctx)
    mark(ctx, leg_of(st, "NIFTY", "CE")["symbol"], 140.0)
    tick(st, ctx, at(10))

    fresh = IntradayStrangleComboStrategy(mtm_stop_per_lot=NO_MTM, same_strike_action="skip")
    fresh.load_state(st.export_state())
    assert fresh.sides["NIFTY"]["CE"]["pending"] == "sl"
    assert fresh.sides["NIFTY"]["CE"]["blocked_strike"] == 25150.0
    assert fresh.export_state() == st.export_state()


def test_hold_mode_defers_the_exit_until_the_strike_can_move_away():
    """"hold": when the recomputed OTM3 is the strike we're already on there is nothing to
    roll TO, so carry the leg. The exit is DEFERRED, not cancelled — the moment the strike
    moves, the leg is booked and re-sold there."""
    st, ctx = setup(mtm_stop_per_lot=NO_MTM, same_strike_action="hold")
    open_all(st, ctx)
    ce = leg_of(st, "NIFTY", "CE")

    mark(ctx, ce["symbol"], 140.0)                     # stop breached, spot unmoved
    assert tick(st, ctx, at(10)) == []                 # held, not booked
    assert leg_of(st, "NIFTY", "CE") is ce             # the SAME leg, still on
    assert st.realized["NIFTY"] == 0.0

    mark(ctx, ce["symbol"], 200.0)                     # deeper underwater, strike still pinned
    assert tick(st, ctx, at(10, 1)) == []              # still held — the deferral has no cap
    assert st.sides["NIFTY"]["CE"]["sl_reentries"] == 0

    ctx.market.chains["NIFTY"] = chain(spot=25200.0)   # strike can move now → roll away
    out = tick(st, ctx, at(10, 2))
    assert [s.action.name for s in out] == ["EXIT_ALL", "ENTER_SHORT"]
    assert out[1].symbol.split("|")[2] == "25350"
    assert st.realized["NIFTY"] == (100.0 - 200.0) * ce["units"]  # at the DEFERRED price
    assert st.sides["NIFTY"]["CE"]["sl_reentries"] == 1


def test_hold_still_squares_off_at_the_hard_time_exit():
    """The deferral must never outrank 15:25 — that is the one gate nothing bypasses."""
    st, ctx = setup(mtm_stop_per_lot=NO_MTM, same_strike_action="hold")
    open_all(st, ctx)
    mark(ctx, leg_of(st, "NIFTY", "CE")["symbol"], 300.0)
    assert tick(st, ctx, at(14)) == []                 # deferred all afternoon
    out = tick(st, ctx, at(15, 25))
    assert len(out) == 2 and all(s.reason == "isc_eod" for s in out)


def test_hold_does_not_outrank_the_overall_mtm_stop():
    """The MTM stop is the only backstop while a deferred leg runs — it must still fire."""
    st, ctx = setup(same_strike_action="hold")         # default NIFTY Rs1,500/lot
    open_all(st, ctx)
    mark(ctx, leg_of(st, "NIFTY", "CE")["symbol"], 140.0)   # -Rs3,000 → past the budget
    out = tick(st, ctx, at(10))
    assert out and all(s.reason == "isc_mtm_stop" for s in out)


def test_otm_steps_zero_is_a_straddle():
    """0 steps = both legs on the ATM strike. Everything else — the per-leg 40/70 exits, the
    independent budgets, the re-entry modes — is structure-agnostic and applies unchanged."""
    st, ctx = setup(otm_steps=0)
    sigs = tick(st, ctx, FRI)
    assert {s.symbol.split("|")[2] for s in sigs} == {"25000"}        # both at ATM
    assert {s.symbol.split("|")[3] for s in sigs} == {"CE", "PE"}     # still two distinct legs
    assert leg_of(st, "NIFTY", "CE")["strike"] == leg_of(st, "NIFTY", "PE")["strike"]


def test_ui_copy_never_hardcodes_the_otm_offset():
    """`otm_steps` is a knob, so no user-facing string may name a particular value — at 0
    the structure is a STRADDLE and "OTM0" is nonsense."""
    assert "the ATM strike" in IntradayStrangleComboStrategy(otm_steps=0).exit_rules()[1]
    assert "OTM0" not in " ".join(IntradayStrangleComboStrategy(otm_steps=0).exit_rules())
    assert "OTM2" in " ".join(IntradayStrangleComboStrategy(otm_steps=2).exit_rules())


def test_zero_leg_thresholds_mean_OFF_not_breakeven():
    """0 must disable the leg exit. Read literally the comparison is `cur >= entry`, a
    breakeven stop that fires on the first tick — the run #249 trap (intraday_straddle,
    2026-08-07), where a 0% SL silently stopped out every single trade."""
    st, ctx = setup(mtm_stop_per_lot=NO_MTM, leg_stop_pct=0, leg_target_pct=0)
    open_all(st, ctx)
    ce = leg_of(st, "NIFTY", "CE")
    mark(ctx, ce["symbol"], 100.0)          # exactly at entry
    assert tick(st, ctx, at(10)) == []
    mark(ctx, ce["symbol"], 100.01)         # a hair above — a breakeven stop would fire
    assert tick(st, ctx, at(10, 1)) == []
    mark(ctx, ce["symbol"], 1.0)            # and a hair from zero on the target side
    assert tick(st, ctx, at(10, 2)) == []
    assert leg_of(st, "NIFTY", "CE") is ce  # still holding, untouched


# ------------------------------------------------------------------ wings (iron fly)
def test_wings_enter_with_the_shorts_making_an_iron_fly():
    """wing_steps=3 on the ATM straddle = an intraday iron fly: long protection 150 pts
    beyond each short, same expiry, same units, bought in the same slice."""
    st, ctx = setup(mtm_stop_per_lot=NO_MTM, otm_steps=0, wing_steps=3)
    sigs = tick(st, ctx, FRI)
    assert [s.action.name for s in sigs] == ["ENTER_SHORT", "ENTER_LONG",
                                             "ENTER_SHORT", "ENTER_LONG"]
    ks = {(s.symbol.split("|")[3], s.action.name): s.symbol.split("|")[2] for s in sigs}
    assert ks[("CE", "ENTER_SHORT")] == "25000" and ks[("CE", "ENTER_LONG")] == "25150"
    assert ks[("PE", "ENTER_SHORT")] == "25000" and ks[("PE", "ENTER_LONG")] == "24850"
    assert st.sides["NIFTY"]["CE"]["wing"]["units"] == 75.0


def test_no_priceable_wing_means_no_entry_at_all():
    """The defined-risk promise must never silently degrade to a naked short."""
    c = chain()
    for r in c["rows"]:
        if r["strike"] == 25150.0:
            r["ce"] = None                       # the CE wing strike is unpriceable
    st, ctx = setup(chains={"NIFTY": c}, mtm_stop_per_lot=NO_MTM, otm_steps=0, wing_steps=3)
    sigs = tick(st, ctx, FRI)
    assert {s.symbol.split("|")[3] for s in sigs} == {"PE"}      # PE side intact
    assert st.sides["NIFTY"]["CE"]["leg"] is None                # CE fully skipped


def test_a_finished_side_sells_its_wing_and_banks_its_pnl():
    st, ctx = setup(mtm_stop_per_lot=NO_MTM, otm_steps=0, wing_steps=3,
                    max_sl_reentries=0)
    open_all(st, ctx)
    ce_wing = st.sides["NIFTY"]["CE"]["wing"]
    mark(ctx, ce_wing["symbol"], ce_wing["entry"] + 10)          # wing gained 10
    mark(ctx, leg_of(st, "NIFTY", "CE")["symbol"], 140.0)        # short stopped, no budget
    out = tick(st, ctx, at(10))
    assert [s.action.name for s in out] == ["EXIT_ALL", "EXIT_ALL"]   # short AND wing
    assert st.sides["NIFTY"]["CE"]["wing"] is None
    assert st.realized["NIFTY"] == (100.0 - 140.0) * 75 + 10 * 75
    assert st.sides["NIFTY"]["PE"]["wing"] is not None           # other side untouched


def test_eod_closes_wings_with_everything_else():
    st, ctx = setup(mtm_stop_per_lot=NO_MTM, otm_steps=0, wing_steps=3)
    open_all(st, ctx)
    for r in ("CE", "PE"):
        mark(ctx, st.sides["NIFTY"][r]["wing"]["symbol"], 5.0)
    out = tick(st, ctx, at(15, 25))
    assert len(out) == 4 and all(s.reason == "isc_eod" for s in out)
    assert all(st.sides["NIFTY"][r]["wing"] is None for r in ("CE", "PE"))


def test_wing_rolls_when_its_sides_reentry_moves_strike():
    """A target re-entry at a new strike drags the wing along — close old, buy new — and
    the closes precede the opens (every EXIT_ALL resolves against the pre-slice book)."""
    st, ctx = setup(mtm_stop_per_lot=NO_MTM, otm_steps=0, wing_steps=3)
    open_all(st, ctx)
    old_wing = st.sides["NIFTY"]["CE"]["wing"]
    mark(ctx, old_wing["symbol"], old_wing["entry"])
    ctx.market.chains["NIFTY"] = chain(spot=25200.0)             # ATM moved to 25200
    mark(ctx, leg_of(st, "NIFTY", "CE")["symbol"], 30.0)         # −70% → target re-entry
    out = tick(st, ctx, at(10))
    acts = [(s.action.name, s.symbol.split("|")[2]) for s in out]
    assert acts == [("EXIT_ALL", "25000"),        # old short
                    ("EXIT_ALL", "25150"),        # old wing — close BEFORE any open
                    ("ENTER_SHORT", "25200"),     # new short at the new ATM
                    ("ENTER_LONG", "25350")]      # new wing 3 steps beyond it
    assert st.sides["NIFTY"]["CE"]["wing"]["strike"] == 25350.0


def test_wing_gain_offsets_the_mtm_stop():
    """The stop reads the WHOLE book. Shorts −Rs3,000 alone would breach Rs1,500×1; a wing
    up Rs2,000 keeps the day inside budget — that offset is the iron fly working."""
    st, ctx = setup(otm_steps=0, wing_steps=3)                   # NIFTY Rs1,500/lot stop ON
    open_all(st, ctx)
    for r in ("CE", "PE"):
        w = st.sides["NIFTY"][r]["wing"]
        mark(ctx, w["symbol"], w["entry"])
    mark(ctx, leg_of(st, "NIFTY", "CE")["symbol"], 120.0)        # shorts: −20×75 −Rs1,500... 
    mark(ctx, leg_of(st, "NIFTY", "PE")["symbol"], 106.0)        # −6×75 → −Rs1,950 total
    w = st.sides["NIFTY"]["CE"]["wing"]
    mark(ctx, w["symbol"], w["entry"] + 27)                      # wing +Rs2,025
    assert tick(st, ctx, at(11)) == []                           # inside budget → no stop
    mark(ctx, w["symbol"], w["entry"])                           # wing gain gone
    out = tick(st, ctx, at(11, 1))
    assert out and all(s.reason == "isc_mtm_stop" for s in out)
    assert all(st.sides["NIFTY"][r]["wing"] is None for r in ("CE", "PE"))


def test_wing_state_round_trips():
    st, ctx = setup(mtm_stop_per_lot=NO_MTM, otm_steps=0, wing_steps=3)
    open_all(st, ctx)
    fresh = IntradayStrangleComboStrategy(mtm_stop_per_lot=NO_MTM, otm_steps=0, wing_steps=3)
    fresh.load_state(st.export_state())
    assert fresh.export_state() == st.export_state()
    assert fresh.sides["NIFTY"]["PE"]["wing"]["strike"] == 24850.0


def test_every_deploy_model_field_is_a_real_ctor_kwarg():
    """The deploy route enumerates kwargs by hand and the ctor ends in **_ignored, so a
    typo'd or unforwarded knob is accepted and silently dropped (the fvc roll_days_before
    lesson). The strategy had NO deploy surface until 2026-08-27 — the Aug-13 live
    carve-out was engine-only and deploys were raw-API only."""
    import inspect

    from skas_algo.api.models import IntradayStrangleComboDeploy

    ctor = set(inspect.signature(IntradayStrangleComboStrategy.__init__).parameters)
    infra = {"name", "notes", "underlying", "capital", "mode", "quote_source",
             "broker_account_id", "refresh_seconds", "ignore_market_hours", "auto"}
    strategy_fields = set(IntradayStrangleComboDeploy.model_fields) - infra
    missing = strategy_fields - ctor
    assert not missing, f"deploy model sends non-ctor params: {sorted(missing)}"
