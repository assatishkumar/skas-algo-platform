"""delta_neutral_monthly: entry-day math, 18Δ picks, the spec's 10/10→15/6 roll example,
straddle cap + iron-fly hedges, cooldown, exits, cycle gating, state — fake chain only."""

from __future__ import annotations

from datetime import date, datetime

from skas_algo.strategies.delta_neutral_monthly import DeltaNeutralMonthlyStrategy

# BANKNIFTY monthly cycle used throughout: prev expiry Tue 2026-06-30, current 2026-07-28.
PREV_EXP, CUR_EXP = date(2026, 6, 30), date(2026, 7, 28)
SPOT = 57000.0


def bs_chain(spot=SPOT, iv=0.14, t_days=26, lot=35, overrides=None):
    """Synthetic BANKNIFTY chain priced with flat-vol BS so deltas are well-defined."""
    from skas_algo.engine.options import black_scholes as bs

    t = t_days / 365.0
    rows = []
    for k in range(int(spot - 6000), int(spot + 6100), 100):
        ce = max(bs.price(spot, k, t, 0.065, iv, "CE"), 0.6)
        pe = max(bs.price(spot, k, t, 0.065, iv, "PE"), 0.6)
        rows.append({"strike": float(k),
                     "ce": {"ltp": round(ce, 2), "oi": 9000},
                     "pe": {"ltp": round(pe, 2), "oi": 9000}})
    chain = {"spot": spot, "atm_strike": float(round(spot / 100) * 100),
             "lot_size": lot, "rows": rows}
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
    def __init__(self, chain):
        self.chain = chain
        self.prices: dict[str, float] = {}

    def live_chain(self, _u, _e):
        return self.chain

    def index_spot(self, _u):
        return self.chain["spot"]

    def has_print(self, s):
        return s in self.prices

    def close(self, s):
        return self.prices[s]


class FakeCtx:
    def __init__(self, market, expiries=(PREV_EXP, CUR_EXP)):
        self.market = market
        self.cache = FakeCacheChain(list(expiries))
        self._now: datetime | None = None
        self.positions: dict[str, float] = {}

    def now(self):
        return self._now

    def today(self):
        return self._now.date()

    def option_chain(self):
        return self.cache

    def lots(self, s):
        return self.positions.get(s, 0)

    def close(self, s):
        if s in self.market.prices:
            return self.market.prices[s]
        raise KeyError(s)

    def position_margin(self):
        return None  # model margin path


ENTRY_DAY = date(2026, 7, 2)  # Tue expiry +2 trading days = Thursday


def tick(st, ctx, dt, held=True):
    ctx._now = dt
    return st.on_slice(ctx)


def enter(st=None):
    st = st or DeltaNeutralMonthlyStrategy()
    ctx = FakeCtx(FakeMarket(bs_chain()))
    sigs = tick(st, ctx, datetime(2026, 7, 2, 11, 0, 30))
    for s in sigs:
        ctx.positions[s.symbol] = s.quantity
    # feed marks = entries so manage() has prints
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = leg["entry"]
    st.set_broker_margin(500_000.0)  # the manager's push (broker-only margin rule)
    return st, ctx, sigs


def test_entry_day_arithmetic_and_window():
    st = DeltaNeutralMonthlyStrategy()
    assert st._entry_day(PREV_EXP) == ENTRY_DAY          # Tue → Wed(1) → Thu(2)
    assert st._entry_day(date(2026, 7, 3)) == date(2026, 7, 7)  # Fri → Mon, Tue

    ctx = FakeCtx(FakeMarket(bs_chain()))
    # Right day, before the window → nothing.
    assert tick(st, ctx, datetime(2026, 7, 2, 10, 59)) == []
    # Wrong day, in the window → nothing.
    assert tick(st, ctx, datetime(2026, 7, 3, 11, 5)) == []
    # Right day + window → two short legs.
    sigs = tick(st, ctx, datetime(2026, 7, 2, 11, 0))
    assert len(sigs) == 2 and all(s.action.name == "ENTER_SHORT" for s in sigs)
    assert st.phase == "strangle" and st.cycle_expiry == CUR_EXP.isoformat()


