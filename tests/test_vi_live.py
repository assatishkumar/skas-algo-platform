"""value_investing's Live-tile view (services/vi_live + ValueInvestingStrategy.preview_plan):
invested vs market value per WATCHLIST name with the fund source kept separate, the pooled
rupees each name is saving, and a dry run of today's buys that credits and spends nothing."""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

from skas_algo.engine.portfolio import Portfolio
from skas_algo.services.vi_live import value_investing_report
from skas_algo.strategies.value_investing import ValueInvestingStrategy

FUND = "LIQUIDCASE"
D = date(2026, 9, 8)


class Market:
    def __init__(self, prev: dict[str, float], last: dict[str, float]):
        self.prev, self.last = prev, last

    def prev_close(self, s):
        return self.prev.get(s)

    def last_close(self, s):
        return self.last.get(s)

    def close(self, s):
        if s not in self.last:
            raise KeyError(s)
        return self.last[s]


def _strat(**kw):
    kw.setdefault("watchlist", "AAA,BBB,CCC")
    kw.setdefault("fund_source", FUND)
    kw.setdefault("daily_budget", 3_000.0)
    kw.setdefault("settlement_days", 1)
    kw.setdefault("sizing", "equal_value")
    return ValueInvestingStrategy(universe=["AAA", "BBB", "CCC", FUND], initial_capital=50_000, **kw)


def _run(strategy, market, txns, pf):
    session = SimpleNamespace(strategy=strategy, market=market, portfolio=pf, transactions=txns)
    return SimpleNamespace(run_id=28, session=session)


def _tx(day, ticker, action, units, price):
    return {"date": datetime(day.year, day.month, day.day, 15, 5), "ticker": ticker,
            "action": action, "units": units, "price": price, "amount": units * price,
            "profit": 0.0, "pnl_pct": 0.0, "tag": ""}


def test_preview_plan_ranks_fallers_first_and_leaves_the_pots_untouched():
    st = _strat()
    st.settled_cash = 10_000.0
    st.pot = {"AAA": 500.0, "BBB": 2_000.0, "CCC": 100.0}
    st.pot_day = "2026-09-07"
    mkt = Market(prev={"AAA": 100, "BBB": 200, "CCC": 50}, last={"AAA": 99, "BBB": 190, "CCC": 51})
    out = st.preview_plan(mkt, D)
    assert [r[0] for r in out["ranked"]] == ["BBB", "AAA", "CCC"]          # −5%, −1%, +2%
    assert out["ranked"][0][1] == -5.0
    # today's ₹1,000 slice is credited to each pot BEFORE the walk: BBB 3,000 // 190 = 15
    assert out["pots"] == {"AAA": 1_500.0, "BBB": 3_000.0, "CCC": 1_100.0}
    assert out["plan"] == [("BBB", 190.0, 15, 2_850.0), ("AAA", 99.0, 15, 1_485.0),
                           ("CCC", 51.0, 21, 1_071.0)]
    assert out["spendable"] == 10_000.0 and out["projected"] is False
    # …and nothing moved: the real decision at 15:05 starts from the same state
    assert st.pot == {"AAA": 500.0, "BBB": 2_000.0, "CCC": 100.0} and st.pot_day == "2026-09-07"


def test_preview_is_capped_by_settled_cash_like_the_real_walk():
    st = _strat()
    st.settled_cash = 2_000.0
    st.pot = {"AAA": 5_000.0, "BBB": 5_000.0, "CCC": 5_000.0}
    st.pot_day = D.isoformat()
    mkt = Market(prev={"AAA": 100, "BBB": 100, "CCC": 100}, last={"AAA": 90, "BBB": 95, "CCC": 99})
    out = st.preview_plan(mkt, D)
    assert out["plan"] == [("AAA", 90.0, 22, 1_980.0)]                     # ₹2,000 buys 22, then dry
    assert out["spendable"] == 2_000.0


