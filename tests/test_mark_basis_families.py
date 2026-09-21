"""mark_basis everywhere (owner decision 2026-09-21): every option family's %-target/stop
reads the price an EXIT would get — a long at the bid, a short at the ask — against the
book's REAL fill, by default. `MarkBasisMixin` (`_options_common`) is the one
implementation; each family calls it at its threshold. Fail-open: no two-sided book →
the LTP, so backtests and replays decide exactly as before (the family suites pin that).

Run 209, 2026-09-18: an iron fly on SENSEX booked a "target" on prints an hour old that
the exit realised as −₹27,280. Under "exit" the same book reads the ask on the short and
never fires."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from skas_algo.strategies._options_common import MarkBasisMixin

CE = "NIFTY|2026-10-29|24000|CE"
PE = "NIFTY|2026-10-29|24000|PE"


class Market:
    def __init__(self, prices, spread=None):
        self.prices = dict(prices)
        self.spread = spread            # half-width either side of LTP; None = no book

    def _bid_ask(self, symbol):
        if self.spread is None or symbol not in self.prices:
            return None
        ltp = self.prices[symbol]
        return (ltp - self.spread, ltp + self.spread)

    def has_print(self, s):
        return s in self.prices

    def index_spot(self, _u):
        return 24000.0


class Ctx:
    def __init__(self, market, book=None):
        self.market = market
        self.book = book or {}          # symbol -> [lot objects]
        self._now = datetime(2026, 10, 1, 10, 30)

    def lots(self, s):
        return self.book.get(s, [])

    def close(self, s):
        if s in self.market.prices:
            return self.market.prices[s]
        raise KeyError(s)

    def now(self):
        return self._now

    def today(self):
        return self._now.date()


def lot(units, price):
    return SimpleNamespace(units=units, price=price)


class Toy(MarkBasisMixin):
    strategy_id = "toy"

    def __init__(self, mark_basis="exit"):
        self._init_mark_basis(mark_basis)


def test_the_exit_price_is_the_side_an_exit_crosses_and_fails_open_without_a_book():
    t = Toy()
    ctx = Ctx(Market({CE: 100.0}, spread=2.0))
    assert t._exit_price(ctx, CE, +1, 100.0) == 98.0        # a long sells into the bid
    assert t._exit_price(ctx, CE, -1, 100.0) == 102.0       # a short buys back at the ask
    assert t._exit_price(Ctx(Market({CE: 100.0})), CE, -1, 100.0) == 100.0   # no book: LTP
    assert t._exit_price(Ctx(SimpleNamespace()), CE, -1, 100.0) == 100.0    # no market
    # a one-sided / zero quote falls back too
    ctx.market.prices[PE] = 50.0
    ctx.market._bid_ask = lambda s: (0, None)
    assert t._exit_price(ctx, PE, +1, 50.0) == 50.0 and t._exit_price(ctx, PE, -1, 50.0) == 50.0
    # "ltp" only watches: the acting mark is the LTP whatever the book says
    assert Toy("ltp")._acting_mark(Ctx(Market({CE: 100.0}, spread=2.0)), CE, -1, 100.0) == 100.0
    with pytest.raises(ValueError):
        Toy("mid")


def test_a_leg_adopts_its_real_fill_once_and_only_from_real_lots():
    t = Toy()
    leg = {"symbol": CE, "dir": -1, "units": 65.0, "entry": 100.0}
    ctx = Ctx(Market({CE: 100.0}, spread=2.0), book={CE: [lot(65, 98.0)]})   # a short filled 2 below
    px = t._leg_mark(ctx, leg, 100.0)
    assert leg["entry"] == 98.0 and leg["entry_ltp"] == 100.0 and leg["entry_fill"] == 98.0
    assert leg["fill_seen"] and t.entry_shortfall == pytest.approx(130.0)   # 2 × 65 worse
    assert px == 102.0 and t._marks_for({CE: 100.0}) == {CE: 102.0}
    # a second look never re-adopts, even if the book changed
    ctx.book[CE] = [lot(65, 1.0)]
    t._leg_mark(ctx, leg, 100.0)
    assert leg["entry"] == 98.0
    # the replay hands back an int for lots → nothing to adopt, entry untouched
    leg2 = {"symbol": PE, "dir": 1, "units": 65.0, "entry": 50.0}
    t._leg_mark(Ctx(Market({PE: 50.0}), book={PE: 1}), leg2, 50.0)
    assert leg2["entry"] == 50.0 and "fill_seen" not in leg2
    # under "ltp" the fill is recorded, the entry stays the decision
    u = Toy("ltp")
    leg3 = {"symbol": CE, "dir": -1, "units": 65.0, "entry": 100.0}
    u._leg_mark(ctx, leg3, 100.0)
    assert leg3["entry"] == 100.0 and leg3["entry_fill"] == 1.0 and leg3["fill_seen"]


# --------------------------------------------------------------------- the families
def _live_ctx(prices, spread, book):
    return Ctx(Market(prices, spread=spread), book=book)


def test_the_ratio_family_reads_the_ask_on_its_shorts_so_a_thin_target_does_not_fire():
    """Run 209's shape on the ratio base: LTP says +target, the ask says not yet."""
    from skas_algo.strategies.call_ratio_monthly import CallRatioMonthlyStrategy

    st = CallRatioMonthlyStrategy(universe=["NIFTY"], initial_capital=1_000_000, lots=1,
                                  profit_target_pct=0.10, stop_loss_pct=0.0, sizing="fixed",
                                  profit_check="tick")
    st.legs = [{"symbol": CE, "dir": -1, "units": 65.0, "entry": 100.0}]
    st._frozen_margin = 10_000.0                       # the broker's frozen base → target ₹1,000
    ctx = _live_ctx({CE: 100.0}, spread=None, book={CE: [lot(65, 100.0)]})
    ctx._now = datetime(2026, 10, 1, 10, 30)
    target = 0.10 * st._risk_base(ctx)
    # the print says +5% past the target; the ask sits far enough above to read only half
    ltp = 100.0 - 1.05 * target / 65.0
    ctx.market.prices[CE] = ltp
    ctx.market.spread = 0.5 * target / 65.0
    assert st._manage(ctx) == [] and st.legs
    assert st.strategy_pnl({CE: ltp}) == pytest.approx(0.55 * target, rel=1e-6)   # the snapshot reads the same
    # the same book on "ltp" fires on the print
    lt = CallRatioMonthlyStrategy(universe=["NIFTY"], initial_capital=1_000_000, lots=1,
                                  profit_target_pct=0.10, stop_loss_pct=0.0, sizing="fixed",
                                  profit_check="tick",
                                  mark_basis="ltp")
    lt.legs = [{"symbol": CE, "dir": -1, "units": 65.0, "entry": 100.0}]
    lt._frozen_margin = 10_000.0
    out = lt._manage(ctx)
    assert out and out[0].reason == "target"


