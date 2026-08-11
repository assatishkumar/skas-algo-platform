"""put_condor: first-usable-session entry, the four-strike geometry, all-or-nothing entry,
max-loss from the payoff, both adjustments (bank sign, merge refusal, atomicity), the
defined-risk guard and state round-trip — fake market/chain/ctx, no network."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from skas_algo.strategies.put_condor import PutCondorStrategy

EXPIRY = date(2026, 8, 25)          # a monthly
SPOT = 25000.0
LOT = 65


def chain(spot=SPOT, lot=LOT, drop: set[float] | None = None):
    """Puts priced so they fall away monotonically below spot — enough structure for the
    condor to be a genuine debit, which is all these tests need."""
    rows = []
    for k in range(int(spot) - 2000, int(spot) + 1100, 100):
        d = max(spot - k, 0.0)
        pe = round(60.0 * (0.97 ** ((spot - k) / 100.0)) + d * 0.15, 2)
        ce = round(60.0 * (0.97 ** ((k - spot) / 100.0)), 2)
        r = {"strike": float(k), "ce": {"ltp": ce, "oi": 5000},
             "pe": {"ltp": pe, "oi": 5000}}
        if drop and float(k) in drop:
            r["pe"] = None
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

    def position_margin(self):
        return None


def setup(**kw):
    st = PutCondorStrategy(universe=["NIFTY"], **kw)
    ctx = FakeCtx(FakeMarket(chain()), FakeCacheChain([EXPIRY]))
    return st, ctx


def tick(st, ctx, dt):
    ctx._now = dt
    return st.on_slice(ctx)


def book(st, ctx):
    """Mark every leg open at the engine, at its own entry price."""
    for leg in st.legs:
        ctx.positions[leg["symbol"]] = leg["units"]
        ctx.market.prices[leg["symbol"]] = leg["entry"]


def K(sym):
    return float(sym.split("|")[2])


def enter(st, ctx, when=datetime(2026, 8, 3, 9, 20)):
    sigs = tick(st, ctx, when)
    book(st, ctx)
    return sigs


# --------------------------------------------------------------------- entry
def test_entry_geometry_matches_the_spec_example():
    st, ctx = setup()
    sigs = enter(st, ctx)
    assert len(sigs) == 4
    by_k = {K(x["symbol"]): x for x in st.legs}
    # spot 25000, spacing 200 → 24800 long / 24600 short / 24400 short / 24200 long
    assert sorted(by_k) == [24200.0, 24400.0, 24600.0, 24800.0]
    assert by_k[24800.0]["dir"] == 1 and by_k[24200.0]["dir"] == 1
    assert by_k[24600.0]["dir"] == -1 and by_k[24400.0]["dir"] == -1
    assert all(x["right"] == "PE" for x in st.legs)
    assert all(x["units"] == LOT for x in st.legs)
    # LONGS are emitted first — a partial fill must leave the book over-hedged, never naked.
    assert [s.action.name for s in sigs][:2] == ["ENTER_LONG", "ENTER_LONG"]


def test_it_is_a_debit_and_max_loss_equals_it():
    st, ctx = setup()
    enter(st, ctx)
    debit = sum(x["dir"] * x["entry"] * x["units"] for x in st.legs)
    assert debit > 0, "a long condor is always a net debit (no-arbitrage)"
    assert st.max_loss(SPOT) == pytest.approx(debit, abs=1e-6)
    assert st.entry_max_loss == pytest.approx(debit, abs=1e-6)
    # max profit ≈ spacing − debit, at the flat top between the shorts
    assert st._payoff_max(st.legs, SPOT) == pytest.approx(st.spacing * LOT - debit, abs=1.0)


def test_entry_is_all_or_nothing():
    st = PutCondorStrategy(universe=["NIFTY"])
    ctx = FakeCtx(FakeMarket(chain(drop={24400.0})), FakeCacheChain([EXPIRY]))
    assert tick(st, ctx, datetime(2026, 8, 3, 9, 20)) == []
    assert st.legs == []          # never half-enter a condor


def test_one_entry_per_month_and_only_on_the_first_usable_session():
    st, ctx = setup()
    enter(st, ctx, datetime(2026, 8, 3, 9, 20))
    assert len(st.legs) == 4
    # same month, flat again → no re-entry
    st.legs = []
    st.phase = "idle"
    ctx.positions.clear()
    assert tick(st, ctx, datetime(2026, 8, 10, 9, 20)) == []


def test_a_dataless_session_does_not_consume_the_month():
    """2024-11-01 (Muhurat) is a captured day with no in-hours bars; a calendar detector
    burns November on it. The session stream must not advance on a day with no spot."""
    st, ctx = setup()
    st.last_session = "2026-07-31"                     # we have been running since July
    ctx.market.chain_dict = None                       # no chain, no spot
    assert tick(st, ctx, datetime(2026, 8, 3, 9, 20)) == []
    assert st.last_session == "2026-07-31"             # the month was NOT consumed
    ctx.market.chain_dict = chain()
    sigs = tick(st, ctx, datetime(2026, 8, 4, 9, 20))  # next real session enters
    assert len(sigs) == 4


def test_new_month_re_arms_without_the_holiday_table():
    st, ctx = setup()
    enter(st, ctx, datetime(2026, 8, 3, 9, 20))
    st.legs = []
    st.phase = "idle"
    st.done_expiry = None
    ctx.positions.clear()
    st.entered_day = None
    # A September session is a new month vs the recorded August session — entry re-arms even
    # though the holiday table knows nothing about September's first trading day.
    ctx.cache_chain = FakeCacheChain([date(2026, 9, 29)])
    assert st._is_entry_day(ctx, date(2026, 9, 2)) is True
    assert st._is_entry_day(ctx, date(2026, 8, 20)) is False


# ---------------------------------------------------------------- adjustments
def test_rule_a_banks_the_upper_long_and_reopens_lower():
    st, ctx = setup(down_breach_action="roll_long", loss_repair="none", adjust_cooldown_min=0)
    enter(st, ctx)
    st.set_broker_margin(400_000.0)
    hi = max(st.legs, key=lambda x: K(x["symbol"]))
    assert K(hi["symbol"]) == 24800.0
    # market falls to the upper short; the 24800 put has appreciated
    ctx.market.chain_dict = chain(spot=24600.0)
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = leg["entry"] * 2.0
    sigs = tick(st, ctx, datetime(2026, 8, 5, 11, 0))
    assert [s.action.name for s in sigs] == ["EXIT_ALL", "ENTER_LONG"]
    assert sigs[0].symbol == hi["symbol"] and sigs[0].reason == "pc_adjust_long"
    assert K(sigs[1].symbol) == 24700.0                     # 100 below the closed long
    assert 24800.0 not in {K(x["symbol"]) for x in st.legs}
    assert 24700.0 in {K(x["symbol"]) for x in st.legs}
    # the closed LONG appreciated → the bank must be POSITIVE (dir-aware, not the short
    # convention the strangle roll uses)
    assert st.adjust_realized == pytest.approx(hi["entry"] * LOT, abs=1e-6)
    assert st.n_long_rolls == 1


def test_rule_a_refuses_a_destination_already_held():
    """A replacement landing on a held strike produces the SAME symbol, merging the two
    positions so a later EXIT_ALL closes both — the run-#203 shape."""
    st, ctx = setup(down_breach_action="roll_long", long_roll_step=200,
                    loss_repair="none", adjust_cooldown_min=0)
    enter(st, ctx)
    st.set_broker_margin(400_000.0)
    ctx.market.chain_dict = chain(spot=24600.0)
    before = {x["symbol"] for x in st.legs}
    sigs = tick(st, ctx, datetime(2026, 8, 5, 11, 0))
    assert sigs == []                                   # 24800−200 = 24600, already short
    assert {x["symbol"] for x in st.legs} == before      # book untouched
    assert st.adjust_realized == 0.0                     # nothing banked on a refusal