def test_18_delta_strikes_are_otm_and_sane():
    st, ctx, sigs = enter()
    ce = next(leg for leg in st.legs if leg["right"] == "CE")
    pe = next(leg for leg in st.legs if leg["right"] == "PE")
    ce_k, pe_k = float(ce["symbol"].split("|")[2]), float(pe["symbol"].split("|")[2])
    assert ce_k > SPOT > pe_k
    # 18Δ at 14% IV / 26d on 57000 ≈ 1400-2600 pts OTM — sanity band, not exact.
    assert 700 <= ce_k - SPOT <= 3500 and 700 <= SPOT - pe_k <= 3500
    assert all(s.quantity == 35 for s in sigs)  # 1 lot × 35
    # Broker-only rule: at entry the base is PENDING until the manager pushes it...
    assert st.margin_source == "pending" and st.margin_base == 0.0
    # ...and the first managed tick after a push freezes it.
    st.set_broker_margin(500_000.0)
    for leg in st.legs:
        ctx.positions[leg["symbol"]] = leg["units"]
        ctx.market.prices[leg["symbol"]] = leg["entry"]
    tick(st, ctx, datetime(2026, 7, 2, 11, 1))
    assert st.margin_source == "broker" and st.margin_base == 500_000.0


def test_force_entry_bypasses_the_day_gate():
    st = DeltaNeutralMonthlyStrategy(force_entry=True)
    ctx = FakeCtx(FakeMarket(bs_chain()))
    sigs = tick(st, ctx, datetime(2026, 7, 10, 11, 30))  # mid-cycle Friday
    assert len(sigs) == 2


def test_spec_example_roll_geometry():
    """The spec's example in ratio form (its 10/10 → 15/6 numbers scaled to real
    premiums): rich side ×1.5, cheap side ×0.6 → diff 0.9E > 40% of 2.1E = 0.84E →
    roll fires; the cheap PE closes and the new PE is the strike whose LTP ≈ the rich
    CE's mark, capped below the CE strike. P&L stays tiny so no target interference."""
    st, ctx, _ = enter()
    ce = next(leg for leg in st.legs if leg["right"] == "CE")
    pe = next(leg for leg in st.legs if leg["right"] == "PE")
    ce_k, pe_k = float(ce["symbol"].split("|")[2]), float(pe["symbol"].split("|")[2])
    rich_mark = round(ce["entry"] * 1.5, 2)
    cheap_mark = round(pe["entry"] * 0.6, 2)
    target_pe_k = pe_k + 800
    assert target_pe_k < ce_k
    ctx.market.chain = bs_chain(overrides={
        (ce_k, "ce"): rich_mark, (pe_k, "pe"): cheap_mark,
        (target_pe_k, "pe"): rich_mark,  # the premium-matched landing strike
    })
    ctx.market.prices[ce["symbol"]] = rich_mark
    ctx.market.prices[pe["symbol"]] = cheap_mark
    sigs = tick(st, ctx, datetime(2026, 7, 6, 11, 30))
    assert [s.action.name for s in sigs] == ["EXIT_ALL", "ENTER_SHORT"]
    assert sigs[0].symbol == pe["symbol"]
    new_pe = next(leg for leg in st.legs if leg["right"] == "PE" and leg["dir"] < 0)
    assert float(new_pe["symbol"].split("|")[2]) == target_pe_k
    assert st.adjust_count == 1 and st.phase == "strangle"

    # Below-threshold drift must NOT roll (both sides ±10%).
    st2, ctx2, _ = enter()
    ce2 = next(leg for leg in st2.legs if leg["right"] == "CE")
    pe2 = next(leg for leg in st2.legs if leg["right"] == "PE")
    ctx2.market.prices[ce2["symbol"]] = ce2["entry"] * 1.1
    ctx2.market.prices[pe2["symbol"]] = pe2["entry"] * 0.9
    assert tick(st2, ctx2, datetime(2026, 7, 6, 11, 30)) == []


