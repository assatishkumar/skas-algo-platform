"""double_diagonal_calendar: two-expiry entry, delta-first strikes + bias skew, ±%-margin exits,
the untested-short roll + far-hedge drag (straddle cap, <3-DTE gate), near-expiry square-off,
one-shot (no re-entry), VIX regime label, and state round-trip — fake two-expiry chain only."""

from __future__ import annotations

from datetime import date, datetime

from skas_algo.engine.options import black_scholes as bs
from skas_algo.engine.types import SignalAction
from skas_algo.strategies.double_diagonal_calendar import DoubleDiagonalCalendarStrategy

# NIFTY calendar: enter Mon 2026-07-06 (weekday 0); near weekly 2026-07-16 (10 DTE),
# far 2026-07-30 (24 DTE, ≥10 and after near).
ENTRY = datetime(2026, 7, 6, 11, 0, 0)
NEAR, FAR = date(2026, 7, 16), date(2026, 7, 30)
SPOT = 25000.0
LOT = 75


def bs_chain(spot, iv, t_days, lot=LOT, overrides=None):
    t = t_days / 365.0
    rows = []
    for k in range(int(spot - 4000), int(spot + 4100), 100):
        ce = max(bs.price(spot, k, t, 0.065, iv, "CE"), 0.6)
        pe = max(bs.price(spot, k, t, 0.065, iv, "PE"), 0.6)
        rows.append(
            {
                "strike": float(k),
                "ce": {"ltp": round(ce, 2), "oi": 9000},
                "pe": {"ltp": round(pe, 2), "oi": 9000},
            }
        )
    chain = {"spot": spot, "lot_size": lot, "rows": rows}
    if overrides:
        by = {r["strike"]: r for r in rows}
        for (k, side), ltp in overrides.items():
            by[float(k)][side] = {"ltp": float(ltp), "oi": 9000}
    return chain


class FakeCacheChain:
    def __init__(self, expiries):
        self._e = expiries

    def expiries(self, _u, frm):
        return [e for e in self._e if e >= frm]


class FakeMarket:
    def __init__(self, chains, spot=SPOT):
        self.chains = chains  # {iso_expiry: chain}
        self.prices: dict[str, float] = {}
        self._spot = spot
        self._vix: float | None = None

    def live_chain(self, _u, e):
        return self.chains.get(str(e)[:10])

    def index_spot(self, u):
        # Live path: the manager feeds INDIA VIX via set_index_spot → index_spot("INDIA VIX").
        if str(u).upper() == "INDIA VIX":
            return self._vix
        return self._spot

    def has_print(self, s):
        return s in self.prices

    def close(self, s):
        return self.prices[s]


class FakeCtx:
    def __init__(self, market, expiries=(NEAR, FAR), vix=None):
        self.market = market
        self.cache = FakeCacheChain(list(expiries))
        self._now: datetime | None = None
        self.positions: dict[str, float] = {}
        self._vix = vix
        market._vix = vix

    def now(self):
        return self._now

    def today(self):
        return self._now.date()

    def option_chain(self):
        return self.cache

    def lots(self, s):
        return self.positions.get(s, 0)

    def close(self, s):
        if s == "INDIA VIX" and self._vix is not None:
            return self._vix
        if s in self.market.prices:
            return self.market.prices[s]
        raise KeyError(s)


def near_far_chains(spot=SPOT, niv=0.12, fiv=0.13, ndays=10, fdays=24, **kw):
    return {
        NEAR.isoformat(): bs_chain(spot, niv, ndays, **kw),
        FAR.isoformat(): bs_chain(spot, fiv, fdays, **kw),
    }


def tick(st, ctx, dt):
    ctx._now = dt
    return st.on_slice(ctx)


def enter(st=None, vix=None, spot=SPOT):
    st = st or DoubleDiagonalCalendarStrategy(universe=["NIFTY"], force_entry=True)
    ctx = FakeCtx(FakeMarket(near_far_chains(spot=spot), spot=spot), vix=vix)
    sigs = tick(st, ctx, ENTRY)
    for s in sigs:
        ctx.positions[s.symbol] = s.quantity
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = leg["entry"]
    st.set_broker_margin(400_000.0)
    return st, ctx, sigs


