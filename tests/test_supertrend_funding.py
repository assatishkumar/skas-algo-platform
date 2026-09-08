"""supertrend_momentum entry funding (EntryFundingMixin, owner ask 2026-09-08).

``ledger`` must be the old strategy byte for byte; ``on_demand`` queues, tells the owner the
rupees and retries while the signal holds; ``park`` sells the fund-source ETF for what
tomorrow needs and buys from SETTLED cash, keeping a float so a signal fills the day it
fires. Fake ctx, no engine — plus one end-to-end BacktestRunner pass for the park mode."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from skas_algo.engine.types import SignalAction
from skas_algo.strategies.supertrend_momentum import SuperTrendMomentumStrategy

ETF = "LIQUIDCASE"
D1 = date(2026, 9, 7)   # a Monday


class Ctx:
    """Just enough of AlgoContext for the strategy: prices, SuperTrend directions, a book
    that fills every signal at the close and a cash balance — the engine's job, faked."""

    def __init__(self, cash: float, today: date = D1):
        self.cash = cash
        self.closes: dict[str, float] = {}
        self.dirs: dict[str, float] = {}
        self.positions: dict[str, list] = {}
        self._today = today
        self._next_id = 1

    def present_symbols(self):
        return list(self.closes)

    def lot_symbols(self):
        return [s for s, lots in self.positions.items() if lots]

    def lots(self, sym):
        return list(self.positions.get(sym, []))

    def close(self, sym):
        if sym not in self.closes:
            raise KeyError(sym)
        return self.closes[sym]

    def supertrend_dir(self, sym):
        return self.dirs.get(sym)

    def equity(self):
        return self.cash + sum(lot.units * self.closes.get(s, lot.price)
                               for s, lots in self.positions.items() for lot in lots)

    def today(self):
        return self._today

    # ---- the "engine": fill in order, credit/debit cash synchronously ----
    def add_lot(self, sym, units, price, adopted_by=None):
        """A lot on the book. ``adopted_by`` = the strategy told, as LiveSession does when
        it adopts a broker holding (no cash moves for it)."""
        lot = SimpleNamespace(id=self._next_id, units=int(units), price=float(price), direction=1)
        self._next_id += 1
        self.positions.setdefault(sym, []).append(lot)
        if adopted_by is not None:
            adopted_by.on_fund_adopted(sym, units, price)
        return lot

    def apply(self, signals):
        for s in signals:
            px = self.closes[s.symbol]
            if s.action is SignalAction.ENTER_LONG:
                self.add_lot(s.symbol, s.quantity, px)
                self.cash -= s.quantity * px
            elif s.action is SignalAction.EXIT_ALL:
                for lot in self.positions.pop(s.symbol, []):
                    self.cash += lot.units * px
            elif s.action is SignalAction.EXIT:
                assert s.lot_id is not None, "an EXIT with no lot_id is a silent no-op"
                lots = self.positions.get(s.symbol, [])
                lot = next(x for x in lots if x.id == s.lot_id)
                take = min(s.quantity, lot.units)
                lot.units -= take
                self.cash += take * px
                if lot.units == 0:
                    lots.remove(lot)
        assert self.cash > -1e-6, f"the engine's cash went negative: {self.cash}"
        return signals

    def next_day(self, n=1):
        self._today = self._today + timedelta(days=n)


def strat(**kw):
    kw.setdefault("initial_capital", 1_000_000)
    kw.setdefault("capital_parts", 10)
    return SuperTrendMomentumStrategy(universe=["AAA", "BBB"], **kw)


def tick(st, ctx, closes, dirs):
    ctx.closes.update(closes)
    ctx.dirs.update(dirs)
    return ctx.apply(st.on_slice(ctx))


def kinds(sigs):
    return [(s.symbol, s.action.name, (s.meta or {}).get("tag")) for s in sigs]


# ------------------------------------------------------------------ ledger (unchanged)


