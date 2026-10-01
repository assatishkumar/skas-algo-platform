"""The automatic BIDS deployment (strategies/bids.py, owner 2026-09-29): the tab's ladder,
bought — joining, levels, one decision a day, the cash float funded from the fund ETF (T+1),
a queued level retried and cancelled when the dip is gone, the per-holding rules from the
tab (enabled, knobs, the ladder carried across), and the portfolio write-back."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from skas_algo.db.base import session_scope
from skas_algo.db.models import (
    PortfolioBidsRule,
    PortfolioBidsSuggestion,
    PortfolioHolding,
    PortfolioSetting,
    PortfolioTransaction,
)
from skas_algo.engine.types import SignalAction
from skas_algo.services import bids
from skas_algo.strategies.bids import BidsStrategy

FUND = "LIQUIDCASE"
D1 = date(2026, 9, 28)   # a Monday


class Ctx:
    """Prices, a book that fills every signal at the close, and a cash balance."""

    def __init__(self, cash: float, today: date = D1):
        self.cash = cash
        self.closes: dict[str, float] = {}
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

    def today(self):
        return self._today

    def add_lot(self, sym, units, price, adopted_by=None):
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
            elif s.action is SignalAction.EXIT:
                assert s.lot_id is not None, "an EXIT with no lot_id is a silent no-op"
                lot = next(x for x in self.positions[s.symbol] if x.id == s.lot_id)
                lot.units -= s.quantity
                self.cash += s.quantity * px
                if lot.units <= 0:
                    self.positions[s.symbol].remove(lot)

    def next_day(self, days: int = 1):
        self._today += timedelta(days=days)


def _run(cash=15_000.0, fund_units=1000, **kw):
    """A run deployed like the owner's: a ₹15k float (3 × ₹5,000) in cash, the fund adopted."""
    s = BidsStrategy(universe=["HDFCMOMENT", "BANKBEES", FUND], watchlist="HDFCMOMENT,BANKBEES",
                     **kw)
    ctx = Ctx(cash)
    ctx.closes = {"HDFCMOMENT": 30.0, "BANKBEES": 500.0, FUND: 100.0}
    ctx.add_lot(FUND, fund_units, 100.0, adopted_by=s)
    return s, ctx


def _buys(signals):
    return [(x.symbol, x.quantity) for x in signals if x.action is SignalAction.ENTER_LONG
            and x.symbol != FUND]


def test_a_holding_joins_at_its_first_price_and_the_next_dip_buys_from_the_float():
    s, ctx = _run()
    assert _buys(s.on_slice(ctx)) == []                      # both join today
    assert s.ladders["HDFCMOMENT"] == {"peak": 30.0, "levels_fired": 0,
                                       "peak_source": "joined", "peak_asof": "2026-09-28"}
    assert s.on_slice(ctx) == []                              # one decision a day
    ctx.next_day()
    ctx.closes["HDFCMOMENT"] = 29.40                          # −2.0%: L1
    sig = s.on_slice(ctx)
    assert _buys(sig) == [("HDFCMOMENT", 170)]                # ₹5,000 // 29.40
    # the float is refilled from the fund, and the sale comes BEFORE the buy
    assert [x.symbol for x in sig][0] == FUND and sig[0].action is SignalAction.EXIT
    assert s.pending_credits and s.pending_credits[0][0] == "2026-09-30"   # T+1
    assert s.last_fired == [{"symbol": "HDFCMOMENT", "levels": [1], "amount": 5000.0,
                             "price": 29.40, "day": "2026-09-29"}]


def test_a_gap_through_two_levels_is_one_buy_of_both_amounts():
    s, ctx = _run()
    s.on_slice(ctx)
    ctx.next_day()
    ctx.closes["BANKBEES"] = 479.0                            # −4.2%: L1 + L2 = ₹15,000
    assert _buys(s.on_slice(ctx)) == [("BANKBEES", 31)]       # 15,000 // 479
    assert s.ladders["BANKBEES"]["levels_fired"] == 2


