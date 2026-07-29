"""gap_reversal: gap-up + above-EMA + oversold-RSI entry, EMA-cross exit, fail-closed live.

Synthetic 7-bar path (small periods so every value is hand-checkable):
closes 100,100,98,96,94,95.5,90 · opens follow prev close EXCEPT day-6 (open 96 vs prev
close 94 = +2.13% gap up). With ema_period=2 / rsi_period=2: day-6 EMA≈95.32 (< close
95.5), Wilder RSI≈46.15 — so rsi_entry_below=50 admits the entry and 40 refuses it.
Day-7 close 90 < EMA → exit."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from skas_algo.engine.context import AlgoContext
from skas_algo.engine.indicators import rsi as rsi_fn
from skas_algo.engine.live_market import LiveMarketView
from skas_algo.engine.portfolio import Portfolio
from skas_algo.engine.runner import BacktestRunner
from skas_algo.strategies.gap_reversal import GapReversalStrategy

CLOSES = [100.0, 100.0, 98.0, 96.0, 94.0, 95.5, 90.0]
OPENS = [100.0, 100.0, 100.0, 98.0, 96.0, 96.0, 89.0]  # day-6 opens ABOVE prev close (gap up)


def _loader(symbol, start_date, end_date):
    dates = pd.bdate_range(start="2024-01-01", periods=len(CLOSES))
    df = pd.DataFrame({
        "date": dates,
        "open": OPENS,
        "high": [max(o, c) + 0.5 for o, c in zip(OPENS, CLOSES, strict=True)],
        "low": [min(o, c) - 0.5 for o, c in zip(OPENS, CLOSES, strict=True)],
        "close": CLOSES,
    })
    return df[(df["date"] >= pd.Timestamp(start_date))
              & (df["date"] <= pd.Timestamp(end_date))].reset_index(drop=True)


def _run(loader=None, n_days=len(CLOSES), **params):
    # rsi_source="close" keeps the original hand-computed path deterministic; the
    # ema-source (default) behaviour is pinned by test_rsi_source_ema_is_the_chart_spec.
    kw = {"ema_period": 2, "rsi_period": 2, "rsi_entry_below": 50.0,
          "rsi_source": "close", **params}
    strat = GapReversalStrategy(
        universe=["AAA"], initial_capital=1_000_000, capital_parts=10, **kw)
    runner = BacktestRunner(
        strategy=strat, universe=["AAA"], loader=loader or _loader, initial_capital=1_000_000,
        lookback=2, tax_rate=0.0, indicators=strat.indicator_config())
    end = pd.bdate_range("2024-01-01", periods=n_days)[-1].date()
    return runner.run(date(2024, 1, 1), end)


def test_wilder_rsi_hand_computed():
    s = pd.Series(CLOSES)
    r = rsi_fn(s, 2)
    # deltas 0,-2,-2,-2,+1.5,-5.5 → Wilder(α=½): day-6 avg_gain .75, avg_loss .875
    assert r.iloc[5] == pytest.approx(100 - 100 / (1 + 0.75 / 0.875), abs=1e-9)  # ≈46.15
    assert pd.isna(r.iloc[0])                       # warmup
    assert r.iloc[3] == 0.0                         # all-loss window → 0


def test_entry_on_gap_and_exit_on_ema_cross():
    result = _run()
    buys = [t for t in result.transactions if t["action"] == "BUY"]
    sells = [t for t in result.transactions if t["action"] == "SELL"]
    assert len(buys) == 1 and buys[0]["ticker"] == "AAA"
    assert buys[0]["price"] == 95.5                 # the gap day's close, not earlier
    assert buys[0]["units"] == int(100_000 // 95.5)  # capital/parts allocation
    assert len(sells) == 1 and sells[0]["price"] == 90.0
    assert sells[0].get("exit_reason") == "ema_exit"


def test_every_leg_of_the_condition_gates():
    # min_gap_pct above the actual 2.13% gap → no trade.
    assert not _run(min_gap_pct=3.0).transactions
    # tighter RSI band (40 < the day's 46.15) → no trade.
    assert not _run(rsi_entry_below=40.0).transactions


def test_live_unseeded_fails_closed():
    """LiveMarketView has no indicator values until the manager seeds them (Phase 2) —
    the strategy must trade NOTHING (no entries, and no blind exit of a held lot)."""
    view = LiveMarketView(lookback=2)
    view.seed("AAA", [100.0, 94.0])
    view.update_quote("AAA", 96.0)
    pf = Portfolio(cash=1_000_000)
    strat = GapReversalStrategy(universe=["AAA"], initial_capital=1_000_000)
    ctx = AlgoContext(None, {}, pf, view)
    assert strat.on_slice(ctx) == []                # no entry without indicators
    pf.buy("AAA", 100, 95.0, date(2024, 1, 2))      # now holding — still no blind exit
    assert strat.on_slice(ctx) == []
    view.set_indicators("AAA", {"ema": 97.0})       # seeded: close 96 < ema 97 → exit fires
    out = strat.on_slice(ctx)
    assert len(out) == 1 and out[0].reason == "ema_exit"


# --- the TRUE chart spec: RSI computed ON the EMA ("RSI 10 EMA:EMA"), band 10 ---
# Downtrend 100→80 pins RSI-of-EMA at 0; day-7 gaps up (open 82 > prev close 80) and
# closes 85, retaking the EMA(3)=84.44 while RSI-of-EMA is still 7.1 → the entry the
# owner's BSE chart shows. Raw-close RSI reads 38.46 there → source="close" refuses it.
REV_CLOSES = [100.0, 96.0, 92.0, 88.0, 84.0, 80.0, 85.0, 78.0]
REV_OPENS = [100.0, 100.0, 96.0, 92.0, 88.0, 84.0, 82.0, 77.0]


def _rev_loader(symbol, start_date, end_date):
    dates = pd.bdate_range(start="2024-01-01", periods=len(REV_CLOSES))
    df = pd.DataFrame({
        "date": dates,
        "open": REV_OPENS,
        "high": [max(o, c) + 0.5 for o, c in zip(REV_OPENS, REV_CLOSES, strict=True)],
        "low": [min(o, c) - 0.5 for o, c in zip(REV_OPENS, REV_CLOSES, strict=True)],
        "close": REV_CLOSES,
    })
    return df[(df["date"] >= pd.Timestamp(start_date))
              & (df["date"] <= pd.Timestamp(end_date))].reset_index(drop=True)


def test_rsi_source_ema_is_the_chart_spec():
    base = {"loader": _rev_loader, "n_days": len(REV_CLOSES),
            "ema_period": 3, "rsi_period": 3, "rsi_entry_below": 10.0}
    # source="ema" (the strategy default): enters the day-7 recovery, exits day-8.
    result = _run(rsi_source="ema", **base)
    buys = [t for t in result.transactions if t["action"] == "BUY"]
    sells = [t for t in result.transactions if t["action"] == "SELL"]
    assert len(buys) == 1 and buys[0]["price"] == 85.0
    assert len(sells) == 1 and sells[0]["price"] == 78.0
    assert sells[0].get("exit_reason") == "ema_exit"
    # raw-close RSI is 38.46 that day → the same band never admits the trade.
    assert not _run(rsi_source="close", **base).transactions
    # sanity: the saturation mechanism itself (downtrend pins RSI-of-EMA at ~0).
    from skas_algo.engine.indicators import ema as ema_fn
    r = rsi_fn(ema_fn(pd.Series(REV_CLOSES), 3), 3)
    assert r.iloc[5] == 0.0 and r.iloc[6] < 10.0


# --- indicator warmup (run #237's phantom day-one entries) ---
# 40-bar uptrend, then a 14-bar decline ending in a gap-up pop that retakes the EMA.
# CONVERGED RSI-of-EMA on the pop day = 12.34 (> band 10 → refuse); a run STARTING at the
# decline sees a freshly-seeded 5.54 (< 10 → phantom entry). services/backtest passes
# warmup_days=250 for needs_indicators strategies; this pins the runner-level mechanics.
WU_UP = [100.0 + 2 * i for i in range(40)]
WU_DOWN = [178.0 - 1.5 * (i + 1) for i in range(14)]
WU_CLOSES = WU_UP + WU_DOWN + [WU_DOWN[-1] + 4.0]
WU_OPENS = [WU_CLOSES[0]] + [c for c in WU_CLOSES[:-1]]
WU_OPENS[-1] = WU_CLOSES[-2] + 1.0                      # the pop day gaps UP
WU_DATES = pd.bdate_range(start="2024-01-01", periods=len(WU_CLOSES))


def _wu_loader(symbol, start_date, end_date):
    df = pd.DataFrame({
        "date": WU_DATES,
        "open": WU_OPENS,
        "high": [max(o, c) + 0.5 for o, c in zip(WU_OPENS, WU_CLOSES, strict=True)],
        "low": [min(o, c) - 0.5 for o, c in zip(WU_OPENS, WU_CLOSES, strict=True)],
        "close": WU_CLOSES,
    })
    return df[(df["date"] >= pd.Timestamp(start_date))
              & (df["date"] <= pd.Timestamp(end_date))].reset_index(drop=True)


def test_warmup_kills_the_fresh_seed_phantom_entry():
    start = WU_DATES[40].date()                          # the run begins at the decline
    strat_kw = dict(universe=["AAA"], initial_capital=1_000_000, capital_parts=10,
                    ema_period=5, rsi_period=5, rsi_source="ema", rsi_entry_below=10.0)

    def run(warmup_days):
        strat = GapReversalStrategy(**strat_kw)
        runner = BacktestRunner(
            strategy=strat, universe=["AAA"], loader=_wu_loader, initial_capital=1_000_000,
            lookback=2, tax_rate=0.0, indicators=strat.indicator_config())
        return runner.run(start, WU_DATES[-1].date(), warmup_days=warmup_days)

    # No warmup: the freshly-seeded RSI (5.54) admits the phantom pop-day entry.
    assert any(t["action"] == "BUY" for t in run(0).transactions)
    # With pre-start bars loaded, the converged RSI (12.34) refuses it — and nothing is
    # ever traded/recorded inside the warmup buffer itself.
    warmed = run(80)
    assert warmed.transactions == []
