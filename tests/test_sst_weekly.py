"""SST Weekly: tracks a weekly Donchian low, buys the weekly-high breakout, exits per-lot.

Uses one bar per ISO week so each slice is a week boundary, with a 3-week Donchian window
(small warmup). Closes: 100,100,90,88 then a 130 breakout (buy), then 150 (+15% → exit)."""

from __future__ import annotations

from datetime import date

import pandas as pd

from skas_algo.engine.runner import BacktestRunner
from skas_algo.strategies.sst_weekly import SSTWeeklyFifoStrategy, SSTWeeklyStrategy

# Seven consecutive Mondays → seven distinct ISO weeks (one weekly close each).
MONDAYS = [date(2024, 1, 1), date(2024, 1, 8), date(2024, 1, 15), date(2024, 1, 22),
           date(2024, 1, 29), date(2024, 2, 5), date(2024, 2, 12)]
CLOSES = [100.0, 100.0, 100.0, 90.0, 88.0, 130.0, 150.0]


def _loader(symbol, start_date, end_date):
    df = pd.DataFrame({"date": MONDAYS, "close": CLOSES})
    return df[(df["date"] >= start_date) & (df["date"] <= end_date)].reset_index(drop=True)


def test_weekly_breakout_buy_and_lot_exit():
    strat = SSTWeeklyStrategy(
        universe=["AAA"], initial_capital=2_500_000, capital_parts=50,
        donchian_weeks=3, profit_target=0.10,
    )
    runner = BacktestRunner(strategy=strat, universe=["AAA"], loader=_loader,
                            initial_capital=2_500_000, lookback=1, tax_rate=0.0)
    result = runner.run(MONDAYS[0], MONDAYS[-1])
    tx = [(t["date"].date().isoformat(), t["action"], round(t["price"]), t.get("lots"))
          for t in result.transactions]

    buys = [t for t in result.transactions if t["action"] in ("BUY", "AVG_BUY")]
    sells = [t for t in result.transactions if t["action"] == "SELL"]

    # One breakout buy at the 130 weekly close (after a 90/88 weekly low → tracking).
    assert len(buys) == 1 and round(buys[0]["price"]) == 130, tx
    assert buys[0]["date"].date() == date(2024, 2, 5)
    # Exited the lot the next week at 150 (+15% ≥ the 10% per-lot target).
    assert len(sells) == 1 and round(sells[0]["price"]) == 150, tx
    assert sells[0]["date"].date() == date(2024, 2, 12)
    assert sells[0]["profit"] > 0


def test_weekly_fifo_pooled_exit_at_tiered_target():
    # Same weekly entry; the FIFO variant exits the WHOLE position at the (1-lot) tiered
    # target. With profit_target_1=10%, the 150 close (+15% over the 130 entry) books it.
    strat = SSTWeeklyFifoStrategy(
        universe=["AAA"], initial_capital=2_500_000, capital_parts=50,
        donchian_weeks=3, profit_target_1=0.10,
    )
    runner = BacktestRunner(strategy=strat, universe=["AAA"], loader=_loader,
                            initial_capital=2_500_000, lookback=1, tax_rate=0.0)
    result = runner.run(MONDAYS[0], MONDAYS[-1])
    buys = [t for t in result.transactions if t["action"] in ("BUY", "AVG_BUY")]
    sells = [t for t in result.transactions if t["action"] == "SELL"]
    assert len(buys) == 1 and round(buys[0]["price"]) == 130
    # Pooled exit: one SELL covering the whole position at 150.
    assert len(sells) == 1 and round(sells[0]["price"]) == 150 and sells[0]["profit"] > 0


def test_no_trades_during_weekly_warmup():
    # With donchian_weeks=6 there is never enough weekly history here → no trades at all.
    strat = SSTWeeklyStrategy(universe=["AAA"], initial_capital=2_500_000, donchian_weeks=6)
    runner = BacktestRunner(strategy=strat, universe=["AAA"], loader=_loader,
                            initial_capital=2_500_000, lookback=1, tax_rate=0.0)
    result = runner.run(MONDAYS[0], MONDAYS[-1])
    assert result.transactions == []