def test_a_level_the_float_cannot_cover_is_queued_retried_and_cancelled_when_the_dip_is_gone():
    s, ctx = _run(cash=4_000.0)
    s.on_slice(ctx)
    ctx.next_day()
    ctx.closes["HDFCMOMENT"] = 29.40
    sig = s.on_slice(ctx)
    assert _buys(sig) == [] and s.queued["HDFCMOMENT"]["amount"] == 5000.0
    assert "QUEUED BUY" in (s.strategy_alert or "") or "WAITING FOR CASH" in (s.strategy_alert or "")
    fund_sale = [x for x in sig if x.symbol == FUND]
    assert sum(x.quantity for x in fund_sale) == 50           # sold NOW for 4,998, rounded up
    ctx.apply(sig)                                            # the fund sale settles tomorrow
    ctx.next_day()
    ctx.closes["HDFCMOMENT"] = 29.60                          # still below the peak
    sig = s.on_slice(ctx)
    assert _buys(sig) == [("HDFCMOMENT", 168)]                # retried, re-sized at today's price
    assert [x for x in sig if x.symbol == FUND] == []         # funded once — never sold twice
    assert s.queued == {}
    # a second one, queued and then the price recovers to the high → cancelled, not bought
    s2, ctx2 = _run(cash=0.0, fund_units=0)
    s2.on_slice(ctx2)
    ctx2.next_day()
    ctx2.closes["HDFCMOMENT"] = 29.40
    s2.on_slice(ctx2)
    assert "HDFCMOMENT" in s2.queued
    ctx2.next_day()
    ctx2.closes["HDFCMOMENT"] = 30.10                         # a new high
    assert _buys(s2.on_slice(ctx2)) == [] and s2.queued == {}
    assert s2.ladders["HDFCMOMENT"]["peak"] == 30.10 and s2.ladders["HDFCMOMENT"]["levels_fired"] == 0


def test_the_tab_rules_decide_what_is_bought_and_carry_the_ladder_across():
    s, ctx = _run()
    s.set_bids_rules_fn(lambda syms: {
        "HDFCMOMENT": {"enabled": True, "dip_pct": 5.0, "amount": 2000.0, "max_levels": 3,
                       "peak": 31.0, "levels_fired": 0, "peak_source": "joined",
                       "peak_asof": "2026-09-25"},
        "BANKBEES": {"enabled": False},
    })
    ctx.closes["HDFCMOMENT"] = 29.40                          # −5.2% of the TAB's 31.00 peak
    ctx.closes["BANKBEES"] = 400.0                            # switched off on the tab
    assert _buys(s.on_slice(ctx)) == [("HDFCMOMENT", 68)]     # the tab's ₹2,000, not ₹5,000
    assert "BANKBEES" not in s.ladders
    # a reference high typed on the tab later restarts the ladder from it
    s.set_bids_rules_fn(lambda syms: {"HDFCMOMENT": {
        "enabled": True, "dip_pct": 5.0, "amount": 2000.0, "max_levels": 3, "peak": 35.0,
        "levels_fired": 0, "peak_source": "manual", "peak_asof": "2026-09-29"}})
    ctx.next_day()
    ctx.closes["HDFCMOMENT"] = 29.40                          # −16% of 35: L1-L3
    assert _buys(s.on_slice(ctx)) == [("HDFCMOMENT", 408)]    # (2+4+6)k = 12,000 // 29.40
    assert s.ladders["HDFCMOMENT"]["peak_source"] == "manual"


def test_a_failed_rules_read_buys_nothing_and_says_so():
    s, ctx = _run()

    def boom(_):
        raise RuntimeError("db locked")

    s.set_bids_rules_fn(boom)
    ctx.closes["HDFCMOMENT"] = 10.0
    assert _buys(s.on_slice(ctx)) == []
    assert "could not read the BIDS rules" in s.strategy_alert