def test_the_report_shows_every_watchlist_name_and_keeps_the_fund_out_of_the_totals():
    st = _strat()
    st.settled_cash = 6_000.0
    st.pot = {"AAA": 100.0, "BBB": 900.0, "CCC": 0.0}
    st.pot_day = D.isoformat()
    pf = Portfolio(cash=50_000.0)
    pf.buy(FUND, 400, 115.0, date(2026, 8, 31))                          # ₹46,000 parked
    pf.buy("AAA", 20, 100.0, date(2026, 8, 31))
    pf.buy("BBB", 5, 200.0, date(2026, 9, 1))
    txns = [_tx(date(2026, 8, 31), FUND, "BUY", 400, 115.0),
            _tx(date(2026, 8, 31), "AAA", "BUY", 20, 100.0),
            _tx(date(2026, 9, 1), "BBB", "BUY", 5, 200.0)]
    mkt = Market(prev={"AAA": 105, "BBB": 210, "CCC": 50, FUND: 115.5},
                 last={"AAA": 110, "BBB": 189, "CCC": 51, FUND: 115.6})
    rep = value_investing_report(_run(st, mkt, txns, pf), D)

    by = {r["symbol"]: r for r in rep["rows"]}
    assert set(by) == {"AAA", "BBB", "CCC"}                                # the ETF is NOT a row
    assert by["AAA"]["status"] == "held" and by["AAA"]["invested"] == 2_000 and by["AAA"]["value"] == 2_200
    assert by["AAA"]["pnl"] == 200 and by["AAA"]["pnl_pct"] == 10.0
    assert by["BBB"]["pnl"] == -55 and by["BBB"]["change_pct"] == -10.0 and by["BBB"]["rank"] == 1
    assert by["CCC"]["status"] == "pending" and by["CCC"]["units"] == 0 and by["CCC"]["last_price"] == 51
    # invested / market value are the stocks only; the ETF is reported beside them
    assert rep["totals"]["invested"] == 3_000 and rep["totals"]["value"] == 3_145
    assert rep["fund"]["symbol"] == FUND and rep["fund"]["units"] == 400
    assert rep["fund"]["value"] == 400 * 115.6
    # today (pots already credited for the day): BBB fell the most and its ₹900 pot buys
    # 4 @ 189; AAA's ₹100 and CCC's ₹0 afford nothing yet — they keep saving
    assert by["BBB"]["buys_today"] == {"units": 4, "price": 189.0, "cost": 756.0}
    assert by["AAA"]["buys_today"] is None and by["CCC"]["buys_today"] is None
    assert by["AAA"]["pot"] == 100.0 and by["BBB"]["pot"] == 900.0
    assert rep["today"]["plan_total"] == 756.0 and rep["today"]["spendable"] == 6_000.0
    assert rep["today"]["pots_total"] == 1_000.0
    # the buy of the day leads the table, then held names by value, then the rest
    assert [r["symbol"] for r in rep["rows"]] == ["BBB", "AAA", "CCC"]


def test_a_name_that_was_sold_out_is_still_listed_as_exited():
    st = _strat(watchlist="AAA")
    st.settled_cash = 0.0
    pf = Portfolio(cash=0.0)
    txns = [_tx(date(2026, 8, 31), "AAA", "BUY", 10, 100.0),
            _tx(date(2026, 9, 2), "AAA", "SELL", 10, 120.0),
            _tx(date(2026, 9, 3), "ZZZ", "BUY", 1, 500.0)]          # a stray adopted name
    pf.buy("ZZZ", 1, 500.0, date(2026, 9, 3))
    mkt = Market(prev={"AAA": 100}, last={"AAA": 125})
    rep = value_investing_report(_run(st, mkt, txns, pf), D)
    by = {r["symbol"]: r for r in rep["rows"]}
    assert by["AAA"]["status"] == "exited" and by["AAA"]["units"] == 0
    assert by["ZZZ"]["status"] == "held" and not by["ZZZ"]["in_watchlist"]
    assert by["ZZZ"]["last_price"] == 500.0                            # no live mark → last fill