def test_rule_c_rolls_the_lower_short_up_on_a_loss():
    st, ctx = setup(down_breach_action="none", loss_repair="roll_short_up",
                    repair_trigger_pct=50.0, adjust_cooldown_min=0)
    enter(st, ctx)
    st.set_broker_margin(400_000.0)
    ml = st.entry_max_loss
    # drive the book to −60% of max loss without moving spot into the tent
    per_unit = (ml * 0.60) / (4 * LOT)
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = max(leg["entry"] - per_unit * leg["dir"], 0.05)
    sigs = tick(st, ctx, datetime(2026, 8, 6, 11, 0))
    assert [s.action.name for s in sigs] == ["EXIT_ALL", "ENTER_SHORT"]
    assert sigs[0].reason == "pc_adjust_short"
    assert K(sigs[0].symbol) == 24400.0 and K(sigs[1].symbol) == 24500.0
    assert st.n_short_rolls == 1


def test_max_loss_is_recomputed_after_an_adjustment():
    st, ctx = setup(down_breach_action="roll_long", loss_repair="none", adjust_cooldown_min=0)
    enter(st, ctx)
    st.set_broker_margin(400_000.0)
    before = st.max_loss(SPOT)
    ctx.market.chain_dict = chain(spot=24600.0)
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = leg["entry"] * 2.0
    tick(st, ctx, datetime(2026, 8, 5, 11, 0))
    after = st.max_loss(24600.0)
    # the adjusted structure is a NARROWER tent with a floor below the lower long, so the
    # worst case is materially different from entry — a frozen debit would have lied
    assert after != pytest.approx(before, abs=1.0)
    assert st.is_defined_risk()