def strike(sym):
    return float(sym.split("|")[2])


def expiry(sym):
    return sym.split("|")[1]


# --------------------------------------------------------------------------- entry


def test_entry_builds_four_legs_across_two_expiries():
    st, ctx, sigs = enter()
    assert len(sigs) == 4
    shorts = [leg for leg in st.legs if leg["dir"] < 0]
    longs = [leg for leg in st.legs if leg["dir"] > 0]
    assert len(shorts) == 2 and len(longs) == 2
    # Near shorts on NEAR expiry; far hedges on FAR expiry.
    assert all(expiry(leg["symbol"]) == NEAR.isoformat() for leg in shorts)
    assert all(expiry(leg["symbol"]) == FAR.isoformat() for leg in longs)
    assert st.near_expiry == NEAR.isoformat() and st.far_expiry == FAR.isoformat()
    assert st.phase == "strangle" and st.entered_once is True
    # ENTER_SHORT for near, ENTER_LONG for far.
    short_syms = {leg["symbol"] for leg in shorts}
    assert all(s.action == SignalAction.ENTER_SHORT for s in sigs if s.symbol in short_syms)
    assert all(s.action == SignalAction.ENTER_LONG for s in sigs if s.symbol not in short_syms)


def test_delta_first_strikes_and_hedge_wider():
    st, _, _ = enter()
    ce_s = strike(next(lg["symbol"] for lg in st.legs if lg["right"] == "CE" and lg["dir"] < 0))
    pe_s = strike(next(lg["symbol"] for lg in st.legs if lg["right"] == "PE" and lg["dir"] < 0))
    ce_h = strike(next(lg["symbol"] for lg in st.legs if lg["right"] == "CE" and lg["dir"] > 0))
    pe_h = strike(next(lg["symbol"] for lg in st.legs if lg["right"] == "PE" and lg["dir"] > 0))
    # Shorts straddle spot; both OTM.
    assert pe_s < SPOT < ce_s and pe_h < SPOT < ce_h
    # Hedges (15-20Δ) are further OTM than the shorts (20-25Δ).
    assert ce_h > ce_s and pe_h < pe_s


def test_bias_skew_shifts_the_shorts():
    up, _, _ = enter(
        DoubleDiagonalCalendarStrategy(universe=["NIFTY"], force_entry=True, bias="up")
    )
    neu, _, _ = enter(
        DoubleDiagonalCalendarStrategy(universe=["NIFTY"], force_entry=True, bias="neutral")
    )

    def shorts(st):
        return (
            strike(next(lg["symbol"] for lg in st.legs if lg["right"] == "CE" and lg["dir"] < 0)),
            strike(next(lg["symbol"] for lg in st.legs if lg["right"] == "PE" and lg["dir"] < 0)),
        )

    ce_up, pe_up = shorts(up)
    ce_n, pe_n = shorts(neu)
    # up-lean: wider calls (higher CE strike) + tighter puts (higher PE strike, closer to spot).
    assert ce_up >= ce_n and pe_up >= pe_n
    assert (ce_up, pe_up) != (ce_n, pe_n)


def test_net_premium_sign_and_vix_regime():
    st, _, _ = enter(vix=22.5)  # high VIX
    assert st.regime == "high" and st.vix_entry == 22.5
    assert st.net_premium is not None  # Σ short − Σ long premiums (points)
    lo, _ = (
        enter(DoubleDiagonalCalendarStrategy(universe=["NIFTY"], force_entry=True), vix=10.0)[0],
        None,
    )
    assert lo.regime == "low"


# ------------------------------------------------------------------------- exits