def test_cooldown_suppresses_back_to_back_rolls():
    st, ctx, _ = enter()
    ce = next(leg for leg in st.legs if leg["right"] == "CE")
    pe = next(leg for leg in st.legs if leg["right"] == "PE")
    ce_k, pe_k = float(ce["symbol"].split("|")[2]), float(pe["symbol"].split("|")[2])
    rich_mark = round(ce["entry"] * 1.5, 2)
    ctx.market.chain = bs_chain(overrides={
        (ce_k, "ce"): rich_mark, (pe_k, "pe"): round(pe["entry"] * 0.6, 2),
        (pe_k + 800, "pe"): rich_mark,
    })
    ctx.market.prices[ce["symbol"]] = rich_mark
    ctx.market.prices[pe["symbol"]] = round(pe["entry"] * 0.6, 2)
    assert len(tick(st, ctx, datetime(2026, 7, 6, 11, 30))) == 2
    # Immediately imbalanced again on the NEW leg — inside the cooldown nothing rolls.
    new_pe = next(leg for leg in st.legs if leg["right"] == "PE")
    ctx.positions[new_pe["symbol"]] = new_pe["units"]
    ctx.positions[pe["symbol"]] = 0
    ctx.market.prices[new_pe["symbol"]] = round(new_pe["entry"] * 0.55, 2)
    assert tick(st, ctx, datetime(2026, 7, 6, 11, 40)) == []  # 10 min < 15 min cooldown
    assert st.adjust_count == 1


def test_straddle_cap_builds_iron_fly():
    st, ctx, _ = enter()
    ce = next(leg for leg in st.legs if leg["right"] == "CE")
    pe = next(leg for leg in st.legs if leg["right"] == "PE")
    ce_k, pe_k = float(ce["symbol"].split("|")[2]), float(pe["symbol"].split("|")[2])
    rich_mark = round(ce["entry"] * 1.45, 2)
    cheap_mark = round(pe["entry"] * 0.55, 2)
    cap_pe_mark = rich_mark - 5.0
    # Controlled chain: the ONLY PE rows at/below the cap are the cap strike itself
    # (LTP ≈ rich) and the current cheap strike — the premium match MUST land on the
    # cap → straddle → iron-fly hedges in the same decision.
    hedge_up = round((ce_k + rich_mark + cap_pe_mark) / 100) * 100
    hedge_dn = round((ce_k - rich_mark - cap_pe_mark) / 100) * 100
    def row(k, ce_ltp, pe_ltp):
        return {"strike": float(k), "ce": {"ltp": ce_ltp, "oi": 9000},
                "pe": {"ltp": pe_ltp, "oi": 9000}}

    ctx.market.chain = {"spot": SPOT, "atm_strike": 57000.0, "lot_size": 35, "rows": [
        row(ce_k, rich_mark, cap_pe_mark),
        row(pe_k, 5.0, cheap_mark),
        row(hedge_up, 40.0, 900.0),
        row(hedge_dn, 900.0, 40.0),
    ]}
    ctx.market.prices[ce["symbol"]] = rich_mark
    ctx.market.prices[pe["symbol"]] = cheap_mark
    sigs = tick(st, ctx, datetime(2026, 7, 6, 12, 0))
    kinds = [s.action.name for s in sigs]
    assert kinds == ["EXIT_ALL", "ENTER_SHORT", "ENTER_LONG", "ENTER_LONG"]
    assert st.phase == "ironfly"
    new_pe = next(s for s in sigs if s.action.name == "ENTER_SHORT")
    assert float(new_pe.symbol.split("|")[2]) == ce_k  # capped AT the CE strike
    hedge_ks = sorted(float(s.symbol.split("|")[2]) for s in sigs if s.action.name == "ENTER_LONG")
    assert hedge_ks == sorted([float(hedge_dn), float(hedge_up)])
    # Iron fly is terminal: further drift does nothing.
    for s in sigs:
        if s.action.name.startswith("ENTER"):
            ctx.positions[s.symbol] = s.quantity
    ctx.positions[pe["symbol"]] = 0
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = leg["entry"]
    assert tick(st, ctx, datetime(2026, 7, 6, 13, 0)) == []


