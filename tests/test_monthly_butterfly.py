"""monthly_butterfly: the 2:1:1 structure, the "previous expiry + 1 day" entry, the
all-or-nothing wings, the hard expiry-day exit and the one-cycle-per-expiry latch —
fake market/chain, no network."""

from __future__ import annotations

from datetime import date, datetime

from skas_algo.engine.options.contract_specs import lot_size_for
from skas_algo.strategies.monthly_butterfly import MonthlyButterflyStrategy

JUL_MON = date(2026, 7, 28)   # the PREVIOUS month's monthly
AUG_W1 = date(2026, 8, 4)
AUG_W2 = date(2026, 8, 11)
AUG_MON = date(2026, 8, 25)   # the expiry this cycle trades
SEP_MON = date(2026, 9, 29)
EXPIRIES = [JUL_MON, AUG_W1, AUG_W2, AUG_MON, SEP_MON]
LOT = lot_size_for("NIFTY", AUG_MON)
SPOT = 25_000.0


def chain(spot: float = SPOT, drop: set[float] | None = None, oi: int = 5000):
    """A V-shaped premium curve around spot so the ATM body is dearer than either wing —
    the convexity that makes the butterfly a net debit. ``drop`` removes strikes entirely."""
    rows = []
    for i in range(-25, 26):
        k = float(25_000 + i * 100)
        if drop and k in drop:
            continue
        rows.append({
            "strike": k,
            "pe": {"ltp": round(200.0 - abs(k - spot) * 0.4 + 0.0005 * (k - spot) ** 2 / 100, 2),
                   "oi": oi},
            "ce": {"ltp": round(200.0 - abs(k - spot) * 0.4 + 0.0005 * (k - spot) ** 2 / 100, 2),
                   "oi": oi},
        })
    return {"spot": spot, "atm_strike": spot, "lot_size": LOT, "rows": rows}


class FakeChain:
    def __init__(self, expiries):
        self._e = expiries

    def expiries(self, _u, _today):
        return [e.isoformat() for e in self._e]


class FakeMarket:
    def __init__(self, by_expiry, spot=SPOT):
        self.by_expiry, self.spot = by_expiry, spot
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


def setup(aug=None, spot=SPOT, primed=True, **kw):
    """``primed`` = a run that has already seen the July expiry pass (what a deployed run
    holds after its first cycle), so 29 July is a legitimate entry day. A FRESH run
    (primed=False) must instead wait for the current expiry — see the wait test."""
    st = MonthlyButterflyStrategy(universe=["NIFTY"], **kw)
    if primed:
        st.done_expiry = JUL_MON.isoformat()
    c = aug if aug is not None else chain(spot=spot)
    mkt = FakeMarket({e.isoformat(): c for e in EXPIRIES}, spot=spot)
    return st, FakeCtx(mkt)


def tick(st, ctx, dt):
    ctx._now = dt
    sigs = st.on_slice(ctx)
    for s in sigs:
        if s.action.name in ("ENTER_SHORT", "ENTER_LONG"):
            ctx.positions[s.symbol] = s.quantity
        elif s.action.name == "EXIT_ALL":
            ctx.positions.pop(s.symbol, None)
    return sigs


def mark_all(st, ctx, mult: float = 1.0):
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = leg["entry"] * mult


def parts(st):
    """{strike: (dir, units)} of the live legs."""
    return {float(lg["symbol"].split("|")[2]): (lg["dir"], lg["units"]) for lg in st.legs}


def test_the_structure_is_two_short_at_the_money_against_one_long_each_side():
    st, ctx = setup(sets=2)
    sigs = tick(st, ctx, datetime(2026, 7, 29, 9, 20))
    assert len(sigs) == 3
    p = parts(st)
    assert p[25_000.0] == (-1, 2 * 2 * LOT)   # sets x body_lots x lot
    assert p[25_100.0] == (1, 2 * 1 * LOT)    # sets x wing_lots x lot
    assert p[24_900.0] == (1, 2 * 1 * LOT)
    # every leg is the SAME side and the SAME expiry — a butterfly, not a mixed book
    assert {lg["right"] for lg in st.legs} == {"PE"}
    assert {lg["symbol"].split("|")[1] for lg in st.legs} == {AUG_MON.isoformat()}
    # and it is a net DEBIT: the body's credit does not cover the two wings (convexity)
    debit = sum(-lg["dir"] * lg["entry"] * lg["units"] for lg in st.legs)
    assert debit > 0


def test_it_waits_for_the_previous_expiry_to_pass_before_opening_the_next():
    """The spec is "previous month expiry + 1 day". On the old monthly's expiry day the
    next butterfly must NOT open — that day still belongs to the expiring cycle."""
    st, ctx = setup()
    assert tick(st, ctx, datetime(2026, 7, 28, 9, 20)) == []   # ON the July expiry
    assert len(tick(st, ctx, datetime(2026, 7, 29, 9, 20))) == 3   # the session after