def test_cagr_is_blank_until_a_holding_is_old_enough_to_annualise():
    st = _strat(watchlist="AAA,BBB")
    st.settled_cash = 0.0
    pf = Portfolio(cash=0.0)
    pf.buy("AAA", 10, 100.0, date(2026, 1, 5))
    pf.buy("BBB", 10, 100.0, date(2026, 9, 1))
    txns = [_tx(date(2026, 1, 5), "AAA", "BUY", 10, 100.0), _tx(date(2026, 9, 1), "BBB", "BUY", 10, 100.0)]
    mkt = Market(prev={"AAA": 100, "BBB": 100}, last={"AAA": 110, "BBB": 98})
    rep = value_investing_report(_run(st, mkt, txns, pf), D)
    by = {r["symbol"]: r for r in rep["rows"]}
    assert by["AAA"]["xirr_pct"] is not None and by["AAA"]["xirr_pct"] > 0   # 8 months old
    assert by["BBB"]["xirr_pct"] is None                                      # 7 days old
    assert rep["totals"]["xirr_pct"] is not None                              # the sleeve is 8 months old


def test_a_day_with_no_settled_cash_says_so_and_lists_what_the_pots_are_ready_to_buy():
    """₹0 settled read as "no pot affords a share" while every pot held ₹2,857 against a
    ₹304 stock (owner, 2026-09-08). The plan is capped by cash; the affordable list is not,
    and the reason an empty plan is empty is named."""
    st = _strat()
    st.settled_cash = 0.0
    st.pending_credits = [["2026-09-09", 5_523.0]]
    st.pot = {"AAA": 2_857.0, "BBB": 2_857.0, "CCC": 100.0}
    st.pot_day = D.isoformat()
    pf = Portfolio(cash=0.0)
    mkt = Market(prev={"AAA": 300, "BBB": 1_150, "CCC": 50}, last={"AAA": 297, "BBB": 1_146, "CCC": 51})
    rep = value_investing_report(_run(st, mkt, [], pf), D)
    assert rep["today"]["plan"] == [] and rep["today"]["blocked_by"] == "cash"
    assert [(a["symbol"], a["units"]) for a in rep["today"]["affordable"]] == [("AAA", 9), ("BBB", 2), ("CCC", 1)]
    by = {r["symbol"]: r for r in rep["rows"]}
    assert by["AAA"]["buys_today"] is None and by["AAA"]["affordable"]["units"] == 9
    assert rep["today"]["affordable_total"] == 9 * 297 + 2 * 1_146 + 51
    # with cash, the same pots become the plan and nothing is "blocked"
    st.settled_cash = 10_000.0
    rep = value_investing_report(_run(st, mkt, [], pf), D)
    assert rep["today"]["blocked_by"] is None and [p["symbol"] for p in rep["today"]["plan"]] == ["AAA", "BBB", "CCC"]
    # and a day where no pot affords a share names THAT
    st.pot = {"AAA": 10.0, "BBB": 10.0, "CCC": 10.0}
    rep = value_investing_report(_run(st, mkt, [], pf), D)
    assert rep["today"]["blocked_by"] == "pots" and rep["today"]["affordable"] == []


def test_the_group_row_metrics_leave_the_fund_source_out_of_unrealized_and_the_open_count():
    """₹5,19,945 of GOLDBEES sat on value_investing's group row as "unrealized" beside ₹1,000
    of stocks. The ETF is money waiting; only the stocks are positions. Realized is untouched."""
    from skas_algo.api.routes.live import _tile_positions

    snap = {"open_positions": 4, "positions": [
        {"symbol": "GOLDBEES", "unrealized_pnl": 519_945.0},
        {"symbol": "WIPRO", "unrealized_pnl": -28.0},
        {"symbol": "SOUTHBANK", "unrealized_pnl": 4.0},
        {"symbol": "IDFCFIRSTB", "unrealized_pnl": 1.0},
    ]}
    pos, n = _tile_positions(snap, SimpleNamespace(fund_source="goldbees"))
    assert n == 3 and sum(p["unrealized_pnl"] for p in pos) == -23.0
    # a strategy without a fund source is exactly as before
    pos, n = _tile_positions(snap, SimpleNamespace())
    assert n == 4 and len(pos) == 4
    pos, n = _tile_positions(snap, None)
    assert n == 4