def test_ledger_mode_is_the_old_strategy():
    st = strat()
    assert not st.funds_managed and st.funding_rules() == []
    ctx = Ctx(cash=1_000_000)
    tick(st, ctx, {"AAA": 100.0}, {"AAA": -1})
    sigs = tick(st, ctx, {"AAA": 100.0}, {"AAA": 1})          # green flip
    assert kinds(sigs) == [("AAA", "ENTER_LONG", None)] and sigs[0].quantity == 1_000
    assert st.pending_entries == {} and st.settled_cash is None   # the ledger never engaged
    # a second name the ledger cannot cover is skipped, never queued (the old rule)
    ctx.cash = 50_000
    tick(st, ctx, {"BBB": 100.0}, {"BBB": -1})
    assert tick(st, ctx, {"BBB": 100.0}, {"BBB": 1}) == []
    assert st.pending_entries == {}


# ------------------------------------------------------------------ on demand


def test_on_demand_queues_tells_the_owner_and_retries_until_the_account_can_pay():
    st = strat(funding="on_demand")
    pushes = []
    st.set_notify_fn(lambda u, m: pushes.append(m))
    ctx = Ctx(cash=1_000_000)                # the deploy's notional capital…
    st.set_broker_funds(20_000)              # …against what the account really holds
    tick(st, ctx, {"AAA": 100.0}, {"AAA": -1})
    sigs = tick(st, ctx, {"AAA": 100.0}, {"AAA": 1})
    assert sigs == []                                              # nothing the broker would reject
    assert st.pending_entries["AAA"]["cost"] == 100_000
    assert len(pushes) == 1 and "Add ₹80,000" in pushes[0] and "AAA" in pushes[0]
    assert "WAITING FOR FUNDS" in st.strategy_alert and "short ₹80,000" in st.strategy_alert
    # the next day, still green, still unfunded: retried, one more push (a new day)
    ctx.next_day()
    assert tick(st, ctx, {"AAA": 102.0}, {"AAA": 1}) == []
    assert len(pushes) == 2 and st.pending_entries["AAA"]["since"] == D1.isoformat()
    # …and no second push the same day
    assert tick(st, ctx, {"AAA": 102.0}, {"AAA": 1}) == [] and len(pushes) == 2
    # the owner adds money → the queued buy fires, sized at TODAY's price
    ctx.next_day()
    st.set_broker_funds(150_000)
    sigs = tick(st, ctx, {"AAA": 104.0}, {"AAA": 1})
    assert kinds(sigs) == [("AAA", "ENTER_LONG", None)] and sigs[0].quantity == 100_000 // 104
    assert st.pending_entries == {} and st.strategy_alert is None


def test_on_demand_cancels_a_queued_buy_when_the_signal_dies():
    st = strat(funding="on_demand")
    pushes = []
    st.set_notify_fn(lambda u, m: pushes.append(m))
    ctx = Ctx(cash=1_000_000)
    st.set_broker_funds(0)
    tick(st, ctx, {"AAA": 100.0}, {"AAA": -1})
    tick(st, ctx, {"AAA": 100.0}, {"AAA": 1})
    assert "AAA" in st.pending_entries
    ctx.next_day()
    st.set_broker_funds(500_000)                                   # money arrived too late
    assert tick(st, ctx, {"AAA": 90.0}, {"AAA": -1}) == []
    assert st.pending_entries == {} and any("cancelled" in m for m in pushes)


def test_on_demand_never_places_an_order_the_broker_would_reject():
    """The rule this exists for: the live rail halts on an unfilled entry, so the strategy
    must not emit a buy the balance cannot cover — even when its own ledger says it can."""
    st = strat(funding="on_demand")
    ctx = Ctx(cash=1_000_000)
    tick(st, ctx, {"AAA": 100.0, "BBB": 200.0}, {"AAA": -1, "BBB": -1})
    st.set_broker_funds(120_000)                                   # one part, not two
    sigs = tick(st, ctx, {"AAA": 100.0, "BBB": 200.0}, {"AAA": 1, "BBB": 1})
    assert len(sigs) == 1 and sum(s.quantity * ctx.closes[s.symbol] for s in sigs) <= 120_000
    assert len(st.pending_entries) == 1


