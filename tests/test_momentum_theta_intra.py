"""momentum_theta_gainer_intra: candle aggregation, SuperTrend/pivot gating, trade cap,
EOD exit, state round-trip, weekly-expiry pick, and the Zerodha BFO/SENSEX plumbing.
All fake-driven — no network, no cache."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from skas_algo.strategies.momentum_theta_intra import MomentumThetaGainerIntra


class FakeMarket:
    def __init__(self):
        self.spots: dict[str, float] = {}

    def index_spot(self, u):
        return self.spots.get(u)


class FakeCtx:
    def __init__(self):
        self.market = FakeMarket()
        self._now: datetime | None = None
        self.prices: dict[str, float] = {}
        self.positions: dict[str, float] = {}

    def now(self):
        return self._now

    def today(self):
        return self._now.date()

    def close(self, s):
        if s in self.prices:
            return self.prices[s]
        raise KeyError(s)

    def lots(self, s):
        return self.positions.get(s, 0)

    def option_chain(self):
        return None  # forces the calendar weekly fallback


def tick(st, ctx, dt: datetime, spot: float, u: str = "NIFTY"):
    ctx._now = dt
    ctx.market.spots[u] = spot
    return st.on_slice(ctx)


def seeded_strategy(u: str = "NIFTY", **kw) -> tuple[MomentumThetaGainerIntra, FakeCtx]:
    """Strategy warmed with 2 prior days of synthetic 15-min bars: day1 flat (pivots),
    day2 a steady uptrend (SuperTrend green with the line well below price)."""
    st = MomentumThetaGainerIntra(underlyings=[u], **kw)
    ctx = FakeCtx()
    bars = []
    d1, d2 = date(2026, 7, 1), date(2026, 7, 2)
    for i in range(25):  # day1: 24000 ± tight range → P≈24000, R1/S1 close by
        t0 = datetime(d1.year, d1.month, d1.day, 9, 15) + timedelta(minutes=15 * i)
        bars.append({"start": t0.isoformat(), "open": 24000, "high": 24020,
                     "low": 23980, "close": 24000})
    for i in range(25):  # day2: ramp +12/candle → ST green
        t0 = datetime(d2.year, d2.month, d2.day, 9, 15) + timedelta(minutes=15 * i)
        px = 24000 + 12 * i
        bars.append({"start": t0.isoformat(), "open": px, "high": px + 8,
                     "low": px - 8, "close": px + 6})
    st.seed_intraday_bars(lambda _u, _d, _m: bars)
    return st, ctx


def test_candle_aggregation_boundaries():
    st = MomentumThetaGainerIntra(underlyings=["NIFTY"])
    ctx = FakeCtx()
    d = date(2026, 7, 3)

    def t(h, m, s=0):
        return datetime(d.year, d.month, d.day, h, m, s)

    tick(st, ctx, t(9, 16), 100.0)
    tick(st, ctx, t(9, 20), 105.0)
    tick(st, ctx, t(9, 29, 30), 99.0)
    assert st.bars["NIFTY"] == []          # candle still building
    tick(st, ctx, t(9, 30), 101.0)         # first tick ≥ boundary closes 09:15
    assert len(st.bars["NIFTY"]) == 1
    s0, o, h, lo, c = st.bars["NIFTY"][0]
    assert s0.endswith("09:15:00") and (o, h, lo, c) == (100.0, 105.0, 99.0, 99.0)
    # A gap across several boundaries closes the pending candle once, then restarts.
    tick(st, ctx, t(11, 5), 110.0)
    assert len(st.bars["NIFTY"]) == 2 and st.pending["NIFTY"]["start"].endswith("11:00:00")


def test_bullish_entry_sells_atm_put_weekly():
    st, ctx = seeded_strategy()
    piv_probe = None
    # Day 3 (Fri 2026-07-03): close far above R1 with ST green → sell ATM PE.
    spot = 24600.0
    d = datetime(2026, 7, 3, 9, 30)  # closes the 09:15 candle
    tick(st, ctx, datetime(2026, 7, 3, 9, 16), spot)
    # nearest NIFTY weekly ≥ 2026-07-03 (Tue calendar) = 2026-07-07
    sym = "NIFTY|2026-07-07|24600|PE"
    ctx.prices[sym] = 180.0
    sigs = tick(st, ctx, d, spot)
    piv_probe = st.pivots["NIFTY"]
    assert piv_probe is not None and piv_probe["day"] == "2026-07-03"
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.symbol == sym and sig.reason == "mtg_bull"
    assert sig.quantity == 65  # 1 lot × NIFTY 65 (2026 revision)
    assert st.entries_today["NIFTY"] == 1
    assert st.open_leg["NIFTY"]["right"] == "PE"


def test_flip_exits_and_only_later_candle_reenters():
    st, ctx = seeded_strategy()
    spot = 24600.0
    tick(st, ctx, datetime(2026, 7, 3, 9, 16), spot)
    sym = "NIFTY|2026-07-07|24600|PE"
    ctx.prices[sym] = 180.0
    tick(st, ctx, datetime(2026, 7, 3, 9, 30), spot)
    ctx.positions[sym] = 65  # engine now holds the short

    # Crash hard: several candles straight down so ST flips red and close < S1.
    px = 23200.0
    flip_sigs, reenter_sigs = [], []
    cur = datetime(2026, 7, 3, 9, 40)
    for _ in range(8):
        cur = cur + timedelta(minutes=15)
        px -= 120
        atm = round(px / 50) * 50
        ctx.prices[f"NIFTY|2026-07-07|{int(atm)}|CE"] = 150.0
        for s in tick(st, ctx, cur, px):
            (flip_sigs if s.reason == "st_flip" else reenter_sigs).append(s)
            if s.reason == "mtg_bear":
                ctx.positions[s.symbol] = s.quantity  # engine holds the new short
        if flip_sigs:
            ctx.positions[sym] = 0  # engine closed the put on the flip
    assert len(flip_sigs) == 1 and flip_sigs[0].symbol == sym
    # Re-entry (bearish CE sell) happened — but on a LATER candle than the flip.
    assert reenter_sigs and reenter_sigs[0].reason == "mtg_bear"
    assert st.entries_today["NIFTY"] == 2


def test_trade_cap_and_entry_cutoff_and_eod():
    st, ctx = seeded_strategy()
    spot = 24600.0
    tick(st, ctx, datetime(2026, 7, 3, 9, 16), spot)
    st.entries_today["NIFTY"] = 3  # cap reached
    sym = "NIFTY|2026-07-07|24600|PE"
    ctx.prices[sym] = 180.0
    assert tick(st, ctx, datetime(2026, 7, 3, 9, 30), spot) == []

    # Fresh candle after the cutoff → no entry either.
    st.entries_today["NIFTY"] = 0
    assert tick(st, ctx, datetime(2026, 7, 3, 15, 1), spot) == []

    # Open leg at 15:20 → forced exit, whatever the candle state.
    st.open_leg["NIFTY"] = {"symbol": sym, "right": "PE", "units": 65.0, "entry_close": 180.0}
    ctx.positions[sym] = 65
    sigs = tick(st, ctx, datetime(2026, 7, 3, 15, 20), spot)
    assert [s.reason for s in sigs] == ["eod_1520"]


def test_state_round_trip():
    st, ctx = seeded_strategy()
    spot = 24600.0
    tick(st, ctx, datetime(2026, 7, 3, 9, 16), spot)
    sym = "NIFTY|2026-07-07|24600|PE"
    ctx.prices[sym] = 180.0
    tick(st, ctx, datetime(2026, 7, 3, 9, 30), spot)
    dump = st.export_state()

    st2 = MomentumThetaGainerIntra(underlyings=["NIFTY"])
    st2.load_state(dump)
    assert st2.bars["NIFTY"] == st.bars["NIFTY"]
    assert st2.open_leg["NIFTY"] == st.open_leg["NIFTY"]
    assert st2.entries_today["NIFTY"] == 1 and st2._seeded
    # A restart mid-day must not re-evaluate the already-traded candle.
    ctx.positions[sym] = 65
    assert tick(st2, ctx, datetime(2026, 7, 3, 9, 31), spot) == []


def test_weekly_expiry_calendar_fallback():
    st = MomentumThetaGainerIntra(underlyings=["NIFTY", "SENSEX"])
    ctx = FakeCtx()
    ctx._now = datetime(2026, 7, 3, 10, 0)  # Friday
    assert st._weekly_expiry(ctx, "NIFTY", date(2026, 7, 3)) == date(2026, 7, 7)    # Tue
    assert st._weekly_expiry(ctx, "SENSEX", date(2026, 7, 3)) == date(2026, 7, 9)   # Thu
    # 0DTE: on the expiry day itself, min_dte=0 keeps the same-day contract.
    assert st._weekly_expiry(ctx, "NIFTY", date(2026, 7, 7)) == date(2026, 7, 7)
    st2 = MomentumThetaGainerIntra(underlyings=["NIFTY"], min_dte=1)
    assert st2._weekly_expiry(ctx, "NIFTY", date(2026, 7, 7)) == date(2026, 7, 14)


def test_sensex_contract_specs():
    from skas_algo.engine.options.contract_specs import expiry_weekday_for, lot_size_for

    assert lot_size_for("SENSEX", date(2026, 7, 3)) == 20
    assert expiry_weekday_for("SENSEX", date(2026, 7, 3), "weekly") == 3  # Thursday


# ---------------------------------------------------------- Zerodha BFO plumbing

class _FakeKite:
    def __init__(self):
        self.ltp_calls = []

    def set_access_token(self, tok):
        pass

    def instruments(self, exchange):
        if exchange == "NFO":
            return [{"name": "NIFTY", "instrument_type": "CE", "expiry": date(2026, 7, 7),
                     "strike": 24600.0, "tradingsymbol": "NIFTY26JUL24600CE", "lot_size": 65}]
        if exchange == "BFO":
            return [{"name": "SENSEX", "instrument_type": "CE", "expiry": date(2026, 7, 9),
                     "strike": 80000.0, "tradingsymbol": "SENSEX2670980000CE", "lot_size": 20}]
        raise AssertionError(exchange)

    def ltp(self, keys):
        self.ltp_calls.append(list(keys))
        return {k: {"last_price": 100.0, "instrument_token": 265} for k in keys}


def _adapter():
    from skas_algo.brokers.zerodha import ZerodhaAdapter, ZerodhaCredentials

    return ZerodhaAdapter(ZerodhaCredentials("k", "s"), kite=_FakeKite())


def test_bfo_merge_and_exchange_prefixes():
    a = _adapter()
    q = a.get_quote(["SENSEX", "NIFTY|2026-07-07|24600|CE", "SENSEX|2026-07-09|80000|CE"])
    kite = a._kite_client()
    keys = kite.ltp_calls[-1]
    assert "BSE:SENSEX" in keys                    # BSE index series
    assert "NFO:NIFTY26JUL24600CE" in keys         # NSE option
    assert "BFO:SENSEX2670980000CE" in keys        # BSE option via the merged LUT
    assert q["SENSEX|2026-07-09|80000|CE"] == 100.0
    assert a._nfo_lot["SENSEX"] == 20
    assert a.option_expiries("SENSEX") == ["2026-07-09"]
