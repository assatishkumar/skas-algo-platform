"""mark_basis (owner 2026-09-07): read the %-target/stop on EXIT prices against the REAL
fills, instead of LTP marks against the decision-time LTP.

Paper run 30 booked a "+3% target" two minutes after a 09:15 entry — on LTP marks — that the
book realised as −₹9,765: the spread had been paid on the way in and was about to be paid on
the way out, and the strategy could see neither. "exit" closes both halves. "ltp" (the ctor
default, §1) is byte-identical to before but now LOGS what the other basis would have done."""

from __future__ import annotations

import logging
from datetime import datetime
from types import SimpleNamespace

import pytest

from skas_algo.strategies.monthly_butterfly import MonthlyButterflyStrategy
from tests.test_monthly_butterfly import EXPIRIES, JUL_MON, SPOT, FakeCtx, FakeMarket, chain

LOGGER = "skas_algo.strategies.delta_neutral_monthly"
ENTRY = datetime(2026, 7, 29, 9, 20)
LATER = datetime(2026, 8, 5, 11, 0)
LATER2 = datetime(2026, 8, 6, 11, 0)
SLIP = 1.0        # ₹ per unit the fills were worse than the decision LTP, every leg


class BookMarket(FakeMarket):
    """A live chain with a two-sided book: ``spread`` is the half-width either side of LTP."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.spread: float | None = None

    def _bid_ask(self, symbol):
        if self.spread is None or symbol not in self.prices:
            return None
        ltp = self.prices[symbol]
        return (ltp - self.spread, ltp + self.spread)


class BookCtx(FakeCtx):
    """Lots the way the engine hands them back — objects with units/price/direction — filled
    SLIP worse than the decision on every leg (a long above LTP, a short below it)."""

    def __init__(self, market):
        super().__init__(market)
        self.book: dict[str, list] = {}

    def lots(self, s):
        return self.book.get(s, [])


def setup(**kw):
    st = MonthlyButterflyStrategy(universe=["NIFTY"], sets=1, margin_per_set=70_000,
                                  profit_target_pct=1, **kw)
    st.done_expiry = JUL_MON.isoformat()
    c = chain(spot=SPOT)
    mkt = BookMarket({e.isoformat(): c for e in EXPIRIES}, spot=SPOT)
    return st, BookCtx(mkt)


def tick(st, ctx, dt):
    ctx._now = dt
    sigs = st.on_slice(ctx)
    for s in sigs:
        if s.action.name in ("ENTER_SHORT", "ENTER_LONG"):
            d = -1 if s.action.name == "ENTER_SHORT" else 1
            leg = next(lg for lg in st.legs if lg["symbol"] == s.symbol)
            ctx.book[s.symbol] = [SimpleNamespace(
                units=s.quantity, direction=d, price=leg["entry"] + SLIP * d)]
        elif s.action.name == "EXIT_ALL":
            ctx.book.pop(s.symbol, None)
    return sigs


def mark_all(st, ctx, mult: float, spread: float | None):
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = leg.get("entry_ltp", leg["entry"]) * mult
    ctx.market.spread = spread


def units(st) -> int:
    return sum(int(lg["units"]) for lg in st.legs)


def test_the_default_basis_is_the_old_one_and_only_watches():
    """mark_basis="ltp": entries stay the decision LTP, marks stay LTP, the target fires on
    them exactly as before — but the fill is recorded, the shortfall is counted and the
    disagreement with the exit basis is logged. A running deploy gains evidence, not a
    behaviour change (§1)."""
    st, ctx = setup()
    assert st.mark_basis == "ltp"
    tick(st, ctx, ENTRY)
    decision = {lg["symbol"]: lg["entry"] for lg in st.legs}
    n = units(st)
    mark_all(st, ctx, 1.0, spread=None)
    tick(st, ctx, LATER)                                  # first look at the book
    for lg in st.legs:
        assert lg["entry"] == decision[lg["symbol"]]      # NOT adopted
        assert lg["fill_seen"] and lg["entry_ltp"] == decision[lg["symbol"]]
        assert lg["entry_fill"] == pytest.approx(decision[lg["symbol"]] + SLIP * lg["dir"])
    assert st.entry_shortfall == pytest.approx(SLIP * n)  # every leg cost ₹1/unit more
    # halve every premium → LTP P&L well past the ₹700 target; a ₹10 half-spread makes the
    # exit basis read ₹10/unit worse per leg on top of the ₹1 shortfall → below the target
    mark_all(st, ctx, 0.5, spread=10.0)
    sigs = tick(st, ctx, LATER2)
    assert {s.reason for s in sigs} == {"target"}        # the LTP basis still acts


def test_the_exit_basis_adopts_the_fill_and_holds_until_the_exit_side_crosses(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    st, ctx = setup(mark_basis="exit")
    tick(st, ctx, ENTRY)
    decision = {lg["symbol"]: lg["entry"] for lg in st.legs}
    n = units(st)
    mark_all(st, ctx, 1.0, spread=0.5)
    assert tick(st, ctx, LATER) == []
    for lg in st.legs:                                    # cost basis = what was PAID
        assert lg["entry"] == pytest.approx(decision[lg["symbol"]] + SLIP * lg["dir"])
        assert lg["entry_ltp"] == decision[lg["symbol"]]
    assert st.entry_shortfall == pytest.approx(SLIP * n)
    fills = [r.getMessage() for r in caplog.records if r.getMessage().startswith("MARKS fill")]
    assert len(fills) == 3 and all("adopted" in m for m in fills)

    # LTP says the target is hit; the exit side (long@bid, short@ask) does not → HOLD, and
    # say so in the log
    caplog.clear()
    mark_all(st, ctx, 0.5, spread=10.0)
    assert tick(st, ctx, LATER2) == []
    pnl_ltp, pnl_exit = st._pnl_pair
    assert pnl_ltp >= 700 > pnl_exit
    assert pnl_ltp - pnl_exit == pytest.approx(10.0 * n)   # both sit on the fill already
    div = [r.getMessage() for r in caplog.records if r.getMessage().startswith("MARKS diverge")]
    assert len(div) == 1 and "acting=exit" in div[0] and "the ltp basis would fire" in div[0]

    # a tight book → the exit side crosses too → target, logged on both bases
    caplog.clear()
    mark_all(st, ctx, 0.5, spread=0.25)
    sigs = tick(st, ctx, datetime(2026, 8, 7, 11, 0))
    assert {s.reason for s in sigs} == {"target"}
    ex = [r.getMessage() for r in caplog.records if r.getMessage().startswith("MARKS exit")]
    assert len(ex) == 1 and "reason=target basis=exit" in ex[0] and "entry_shortfall=+" in ex[0]
    assert st.entry_shortfall == 0.0 and st._pnl_pair is None   # the next cycle counts afresh


def test_the_divergence_log_is_rate_limited_to_one_a_minute(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    st, ctx = setup(mark_basis="exit")
    tick(st, ctx, ENTRY)
    mark_all(st, ctx, 0.5, spread=10.0)
    for sec in (0, 15, 30, 45):
        tick(st, ctx, datetime(2026, 8, 5, 11, 0, sec))
    tick(st, ctx, datetime(2026, 8, 5, 11, 1, 5))
    div = [r for r in caplog.records if r.getMessage().startswith("MARKS diverge")]
    assert len(div) == 2


def test_a_stop_reads_earlier_on_the_exit_basis():
    """The same spread that delays a target brings a stop forward — the right side to err
    on for a stop. LTP sits just above the stop; the exit side is through it."""
    st, ctx = setup(mark_basis="exit", stop_loss_pct=2)     # stop at −₹1,400
    tick(st, ctx, ENTRY)
    n = units(st)
    mark_all(st, ctx, 1.0, spread=0.5)
    tick(st, ctx, LATER)
    # push LTP so the LTP basis reads ≈ −₹1,000: every leg loses ₹x/unit against the book
    # entry; simplest is a uniform half-spread big enough to cross on the exit side only
    mark_all(st, ctx, 1.0, spread=(1_400 - SLIP * n) / n + 1.0)
    sigs = tick(st, ctx, LATER2)
    pnl_ltp, pnl_exit = st._pnl_pair if st._pnl_pair else (None, None)
    assert {s.reason for s in sigs} == {"stop"}
    st2, ctx2 = setup(mark_basis="ltp", stop_loss_pct=2)
    tick(st2, ctx2, ENTRY)
    mark_all(st2, ctx2, 1.0, spread=(1_400 - SLIP * n) / n + 1.0)
    assert tick(st2, ctx2, LATER2) == []                    # LTP basis: flat marks, no stop


def test_without_a_book_the_exit_basis_is_the_ltp_basis():
    """Backtest chain / cache source / one-sided quote: no bid-ask → LTP, the same fail-open
    as the spread gate, so a replay under the flag decides exactly as before."""
    st, ctx = setup(mark_basis="exit")
    tick(st, ctx, ENTRY)
    mark_all(st, ctx, 1.0, spread=None)
    assert tick(st, ctx, LATER) == []
    pnl_ltp, pnl_exit = st._pnl_pair
    assert pnl_ltp == pnl_exit                     # no book → the exit mark IS the LTP
    assert pnl_exit == pytest.approx(-SLIP * units(st))   # …against the adopted fills
    mark_all(st, ctx, 0.5, spread=None)
    assert {s.reason for s in tick(st, ctx, LATER2)} == {"target"}


def test_int_lots_and_a_shared_symbol_are_never_adopted():
    """The engine's fakes hand back an int; a symbol two legs share (the run-#203 merge)
    cannot be attributed to either. Both are left exactly as they were."""
    st, ctx = setup(mark_basis="exit")
    tick(st, ctx, ENTRY)
    before = [dict(lg) for lg in st.legs]
    sym0 = st.legs[0]["symbol"]
    ctx.book[sym0] = 5                                     # an int, not lots
    st.legs.append(dict(st.legs[1]))                       # a duplicate symbol
    mark_all(st, ctx, 1.0, spread=None)
    tick(st, ctx, LATER)
    assert st.legs[0]["entry"] == before[0]["entry"] and "fill_seen" not in st.legs[0]
    assert st.legs[1]["entry"] == before[1]["entry"] and "fill_seen" not in st.legs[1]
    assert st.legs[2]["fill_seen"]                         # the unshared wing IS adopted


def test_the_fill_and_the_shortfall_survive_a_restart():
    st, ctx = setup(mark_basis="exit")
    tick(st, ctx, ENTRY)
    mark_all(st, ctx, 1.0, spread=0.5)
    tick(st, ctx, LATER)
    state = st.export_state()
    st2 = MonthlyButterflyStrategy(universe=["NIFTY"], sets=1, margin_per_set=70_000,
                                   profit_target_pct=1, mark_basis="exit")
    st2.load_state(state)
    assert st2.entry_shortfall == pytest.approx(st.entry_shortfall)
    assert [lg["entry_fill"] for lg in st2.legs] == [lg["entry_fill"] for lg in st.legs]
    assert all(lg["fill_seen"] for lg in st2.legs)        # not adopted twice after recovery


def test_the_snapshot_reads_both_bases():
    """``strategy_pnl`` is what the strategy ACTS on; ``strategy_pnl_ltp`` is the historical
    number beside it. Under "exit" they differ by the spread an exit would pay."""
    st, ctx = setup(mark_basis="exit")
    tick(st, ctx, ENTRY)
    mark_all(st, ctx, 0.5, spread=10.0)
    tick(st, ctx, LATER)
    closes = dict(ctx.market.prices)
    acts, ltp = st.strategy_pnl(closes), st.strategy_pnl_ltp(closes)
    assert ltp - acts == pytest.approx(10.0 * units(st))   # entries are fills on both
    assert acts == pytest.approx(st._pnl_pair[1])


def test_an_unknown_basis_raises():
    with pytest.raises(ValueError):
        MonthlyButterflyStrategy(universe=["NIFTY"], mark_basis="mid")


def test_mark_basis_is_forwarded_through_every_explicit_subclass():
    """The margin_per_set lesson: an explicit signature ending in **_ignored swallows a base
    knob it does not forward, and the deploy then reports "exit" while trading on LTP."""
    from skas_algo.strategies.delta_neutral_monthly import DeltaNeutralMonthlyStrategy
    from skas_algo.strategies.double_diagonal_calendar import DoubleDiagonalCalendarStrategy
    from skas_algo.strategies.fair_value_calendar import FairValueCalendarStrategy
    from skas_algo.strategies.iron_fly_monthly import IronFlyMonthlyStrategy
    from skas_algo.strategies.put_condor import PutCondorStrategy
    from skas_algo.strategies.volcano_calendar import VolcanoCalendarStrategy

    for cls in (DeltaNeutralMonthlyStrategy, IronFlyMonthlyStrategy, MonthlyButterflyStrategy,
                FairValueCalendarStrategy, VolcanoCalendarStrategy,
                DoubleDiagonalCalendarStrategy, PutCondorStrategy):
        s = cls(universe=["NIFTY"], underlying="NIFTY", mark_basis="exit")
        assert s.mark_basis == "exit", f"{cls.__name__} swallowed it into **_ignored"
        assert cls(universe=["NIFTY"], underlying="NIFTY").mark_basis == "ltp", cls.__name__