def test_defined_risk_guard_refuses_a_naked_book():
    st, ctx = setup()
    enter(st, ctx)
    st.legs = [x for x in st.legs if x["dir"] > 0][:1]   # simulate a lost short
    assert st.is_defined_risk() is False
    assert st.max_loss(SPOT) == 0.0                      # refuses to report a wrong number


def test_adjustments_off_means_off():
    st, ctx = setup(down_breach_action="none", loss_repair="none", adjust_cooldown_min=0)
    enter(st, ctx)
    st.set_broker_margin(400_000.0)
    ctx.market.chain_dict = chain(spot=24600.0)
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = leg["entry"] * 2.0
    assert tick(st, ctx, datetime(2026, 8, 5, 11, 0)) == []
    assert len(st.legs) == 4


# ---------------------------------------------------------------------- exits
def test_target_fires_on_a_multiple_of_max_loss():
    st, ctx = setup(target_pct_of_max_loss=100.0, down_breach_action="none",
                    loss_repair="none")
    enter(st, ctx)
    st.set_broker_margin(400_000.0)
    ml = st.entry_max_loss
    per_unit = (ml * 1.1) / (4 * LOT)
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = max(leg["entry"] + per_unit * leg["dir"], 0.05)
    sigs = tick(st, ctx, datetime(2026, 8, 6, 11, 0))
    assert len(sigs) == 4 and all(s.reason == "pc_target" for s in sigs)
    assert st.legs == []


def test_hold_to_expiry_disarms_both_thresholds():
    st, ctx = setup(hold_to_expiry=True, target_pct_of_max_loss=100.0,
                    down_breach_action="none", loss_repair="none")
    enter(st, ctx)
    st.set_broker_margin(400_000.0)
    per_unit = (st.entry_max_loss * 5) / (4 * LOT)
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = max(leg["entry"] + per_unit * leg["dir"], 0.05)
    assert tick(st, ctx, datetime(2026, 8, 6, 11, 0)) == []
    assert st.exit_amounts() == (None, None)


def test_spacing_snaps_to_the_selection_step():
    # NIFTY may only SELECT 100-multiples, so the spec's "200-250" is 200 or 300 here.
    assert PutCondorStrategy(universe=["NIFTY"], spacing=250).spacing == 200
    assert PutCondorStrategy(universe=["NIFTY"], spacing=300).spacing == 300


def test_state_round_trip():
    st, ctx = setup()
    enter(st, ctx)
    st.adjust_realized = 1234.5
    st.n_long_rolls = 1
    blob = st.export_state()
    st2 = PutCondorStrategy(universe=["NIFTY"])
    st2.load_state(blob)
    assert st2.legs == st.legs
    assert st2.entry_max_loss == pytest.approx(st.entry_max_loss)
    assert st2.last_session == st.last_session
    assert st2.n_long_rolls == 1
    assert st2.adjust_realized == pytest.approx(1234.5)


def test_recenter_closes_everything_before_reopening():
    """Shifting the condor down one spacing puts the NEW upper long on the OLD upper short's
    strike — the same symbol. All closes must precede all opens, and the book must end with
    exactly four distinct legs (the interleaved version wiped two of them)."""
    st, ctx = setup(down_breach_action="recenter", loss_repair="none", adjust_cooldown_min=0)
    enter(st, ctx)
    st.set_broker_margin(400_000.0)
    ctx.market.chain_dict = chain(spot=24600.0)
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = leg["entry"] * 1.5
    sigs = tick(st, ctx, datetime(2026, 8, 5, 11, 0))
    acts = [s.action.name for s in sigs]
    assert acts == ["EXIT_ALL"] * 4 + ["ENTER_LONG", "ENTER_LONG", "ENTER_SHORT", "ENTER_SHORT"]
    assert len(st.legs) == 4
    assert len({x["symbol"] for x in st.legs}) == 4          # no collapsed symbols
    assert sorted(K(x["symbol"]) for x in st.legs) == [24000.0, 24200.0, 24400.0, 24600.0]
    assert st.is_defined_risk()
