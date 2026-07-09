"""iron_fly_monthly: direct ATM-straddle + breakeven-wing entry, and the post-iron-fly
adjustment (breakeven breach → sell ~15-20Δ untested side, roll on decay, exit-all when the
expiry payoff turns fully negative). Fake chain only."""

from __future__ import annotations

from datetime import date, datetime

from skas_algo.engine.types import SignalAction
from skas_algo.strategies.iron_fly_monthly import IronFlyMonthlyStrategy

PREV_EXP, CUR_EXP = date(2026, 6, 30), date(2026, 7, 28)
SPOT = 57000.0


def bs_chain(spot=SPOT, iv=0.14, t_days=26, lot=35):
    from skas_algo.engine.options import black_scholes as bs

    t = t_days / 365.0
    rows = []
    for k in range(int(spot - 8000), int(spot + 8100), 100):
        ce = max(bs.price(spot, k, t, 0.065, iv, "CE"), 0.6)
        pe = max(bs.price(spot, k, t, 0.065, iv, "PE"), 0.6)
        rows.append({"strike": float(k),
                     "ce": {"ltp": round(ce, 2), "oi": 9000},
                     "pe": {"ltp": round(pe, 2), "oi": 9000}})
    return {"spot": spot, "atm_strike": float(round(spot / 100) * 100),
            "lot_size": lot, "rows": {r["strike"]: r for r in rows}}


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
        # _chain_rows keys by float(strike); our chain["rows"] is already that shape.
        return {"spot": self.chain["spot"], "rows": list(self.chain["rows"].values())}

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
        return None


def _apply(st, ctx, sigs):
    """Book the emitted signals into the fake ctx (positions + marks=entry)."""
    for s in sigs:
        if s.action in (SignalAction.ENTER_SHORT, SignalAction.ENTER_LONG):
            ctx.positions[s.symbol] = s.quantity
        elif s.action == SignalAction.EXIT_ALL:
            ctx.positions.pop(s.symbol, None)
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = leg["entry"]


def _tick(st, ctx, dt):
    ctx._now = dt
    return st.on_slice(ctx)


def enter_ifly(st=None):
    st = st or IronFlyMonthlyStrategy()
    ctx = FakeCtx(FakeMarket(bs_chain()))
    sigs = _tick(st, ctx, datetime(2026, 7, 2, 11, 0, 30))
    _apply(st, ctx, sigs)
    st.set_broker_margin(500_000.0)
    return st, ctx, sigs


def _strike(leg):
    return float(leg["symbol"].split("|")[2])


def test_entry_is_atm_straddle_plus_breakeven_wings():
    st, ctx, sigs = enter_ifly()
    assert st.phase == "ironfly"
    shorts = [leg for leg in st.legs if leg["dir"] < 0]
    longs = [leg for leg in st.legs if leg["dir"] > 0]
    assert len(shorts) == 2 and len(longs) == 2
    ks = {leg["right"]: _strike(leg) for leg in shorts}
    assert ks["CE"] == ks["PE"]                       # straddle: both shorts at ATM K
    k = ks["CE"]
    combined = shorts[0]["entry"] + shorts[1]["entry"]
    up = next(_strike(leg) for leg in longs if leg["right"] == "CE")
    dn = next(_strike(leg) for leg in longs if leg["right"] == "PE")
    assert abs(up - (k + combined)) <= 100          # wings at the straddle breakevens (grid-snapped)
    assert abs(dn - (k - combined)) <= 100
    assert dn < k < up


def test_adjustment_sells_untested_side_on_breakeven_breach():
    st, ctx, _ = enter_ifly()
    _lo, be_hi = st._ironfly_breakevens()
    # push spot above the UPPER breakeven → call side tested → expect a naked PUT sold
    ctx.market.chain = bs_chain(spot=be_hi + 1500)
    for leg in st.legs:  # keep prints for the held legs
        ctx.market.prices[leg["symbol"]] = leg["entry"]
    sigs = _tick(st, ctx, datetime(2026, 7, 2, 11, 30, 0))
    sells = [s for s in sigs if s.action == SignalAction.ENTER_SHORT]
    assert len(sells) == 1 and sells[0].symbol.endswith("PE")     # untested = put side
    assert st.adjust_symbol == sells[0].symbol
    d = st._leg_delta(ctx.market.chain["spot"], _strike(st.legs[-1]), 20 / 365.0, "PE",
                      st.legs[-1]["entry"])
    assert 0.10 < d < 0.25                                        # ~15-20Δ


def test_adjustment_rolls_a_decayed_leg():
    st, ctx, _ = enter_ifly()
    _lo, be_hi = st._ironfly_breakevens()
    ctx.market.chain = bs_chain(spot=be_hi + 1500)
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = leg["entry"]
    _tick(st, ctx, datetime(2026, 7, 2, 11, 30, 0))              # sells the untested PUT
    adj = st.adjust_symbol
    ctx.positions[adj] = st.legs[-1]["units"]
    # decay it: mark ≤ ¼ of its sold premium
    ctx.market.prices[adj] = st.legs[-1]["entry"] * 0.2
    for leg in st.legs[:-1]:
        ctx.market.prices[leg["symbol"]] = leg["entry"]
    sigs = _tick(st, ctx, datetime(2026, 7, 2, 12, 0, 0))         # past the 15-min cooldown
    closed = [s for s in sigs if s.action == SignalAction.EXIT_ALL and s.symbol == adj]
    resold = [s for s in sigs if s.action == SignalAction.ENTER_SHORT]
    assert closed and resold                                     # closed the decayed one + re-sold
    assert st.adjust_realized > 0                                # banked the harvested credit
    assert st.adjust_symbol == resold[0].symbol


def test_exit_all_when_payoff_entirely_negative():
    st, ctx, _ = enter_ifly()
    # corrupt to a net-debit book (short entries ~0, long entries high) → payoff always < 0
    for leg in st.legs:
        leg["entry"] = 1.0 if leg["dir"] < 0 else 400.0
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = leg["entry"]
    assert st._payoff_max(st.legs, ctx.market.chain["spot"]) < 0
    sigs = _tick(st, ctx, datetime(2026, 7, 2, 11, 30, 0))
    assert all(s.action == SignalAction.EXIT_ALL for s in sigs) and sigs
    assert st.phase == "idle" and st.adjust_symbol is None


def test_target_still_books_in_ironfly():
    st, ctx, _ = enter_ifly()
    # push the whole book to +2.6% of the ₹5L margin (> the 2.5% target)
    gain = 0.026 * 500_000
    # realize it via the shorts: lower each short's mark so pnl rises
    per = gain / sum(leg["units"] for leg in st.legs if leg["dir"] < 0)
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = (leg["entry"] - per) if leg["dir"] < 0 else leg["entry"]
    sigs = _tick(st, ctx, datetime(2026, 7, 2, 11, 30, 0))
    assert sigs and all(s.action == SignalAction.EXIT_ALL for s in sigs)


def test_state_round_trip_preserves_adjustment():
    st, _, _ = enter_ifly()
    st.set_ironfly_adjust(True)
    st.adjust_symbol = "BANKNIFTY|2026-07-28|55000.0|PE"
    st.adjust_realized = 4321.0
    st2 = IronFlyMonthlyStrategy()
    st2.load_state(st.export_state())
    assert st2.ironfly_adjust is True
    assert st2.adjust_symbol == st.adjust_symbol
    assert st2.adjust_realized == 4321.0
    assert st2.phase == "ironfly" and len(st2.legs) == len(st.legs)