def test_a_wing_that_does_not_price_opens_nothing_at_all():
    """All or nothing: a butterfly missing a wing is a naked ratio spread, which is the one
    thing this structure exists to avoid. It retries instead."""
    st, ctx = setup(aug=chain(drop={25_100.0}))
    assert tick(st, ctx, datetime(2026, 7, 29, 9, 20)) == []
    assert st.legs == []
    # a strike with no open interest is refused the same way
    st2, ctx2 = setup(aug=chain(oi=0), min_leg_oi=10)
    assert tick(st2, ctx2, datetime(2026, 7, 29, 9, 20)) == []


def test_expiry_day_closes_the_book_at_the_exit_time_and_not_before():
    st, ctx = setup()
    tick(st, ctx, datetime(2026, 7, 29, 9, 20))
    mark_all(st, ctx)
    assert tick(st, ctx, datetime(2026, 8, 25, 15, 0)) == []      # before exit_time
    sigs = tick(st, ctx, datetime(2026, 8, 25, 15, 15))
    assert {s.action.name for s in sigs} == {"EXIT_ALL"}
    assert {s.reason for s in sigs} == {"mbf_expiry"}
    assert st.legs == []


def test_the_time_exit_stays_silent_when_the_legs_no_longer_print():
    """2023-06-29 is a monthly expiry the 1-min store simply does not hold. Ordering into a
    session that isn't there is noise — the replay settles those legs at intrinsic instead,
    so the strategy must not emit an unfillable exit."""
    st, ctx = setup()
    tick(st, ctx, datetime(2026, 7, 29, 9, 20))
    mark_all(st, ctx)
    ctx.market.prices.pop(st.legs[0]["symbol"])      # that contract stopped printing
    assert tick(st, ctx, datetime(2026, 8, 26, 15, 15)) == []
    assert len(st.legs) == 3                          # the book is left intact for settlement


def test_a_target_exit_stands_the_month_down_until_the_next_expiry():
    """Booked early on target → flat for the REST of that expiry, then the next cycle opens
    the session after it passes. The whole point of the one-cycle-per-expiry latch."""
    st, ctx = setup(sets=1, margin_per_set=70_000, profit_target_pct=1)
    tick(st, ctx, datetime(2026, 7, 29, 9, 20))
    mark_all(st, ctx)                                 # flat marks → no exit, but the anchor lands
    assert tick(st, ctx, datetime(2026, 8, 5, 11, 0)) == []
    # the manual anchor is applied on a MANAGE slice, not at entry (the base class rule), and
    # it is cleared again by the exit — so it can only be read here, mid-cycle
    assert st.margin_base == 70_000 and st.margin_source == "manual"
    mark_all(st, ctx, mult=0.5)                       # everything halves → the fly is up
    sigs = tick(st, ctx, datetime(2026, 8, 10, 11, 0))
    assert {s.reason for s in sigs} == {"target"} and st.done_expiry == AUG_MON.isoformat()
    # …and it stays flat for the rest of August's cycle, target or no target
    assert tick(st, ctx, datetime(2026, 8, 11, 9, 20)) == []
    assert tick(st, ctx, datetime(2026, 8, 25, 9, 20)) == []
    # the next monthly opens the day AFTER the August expiry, never on it
    assert len(tick(st, ctx, datetime(2026, 8, 26, 9, 20))) == 3
    assert st.cycle_expiry == SEP_MON.isoformat()


def test_the_manual_margin_anchor_scales_by_lot_sets():
    """`sets` is the lot-SET count, so a ₹70,000 anchor is per set — the %-target must be
    measured against the whole book, not one butterfly (the family's _size_multiple rule)."""
    one = MonthlyButterflyStrategy(universe=["NIFTY"], sets=1, margin_per_set=70_000)
    ten = MonthlyButterflyStrategy(universe=["NIFTY"], sets=10, margin_per_set=70_000)
    assert one._manual_margin() == 70_000
    assert ten._manual_margin() == 700_000
    # …and the owner's 3% target on 10 sets is ₹21,000, not ₹2,100
    ten.margin_base, ten.margin_source = ten._manual_margin(), "manual"
    ten.target_pct = 3.0
    assert round(ten.exit_amounts()[0]) == 21_000


def test_the_ctor_defaults_are_the_spec_and_no_stop():
    st = MonthlyButterflyStrategy(universe=["NIFTY"])
    assert (st.body_lots, st.wing_lots, st.wing_points) == (2, 1, 100.0)
    assert st.side == "pe" and st.stop_pct == 0.0   # the debit is the floor; no stop to tune
    assert st.phase == "idle"