def test_profit_exit_on_pct_of_broker_margin():
    st, ctx, _ = enter()
    tick(st, ctx, datetime(2026, 7, 6, 11, 1))  # freeze the pushed broker base
    assert st.margin_base == 400_000.0 and st.margin_source == "broker"
    # Push all legs to a combined +1.5% of margin gain (shorts decay, longs unchanged).
    total_short = sum(leg["units"] for leg in st.legs if leg["dir"] < 0)
    gain = 400_000.0 * 0.015 / total_short + 0.05
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = leg["entry"] - gain if leg["dir"] < 0 else leg["entry"]
    sigs = tick(st, ctx, datetime(2026, 7, 8, 12, 0))
    assert sigs and all(s.reason == "target" for s in sigs)
    assert st.phase == "idle"


def test_stop_exit_on_pct_of_broker_margin():
    st, ctx, _ = enter()
    tick(st, ctx, datetime(2026, 7, 6, 11, 1))
    total_short = sum(leg["units"] for leg in st.legs if leg["dir"] < 0)
    loss = 400_000.0 * 0.015 / total_short + 0.05
    for leg in st.legs:  # shorts blow out (loss), longs flat → net loss past −1.5%
        ctx.market.prices[leg["symbol"]] = leg["entry"] + loss if leg["dir"] < 0 else leg["entry"]
    sigs = tick(st, ctx, datetime(2026, 7, 8, 12, 0))
    assert sigs and all(s.reason == "stop" for s in sigs)


# -------------------------------------------------------------------- adjustment


def _diagonal_legs(spot, ce_s_k, pe_s_k, ce_h_k, pe_h_k, entries):
    def leg(k, right, d):
        return {
            "symbol": f"NIFTY|{(NEAR if d < 0 else FAR).isoformat()}|{float(k)}|{right}",
            "right": right,
            "dir": d,
            "units": float(LOT),
            "entry": entries[(right, d)],
        }

    return [
        leg(ce_s_k, "CE", -1),
        leg(pe_s_k, "PE", -1),
        leg(ce_h_k, "CE", 1),
        leg(pe_h_k, "PE", 1),
    ]


def _seed_adjust(spot=25300.0, dte_days=8):
    """A held diagonal where the PE short has DECAYED (market rallied): PE mark ≤ ¼ entry,
    CE short still rich. Returns (st, ctx) ready for a manage tick."""
    st = DoubleDiagonalCalendarStrategy(universe=["NIFTY"])
    st.legs = _diagonal_legs(
        spot,
        25200,
        24700,
        25600,
        24200,
        {("CE", -1): 120.0, ("PE", -1): 110.0, ("CE", 1): 70.0, ("PE", 1): 40.0},
    )
    st.phase = "strangle"
    st.near_expiry, st.far_expiry, st.cycle_expiry = (
        NEAR.isoformat(),
        FAR.isoformat(),
        NEAR.isoformat(),
    )
    st.set_broker_margin(400_000.0)
    ctx = FakeCtx(
        FakeMarket(near_far_chains(spot=spot, ndays=dte_days, fdays=dte_days + 14), spot=spot)
    )
    for leg in st.legs:
        ctx.positions[leg["symbol"]] = leg["units"]
    # marks: CE short rich (delta stays up), PE short decayed to ¼, hedges flat
    ctx.market.prices[st.legs[0]["symbol"]] = 260.0  # CE short (tested, rich)
    ctx.market.prices[st.legs[1]["symbol"]] = 110.0 * 0.2  # PE short decayed (≤¼)
    ctx.market.prices[st.legs[2]["symbol"]] = 70.0
    ctx.market.prices[st.legs[3]["symbol"]] = 40.0
    return st, ctx


def test_untested_short_rolls_and_drags_its_hedge():
    st, ctx = _seed_adjust()
    old_pe_short = strike(st.legs[1]["symbol"])
    ce_cap = strike(st.legs[0]["symbol"])  # 25200
    sigs = tick(st, ctx, datetime(2026, 7, 8, 12, 0))
    kinds = [s.action.name for s in sigs]
    # A short roll (EXIT_ALL + ENTER_SHORT) AND a hedge drag (EXIT_ALL + ENTER_LONG).
    assert kinds.count("EXIT_ALL") >= 1 and any(s.action == SignalAction.ENTER_SHORT for s in sigs)
    new_pe_short = strike(
        next(lg["symbol"] for lg in st.legs if lg["right"] == "PE" and lg["dir"] < 0)
    )
    assert (
        old_pe_short < new_pe_short <= ce_cap
    )  # rolled up toward spot, never crossing the CE short
    assert st.adjust_count >= 1
    # the far PE hedge was dragged (a long ENTER_LONG in the signals)
    assert any(s.action == SignalAction.ENTER_LONG for s in sigs)


