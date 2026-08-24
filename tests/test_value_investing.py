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
    for day in range(40):                     # Rs500/day each
        st.last_shop_day = None
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
        st.last_shop_day = None
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
