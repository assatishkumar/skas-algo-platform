"""fair_value_calendar: the premium-matched ratio structure, the fair-value side pick, the
900-point gap rule, weekly rolls (incl. the buy-leg push-out), the %-of-margin target and
the settlement backstop — fake market/chain, no network."""

from __future__ import annotations

from datetime import date, datetime

from skas_algo.engine.options.contract_specs import lot_size_for
from skas_algo.strategies.fair_value_calendar import FairValueCalendarStrategy

W1 = date(2026, 8, 4)        # too-near weekly (< min_sold_dte from the entry day)
W2 = date(2026, 8, 11)       # the sold weekly
W3 = date(2026, 8, 18)
AUG_MON = date(2026, 8, 25)  # August monthly (the buy leg)
SEP_W1 = date(2026, 9, 1)
SEP_MON = date(2026, 9, 29)
EXPIRIES = [W1, W2, W3, AUG_MON, SEP_W1, SEP_MON]

DAY = datetime(2026, 8, 3, 9, 45)   # Monday, inside the 09:30-15:00 window
LOT = lot_size_for("NIFTY", W2)


def at(d: date, h: int, m: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, h, m)


def chain(pe_anchor: float, ce_anchor: float, spot: float = 25000.0):
    """Linear premium curves: pe(k) = (k − pe_anchor)·0.5, ce(k) = (ce_anchor − k)·0.5.
    Defaults put the worked example on the grid: weekly pe_anchor 24400 → ₹150 @ 24700 and
    ₹450 @ 25300; monthly pe_anchor 24100 → ₹200 @ 24500."""
    rows = []
    for i in range(-25, 26):
        k = float(25000 + i * 100)
        rows.append({
            "strike": k,
            "pe": {"ltp": max(0.5, (k - pe_anchor) * 0.5), "oi": 5000},
            "ce": {"ltp": max(0.5, (ce_anchor - k) * 0.5), "oi": 5000},
        })
    return {"spot": spot, "atm_strike": 25000.0, "lot_size": LOT, "rows": rows}


def weekly_chain(**kw):
    return chain(pe_anchor=24400.0, ce_anchor=25600.0, **kw)


def monthly_chain(**kw):
    return chain(pe_anchor=24100.0, ce_anchor=25900.0, **kw)


class FakeChain:
    def __init__(self, expiries):
        self._e = expiries

    def expiries(self, _u, _today):
        return [e.isoformat() for e in self._e]


class FakeMarket:
    def __init__(self, by_expiry, spot=25000.0):
        self.by_expiry = by_expiry          # iso → chain dict
        self.spot = spot
        self.prices: dict[str, float] = {}

    def live_chain(self, _u, expiry_iso):
        return self.by_expiry.get(str(expiry_iso)[:10])

    def index_spot(self, _u):
        return self.spot

    def has_print(self, s):
        return s in self.prices


class FakeCtx:
    def __init__(self, market, expiries=None):
        self.market = market
        self._chain = FakeChain(expiries or EXPIRIES)
        self._now = None
        self.positions: dict[str, int] = {}

    def now(self):
        return self._now

    def today(self):
        return self._now.date()

    def option_chain(self):
        return self._chain

    def lots(self, s):
        return self.positions.get(s, 0)

    def close(self, s):
        if s in self.market.prices:
            return self.market.prices[s]
        raise KeyError(s)


def setup(weeklies=None, monthlies=None, spot=25000.0, expiries=None, **kw):
    kw.setdefault("side_mode", "pe")
    st = FairValueCalendarStrategy(universe=["NIFTY"], **kw)
    wc = weeklies if weeklies is not None else weekly_chain(spot=spot)
    mc = monthlies if monthlies is not None else monthly_chain(spot=spot)
    mkt = FakeMarket({W1.isoformat(): wc, W2.isoformat(): wc, W3.isoformat(): wc,
                      SEP_W1.isoformat(): wc,
                      AUG_MON.isoformat(): mc, SEP_MON.isoformat(): mc}, spot=spot)
    return st, FakeCtx(mkt, expiries)


def tick(st, ctx, dt):
    ctx._now = dt
    sigs = st.on_slice(ctx)
    for s in sigs:
        if s.action.name in ("ENTER_SHORT", "ENTER_LONG"):
            ctx.positions[s.symbol] = s.quantity
        elif s.action.name == "EXIT_ALL":
            ctx.positions.pop(s.symbol, None)
    return sigs


def mark_all(st, ctx):
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = leg["entry"]