def test_manual_entry_legs_used_verbatim():
    """The Build view sends explicit tuned legs (ISO expiries) — they must be used VERBATIM
    (WYSIWYG), and near/far derived from the legs' own expiries."""
    legs = [
        {"side": "sell", "right": "CE", "strike": 25300, "expiry": NEAR.isoformat(), "lots": 1},
        {"side": "sell", "right": "PE", "strike": 24700, "expiry": NEAR.isoformat(), "lots": 1},
        {"side": "buy", "right": "CE", "strike": 25600, "expiry": FAR.isoformat(), "lots": 1},
        {"side": "buy", "right": "PE", "strike": 24400, "expiry": FAR.isoformat(), "lots": 1},
    ]
    st = DoubleDiagonalCalendarStrategy(universe=["NIFTY"], force_entry=True, entry_legs=legs)
    ctx = FakeCtx(FakeMarket(near_far_chains()))
    sigs = tick(st, ctx, ENTRY)
    assert len(sigs) == 4
    got = {(strike(lg["symbol"]), expiry(lg["symbol"]), lg["dir"]) for lg in st.legs}
    assert (25300.0, NEAR.isoformat(), -1) in got
    assert (24700.0, NEAR.isoformat(), -1) in got
    assert (25600.0, FAR.isoformat(), 1) in got
    assert (24400.0, FAR.isoformat(), 1) in got
    assert st.near_expiry == NEAR.isoformat() and st.far_expiry == FAR.isoformat()


def test_no_adjustment_inside_min_dte():
    st, ctx = _seed_adjust(dte_days=2)  # 2 DTE < min_adjust_dte (3)
    assert tick(st, ctx, datetime(2026, 7, 14, 12, 0)) == []
    assert st.adjust_count == 0


def test_balanced_shorts_do_not_adjust():
    st, ctx = _seed_adjust()
    # Both shorts near entry (no decay, no big loss → no roll, no stop).
    ctx.market.prices[st.legs[0]["symbol"]] = 120.0
    ctx.market.prices[st.legs[1]["symbol"]] = 110.0
    assert tick(st, ctx, datetime(2026, 7, 8, 12, 0)) == []
    assert st.adjust_count == 0


# ------------------------------------------------------------- near-expiry roll-off


def test_squares_whole_structure_at_near_expiry_eod():
    st, ctx, _ = enter()
    for leg in st.legs:  # keep marks/prints available
        ctx.market.prices[leg["symbol"]] = leg["entry"]
    # On the near-expiry day at/after eod_time → square ALL four legs (far hedge not left naked).
    sigs = tick(st, ctx, datetime(2026, 7, 16, 15, 25))
    assert len(sigs) == 4 and all(s.action == SignalAction.EXIT_ALL for s in sigs)
    assert sigs[0].reason == "near_expired" and st.phase == "idle"


def test_squares_when_near_legs_settled():
    st, ctx, _ = enter()
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = leg["entry"]
    # Simulate the engine having settled the near shorts (their lots gone) mid-day → square far.
    for leg in st.legs:
        if expiry(leg["symbol"]) == st.near_expiry:
            ctx.positions[leg["symbol"]] = 0
    sigs = tick(st, ctx, datetime(2026, 7, 16, 11, 0))
    assert sigs and all(s.reason == "near_expired" for s in sigs)


# ------------------------------------------------------------ schedule / one-shot


