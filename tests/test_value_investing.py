"""value_investing: the daily top-down drip, ETF funding, and the depletion warning.

Three harnesses, each for what only it can prove:
  * a real MarketView + Portfolio for one slice — real Lot ids, a real prev_close;
  * a full BacktestRunner over a synthetic loader — proves the resolver and SliceExecutor
    actually execute the multi-lot funding walk (a fake ctx can never prove that);
  * LiveMarketView directly — pins prev_close vs last_close, the confusion that would make
    every change % read 0.00 and the ranking meaningless.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from skas_algo.engine.context import AlgoContext
from skas_algo.engine.live_market import LiveMarketView
from skas_algo.engine.market import MarketView
from skas_algo.engine.portfolio import Portfolio
from skas_algo.engine.runner import BacktestRunner
from skas_algo.engine.types import SignalAction
from skas_algo.strategies.value_investing import ValueInvestingStrategy

FUND = "LIQUIDBEES"
D0, D1 = pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-02")


def _view(bars: dict[str, tuple[float, float]]) -> MarketView:
    """Two bars per symbol: (prev_close, close). lookback=1 so both bars are 'present'."""
    view = MarketView(lookback=1)
    for sym, (prev, last) in bars.items():
        view.add_symbol(sym, pd.DataFrame({"date": [D0, D1], "close": [prev, last]}))
    view.finalize()
    view.set_date(D1)
    return view


def _next_day(st, ctx, view, bars: dict[str, tuple[float, float]], n: int = 1):
    """Advance to the next trading day properly — a new DATE, not just a cleared latch.

    These tests used to fake a day by resetting `last_shop_day` while the view stayed on D1.
    That let a per-CALL pot credit masquerade as a per-day one, which is exactly how the
    double-credit bug survived to production (2026-08-28)."""
    d = pd.Timestamp(view._current) if hasattr(view, "_current") else D1
    nxt = d + pd.Timedelta(days=n)
    v = MarketView(lookback=1)
    for sym, (prev, last) in bars.items():
        v.add_symbol(sym, pd.DataFrame({"date": [d, nxt], "close": [prev, last]}))
    v.finalize()
    v.set_date(nxt)
    st.last_shop_day = None
    return AlgoContext(None, {}, ctx.portfolio, v), v


def _ctx(view: MarketView, cash: float = 0.0) -> tuple[AlgoContext, Portfolio]:
    pf = Portfolio(cash=cash)
    return AlgoContext(None, {}, pf, view), pf


def _strat(**kw) -> ValueInvestingStrategy:
    kw.setdefault("fund_source", FUND)
    kw.setdefault("daily_budget", 5_000.0)
    return ValueInvestingStrategy(universe=kw.pop("universe", []), **kw)


def _fund_lots(pf: Portfolio, *sizes: int, price: float = 100.0) -> None:
    for n in sizes:
        pf.buy(FUND, n, price, D1)
    pf.cash = 0.0  # the buys above are scaffolding, not the run's cash


# ---------------------------------------------------------------- the walk
def test_buys_one_share_top_down_and_never_wraps_around():
    """The owner's worked example: biggest faller first, one share each, stop at the bottom
    even when budget remains — no second share of the leader."""
    view = _view({"AAA": (100, 95), "BBB": (100, 96), "CCC": (100, 97),  # −5 / −4 / −3 %
                  "DDD": (100, 98), "EEE": (100, 101), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 100)
    st = _strat(watchlist="AAA,BBB,CCC,DDD,EEE", daily_budget=300.0)
    sigs = st.on_slice(ctx)

    bought = [s.symbol for s in sigs if s.action is SignalAction.ENTER_LONG]
    assert bought == ["AAA", "BBB", "CCC"]        # 95 + 96 + 97 = 288 ≤ 300
    assert all(s.quantity == 1 for s in sigs if s.action is SignalAction.ENTER_LONG)
    assert bought.count("AAA") == 1               # ₹12 left over — NO wrap-around


def test_an_unaffordable_name_is_skipped_and_the_walk_continues():
    """The owner's choice: one expensive leader must not freeze the whole day."""
    view = _view({"MRF": (100_000, 95_900), "AAA": (100, 96), "BBB": (100, 97),
                  FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 100)
    st = _strat(watchlist="MRF,AAA,BBB", daily_budget=300.0)
    bought = [s.symbol for s in st.on_slice(ctx) if s.action is SignalAction.ENTER_LONG]
    assert bought == ["AAA", "BBB"]               # MRF ranks first and is skipped, not fatal


def test_ties_break_on_watchlist_order_so_a_run_is_reproducible():
    view = _view({"AAA": (100, 99), "BBB": (100, 99), "CCC": (100, 99), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 100)
    st = _strat(watchlist="CCC,AAA,BBB", daily_budget=150.0)
    bought = [s.symbol for s in st.on_slice(ctx) if s.action is SignalAction.ENTER_LONG]
    assert bought == ["CCC"]      # identical change → the owner's own list order decides


def test_an_all_green_day_still_buys_the_weakest():
    view = _view({"AAA": (100, 103), "BBB": (100, 101), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 100)
    st = _strat(watchlist="AAA,BBB", daily_budget=500.0)
    bought = [s.symbol for s in st.on_slice(ctx) if s.action is SignalAction.ENTER_LONG]
    assert bought == ["BBB", "AAA"]               # least-up first; the drip never pauses


# ---------------------------------------------------------------- funding
def test_every_fund_exit_carries_a_lot_id():
    """A regression pin on the engine footgun: EXIT with a quantity but NO lot_id is a
    SILENT no-op (engine/overrides.py) — the sale would vanish and the buys would overdraw."""
    view = _view({"AAA": (100, 95), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 100)
    sigs = _strat(watchlist="AAA", daily_budget=500.0).on_slice(ctx)
    exits = [s for s in sigs if s.action is SignalAction.EXIT]
    assert exits and all(s.lot_id is not None and s.quantity > 0 for s in exits)


def test_funding_walks_only_as_many_lots_as_it_needs():
    """Three ETF lots of 4 units @ ₹100; a ₹600 list needs 6 units → lot1 in full, 2 from
    lot2, lot3 untouched. One signal per lot: the engine clamps a quantity to ONE lot."""
    view = _view({"AAA": (1000, 300), "BBB": (1000, 300), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 4, 4, 4)
    ids = [lot.id for lot in pf.lots(FUND)]
    sigs = _strat(watchlist="AAA,BBB", daily_budget=600.0).on_slice(ctx)
    exits = [s for s in sigs if s.action is SignalAction.EXIT]
    assert [(s.lot_id, s.quantity) for s in exits] == [(ids[0], 4), (ids[1], 2)]


def test_exits_always_precede_entries():
    """Load-bearing: the engine executes in list order and credits a sale synchronously."""
    view = _view({"AAA": (100, 95), "BBB": (100, 96), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 100)
    actions = [s.action for s in _strat(watchlist="AAA,BBB", daily_budget=500.0).on_slice(ctx)]
    assert actions == [SignalAction.EXIT, SignalAction.ENTER_LONG, SignalAction.ENTER_LONG]


def test_a_dry_fund_source_buys_NOTHING_and_alerts():
    """All-or-nothing on the day's list (owner rule) — never a half-filled day."""
    view = _view({"AAA": (100, 95), "BBB": (100, 96), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 2)                                   # ₹200 held vs a ₹191 list…
    st = _strat(watchlist="AAA,BBB", daily_budget=500.0)
    assert st.on_slice(ctx)                             # …that one fits
    ctx2, pf2 = _ctx(_view({"AAA": (100, 95), "BBB": (100, 96), FUND: (100, 100)}))
    _fund_lots(pf2, 1)                                  # ₹100 held vs a ₹191 list
    st2 = _strat(watchlist="AAA,BBB", daily_budget=500.0)
    assert st2.on_slice(ctx2) == []                     # no EXIT and no ENTER_LONG
    assert "FUND DRY" in st2.strategy_alert


def test_an_unpriced_fund_source_never_sells_blind():
    view = _view({"AAA": (100, 95)})                    # the ETF has no bars at all
    ctx, pf = _ctx(view, cash=10_000.0)
    st = _strat(watchlist="AAA")
    assert st.on_slice(ctx) == []
    assert "no price for LIQUIDBEES" in st.strategy_alert


# ---------------------------------------------------------------- warnings
def test_the_runway_warning_fires_at_exactly_two_days_and_only_once_a_day():
    sent: list[tuple[str, str]] = []
    view = _view({"AAA": (100, 95), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 3)                       # ₹300; the list costs ₹95 → ₹205 ≈ 2 × ₹100
    st = _strat(watchlist="AAA", daily_budget=100.0, warn_days_left=2)
    st.set_notify_fn(lambda u, m: sent.append((u, m)))
    st.on_slice(ctx)
    assert "covers about 2 more day(s)" in st.strategy_alert
    assert len(sent) == 1

    st.last_shop_day = None                 # a second decision the SAME day
    st.on_slice(ctx)
    assert len(sent) == 1                   # the latch holds — no push spam
    st.last_shop_day = None
    st._alerted = {}                        # a new day
    st.on_slice(ctx)
    assert len(sent) == 2


def test_a_healthy_runway_is_silent():
    view = _view({"AAA": (100, 95), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 50)                      # ₹5,000 ≈ 50 days of a ₹100 budget
    st = _strat(watchlist="AAA", daily_budget=100.0)
    st.on_slice(ctx)
    assert st.strategy_alert is None


def test_a_watchlist_name_with_no_price_is_named_in_the_alert():
    """The only layer that catches a name added by a LIVE params edit — the run's symbol
    list is edit-blocklisted, so such a name is otherwise silently never bought."""
    view = _view({"AAA": (100, 95), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 100)
    st = _strat(watchlist="AAA,ZZZ")
    st.on_slice(ctx)
    assert "ZZZ" in st.strategy_alert and "no price today" in st.strategy_alert


# ---------------------------------------------------------------- fail closed
def test_a_stale_feed_refuses_the_day():
    """The cache-quote-source trap: every name flat to 9 decimals is not a market, it is
    yesterday's closes being replayed — the ranking would be meaningless."""
    view = _view({"AAA": (100, 100), "BBB": (200, 200), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 100)
    st = _strat(watchlist="AAA,BBB", daily_budget=5_000.0)
    assert st.on_slice(ctx) == []
    assert "replaying cached closes" in st.strategy_alert


def test_one_unchanged_name_does_NOT_trip_the_stale_guard():
    view = _view({"AAA": (100, 100), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 100)
    st = _strat(watchlist="AAA", daily_budget=5_000.0)
    assert [s.symbol for s in st.on_slice(ctx) if s.action is SignalAction.ENTER_LONG] == ["AAA"]


def test_a_name_without_a_previous_close_drops_out_but_the_rest_still_trade():
    view = MarketView(lookback=1)
    view.add_symbol("AAA", pd.DataFrame({"date": [D0, D1], "close": [100, 95]}))
    view.add_symbol("NEW", pd.DataFrame({"date": [D1], "close": [50]}))   # its first bar
    view.add_symbol(FUND, pd.DataFrame({"date": [D0, D1], "close": [100, 100]}))
    view.finalize()
    view.set_date(D1)
    ctx, pf = _ctx(view)
    _fund_lots(pf, 100)
    st = _strat(watchlist="NEW,AAA", daily_budget=500.0)
    bought = [s.symbol for s in st.on_slice(ctx) if s.action is SignalAction.ENTER_LONG]
    assert bought == ["AAA"]
    assert ctx.prev_close("NEW") is None and ctx.prev_close("AAA") == 100


def test_one_shop_per_day():
    view = _view({"AAA": (100, 95), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 100)
    st = _strat(watchlist="AAA")
    assert st.on_slice(ctx)
    assert st.on_slice(ctx) == []          # a manual "Run decision" must not double-shop


def test_state_round_trip_keeps_the_latch_and_the_day():
    view = _view({"AAA": (100, 95), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 100)
    st = _strat(watchlist="AAA")
    st.on_slice(ctx)
    fresh = _strat(watchlist="AAA")
    fresh.load_state(st.export_state())
    assert fresh.last_shop_day == st.last_shop_day and fresh._alerted == st._alerted
    assert fresh.on_slice(ctx) == []       # a 15:25 restart does not re-shop


# ---------------------------------------------------------------- end to end
def _loader(bars: dict[str, list[float]]):
    dates = pd.bdate_range("2026-01-01", periods=len(next(iter(bars.values()))))

    def load(symbol, start, end):
        closes = bars.get(symbol)
        if closes is None:
            return None
        df = pd.DataFrame({"date": dates, "open": closes, "high": closes,
                           "low": closes, "close": closes})
        m = (df["date"].dt.date >= start) & (df["date"].dt.date <= end)
        return df[m]

    return load, dates


def test_end_to_end_drip_never_overdraws_and_never_sells_a_stock():
    n = 20
    bars = {
        FUND: [100.0] * n,
        "AAA": [100 + (i % 5) for i in range(n)],
        "BBB": [200 - (i % 3) for i in range(n)],
        "CCC": [50 + (i % 7) for i in range(n)],
    }
    loader, dates = _loader(bars)
    st = ValueInvestingStrategy(universe=list(bars), initial_capital=100_000,
                                fund_source=FUND, daily_budget=400.0,
                                watchlist="AAA,BBB,CCC", fund_seed="if_empty")
    res = BacktestRunner(strategy=st, universe=list(bars), loader=loader,
                         initial_capital=100_000, lookback=1, tax_rate=0.0).run(
        dates[0].date(), dates[-1].date())

    txns = res.transactions
    # Bar 1 trades nothing at all: with lookback=1 the rolling levels are NaN on a symbol's
    # first bar, so present_symbols() is empty. The ETF bootstrap is therefore the FIRST
    # transaction of the run, alone on its day, and the drip starts the day after.
    assert txns[0]["ticker"] == FUND and txns[0]["action"] == "BUY"
    seed_day = str(txns[0]["date"])[:10]
    assert [t for t in txns if str(t["date"])[:10] == seed_day] == [txns[0]]
    # the stocks are NEVER sold — the whole point of the strategy
    assert [t for t in txns if t["action"] == "SELL" and t["ticker"] != FUND] == []
    # the ETF is drawn down, the stocks only accumulate
    assert res.portfolio.units(FUND) < 1000
    for sym in ("AAA", "BBB", "CCC"):
        assert res.portfolio.units(sym) > 0
    # the shadow ledger's real job: the engine never checks cash, so this must never go under
    assert res.portfolio.cash >= 0
    # every open holding still marks (the ETF and the three stocks) — nothing was orphaned
    assert set(res.final_marks) >= {FUND, "AAA", "BBB", "CCC"}


def test_idle_cash_is_spent_before_the_fund_source_is_touched():
    """Selling the ETF while cash sits idle would be strictly worse, so cash goes first.
    Live the account's cash is ~0 and every day sells; a backtest without fund_seed spends
    its opening capital first and only then reports the fund dry — visible, not silent."""
    n = 6
    bars = {FUND: [100.0] * n, "AAA": [100 - i for i in range(n)]}
    loader, dates = _loader(bars)
    st = ValueInvestingStrategy(universe=list(bars), initial_capital=300,
                                fund_source=FUND, daily_budget=400.0, watchlist="AAA")
    res = BacktestRunner(strategy=st, universe=list(bars), loader=loader,
                         initial_capital=300, lookback=1, tax_rate=0.0).run(
        dates[0].date(), dates[-1].date())
    # every trade is a stock BUY funded from cash — the ETF is never sold (none is held)
    assert res.transactions and all(t["ticker"] == "AAA" and t["action"] in ("BUY", "AVG_BUY")
                                    for t in res.transactions)
    assert res.portfolio.cash < 100                      # cash drained
    assert "FUND DRY" in (st.strategy_alert or "")       # and then it says so, loudly


# ---------------------------------------------------------------- live twin
def test_live_prev_close_is_yesterday_not_the_live_quote():
    """If prev_close preferred the live quote (as last_close does) every change % would read
    0.00% forever and the ranking would silently rank nothing."""
    view = LiveMarketView(lookback=1)
    view.seed("AAA", [100.0, 101.0])
    view.update_quote("AAA", 105.0)
    assert view.prev_close("AAA") == 101.0      # yesterday
    assert view.last_close("AAA") == 105.0      # today — deliberately different
    view.roll_forward()
    assert view.prev_close("AAA") == 105.0      # today's print became history
    assert view.prev_close("ZZZ") is None       # unknown symbol → fail closed


def test_backtest_and_live_prev_close_agree():
    """Mode equivalence for the accessor the whole strategy ranks on."""
    bt = _view({"AAA": (100.0, 105.0)})
    live = LiveMarketView(lookback=1)
    live.seed("AAA", [100.0])
    live.update_quote("AAA", 105.0)
    assert bt.prev_close("AAA") == live.prev_close("AAA") == 100.0
    assert bt.close("AAA") == live.close("AAA") == 105.0


# ------------------------------------------------------- balanced sizing
def test_balanced_mode_skips_a_name_that_has_run_ahead_of_the_pack():
    """One share of a Rs2,300 stock and one share of a Rs27 stock are not the same bet, so
    one-share-each silently weights the book by SHARE PRICE. Balanced keeps the faller-first
    walk but waits for the laggards to catch up."""
    view = _view({"RICH": (3000, 2900), "MID": (300, 295), "CHEAP": (30, 29.5),
                  FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 1000)
    st = _strat(watchlist="RICH,MID,CHEAP", daily_budget=5_000.0, sizing="balanced")
    st.epoch_names = ["CHEAP", "MID", "RICH"]                        # an epoch already running
    st.epoch_base = {"RICH": 0.0, "MID": 0.0, "CHEAP": 0.0}
    st.invested = {"RICH": 10_000.0, "MID": 100.0, "CHEAP": 100.0}   # RICH ran ahead IN it
    bought = [s.symbol for s in st.on_slice(ctx) if s.action is SignalAction.ENTER_LONG]
    assert "RICH" not in bought and set(bought) == {"MID", "CHEAP"}


def test_balanced_mode_always_lets_an_untouched_name_buy_its_first_share():
    """Testing the POST-buy total against a near-zero average locked out every name after the
    first purchase and the strategy invested Rs159 over six years (2026-08-21). The cap judges
    what a name ALREADY owns, so a name at zero is never blocked."""
    view = _view({"RICH": (3000, 2900), "CHEAP": (30, 29.5), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 1000)
    st = _strat(watchlist="RICH,CHEAP", daily_budget=5_000.0, sizing="balanced")
    st.invested = {"CHEAP": 500.0}          # RICH has never been bought
    bought = [s.symbol for s in st.on_slice(ctx) if s.action is SignalAction.ENTER_LONG]
    assert "RICH" in bought


def test_balanced_mode_is_opt_in_and_one_share_stays_the_default():
    """CLAUDE.md §1 — the ctor keeps the spec's behaviour; the form opts in."""
    assert _strat(watchlist="AAA").sizing == "one_share"
    view = _view({"RICH": (3000, 2900), "CHEAP": (30, 29.5), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 1000)
    st = _strat(watchlist="RICH,CHEAP", daily_budget=5_000.0)
    st.invested = {"RICH": 10_000.0}
    bought = [s.symbol for s in st.on_slice(ctx) if s.action is SignalAction.ENTER_LONG]
    assert "RICH" in bought                 # unbalanced: price-weighting is not corrected


def test_the_running_book_survives_a_restart():
    """The balanced walk is only fair if it remembers what it already bought."""
    view = _view({"AAA": (100, 95), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 1000)
    st = _strat(watchlist="AAA", sizing="balanced")
    st.on_slice(ctx)
    assert st.invested.get("AAA", 0) > 0
    fresh = _strat(watchlist="AAA", sizing="balanced")
    fresh.load_state(st.export_state())
    assert fresh.invested == st.invested


def test_a_new_name_takes_its_share_of_the_FLOW_not_a_catch_up():
    """Owner rule (2026-08-21): "the ratio should be maintained". A name added to a mature
    book must NOT be bought every day until it reaches the incumbents' average — on the real
    numbers that was ~18 months of a permanent daily slot. Changing the watchlist starts a new
    balance epoch: every name re-bases to what it holds, and the ONGOING budget splits evenly."""
    view = _view({"OLD1": (300, 295), "OLD2": (300, 296), "NEW": (250, 245), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 1000)
    st = _strat(watchlist="OLD1,OLD2", daily_budget=5_000.0, sizing="balanced")
    st.epoch_names = ["OLD1", "OLD2"]
    st.epoch_base = {"OLD1": 0.0, "OLD2": 0.0}
    st.invested = {"OLD1": 100_000.0, "OLD2": 100_000.0}      # years of accumulation

    st.watchlist = ["OLD1", "OLD2", "NEW"]                     # the owner adds a name
    for day in range(4):                                       # a few sessions
        st.last_shop_day = None
        bought = [s.symbol for s in st.on_slice(ctx) if s.action is SignalAction.ENTER_LONG]
        assert set(bought) == {"OLD1", "OLD2", "NEW"}, f"day {day}: {bought}"
    # NEW is one of three, not soaking the budget: each got ~one share per session
    flow = {n: st.invested[n] - st.epoch_base[n] for n in ("OLD1", "OLD2", "NEW")}
    assert max(flow.values()) / min(flow.values()) < 1.5
    # and the incumbents' six-figure history did NOT make NEW look starved
    assert st.epoch_base["OLD1"] == 100_000.0 and st.epoch_base["NEW"] == 0.0


def test_dropping_a_name_rebases_the_survivors_too():
    view = _view({"AAA": (100, 95), "BBB": (100, 96), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 1000)
    st = _strat(watchlist="AAA,BBB", daily_budget=1_000.0, sizing="balanced")
    st.epoch_names = ["AAA", "BBB", "CCC"]
    st.epoch_base = {"AAA": 0.0, "BBB": 0.0, "CCC": 0.0}
    st.invested = {"AAA": 5_000.0, "BBB": 5_000.0, "CCC": 50_000.0}
    st.on_slice(ctx)
    assert st.epoch_names == ["AAA", "BBB"]          # CCC is gone from the balance entirely
    assert "CCC" not in st.epoch_base


def test_a_missing_quote_does_not_reset_the_balance_epoch():
    """The epoch keys on the CONFIGURED watchlist. Keyed on today's printers instead, a single
    missing quote would re-base everything and forgive every accumulated overweight — the
    balance would quietly stop working on exactly the days the data is patchy."""
    view = _view({"AAA": (100, 95), FUND: (100, 100)})       # BBB has no price today
    ctx, pf = _ctx(view)
    _fund_lots(pf, 1000)
    st = _strat(watchlist="AAA,BBB", daily_budget=1_000.0, sizing="balanced")
    st.epoch_names = ["AAA", "BBB"]
    st.epoch_base = {"AAA": 0.0, "BBB": 0.0}
    st.invested = {"AAA": 9_000.0, "BBB": 1_000.0}           # AAA is overweight in-epoch
    bought = [s.symbol for s in st.on_slice(ctx) if s.action is SignalAction.ENTER_LONG]
    assert st.epoch_base == {"AAA": 0.0, "BBB": 0.0}         # epoch intact
    assert bought == []                                      # AAA still correctly skipped


# ---------------------------------------------------- equal-value sizing
def test_equal_value_puts_the_same_RUPEES_into_every_name():
    """The fix for the 207x imbalance (run #278: TCS Rs2,85,902 vs SOUTHBANK Rs1,383 on the
    same number of buys). Each name is credited an equal slice of the budget into its own pot
    and buys whole shares when the pot can afford them, so rupees in are equal by
    construction however the share prices differ."""
    view = _view({"RICH": (3000, 2900), "CHEAP": (30, 29), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 10_000)
    st = _strat(watchlist="RICH,CHEAP", daily_budget=1_000.0, sizing="equal_value")
    bars = {"RICH": (3000, 2900), "CHEAP": (30, 29), FUND: (100, 100)}
    for _day in range(40):                    # Rs500/day each, on 40 REAL days
        ctx, view = _next_day(st, ctx, view, bars)
        st.on_slice(ctx)
    # CREDITED rupees are exactly equal — that is the guarantee.
    credited = {n: st.invested.get(n, 0) + st.pot.get(n, 0) for n in ("RICH", "CHEAP")}
    assert credited["RICH"] == credited["CHEAP"] == 20_000.0
    # INVESTED can differ only by what a whole share forces to sit idle: an expensive name
    # always has more waiting in its pot between purchases. Bounded by one share price.
    rich, cheap = st.invested["RICH"], st.invested["CHEAP"]
    assert abs(rich - cheap) < 2_900                       # < one RICH share
    assert rich > 15_000 and cheap > 15_000                # both really got ~Rs20k


def test_an_expensive_name_saves_up_instead_of_being_priced_out():
    """A Rs2,900 stock on a Rs500/day slice cannot buy daily — its pot carries until it can.
    Under one-share-each the same name would instead have hoovered up the budget."""
    view = _view({"RICH": (3000, 2900), "CHEAP": (30, 29), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 10_000)
    st = _strat(watchlist="RICH,CHEAP", daily_budget=1_000.0, sizing="equal_value")
    st.last_shop_day = None
    day1 = [s.symbol for s in st.on_slice(ctx) if s.action is SignalAction.ENTER_LONG]
    assert day1 == ["CHEAP"]                  # RICH cannot afford a share on day 1…
    assert st.pot["RICH"] == 500.0            # …so its money waits
    for _ in range(5):
        ctx, view = _next_day(st, ctx, view, {"RICH": (3000, 2900), "CHEAP": (30, 29), FUND: (100, 100)})
        st.on_slice(ctx)
    assert st.invested.get("RICH", 0) >= 2900   # …and buys once the pot is big enough


def test_equal_value_buys_multiple_shares_when_the_pot_allows():
    view = _view({"CHEAP": (30, 29), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 10_000)
    st = _strat(watchlist="CHEAP", daily_budget=1_000.0, sizing="equal_value")
    st.last_shop_day = None
    sigs = [s for s in st.on_slice(ctx) if s.action is SignalAction.ENTER_LONG]
    assert sigs[0].quantity == 34             # floor(1000 / 29) — not 1


def test_an_unknown_sizing_mode_fails_loudly():
    """It used to fall through to one_share in silence: run #279 asked for equal_value against
    a backend that predated the mode, traded price-weighted, and looked perfectly healthy. A
    sizing typo must never be a silent change of allocation."""
    import pytest

    with pytest.raises(ValueError, match="unknown sizing"):
        _strat(watchlist="AAA", sizing="equalvalue")
    for good in ("one_share", "balanced", "equal_value"):
        assert _strat(watchlist="AAA", sizing=good).sizing == good


# ------------------------------------------- decision time (owner call, 2026-08-24)

def test_value_investing_names_its_own_decision_time_of_15_05():
    """The platform default is 15:20, which since SEBI's Closing Auction Session is PAST the
    end of continuous cash trading for F&O-listed stocks — an order would rest in the auction,
    never fill, and (an unfilled entry halts) stop the run every day. This strategy is the
    first equity one that can deploy live, so it names its own."""
    from skas_algo.strategies.value_investing import ValueInvestingStrategy

    assert ValueInvestingStrategy.default_decision_time == "15:05"


def test_the_deploy_route_resolves_the_strategys_own_default(client):
    """Explicit wins; omitted asks the STRATEGY; unknown strategies keep 15:20. Resolved
    server-side so an API caller gets it too, not just the deploy form."""
    from skas_algo.api.routes.live import _resolve_decision_time

    assert _resolve_decision_time("value_investing", None) == "15:05"
    assert _resolve_decision_time("value_investing", "14:30") == "14:30"   # explicit wins
    assert _resolve_decision_time("sst_lifo", None) == "15:20"             # platform default
    assert _resolve_decision_time("no_such_strategy", None) == "15:20"     # never raises


def test_the_strategies_endpoint_publishes_the_defaults(client):
    """The deploy form reads these rather than keeping its own copy — one source of truth."""
    body = client.get("/api/v1/strategies").json()
    assert body["decision_times"]["value_investing"] == "15:05"
    assert body["decision_times"]["sst_lifo"] == "15:20"


def test_the_auction_window_warns_but_never_blocks():
    """A hard block would be wrong: a watchlist of only non-F&O names trades continuously to
    15:30, so 15:20 is legitimate there. The owner is warned and decides."""
    from skas_algo.live.quotes import auction_warning, in_closing_auction
    from datetime import time as _t

    assert in_closing_auction(_t(15, 20)) and not in_closing_auction(_t(15, 5))
    assert "F&O-LISTED" in (auction_warning("15:20") or "")
    assert auction_warning("15:05") is None
    assert auction_warning("15:20", segment="DERIV") is None   # index F&O runs to 15:40


def test_the_forms_params_are_real_ctor_knobs_not_swallowed_by_ignored():
    """The ctor ends in ``**_ignored``, so a param a FORM sends but the ctor does not define
    is accepted and silently does nothing.

    The Deploy page did exactly that until 2026-08-25: it sent the generic equity knobs
    (capital_parts / profit_target / max_lots / allocation_mode), every one of which landed
    in _ignored, so the run quietly used ctor defaults — ₹5,000/day, an empty watchlist and
    sizing="one_share", the price-weighted mode the owner had rejected — while the form
    displayed knobs that did nothing. Renaming any of these without updating both forms
    would fail exactly as quietly, so pin the names."""
    import inspect

    from skas_algo.strategies.value_investing import ValueInvestingStrategy

    sent = {"daily_budget", "fund_source", "watchlist", "warn_days_left", "sizing",
            "max_skew_pct", "fund_yield_pct", "fund_seed"}
    missing = sent - set(inspect.signature(ValueInvestingStrategy.__init__).parameters)
    assert not missing, f"forms send params the ctor does not define: {sorted(missing)}"


# ─────────────────────────────────── T+1 settlement (owner decision, 2026-08-27)

def _t1(**kw) -> ValueInvestingStrategy:
    kw.setdefault("settlement_days", 1)
    kw.setdefault("funding_buffer_pct", 10.0)
    return _strat(**kw)


def test_sale_proceeds_are_not_spendable_the_same_day():
    """The whole point. An equity CNC sale settles T+1 — Dhan confirmed it in writing after
    live run 23 halted. Day one raises the float and buys nothing; day two spends it."""
    view = _view({"AAA": (100, 95), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 200)                       # ₹20,000 of ETF, ₹0 cash
    st = _t1(watchlist="AAA", daily_budget=1_000.0)

    day1 = st.on_slice(ctx)
    assert [s.action for s in day1] == [SignalAction.EXIT], "day 1 sells only — nothing settled"
    assert st.pending_credits and st.pending_credits[0][0] == "2026-01-05"  # Fri → Mon
    assert st.settled_cash == 0.0

    # …the money lands on the settlement day, and only then does the drip buy. (A fresh view
    # because the settlement day needs its own price bar; the strategy carries the ledger.)
    d2 = pd.Timestamp("2026-01-05")
    view2 = MarketView(lookback=1)
    for sym, (prev, last) in {"AAA": (100, 95), FUND: (100, 100)}.items():
        view2.add_symbol(sym, pd.DataFrame({"date": [D1, d2], "close": [prev, last]}))
    view2.finalize()
    view2.set_date(d2)
    ctx2 = AlgoContext(None, {}, pf, view2)
    st.last_shop_day = None
    day2 = [s for s in st.on_slice(ctx2) if s.action is SignalAction.ENTER_LONG]
    assert day2 and day2[0].symbol == "AAA", "the settled money is now spendable"


def test_the_sell_is_emitted_before_every_buy():
    """Order is load-bearing for a NEW reason: a rejected BUY halts the run, and a sale queued
    behind the buys would then never fire — starving tomorrow as well as today."""
    view = _view({"AAA": (100, 95), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 200)
    st = _t1(watchlist="AAA", daily_budget=1_000.0)
    st.settled_cash = 200.0                   # enough to buy, low enough to need a top-up
    sigs = st.on_slice(ctx)
    kinds = [s.action for s in sigs]
    assert SignalAction.EXIT in kinds and SignalAction.ENTER_LONG in kinds
    assert kinds.index(SignalAction.EXIT) < kinds.index(SignalAction.ENTER_LONG)


def test_the_float_tops_up_by_the_difference_only():
    """Sell only the DIFFERENCE, so cash already settled (and sales already in flight) count
    and the float never ratchets up. Target = 1.1 x ₹1,000 = ₹1,100."""
    view = _view({"AAA": (100, 95), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 500)
    st = _t1(watchlist="AAA", daily_budget=1_000.0, sizing="one_share")
    st.settled_cash = 1_000.0                 # buys ₹95 → ₹905 left, so ₹195 short of target
    sold = [s for s in st.on_slice(ctx) if s.action is SignalAction.EXIT]
    assert sum(s.quantity for s in sold) == 2, "2 x ₹100 covers the ₹195 gap — not a whole float"

    # …and with the float already covered and nothing bought, it sells NOTHING.
    st2 = _t1(watchlist="AAA", daily_budget=1_000.0, sizing="one_share")
    st2.settled_cash = 5_000.0
    st2.pot.clear()
    view_rich = _view({"AAA": (100, 9_999), FUND: (100, 100)})   # unaffordable → no buys
    ctx_rich, pf_rich = _ctx(view_rich)
    _fund_lots(pf_rich, 500)
    assert [s for s in st2.on_slice(ctx_rich) if s.action is SignalAction.EXIT] == []


def test_a_shortfall_spends_what_settled_and_leaves_the_pot_intact():
    """Owner's call: buy down the list until the settled cash runs out. And the money for a
    buy that did NOT happen must stay in its pot — the planner used to debit pots for buys
    the caller then declined to emit, silently losing that money."""
    view = _view({"AAA": (100, 90), "BBB": (100, 92), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 500)
    st = _t1(watchlist="AAA,BBB", daily_budget=1_000.0, sizing="equal_value")
    st.settled_cash = 90.0                    # exactly one AAA share, nothing for BBB
    bought = [s.symbol for s in st.on_slice(ctx) if s.action is SignalAction.ENTER_LONG]
    assert bought == ["AAA"]
    assert st.pot["BBB"] == 500.0, "BBB never bought — its whole slice must still be there"


def test_live_reconciliation_caps_at_the_brokers_real_balance():
    """Run 23, exactly: a ₹1,00,00,000 ledger against a ₹146.03 Dhan balance. The strategy
    bought MANAPPURAM at ₹346.50 and then had KTKBANK rejected for want of ₹83.48. The broker
    is truth, and min() is the conservative side — the account may fund other runs too."""
    view = _view({"MANAPPURAM": (350, 346.5), FUND: (100, 100)})
    ctx, pf = _ctx(view, cash=10_000_000.0)
    _fund_lots(pf, 500)
    pf.cash = 10_000_000.0
    st = _t1(watchlist="MANAPPURAM", daily_budget=5_000.0)
    st.set_broker_funds(146.03)
    bought = [s for s in st.on_slice(ctx) if s.action is SignalAction.ENTER_LONG]
    assert bought == [], "₹146 cannot buy a ₹346.50 share — and must not try"
    # The cap bounds what may be SPENT; it must NOT be written back into the ledger. The
    # broker balance is shared with every other run on the account, so an options run
    # blocking margin would otherwise destroy the difference permanently (2026-08-28).
    assert st.settled_cash == 10_000_000.0, "the ledger is untouched — only the spend is capped"
    st.set_broker_funds(10_000_000.0)          # margin released
    assert st._settle(ctx, date(2026, 1, 2)) == 10_000_000.0, "and it recovers in full"


def test_settlement_days_zero_is_the_historical_behaviour():
    """CLAUDE.md §1 — a recovered deploy must be byte-identical. 0 keeps the same-day model:
    the sale funds the same tick and the buys go out with it."""
    view = _view({"AAA": (100, 95), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 200)
    st = _strat(watchlist="AAA", daily_budget=1_000.0)          # settlement_days defaults to 0
    assert st.settlement_days == 0
    sigs = st.on_slice(ctx)
    assert any(s.action is SignalAction.EXIT for s in sigs)
    assert any(s.action is SignalAction.ENTER_LONG for s in sigs)   # bought the SAME day
    assert st.pending_credits == []


def test_the_settlement_ledger_survives_a_restart():
    view = _view({"AAA": (100, 95), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 200)
    st = _t1(watchlist="AAA", daily_budget=1_000.0)
    st.on_slice(ctx)
    assert st.pending_credits
    fresh = _t1(watchlist="AAA", daily_budget=1_000.0)
    fresh.load_state(st.export_state())
    assert fresh.pending_credits == st.pending_credits
    assert fresh.settled_cash == st.settled_cash


def test_a_depleted_fund_source_warns_loudly_but_never_stops_the_run():
    """Owner's model (2026-08-27): the ETF is topped up OUTSIDE the strategy, so treat it as
    an effectively infinite source and make a real depletion VISIBLE — a banner plus a
    once-a-day push — without halting. Settled cash keeps buying whatever it can."""
    sent: list[tuple[str, str]] = []
    view = _view({"AAA": (100, 95), FUND: (100, 100)})
    ctx, pf = _ctx(view)                       # NO fund lots at all — fully depleted
    st = _t1(watchlist="AAA", daily_budget=1_000.0)
    st.set_notify_fn(lambda u, m: sent.append((u, m)))
    st.settled_cash = 500.0                    # …but cash HAS settled from an earlier sale

    sigs = st.on_slice(ctx)
    bought = [s for s in sigs if s.action is SignalAction.ENTER_LONG]
    assert bought, "settled cash must keep working even with the ETF empty"
    assert "FUND DRY" in (st.strategy_alert or ""), "the banner must say so"
    assert sent, "and it must push"
    assert any("top it up" in m.lower() for _sym, m in sent), sent
    assert not any("stops" in m for _sym, m in sent), "it does NOT stop — do not say it does"
    assert st.last_shop_day is not None, "the run carries on — no halt, no stall"


def test_units_the_broker_holds_but_the_ledger_does_not_are_adoptable():
    """The refill reaches the strategy by ADOPTION. The platform only knows lots the RUN
    created, and an EXIT without a lot_id is a silent no-op — so an ETF topped up in the
    broker was unsellable, and a live run with capital ≈ one day's budget said FUND DRY on
    day one while the account held plenty."""
    from skas_algo.engine.live import LiveSession

    sess = LiveSession(_strat(watchlist="AAA"), initial_capital=5_000.0, lookback=1)
    assert sess.portfolio.lots(FUND) == []
    cash_before = sess.portfolio.cash

    sess.adopt_broker_holding(D1, FUND, 45, 114.8)

    lots = sess.portfolio.lots(FUND)
    assert sum(lot.units for lot in lots) == 45
    assert lots[0].price == 114.8, "the broker's own cost basis, not an invented one"
    assert sess.portfolio.cash == cash_before, "no cash moves — it was spent at the broker"

    # …and the lots are now real EXIT targets, which is the entire point.
    assert all(lot.id is not None for lot in lots)


def test_a_zero_starting_capital_does_not_blow_up_the_report():
    """Legitimate for a strategy funded by SELLING a holding rather than from a cash float —
    the owner deployed live at capital 0 (2026-08-28) because any figure would have been
    fiction. Percent-of-capital is undefined then, but an unguarded divide took out the WHOLE
    report, and the Live tile, the snapshot and Analyze all build one."""
    from skas_algo.engine.metrics import compute_metrics
    from skas_algo.engine.runner import RunResult

    res = RunResult(history=[
        {"date": date(2026, 1, 1), "total_equity": 0.0, "cash": 0.0, "holdings_value": 0.0},
        {"date": date(2026, 6, 1), "total_equity": 5_000.0, "cash": 0.0,
         "holdings_value": 5_000.0},
    ])
    m = compute_metrics(res, 0.0)          # must not raise
    assert m["Total Return %"] == 0.0
    assert m["CAGR %"] == 0.0


def test_adoption_is_account_scoped_not_per_run():
    """The broker holding is per-ACCOUNT. A per-run check let TWO value_investing runs on one
    Dhan account each adopt the same 798 LIQUIDCASE units — platform +1596 vs broker +798 —
    and reconciliation, which aggregates across runs, halted them both (live, 2026-08-28).
    Adoption must count the same book reconciliation does, or the two can never agree."""
    import inspect

    from skas_algo.live.manager import LiveRun

    src = inspect.getsource(LiveRun._maybe_adopt_fund_holding)
    assert "manager.runs.values()" in src, "adoption must scan every run on the account"
    assert "broker_account_id" in src, "…and scope that scan to THIS account"
    # the per-run form that caused the double-adopt must not come back
    assert "sum(lot.units for lot in self.session.portfolio.lots(fund))" not in src


def test_an_accumulation_run_can_be_stopped_even_while_holding():
    """The stop route 409s while positions are open — sound for a strategy that manages a
    book, impossible for one that NEVER sells: run 23 could not be stopped from the UI at all
    and had to be archived out (2026-08-28). The holdings are delivery stock that stays in the
    broker account either way; stopping just ends platform management of it."""
    from skas_algo.strategies.value_investing import ValueInvestingStrategy

    assert ValueInvestingStrategy.never_sells is True

    import inspect

    from skas_algo.api.routes import live as live_routes

    src = inspect.getsource(live_routes.stop_live)
    assert 'getattr(strategy, "never_sells", False)' in src, (
        "the stop guard must exempt strategies that cannot exit by design"
    )


def test_the_broker_cap_never_destroys_ledger_cash():
    """The account is SHARED — an options run blocking margin drops `available` for everyone.
    Capping by assignment meant ₹5,500 of settled cash clamped to ₹200 stayed ₹200 after the
    margin released, because the ledger is persisted and nothing restores it. The cap is a
    ceiling on today's SPEND only."""
    st = _t1(watchlist="AAA", daily_budget=5_000.0)
    st.settled_cash = 5_500.0
    ctx, _pf = _ctx(_view({"AAA": (100, 95), FUND: (100, 100)}))

    st.set_broker_funds(200.0)
    assert st._settle(ctx, date(2026, 1, 2)) == 200.0, "today's spend is capped…"
    assert st.settled_cash == 5_500.0, "…but the ledger is NOT rewritten"

    st.set_broker_funds(5_500.0)
    assert st._settle(ctx, date(2026, 1, 2)) == 5_500.0, "and it is all still there"


def test_pots_are_credited_once_a_day_however_often_the_decision_runs():
    """A manual "Run decision", or a day that produced no signals and so never latched, used
    to add ANOTHER full day's budget to every pot. sum(pot) drives the pre-sale float target,
    so the error compounds into real ETF selling."""
    st = _t1(watchlist="AAA,BBB", daily_budget=2_000.0, sizing="equal_value")
    ranked = [(-0.05, 0, "AAA", 99_999.0), (-0.04, 1, "BBB", 99_999.0)]   # nothing affordable

    for _ in range(5):                       # five decisions, same day
        st._shopping_list(ranked, 0.0, "2026-01-02")
    assert st.pot["AAA"] == 1_000.0, "one day, one slice — not five"

    st._shopping_list(ranked, 0.0, "2026-01-05")   # a NEW day credits again
    assert st.pot["AAA"] == 2_000.0


def test_a_no_buy_day_names_the_REAL_reason():
    """A diagnostic that points at the wrong cause is worse than none. This used to report
    "the daily budget ₹1,000 does not cover a single share (cheapest ₹45)" on a day when the
    budget was fine and only ₹15 had settled — blaming the budget for a settlement shortfall
    (found in the 2026-08-30 audit sim, not in production)."""
    view = _view({"CHEAP": (46, 45), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    _fund_lots(pf, 50)
    st = _t1(watchlist="CHEAP", daily_budget=1_000.0)
    st.settled_cash = 15.0                     # plenty of budget, almost no settled cash
    st.on_slice(ctx)
    alert = st.strategy_alert or ""
    assert "only ₹15" in alert and "cheapest watchlist name is ₹45" in alert, alert
    assert "daily budget" not in alert, "the budget is not the problem — do not blame it"


def test_an_unread_fund_source_is_not_reported_as_dry():
    """Adoption is market-hours gated, so before the first open tick the platform ledger is
    empty while the broker may hold lakhs. The tile must tell "not looked yet" apart from
    "looked and found none" — reporting the first as FUND DRY told the owner to top up an
    ETF that already held ~90k (2026-08-31)."""
    view = _view({"AAA": (100, 95), FUND: (100, 100)})
    ctx, pf = _ctx(view)
    st = _strat(watchlist="AAA")

    b = st.basket_status(view, pf)
    assert b["fund_units"] == 0
    assert b["fund_checked"] is False, "nothing has read the broker yet — do not claim empty"

    st._fund_checked = True              # the manager's stamp, after a holdings() read
    assert st.basket_status(view, pf)["fund_checked"] is True   # now it IS a real empty


def test_fund_checked_is_transient_across_a_restart():
    """A recovered run has not read the broker either, so the flag must not persist."""
    st = _strat(watchlist="AAA")
    st._fund_checked = True
    assert "fund_checked" not in st.export_state()
    fresh = _strat(watchlist="AAA")
    fresh.load_state(st.export_state())
    assert fresh._fund_checked is False


def test_fund_adoption_is_throttled_not_called_every_tick(monkeypatch):
    """holdings() used to run on EVERY tick — twice a minute, all session, long after there
    was anything left to adopt. It shares a rate-limit budget with quotes, and on 2026-08-31
    the Dhan account was pushed into HTTP 429: run 28 lost live quotes with its 15:05
    decision still ahead of it. What this catches is the owner topping the ETF up by hand,
    days apart, so a 5-minute cadence loses nothing."""
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    from skas_algo.live import manager as mgr
    from skas_algo.live.manager import IST, LiveRun

    monkeypatch.setattr(mgr, "is_market_open", lambda **kw: True)
    calls = {"n": 0}

    class _Adapter:
        def positions(self):
            return []

        def holdings(self):
            calls["n"] += 1
            return {}

    stub = SimpleNamespace(
        session=SimpleNamespace(strategy=SimpleNamespace(fund_source="LIQUIDCASE")),
        config=SimpleNamespace(instrument_class="STOCK", quote_source="dhan",
                               segment="EQUITY", broker_account_id=2),
        quote_source=SimpleNamespace(adapter=_Adapter()),
        run_id=28,
    )
    for _ in range(20):                      # 20 ticks ~ 10 minutes at the 30s tick
        LiveRun._maybe_adopt_fund_holding(stub)
    assert calls["n"] == 1, f"throttled to one broker call, got {calls['n']}"

    stub._last_fund_adopt_at = datetime.now(IST) - timedelta(seconds=301)
    LiveRun._maybe_adopt_fund_holding(stub)
    assert calls["n"] == 2, "after the window it must look again — a top-up must be seen"


def test_the_broker_book_nets_todays_trades_against_settled_holdings():
    """holdings() lags a day BOTH ways: today's buy is not in it, today's SALE has not left
    it. Reading holdings alone halted run 28 (WIPRO platform +2 vs broker +1) and made
    adoption re-add the 24 LIQUIDCASE units it had just sold (2026-08-31). Real numbers."""
    from skas_algo.live.manager import _broker_delivery_book

    class _Adapter:
        holdings_exclude_today = True              # Dhan-verified; see _broker_delivery_book

        def positions(self):                       # today's book
            return [{"tradingsymbol": "WIPRO", "quantity": 2},        # bought today
                    {"tradingsymbol": "LIQUIDCASE", "quantity": -24},  # SOLD today
                    {"tradingsymbol": "MANAPPURAM", "quantity": 1}]
        def holdings(self):                        # settled, a day behind
            return {"WIPRO": {"units": 1}, "LIQUIDCASE": {"units": 778},
                    "MANAPPURAM": {"units": 2}}

    book = _broker_delivery_book(_Adapter())
    assert book["WIPRO"] == 3            # 1 settled + 2 bought today
    assert book["LIQUIDCASE"] == 754     # 778 on record MINUS the 24 sold today
    assert book["MANAPPURAM"] == 3
    # the sold units must NOT look adoptable: platform already booked the sale
    assert book["LIQUIDCASE"] - 754 < 1


def test_a_never_sells_run_adopts_shares_an_archived_run_left_behind(monkeypatch):
    """Run 26 was archived holding SOUTHBANK/IDFCFIRSTB/…; the shares stayed in the Dhan
    account owned by no run, so reconciliation — which compares the whole account — halted
    run 28 and would have every day after (2026-08-31, owner: "pls adopt the stray shares")."""
    from types import SimpleNamespace

    from skas_algo.live import manager as mgr
    from skas_algo.live.manager import LiveRun

    monkeypatch.setattr(mgr, "is_market_open", lambda **kw: True)
    monkeypatch.setattr(mgr.manager, "runs", {}, raising=False)
    adopted = []

    class _Adapter:
        holdings_exclude_today = True              # Dhan-verified; see _broker_delivery_book

        def positions(self):
            return [{"tradingsymbol": "SOUTHBANK", "quantity": 7}]   # run 28 bought today
        def holdings(self):
            return {"SOUTHBANK": {"units": 7, "avg_price": 45.41},   # run 26's strays
                    "LIQUIDCASE": {"units": 778, "avg_price": 115.64},
                    "ITC": {"units": 1, "avg_price": 269.2}}         # NOT on the watchlist

    st = _strat(watchlist="SOUTHBANK,INFY", fund_source="LIQUIDCASE")
    stub = SimpleNamespace(
        session=SimpleNamespace(
            strategy=st,
            portfolio=SimpleNamespace(lots=lambda s: []),
            adopt_broker_holding=lambda *a: adopted.append((a[1], a[2], a[3])),
            market=SimpleNamespace(last_close=lambda s: None),
        ),
        config=SimpleNamespace(instrument_class="STOCK", quote_source="dhan",
                               segment="EQUITY", broker_account_id=2),
        quote_source=SimpleNamespace(adapter=_Adapter()),
        run_id=28,
    )
    LiveRun._maybe_adopt_fund_holding(stub)
    got = {sym: units for sym, units, _px in adopted}
    assert got["SOUTHBANK"] == 14, "7 strays + 7 bought today, all now this run's"
    assert got["LIQUIDCASE"] == 778
    assert "ITC" not in got, "never adopt a symbol the run is not configured for"
