"""API tests for the backtest + reports endpoints (synthetic data, no cache needed)."""

from __future__ import annotations

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from skas_algo.api import create_app
from skas_algo.data.provider import get_price_loader


def _ramp_frame() -> pd.DataFrame:
    dates = pd.bdate_range(start="2020-01-01", periods=80)
    closes = [100.0] * 25 + [90.0] + [100.0] * 5
    price = 101.0
    while len(closes) < len(dates):
        closes.append(price)
        price += 1.5
    closes = closes[: len(dates)]
    # float32 mirrors skas-data's real dtype (and guards JSON serialization).
    closes_f32 = pd.array(closes, dtype="float32")
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes_f32,
            "high": closes_f32,
            "low": closes_f32,
            "close": closes_f32,
            "volume": [1000] * len(dates),
        }
    )


@pytest.fixture
def api_client() -> TestClient:
    app = create_app()
    frame = _ramp_frame()
    app.dependency_overrides[get_price_loader] = lambda: (lambda sym, s, e: frame)
    return TestClient(app)


def test_list_strategies(api_client: TestClient):
    resp = api_client.get("/api/v1/strategies")
    assert resp.status_code == 200
    assert "sst_lifo" in resp.json()["strategies"]


def test_backtest_run_and_reports(api_client: TestClient):
    body = {
        "strategy_id": "sst_lifo",
        "symbols": ["AAA"],
        "start_date": "2020-01-01",
        "end_date": "2021-12-31",
        "capital": 100000,
        "params": {"capital_parts": 10, "profit_target": 0.06},
        "tax_rate": 0.0,
    }
    resp = api_client.post("/api/v1/backtest", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    run_id = data["run_id"]
    assert data["report"]["metrics"]["Total Trades"] >= 1
    assert any(t["action"] == "BUY" for t in data["trades"])

    # Persisted run is retrievable.
    got = api_client.get(f"/api/v1/runs/{run_id}")
    assert got.status_code == 200
    assert got.json()["strategy_id"] == "sst_lifo"

    # Listed.
    runs = api_client.get("/api/v1/runs").json()
    assert any(r["run_id"] == run_id for r in runs)

    # Trades CSV export.
    csv_resp = api_client.get(f"/api/v1/runs/{run_id}/trades.csv")
    assert csv_resp.status_code == 200
    assert csv_resp.headers["content-type"].startswith("text/csv")
    assert "date,ticker,action" in csv_resp.text


def test_backtest_unknown_strategy(api_client: TestClient):
    body = {
        "strategy_id": "nope",
        "symbols": ["AAA"],
        "start_date": "2020-01-01",
        "end_date": "2021-12-31",
    }
    resp = api_client.post("/api/v1/backtest", json=body)
    assert resp.status_code == 404
