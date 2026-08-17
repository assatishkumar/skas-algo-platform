"""asymmetric_premium_intra: the two-expiry structure, the relative-premium adjustment,
the combined points stop, and the hard time exit — fake market/chain, no network."""

from __future__ import annotations

from datetime import date, datetime

from skas_algo.strategies.asymmetric_premium_intra import AsymmetricPremiumIntraStrategy

THIS_WK = date(2026, 8, 18)     # current-week expiry
NEXT_WK = date(2026, 8, 25)     # next-week expiry
DAY = datetime(2026, 8, 17, 9, 30)


def at(h, m=0):
    return datetime(2026, 8, 17, h, m)


def chain(spot=25000.0, lot=75, ce=120.0, pe=120.0, step=100, span=20, slope=0.0):
    """Flat-ish premiums so a test can set exact ratios. ``slope`` tilts premium by strike
    so the adjustment has somewhere meaningful to roll to."""
    rows = []
    atm = round(spot / step) * step
    for i in range(-span, span + 1):
        k = float(atm + i * step)
        rows.append({"strike": k,
                     "ce": {"ltp": max(0.05, ce - slope * i), "oi": 5000},
                     "pe": {"ltp": max(0.05, pe + slope * i), "oi": 5000}})
    return {"spot": spot, "atm_strike": float(atm), "lot_size": lot, "rows": rows}


class FakeCacheChain:
    def __init__(self, expiries):
        self._e = expiries

    def expiries(self, _u, today):
        return [e for e in self._e if e >= today]


class FakeMarket:
    def __init__(self, by_expiry):
        self.by_expiry = by_expiry      # iso -> chain dict
        self.prices: dict[str, float] = {}
        self.current_date = None

    def live_chain(self, _u, expiry_iso):
        return self.by_expiry.get(str(expiry_iso)[:10])

    def index_spot(self, _u):
        return next(iter(self.by_expiry.values()))["spot"]

    def has_print(self, s):
        return s in self.prices


class FakeCtx:
    def __init__(self, market, cache_chain):
        self.market, self.cache_chain = market, cache_chain
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


def setup(this_wk=None, next_wk=None, expiries=None, **kw):
    st = AsymmetricPremiumIntraStrategy(universe=["NIFTY"], **kw)
    mkt = FakeMarket({THIS_WK.isoformat(): this_wk or chain(),
                      NEXT_WK.isoformat(): next_wk or chain()})
    return st, FakeCtx(mkt, FakeCacheChain(expiries or [THIS_WK, NEXT_WK]))


def tick(st, ctx, dt):
    ctx._now = dt
    sigs = st.on_slice(ctx)
    for s in sigs:
        if s.action.name == "ENTER_SHORT":
            ctx.positions[s.symbol] = s.quantity
        elif s.action.name == "EXIT_ALL":
            ctx.positions.pop(s.symbol, None)
    return sigs


def mark(ctx, sym, px):
    ctx.market.prices[sym] = px


def reprice(ctx, expiry, **kw):
    """Swap in a re-priced chain. A real chain moves with the market, so a test that marks
    a leg down MUST also move the chain — otherwise "the strike trading at 120" is still
    the strike we already hold and no roll is possible."""
    ctx.market.by_expiry[expiry.isoformat()] = chain(**kw)


def open_both(st, ctx, dt=DAY, ce=120.0, pe=120.0):
    sigs = tick(st, ctx, dt)
    mark(ctx, st.legs["CE"]["symbol"], ce)
    mark(ctx, st.legs["PE"]["symbol"], pe)
    return sigs


# ------------------------------------------------------------------ structure
def test_call_is_current_week_and_put_is_next_week():
    """The whole point of the sheet: the two shorts sit on DIFFERENT expiries."""
    st, ctx = setup()
    sigs = tick(st, ctx, DAY)
    assert len(sigs) == 2 and all(s.action.name == "ENTER_SHORT" for s in sigs)
    by_right = {s.symbol.split("|")[3]: s.symbol.split("|")[1] for s in sigs}
    assert by_right == {"CE": THIS_WK.isoformat(), "PE": NEXT_WK.isoformat()}
    assert {s.symbol.split("|")[2] for s in sigs} == {"25000"}   # both near-ATM
    assert all(s.quantity == 75 for s in sigs)


