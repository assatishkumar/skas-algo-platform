"""21_ema_momentum: crossover entries, credit-window search, reverse, roll, expiry pick,
once-per-day gate, state round-trip — fake chain + fake daily-bars fn, no cache."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pandas as pd

from skas_algo.strategies.ema21_momentum import Ema21MomentumStrategy


@dataclass(frozen=True)
class Row:
    strike: float
    right: str
    close: float
    oi: int
    symbol: str


class FakeChain:
    """Minimal OptionChainView stand-in: fixed expiries, price-by-distance premiums."""

    def __init__(self, expiries: list[date], spot: float):
        self._expiries = expiries
        self._spot = spot

    def expiries(self, _u, today):
        return [e for e in self._expiries if e >= today]

    def spot(self, _u, _d):
        return self._spot

    def chain(self, u, _d, expiry):
        rows = []
        atm = round(self._spot / 100) * 100
        for k in range(int(atm - 2000), int(atm + 2100), 100):
            for right in ("CE", "PE"):
                dist = abs(k - self._spot)
                # Monotone-decaying premium in distance; wide strikes stay liquid.
                prem = max(300.0 - dist * 0.35, 2.0)
                sym = f"{u}|{expiry.isoformat()}|{k}|{right}"
                rows.append(Row(float(k), right, prem, 1000, sym))
        return rows


class FakeCtx:
    def __init__(self, chain):
        self.chain = chain
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


def bars_fn_factory(rows: list[tuple[str, float, float, float]]):
    """rows: (date_iso, high, low, close). Returns the set_daily_bars_fn provider."""
    df_all = pd.DataFrame(rows, columns=["date", "high", "low", "close"])

    def fn(_u, start, end):
        m = (df_all["date"] >= start.isoformat()) & (df_all["date"] <= end.isoformat())
        return df_all[m].reset_index(drop=True)

    return fn


def flat_then_breakout(break_dir: str, days: int = 40, breakout_days: int = 1):
    """Daily bars: a long flat channel, then `breakout_days` closes beyond the band."""
    rows = []
    d = date(2026, 5, 1)
    for _ in range(days):
        if d.weekday() < 5:
            rows.append((d.isoformat(), 24050.0, 23950.0, 24000.0))
        d += timedelta(days=1)
    for _ in range(breakout_days):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        px = 24400.0 if break_dir == "bull" else 23600.0
        rows.append((d.isoformat(), px + 50, px - 50, px))
        d += timedelta(days=1)
    return rows, date.fromisoformat(rows[-1][0])


def make_strategy(**kw):
    st = Ema21MomentumStrategy(**kw)
    return st


EXPIRIES = [date(2026, 6, 30), date(2026, 7, 28), date(2026, 8, 25)]


def decide(st, ctx, day: date, hh=15, mm=25):
    ctx._now = datetime(day.year, day.month, day.day, hh, mm)
    return st.on_slice(ctx)


def test_bull_crossover_enters_put_spread_with_credit_window():
    rows, last = flat_then_breakout("bull")
    st = make_strategy()
    st.set_daily_bars_fn(bars_fn_factory(rows))
    ctx = FakeCtx(FakeChain(EXPIRIES, spot=24400.0))
    sigs = decide(st, ctx, last)
    assert len(sigs) == 2
    sell, buy = sigs[0], sigs[1]
    assert sell.action.name == "ENTER_SHORT" and buy.action.name == "ENTER_LONG"
    s_strike = float(sell.symbol.split("|")[2])
    b_strike = float(buy.symbol.split("|")[2])
    assert sell.symbol.endswith("PE") and buy.symbol.endswith("PE")
    assert s_strike < 24400.0 and s_strike % 100 == 0        # OTM put, 100-multiple
    assert 300 <= s_strike - b_strike <= 500                 # width window
    assert 80 <= st.entry_credit <= 140                      # credit window
    assert st.direction == "bull" and st.entry_expiry is not None


def test_no_entry_without_fresh_crossover():
    # Two breakout days: day 2's close is above, but so was day 1's → not fresh.
    rows, last = flat_then_breakout("bull", breakout_days=2)
    st = make_strategy()
    st.set_daily_bars_fn(bars_fn_factory(rows))
    ctx = FakeCtx(FakeChain(EXPIRIES, spot=24400.0))
    assert decide(st, ctx, last) == []
    assert st.direction is None


def test_once_per_day_gate_and_before_decision_time():
    rows, last = flat_then_breakout("bull")
    st = make_strategy()
    st.set_daily_bars_fn(bars_fn_factory(rows))
    ctx = FakeCtx(FakeChain(EXPIRIES, spot=24400.0))
    assert decide(st, ctx, last, hh=15, mm=10) == []   # before 15:20 — no decision
    sigs = decide(st, ctx, last)                       # 15:25 — decides
    assert len(sigs) == 2
    for s in sigs:
        ctx.positions[s.symbol] = s.quantity
    assert decide(st, ctx, last, hh=15, mm=26) == []   # same-day second tick no-ops


def test_credit_miss_skips_then_retries_next_day():
    rows, last = flat_then_breakout("bull")
    st = make_strategy(credit_min=1000.0, credit_max=2000.0)  # impossible window
    st.set_daily_bars_fn(bars_fn_factory(rows))
    ctx = FakeCtx(FakeChain(EXPIRIES, spot=24400.0))
    assert decide(st, ctx, last) == []
    assert st.direction == "bull" and not st.legs      # direction stays armed
    # Next day the window is normal again (e.g. premiums moved) → retry enters.
    st.credit_min, st.credit_max = 80.0, 140.0
    nxt = last + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    rows.append((nxt.isoformat(), 24450.0, 24350.0, 24400.0))
    st.set_daily_bars_fn(bars_fn_factory(rows))
    sigs = decide(st, ctx, nxt)
    assert len(sigs) == 2 and st.legs


def test_opposite_signal_closes_and_reverses_same_slice():
    rows, last = flat_then_breakout("bull")
    st = make_strategy()
    st.set_daily_bars_fn(bars_fn_factory(rows))
    ctx = FakeCtx(FakeChain(EXPIRIES, spot=24400.0))
    for s in decide(st, ctx, last):
        ctx.positions[s.symbol] = s.quantity
    old_legs = [leg["symbol"] for leg in st.legs]

    # Crash below the lower band the following week (fresh bear crossover).
    d = last
    for px in (24350.0, 23300.0):  # one in-channel day, then the break
        d += timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        rows.append((d.isoformat(), px + 60, px - 60, px))
    st.set_daily_bars_fn(bars_fn_factory(rows))
    ctx.chain = FakeChain(EXPIRIES, spot=23300.0)
    sigs = decide(st, ctx, d)
    exits = [s for s in sigs if s.action.name == "EXIT_ALL"]
    entries = [s for s in sigs if s.action.name.startswith("ENTER")]
    assert sorted(s.symbol for s in exits) == sorted(old_legs)
    assert len(entries) == 2 and all(s.symbol.endswith("CE") for s in entries)
    assert st.direction == "bear"


def test_expiry_pick_before_and_after_the_15th():
    st = make_strategy()
    chain = FakeChain(EXPIRIES, spot=24000.0)
    assert st._target_expiry(chain, date(2026, 7, 3)) == date(2026, 7, 28)   # before 15th
    assert st._target_expiry(chain, date(2026, 7, 15)) == date(2026, 8, 25)  # on/after 15th
    # Within roll_days_before of the month's expiry → skip to the next month.
    assert st._target_expiry(chain, date(2026, 6, 26)) == date(2026, 7, 28)


def test_roll_exits_before_expiry_and_reenters_next_month():
    rows, last = flat_then_breakout("bull")
    st = make_strategy()
    st.set_daily_bars_fn(bars_fn_factory(rows))
    ctx = FakeCtx(FakeChain(EXPIRIES, spot=24400.0))
    for s in decide(st, ctx, last):
        ctx.positions[s.symbol] = s.quantity
    # Entry lands ~June 12 (before the 15th) → CURRENT month's expiry, June 30.
    assert st.entry_expiry == date(2026, 6, 30)

    # Walk forward (in-channel days — no new signal) to 5 days before the June expiry.
    d = last
    roll_day = date(2026, 6, 25)
    while d < roll_day:
        d += timedelta(days=1)
        if d.weekday() < 5:
            rows.append((d.isoformat(), 24450.0, 24350.0, 24400.0))
    st.set_daily_bars_fn(bars_fn_factory(rows))
    sigs = decide(st, ctx, roll_day)
    exits = [s for s in sigs if s.action.name == "EXIT_ALL"]
    entries = [s for s in sigs if s.action.name.startswith("ENTER")]
    assert len(exits) == 2, "rolls out 5 days before expiry"
    assert len(entries) == 2, "direction persists → re-enters"
    # Roll day June 25 is on/after the 15th → NEXT month's expiry.
    assert st.entry_expiry == date(2026, 7, 28)


def test_state_round_trip_mid_position():
    rows, last = flat_then_breakout("bull")
    st = make_strategy()
    st.set_daily_bars_fn(bars_fn_factory(rows))
    ctx = FakeCtx(FakeChain(EXPIRIES, spot=24400.0))
    for s in decide(st, ctx, last):
        ctx.positions[s.symbol] = s.quantity
    dump = st.export_state()

    st2 = make_strategy()
    st2.load_state(dump)
    assert st2.direction == "bull"
    assert st2.legs == st.legs
    assert st2.entry_expiry == st.entry_expiry
    assert st2.last_decision_date == last.isoformat()