def test_state_round_trips_and_the_fund_is_adoptable_but_never_bought_on_a_dip():
    s, ctx = _run()
    s.on_slice(ctx)
    assert "HDFCMOMENT" in s.adoptable_symbols() and FUND in s.adoptable_symbols()
    assert FUND not in s.ladders
    s2 = BidsStrategy(universe=["HDFCMOMENT"], watchlist="HDFCMOMENT,BANKBEES")
    s2.load_state(s.export_state())
    assert s2.ladders == s.ladders and s2.last_shop_day == s.last_shop_day
    assert s2.adopted_value == s.adopted_value


def test_the_fund_is_never_bought_however_much_cash_sits_idle():
    """Owner 2026-09-29: "LIQUIDCASE is for funding. No buying here." The first version
    parked cash above a float back into the fund — with ₹30k of capital that bought it."""
    s, ctx = _run(cash=10_00_000)
    for day in range(5):
        sig = s.on_slice(ctx)
        assert not any(x.symbol == FUND and x.action is SignalAction.ENTER_LONG for x in sig)
        ctx.apply(sig)
        ctx.next_day()
        ctx.closes["HDFCMOMENT"] *= 0.97


def test_each_buy_sells_the_fund_worth_its_cost_before_the_buy():
    s, ctx = _run()
    s.on_slice(ctx)
    ctx.next_day()
    ctx.closes["HDFCMOMENT"] = 29.40                          # L1 ₹5,000 → 170 units = 4,998
    ctx.closes["BANKBEES"] = 489.0                            # L1 ₹5,000 → 10 units = 4,890
    sig = s.on_slice(ctx)
    kinds = [(x.symbol, x.action) for x in sig]
    first_buy = next(i for i, k in enumerate(kinds) if k[1] is SignalAction.ENTER_LONG)
    assert all(k[0] == FUND and k[1] is SignalAction.EXIT for k in kinds[:first_buy])
    assert sum(x.quantity for x in sig if x.symbol == FUND) == 50 + 49
    assert sorted(_buys(sig)) == [("BANKBEES", 10), ("HDFCMOMENT", 170)]


def test_a_dry_fund_still_buys_from_cash_and_says_so():
    s, ctx = _run(fund_units=0)
    s.on_slice(ctx)
    ctx.next_day()
    ctx.closes["HDFCMOMENT"] = 29.40
    sig = s.on_slice(ctx)
    assert _buys(sig) == [("HDFCMOMENT", 170)] and [x for x in sig if x.symbol == FUND] == []
    assert "FUND DRY" in s.strategy_alert


# ------------------------------------------------------------------ portfolio side
@pytest.fixture
def _clean():
    yield
    with session_scope() as db:
        for m in (PortfolioBidsSuggestion, PortfolioBidsRule, PortfolioTransaction,
                  PortfolioHolding):
            db.execute(delete(m))
        db.execute(delete(PortfolioSetting).where(PortfolioSetting.key == bids.SETTINGS_KEY))


def _h(db, name, cls, price, account=1, units=100.0):
    h = PortfolioHolding(name=name, asset_class=cls, last_price=price, price_asof="2026-09-28",
                         sync_source="broker", sync_ref=name, broker_account_id=account,
                         units=units, invested=units * price, value=units * price,
                         buy_month="2025-01")
    db.add(h)
    db.flush()
    return h