# ------------------------------------------------------------------ park


def park(**kw):
    kw.setdefault("funding", "park")
    kw.setdefault("fund_source", ETF)
    kw.setdefault("float_parts", 1)
    kw.setdefault("funding_buffer_pct", 5)
    return strat(**kw)


def test_park_buys_from_the_float_and_sells_the_etf_for_tomorrow():
    st = park()
    ctx = Ctx(cash=1_000_000)                # deploy capital; ₹9L of it is the ETF below
    ctx.add_lot(ETF, 4_000, 100.0, st)       # ₹4L parked (adopted: no cash moved)
    ctx.add_lot(ETF, 5_000, 100.0, st)       # …in two lots (FIFO matters below)
    tick(st, ctx, {"AAA": 100.0, ETF: 100.0}, {"AAA": -1, ETF: 1})
    sigs = tick(st, ctx, {"AAA": 100.0, ETF: 100.0}, {"AAA": 1, ETF: 1})
    # the ETF sale goes BEFORE the buy; the buy is funded from the float, not the sale
    assert kinds(sigs) == [(ETF, "EXIT", "FUND"), ("AAA", "ENTER_LONG", None)]
    assert sigs[0].lot_id == 1 and sigs[0].quantity == 1_000          # refill the ₹1L float
    assert sigs[1].quantity == 1_000
    # the proceeds are NOT spendable today: they land on the next trading day
    assert st.pending_credits == [[(D1 + timedelta(days=1)).isoformat(), 100_000.0]]
    assert st._spendable == pytest.approx(0.0)                    # the float is spent
    # …and the ETF was never bought as a stock, even though its SuperTrend is green
    assert not any(s.symbol == ETF and s.action is SignalAction.ENTER_LONG for s in sigs)
    # next day: settled; a second signal fills from it and the float is raised again
    ctx.next_day()
    sigs = tick(st, ctx, {"AAA": 101.0, "BBB": 50.0, ETF: 100.0},
                {"AAA": 1, "BBB": 1, ETF: 1})
    assert tick(st, ctx, {"BBB": 50.0}, {"BBB": -1}) == []        # arm a flip for BBB
    ctx.next_day()
    sigs = tick(st, ctx, {"BBB": 50.0}, {"BBB": 1})
    assert [k for k in kinds(sigs) if k[1] == "ENTER_LONG"] == [("BBB", "ENTER_LONG", None)]
    assert sigs[-1].quantity == 2_000
    assert all(s.lot_id is not None for s in sigs if s.action is SignalAction.EXIT)


def test_park_queues_the_second_signal_of_the_day_and_fills_it_when_the_sale_settles():
    st = park()
    ctx = Ctx(cash=1_000_000)
    ctx.add_lot(ETF, 9_000, 100.0, st)
    tick(st, ctx, {"AAA": 100.0, "BBB": 100.0, ETF: 100.0}, {"AAA": -1, "BBB": -1})
    sigs = tick(st, ctx, {"AAA": 100.0, "BBB": 100.0, ETF: 100.0}, {"AAA": 1, "BBB": 1})
    buys = [s for s in sigs if s.action is SignalAction.ENTER_LONG]
    assert len(buys) == 1 and "BBB" in st.pending_entries         # one part of float, two signals
    sale = next(s for s in sigs if s.symbol == ETF)
    assert sale.quantity == 2_050                                  # 1L owed × 1.05 + the 1L float
    assert "WAITING FOR FUNDS" in st.strategy_alert
    ctx.next_day()
    sigs = tick(st, ctx, {"AAA": 100.0, "BBB": 100.0, ETF: 100.0}, {"AAA": 1, "BBB": 1})
    # the queued buy fills from the settled sale; the 5% buffer that was not needed goes
    # straight back into the ETF, AFTER the buy (park-back is always last)
    assert [k for k in kinds(sigs) if k[1] == "ENTER_LONG"] == [
        ("BBB", "ENTER_LONG", None), (ETF, "ENTER_LONG", "FUND")]
    assert sigs[-1].quantity == 50
    assert st.pending_entries == {} and st.strategy_alert is None


