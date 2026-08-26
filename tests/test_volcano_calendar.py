"""volcano_calendar: the last-Friday entry, the two-monthly expiry pick, the 5-leg
structure, the 4% center-credit strike walk (BS-priced far leg), the ±2% margin exits and
the near-expiry close-all — fake market/chain, no network."""

from __future__ import annotations

from datetime import date, datetime

from skas_algo.engine.options import black_scholes as bs
from skas_algo.engine.options.contract_specs import lot_size_for
from skas_algo.strategies.volcano_calendar import VolcanoCalendarStrategy, last_trading_friday

# Entry: last Friday of April 2026 = the 24th (matches the deck's worked example, whose
# legs were 26 May + 30 Jun). April's own monthly (28 Apr, days away) must be SKIPPED.
ENTRY_DAY = date(2026, 4, 24)
APR_MON = date(2026, 4, 28)
MAY_W1 = date(2026, 5, 5)          # a weekly — _monthly_of must take the month's MAX
MAY_MON = date(2026, 5, 26)
JUN_W1 = date(2026, 6, 2)
JUN_MON = date(2026, 6, 30)
EXPIRIES = [APR_MON, MAY_W1, MAY_MON, JUN_W1, JUN_MON]

SPOT = 24000.0
LOT = lot_size_for("NIFTY", MAY_MON)


def at(d: date, h: int, m: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, h, m)


def mk_chain(ce_prems: dict[float, float], pe_prems: dict[float, float], spot=SPOT):
    """A chain whose CE/PE premiums are EXACTLY what the test dictates per strike —
    no smooth curve, so each scenario controls the walk's arithmetic directly."""
    rows = []
    for i in range(-15, 16):
        k = float(24000 + i * 100)
        rows.append({
            "strike": k,
            "pe": {"ltp": pe_prems.get(k, 50.0), "oi": 5000},
            "ce": {"ltp": ce_prems.get(k, 50.0), "oi": 5000},
        })
    return {"spot": spot, "atm_strike": 24000.0, "lot_size": LOT, "rows": rows}


# Butterfly premiums: 24000 PE 180, 23600 PE 90 (×2 → 180), 23200 PE 45.
# Payoff at spot on near expiry from the PE side alone: −180 + 2·90 − 45 = −45/share.
PE_NEAR = {24000.0: 180.0, 23600.0: 90.0, 23200.0: 45.0}
# Near CE: rich at the base strike, cheap one step out — the walk's fulcrum. Values are
# BS-computed, not guessed: at 24200 (near 400, far 420 → IV 9.15%, residual value 247.6)
# the center credit is +₹11,869 = 6.25% of 1.9L → shift; at 24300 (near 120, far 360 →
# IV 8.92%, residual 197.3) it is −3.00% → accept.
CE_NEAR = {24200.0: 400.0, 24300.0: 120.0, 24400.0: 30.0}
# Far CE: enough premium that the IV solve works at every candidate strike.
CE_FAR = {24200.0: 420.0, 24300.0: 360.0, 24400.0: 300.0}


class FakeChain:
    def __init__(self, expiries):
        self._e = expiries

    def expiries(self, _u, _today):
        return [e.isoformat() for e in self._e]


class FakeMarket:
    def __init__(self, by_expiry, spot=SPOT):
        self.by_expiry = by_expiry
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


def setup(near=None, far=None, spot=SPOT, expiries=None, **kw):
    kw.setdefault("margin_per_set", 190_000.0)
    st = VolcanoCalendarStrategy(universe=["NIFTY"], **kw)
    nc = near if near is not None else mk_chain(CE_NEAR, PE_NEAR, spot=spot)
    fc = far if far is not None else mk_chain(CE_FAR, PE_NEAR, spot=spot)
    mkt = FakeMarket({APR_MON.isoformat(): nc, MAY_W1.isoformat(): nc,
                      MAY_MON.isoformat(): nc, JUN_W1.isoformat(): fc,
                      JUN_MON.isoformat(): fc}, spot=spot)
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