def test_profit_target_and_optional_stop():
    st, ctx, sigs = enter()
    tick(st, ctx, datetime(2026, 7, 2, 11, 2))  # freeze the pushed broker base
    base = st.margin_base
    assert base == 500_000.0 and st.margin_source == "broker"
    total_units = sum(leg["units"] for leg in st.legs)
    gain_per_unit = base * 0.025 / total_units + 0.01
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = leg["entry"] - gain_per_unit  # shorts decayed
    sigs = tick(st, ctx, datetime(2026, 7, 8, 12, 0))
    assert sigs and all(s.reason == "target" for s in sigs)
    assert st.phase == "idle" and st.done_expiry == CUR_EXP.isoformat()

    # Stop off by default: a huge adverse move alone must not exit...
    st2, ctx2, _ = enter()
    for leg in st2.legs:
        ctx2.market.prices[leg["symbol"]] = leg["entry"]  # balanced (no roll)...
    ce2 = next(leg for leg in st2.legs if leg["right"] == "CE")
    pe2 = next(leg for leg in st2.legs if leg["right"] == "PE")
    ctx2.market.prices[ce2["symbol"]] = ce2["entry"] + base  # massive loss, balanced-ish? no:
    ctx2.market.prices[pe2["symbol"]] = pe2["entry"] + base  # both sides up → no imbalance
    out = tick(st2, ctx2, datetime(2026, 7, 8, 12, 0))
    assert out == []  # no stop configured → holds
    # ...but with stop armed it exits.
    st3 = DeltaNeutralMonthlyStrategy(stop_loss_pct=2.5)
    ctx3 = FakeCtx(FakeMarket(bs_chain()))
    s3 = tick(st3, ctx3, datetime(2026, 7, 2, 11, 0))
    for s in s3:
        ctx3.positions[s.symbol] = s.quantity
    st3.set_broker_margin(500_000.0)
    tot3 = sum(leg["units"] for leg in st3.legs)
    for leg in st3.legs:
        ctx3.market.prices[leg["symbol"]] = leg["entry"] + 500_000.0 * 0.025 / tot3 + 0.01
    sigs3 = tick(st3, ctx3, datetime(2026, 7, 8, 12, 0))
    assert sigs3 and all(s.reason == "stop" for s in sigs3)


def test_thresholds_wait_for_broker_margin():
    """Broker-only rule: no target/stop until the manager pushes a broker margin —
    even a monster profit holds. Adjustments (non-margin logic) still run."""
    st = DeltaNeutralMonthlyStrategy()
    ctx = FakeCtx(FakeMarket(bs_chain()))
    sigs = tick(st, ctx, datetime(2026, 7, 2, 11, 0))
    for s in sigs:
        ctx.positions[s.symbol] = s.quantity
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = 0.05  # shorts nearly worthless → huge profit
    assert tick(st, ctx, datetime(2026, 7, 3, 12, 0)) == []  # pending → no exit
    st.set_broker_margin(500_000.0)
    sigs = tick(st, ctx, datetime(2026, 7, 3, 12, 1))
    assert sigs and all(s.reason == "target" for s in sigs)


def test_cycle_gating_and_state_round_trip():
    st, ctx, _ = enter()
    dump = st.export_state()
    st2 = DeltaNeutralMonthlyStrategy()
    st2.load_state(dump)
    assert st2.legs == st.legs and st2.phase == "strangle"
    assert st2.cycle_expiry == CUR_EXP.isoformat()

    # After a completed cycle, the same month's expiry never re-enters.
    st3 = DeltaNeutralMonthlyStrategy(force_entry=True)
    st3.done_expiry = CUR_EXP.isoformat()
    ctx3 = FakeCtx(FakeMarket(bs_chain()))
    assert tick(st3, ctx3, datetime(2026, 7, 10, 11, 30)) == []


def test_entry_with_tz_aware_clock():
    """Live now() is IST-aware — run 203's force-entry crashed on naive-minus-aware.
    The whole entry path must work with an aware clock."""
    from zoneinfo import ZoneInfo

    st = DeltaNeutralMonthlyStrategy(force_entry=True)
    ctx = FakeCtx(FakeMarket(bs_chain()))
    ctx._now = datetime(2026, 7, 6, 13, 20, tzinfo=ZoneInfo("Asia/Kolkata"))
    sigs = st.on_slice(ctx)
    assert len(sigs) == 2 and st.phase == "strangle"


def test_force_entry_hook_bypasses_window_and_day():
    st = DeltaNeutralMonthlyStrategy()
    ctx = FakeCtx(FakeMarket(bs_chain()))
    # Mid-cycle Friday, before the entry window → refused normally.
    assert tick(st, ctx, datetime(2026, 7, 10, 9, 30)) == []
    st.request_force_entry()
    sigs = tick(st, ctx, datetime(2026, 7, 10, 9, 31))
    assert len(sigs) == 2 and not st.force_pending
