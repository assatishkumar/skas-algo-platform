"""intraday_straddle: entry window + once-a-day latch, ATM straddle, hard time exit,
fixed %-of-margin stop, trailing stop (ratchet + below_peak), delta-ITM strikes, force
entry, state round-trip — fake market/chain, no network."""

from __future__ import annotations

from datetime import date, datetime

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