def enter(st, ctx, when=None):
    sigs = tick(st, ctx, when or at(ENTRY_DAY, 15, 16))
    assert sigs, "fixture should enter"
    mark_all(st, ctx)
    return sigs


# --------------------------------------------------------------- the entry day

def test_last_trading_friday():
    assert last_trading_friday(2026, 4) == date(2026, 4, 24)
    # June 2026's last Friday (the 26th) is an NSE holiday — the deck's own video entered
    # THURSDAY 25 Jun, and the helper must reproduce exactly that shift.
    assert last_trading_friday(2026, 6) == date(2026, 6, 25)
    assert last_trading_friday(2026, 8) == date(2026, 8, 28)


def test_no_entry_before_the_last_friday_or_before_1516():
    st, ctx = setup()
    assert tick(st, ctx, at(date(2026, 4, 17), 15, 16)) == []   # a Friday, not the LAST
    assert tick(st, ctx, at(ENTRY_DAY, 15, 15)) == []           # right day, too early
    assert tick(st, ctx, at(ENTRY_DAY, 15, 16))                 # deck: 3:16 PM


def test_missed_friday_still_enters_later_in_the_month():
    """The >= gate (fvc catch-up idiom): a restart or data hiccup that burned the Friday
    enters on the next session of the SAME month rather than skipping a month."""
    st, ctx = setup()
    assert tick(st, ctx, at(date(2026, 4, 27), 15, 16))         # the Monday after


def test_one_cycle_per_calendar_month():
    st, ctx = setup()
    enter(st, ctx)
    # target fires mid-cycle → flat again, same month → no re-entry in April
    st.legs = []
    ctx.positions.clear()
    assert tick(st, ctx, at(date(2026, 4, 27), 15, 16)) == []
    assert st.entered_month == "2026-04"


def test_a_close_never_burns_the_next_entry_month():
    """The month latch is stamped at ENTRY only. A cycle entered in April closes on the
    MAY expiry (the 26th) — if the close stamped May as done, the May-29 last-Friday
    entry would be silently skipped and the strategy would trade six cycles a year."""
    st, ctx = setup()
    enter(st, ctx)
    sigs = tick(st, ctx, at(MAY_MON, 15, 15))                   # near-expiry close-all
    assert sigs and all(s.action.name == "EXIT_ALL" for s in sigs)
    assert st.entered_month == "2026-04"                        # April still owns April


# --------------------------------------------------------------- the structure

def test_structure_five_legs_two_expiries():
    st, ctx = setup(lots=1)
    sigs = enter(st, ctx)
    assert len(sigs) == 5
    legs = {(strike(g["symbol"]), g["right"], g["dir"], expiry(g["symbol"])): g["units"]
            for g in st.legs}
    near, far = MAY_MON.isoformat(), JUN_MON.isoformat()
    assert legs[(24000.0, "PE", 1, near)] == LOT        # buy ATM PE
    assert legs[(23600.0, "PE", -1, near)] == 2 * LOT   # sell 2× ATM−400
    assert legs[(23200.0, "PE", 1, near)] == LOT        # buy ATM−800
    # CE calendar: ONE shared strike, short near / long far
    ce = [g for g in st.legs if g["right"] == "CE"]
    assert {g["dir"] for g in ce} == {-1, 1}
    assert len({strike(g["symbol"]) for g in ce}) == 1
    assert {expiry(g["symbol"]) for g in ce} == {near, far}
    # the entry month's own expiry (28 Apr) appears nowhere
    assert all(expiry(g["symbol"]) != APR_MON.isoformat() for g in st.legs)


def test_expiry_pick_takes_the_months_max_not_a_weekly():
    st, ctx = setup()
    ctx._now = at(ENTRY_DAY, 15, 16)
    near, far = st._pick_expiries(ctx, ENTRY_DAY)
    assert (near, far) == (MAY_MON, JUN_MON)


