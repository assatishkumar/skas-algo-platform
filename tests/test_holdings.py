"""The accumulation report: per-holding breakdown, money-weighted return, index-SIP compare.

A buy-and-hold strategy's trading metrics describe its cash management, not its investing —
this section is what actually measures it, so the arithmetic is pinned here.
"""

from __future__ import annotations

from datetime import date

from skas_algo.services.holdings import holdings_report, xirr


def _buy(day, sym, units, price):
    return {"date": date.fromisoformat(day), "ticker": sym, "action": "BUY",
            "units": units, "price": price, "amount": units * price}


# ------------------------------------------------------------------- xirr
def test_xirr_recovers_a_known_rate():
    assert abs(xirr([(date(2025, 1, 1), -100.0), (date(2026, 1, 1), 110.0)]) - 0.10) < 1e-3


def test_xirr_of_a_drip_is_money_weighted_not_start_to_end():
    """Rs100 in at t0, Rs100 in at t+1y, Rs400 out at t+2y. A naive "total invested doubled
    over two years" reading gives sqrt(2)-1 = 41.4%; the money-weighted answer is higher,
    because half the money only had one year to do it. Check: 100(1.5608)^2 + 100(1.5608)
    = 400."""
    flows = [(date(2024, 1, 1), -100.0), (date(2025, 1, 1), -100.0), (date(2026, 1, 1), 400.0)]
    r = xirr(flows)
    assert abs(r - 0.5608) < 0.001
    assert r > (2 ** 0.5 - 1)          # strictly better than the naive start/end reading


def test_xirr_returns_none_rather_than_a_fake_number():
    assert xirr([(date(2025, 1, 1), -100.0)]) is None              # one flow
    assert xirr([(date(2025, 1, 1), -100.0), (date(2025, 1, 1), 110.0)]) is None  # same day
    assert xirr([(date(2025, 1, 1), -100.0), (date(2026, 1, 1), -50.0)]) is None  # no return


# -------------------------------------------------------------- the table
def test_the_breakdown_reports_units_cost_value_and_pnl_per_name():
    tx = [_buy("2025-01-01", "AAA", 10, 100.0), _buy("2025-06-01", "AAA", 10, 150.0),
          _buy("2025-01-01", "BBB", 5, 200.0)]
    h = holdings_report(tx, {"AAA": 200.0, "BBB": 100.0}, as_of=date(2026, 1, 1))
    aaa = next(r for r in h["rows"] if r["symbol"] == "AAA")
    assert aaa["units"] == 20 and aaa["invested"] == 2500.0 and aaa["avg_cost"] == 125.0
    assert aaa["value"] == 4000.0 and aaa["pnl"] == 1500.0 and aaa["pnl_pct"] == 60.0
    assert aaa["buys"] == 2 and aaa["first_buy"] == "2025-01-01"
    bbb = next(r for r in h["rows"] if r["symbol"] == "BBB")
    assert bbb["pnl"] == -500.0 and bbb["pnl_pct"] == -50.0      # a loser reports as one
    t = h["totals"]
    assert t["invested"] == 3500.0 and t["value"] == 4500.0 and t["pnl"] == 1000.0
    assert t["names"] == 2


def test_rows_are_ranked_by_value_and_weights_sum_to_a_hundred():
    tx = [_buy("2025-01-01", "AAA", 1, 100.0), _buy("2025-01-01", "BBB", 1, 300.0)]
    h = holdings_report(tx, {"AAA": 100.0, "BBB": 300.0}, as_of=date(2026, 1, 1))
    assert [r["symbol"] for r in h["rows"]] == ["BBB", "AAA"]
    assert abs(sum(r["weight_pct"] for r in h["rows"]) - 100.0) < 0.01


def test_a_fully_exited_name_leaves_the_holdings_table():
    """It belongs to realized P&L, not to what you own."""
    tx = [_buy("2025-01-01", "AAA", 10, 100.0),
          {"date": date(2025, 7, 1), "ticker": "AAA", "action": "SELL", "units": 10,
           "price": 150.0, "amount": 1500.0}]
    h = holdings_report(tx, {"AAA": 150.0}, as_of=date(2026, 1, 1))
    assert h["rows"] == [] and h["totals"]["names"] == 0


def test_per_holding_cagr_is_that_holdings_own_money_weighted_return():
    tx = [_buy("2025-01-01", "AAA", 1, 100.0)]
    h = holdings_report(tx, {"AAA": 110.0}, as_of=date(2026, 1, 1))
    assert abs(h["rows"][0]["xirr_pct"] - 10.0) < 0.1


# ------------------------------------------------------------- fund source
def test_the_fund_source_is_kept_out_of_the_holdings_and_reported_separately():
    tx = [_buy("2025-01-01", "LIQUIDBEES", 100, 1000.0), _buy("2025-01-02", "AAA", 1, 100.0)]
    h = holdings_report(tx, {"LIQUIDBEES": 1000.0, "AAA": 120.0}, as_of=date(2026, 1, 1),
                        fund_source="LIQUIDBEES")
    assert [r["symbol"] for r in h["rows"]] == ["AAA"]      # the ETF is not an investment
    assert h["fund"]["symbol"] == "LIQUIDBEES" and h["fund"]["units"] == 100.0
    assert h["fund"]["value"] == 100_000.0