def strike(sym):
    return float(sym.split("|")[2])


def expiry(sym):
    return sym.split("|")[1]


# ------------------------------------------------------------------ structure
def test_pe_side_builds_the_worked_example():
    """Screenshot #126 on the fake grid: sell ~150 (24700) + ~450 (25300) on the weekly,
    buy 3 lots ~200 (24500) on the monthly — premiums matched by construction."""
    st, ctx = setup()
    sigs = tick(st, ctx, DAY)
    assert [s.action.name for s in sigs] == ["ENTER_SHORT", "ENTER_SHORT", "ENTER_LONG"]
    shorts = [s for s in sigs if s.action.name == "ENTER_SHORT"]
    long = next(s for s in sigs if s.action.name == "ENTER_LONG")
    assert {strike(s.symbol) for s in shorts} == {24700.0, 25300.0}
    assert strike(long.symbol) == 24500.0
    assert {expiry(s.symbol) for s in shorts} == {W2.isoformat()}   # ≥4 DTE: W1 skipped
    assert expiry(long.symbol) == AUG_MON.isoformat()
    assert all(s.quantity == LOT for s in shorts)
    assert long.quantity == 3 * LOT
    sold = sum(leg["entry"] for leg in st.legs if leg["dir"] < 0)
    bought = 3 * next(leg["entry"] for leg in st.legs if leg["dir"] > 0)
    assert sold == bought                       # 150 + 450 == 3 × 200
    assert st.entered_month == "2026-08" and st.roll_count == 0
    assert st.cycle_side == "PE"


def test_ce_side_mirrors():
    st, ctx = setup(side_mode="ce")
    sigs = tick(st, ctx, DAY)
    shorts = {strike(s.symbol) for s in sigs if s.action.name == "ENTER_SHORT"}
    long = next(s for s in sigs if s.action.name == "ENTER_LONG")
    assert shorts == {25300.0, 24700.0}          # ce(25300)=150, ce(24700)=450
    assert strike(long.symbol) == 25500.0        # monthly ce(25500)=200
    assert all(s.symbol.split("|")[3] == "CE" for s in sigs)


def test_both_mode_builds_both_sides_in_one_decision():
    st, ctx = setup(side_mode="both")
    sigs = tick(st, ctx, DAY)
    assert len(sigs) == 6
    assert {s.symbol.split("|")[3] for s in sigs} == {"PE", "CE"}
    assert st.cycle_side == "BOTH"
    assert st.sell_lots == 4                     # harness sizing hint doubles


def test_fair_value_side_pick():
    """Above the band → puts; below → calls; inside → calls (rollover preference).
    fv on 2026-08-03 with the video constants ≈ ₹25.7k."""
    for spot, want in ((27500.0, "PE"), (23000.0, "CE"), (25400.0, "CE")):
        st, ctx = setup(side_mode="fair_value", spot=spot)
        tick(st, ctx, DAY)
        assert st.cycle_side == want, (spot, st.cycle_side, st.entry_dev_pct)
        assert st.entry_fv and 25000 < st.entry_fv < 26300


# ------------------------------------------------------------------ entry gates
def test_gap_rule_skips_the_day_but_not_the_month():
    """Monthly hunt landing 1,000 pts from the far sell → skip; a sane chain the next
    day enters — the gap rule never burns the month."""
    st, ctx = setup(monthlies=chain(pe_anchor=23900.0, ce_anchor=26100.0))  # 200 @ 24300
    assert tick(st, ctx, DAY) == []              # |25300 − 24300| = 1000 > 900
    assert st.entered_month is None and st.legs == []
    ctx.market.by_expiry[AUG_MON.isoformat()] = monthly_chain()
    assert len(tick(st, ctx, at(date(2026, 8, 4), 10))) == 3


def test_tolerance_miss_retries():
    """No strike within 30% of ₹450 → no entry, nothing stamped."""
    dead = weekly_chain()
    for r in dead["rows"]:
        if r["pe"]["ltp"] > 300:                 # kill everything near the 450 target
            r["pe"] = None
    st, ctx = setup(weeklies=dead)
    assert tick(st, ctx, DAY) == []
    assert st.entered_month is None


def test_entry_window_and_month_latch():
    st, ctx = setup()
    ctx._now = at(date(2026, 8, 3), 9, 15)
    assert st.on_slice(ctx) == []                # before 09:30
    tick(st, ctx, DAY)
    # cycle over (however it ended) → the month is spent
    st.legs = []
    ctx.positions.clear()
    assert tick(st, ctx, at(date(2026, 8, 12), 10)) == []
    assert st.entered_month == "2026-08"