# --------------------------------------------------------- the 4% center-credit walk

def test_center_credit_walk_moves_the_calendar_out():
    """At 24200 the near CE is rich (₹300): center credit ≈ (300 − 45 − far decay)·LOT
    ≈ well above 4% of ₹1.9L. At 24300 the near CE is ₹50 → below the cap. The walk must
    land BOTH CE legs on 24300."""
    st, ctx = setup()
    enter(st, ctx)
    ce_strikes = {strike(g["symbol"]) for g in st.legs if g["right"] == "CE"}
    assert ce_strikes == {24300.0}
    assert st.ce_strike == 24300.0
    assert st.entry_center_pct is not None and st.entry_center_pct <= st.max_credit_pct


def test_walk_stays_put_when_the_center_is_already_lean():
    """A huge margin makes any payoff a tiny % → no shift; the base ATM+200 stands."""
    st, ctx = setup(margin_per_set=100_000_000.0)
    enter(st, ctx)
    assert {strike(g["symbol"]) for g in st.legs if g["right"] == "CE"} == {24200.0}


def test_walk_exhaustion_defers_the_entry_and_does_not_latch():
    """Every candidate strike too rich → no entry, an alert, and NO latches — the next
    tick retries (an IV spike must not burn the month silently)."""
    rich = dict.fromkeys((24200.0, 24300.0, 24400.0, 24500.0, 24600.0), 500.0)
    st, ctx = setup(near=mk_chain(rich, PE_NEAR), max_ce_shifts=2)
    assert tick(st, ctx, at(ENTRY_DAY, 15, 16)) == []
    assert st.entered_day is None and st.entered_month is None
    assert "center credit" in (st.strategy_alert or "")


def test_no_margin_anchor_skips_the_walk_but_still_enters():
    """margin_per_set=0: the rule has no denominator before the order exists (broker
    margin is pushed only after a fill) — skip the walk with an alert, base strike."""
    st, ctx = setup(margin_per_set=0.0)
    enter(st, ctx)
    assert {strike(g["symbol"]) for g in st.legs if g["right"] == "CE"} == {24200.0}
    assert "margin_per_set" in (st.strategy_alert or "")


def test_missing_far_quote_defers_without_latching():
    st, ctx = setup(far=mk_chain({}, PE_NEAR))   # far CE falls to the 50 default… kill it
    for row in ctx.market.by_expiry[JUN_MON.isoformat()]["rows"]:
        row["ce"] = None
    assert tick(st, ctx, at(ENTRY_DAY, 15, 16)) == []
    assert st.entered_day is None and st.entered_month is None


def test_payoff_on_near_expiry_prices_the_far_leg_with_bs():
    """The far CE must carry its residual time value — intrinsic-only (the base's
    _payoff_at) would misprice the calendar's whole point. Hand-check one leg."""
    st, _ = setup()
    iv, t_resid = 0.15, 35 / 365.0
    legs = [("CE", 24300.0, 1, 1.0, 360.0, True)]
    got = st._payoff_on_near_expiry(legs, SPOT, iv, t_resid)
    want = bs.price(SPOT, 24300.0, t_resid, st.r, iv, "CE") - 360.0
    assert abs(got - want) < 1e-9
    # and a near leg is plain intrinsic
    got_near = st._payoff_on_near_expiry([("PE", 24000.0, 1, 1.0, 180.0, False)], 23500.0,
                                         iv, t_resid)
    assert abs(got_near - (500.0 - 180.0)) < 1e-9


# ------------------------------------------------------------- exits

