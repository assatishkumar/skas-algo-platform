"""Parity gate for SST-FIFO: reproduce skas-trading's plain SST (pooled/tiered exit).

Same approach as the SST-LIFO parity test — runs the original SSTStrategy and the
new sst_fifo engine over identical symbols/dates and asserts trades + realized
metrics match exactly. Skips if skas-data / skas-trading / the cache aren't present.
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
T1, T2, T3 = 0.10, 0.08, 0.06
LOOKBACK = 20
TAX = 0.20


def _load_skas_data():
    skas_data = pytest.importorskip("skas_data")
    sd = skas_data.SkasData(cache_only=True)
    for s in SYMBOLS:
        df = sd.get_prices(symbol=s, start_date=START, end_date=END)
        if df is None or df.empty:
            pytest.skip(f"No cached data for {s}; skipping FIFO parity test")
    return sd


@pytest.fixture(scope="module")
def reference_run():
    sd = _load_skas_data()
    if not os.path.isdir(SKAS_TRADING):
        pytest.skip("skas-trading repo not found")
    sys.path.insert(0, SKAS_TRADING)
    try:
        from strategies.sst.strategy import SSTStrategy as RefStrategy
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Cannot import reference SST strategy: {exc}")

    ref = RefStrategy(
        universe=list(SYMBOLS),
        initial_capital=CAPITAL,
        capital_parts=PARTS,
        profit_target_1=T1,
        profit_target_2=T2,
        profit_target_3=T3,
        lookback_period=LOOKBACK,
        tax_rate=TAX,
    )
    ref.load_data(sd, START, END)
    ref.run()
    return sd, ref


def _new_run(sd):
    from skas_algo.engine.metrics import compute_metrics
    from skas_algo.engine.runner import BacktestRunner
    from skas_algo.strategies.sst_fifo import SSTFifoStrategy

    def loader(sym, s, e):
        return sd.get_prices(symbol=sym, start_date=s, end_date=e)

    strat = SSTFifoStrategy(
        universe=list(SYMBOLS),
        initial_capital=CAPITAL,
        capital_parts=PARTS,
        profit_target_1=T1,
        profit_target_2=T2,
        profit_target_3=T3,
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


def test_fifo_transactions_match(reference_run):
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


def test_fifo_metrics_match(reference_run):
    sd, ref = reference_run
    _, metrics = _new_run(sd)
    ref_metrics = ref.get_metrics()
    # Realized / trading-logic metrics must match (Total Return % and CAGR % use our
    # consistent investor-return convention, so they're not compared here).
    for key in [
        "Final Equity",
        "Total Trades",
        "Win Rate %",
        "Cash Balance",
        "Total Taxes",
        "Avg Monthly Profit Booking",
        "Avg Monthly Profit (Pre-Tax)",
    ]:
        assert metrics[key] == pytest.approx(
            ref_metrics[key], rel=1e-9, abs=1e-6
        ), f"{key}: new={metrics[key]} ref={ref_metrics[key]}"
