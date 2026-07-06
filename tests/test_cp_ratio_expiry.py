"""call_put_ratio_expiry: expiry-day gate, entry window, ⅓-strike search, 1:3 legs,
margin-frozen exits, one-entry-per-day, state round-trip — fake market/chain, no network."""

from __future__ import annotations

from datetime import date, datetime

from skas_algo.strategies.call_put_ratio_expiry import CallPutRatioExpiryStrategy

# Tue 2026-07-07 = NIFTY weekly expiry; Thu 2026-07-09 = SENSEX weekly expiry.
NIFTY_EXP = date(2026, 7, 7)


def live_chain(spot=24000.0, atm_ce=180.0, atm_pe=170.0, decay=0.35, lot=65):
    """Synthetic chain: ATM premiums as given; OTM premiums decay linearly per 50-pt step
    so a ≈⅓ premium strike exists a few hundred points out."""
    rows = []
    for k in range(int(spot - 1500), int(spot + 1550), 50):
        dist = abs(k - spot)
        ce = max(atm_ce - decay * max(k - spot, 0) - 0.02 * max(spot - k, 0), 1.0) \
            if k >= spot else atm_ce + (spot - k) * 0.9
        pe = max(atm_pe - decay * max(spot - k, 0) - 0.02 * max(k - spot, 0), 1.0) \
            if k <= spot else atm_pe + (k - spot) * 0.9
        rows.append({"strike": float(k),
                     "ce": {"ltp": round(ce, 2), "oi": 5000, "bid": ce - 1, "ask": ce + 1},
                     "pe": {"ltp": round(pe, 2), "oi": 5000, "bid": pe - 1, "ask": pe + 1}})
    return {"spot": spot, "atm_strike": float(round(spot / 50) * 50), "lot_size": lot,
            "rows": rows}


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

    def position_margin(self):
        return None  # model fallback path


def setup(chain=None, expiries=(NIFTY_EXP,)):
    st = CallPutRatioExpiryStrategy(underlyings=["NIFTY"])
    ctx = FakeCtx(FakeMarket(chain if chain is not None else live_chain()),
                  FakeCacheChain(list(expiries)))
    return st, ctx


def tick(st, ctx, dt):
    ctx._now = dt
    return st.on_slice(ctx)


def test_enters_only_on_expiry_day_and_in_window():
    st, ctx = setup()
    # Monday (not expiry) inside the window → nothing.
    assert tick(st, ctx, datetime(2026, 7, 6, 9, 22)) == []
    # Expiry Tuesday but before/after the window → nothing.
    assert tick(st, ctx, datetime(2026, 7, 7, 9, 19)) == []
    st2, ctx2 = setup()
    assert tick(st2, ctx2, datetime(2026, 7, 7, 9, 28)) == []
    # Expiry Tuesday inside the window → 4 legs.
    st3, ctx3 = setup()
    sigs = tick(st3, ctx3, datetime(2026, 7, 7, 9, 22))
    assert len(sigs) == 4


def test_leg_construction_1_3_and_third_premium_strikes():
    st, ctx = setup()
    sigs = tick(st, ctx, datetime(2026, 7, 7, 9, 22))
    by = {s.symbol: s for s in sigs}
    longs = [s for s in sigs if s.action.name == "ENTER_LONG"]
    shorts = [s for s in sigs if s.action.name == "ENTER_SHORT"]
    assert len(longs) == 2 and len(shorts) == 2
    assert all(s.quantity == 65 for s in longs)          # 1 lot × 65
    assert all(s.quantity == 195 for s in shorts)        # 3 lots × 65
    # ATM longs at 24000; shorts at the ≈⅓-premium strikes with sane premiums.
    legs = st.legs["NIFTY"]
    prem = {leg["symbol"]: leg["entry"] for leg in legs}
    ce_short = next(leg for leg in legs if leg["dir"] < 0 and leg["symbol"].endswith("CE"))
    pe_short = next(leg for leg in legs if leg["dir"] < 0 and leg["symbol"].endswith("PE"))
    assert abs(ce_short["entry"] - 60.0) <= 18.0         # ≈ 180/3 within tolerance
    assert abs(pe_short["entry"] - 170.0 / 3) <= 17.0
    assert float(ce_short["symbol"].split("|")[2]) > 24000
    assert float(pe_short["symbol"].split("|")[2]) < 24000
    # Broker-only rule: base pending at entry, frozen from the manager's push.
    assert st.margin_source["NIFTY"] == "pending"
    st.set_broker_margin(300_000.0)
    for leg in legs:
        ctx.positions[leg["symbol"]] = leg["units"]
        ctx.market.prices[leg["symbol"]] = leg["entry"]
    tick(st, ctx, datetime(2026, 7, 7, 9, 40))
    assert st.margin_source["NIFTY"] == "broker" and st.margin_base["NIFTY"] == 300_000.0
    assert by  # silence lint