def test_the_call_side_builds_the_same_shape_on_calls():
    st, ctx = setup(side="ce")
    tick(st, ctx, datetime(2026, 7, 29, 9, 20))
    assert {lg["right"] for lg in st.legs} == {"CE"}
    assert parts(st)[25_000.0][0] == -1 and parts(st)[25_100.0][0] == 1


def test_the_forms_lots_reach_the_strategy_as_lot_sets():
    """The harness footgun that cost run 287 (2026-09-03): the form's Sizing LOTS is the
    lot-SET count, but the ctor knob is `sets` and a stray `lots` kwarg falls into
    **_ignored. Without the translation the run trades ONE set at a tenth of the size and
    the result reads as "the strategy doesn't work" rather than as a sizing bug."""
    from skas_algo.services.intraday_replay import _SIZES_BY_SETS

    assert "monthly_butterfly" in _SIZES_BY_SETS
    # and the ctor really does ignore `lots`, which is WHY the translation must exist
    st = MonthlyButterflyStrategy(universe=["NIFTY"], lots=10)
    assert st.sets == 1
    assert MonthlyButterflyStrategy(universe=["NIFTY"], sets=10).sets == 10


def test_the_wings_are_sent_before_the_body():
    """Live, a real order that fails abandons the rest of the decision's actions. Body
    first would leave a naked short on a wing rejection — the exposure the butterfly
    exists to rule out. Wings first leaves two long options at worst."""
    st, ctx = setup(sets=20)
    sigs = tick(st, ctx, datetime(2026, 7, 29, 9, 20))
    assert [s.action.name for s in sigs] == ["ENTER_LONG", "ENTER_LONG", "ENTER_SHORT"]
    assert sigs[-1].quantity == 20 * 2 * LOT   # the body is last, and it is the big one


def test_the_body_is_covered_before_the_wings_are_sold():
    """Mirror of the entry order: on the way out the short body goes first, so a failure
    later in the list leaves a long wing, never a naked short."""
    st, ctx = setup()
    tick(st, ctx, datetime(2026, 7, 29, 9, 20))
    mark_all(st, ctx)
    sigs = tick(st, ctx, datetime(2026, 8, 25, 15, 15))
    assert [s.action.name for s in sigs] == ["EXIT_ALL"] * 3
    assert float(sigs[0].symbol.split("|")[2]) == 25_000.0   # the ATM body first


def test_banknifty_lot_size_is_thirty_in_the_current_series():
    """The contract table carried an UNSOURCED 35 for BANKNIFTY from 2026-01-01 (added
    2026-07-17). The live Kite instruments dump on 2026-09-04 reads 30 for every listed
    series, Sep-2026 to Jun-2027, and the owner confirms 30 from the January-2026 series.
    A wrong lot here makes every live order a non-multiple → rejected on arrival."""
    from datetime import date as _d

    from skas_algo.engine.options.contract_specs import lot_size_for

    assert lot_size_for("BANKNIFTY", _d(2026, 9, 29)) == 30
    assert lot_size_for("BANKNIFTY", _d(2026, 3, 31)) == 30
    assert lot_size_for("BANKNIFTY", _d(2025, 6, 26)) == 30   # 2024-11-20 revision onward


def test_a_fresh_run_waits_for_the_current_expiry_to_pass_unless_forced():
    """A run started mid-cycle has not seen an expiry yet. Un-forced, it must stand down
    until the current one passes and open the session after (the spec's entry day) —
    before this it opened a three-week butterfly at 09:30 the next morning, which a paper
    test could not tell apart from the real cadence. Force is the deliberate way in."""
    st, ctx = setup(primed=False)
    assert tick(st, ctx, datetime(2026, 8, 12, 11, 0)) == []        # mid-cycle: wait
    assert st.done_expiry == AUG_MON.isoformat()                      # …for THIS expiry
    assert tick(st, ctx, datetime(2026, 8, 25, 9, 30)) == []          # the expiry day itself
    assert len(tick(st, ctx, datetime(2026, 8, 26, 9, 30))) == 3      # the session after
    assert st.cycle_expiry == SEP_MON.isoformat()
    # the deploy toggle opens on the current expiry at once
    f, fctx = setup(primed=False, force_entry=True)
    assert len(tick(f, fctx, datetime(2026, 8, 12, 11, 0))) == 3 and f.cycle_expiry == AUG_MON.isoformat()
    # …and so does the tile button, on the next tick
    b, bctx = setup(primed=False)
    assert tick(b, bctx, datetime(2026, 8, 12, 11, 0)) == []
    assert "ATM" in b.request_force_entry()
    assert len(tick(b, bctx, datetime(2026, 8, 12, 11, 1))) == 3
