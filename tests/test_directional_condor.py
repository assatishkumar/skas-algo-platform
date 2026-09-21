"""directional_condor: the daily-SuperTrend read on settled bars (wait-for-flip, confirm,
force), the four-strike geometry both ways, all-or-nothing entry, the margin anchor, the
three breakeven rules, the half-loss stop, the profit-lock wing walk and its exhaustion, the
reverse-on-flip and the pre-expiry roll, and the state round-trip — fake market/chain/ctx,
no network."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from skas_algo.engine.indicators.supertrend import _supertrend_bars
from skas_algo.strategies.directional_condor import DirectionalCondorStrategy

EXPIRY = date(2026, 9, 29)          # a monthly, far from the roll window
LOT = 65
BASE = 100_000.0                    # the manual anchor per lot-set


# ------------------------------------------------------------------ fakes
def chain(spot: float, lot: int = LOT, drop: set[tuple[float, str]] | None = None):
    """A chain ON THE 100 GRID (put_condor's fake steps from int(spot), which is off-grid).
    Premiums fall away monotonically OTM on both sides, so any condor is a genuine debit."""
    rows = []
    lo = int(spot // 100) * 100 - 2000
    for k in range(lo, lo + 4200, 100):
        ce = round(250.0 * (0.82 ** (max(k - spot, 0.0) / 100.0)) + max(spot - k, 0.0), 2)
        pe = round(250.0 * (0.82 ** (max(spot - k, 0.0) / 100.0)) + max(k - spot, 0.0), 2)
        r = {"strike": float(k), "ce": {"ltp": ce, "oi": 5000}, "pe": {"ltp": pe, "oi": 5000}}
        for right in ("ce", "pe"):
            if drop and (float(k), right.upper()) in drop:
                r[right] = None
        rows.append(r)
    return {"spot": spot, "atm_strike": float(round(spot / 100) * 100),
            "lot_size": lot, "rows": rows}


class FakeCacheChain:
    def __init__(self, expiries):
        self._e = expiries

    def expiries(self, _u, _today):
        return list(self._e)


class FakeMarket:
    def __init__(self, cd):
        self.chain_dict = cd
        self.prices: dict[str, float] = {}

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

    def position_margin(self):
        return None


def daily_series(flip_to: str, flat_days: int = 60, trend_days: int = 20,
                 start: date = date(2026, 5, 1), px: float = 24000.0):
    """Settled daily bars: a flat stretch, then a run that flips SuperTrend(11, 2.9) the
    requested way. Returns the rows and the date of the FIRST settled bar in the new
    direction (the flip bar)."""
    # a flat stretch seeds the indicator BEAR (close under the upper band), so a run the
    # OTHER way first guarantees a genuine flip into the wanted direction at the end
    rows, d = [], start
    step = 150.0 if flip_to == "bull" else -150.0
    for i in range(flat_days + 2 * trend_days):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        if i >= flat_days + trend_days:
            px += step
        elif i >= flat_days:
            px -= step
        rows.append((d.isoformat(), px + 40.0, px - 40.0, px))
        d += timedelta(days=1)
    df = pd.DataFrame(rows, columns=["date", "high", "low", "close"])
    st = _supertrend_bars(df[["high", "low", "close"]], 11, 2.9)
    want = 1.0 if flip_to == "bull" else -1.0
    flip_idx = max(i for i in range(1, len(st))
                   if st["direction"].iloc[i] == want and st["direction"].iloc[i - 1] == -want)
    return rows, date.fromisoformat(rows[flip_idx][0])


def bars_fn(rows):
    df_all = pd.DataFrame(rows, columns=["date", "high", "low", "close"])

    def fn(_u, start, end):
        m = (df_all["date"] >= start.isoformat()) & (df_all["date"] <= end.isoformat())
        return df_all[m].reset_index(drop=True)

    return fn


def next_session(d: date, n: int = 1) -> date:
    for _ in range(n):
        d += timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
    return d


def setup(direction="bull", spot=24299.0, expiries=(EXPIRY,), **kw):
    kw.setdefault("margin_per_set", BASE)
    st = DirectionalCondorStrategy(universe=["NIFTY"], initial_capital=500_000, **kw)
    rows, flip_day = daily_series(direction)
    st.set_daily_bars_fn(bars_fn(rows))
    ctx = FakeCtx(FakeMarket(chain(spot)), FakeCacheChain(list(expiries)))
    return st, ctx, flip_day


def tick(st, ctx, dt):
    ctx._now = dt
    return st.on_slice(ctx)


def at(day: date, hh=9, mm=30):
    return datetime(day.year, day.month, day.day, hh, mm)


def book(st, ctx):
    """Mark every leg open at the engine, at its own entry price."""
    ctx.positions = {}
    for leg in st.legs:
        ctx.positions[leg["symbol"]] = leg["units"]
        ctx.market.prices[leg["symbol"]] = leg["entry"]


def K(sym):
    return float(sym.split("|")[2])


def enter(st, ctx, flip_day, direction="bull"):
    """Walk the sessions up to the confirmed flip and the 09:30 entry the day after."""
    d = flip_day - timedelta(days=10)
    sigs = []
    for _ in range(30):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        sigs = tick(st, ctx, at(d))
        if sigs:
            break
        d += timedelta(days=1)
    assert sigs, "the condor never entered"
    book(st, ctx)
    return sigs, d


def move_spot(ctx, st, spot: float, pnl_per_unit: float | None = None):
    """Re-point the chain at ``spot`` and mark the legs so the book reads a chosen P&L
    (spread evenly over the units) — the rules read spot and P&L separately."""
    ctx.market.chain_dict = chain(spot)
    if pnl_per_unit is not None:
        n = len(st.legs)
        for leg in st.legs:
            ctx.market.prices[leg["symbol"]] = leg["entry"] + leg["dir"] * pnl_per_unit / n


# --------------------------------------------------------------- the signal
def test_a_fresh_run_inside_a_trend_waits_and_says_so():
    st, ctx, flip_day = setup()
    d = flip_day - timedelta(days=10)
    out = tick(st, ctx, at(d))
    assert out == [] and st.direction is None and not st.armed
    assert "waiting for a confirmed daily SuperTrend flip" in st.last_skip["reason"]
    assert st.last_st_date == d.isoformat() and st.last_dir == -1


def test_the_read_uses_settled_bars_only_and_needs_history():
    st, ctx, flip_day = setup()
    rows, _ = daily_series("bull")
    poison_day = next_session(date.fromisoformat(rows[-1][0]))
    poison = rows + [(poison_day.isoformat(), 99999.0, 1.0, 99999.0)]
    st.set_daily_bars_fn(bars_fn(poison))
    today = date.fromisoformat(poison[-1][0])       # the poisoned row IS today → dropped
    tick(st, ctx, at(today))
    assert st.last_dir == 1 and st.last_st_date == today.isoformat()
    # too little history → no latch, the day retries
    thin = DirectionalCondorStrategy(universe=["NIFTY"], margin_per_set=BASE)
    thin.set_daily_bars_fn(bars_fn(rows[-20:]))
    tick(thin, ctx, at(today))
    assert thin.last_st_date is None and thin.last_dir is None


def test_the_flip_confirms_after_confirm_bars_then_enters_at_0930_next_session():
    st, ctx, flip_day = setup()
    tick(st, ctx, at(flip_day - timedelta(days=1)))         # bear, reading the day before the flip
    # the flip bar settles on flip_day; it is READ the next session (bars < today)
    seen_flip = next_session(flip_day)
    tick(st, ctx, at(seen_flip))
    assert st.pending_signal == {"dir": "bull", "seen": 0} and not st.armed
    confirm = next_session(seen_flip)
    out = tick(st, ctx, at(confirm, 9, 29))                  # before entry_time: no read yet
    assert out == [] and st.pending_signal == {"dir": "bull", "seen": 0}
    out = tick(st, ctx, at(confirm, 9, 30))
    assert st.direction == "bull" and len(out) == 4
    assert [s.action.name for s in out][:2] == ["ENTER_LONG", "ENTER_LONG"]
    assert all(s.reason == "dc_entry_bull" for s in out)


def test_confirm_bars_zero_acts_on_the_flip_bar_itself():
    st, ctx, flip_day = setup(confirm_bars=0)
    tick(st, ctx, at(flip_day - timedelta(days=1)))
    out = tick(st, ctx, at(next_session(flip_day)))
    assert len(out) == 4 and st.direction == "bull"


def test_force_takes_the_current_side():
    st, ctx, flip_day = setup(force_entry=True)
    d = flip_day - timedelta(days=10)                        # deep in the bear trend
    out = tick(st, ctx, at(d))
    assert len(out) == 4 and st.direction == "bear"
    assert all(x["right"] == "PE" for x in st.legs)
    # the tile button: same, from a flat un-forced run
    st2, ctx2, _ = setup()
    tick(st2, ctx2, at(d))
    assert st2.request_force_entry().startswith("next tick")
    out = tick(st2, ctx2, at(d, 9, 31))
    assert len(out) == 4 and st2.direction == "bear" and not st2.force_pending


# ------------------------------------------------------------------ geometry
def test_bull_geometry_first_otm_strike_and_one_percent_segments():
    st, ctx, flip_day = setup("bull", spot=24299.0)
    enter(st, ctx, flip_day)
    by_k = {K(x["symbol"]): x for x in st.legs}
    # spot 24,299 → K1 24,300 (first OTM CE); w = round(242.99/100)*100 = 200
    assert sorted(by_k) == [24300.0, 24500.0, 24700.0, 24900.0]
    assert by_k[24300.0]["dir"] == 1 and by_k[24900.0]["dir"] == 1
    assert by_k[24500.0]["dir"] == -1 and by_k[24700.0]["dir"] == -1
    assert all(x["right"] == "CE" and x["units"] == LOT for x in st.legs)
    debit = sum(x["dir"] * x["entry"] for x in st.legs)
    assert debit > 0 and st.entry_debit == pytest.approx(debit)
    assert st.entry_max_loss == pytest.approx(debit * LOT)
    assert st.be_near == pytest.approx(24300.0 + debit)
    assert st.be_far == pytest.approx(24900.0 - debit)
    assert st.margin_source == "" or st.margin_base == 0.0   # frozen on the first _manage


def test_bear_geometry_mirrors_downward_and_spot_on_a_strike_is_not_otm():
    st, ctx, flip_day = setup("bear", spot=24000.0, width_pct=1.25)
    enter(st, ctx, flip_day, "bear")
    by_k = {K(x["symbol"]): x for x in st.legs}
    # spot ON 24,000 → K1 23,900 (the first strike strictly OTM); w = round(300/100)*100 = 300
    assert sorted(by_k) == [23000.0, 23300.0, 23600.0, 23900.0]
    assert by_k[23900.0]["dir"] == 1 and by_k[23000.0]["dir"] == 1
    assert all(x["right"] == "PE" for x in st.legs)
    debit = st.entry_debit
    assert st.be_near == pytest.approx(23900.0 - debit)
    assert st.be_far == pytest.approx(23000.0 + debit)


def test_a_thin_leg_refuses_the_whole_condor_and_retries_inside_the_window():
    st, ctx, flip_day = setup()
    ctx.market.chain_dict = chain(24299.0, drop={(24900.0, "CE")})
    d = flip_day - timedelta(days=10)
    sigs = []
    for _ in range(30):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        sigs = tick(st, ctx, at(d))
        if st.armed:
            break
        d += timedelta(days=1)
    assert sigs == [] and st.legs == [] and "24900 unpriced" in st.last_skip["reason"]
    # the leg prints a minute later → the same day enters (no day latch on a refusal)
    ctx.market.chain_dict = chain(24299.0)
    out = tick(st, ctx, at(d, 9, 31))
    assert len(out) == 4 and st.entered_day == d.isoformat()


# ------------------------------------------------------------------- margin
def test_the_manual_anchor_outranks_the_broker_push_and_arms_from_the_first_tick():
    st, ctx, flip_day = setup()
    _, d = enter(st, ctx, flip_day)
    st.set_broker_margin(4_500.0)                            # ≈ the debit, live
    tick(st, ctx, at(d, 9, 31))
    assert st.margin_source == "manual" and st.margin_base == BASE
    # anchor off: pending until the push, then the broker figure is the cycle's base
    st2, ctx2, flip2 = setup(margin_per_set=0)
    _, d2 = enter(st2, ctx2, flip2)
    tick(st2, ctx2, at(d2, 9, 31))
    assert st2.margin_source == "pending"
    st2.set_broker_margin(4_500.0)
    tick(st2, ctx2, at(d2, 9, 32))
    assert st2.margin_source == "broker" and st2.margin_base == 4_500.0


# --------------------------------------------------------------- breakevens
def test_same_side_breakout_exits_and_rebuilds_from_the_new_spot_next_slice():
    st, ctx, flip_day = setup()
    _, d = enter(st, ctx, flip_day)
    old = {K(x["symbol"]) for x in st.legs}
    move_spot(ctx, st, st.be_far + 50.0, pnl_per_unit=10.0)
    out = tick(st, ctx, at(d, 10, 0))
    assert [s.action.name for s in out] == ["EXIT_ALL"] * 4
    assert all(s.reason == "dc_breakout" for s in out)
    assert st.legs == [] and st.armed and st.rebuild_pending == {"reason": "breakout"}
    ctx.positions = {}
    out = tick(st, ctx, at(d, 10, 1))                        # not 09:30, not a new day
    assert len(out) == 4 and all(s.reason == "dc_entry_bull" for s in out)
    new = {K(x["symbol"]) for x in st.legs}
    assert min(new) > min(old) and st.rebuild_pending is None


def test_back_through_the_near_breakeven_after_profit_exits_and_waits_for_a_flip():
    st, ctx, flip_day = setup(lock_step_pct=0)               # no wing walk in this one
    _, d = enter(st, ctx, flip_day)
    # the structure paid: spot past the near breakeven, P&L over the 2% lock threshold
    move_spot(ctx, st, st.be_near + 30.0, pnl_per_unit=(0.025 * BASE) / LOT)
    out = tick(st, ctx, at(d, 10, 0))
    assert out == [] and st.crossed_near and st.peak_pnl >= 0.02 * BASE
    # ... then it comes back through, still slightly positive: keep what is showing
    move_spot(ctx, st, st.be_near - 20.0, pnl_per_unit=(0.003 * BASE) / LOT)
    out = tick(st, ctx, at(d, 10, 1))
    assert out and all(s.reason == "dc_lock_exit" for s in out)
    assert not st.armed and st.rebuild_pending is None
    # flat for the rest of the trend — no entry tomorrow
    ctx.positions = {}
    out = tick(st, ctx, at(next_session(d)))
    assert out == [] and st.legs == []


def test_no_prior_profit_means_the_breach_rule_stays_quiet():
    st, ctx, flip_day = setup()
    _, d = enter(st, ctx, flip_day)
    # spot dips a little below the near breakeven from the start (it always is, at entry)
    move_spot(ctx, st, st.be_near - 20.0, pnl_per_unit=-1.0)
    assert tick(st, ctx, at(d, 10, 0)) == [] and st.legs


def test_the_half_loss_stop():
    st, ctx, flip_day = setup()
    _, d = enter(st, ctx, flip_day)
    half = 0.5 * st.entry_max_loss
    move_spot(ctx, st, 24000.0, pnl_per_unit=-(half * 0.9) / LOT)
    assert tick(st, ctx, at(d, 10, 0)) == []
    move_spot(ctx, st, 23950.0, pnl_per_unit=-(half * 1.05) / LOT)
    out = tick(st, ctx, at(d, 10, 1))
    assert out and all(s.reason == "dc_half_loss" for s in out) and not st.armed
    st.set_broker_margin(0)
    assert st.exit_amounts() == (None, None)                 # the cycle's base is cleared


def test_nothing_fires_before_0920():
    st, ctx, flip_day = setup()
    _, d = enter(st, ctx, flip_day)
    move_spot(ctx, st, 23950.0, pnl_per_unit=-st.entry_max_loss / LOT)
    assert tick(st, ctx, at(next_session(d), 9, 16)) == [] and st.legs
    assert tick(st, ctx, at(next_session(d), 9, 20)) and st.legs == []


# ------------------------------------------------------------ profit lock
def test_the_wing_walk_steps_once_per_level_and_exhausts_a_step_from_the_short():
    st, ctx, flip_day = setup(width_pct=1.25)                # w = 300 → two levels available
    _, d = enter(st, ctx, flip_day)
    k1, k4 = 24300.0, 25200.0
    assert {K(x["symbol"]) for x in st.legs} == {24300.0, 24600.0, 24900.0, 25200.0}
    entry_be = (st.be_near, st.be_far)
    move_spot(ctx, st, 24400.0, pnl_per_unit=(0.021 * BASE) / LOT)
    out = tick(st, ctx, at(d, 10, 0))
    assert [s.action.name for s in out] == ["EXIT_ALL", "EXIT_ALL", "ENTER_LONG", "ENTER_LONG"]
    assert all(s.reason == "dc_lock" for s in out)
    assert {K(x["symbol"]) for x in st.legs} == {24400.0, 24600.0, 24900.0, 25100.0}
    assert st.lock_level == 1 and st.adjust_realized != 0.0
    assert (st.be_near, st.be_far) == entry_be          # the decision levels are the entry's
    book(st, ctx)
    # +2.5% of the base INCLUDING the banked wings: still level 1 → nothing
    move_spot(ctx, st, 24400.0, pnl_per_unit=(0.025 * BASE - st.adjust_realized) / LOT)
    assert tick(st, ctx, at(d, 10, 1)) == [] and st.lock_level == 1
    # +3%: level 2 → the wings walk again, now one step from their shorts
    move_spot(ctx, st, 24400.0, pnl_per_unit=(0.031 * BASE - st.adjust_realized) / LOT)
    out = tick(st, ctx, at(d, 10, 2))
    assert len(out) == 4
    assert {K(x["symbol"]) for x in st.legs} == {24500.0, 24600.0, 24900.0, 25000.0}
    assert st.lock_level == 2 and not st.lock_exhausted
    book(st, ctx)
    # +4%: no room left → exhausted, nothing emitted, no level charged
    move_spot(ctx, st, 24400.0, pnl_per_unit=(0.041 * BASE - st.adjust_realized) / LOT)
    assert tick(st, ctx, at(d, 10, 3)) == [] and st.lock_exhausted and st.lock_level == 2
    assert k1 < 24500.0 < 24600.0 and 24900.0 < 25000.0 < k4


def test_a_one_percent_width_has_no_second_level():
    st, ctx, flip_day = setup()                              # w = 200
    _, d = enter(st, ctx, flip_day)
    move_spot(ctx, st, 24400.0, pnl_per_unit=(0.021 * BASE) / LOT)
    assert len(tick(st, ctx, at(d, 10, 0))) == 4
    assert {K(x["symbol"]) for x in st.legs} == {24400.0, 24500.0, 24700.0, 24800.0}
    book(st, ctx)
    move_spot(ctx, st, 24400.0, pnl_per_unit=(0.031 * BASE - st.adjust_realized) / LOT)
    assert tick(st, ctx, at(d, 10, 1)) == [] and st.lock_exhausted


# ------------------------------------------------------- flip, roll, state
def test_an_opposite_confirmed_flip_exits_and_reverses_next_slice():
    st, ctx, flip_day = setup("bull", expiries=(EXPIRY, date(2026, 10, 27)))
    _, d = enter(st, ctx, flip_day)
    # hand the strategy a series that flips BACK to bear right after
    rows, _ = daily_series("bull")
    px = rows[-1][3]
    d2 = date.fromisoformat(rows[-1][0])
    for _ in range(25):
        d2 = next_session(d2)
        px -= 400.0
        rows.append((d2.isoformat(), px + 40.0, px - 40.0, px))
    st.set_daily_bars_fn(bars_fn(rows))
    reversed_at = None
    day = d
    for _ in range(40):
        day = next_session(day)
        out = tick(st, ctx, at(day))
        if out and out[0].reason == "dc_reverse":
            reversed_at = day
            break
    assert reversed_at is not None and st.direction == "bear" and st.armed
    assert st.rebuild_pending == {"reason": "reverse"} and st.legs == []
    ctx.positions = {}
    ctx.market.chain_dict = chain(23000.0)
    out = tick(st, ctx, at(reversed_at, 9, 31))
    assert len(out) == 4 and all(s.reason == "dc_entry_bear" for s in out)
    assert all(x["right"] == "PE" for x in st.legs)


def test_the_roll_exits_before_expiry_and_re_enters_the_next_month():
    nxt = date(2026, 10, 27)
    st, ctx, flip_day = setup(expiries=(EXPIRY, nxt))
    _, d = enter(st, ctx, flip_day)
    assert st.cycle_expiry == EXPIRY.isoformat()
    roll_day = EXPIRY - timedelta(days=5)
    move_spot(ctx, st, 24299.0, pnl_per_unit=1.0)
    out = tick(st, ctx, at(roll_day, 10, 0))
    assert out and all(s.reason == "dc_roll" for s in out)
    assert st.rebuild_pending == {"reason": "roll"} and st.min_expiry == EXPIRY
    ctx.positions = {}
    out = tick(st, ctx, at(roll_day, 10, 1))
    assert len(out) == 4 and st.cycle_expiry == nxt.isoformat()


def test_expiry_switch_day_picks_the_next_month():
    nxt = date(2026, 10, 27)
    st, ctx, _ = setup(expiries=(EXPIRY, nxt))
    assert st._target_expiry(ctx, date(2026, 9, 14)) == EXPIRY
    assert st._target_expiry(ctx, date(2026, 9, 15)) == nxt


def test_state_round_trip_carries_the_cycle_and_the_signal_machine():
    st, ctx, flip_day = setup(width_pct=1.25)
    _, d = enter(st, ctx, flip_day)
    move_spot(ctx, st, 24400.0, pnl_per_unit=(0.021 * BASE) / LOT)
    tick(st, ctx, at(d, 10, 0))
    st.pending_signal = {"dir": "bear", "seen": 0}
    st.rebuild_pending = {"reason": "breakout"}
    st.min_expiry = EXPIRY
    saved = st.export_state()
    fresh = DirectionalCondorStrategy(universe=["NIFTY"], margin_per_set=BASE, width_pct=1.25)
    fresh.load_state(saved)
    for k in ("direction", "armed", "last_dir", "pending_signal", "last_st_date", "last_line",
              "entry_max_loss", "entry_debit", "entry_spot", "be_near", "be_far", "peak_pnl",
              "crossed_near", "lock_level", "lock_exhausted", "rebuild_pending", "min_expiry",
              "legs", "cycle_expiry", "adjust_realized", "margin_base", "margin_source"):
        assert getattr(fresh, k) == getattr(st, k), k


def test_status_and_rules_name_the_structure():
    st, ctx, flip_day = setup()
    _, d = enter(st, ctx, flip_day)
    tick(st, ctx, at(d, 9, 31))                              # freezes the anchor
    s = st.basket_status(ctx.market, None)
    assert s["kind"] == "directional_condor" and s["direction"] == "bull"
    assert s["be_near"] and s["be_far"] and s["max_loss"] == pytest.approx(st.entry_max_loss)
    assert s["max_profit"] > 0 and len(s["payoff"]) >= 4
    rules = "\n".join(st.exit_rules())
    assert "Long CE condor" in rules and "manual margin anchor" in rules
    assert "−50% of the entry max loss" in rules and "exit and reverse" in rules
    tgt, stp = st.exit_amounts()
    assert tgt is None and stp == pytest.approx(0.5 * st.entry_max_loss)
