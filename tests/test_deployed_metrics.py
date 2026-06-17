"""Deployed-capital + idle-cash CAGR overlay (reporting only) in compute_metrics."""

from __future__ import annotations

from datetime import date, timedelta

from skas_algo.engine.metrics import compute_metrics
from skas_algo.engine.runner import RunResult


class _Pf:
    total_taxes = 0.0
    total_withdrawals = 0.0
    cash = 0.0


def _history(years: int = 1, deployed: float = 200_000.0, idle_cash: float = 800_000.0,
             final_equity: float = 1_100_000.0):
    """A flat-ish daily series: constant deployed capital + idle cash, ending at final_equity."""
    n = int(years * 365) + 1
    d0 = date(2020, 1, 1)
    hist = []
    for i in range(n):
        eq = 1_000_000.0 if i < n - 1 else final_equity
        hist.append({
            "date": d0 + timedelta(days=i),
            "cash": idle_cash,
            "holdings_value": deployed,
            "invested_capital": deployed,
            "total_equity": eq,
        })
    return hist


def test_deployed_and_idle_metrics_present_and_sane():
    rr = RunResult(history=_history(), transactions=[], portfolio=_Pf())
    m = compute_metrics(rr, initial_capital=1_000_000.0, deployed=True, idle_return=0.06)

    # +100k profit on ~1,000,000 → 10% total return; deployed base is the 200k cost basis.
    assert round(m["Total Return %"]) == 10
    assert round(m["Avg Deployed Capital"]) == 200_000
    # Return on deployed = 100k / 200k = 50% (lifetime, cumulative).
    assert round(m["Return on Deployed Capital %"]) == 50
    # Simple per-year return on deployed = 50% / 1yr = 50%, above the 10% whole-capital CAGR.
    assert round(m["Deployed Return %/yr"]) == 50
    assert m["Deployed Return %/yr"] > m["CAGR %"]
    # Idle 6% on ~800k for a year ≈ ₹48k of extra interest, lifting the idle-adjusted CAGR.
    assert 40_000 < m["Idle Interest (assumed)"] < 56_000
    assert m["CAGR (idle @ 6%) %"] > m["CAGR %"]


def test_deployed_metrics_absent_by_default():
    rr = RunResult(history=_history(), transactions=[], portfolio=_Pf())
    m = compute_metrics(rr, initial_capital=1_000_000.0)  # deployed=False
    assert "Deployed Return %/yr" not in m and "Idle Interest (assumed)" not in m
