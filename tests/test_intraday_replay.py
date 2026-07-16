"""intraday_replay harness: the run-report contract (metrics/equity_curve/Trade rows),
charges applied, single strategy instance across days (fresh entries daily), the
weekly-straddle store-fed replay (y from day 1 → entry day 2), and input validation —
synthetic store in tmp, no network."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from skas_algo.data import option_intraday_store as store
from skas_algo.services.intraday_replay import run_intraday_backtest

EXP = "2026-07-21"
D1, D2 = date(2026, 7, 14), date(2026, 7, 15)


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "OPTION_INTRADAY_DIR", tmp_path / "1min")
    from skas_algo.config import get_settings
    monkeypatch.setattr(get_settings(), "option_bars_backup_dir", None)


def _leg_rows(day, strike, right, px_by_minute):
    sym = f"NIFTY|{EXP}|{strike}|{right}"
    return [{"symbol": sym, "start": datetime(day.year, day.month, day.day, hh, mm),
             "open": px, "high": px, "low": px, "close": px, "volume": 100.0, "oi": 5000.0}
            for (hh, mm), px in px_by_minute.items()]


def _flat_day(day, level_ce=150.0, level_pe=152.0):
    """One bar per 5-min boundary, premiums decaying slightly — a quiet straddle day."""
    minutes = [(9, m) for m in range(15, 60, 5)] + [(h, m) for h in (10, 11, 12, 13, 14)
                                                    for m in range(0, 60, 5)] + \
              [(15, m) for m in range(0, 30, 5)]
    rows = []
    for i, (hh, mm) in enumerate(minutes):
        decay = i * 0.3
        rows += _leg_rows(day, 24000, "CE", {(hh, mm): level_ce - decay})
        rows += _leg_rows(day, 24000, "PE", {(hh, mm): level_pe - decay})
        rows += _leg_rows(day, 24100, "CE", {(hh, mm): level_ce - 45 - decay})
        rows += _leg_rows(day, 24100, "PE", {(hh, mm): level_pe + 48 - decay})
    return pd.DataFrame(rows, columns=store.COLUMNS)


def test_contract_and_single_instance_across_days():
    store.write_day(D1, _flat_day(D1))
    store.write_day(D2, _flat_day(D2))
    out = run_intraday_backtest("intraday_straddle", "NIFTY", D1, D2, 1_000_000, {})
    report, trades = out["report"], out["trades"]
    m = report["metrics"]
    # The report contract the Runs list / ReportView render:
    for key in ("Total Return %", "Final Equity", "Max Drawdown %", "Total Trades",
                "Win Rate %", "Net Realized P&L", "Total Charges", "Max Margin Used"):
        assert key in m
    assert [p["date"] for p in report["equity_curve"]] == [D1.isoformat(), D2.isoformat()]
    # ONE instance across days → entered_day resets → 2 straddle entries (4 legs each way).
    entries = [t for t in trades if t["action"] == "SHORT"]
    closes = [t for t in trades if t["action"] == "COVER"]
    assert len(entries) == 4 and len(closes) == 4
    assert m["Total Trades"] == 2                          # cycles, not legs (options semantics)
    assert all(t["profit"] is not None for t in closes)   # P&L renders on closing rows
    assert m["Total Charges"] > 0                          # charges actually deducted
    assert m["Net Realized P&L"] == pytest.approx(
        sum(t["profit"] for t in closes), abs=1)
    # The options sub-report: absent-or-COMPLETE — every non-optional field OptionsReport
    # dereferences must exist (its presence flips ReportView to the options layout).
    o = report["options"]
    for key in ("total_premium_collected", "total_premium_captured", "premium_capture_pct",
                "avg_holding_days", "num_positions", "num_cycles", "win_rate_pct",
                "max_margin_used", "avg_margin_used", "capital_efficiency",
                "avg_premium_per_cycle", "total_charges", "net_after_charges"):
        assert key in o["summary"], key
    assert o["summary"]["num_cycles"] == 2 and o["summary"]["num_positions"] == 4
    assert o["summary"]["total_premium_collected"] > 0
    assert o["charges"]["total"] == pytest.approx(m["Total Charges"], abs=0.01)
    assert set(o["exit_reasons"]) == {"eod"}
    assert o["exit_reasons"]["eod"]["count"] == 2
    assert len(o["cycles"]) == 2 and len(o["positions"]) == 4
    c = o["cycles"][0]
    assert c["ce"] is not None and c["pe"] is not None       # straddle → ce/pe legs set
    assert c["legs_detail"] and c["premium_collected"] > 0
    assert 0 < c["holding_days"] <= 1.0                       # intraday fraction of a session
    assert len(o["per_expiry_cycle"]) == 1 and o["per_expiry_cycle"][0]["entries"] == 2
    assert len(o["margin_series"]) == 2 and len(o["premium_curve"]) == 2


def test_weekly_straddle_y_from_day1_entry_day2():
    # Day 1: quiet high premiums → y (low) established. Day 2: premiums open BELOW day-1's
    # low and below VWAP → the weekly straddle sells; a later spike crosses VWAP → exit.
    store.write_day(D1, _flat_day(D1, level_ce=200.0, level_pe=202.0))
    d2 = _flat_day(D2, level_ce=120.0, level_pe=122.0)
    # Spike both legs late so the combined premium crosses back above VWAP at ~14:00.
    for (hh, mm), bump in {(14, 0): 80.0, (14, 5): 82.0}.items():
        for right in ("CE", "PE"):
            d2.loc[(d2["symbol"] == f"NIFTY|{EXP}|24000|{right}")
                   & (d2["start"] == datetime(2026, 7, 15, hh, mm)),
                   ["open", "high", "low", "close"]] += bump
    store.write_day(D2, d2)
    out = run_intraday_backtest("weekly_intraday_straddle", "NIFTY", D1, D2, 1_000_000, {})
    trades = out["trades"]
    d2_entries = [t for t in trades
                  if t["action"] == "SHORT" and t["date"].startswith(D2.isoformat())]
    assert len(d2_entries) >= 2                       # entered on day 2 (y came from day 1)
    assert not any(t["action"] == "SHORT" and t["date"].startswith(D1.isoformat())
                   for t in trades)                    # day 1 had no prior-day bars → gated
    assert any(t["action"] == "COVER" and t.get("tag") in ("vwap_cross", "eod")
               for t in trades)


def test_unsupported_strategy_and_empty_window_raise():
    with pytest.raises(ValueError, match="not intraday-replayable"):
        run_intraday_backtest("hni_weekly", "NIFTY", D1, D2, 1_000_000, {})
    with pytest.raises(ValueError, match="no captured days"):
        run_intraday_backtest("intraday_straddle", "NIFTY", D1, D2, 1_000_000, {})


# ------------------------------------------------------------------- routes
@pytest.fixture
def api_client():
    from fastapi.testclient import TestClient

    from skas_algo.api import create_app

    return TestClient(create_app())


def test_strategies_basis_lists(api_client):
    eod = api_client.get("/api/v1/strategies").json()["strategies"]
    intraday = api_client.get("/api/v1/strategies?basis=intraday").json()["strategies"]
    assert "weekly_intraday_straddle" not in eod and "intraday_straddle" not in eod
    assert intraday[0] == "intraday_straddle" and "momentum_theta_gainer_intra" in intraday
    assert "hni_weekly" not in intraday


def test_intraday_backtest_preview_and_persist_roundtrip(api_client):
    store.write_day(D1, _flat_day(D1))
    store.write_day(D2, _flat_day(D2))
    body = {"strategy_id": "intraday_straddle", "underlying": "NIFTY",
            "instrument_class": "DERIV", "symbols": ["NIFTY"],
            "start_date": D1.isoformat(), "end_date": D2.isoformat(),
            "capital": 1_000_000, "params": {"lots": 1}, "persist": False,
            "name": "replay test"}
    r = api_client.post("/api/v1/backtest/intraday", json=body)
    assert r.status_code == 200, r.text
    prev = r.json()
    assert prev["run_id"] is None and prev["report"]["metrics"]["Total Trades"] == 2

    body["persist"] = True
    r2 = api_client.post("/api/v1/backtest/intraday", json=body)
    assert r2.status_code == 200, r2.text
    run_id = r2.json()["run_id"]
    assert run_id is not None
    detail = api_client.get(f"/api/v1/runs/{run_id}").json()
    assert detail["report"]["metrics"]["Total Trades"] == 2
    assert detail["report"]["options"]["summary"]["num_cycles"] == 2  # options layout renders
    assert detail["params"]["data_basis"] == "intraday"      # the run tag
    assert len(detail["report"]["equity_curve"]) == 2
    lst = api_client.get("/api/v1/runs").json()
    assert any(x["run_id"] == run_id for x in lst)


def test_intraday_backtest_no_coverage_is_422(api_client):
    body = {"strategy_id": "intraday_straddle", "underlying": "NIFTY",
            "instrument_class": "DERIV", "symbols": ["NIFTY"],
            "start_date": "2031-01-01", "end_date": "2031-01-05",
            "capital": 1_000_000, "params": {}, "persist": False}
    r = api_client.post("/api/v1/backtest/intraday", json=body)
    assert r.status_code == 422 and "no captured days" in r.json()["detail"]