def test_sold_and_buy_collision_at_entry_pushes_buy_to_next_month():
    """Entering late-month: the ≥4-DTE weekly IS the monthly → buy on next month's."""
    st, ctx = setup()
    sigs = tick(st, ctx, at(date(2026, 8, 20), 10))
    long = next(s for s in sigs if s.action.name == "ENTER_LONG")
    shorts = {expiry(s.symbol) for s in sigs if s.action.name == "ENTER_SHORT"}
    assert shorts == {AUG_MON.isoformat()}
    assert expiry(long.symbol) == SEP_MON.isoformat()


def test_era_scaling_halves_the_hunt_targets():
    """Pre-cutoff entries hunt spot/ref-scaled premiums: ref = 2× spot → 75/225/100,
    which sit on different strikes of the same curves."""
    # tolerance 40: the ₹50-per-strike grid puts the nearest-75 strike 25 off (33%)
    st, ctx = setup(premium_scale_before="2027-01-01", premium_ref_spot=50000.0,
                    max_gap_points=100000.0, premium_tolerance_pct=40.0)
    sigs = tick(st, ctx, DAY)
    shorts = {strike(s.symbol) for s in sigs if s.action.name == "ENTER_SHORT"}
    long = next(s for s in sigs if s.action.name == "ENTER_LONG")
    assert shorts == {24500.0, 24800.0}          # pe=50 (nearest 75) and pe=200 (nearest 225)
    assert strike(long.symbol) == 24300.0        # monthly pe(24300) = 100 exact
    assert st.entry_scale == 0.5


def test_era_scale_helper():
    st, _ = setup()
    assert st._era_scale(date(2024, 8, 1), 20000.0) == 1.0     # cutoff day → absolute
    assert st._era_scale(date(2024, 7, 31), 12250.0) == 0.5    # before → spot/ref


# ------------------------------------------------------------------ rolls
def enter(st, ctx, dt=DAY):
    sigs = tick(st, ctx, dt)
    assert sigs, "entry failed"
    mark_all(st, ctx)
    return sigs


def test_roll_day_resells_same_strikes_on_next_weekly():
    st, ctx = setup()
    enter(st, ctx)
    sold_syms = [leg["symbol"] for leg in st.legs if leg["dir"] < 0]
    ctx.market.prices[next(s for s in sold_syms if strike(s) == 24700.0)] = 40.0
    ctx.market.prices[next(s for s in sold_syms if strike(s) == 25300.0)] = 120.0

    assert tick(st, ctx, at(W2, 14, 59)) == []           # before roll_time
    out = tick(st, ctx, at(W2, 15, 0))
    assert [s.action.name for s in out] == ["EXIT_ALL", "EXIT_ALL",
                                            "ENTER_SHORT", "ENTER_SHORT"]
    assert all(s.reason == "fvc_roll" for s in out)
    news = [s for s in out if s.action.name == "ENTER_SHORT"]
    assert {strike(s.symbol) for s in news} == {24700.0, 25300.0}   # SAME strikes
    assert {expiry(s.symbol) for s in news} == {W3.isoformat()}     # next weekly
    assert st.roll_count == 1 and st.sold_expiry == W3.isoformat()
    assert st.realized_rolls == (150.0 - 40.0) * LOT + (450.0 - 120.0) * LOT
    # the monthly long is untouched
    assert any(leg["dir"] > 0 and expiry(leg["symbol"]) == AUG_MON.isoformat()
               for leg in st.legs)


def test_collision_closes_the_cycle_and_a_fresh_one_opens():
    """Sold 8/18 rolling into 8/25 — the buy expiry: the buy leg is NEVER rolled (owner
    rule) — everything closes, the month latch clears, and the NEXT session hunts a
    fresh cycle (sells on the Aug monthly, buys on the Sep monthly)."""
    st, ctx = setup()
    enter(st, ctx, at(date(2026, 8, 13), 10))            # sold W3, buy AUG_MON
    assert st.sold_expiry == W3.isoformat() and st.buy_expiry == AUG_MON.isoformat()
    out = tick(st, ctx, at(W3, 15, 0))
    assert [s.action.name for s in out] == ["EXIT_ALL"] * 3
    assert all(s.reason == "fvc_cycle_end" for s in out)
    assert st.legs == [] and st.entered_month is None    # month latch cleared
    fresh = tick(st, ctx, at(date(2026, 8, 19), 9, 45))
    assert len(fresh) == 3                               # a NEW cycle, immediately
    shorts = {expiry(s.symbol) for s in fresh if s.action.name == "ENTER_SHORT"}
    long = next(s for s in fresh if s.action.name == "ENTER_LONG")
    assert shorts == {AUG_MON.isoformat()}               # ≥4 DTE from 8/19 → the monthly
    assert expiry(long.symbol) == SEP_MON.isoformat()    # buy beyond the sold, next month
    assert st.roll_count == 0                            # fresh cycle, fresh counters