def test_park_re_parks_a_sales_proceeds_once_they_settle():
    st = park()
    ctx = Ctx(cash=900_000)                  # ₹10L capital: ₹9L adopted ETF, the ₹1L float
    ctx.add_lot(ETF, 9_000, 100.0, st)       # spent on the AAA position below
    ctx.add_lot("AAA", 1_000, 100.0)
    st.prev_dir["AAA"] = 1
    # red flip: the exit's ₹1.2L is pending, nothing to buy, the float is intact → no ETF leg
    sigs = tick(st, ctx, {"AAA": 120.0, ETF: 100.0}, {"AAA": -1})
    assert kinds(sigs) == [("AAA", "EXIT_ALL", None)]
    assert st.pending_credits == [[(D1 + timedelta(days=1)).isoformat(), 120_000.0]]
    # tomorrow the ₹1.2L settles; the ₹1L float is restored and the ₹20k above it parked
    ctx.next_day()
    sigs = tick(st, ctx, {"AAA": 120.0, ETF: 100.0}, {"AAA": -1})
    assert kinds(sigs) == [(ETF, "ENTER_LONG", "FUND")] and sigs[0].quantity == 200
    assert st.settled_cash == pytest.approx(120_000) and st._spendable == pytest.approx(100_000)


def test_park_says_fund_dry_and_keeps_the_queue_when_the_etf_is_empty():
    st = park()
    pushes = []
    st.set_notify_fn(lambda u, m: pushes.append(m))
    ctx = Ctx(cash=0)
    tick(st, ctx, {"AAA": 100.0, ETF: 100.0}, {"AAA": -1})
    assert tick(st, ctx, {"AAA": 100.0, ETF: 100.0}, {"AAA": 1}) == []
    assert "AAA" in st.pending_entries and "FUND DRY" in st.strategy_alert
    assert any("top it up" in m for m in pushes)


def test_the_broker_balance_caps_the_spend_but_never_rewrites_the_ledger():
    st = park()
    ctx = Ctx(cash=1_000_000)
    ctx.add_lot(ETF, 9_000, 100.0, st)
    st.set_broker_funds(30_000)                                    # another run blocked the cash
    tick(st, ctx, {"AAA": 100.0, ETF: 100.0}, {"AAA": -1})
    sigs = tick(st, ctx, {"AAA": 100.0, ETF: 100.0}, {"AAA": 1})
    assert not any(s.action is SignalAction.ENTER_LONG for s in sigs)   # can't pay → queued
    assert st.settled_cash == pytest.approx(100_000)               # the ledger is untouched
    st.set_broker_funds(400_000)                                   # the block lifted
    ctx.next_day()
    sigs = tick(st, ctx, {"AAA": 100.0, ETF: 100.0}, {"AAA": 1})
    assert any(s.symbol == "AAA" and s.action is SignalAction.ENTER_LONG for s in sigs)


def test_settlement_zero_spends_a_sale_the_same_day():
    st = park(settlement_days=0)
    ctx = Ctx(cash=0)
    ctx.add_lot(ETF, 9_000, 100.0, st)
    tick(st, ctx, {"AAA": 100.0, ETF: 100.0}, {"AAA": -1})
    sigs = tick(st, ctx, {"AAA": 100.0, ETF: 100.0}, {"AAA": 1})
    assert [k[1] for k in kinds(sigs)] == ["EXIT", "ENTER_LONG"]   # sale, then the buy it funds
    assert st.pending_credits == [] and st.pending_entries == {}


def test_funding_state_survives_a_restart():
    st = park()
    ctx = Ctx(cash=1_000_000)
    ctx.add_lot(ETF, 9_000, 100.0, st)
    tick(st, ctx, {"AAA": 100.0, "BBB": 100.0, ETF: 100.0}, {"AAA": -1, "BBB": -1})
    tick(st, ctx, {"AAA": 100.0, "BBB": 100.0, ETF: 100.0}, {"AAA": 1, "BBB": 1})
    state = st.export_state()
    st2 = park()
    st2.load_state(state)
    assert st2.pending_entries == st.pending_entries
    assert st2.pending_credits == st.pending_credits
    assert st2.settled_cash == st.settled_cash and st2.strategy_alert == st.strategy_alert