def test_auto_rules_and_the_live_write_back(_clean):
    with session_scope() as db:
        etf = _h(db, "HDFCMOMENT", "etf", 30.0)
        _h(db, "ITC", "stk", 300.0)                 # stocks are switched off by default
        fund = _h(db, FUND, "etf", 100.0, units=500.0)
        _h(db, "OTHER", "etf", 10.0, account=2)     # another account: not this run's
        db.commit()
        bids.save_defaults(db, {"fund_source": FUND, "dip_pct": 2.0, "amount": 5000.0})
        r = bids.auto_rules(db, 1, ["HDFCMOMENT", "ITC", FUND, "OTHER"])
        assert set(r) == {"HDFCMOMENT", "ITC", FUND}
        assert r["HDFCMOMENT"]["enabled"] and r["HDFCMOMENT"]["amount"] == 5000.0
        assert not r["ITC"]["enabled"] and not r[FUND]["enabled"]
        events = [
            {"ticker": FUND, "action": "SELL", "units": 50, "price": 100.0},
            {"ticker": "HDFCMOMENT", "action": "BUY", "units": 170, "price": 29.4},
        ]
        ladders = {"HDFCMOMENT": {"peak": 30.0, "levels_fired": 1, "peak_source": "joined",
                                  "peak_asof": "2026-09-28"}}
        assert bids.record_auto(db, 1, 7, events, ladders, date(2026, 9, 29)) == {"ledger_rows": 2}
        db.commit()
        rows = db.execute(select(PortfolioTransaction).order_by(PortfolioTransaction.id)).scalars().all()
        got = [(x.holding_id, x.kind, x.units, x.note) for x in rows]
        # each holding's typed position carried in first, then the fill
        assert (fund.id, "sell", 50.0, "BIDS auto · run 7 · fund sale") in got
        assert (etf.id, "buy", 170.0, "BIDS auto · run 7 · dip buy") in got
        assert sum(1 for x in rows if x.holding_id == etf.id) == 2
        rule = db.execute(select(PortfolioBidsRule).where(
            PortfolioBidsRule.holding_id == etf.id)).scalars().one()
        assert (rule.peak, rule.levels_fired) == (30.0, 1)


def test_only_a_run_whose_orders_reach_the_broker_makes_a_holding_auto(monkeypatch):
    from skas_algo.api.routes import portfolio as route
    from skas_algo.live import manager as mgr

    def run(broker):
        return SimpleNamespace(
            config=SimpleNamespace(strategy_id="bids", broker_account_id=1,
                                   symbols=["HDFCMOMENT"]),
            order_broker=lambda: broker)

    monkeypatch.setattr(mgr.manager, "runs", {1: run("paper")})
    assert route._bids_auto_accounts() == {}
    monkeypatch.setattr(mgr.manager, "runs", {1: run("live")})
    assert route._bids_auto_accounts() == {1: {"HDFCMOMENT"}}


# ------------------------------------------------------------------ end to end
DIP = [100, 100, 99, 97.5, 96, 95, 101, 98.5, 97]


def _loader(symbol, start_date, end_date):
    import pandas as pd

    dates = pd.bdate_range(start="2024-01-01", periods=len(DIP))
    closes = DIP if symbol == "AAA" else [100.0] * len(DIP)
    df = pd.DataFrame({"date": dates, "open": closes, "high": closes, "low": closes,
                       "close": [float(c) for c in closes]})
    return df[(df["date"] >= pd.Timestamp(start_date))
              & (df["date"] <= pd.Timestamp(end_date))].reset_index(drop=True)


