"""straddle_btst: evening entry buys the ATM straddle on an expiry that SURVIVES the
night, hard next-session exit at exit_time, once-a-day latch, optional premium-based
target/stop, state round-trip — fake market/chain, no network."""

from __future__ import annotations

from datetime import date, datetime

from skas_algo.strategies.straddle_btst import StraddleBtstStrategy

TUE_EXPIRY = date(2026, 7, 21)             # a NIFTY weekly (Tuesday)
NEXT_EXPIRY = date(2026, 7, 28)


def chain(spot=24000.0, lot=65, prem=100.0):
    rows = [{"strike": float(k),
             "ce": {"ltp": prem, "oi": 5000},
             "pe": {"ltp": prem, "oi": 5000}}
            for k in range(int(spot - 500), int(spot + 550), 50)]
    return {"spot": spot, "atm_strike": float(round(spot / 50) * 50),
            "lot_size": lot, "rows": rows}


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


def enter(st, ctx, when):
    ctx._now = when
    sigs = st.on_slice(ctx)
    for leg in st.legs:
        ctx.positions[leg["symbol"]] = leg["units"]
        ctx.market.prices[leg["symbol"]] = leg["entry"]
    return sigs


def test_entry_buys_atm_on_next_alive_expiry_and_exits_next_morning():
    st = StraddleBtstStrategy(underlying="NIFTY", lots=1)
    ctx = FakeCtx(FakeMarket(chain()), FakeCacheChain([TUE_EXPIRY, NEXT_EXPIRY]))

    # Before the window — no entry.
    ctx._now = datetime(2026, 7, 21, 15, 0)
    assert st.on_slice(ctx) == []

    # ON EXPIRY DAY at 15:20: today's weekly dies tonight → must pick the NEXT one.
    sigs = enter(st, ctx, datetime(2026, 7, 21, 15, 20))
    assert len(sigs) == 2 and all(s.action.name == "ENTER_LONG" for s in sigs)
    assert all(NEXT_EXPIRY.isoformat() in leg["symbol"] for leg in st.legs)
    assert all(leg["dir"] == 1 for leg in st.legs)
    assert st.premium_paid == 200.0 * 65  # (CE 100 + PE 100) x 65

    # Same evening: no exit, and the latch blocks a second entry.
    ctx._now = datetime(2026, 7, 21, 15, 25)
    assert st.on_slice(ctx) == []

    # Next morning BEFORE exit_time — still held.
    ctx._now = datetime(2026, 7, 22, 9, 16)
    assert st.on_slice(ctx) == []

    # 09:20 next session — hard exit.
    ctx._now = datetime(2026, 7, 22, 9, 20)
    sigs = st.on_slice(ctx)
    assert len(sigs) == 2 and all(s.reason == "btst_exit" for s in sigs)
    assert st.legs == []


def test_premium_target_and_stop():
    st = StraddleBtstStrategy(underlying="NIFTY", lots=1,
                              profit_target_pct=20.0, stop_loss_pct=30.0)
    ctx = FakeCtx(FakeMarket(chain()), FakeCacheChain([NEXT_EXPIRY]))
    enter(st, ctx, datetime(2026, 7, 22, 15, 20))

    # +20% of premium → target exit (evening tick, before the time exit is armed).
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = leg["entry"] * 1.25
    ctx._now = datetime(2026, 7, 22, 15, 24)
    sigs = st.on_slice(ctx)
    assert sigs and all(s.reason == "target" for s in sigs)

    # Fresh strategy, stop side.
    st2 = StraddleBtstStrategy(underlying="NIFTY", lots=1, stop_loss_pct=30.0)
    ctx2 = FakeCtx(FakeMarket(chain()), FakeCacheChain([NEXT_EXPIRY]))
    enter(st2, ctx2, datetime(2026, 7, 22, 15, 20))
    for leg in st2.legs:
        ctx2.market.prices[leg["symbol"]] = leg["entry"] * 0.6  # −40% of premium
    ctx2._now = datetime(2026, 7, 23, 9, 16)  # before exit_time — the stop still runs
    sigs = st2.on_slice(ctx2)
    assert sigs and all(s.reason == "stop" for s in sigs)


def test_state_round_trip():
    st = StraddleBtstStrategy(underlying="NIFTY", lots=2)
    ctx = FakeCtx(FakeMarket(chain()), FakeCacheChain([NEXT_EXPIRY]))
    enter(st, ctx, datetime(2026, 7, 22, 15, 20))
    blob = st.export_state()

    st2 = StraddleBtstStrategy(underlying="NIFTY", lots=2)
    st2.load_state(blob)
    assert st2.legs == st.legs
    assert st2.entered_day == "2026-07-22"
    assert st2.premium_paid == st.premium_paid

    # The restored instance still exits on time next morning.
    ctx2 = FakeCtx(FakeMarket(chain()), FakeCacheChain([NEXT_EXPIRY]))
    for leg in st2.legs:
        ctx2.positions[leg["symbol"]] = leg["units"]
        ctx2.market.prices[leg["symbol"]] = leg["entry"]
    ctx2._now = datetime(2026, 7, 23, 9, 20)
    sigs = st2.on_slice(ctx2)
    assert len(sigs) == 2 and all(s.reason == "btst_exit" for s in sigs)
