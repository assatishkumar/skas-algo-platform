"""Parity gate: BACKTEST mode must reproduce skas-trading's SST-LIFO numbers.

This is the primary correctness check for the unified engine (docs/PLAN.md →
Verification). It runs the *original* strategy from skas-trading and the *new*
engine over the same symbols/dates from the same skas-data cache, and asserts the
trades and headline metrics are identical.

Skips automatically if skas-data / skas-trading or the local price cache aren't
available (e.g. in CI).
"""

from __future__ import annotations

import os
import sys
from datetime import date

import pytest

SKAS_TRADING = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "skas-trading"))

SYMBOLS = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK"]
START = date(2018, 1, 1)
END = date(2022, 12, 31)
CAPITAL = 2_500_000
PARTS = 50
TARGET = 0.06
LOOKBACK = 20
TAX = 0.20


def _load_skas_data():
    skas_data = pytest.importorskip("skas_data")
    sd = skas_data.SkasData(cache_only=True)
    # Require cached data for the test symbols.
    for s in SYMBOLS:
        df = sd.get_prices(symbol=s, start_date=START, end_date=END)
        if df is None or df.empty:
            pytest.skip(f"No cached data for {s}; skipping parity test")
    return sd


@pytest.fixture(scope="module")
def reference_run():
    sd = _load_skas_data()
    if not os.path.isdir(SKAS_TRADING):
        pytest.skip("skas-trading repo not found alongside skas-algo-platform")
    sys.path.insert(0, SKAS_TRADING)
    try:
        from strategies.sst_lifo.strategy import SSTLifoStrategy as RefStrategy
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Cannot import reference strategy: {exc}")

    ref = RefStrategy(
        universe=list(SYMBOLS),
        initial_capital=CAPITAL,
        capital_parts=PARTS,
        profit_target=TARGET,
        lookback_period=LOOKBACK,
        tax_rate=TAX,
    )
    ref.load_data(sd, START, END)
    ref.run()
    return sd, ref


def _new_run(sd):
    from skas_algo.engine.metrics import compute_metrics
    from skas_algo.engine.runner import BacktestRunner
    from skas_algo.strategies.sst_lifo import SSTLifoStrategy

    def loader(sym, s, e):
        return sd.get_prices(symbol=sym, start_date=s, end_date=e)

    strat = SSTLifoStrategy(
        universe=list(SYMBOLS),
        initial_capital=CAPITAL,
        capital_parts=PARTS,
        profit_target=TARGET,
    )
    runner = BacktestRunner(
        strategy=strat,
        universe=list(SYMBOLS),
        loader=loader,
        initial_capital=CAPITAL,
        lookback=LOOKBACK,
        tax_rate=TAX,
        withdrawal_rate=0.0,
    )
    result = runner.run(START, END)
    return result, compute_metrics(result, CAPITAL)


def test_transactions_match(reference_run):
    sd, ref = reference_run
    result, _ = _new_run(sd)

    assert len(result.transactions) == len(ref.transactions), "trade count mismatch"

    for new_t, ref_t in zip(result.transactions, ref.transactions, strict=True):
        assert new_t["date"] == ref_t["date"]
        assert new_t["ticker"] == ref_t["ticker"]
        assert new_t["action"] == ref_t["action"]
        assert new_t["units"] == ref_t["units"]
        assert new_t["price"] == pytest.approx(ref_t["price"], rel=1e-9)
        assert new_t["profit"] == pytest.approx(ref_t["profit"], rel=1e-9, abs=1e-6)
        assert new_t["lots"] == ref_t["lots"]


def test_metrics_match(reference_run):
    sd, ref = reference_run
    _, metrics = _new_run(sd)
    ref_metrics = ref.get_metrics()

    for key in [
        "Total Return %",
        "CAGR %",
        "Final Equity",
        "Max Drawdown %",
        "Max Capital Used",
        "Total Trades",
        "Win Rate %",
        "Cash Balance",
        "Total Taxes",
        "Total Withdrawals",
        "Avg Monthly Profit Booking",
        "Avg Monthly Profit (Pre-Tax)",
        "Avg Monthly Profit (Post-Tax)",
    ]:
        assert metrics[key] == pytest.approx(
            ref_metrics[key], rel=1e-9, abs=1e-6
        ), f"{key}: new={metrics[key]} ref={ref_metrics[key]}"