def test_max_rolls_closes_the_cycle():
    st, ctx = setup(max_rolls=1)
    enter(st, ctx)
    tick(st, ctx, at(W2, 15, 0))                          # roll #1
    mark_all(st, ctx)
    out = tick(st, ctx, at(W3, 15, 0))
    assert out and all(s.action.name == "EXIT_ALL" for s in out)
    assert all(s.reason == "fvc_max_rolls" for s in out)
    assert st.entered_month == "2026-08" and st.legs == []


def test_unpriceable_roll_defers():
    """The re-sell strike must PRICE on the next weekly — a store hole defers the whole
    roll to the next tick rather than rolling half a structure."""
    st, ctx = setup()
    enter(st, ctx)
    dead = weekly_chain()
    for r in dead["rows"]:
        if r["strike"] == 25300.0:
            r["pe"] = None
    ctx.market.by_expiry[W3.isoformat()] = dead
    assert tick(st, ctx, at(W2, 15, 0)) == []
    assert st.roll_count == 0
    ctx.market.by_expiry[W3.isoformat()] = weekly_chain()
    assert len(tick(st, ctx, at(W2, 15, 1))) == 4


# ------------------------------------------------------------------ target / margin
def test_target_needs_the_frozen_broker_margin():
    st, ctx = setup()
    enter(st, ctx)
    for leg in st.legs:                                   # deep profit on the shorts
        if leg["dir"] < 0:
            ctx.market.prices[leg["symbol"]] = leg["entry"] * 0.1
    assert tick(st, ctx, at(date(2026, 8, 5), 11)) == []  # margin pending → target waits
    st.set_broker_margin(200000.0)
    out = tick(st, ctx, at(date(2026, 8, 5), 11, 1))
    assert out and all(s.reason == "target" for s in out)
    assert st.legs == [] and st.entered_month == "2026-08"
    assert st.margin_base == 0.0                          # entry basis re-freezes next cycle


def test_margin_pending_never_blocks_the_roll():
    st, ctx = setup()
    enter(st, ctx)
    out = tick(st, ctx, at(W2, 15, 5))
    assert len(out) == 4 and st.roll_count == 1


# ------------------------------------------------------------------ settlement backstop
def test_settled_sold_legs_bank_and_resell():
    """The engine settled the sold weeklies (roll couldn't fill — store hole): they're
    banked at intrinsic-vs-spot and re-sold on the next weekly, no EXIT signals."""
    st, ctx = setup()
    enter(st, ctx)
    for leg in list(st.legs):
        if leg["dir"] < 0:
            ctx.positions.pop(leg["symbol"], None)        # engine took them at expiry
    out = tick(st, ctx, at(date(2026, 8, 12), 9, 40))     # day after the sold expiry
    assert [s.action.name for s in out] == ["ENTER_SHORT", "ENTER_SHORT"]
    assert {expiry(s.symbol) for s in out} == {W3.isoformat()}
    # settled intrinsic at spot 25000: 24700 PE → 0, 25300 PE → 300
    assert st.realized_rolls == (150.0 - 0.0) * LOT + (450.0 - 300.0) * LOT
    assert st.pending_resell is False and st.roll_count == 1


# ------------------------------------------------------------------ state
def test_state_round_trips():
    st, ctx = setup()
    enter(st, ctx)
    tick(st, ctx, at(W2, 15, 0))
    fresh = FairValueCalendarStrategy(universe=["NIFTY"])
    fresh.load_state(st.export_state())
    assert fresh.export_state() == st.export_state()
    assert fresh.sold_expiry == W3.isoformat() and fresh.roll_count == 1
    assert fresh.sold_specs == st.sold_specs