def test_intraday_straddle_books_a_leg_and_stops_on_exit_prices():
    from tests.test_intraday_straddle import _fill, setup, tick

    st, ctx = setup(leg_book_pct=30, stop_loss_pct=2.0)
    tick(st, ctx, datetime(2026, 7, 13, 9, 18))
    assert len(st.legs) == 2
    _fill(st, ctx, 100_000.0)
    # the family's fake market has no book: give it one, ±1 around the print
    ctx.market._bid_ask = lambda sym: (ctx.market.prices[sym] - 1.0, ctx.market.prices[sym] + 1.0)
    # each short's LTP melts exactly 30% → on LTP the leg books; the ask sits 1 above → not yet
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = leg["entry"] * 0.70
    assert tick(st, ctx, datetime(2026, 7, 13, 10, 0)) == [] and len(st.legs) == 2
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = leg["entry"] * 0.70 - 1.0   # the ask is at 70% now
    out = tick(st, ctx, datetime(2026, 7, 13, 10, 1))
    assert out and all(s.reason == "leg_book" for s in out)


def test_custom_options_and_donchian_price_by_side():
    """The parallel-dict families: a sell leg is marked at the ask, a buy leg at the bid."""
    from skas_algo.strategies.custom_options import CustomOptionsStrategy

    st = CustomOptionsStrategy(universe=["NIFTY"], initial_capital=1_000_000, underlying="NIFTY",
                               expiry="2026-10-29", legs=[], target_pct=0.10)
    st.legs = [CE, PE]
    st.entry_close = {CE: 100.0, PE: 50.0}
    st.units = {CE: 65, PE: 65}
    st.leg_side = {CE: "sell", PE: "buy"}
    st.leg_index = {CE: 0, PE: 1}
    ctx = _live_ctx({CE: 85.0, PE: 50.0}, spread=3.0, book={CE: [lot(65, 100.0)], PE: [lot(65, 50.0)]})
    ctx._now = datetime(2026, 10, 1, 10, 30)
    # net entry credit 50/unit; on LTP the position is +15/unit = 30% ≥ 10% → target fires;
    # on exit prices the short costs 88 to close and the long fetches 47: +9/unit = 18%
    # — still over 10%, so it fires either way; widen the spread and it does not
    ctx.market.spread = 12.0                        # ask 97, bid 38: +3/unit = 6% < 10%
    assert st._manage(ctx, chain=None, today=ctx.today()) == []
    ctx.market.spread = 3.0
    out = st._manage(ctx, chain=None, today=ctx.today())
    assert out and out[0].reason == "target"


def test_every_family_takes_the_flag_and_defaults_to_exit():
    from skas_algo.strategies.registry import get_strategy

    kw = {"universe": ["NIFTY"], "initial_capital": 1e6}
    extra = {"custom_options": {"underlying": "NIFTY", "legs": [], "expiry": "2026-10-29"},
             "donchian_strangle_monthly": {"leg_defs": [], "expiry": "2026-10-29"}}
    for sid in ("call_ratio_monthly", "put_ratio_monthly", "batman_ratio_monthly", "hni_weekly",
                "intraday_straddle", "weekly_intraday_straddle", "call_put_ratio_expiry",
                "intraday_strangle_combo", "custom_options", "supertrend_spread",
                "donchian_strangle_monthly", "delta_neutral_monthly", "iron_fly_monthly",
                "monthly_butterfly", "fair_value_calendar", "volcano_calendar",
                "double_diagonal_calendar", "put_condor"):
        cls = get_strategy(sid)
        assert cls(**kw, **extra.get(sid, {})).mark_basis == "exit", sid
        assert cls(mark_basis="ltp", **kw, **extra.get(sid, {})).mark_basis == "ltp", sid
