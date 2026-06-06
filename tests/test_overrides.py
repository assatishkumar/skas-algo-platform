"""Override engine: 'book 50% at 6%, trail the rest with a 2% SL'."""

from __future__ import annotations

from datetime import date

import pandas as pd

from skas_algo.engine.overrides import OverrideRule
from skas_algo.engine.runner import BacktestRunner
from skas_algo.engine.stops import Stop, StopBook, StopKind
from skas_algo.strategies.sst_lifo import SSTLifoStrategy


def test_trailing_stop_triggers_on_drawdown():
    book = StopBook()
    book.attach(Stop(symbol="X", lot_id=1, kind=StopKind.TRAILING, trail=0.02, hwm=100.0))

    # Rises to 110 (no trigger), then falls 2% from the 110 peak -> trigger at <=107.8.
    assert book.evaluate({"X": 105.0}) == []
    assert book.evaluate({"X": 110.0}) == []
    assert book.evaluate({"X": 108.0}) == []  # 108 > 110*0.98 = 107.8
    triggered = book.evaluate({"X": 107.0})
    assert [s.lot_id for s in triggered] == [1]


def _dip_then_ramp_then_drop() -> pd.DataFrame:
    # 25 flat @100, dip to 90 (new 20d low), recover, break out and ramp up past +6%,
    # peak, then fall back to trigger a 2% trailing stop on the remainder.
    dates = pd.bdate_range(start="2020-01-01", periods=90)
    closes = [100.0] * 25 + [90.0] + [100.0] * 5
    price = 101.0
    for _ in range(30):  # ramp up well past +6%
        closes.append(price)
        price += 1.5
    for _ in range(len(dates) - len(closes)):  # then decline to trip the trail
        price -= 1.5
        closes.append(price)
    closes = closes[: len(dates)]
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1000] * len(dates),
        }
    )


def _run(overrides):
    frames = {"AAA": _dip_then_ramp_then_drop()}
    strat = SSTLifoStrategy(
        universe=["AAA"], initial_capital=100_000, capital_parts=10, profit_target=0.06
    )
    runner = BacktestRunner(
        strategy=strat,
        universe=["AAA"],
        loader=lambda s, a, b: frames[s],
        initial_capital=100_000,
        lookback=20,
        tax_rate=0.0,
        overrides=overrides,
    )
    return runner.run(date(2020, 1, 1), date(2021, 12, 31))


def test_book_half_and_trail_rest():
    override = OverrideRule(
        scope="SYMBOL",
        target="AAA",
        rule={
            "exit": [
                {"at_pct": 6, "action": "book", "qty_pct": 50},
                {"action": "trail_sl", "trail_pct": 2},
            ]
        },
    )
    result = _run([override])

    buys = [t for t in result.transactions if t["action"] in ("BUY", "AVG_BUY")]
    books = [t for t in result.transactions if t["tag"] == "BOOK"]
    trails = [t for t in result.transactions if t["tag"] == "TRAIL"]

    assert len(buys) == 1
    bought_units = buys[0]["units"]

    # Booked ~50% at the +6% trigger; remainder later exits via the trailing stop.
    assert len(books) == 1
    assert len(trails) == 1
    assert books[0]["units"] == bought_units // 2
    assert books[0]["units"] + trails[0]["units"] == bought_units
    # The trailing exit happens after the book (rode the trend up, then stopped out).
    assert trails[0]["date"] > books[0]["date"]
    assert trails[0]["price"] > books[0]["price"]


def test_without_override_full_exit_at_target():
    result = _run(overrides=None)
    sells = [t for t in result.transactions if t["action"] == "SELL"]
    books = [t for t in result.transactions if t["tag"] == "BOOK"]
    # No override -> a single full exit at the 6% target, no partial book.
    assert len(books) == 0
    assert len(sells) == 1
    buys = [t for t in result.transactions if t["action"] == "BUY"]
    assert sells[0]["units"] == buys[0]["units"]