def test_the_parked_yield_is_reported_but_never_folded_into_the_book():
    """LIQUIDBEES holds NAV at Rs1,000 and pays out as units, so a price-only backtest
    credits parked money 0%. The yield is stated at a given rate; nothing else moves."""
    tx = [_buy("2025-01-01", "LIQUIDBEES", 100, 1000.0)]
    curve = [{"date": "2025-01-01", "equity": 100_000.0},
             {"date": "2026-01-01", "equity": 100_000.0}]
    off = holdings_report(tx, {"LIQUIDBEES": 1000.0}, as_of=date(2026, 1, 1),
                          fund_source="LIQUIDBEES", equity_curve=curve)
    assert off["fund"]["yield_credited"] == 0.0            # default: report nothing
    on = holdings_report(tx, {"LIQUIDBEES": 1000.0}, as_of=date(2026, 1, 1),
                         fund_source="LIQUIDBEES", fund_yield_pct=6.5, equity_curve=curve)
    assert abs(on["fund"]["yield_credited"] - 6500.0) < 20.0   # 1 yr at 6.5% on Rs1L
    assert on["totals"] == off["totals"]                    # the portfolio itself is untouched


# --------------------------------------------------------------- benchmark
def test_the_benchmark_buys_the_same_rupees_on_the_same_days():
    """The only fair yardstick for a drip. A lump-sum index line would flatter or damn the
    strategy purely on when the money happened to arrive."""
    tx = [_buy("2025-01-01", "AAA", 1, 100.0), _buy("2025-07-01", "AAA", 1, 100.0)]
    prices = {"2025-01-01": 100.0, "2025-07-01": 200.0, "2026-01-01": 400.0}
    h = holdings_report(tx, {"AAA": 150.0}, as_of=date(2026, 1, 1),
                        benchmark="NIFTYBEES", benchmark_prices=prices)
    # Rs100 at 100 = 1 unit, Rs100 at 200 = 0.5 units → 1.5 units at 400 = Rs600
    assert h["benchmark"]["value"] == 600.0
    assert h["benchmark"]["index"] == "NIFTYBEES"
    # strategy holds 2 units at 150 = Rs300 → it is Rs300 behind
    assert h["benchmark"]["vs_value"] == -300.0
    assert h["benchmark"]["vs_xirr_pts"] < 0


def test_no_benchmark_prices_simply_omits_the_comparison():
    tx = [_buy("2025-01-01", "AAA", 1, 100.0)]
    h = holdings_report(tx, {"AAA": 110.0}, as_of=date(2026, 1, 1),
                        benchmark="NIFTYBEES", benchmark_prices={})
    assert "benchmark" not in h        # never an invented index line


def test_the_backfill_marks_at_the_run_end_date_not_the_last_traded_price():
    """A run saved before the panel existed is valued from the cache at ITS OWN end date.
    Marking at the last TRADED price instead froze every name at whatever it last happened to
    be bought at: run #278 read Rs2.86L profit instead of Rs6.27L, XIRR 4.21% instead of 8.30%,
    and TCS's weight inflated 18.9% -> 29.0%. Today's price would be just as wrong the other
    way for a historical run."""
    from datetime import date as _date
    from types import SimpleNamespace

    import pandas as pd

    from skas_algo.api.routes.backtest import _backfill_holdings

    trades = [{"date": "2024-01-01", "ticker": "AAA", "action": "BUY",
               "units": 10, "price": 100.0, "amount": 1000.0}]
    report = {"equity_curve": []}
    run = SimpleNamespace(id=1, trade_log=trades)
    algo = SimpleNamespace(strategy_id="value_investing",
                           params={"end_date": "2025-01-01", "start_date": "2024-01-01"})

    def loader(symbol, start, end):
        if symbol != "AAA":
            return None
        return pd.DataFrame({"date": [pd.Timestamp("2024-12-31")], "close": [250.0]})

    _backfill_holdings(report, run, algo, loader)
    row = report["holdings"]["rows"][0]
    assert row["last_price"] == 250.0        # the END-DATE close from the cache…
    assert row["value"] == 2500.0            # …not 10 x the Rs100 it was bought at
    assert report["holdings"]["derived"] is True


def test_the_backfill_falls_back_to_the_tape_when_the_cache_cannot_price_a_name():
    """A delisted or uncached name still appears, valued at what it last traded for here —
    better than dropping the holding silently."""
    from types import SimpleNamespace

    from skas_algo.api.routes.backtest import _backfill_holdings

    trades = [{"date": "2024-01-01", "ticker": "GONE", "action": "BUY",
               "units": 10, "price": 100.0, "amount": 1000.0}]
    report = {"equity_curve": []}
    run = SimpleNamespace(id=2, trade_log=trades)
    algo = SimpleNamespace(strategy_id="value_investing",
                           params={"end_date": "2025-01-01", "start_date": "2024-01-01"})
    _backfill_holdings(report, run, algo, lambda *a, **k: None)
    assert report["holdings"]["rows"][0]["last_price"] == 100.0