# --------------------------------------------------------------- roll_days_before
def test_roll_fires_the_trading_day_before_expiry_when_asked():
    """Expiry-day margin on a short weekly spikes into settlement, so the owner's policy is
    to roll the session BEFORE (owner rule 2026-08-20). W2 is Tue 11 Aug → roll Mon 10 Aug."""
    st, ctx = setup(roll_days_before=1)
    enter(st, ctx)
    sold_syms = [leg["symbol"] for leg in st.legs if leg["dir"] < 0]
    ctx.market.prices[next(s for s in sold_syms if strike(s) == 24700.0)] = 40.0
    ctx.market.prices[next(s for s in sold_syms if strike(s) == 25300.0)] = 120.0

    day_before = date(2026, 8, 10)                       # Monday
    assert st._roll_date(W2) == day_before
    assert tick(st, ctx, at(day_before, 14, 59)) == []   # before roll_time — nothing yet
    out = tick(st, ctx, at(day_before, 15, 0))
    assert [s.action.name for s in out] == ["EXIT_ALL", "EXIT_ALL",
                                            "ENTER_SHORT", "ENTER_SHORT"]
    news = [s for s in out if s.action.name == "ENTER_SHORT"]
    assert {expiry(s.symbol) for s in news} == {W3.isoformat()}   # still the NEXT weekly
    assert st.sold_expiry == W3.isoformat() and st.roll_count == 1


def test_roll_days_before_defaults_to_the_old_expiry_day_roll():
    """CLAUDE.md §1: the ctor default must leave a recovered deploy rolling where it did."""
    st, _ = setup()
    assert st.roll_days_before == 0 and st._roll_date(W2) == W2


def test_roll_date_skips_weekends_and_holidays():
    """Trading days, not calendar days — a Monday expiry rolls back to Friday."""
    st, _ = setup(roll_days_before=1)
    assert st._roll_date(date(2026, 8, 17)) == date(2026, 8, 14)   # Mon → Fri
    st.roll_days_before = 2
    assert st._roll_date(date(2026, 8, 11)) == date(2026, 8, 7)    # Tue → Fri (skips the weekend)


def test_a_missed_early_roll_still_catches_up_on_expiry_day():
    """No chain / no print on the roll day must not strand the sells into settlement."""
    st, ctx = setup(roll_days_before=1)
    enter(st, ctx)
    sold_syms = [leg["symbol"] for leg in st.legs if leg["dir"] < 0]
    ctx.market.prices[next(s for s in sold_syms if strike(s) == 24700.0)] = 40.0
    ctx.market.prices[next(s for s in sold_syms if strike(s) == 25300.0)] = 120.0
    saved = ctx.market.by_expiry.pop(W3.isoformat())      # the roll target won't price
    assert tick(st, ctx, at(date(2026, 8, 10), 15, 0)) == []
    ctx.market.by_expiry[W3.isoformat()] = saved
    out = tick(st, ctx, at(W2, 9, 30))                    # expiry day, before roll_time
    assert [s.action.name for s in out] == ["EXIT_ALL", "EXIT_ALL",
                                            "ENTER_SHORT", "ENTER_SHORT"]


# ------------------------------------ manual margin anchor (owner call, 2026-08-25)

def test_margin_per_set_is_forwarded_through_every_explicit_subclass():
    """These subclasses declare explicit signatures ending in ``**_ignored``, so a knob the
    base class defines but the subclass does not FORWARD is accepted and silently dropped —
    the run then anchors to the broker margin while the form says otherwise. That happened
    while writing this feature; the ctor accepted margin_per_set and _manual_margin() stayed
    0. Pin the forwarding, not just the base class."""
    import inspect

    from skas_algo.strategies.delta_neutral_monthly import DeltaNeutralMonthlyStrategy
    from skas_algo.strategies.double_diagonal_calendar import DoubleDiagonalCalendarStrategy
    from skas_algo.strategies.fair_value_calendar import FairValueCalendarStrategy

    for cls in (DeltaNeutralMonthlyStrategy, FairValueCalendarStrategy,
                DoubleDiagonalCalendarStrategy):
        assert "margin_per_set" in inspect.signature(cls.__init__).parameters, cls.__name__
        s = cls(universe=["NIFTY"], underlying="NIFTY", margin_per_set=100_000)
        assert s.margin_per_set == 100_000, f"{cls.__name__} swallowed it into **_ignored"
        assert s._manual_margin() > 0, cls.__name__