def test_a_bad_funding_mode_and_a_park_without_an_etf_are_refused():
    with pytest.raises(ValueError):
        strat(funding="magic")
    with pytest.raises(ValueError):
        strat(funding="park")


def test_decision_time_and_exit_rules():
    assert SuperTrendMomentumStrategy.default_decision_time == "15:05"
    rules = park(profit_target=0.06, partial_book_pct=0.5).exit_rules()
    assert any("red" in r for r in rules) and any("LIQUIDCASE" in r for r in rules)
    assert any("on demand" in r for r in strat(funding="on_demand").exit_rules())


# ------------------------------------------------------------------ end to end


CLOSES = [100, 100, 100, 103, 107, 112, 118, 125, 126, 124, 116, 104, 92, 80]


def _loader(symbol, start_date, end_date):
    dates = pd.bdate_range(start="2024-01-01", periods=len(CLOSES))
    closes = CLOSES if symbol == "AAA" else [100.0] * len(CLOSES)
    df = pd.DataFrame({
        "date": dates,
        "open": [closes[max(0, i - 1)] for i in range(len(CLOSES))],
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": [float(c) for c in closes],
    })
    return df[(df["date"] >= pd.Timestamp(start_date))
              & (df["date"] <= pd.Timestamp(end_date))].reset_index(drop=True)


def test_park_funding_replays_through_the_real_engine():
    """The park mode on the actual runner: day 1 parks the capital above the float, the
    green flip is bought from the float, the ETF is sold (one EXIT per lot) for the next
    day, the red flip sells the stock and the proceeds go back into the ETF — and the
    engine's cash never goes negative, which is the whole point of T+1 modelling."""
    from datetime import date as _date

    from skas_algo.engine.runner import BacktestRunner

    st = SuperTrendMomentumStrategy(
        universe=["AAA", ETF], initial_capital=1_000_000, capital_parts=10,
        timeframe="daily", supertrend_period=3, supertrend_multiplier=2.0,
        profit_target=0.05, partial_book_pct=0.0,
        funding="park", fund_source=ETF, float_parts=1, settlement_days=1,
        funding_buffer_pct=5, fund_seed="if_empty",
    )
    runner = BacktestRunner(
        strategy=st, universe=["AAA", ETF], loader=_loader, initial_capital=1_000_000,
        lookback=2, tax_rate=0.0,
        supertrend={"period": 3, "multiplier": 2.0, "timeframe": "daily"},
    )
    end = pd.bdate_range("2024-01-01", periods=len(CLOSES))[-1].date()
    result = runner.run(_date(2024, 1, 1), end)
    tx = result.transactions
    etf_buys = [t for t in tx if t["ticker"] == ETF and t["action"] in ("BUY", "AVG_BUY")]
    etf_sells = [t for t in tx if t["ticker"] == ETF and t["action"] == "SELL"]
    aaa_buys = [t for t in tx if t["ticker"] == "AAA" and t["action"] in ("BUY", "AVG_BUY")]
    aaa_sells = [t for t in tx if t["ticker"] == "AAA" and t["action"] == "SELL"]
    assert etf_buys and etf_buys[0]["date"] == min(t["date"] for t in tx)   # the day-1 seed
    assert etf_buys[0]["units"] == 9_000                                   # ₹10L less the ₹1L float
    assert len(aaa_buys) == 1 and aaa_buys[0]["units"] == 100_000 // aaa_buys[0]["price"]
    assert etf_sells and etf_sells[0]["date"] <= aaa_buys[0]["date"]      # the float refill
    assert aaa_sells and aaa_sells[-1]["date"] > aaa_buys[0]["date"]
    # the red-flip proceeds settle and go back into the ETF
    assert any(t["date"] > aaa_sells[-1]["date"] for t in etf_buys)
    assert st.pending_entries == {}