def test_no_entry_before_0930_and_hard_exit_at_1515():
    st, ctx = setup()
    assert tick(st, ctx, at(9, 29)) == []
    open_both(st, ctx)
    out = tick(st, ctx, at(15, 15))
    assert len(out) == 2 and all(s.reason == "apx_eod" for s in out)
    assert tick(st, ctx, at(15, 20)) == []          # stays flat


def test_entry_is_all_or_nothing():
    """One leg alone is a naked directional short — not what the sheet describes."""
    broken = chain()
    for r in broken["rows"]:
        r["pe"] = None                               # next-week puts unpriceable
    st, ctx = setup(next_wk=broken)
    assert tick(st, ctx, DAY) == []
    assert st.legs == {}


def test_needs_two_listed_expiries():
    st, ctx = setup(expiries=[THIS_WK])
    assert tick(st, ctx, DAY) == []


# ------------------------------------------------------------------ the stop
def test_hundred_point_combined_stop_is_points_times_lot_not_a_fixed_rupee():
    """The sheet says "lot size x 100 = Rs6,500", which is the OLD 65 lot. Points x the
    CURRENT lot is the reading that stays right as the lot size changes."""
    st, ctx = setup()
    open_both(st, ctx)
    ce, pe = st.legs["CE"]["symbol"], st.legs["PE"]["symbol"]

    mark(ctx, ce, 160.0); mark(ctx, pe, 155.0)       # +75 pts of combined loss
    assert tick(st, ctx, at(11)) == []

    mark(ctx, pe, 181.0)                             # 40 + 61 = 101 pts → stop
    out = tick(st, ctx, at(11, 1))
    assert len(out) == 2 and all(s.reason == "apx_stop" for s in out)
    assert st.realized == (120 - 160) * 75 + (120 - 181) * 75    # -Rs7,575 on a 75 lot
    assert tick(st, ctx, at(12)) == []               # done for the day by default


def test_the_stop_can_be_switched_off():
    st, ctx = setup(stop_loss_points=0)
    open_both(st, ctx)
    mark(ctx, st.legs["CE"]["symbol"], 400.0)
    assert tick(st, ctx, at(11)) == []               # no stop, and no adjustment either
    assert len(st.legs) == 2


def test_reentry_after_a_stop_is_off_by_default_and_capped_when_on():
    st, ctx = setup(max_reentries=1)
    open_both(st, ctx)
    mark(ctx, st.legs["CE"]["symbol"], 300.0)        # blow through the stop
    out = tick(st, ctx, at(11))
    assert [s.action.name for s in out] == ["EXIT_ALL", "EXIT_ALL",
                                            "ENTER_SHORT", "ENTER_SHORT"]
    assert st.reentries == 1 and len(st.legs) == 2


# ------------------------------------------------------------ the adjustment
def test_cheap_leg_rolls_to_match_the_rich_leg_on_its_own_expiry():
    """Sheet §4: when one premium decays to ~50% of the other, move the CHEAP leg to the
    strike whose premium ~matches the RICH one — and keep the other leg untouched."""
    st, ctx = setup(this_wk=chain(slope=10.0), next_wk=chain(slope=10.0),
                    stop_loss_points=0)
    open_both(st, ctx)
    ce_leg, pe_before = st.legs["CE"], dict(st.legs["PE"])

    mark(ctx, ce_leg["symbol"], 50.0)                # CE decayed to 42% of the PE
    mark(ctx, pe_before["symbol"], 120.0)
    # ...and the chain moved with it: ATM calls now trade at 50, so the strike trading at
    # the rich leg's 120 is seven 100-pt steps lower.
    reprice(ctx, THIS_WK, ce=50.0, slope=10.0)
    out = tick(st, ctx, at(11))

    assert [s.action.name for s in out] == ["EXIT_ALL", "ENTER_SHORT"]
    assert out[0].symbol == ce_leg["symbol"]         # exit precedes the re-open
    assert out[1].symbol.split("|")[1] == THIS_WK.isoformat()   # stayed on ITS expiry
    assert out[1].symbol.split("|")[3] == "CE"
    assert out[1].symbol.split("|")[2] == "24300"    # 50 + 10x7 = 120, seven steps down
    assert st.legs["PE"] == pe_before                # the other leg is untouched
    assert st.legs["CE"]["entry"] == 120.0           # rolled to match the rich premium
    assert st.adjusts == 1
    assert st.realized == (120 - 50) * 75            # the decayed leg was banked