def test_tolerance_miss_skips_the_day():
    # Premiums collapse so fast no strike trades near ⅓ of ATM → skip, marked traded.
    chain = live_chain(atm_ce=180.0, atm_pe=170.0, decay=6.0)
    st, ctx = setup(chain=chain)
    assert tick(st, ctx, datetime(2026, 7, 7, 9, 22)) == []
    assert st.traded_day["NIFTY"] == "2026-07-07"
    # And the very next tick doesn't retry.
    assert tick(st, ctx, datetime(2026, 7, 7, 9, 23)) == []


def test_target_stop_and_eod_exits():
    st, ctx = setup()
    sigs = tick(st, ctx, datetime(2026, 7, 7, 9, 22))
    for s in sigs:
        ctx.positions[s.symbol] = s.quantity
    legs = st.legs["NIFTY"]
    st.set_broker_margin(300_000.0)
    base = 300_000.0
    entry_of = {leg["symbol"]: leg for leg in legs}

    def set_all(pnl_per_unit_map):
        for sym, leg in entry_of.items():
            ctx.market.prices[sym] = leg["entry"] + pnl_per_unit_map(leg)

    # Flat marks → no exit.
    set_all(lambda leg: 0.0)
    assert tick(st, ctx, datetime(2026, 7, 7, 10, 0)) == []

    # Profit: shorts decay by enough to cross +1.1% of margin_base.
    total_short_units = sum(leg["units"] for leg in legs if leg["dir"] < 0)
    need = base * 0.011
    per_unit = need / total_short_units + 0.01
    set_all(lambda leg: -per_unit if leg["dir"] < 0 else 0.0)
    sigs = tick(st, ctx, datetime(2026, 7, 7, 10, 15))
    assert sigs and all(s.reason == "target" for s in sigs) and len(sigs) == 4
    assert st.legs["NIFTY"] == []

    # Rebuild for the stop leg of the test.
    st2, ctx2 = setup()
    sigs = tick(st2, ctx2, datetime(2026, 7, 7, 9, 22))
    for s in sigs:
        ctx2.positions[s.symbol] = s.quantity
    legs2 = st2.legs["NIFTY"]
    st2.set_broker_margin(300_000.0)
    base2 = 300_000.0
    tot2 = sum(leg["units"] for leg in legs2 if leg["dir"] < 0)
    per_unit2 = (base2 * 0.01) / tot2 + 0.01
    for leg in legs2:
        ctx2.market.prices[leg["symbol"]] = leg["entry"] + (per_unit2 if leg["dir"] < 0 else 0.0)
    sigs = tick(st2, ctx2, datetime(2026, 7, 7, 11, 0))
    assert sigs and all(s.reason == "stop" for s in sigs)

    # EOD force-exit regardless of marks.
    st3, ctx3 = setup()
    sigs = tick(st3, ctx3, datetime(2026, 7, 7, 9, 22))
    for s in sigs:
        ctx3.positions[s.symbol] = s.quantity
    for leg in st3.legs["NIFTY"]:
        ctx3.market.prices[leg["symbol"]] = leg["entry"]
    sigs = tick(st3, ctx3, datetime(2026, 7, 7, 15, 20))
    assert sigs and all(s.reason == "eod_1520" for s in sigs)


def test_one_entry_per_day_and_state_round_trip():
    st, ctx = setup()
    sigs = tick(st, ctx, datetime(2026, 7, 7, 9, 22))
    for s in sigs:
        ctx.positions[s.symbol] = s.quantity
    dump = st.export_state()

    st2 = CallPutRatioExpiryStrategy(underlyings=["NIFTY"])
    st2.load_state(dump)
    assert st2.legs["NIFTY"] == st.legs["NIFTY"]
    assert st2.margin_source["NIFTY"] == "pending"  # broker base re-arrives post-recovery
    assert st2.traded_day["NIFTY"] == "2026-07-07"
    # Restarted mid-day flat (engine settled everything) → no re-entry same day.
    ctx.positions.clear()
    st2_ctx = ctx
    assert tick(st2, st2_ctx, datetime(2026, 7, 7, 9, 25)) == []


def test_sensex_thursday_gate():
    st = CallPutRatioExpiryStrategy(underlyings=["SENSEX"])
    chain = live_chain(spot=78000.0, atm_ce=300.0, atm_pe=290.0, lot=20)
    ctx = FakeCtx(FakeMarket(chain), None)  # no cache chain → calendar fallback
    ctx._now = datetime(2026, 7, 7, 9, 22)  # Tuesday — not SENSEX's day
    assert st.on_slice(ctx) == []
    ctx._now = datetime(2026, 7, 9, 9, 22)  # Thursday
    sigs = st.on_slice(ctx)
    assert len(sigs) == 4
    assert all(s.quantity in (20, 60) for s in sigs)  # 1×20 longs, 3×20 shorts