def test_a_full_float_on_a_quiet_day_sells_and_parks_nothing():
    """The buffer applies to queued buys only. Applied to the float it sold 5% every quiet
    day and parked the same 5% back the next — two fills a day for nothing."""
    st = park()
    ctx = Ctx(cash=1_000_000)
    ctx.add_lot(ETF, 9_000, 100.0, st)
    assert tick(st, ctx, {"AAA": 100.0, ETF: 100.0}, {"AAA": -1}) == []
    ctx.next_day()
    assert tick(st, ctx, {"AAA": 100.0, ETF: 100.0}, {"AAA": -1}) == []


# ------------------------------------------------------------------ one account, many runs


def test_the_ledger_starts_at_capital_less_the_adopted_etf():
    """Deploy capital = the run's fund size. The ETF adopted from the broker moved no cash,
    so the ledger must start at the CASH portion of the share — the float — not at the
    whole capital, or the run reads a sibling's cash as its own."""
    st = park()
    ctx = Ctx(cash=1_000_000)                # the deploy's capital, untouched by adoption
    ctx.add_lot(ETF, 9_000, 100.0, st)       # ₹9L adopted at the broker's cost
    tick(st, ctx, {"AAA": 100.0, ETF: 100.0}, {"AAA": -1})
    assert st.settled_cash == pytest.approx(100_000)
    assert st.fund_pool(ctx) == pytest.approx(1_000_000)


def test_fund_units_wanted_is_the_gap_between_the_fund_size_and_the_pool():
    st = park(fund_size_cap=True)
    ctx = Ctx(cash=1_000_000)
    ctx.add_lot(ETF, 9_000, 100.0, st)
    # before the first decision the cash share is the float it keeps (₹1L): nothing wanted
    assert st.fund_units_wanted(ctx, 100.0) == 0
    # a run holding only 4,000 units against a ₹10L fund wants 5,000 more (+ the ₹1L float)
    st2 = park(fund_size_cap=True)
    ctx2 = Ctx(cash=1_000_000)
    ctx2.add_lot(ETF, 4_000, 100.0, st2)
    assert st2.fund_units_wanted(ctx2, 100.0) == pytest.approx(5_000)
    # money converted into a stock is still the pool → still nothing wanted
    tick(st, ctx, {"AAA": 100.0, ETF: 100.0}, {"AAA": -1})
    sigs = tick(st, ctx, {"AAA": 100.0, ETF: 100.0}, {"AAA": 1})
    assert any(s.symbol == "AAA" for s in sigs)
    assert st.fund_units_wanted(ctx, 100.0) == pytest.approx(0.0)
    # a realised LOSS opens a gap the owner's top-up may fill — replenish to the fund size
    ctx.next_day()
    tick(st, ctx, {"AAA": 80.0, ETF: 100.0}, {"AAA": -1})          # sold 1,000 @ 80: −₹20k
    assert st.fund_units_wanted(ctx, 100.0) == pytest.approx(200)
    # the cap is opt-in (separate ETFs per strategy is the normal case); modes without an
    # ETF are never capped
    assert park().fund_units_wanted(ctx, 100.0) is None
    assert strat(funding="on_demand", fund_size_cap=True).fund_units_wanted(ctx, 100.0) is None
    assert strat().fund_units_wanted(ctx, 100.0) is None


def test_two_runs_on_one_account_each_adopt_their_own_share_of_the_holding():
    """The broker holds 10,000 LIQUIDCASE for the account. A ₹4L supertrend run and a ₹6L
    value_investing run each take their share; a third strategy without a fund size takes
    whatever is missing, as adoption always did."""
    from skas_algo.live.manager import _adoptable_units
    from skas_algo.strategies.value_investing import ValueInvestingStrategy

    a = park(initial_capital=400_000, fund_size_cap=True)
    b = ValueInvestingStrategy(universe=[], fund_source=ETF, daily_budget=5_000,
                               initial_capital=600_000, fund_size_cap=True)
    c = ValueInvestingStrategy(universe=[], fund_source=ETF, daily_budget=5_000,
                               initial_capital=600_000)                 # cap off (ctor default)
    empty = Ctx(cash=0)
    assert _adoptable_units(a, empty, ETF, 10_000, 100.0) == 3_600      # ₹4L less its ₹40k float
    assert _adoptable_units(b, empty, ETF, 10_000, 100.0) == 5_950      # ₹6L less its ₹5,000 float
    assert _adoptable_units(c, empty, ETF, 10_000, 100.0) == 10_000     # no cap: takes the lot
    # a holding the strategy does not fund from is never capped by its fund size
    assert _adoptable_units(a, empty, "GOLDBEES", 10_000, 100.0) == 10_000
    # …and a run already holding its share wants nothing more
    full = Ctx(cash=0)
    full.add_lot(ETF, 3_600, 100.0, a)
    assert _adoptable_units(a, full, ETF, 6_000, 100.0) == 0