def test_no_adjustment_until_the_ratio_is_breached():
    st, ctx = setup(this_wk=chain(slope=10.0), next_wk=chain(slope=10.0), stop_loss_points=0)
    open_both(st, ctx)
    mark(ctx, st.legs["CE"]["symbol"], 61.0)         # 50.8% — just above the trigger
    mark(ctx, st.legs["PE"]["symbol"], 120.0)
    assert tick(st, ctx, at(11)) == []
    assert st.adjusts == 0


def test_adjustments_are_capped():
    st, ctx = setup(this_wk=chain(slope=10.0), next_wk=chain(slope=10.0),
                    stop_loss_points=0, max_adjusts=1)
    open_both(st, ctx)
    mark(ctx, st.legs["CE"]["symbol"], 50.0); mark(ctx, st.legs["PE"]["symbol"], 120.0)
    reprice(ctx, THIS_WK, ce=50.0, slope=10.0)
    assert len(tick(st, ctx, at(11))) == 2
    mark(ctx, st.legs["CE"]["symbol"], 50.0)
    reprice(ctx, THIS_WK, ce=20.0, slope=10.0)
    assert tick(st, ctx, at(11, 5)) == []            # cap reached
    assert st.adjusts == 1


def test_a_flat_chain_offers_nowhere_to_roll_so_nothing_happens():
    """Every strike matches the target equally, so the nearest one wins — which is the one
    we already hold. Rolling to ourselves would be pure cost."""
    st, ctx = setup(stop_loss_points=0)
    open_both(st, ctx)
    mark(ctx, st.legs["CE"]["symbol"], 50.0); mark(ctx, st.legs["PE"]["symbol"], 120.0)
    assert tick(st, ctx, at(11)) == []
    assert st.adjusts == 0


def test_a_stale_mark_defers_every_decision():
    st, ctx = setup()
    tick(st, ctx, DAY)                               # entered, no prints fed
    assert tick(st, ctx, at(11)) == []
    assert len(st.legs) == 2


def test_new_day_resets_and_state_round_trips():
    st, ctx = setup(stop_loss_points=0, this_wk=chain(slope=10.0), next_wk=chain(slope=10.0))
    open_both(st, ctx)
    mark(ctx, st.legs["CE"]["symbol"], 50.0); mark(ctx, st.legs["PE"]["symbol"], 120.0)
    reprice(ctx, THIS_WK, ce=50.0, slope=10.0)
    tick(st, ctx, at(11))
    assert st.adjusts == 1

    fresh = AsymmetricPremiumIntraStrategy(universe=["NIFTY"])
    fresh.load_state(st.export_state())
    assert fresh.export_state() == st.export_state()
    assert fresh.adjusts == 1 and fresh.realized == st.realized


def test_put_expiry_offset_zero_puts_both_legs_on_the_same_week():
    """The sheet's §7 hypothesis, made testable: offset 0 collapses the structure to a plain
    same-expiry straddle, so the asymmetry can be measured against its own control rather
    than argued about."""
    st, ctx = setup(put_expiry_offset=0)
    sigs = tick(st, ctx, DAY)
    assert {s.symbol.split("|")[1] for s in sigs} == {THIS_WK.isoformat()}
    assert {s.symbol.split("|")[3] for s in sigs} == {"CE", "PE"}


def test_offset_zero_needs_only_one_listed_expiry():
    st, ctx = setup(put_expiry_offset=0, expiries=[THIS_WK])
    assert len(tick(st, ctx, DAY)) == 2