def test_target_fires_at_2pct_of_the_manual_margin():
    st, ctx = setup(lots=1)
    enter(st, ctx)
    # a flat tick freezes the MANUAL anchor — no broker push ever needed
    assert tick(st, ctx, at(date(2026, 4, 27), 9, 30)) == []
    assert st.margin_source == "manual" and st.margin_base == 190_000.0
    # lift the long far CE enough for pnl ≥ 2% of 190,000 = ₹3,800
    far_leg = next(g for g in st.legs if g["right"] == "CE" and g["dir"] == 1)
    ctx.market.prices[far_leg["symbol"]] = far_leg["entry"] + 4000.0 / far_leg["units"]
    sigs = tick(st, ctx, at(date(2026, 4, 27), 10, 0))
    assert sigs and all(s.action.name == "EXIT_ALL" for s in sigs)
    assert sigs[0].reason == "target"


def test_stop_fires_at_minus_2pct():
    st, ctx = setup(lots=1)
    enter(st, ctx)
    far_leg = next(g for g in st.legs if g["right"] == "CE" and g["dir"] == 1)
    ctx.market.prices[far_leg["symbol"]] = far_leg["entry"] - 4000.0 / far_leg["units"]
    sigs = tick(st, ctx, at(date(2026, 4, 27), 10, 0))
    assert sigs and sigs[0].reason == "stop"


def test_near_expiry_day_closes_everything_including_the_far_ce():
    st, ctx = setup()
    enter(st, ctx)
    assert tick(st, ctx, at(MAY_MON, 15, 14)) == []          # before cycle_exit_time
    sigs = tick(st, ctx, at(MAY_MON, 15, 15))
    assert len(sigs) == 5 and all(s.action.name == "EXIT_ALL" for s in sigs)
    assert sigs[0].reason == "volcano_cycle_end"
    assert st.phase == "idle" and st.legs == []
    assert ctx.positions == {}                                # far CE included


def test_past_expiry_backstop_closes_late():
    """Missed the expiry-day exit (restart) → the very next slice gets flat, whatever
    the clock says — never carry a settling short past its expiry unmanaged."""
    st, ctx = setup()
    enter(st, ctx)
    sigs = tick(st, ctx, at(date(2026, 5, 27), 9, 20))
    assert sigs and sigs[0].reason == "volcano_cycle_end_late"


# ------------------------------------------------------------- plumbing

def test_state_round_trip():
    st, ctx = setup()
    enter(st, ctx)
    st2 = VolcanoCalendarStrategy(universe=["NIFTY"], margin_per_set=190_000.0)
    st2.load_state(st.export_state())
    assert st2.legs == st.legs
    assert st2.near_expiry == MAY_MON.isoformat()
    assert st2.far_expiry == JUN_MON.isoformat()
    assert st2.entered_month == "2026-04"
    assert st2.ce_strike == st.ce_strike
    assert st2.phase == "volcano"


def test_force_entry_skips_the_friday_gate():
    st, ctx = setup(force_entry=True)
    sigs = tick(st, ctx, at(date(2026, 4, 7), 10, 0))   # a random Tuesday morning
    assert len(sigs) == 5


def test_every_deploy_model_field_is_a_real_ctor_kwarg():
    """The route enumerates every kwarg by hand and the ctor ends in **_ignored, so a
    typo'd or unforwarded knob is accepted and silently dropped — the exact bug that left
    fair_value_calendar's roll_days_before unset on every deploy until 2026-08-25."""
    import inspect

    from skas_algo.api.models import VolcanoCalendarDeploy
    from skas_algo.api.routes import trade as trade_routes  # noqa: F401 (route imports OK)

    ctor = set(inspect.signature(VolcanoCalendarStrategy.__init__).parameters)
    infra = {"name", "notes", "capital", "mode", "quote_source", "broker_account_id",
             "refresh_seconds", "ignore_market_hours", "auto"}
    strategy_fields = set(VolcanoCalendarDeploy.model_fields) - infra
    missing = strategy_fields - ctor
    assert not missing, f"deploy model sends non-ctor params: {sorted(missing)}"