def test_the_manager_adopts_through_the_per_run_cap():
    import inspect

    from skas_algo.live.manager import LiveRun

    src = inspect.getsource(LiveRun._maybe_adopt_fund_holding)
    assert "_adoptable_units(strategy, self.session.portfolio, sym, missing, price)" in src


def test_the_ledger_is_derived_from_the_book_so_fills_and_charges_cannot_drift_it():
    """A tally kept beside the book drifts by every rupee of slippage and charge, and on
    a shared balance that drift is the other run's money. The figure is derived from the
    run's own cash each decision: book cash − adopted ETF − proceeds still settling."""
    st = park()
    ctx = Ctx(cash=1_000_000)
    ctx.add_lot(ETF, 9_000, 100.0, st)
    tick(st, ctx, {"AAA": 100.0, ETF: 100.0}, {"AAA": -1})
    ctx.cash -= 1_250.0                       # charges the strategy never saw
    tick(st, ctx, {"AAA": 100.0, ETF: 100.0}, {"AAA": -1})
    assert st.settled_cash == pytest.approx(98_750)
    st2 = park()
    st2.load_state(st.export_state())
    assert st2.adopted_value == 900_000


def test_the_session_tells_the_strategy_what_it_adopted():
    from skas_algo.engine.live import LiveSession

    st = park()
    sess = LiveSession(st, initial_capital=1_000_000.0, lookback=1)
    sess.adopt_broker_holding(D1, ETF, 9_000, 100.0)
    assert st.adopted_value == 900_000 and sess.portfolio.cash == 1_000_000.0


def test_equity_scaled_parts_grow_with_the_funds_profit_and_never_count_the_adopted_etf_twice():
    """Park mode, equity-scaled: the book carries the deploy capital as cash AND the adopted
    ETF (no cash moved), so raw equity reads ₹19L on a ₹10L fund. Sizing must use the fund:
    ₹1L a part at the start, and a ₹9,890 profit lifts every part by ₹989."""
    st = park(allocation_mode="equity_scaled")
    ctx = Ctx(cash=1_000_000)
    ctx.add_lot(ETF, 9_000, 100.0, st)
    assert st._allocation(ctx) == pytest.approx(100_000)          # not 190,000
    tick(st, ctx, {"AAA": 150.0, ETF: 100.0}, {"AAA": -1})
    sigs = tick(st, ctx, {"AAA": 150.0, ETF: 100.0}, {"AAA": 1})
    buy = next(s for s in sigs if s.symbol == "AAA")
    assert buy.quantity == 666                                     # ₹1,00,000 // 150
    ctx.next_day()
    tick(st, ctx, {"AAA": 150.0, ETF: 100.0}, {"AAA": 1})          # the refill settles
    ctx.next_day()
    tick(st, ctx, {"AAA": 165.0, ETF: 100.0}, {"AAA": -1})         # red: sold 666 @ 165 → +₹9,990
    ctx.next_day()
    tick(st, ctx, {"AAA": 165.0, ETF: 100.0}, {"AAA": -1})         # settled, excess parked
    assert st._allocation(ctx) == pytest.approx(100_999, abs=1)    # (10L + 9,990) / 10
    assert st.fund_pool(ctx) == pytest.approx(1_009_990, abs=1)