def test_the_manual_anchor_scales_by_sets_not_lots():
    """The fair-value calendar sizes in lot-SETS; the base class in `lots`. A manual anchor
    is quoted per SET (what the Kite basket calculator prices), so it must scale by `sets`."""
    from skas_algo.strategies.fair_value_calendar import FairValueCalendarStrategy

    one = FairValueCalendarStrategy(universe=["NIFTY"], underlying="NIFTY", sets=1,
                                    margin_per_set=134_612)
    three = FairValueCalendarStrategy(universe=["NIFTY"], underlying="NIFTY", sets=3,
                                      margin_per_set=134_612)
    assert one._manual_margin() == 134_612
    assert three._manual_margin() == 403_836          # run 15's real size
    # …and that is what the 5% target would have been measured against: ₹20,192, not the
    # ~₹11,242 the broker-derived base actually exited on.
    assert round(three._manual_margin() * 0.05) == 20_192


def test_the_ctor_default_is_off_so_a_recovered_deploy_is_unchanged():
    """CLAUDE.md §1: a new knob must default to the OLD behaviour. 0 = derive from the
    broker push exactly as before."""
    from skas_algo.strategies.fair_value_calendar import FairValueCalendarStrategy

    s = FairValueCalendarStrategy(universe=["NIFTY"], underlying="NIFTY", sets=3)
    assert s.margin_per_set == 0.0
    assert s._manual_margin() == 0.0


def test_the_fair_value_ctor_survived_the_new_knob():
    """Regression for a real slip: inserting _size_multiple at method indent INSIDE
    __init__ severed the constructor, leaving every later assignment as dead code after a
    return. It imported fine and the method existed — both checks passed while the ctor was
    broken. Assert the fields that came after the insertion point."""
    from skas_algo.strategies.fair_value_calendar import FairValueCalendarStrategy

    s = FairValueCalendarStrategy(universe=["NIFTY"], underlying="NIFTY")
    assert (s.sell_premium_1, s.sell_premium_2, s.buy_premium) == (150.0, 450.0, 200.0)
    assert s.buy_lots_per_set == 3 and s.max_gap_points == 900.0 and s.min_sold_dte == 4


def test_the_manual_margin_is_hot_editable_on_a_running_deploy():
    """The owner asked for it to be changeable via the Live tile's Edit params, not only at
    deploy. The editable surface is every SCALAR ctor knob that is not an infra key, so this
    qualifies — pin it, because blocklisting it later would silently remove the ability
    without breaking anything visible."""
    import inspect

    from skas_algo.live.manager import LiveRun
    from skas_algo.strategies.fair_value_calendar import FairValueCalendarStrategy

    param = inspect.signature(FairValueCalendarStrategy.__init__).parameters["margin_per_set"]
    assert isinstance(param.default, float)          # scalar → the modal renders it
    assert "margin_per_set" not in LiveRun._PARAM_EDIT_BLOCKLIST


def test_a_manual_anchor_needs_no_broker_push_so_thresholds_are_live_at_once():
    """Deriving from the broker leaves margin_source "pending" until the manager's basket
    call lands, and thresholds do not apply while pending. A manual anchor removes that
    wait — the point of the knob is a rupee target you can state up front."""
    from skas_algo.strategies.fair_value_calendar import FairValueCalendarStrategy

    s = FairValueCalendarStrategy(universe=["NIFTY"], underlying="NIFTY", sets=2,
                                  margin_per_set=134_612)
    s._freeze_margin(None, 0.0)             # what a structural change calls
    assert s.margin_source != "pending"     # nothing to wait for

    derived = FairValueCalendarStrategy(universe=["NIFTY"], underlying="NIFTY", sets=2)
    derived._freeze_margin(None, 0.0)
    assert derived.margin_source == "pending"   # unchanged for the broker-derived default


def test_buy_lots_per_set_reaches_the_strategy_from_a_deploy():
    """It is in the deploy model and the route's params dict, but was never on the builder
    form — so every deploy silently traded the default 3 longs per set while a backtest like
    #273 used 4, and the two could not be compared. Pin the whole path."""
    import inspect

    from skas_algo.api.models import FairValueCalendarDeploy
    from skas_algo.strategies.fair_value_calendar import FairValueCalendarStrategy

    assert FairValueCalendarDeploy.model_fields["buy_lots_per_set"].default == 3
    assert "buy_lots_per_set" in inspect.signature(FairValueCalendarStrategy.__init__).parameters
    s = FairValueCalendarStrategy(universe=["NIFTY"], underlying="NIFTY", buy_lots_per_set=4)
    assert s.buy_lots_per_set == 4        # not swallowed by **_ignored
