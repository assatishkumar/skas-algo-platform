"""intraday_straddle: entry window + once-a-day latch, ATM straddle, hard time exit,
fixed %-of-margin stop, trailing stop (ratchet + below_peak), delta-ITM strikes, force
entry, state round-trip — fake market/chain, no network."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from skas_algo.engine.options import black_scholes as bs
from skas_algo.strategies.intraday_straddle import IntradayStraddleStrategy

WEEKLY = date(2026, 7, 21)                 # a NIFTY weekly
ENTRY_DT = datetime(2026, 7, 13, 9, 18)    # inside the default entry window
# t the strategy computes at ENTRY_DT (expiry 15:30 cutoff) — price the chain with the SAME t
# so implied_vol round-trips and the delta picker resolves cleanly.
_T = (datetime(2026, 7, 21, 15, 30) - ENTRY_DT).total_seconds() / (365.0 * 24 * 3600)


def bs_chain(spot=24000.0, lot=65, sigma=0.15, r=0.065):
    rows = []
    for k in range(int(spot - 2500), int(spot + 2550), 50):
        ce = max(bs.price(spot, float(k), _T, r, sigma, "CE"), 0.05)
        pe = max(bs.price(spot, float(k), _T, r, sigma, "PE"), 0.05)
        rows.append({"strike": float(k),
                     "ce": {"ltp": round(ce, 2), "oi": 5000},
                     "pe": {"ltp": round(pe, 2), "oi": 5000}})
    return {"spot": spot, "atm_strike": float(round(spot / 50) * 50), "lot_size": lot, "rows": rows}


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


def setup(**kw):
    st = IntradayStraddleStrategy(underlying=kw.pop("underlying", "NIFTY"), lots=kw.pop("lots", 1), **kw)
    ctx = FakeCtx(FakeMarket(bs_chain()), FakeCacheChain([WEEKLY]))
    return st, ctx


def tick(st, ctx, dt):
    ctx._now = dt
    return st.on_slice(ctx)


def _fill(st, ctx, base):
    """Mark the legs open at the engine + push the broker margin (so stops arm)."""
    for leg in st.legs:
        ctx.positions[leg["symbol"]] = leg["units"]
    st.set_broker_margin(base)


def _set_pnl_pct(st, ctx, base, pct):
    """Set every leg's mark so the aggregate MTM = pct% of base (both legs short → a decay
    is profit)."""
    tot = sum(leg["units"] for leg in st.legs)
    per_unit = (base * pct / 100.0) / tot
    for leg in st.legs:              # dir=-1: (cur-entry)*u*-1 = (entry-cur)*u
        ctx.market.prices[leg["symbol"]] = leg["entry"] - per_unit


def test_enters_atm_straddle_once_per_day_in_window():
    st, ctx = setup()
    assert tick(st, ctx, datetime(2026, 7, 13, 9, 10)) == []      # before the window
    sigs = tick(st, ctx, ENTRY_DT)                                # in window → ATM straddle
    assert len(sigs) == 2 and all(s.action.name == "ENTER_SHORT" for s in sigs)
    assert all(s.quantity == 65 for s in sigs)                   # 1 lot × 65
    assert {s.symbol.split("|")[2] for s in sigs} == {"24000"}   # both legs at the ATM strike
    assert {s.symbol.split("|")[3] for s in sigs} == {"CE", "PE"}
    assert st.margin_source == "pending"
    # already entered today → a later tick doesn't re-enter
    for s in sigs:
        ctx.positions[s.symbol] = s.quantity
    ctx.positions.clear()  # simulate a mid-day flat (SL hit)
    assert tick(st, ctx, datetime(2026, 7, 13, 10, 0)) == []


def test_fixed_stop_and_hard_time_exit():
    st, ctx = setup()
    tick(st, ctx, ENTRY_DT)
    _fill(st, ctx, 100_000)
    _set_pnl_pct(st, ctx, 100_000, 0.0)
    assert tick(st, ctx, datetime(2026, 7, 13, 10, 0)) == []       # flat → no exit
    _set_pnl_pct(st, ctx, 100_000, -2.0)                           # −2% of margin
    sigs = tick(st, ctx, datetime(2026, 7, 13, 10, 5))
    assert sigs and all(s.reason == "stop" for s in sigs) and len(sigs) == 2

    # Hard time exit fires regardless of marks.
    st2, ctx2 = setup()
    tick(st2, ctx2, ENTRY_DT)
    _fill(st2, ctx2, 100_000)
    for leg in st2.legs:
        ctx2.market.prices[leg["symbol"]] = leg["entry"]
    sigs = tick(st2, ctx2, datetime(2026, 7, 13, 15, 25))
    assert sigs and all(s.reason == "eod" for s in sigs)


def test_trailing_ratchet():
    st, ctx = setup()  # defaults: trail_trigger 1%, step 0.5%, ratchet
    tick(st, ctx, ENTRY_DT)
    _fill(st, ctx, 100_000)
    _set_pnl_pct(st, ctx, 100_000, 4.0)          # peak +4% → stop ratchets to breakeven (0%)
    assert tick(st, ctx, datetime(2026, 7, 13, 10, 0)) == []
    assert abs(st.peak_pct - 4.0) < 1e-3
    _set_pnl_pct(st, ctx, 100_000, -0.5)         # give back below the 0% trailed stop
    sigs = tick(st, ctx, datetime(2026, 7, 13, 10, 5))
    assert sigs and all(s.reason == "trail" for s in sigs)  # "trail" — the stop had moved above −2%


def test_trailing_below_peak():
    st, ctx = setup(trail_mode="below_peak")
    tick(st, ctx, ENTRY_DT)
    _fill(st, ctx, 100_000)
    _set_pnl_pct(st, ctx, 100_000, 3.0)          # peak 3% → stop = 3 − 0.5 = 2.5%
    assert tick(st, ctx, datetime(2026, 7, 13, 10, 0)) == []
    _set_pnl_pct(st, ctx, 100_000, 2.4)          # dip below 2.5% → exit, locking profit
    sigs = tick(st, ctx, datetime(2026, 7, 13, 10, 5))
    assert sigs and all(s.reason == "trail" for s in sigs)


def test_delta_strike_sells_itm():
    st, ctx = setup(strike_delta=0.6)
    sigs = tick(st, ctx, ENTRY_DT)
    assert len(sigs) == 2
    ce = next(s for s in sigs if s.symbol.endswith("CE"))
    pe = next(s for s in sigs if s.symbol.endswith("PE"))
    assert float(ce.symbol.split("|")[2]) < 24000   # ~0.6Δ CE is ITM (below spot)
    assert float(pe.symbol.split("|")[2]) > 24000   # ~0.6Δ PE is ITM (above spot)


def test_force_entry_bypasses_window():
    st, ctx = setup()
    assert tick(st, ctx, datetime(2026, 7, 13, 8, 0)) == []  # pre-market, outside the window
    st.request_force_entry()
    sigs = tick(st, ctx, datetime(2026, 7, 13, 8, 0))
    assert len(sigs) == 2 and not st.force_pending


def test_state_round_trip_incl_peak():
    st, ctx = setup()
    sigs = tick(st, ctx, ENTRY_DT)
    for s in sigs:
        ctx.positions[s.symbol] = s.quantity
    st.peak_pct = 3.5
    st.margin_base, st.margin_source = 100_000.0, "broker"
    st2 = IntradayStraddleStrategy(underlying="NIFTY", lots=1)
    st2.load_state(st.export_state())
    assert st2.legs == st.legs and st2.entered_day == "2026-07-13"
    assert st2.peak_pct == 3.5 and st2.margin_base == 100_000.0


def test_leg_book_banks_the_melted_leg_and_keeps_the_other_running():
    """leg_book_pct=80: a short leg at ≤20% of its entry premium is bought back ALONE
    (reason leg_book); the banked ₹ stays in the day's P&L the stop/trail compare; the
    surviving leg still honors the 15:25 hard exit."""
    st, ctx = setup(leg_book_pct=80.0)
    tick(st, ctx, ENTRY_DT)
    base = 200_000.0
    _fill(st, ctx, base)
    ce, pe = st.legs[0], st.legs[1]

    # CE melts to 25% of entry (below the 80%-captured line is 20%) — not yet.
    ctx.market.prices[ce["symbol"]] = ce["entry"] * 0.25
    ctx.market.prices[pe["symbol"]] = pe["entry"]
    assert tick(st, ctx, datetime(2026, 7, 13, 11, 0)) == []
    # CE melts to 15% — booked alone.
    ctx.market.prices[ce["symbol"]] = ce["entry"] * 0.15
    sigs = tick(st, ctx, datetime(2026, 7, 13, 11, 5))
    assert len(sigs) == 1 and sigs[0].reason == "leg_book" and sigs[0].symbol == ce["symbol"]
    assert st.legs == [pe]
    banked = (ce["entry"] - ce["entry"] * 0.15) * ce["units"]
    assert abs(st.leg_realized - banked) < 1e-6
    ctx.positions.pop(ce["symbol"], None)

    # Day P&L = banked + open-leg MTM: push PE 30% ABOVE entry — open MTM is a loss but
    # the banked CE keeps the total above the −2% stop, so no stop fires…
    ctx.market.prices[pe["symbol"]] = pe["entry"] * 1.3
    open_loss = (pe["entry"] * 1.3 - pe["entry"]) * pe["units"]
    assert banked - open_loss > -base * 0.02  # sanity: total is above the stop line
    assert tick(st, ctx, datetime(2026, 7, 13, 11, 10)) == []
    # …and the strategy_pnl hook reports banked + MTM.
    got = st.strategy_pnl({pe["symbol"]: pe["entry"] * 1.3})
    assert abs(got - (banked - open_loss)) < 1e-6

    # The surviving leg still hard-exits at 15:25.
    sigs = tick(st, ctx, datetime(2026, 7, 13, 15, 25))
    assert len(sigs) == 1 and sigs[0].reason == "eod" and sigs[0].symbol == pe["symbol"]

    # leg_realized survives a state round-trip.
    st2 = IntradayStraddleStrategy(underlying="NIFTY", leg_book_pct=80.0)
    st2.load_state(st.export_state())
    assert abs(st2.leg_realized - banked) < 1e-6


def test_leg_book_off_by_default_no_booking():
    st, ctx = setup()  # default leg_book_pct=0
    tick(st, ctx, ENTRY_DT)
    _fill(st, ctx, 200_000.0)
    ce, pe = st.legs[0], st.legs[1]
    ctx.market.prices[ce["symbol"]] = ce["entry"] * 0.05  # 95% captured — still no booking
    ctx.market.prices[pe["symbol"]] = pe["entry"]
    assert tick(st, ctx, datetime(2026, 7, 13, 11, 0)) == []
    assert len(st.legs) == 2 and st.leg_realized == 0.0


def test_stop_compares_banked_plus_open_not_the_open_leg_alone():
    """The SL must fire on the DAY's net — banked (realized) leg bookings plus the
    surviving leg's unrealized — not on the open leg in isolation. Pinned in both
    directions: a loss that alone would breach the stop must NOT stop the day while
    banked profit covers it, and must stop it once the net genuinely breaches."""
    base = 200_000.0
    st, ctx = setup(leg_book_pct=90.0, stop_loss_pct=2.0,
                    trail_trigger_pct=0, trail_step_pct=0)  # trail off — isolate the fixed stop
    tick(st, ctx, ENTRY_DT)
    _fill(st, ctx, base)
    ce, pe = st.legs[0], st.legs[1]
    units = ce["units"]

    # Book the CE at 5% of its entry -> banks 95% of that leg's premium.
    ctx.market.prices[ce["symbol"]] = ce["entry"] * 0.05
    ctx.market.prices[pe["symbol"]] = pe["entry"]
    sigs = tick(st, ctx, datetime(2026, 7, 13, 11, 0))
    assert len(sigs) == 1 and sigs[0].reason == "leg_book"
    ctx.positions.pop(ce["symbol"], None)
    banked = (ce["entry"] - ce["entry"] * 0.05) * units
    assert abs(st.leg_realized - banked) < 1e-6
    assert banked > base * 0.02, "test needs banked > the stop, else it proves nothing"

    stop_rs = base * 0.02  # 4,000

    # PE loses MORE than the whole stop on its own, but less than banked+stop -> net is
    # still above the line, so the day must survive. If the stop looked at the open leg
    # alone this would (wrongly) fire.
    open_loss = banked + stop_rs - 1_000          # net = -(stop-1000) = above the stop
    ctx.market.prices[pe["symbol"]] = pe["entry"] + open_loss / units
    assert open_loss > stop_rs                     # the open leg alone is past the stop
    assert tick(st, ctx, datetime(2026, 7, 13, 12, 0)) == []
    assert abs(st.strategy_pnl({pe["symbol"]: ctx.market.prices[pe["symbol"]]})
               - (banked - open_loss)) < 1e-6

    # Push until the NET breaches -> now it must stop.
    open_loss = banked + stop_rs + 1_000
    ctx.market.prices[pe["symbol"]] = pe["entry"] + open_loss / units
    sigs = tick(st, ctx, datetime(2026, 7, 13, 12, 5))
    assert len(sigs) == 1 and sigs[0].reason == "stop" and sigs[0].symbol == pe["symbol"]


def test_trail_high_water_also_counts_banked_profit():
    """The ratchet's peak is measured on the same net, so banking a leg can itself lift
    the stop — otherwise booking would silently disarm the trail for the rest of the day."""
    base = 100_000.0
    st, ctx = setup(leg_book_pct=90.0, stop_loss_pct=2.0,
                    trail_trigger_pct=1.0, trail_step_pct=0.5, trail_mode="ratchet")
    tick(st, ctx, ENTRY_DT)
    _fill(st, ctx, base)
    ce, pe = st.legs[0], st.legs[1]
    assert st._stop_level() == -2.0                      # nothing banked yet

    ctx.market.prices[ce["symbol"]] = ce["entry"] * 0.05
    ctx.market.prices[pe["symbol"]] = pe["entry"]
    tick(st, ctx, datetime(2026, 7, 13, 11, 0))          # books the CE
    ctx.positions.pop(ce["symbol"], None)
    tick(st, ctx, datetime(2026, 7, 13, 11, 1))          # next tick re-measures the peak

    banked_pct = 100.0 * st.leg_realized / base
    assert st.peak_pct == pytest.approx(banked_pct, abs=1e-6)
    assert st._stop_level() > -2.0, "banked profit should have ratcheted the stop up"


def test_stop_loss_pct_zero_means_off_not_breakeven():
    """stop_loss_pct=0 must DISABLE the fixed stop (platform convention), not read as
    −0.0 — a breakeven stop that cuts every day on its first adverse tick. Backtest #249
    was configured "no SL" and stopped 467 of 497 days at a 6% win rate (2026-08-07)."""
    base = 200_000.0
    st, ctx = setup(stop_loss_pct=0, trail_trigger_pct=0, trail_step_pct=0)
    tick(st, ctx, ENTRY_DT)
    _fill(st, ctx, base)
    assert st._stop_level() is None
    # A loss far past what any sane stop would allow must NOT exit before 15:25.
    _set_pnl_pct(st, ctx, base, -25.0)
    assert tick(st, ctx, datetime(2026, 7, 13, 11, 0)) == []
    _set_pnl_pct(st, ctx, base, -60.0)
    assert tick(st, ctx, datetime(2026, 7, 13, 14, 0)) == []
    # The hard time exit still applies — "no stop" never means "no exit".
    sigs = tick(st, ctx, datetime(2026, 7, 13, 15, 25))
    assert len(sigs) == 2 and all(s.reason == "eod" for s in sigs)
    # …and nothing claims a stop that doesn't exist.
    assert st.exit_amounts() == (None, None)


def test_stop_off_still_lets_the_trail_protect_profit():
    """With the fixed stop off but trailing on, the ratchet arms from breakeven: it can
    protect banked profit but must never invent a stop while the day is still negative."""
    base = 100_000.0
    st, ctx = setup(stop_loss_pct=0, trail_trigger_pct=1.0, trail_step_pct=0.5,
                    trail_mode="ratchet")
    tick(st, ctx, ENTRY_DT)
    _fill(st, ctx, base)
    _set_pnl_pct(st, ctx, base, -5.0)          # deep in the red, no peak yet
    assert st._stop_level() is None
    assert tick(st, ctx, datetime(2026, 7, 13, 10, 0)) == []
    _set_pnl_pct(st, ctx, base, 2.4)           # peak 2.4% -> 2 ratchet steps
    tick(st, ctx, datetime(2026, 7, 13, 11, 0))
    assert st.peak_pct == pytest.approx(2.4)
    assert st._stop_level() == pytest.approx(1.0)   # 0 + 0.5 x 2 — locks in profit
    _set_pnl_pct(st, ctx, base, 0.5)           # gives back below the locked level
    sigs = tick(st, ctx, datetime(2026, 7, 13, 11, 5))
    assert len(sigs) == 2 and all(s.reason == "trail" for s in sigs)


def test_fixed_stop_behaviour_unchanged_when_configured():
    """Byte-identity guard for the 0=off change: with stop_loss_pct > 0 every level the
    old code produced must be reproduced exactly (§1 — recovered deploys must not move)."""
    st = IntradayStraddleStrategy(underlying="NIFTY", stop_loss_pct=2.0,
                                  trail_trigger_pct=1.0, trail_step_pct=0.5)
    for peak, want in ((0.0, -2.0), (0.9, -2.0), (1.0, -1.5), (2.4, -1.0), (6.0, 1.0)):
        st.peak_pct = peak
        assert st._stop_level() == pytest.approx(want), peak
    bp = IntradayStraddleStrategy(underlying="NIFTY", stop_loss_pct=1.25,
                                  trail_trigger_pct=1.0, trail_step_pct=0.5,
                                  trail_mode="below_peak")
    for peak, want in ((0.0, -1.25), (0.9, -1.25), (1.0, 0.5), (3.0, 2.5)):
        bp.peak_pct = peak
        assert bp._stop_level() == pytest.approx(want), peak
    nt = IntradayStraddleStrategy(underlying="NIFTY", stop_loss_pct=2.0,
                                  trail_trigger_pct=0, trail_step_pct=0)
    nt.peak_pct = 9.0
    assert nt._stop_level() == pytest.approx(-2.0)