def test_force_entry_enters_immediately_off_schedule():
    """A force/manual deploy (force_entry=True) must enter on the NEXT tick regardless of weekday
    or window — the Build view relies on this (bug: a Tue deploy sat idle waiting for Monday)."""
    st = DoubleDiagonalCalendarStrategy(universe=["NIFTY"], force_entry=True)
    ctx = FakeCtx(FakeMarket(near_far_chains()))
    # Tuesday 2026-07-07, 14:40 — the wrong weekday AND outside the 11:00–15:00... still enters.
    sigs = tick(st, ctx, datetime(2026, 7, 7, 14, 40))
    assert len(sigs) == 4 and st.entered_once is True
    # One-shot: a later tick (still force_entry) does NOT re-enter.
    for s in sigs:
        ctx.positions[s.symbol] = 0
    assert tick(st, ctx, datetime(2026, 7, 8, 11, 30)) == []


def test_weekday_schedule_and_window():
    st = DoubleDiagonalCalendarStrategy(universe=["NIFTY"])  # not forced; Monday only
    ctx = FakeCtx(FakeMarket(near_far_chains()))
    # Monday before the window → nothing.
    assert tick(st, ctx, datetime(2026, 7, 6, 10, 30)) == []
    # Tuesday in the window → wrong day, nothing.
    assert tick(st, ctx, datetime(2026, 7, 7, 11, 30)) == []
    # Monday in the window → enters.
    assert len(tick(st, ctx, datetime(2026, 7, 6, 11, 30))) == 4


def test_one_shot_no_reentry():
    st, ctx, _ = enter()
    # Force an exit (target).
    tick(st, ctx, datetime(2026, 7, 6, 11, 1))
    total_short = sum(leg["units"] for leg in st.legs if leg["dir"] < 0)
    gain = 400_000.0 * 0.015 / total_short + 0.05
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = leg["entry"] - gain if leg["dir"] < 0 else leg["entry"]
    tick(st, ctx, datetime(2026, 7, 8, 12, 0))
    assert st.phase == "idle" and st.entered_once is True
    for leg in list(ctx.positions):
        ctx.positions[leg] = 0
    # Next Monday, in the window: a recurring=False deploy never re-enters.
    assert tick(st, ctx, datetime(2026, 7, 13, 11, 30)) == []
    # Even a force is refused once the one-shot has fired.
    st.request_force_entry()
    assert tick(st, ctx, datetime(2026, 7, 13, 11, 31)) == []


def test_recurring_reenters_next_cycle():
    st = DoubleDiagonalCalendarStrategy(universe=["NIFTY"], force_entry=True, recurring=True)
    st, ctx, _ = enter(st)
    tick(st, ctx, datetime(2026, 7, 6, 11, 1))
    total_short = sum(leg["units"] for leg in st.legs if leg["dir"] < 0)
    gain = 400_000.0 * 0.015 / total_short + 0.05
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = leg["entry"] - gain if leg["dir"] < 0 else leg["entry"]
    tick(st, ctx, datetime(2026, 7, 8, 12, 0))
    assert st.phase == "idle" and st.entered_once is True
    # Cycle 2 on the next Monday, with fresh expiries valid that day: recurring must NOT be
    # blocked by the one-shot latch.
    n2, f2 = date(2026, 7, 23), date(2026, 8, 6)
    chains = {n2.isoformat(): bs_chain(SPOT, 0.12, 12), f2.isoformat(): bs_chain(SPOT, 0.13, 26)}
    ctx2 = FakeCtx(FakeMarket(chains), expiries=(n2, f2))
    st.request_force_entry()
    sigs = tick(st, ctx2, datetime(2026, 7, 13, 11, 30))
    assert len(sigs) == 4 and st.near_expiry == n2.isoformat()


# -------------------------------------------------------------------- state


def test_state_round_trip():
    st, _, _ = enter(vix=15.0)
    dump = st.export_state()
    st2 = DoubleDiagonalCalendarStrategy(universe=["NIFTY"])
    st2.load_state(dump)
    assert st2.legs == st.legs
    assert st2.near_expiry == st.near_expiry and st2.far_expiry == st.far_expiry
    assert st2.entered_once is True and st2.regime == "medium"
    assert st2.phase == "strangle"