def test_bids_replays_through_the_real_engine():
    """On the actual runner: the capital is parked in the fund on day 1 (backtest seed),
    AAA joins at 100, each 2% level buys k × ₹5,000 from the float, the fund is sold to
    refill it, the new high at 101 resets the ladder — and the engine's cash never goes
    negative (the whole point of the T+1 float)."""
    import pandas as pd

    from skas_algo.engine.runner import BacktestRunner

    st = BidsStrategy(universe=["AAA", FUND], initial_capital=100_000, dip_pct=2.0,
                      amount=5000.0, max_levels=5, fund_source=FUND, float_parts=3,
                      fund_seed="if_empty")
    runner = BacktestRunner(strategy=st, universe=["AAA", FUND], loader=_loader,
                            initial_capital=100_000, lookback=1, tax_rate=0.0)
    end = pd.bdate_range("2024-01-01", periods=len(DIP))[-1].date()
    result = runner.run(date(2024, 1, 1), end)
    tx = result.transactions
    aaa = [(t["date"], t["units"], t["price"]) for t in tx
           if t["ticker"] == "AAA" and t["action"] in ("BUY", "AVG_BUY")]
    # day 1 has no bars, day 2 seeds the fund (a seed decision does nothing else), so AAA
    # joins at 99 on day 3: 97.5 is −1.5% (nothing); 96 → L1 ₹5,000 (L1 97.02); 95 → L2
    # ₹10,000 (L2 95.04); 101 resets; 98.5 (−2.5% of 101) → L1 again ₹5,000
    assert [(u, p) for _, u, p in aaa] == [(52, 96.0), (105, 95.0), (50, 98.5)]
    # every buy sold the fund worth its cost the same day; the fund was bought only by the
    # day-1 backtest seed, never afterwards
    fund_sells = [(t["date"], t["units"]) for t in tx if t["ticker"] == FUND and t["action"] == "SELL"]
    assert [u for _, u in fund_sells] == [50, 100, 50]
    assert [d for d, _ in fund_sells] == [d for d, _, _ in aaa]
    fund_buys = [t for t in tx if t["ticker"] == FUND and t["action"] in ("BUY", "AVG_BUY")]
    assert len(fund_buys) == 1
    cash = [float(h["cash"]) for h in result.history if "cash" in h]
    assert cash and min(cash) >= 0
    assert st.ladders["AAA"]["peak"] == 101.0 and st.ladders["AAA"]["levels_fired"] == 1


def test_a_live_quote_is_enough_no_cached_history_needed():
    """2026-09-29, run 38: the VPS cache had no daily bars for the ETFs, so the live view's
    present_symbols() (quote AND cached history) listed none of them and the 15:05 decision
    bought nothing, alerting "no price" beside live prices. BIDS needs today's price only."""
    s, ctx = _run()
    ctx.present_symbols = lambda: [FUND]          # what the live view said: history-gated
    s.on_slice(ctx)
    assert set(s.ladders) == {"HDFCMOMENT", "BANKBEES"}      # both priced, both joined
    assert "no live price" not in (s.strategy_alert or "")
    ctx.next_day()
    ctx.closes["HDFCMOMENT"] = 29.40
    assert _buys(s.on_slice(ctx)) == [("HDFCMOMENT", 170)]


def test_the_waiting_banner_says_what_happens_next_not_what_happened_yesterday():
    """2026-10-01 09:56: the banner still read yesterday's "WAITING FOR CASH … retried then"
    while the sale had settled and the buy was due at 15:05. The queue line is derived on
    read from the queue, the settlement day and whether today's decision has run."""
    s, ctx = _run(cash=4_000.0)
    s.on_slice(ctx)
    ctx.next_day()                                            # Tue 29 Sept
    ctx.closes["HDFCMOMENT"] = 29.40
    s.on_slice(ctx)
    assert "HDFCMOMENT" in s.queued
    assert "settles 30 Sep" in s._queue_note(date(2026, 9, 29))          # same day
    note = s._queue_note(date(2026, 9, 30))                               # settled, not yet decided
    assert note.startswith("QUEUED BUY") and "bought at today's 15:05" in note
    s.last_shop_day = "2026-09-30"
    short = s._queue_note(date(2026, 9, 30))
    assert short.startswith("WAITING FOR CASH") and "did not cover it at today's 15:05" in short
    # the stored part never carries the queue line, so it can't go stale across a restart
    stored = s.export_state()["strategy_alert"] or ""
    assert "WAITING" not in stored and "QUEUED" not in stored
    s.queued = {}
    assert s._queue_note(date(2026, 9, 30)) is None
