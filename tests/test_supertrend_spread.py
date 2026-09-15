"""supertrend_spread: 09:15-anchored hourly bars from the fed spot (the stub merged into the
14:15 bar, evaluated at 15:15), a confirmed flip → a spread with the short strike behind
the SuperTrend line, the whipsaw brake, the roll, take-profit, state round-trip — fake
chain + fake market, no store."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from skas_algo.strategies.supertrend_spread import SuperTrendSpreadStrategy
from tests.test_ema21_momentum import FakeChain


class FakeMarket:
    def __init__(self):
        self.spot = 24000.0
        self.marks: dict[str, float] = {}

    def index_spot(self, _u):
        return self.spot

    def close(self, sym):
        return self.marks.get(sym)          # no mark → the take-profit check stands aside


class FakeCtx:
    def __init__(self, chain, market):
        self.chain, self.market = chain, market
        self._now: datetime | None = None
        self.positions: dict[str, float] = {}

    def option_chain(self):
        return self.chain

    def now(self):
        return self._now

    def today(self):
        return self._now.date()

    def lots(self, s):
        return self.positions.get(s, 0)

    def close(self, s):
        return self.market.close(s)


def _tick(st, ctx, day: date, hh: int, mm: int, spot: float):
    ctx._now = datetime(day.year, day.month, day.day, hh, mm)
    ctx.market.spot = spot
    sigs = st.on_slice(ctx)
    for s in sigs:
        if s.action.name in ("ENTER_SHORT", "ENTER_LONG"):
            ctx.positions[s.symbol] = 1
        else:
            ctx.positions.pop(s.symbol, None)
    return sigs


def _session_ticks():
    """One tick per minute at each hour boundary plus a few inside — enough to close bars."""
    return [(9, 15), (9, 45), (10, 15), (10, 45), (11, 15), (11, 45), (12, 15), (12, 45),
            (13, 15), (13, 45), (14, 15), (14, 45), (15, 15), (15, 25)]


def _run_days(st, ctx, start: date, path, *, collect=None):
    """``path``: a function (day_index, hh, mm) -> spot."""
    out = []
    d = start
    i = 0
    while i < len(path):
        if d.weekday() < 5:
            for hh, mm in _session_ticks():
                sigs = _tick(st, ctx, d, hh, mm, path[i](hh, mm) if callable(path[i]) else path[i])
                if sigs:
                    out.append((d, hh, mm, sigs))
                    if collect is not None:
                        collect.append((d, hh, mm,
                                        [(s.action.name, s.symbol, s.reason) for s in sigs]))
            i += 1
        d += timedelta(days=1)
    return out


def _warm(n: int = 10):
    """A gentle, noisy uptrend: ATR > 0 and the SuperTrend seeds BULLISH, so a drop is
    the first flip (a flat series seeds bearish with a zero ATR — an artefact, not a test)."""
    return [lambda hh, mm, i=i: 23800.0 + 18.0 * i + 3.0 * (hh - 9) + (15.0 if mm == 15 else -15.0)
            for i in range(n)]


def _drop(n: int = 3, base: float = 24000.0):
    return [lambda hh, mm, i=i: base - 240.0 * i - 40.0 * (hh - 9) for i in range(n)]


def _rally(n: int = 4, base: float = 23280.0):
    return [lambda hh, mm, i=i: base + 360.0 * i + 60.0 * (hh - 9) for i in range(n)]


def _mk(**kw):
    exps = [date(2026, 7, 28), date(2026, 8, 25), date(2026, 9, 29)]
    chain = FakeChain(exps, 24000.0)
    market = FakeMarket()
    ctx = FakeCtx(chain, market)
    st = SuperTrendSpreadStrategy(universe=["NIFTY"], **kw)
    return st, ctx, chain


def test_bars_are_anchored_at_0915_and_the_stub_merges_into_the_1415_bar():
    st, ctx, _ = _mk()
    d = date(2026, 7, 6)
    _run_days(st, ctx, d, [24000.0])
    assert [b[0][11:16] for b in st.bars] == ["09:15", "10:15", "11:15", "12:15", "13:15"]
    assert st.pending["start"][11:16] == "14:15" and st.evaluated_start[11:16] == "14:15"
    assert st.bars_closed == 6                  # six evaluations: 10:15 … 15:15
    # the next morning closes the 14:15 bar WITH the stub, without a seventh evaluation
    _tick(st, ctx, d + timedelta(days=1), 9, 15, 24000.0)
    assert [b[0][11:16] for b in st.bars][-1] == "14:15" and st.bars_closed == 6


def test_a_confirmed_flip_sells_the_spread_behind_the_line_and_reverses():
    st, ctx, chain = _mk(confirm_bars=1, min_hold_bars=3)
    log: list = []
    d0 = date(2026, 7, 6)
    # ten noisy up-drift sessions warm the ATR (60 bars, bullish); then a drop of 40/bar
    # for three sessions (bearish flip), then a rally of 60/bar for four (bullish flip)
    _run_days(st, ctx, d0, _warm() + _drop() + _rally(), collect=log)
    entries = [x for x in log if any(a == "ENTER_SHORT" for a, _, _ in x[3])]
    assert entries, "the confirmed bearish flip should have entered a bear call spread"
    first = entries[0]
    short = next(sym for a, sym, _ in first[3] if a == "ENTER_SHORT")
    assert short.endswith("|CE") and first[3][0][2] == "st_bear"
    # the reverse: a bullish confirmed flip later closes (reason reverse) and opens a put spread
    reverses = [x for x in log if any(r == "reverse" for _, _, r in x[3])]
    assert reverses, "the bullish flip should reverse the bear call spread"
    rv = reverses[0]
    assert [a for a, _, _ in rv[3]] == ["EXIT_ALL", "EXIT_ALL", "ENTER_SHORT", "ENTER_LONG"]
    assert rv[3][2][1].endswith("|PE") and rv[3][2][2] == "st_bull"
    assert len(st.legs) == 2 and st.direction == "bull"


def test_the_short_strike_is_anchored_to_the_line_and_steps_toward_spot():
    st, ctx, chain = _mk()
    st.direction, st.armed = "bull", True
    rows = {(r.strike, r.right): r
            for r in chain.chain("NIFTY", date(2026, 7, 6), date(2026, 7, 28))}
    # the line 23,660 → the highest 100-multiple at/below it is 23,600
    sell, buy, credit = st._find_spread(rows, "PE", 24000.0, 23660.0)
    assert sell.strike <= 23660.0 and buy.strike < sell.strike
    assert 300 <= sell.strike - buy.strike <= 500 and 80 <= credit <= 140
    # a line far below spot: the anchored strike cannot fit → two steps toward spot, else skip
    st.max_strike_steps = 0
    assert st._find_spread(rows, "PE", 24000.0, 21000.0) is None
    st.max_strike_steps = 2
    assert st._find_spread(rows, "PE", 24000.0, 21000.0) is None       # still 2000 pts away
    # a line ABOVE spot on a put (a very tight band) pulls back to the first OTM strike
    sell, _b, _c = st._find_spread(rows, "PE", 24000.0, 24250.0)
    assert sell.strike < 24000.0


def test_a_reversal_inside_min_hold_bars_goes_flat_not_reverse():
    st, ctx, chain = _mk(confirm_bars=0, min_hold_bars=100)
    log: list = []
    _run_days(st, ctx, date(2026, 7, 6), _warm() + _drop() + _rally(), collect=log)
    ws = [x for x in log if any(r == "whipsaw" for _, _, r in x[3])]
    assert ws and [a for a, _, _ in ws[0][3]] == ["EXIT_ALL", "EXIT_ALL"]
    assert not any(r == "reverse" for x in log for _, _, r in x[3])


def test_take_profit_closes_and_waits_for_a_fresh_signal():
    st, ctx, chain = _mk(confirm_bars=0, take_profit_pct=50)
    log: list = []
    _run_days(st, ctx, date(2026, 7, 6), _warm() + _drop(), collect=log)
    assert st.legs and st.entry_credit > 0
    sell = next(leg for leg in st.legs if leg["dir"] < 0)
    buy = next(leg for leg in st.legs if leg["dir"] > 0)
    # the spread decays to a quarter of its credit → a 75% profit ≥ the 50% target
    ctx.market.marks = {sell["symbol"]: buy["entry"] + st.entry_credit * 0.25,
                        buy["symbol"]: buy["entry"]}
    d = date(2026, 7, 6) + timedelta(days=21)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    # 09:15 closes the stub-merged 14:15 bar (already evaluated); 10:15 is the decision
    _tick(st, ctx, d, 9, 15, 23280.0)
    sigs = _tick(st, ctx, d, 10, 15, 23280.0)
    assert [s.reason for s in sigs] == ["target", "target"] and not st.legs
    assert st.direction == "bear" and st.armed is False       # no re-entry until a new flip


def test_state_round_trip_keeps_the_bars_and_the_book():
    st, ctx, chain = _mk(confirm_bars=0)
    _run_days(st, ctx, date(2026, 7, 6), _warm() + _drop())
    state = st.export_state()
    again = SuperTrendSpreadStrategy(universe=["NIFTY"], confirm_bars=0)
    again.load_state(state)
    assert again.export_state() == state and again.legs == st.legs
    assert again.bars_closed == st.bars_closed and again.pending == st.pending
    assert again._seeded is True                              # a restart does not re-seed


def test_seed_aggregates_15min_candles_into_the_hourly_buckets():
    st, _ctx, _ = _mk()
    day = datetime(2026, 7, 6, 9, 15)
    hist = []
    for i in range(25):                                       # 09:15 … 15:15, 15-min
        t = day + timedelta(minutes=15 * i)
        hist.append({"start": t.isoformat(), "open": 100 + i, "high": 110 + i,
                     "low": 90 + i, "close": 105 + i})
    st.seed_intraday_bars(lambda u, days, minutes: hist)
    assert [b[0][11:16] for b in st.bars] == ["09:15", "10:15", "11:15", "12:15", "13:15", "14:15"]
    last = st.bars[-1]
    assert last[1] == 120 and last[2] == 134 and last[3] == 110 and last[4] == 129   # stub merged
    assert st._seeded and st.seed_intraday_bars(lambda *a: []) is None and len(st.bars) == 6


def test_larger_timeframes_split_the_session_the_exchange_way():
    # 120m: three bars, the last (13:15) evaluated at 15:15 with the stub merged
    st, ctx, _ = _mk(timeframe=120)
    _run_days(st, ctx, date(2026, 7, 6), [24000.0])
    assert [b[0][11:16] for b in st.bars] == ["09:15", "11:15"]
    assert st.pending["start"][11:16] == "13:15" and st.evaluated_start[11:16] == "13:15"
    assert st.bars_closed == 3
    # 240m: two bars (09:15–13:15, 13:15–close), the second evaluated at 15:15
    st, ctx, _ = _mk(timeframe=240)
    _run_days(st, ctx, date(2026, 7, 6), [24000.0])
    assert [b[0][11:16] for b in st.bars] == ["09:15"] and st.evaluated_start[11:16] == "13:15"
    assert st.bars_closed == 2
    # 1d: one bar, evaluated at 15:15, closed the next morning without a second evaluation
    st, ctx, _ = _mk(timeframe=375)
    _run_days(st, ctx, date(2026, 7, 6), [24000.0])
    assert st.bars == [] and st.evaluated_start[11:16] == "09:15" and st.bars_closed == 1
    _tick(st, ctx, date(2026, 7, 7), 9, 15, 24000.0)
    assert len(st.bars) == 1 and st.bars_closed == 1


def test_a_take_profit_with_rollover_reenters_next_month_at_once():
    st, ctx, chain = _mk(confirm_bars=0, take_profit_pct=75, tp_rollover=True)
    _run_days(st, ctx, date(2026, 7, 6), _warm() + _drop())
    assert st.legs and st.entry_expiry == date(2026, 8, 25)   # entered after the 15th → Aug
    sell = next(leg for leg in st.legs if leg["dir"] < 0)
    buy = next(leg for leg in st.legs if leg["dir"] > 0)
    ctx.market.marks = {sell["symbol"]: buy["entry"] + st.entry_credit * 0.2,
                        buy["symbol"]: buy["entry"]}                     # 80% banked
    d = date(2026, 7, 27)
    _tick(st, ctx, d, 9, 15, 23280.0)
    sigs = _tick(st, ctx, d, 10, 15, 23280.0)
    assert [s.reason for s in sigs] == ["target", "target", "st_bear", "st_bear"]
    assert st.legs and st.entry_expiry == date(2026, 9, 29)   # the NEXT month, same side
    assert st.direction == "bear" and st.armed
