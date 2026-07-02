"""API tests for the backtest + reports endpoints (synthetic data, no cache needed)."""

from __future__ import annotations

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from skas_algo.api import create_app
from skas_algo.data.provider import get_available_symbols, get_price_loader


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
    # Pretend only these symbols have cached data (for universe resolution).
    app.dependency_overrides[get_available_symbols] = lambda: {"RELIANCE", "TCS", "INFY"}
    return TestClient(app)


def test_universes_listed_with_counts(api_client: TestClient):
    resp = api_client.get("/api/v1/universes")
    assert resp.status_code == 200
    by_name = {u["name"]: u for u in resp.json()}
    assert set(by_name) == {"nifty25", "nifty50", "nifty100", "nifty200", "nifty500"}
    # Only RELIANCE/TCS/INFY are "available", so counts reflect the intersection.
    assert by_name["nifty50"]["count"] == 3
    assert by_name["nifty50"]["label"] == "Nifty 50"


def test_backtest_by_universe(api_client: TestClient):
    body = {
        "strategy_id": "sst_lifo",
        "universe": "nifty50",
        "start_date": "2020-01-01",
        "end_date": "2021-12-31",
        "capital": 100000,
        "params": {"capital_parts": 10, "profit_target": 0.06},
        "tax_rate": 0.0,
    }
    resp = api_client.post("/api/v1/backtest", json=body)
    assert resp.status_code == 200, resp.text
    # Ran against the 3 resolved symbols; at least one trade given the ramp.
    assert resp.json()["report"]["metrics"]["Total Trades"] >= 1


def test_backtest_preview_then_save(api_client: TestClient):
    """persist=False previews without writing; /backtest/save persists the same result (no recompute)."""
    body = {
        "strategy_id": "sst_lifo",
        "universe": "nifty50",
        "start_date": "2020-01-01",
        "end_date": "2021-12-31",
        "capital": 100000,
        "params": {"capital_parts": 10, "profit_target": 0.06},
        "tax_rate": 0.0,
        "persist": False,
    }
    before = len(api_client.get("/api/v1/runs").json())

    preview = api_client.post("/api/v1/backtest", json=body)
    assert preview.status_code == 200, preview.text
    pj = preview.json()
    assert pj["run_id"] is None  # a preview is NOT persisted
    trades_count = pj["report"]["metrics"]["Total Trades"]
    assert trades_count >= 1
    assert len(api_client.get("/api/v1/runs").json()) == before  # nothing written yet

    save = api_client.post(
        "/api/v1/backtest/save",
        json={"request": body, "report": pj["report"], "trades": pj["trades"]},
    )
    assert save.status_code == 200, save.text
    rid = save.json()["run_id"]
    assert rid is not None
    # Saved run is retrievable and identical to the preview (saved, not recomputed).
    got = api_client.get(f"/api/v1/runs/{rid}").json()
    assert got["report"]["metrics"]["Total Trades"] == trades_count
    assert len(api_client.get("/api/v1/runs").json()) == before + 1


def test_run_analysis_and_listing(api_client: TestClient):
    body = {
        "strategy_id": "sst_lifo", "universe": "nifty50",
        "start_date": "2020-01-01", "end_date": "2021-12-31", "capital": 100000,
        "params": {"capital_parts": 10, "profit_target": 0.06}, "tax_rate": 0.0,
    }
    rid = api_client.post("/api/v1/backtest", json=body).json()["run_id"]

    a = api_client.get(f"/api/v1/runs/{rid}/analysis")
    assert a.status_code == 200, a.text
    j = a.json()
    assert j["strategy_id"] == "sst_lifo" and j["instrument_class"] == "STOCK"
    assert isinstance(j["trades"], list) and j["params"]

    runs = api_client.get("/api/v1/analysis/runs").json()
    mine = next(r for r in runs if r["run_id"] == rid)
    assert mine["status"] == "backtest" and mine["instrument_class"] == "STOCK"


def test_backtest_requires_symbols_or_universe(api_client: TestClient):
    resp = api_client.post(
        "/api/v1/backtest",
        json={"strategy_id": "sst_lifo", "start_date": "2020-01-01", "end_date": "2021-12-31"},
    )
    assert resp.status_code == 422


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


def _make_run(api_client: TestClient, **overrides) -> int:
    body = {
        "strategy_id": "sst_lifo",
        "symbols": ["AAA"],
        "start_date": "2020-01-01",
        "end_date": "2021-12-31",
        "capital": 100000,
        "params": {"capital_parts": 10, "profit_target": 0.06},
        "tax_rate": 0.0,
    }
    body.update(overrides)
    return api_client.post("/api/v1/backtest", json=body).json()["run_id"]


def test_run_persists_full_params(api_client: TestClient):
    run_id = _make_run(api_client, universe=None)
    params = api_client.get(f"/api/v1/runs/{run_id}").json()["params"]
    # The full input set is persisted for display on the detail/compare screens.
    for k in ("symbols", "start_date", "end_date", "lookback", "tax_rate", "withdrawal_rate"):
        assert k in params, k
    assert params["start_date"] == "2020-01-01" and params["end_date"] == "2021-12-31"


def test_gross_equity_in_curve(api_client: TestClient):
    # With tax_rate=0 and no withdrawals, gross == net at every point.
    run_id = _make_run(api_client)
    curve = api_client.get(f"/api/v1/runs/{run_id}").json()["report"]["equity_curve"]
    assert curve and all("gross_equity" in p for p in curve)
    assert all(p["gross_equity"] >= p["equity"] for p in curve)
    assert curve[-1]["gross_equity"] == pytest.approx(curve[-1]["equity"])


def test_benchmark_endpoint(api_client: TestClient):
    run_id = _make_run(api_client)
    assert "NIFTY 50" in api_client.get("/api/v1/benchmarks").json()["benchmarks"]

    resp = api_client.get(f"/api/v1/runs/{run_id}/benchmark", params={"index": "NIFTY 50"})
    assert resp.status_code == 200, resp.text
    points = resp.json()["points"]
    # Aligned to the run's equity dates and normalized to the initial capital at t0.
    curve = api_client.get(f"/api/v1/runs/{run_id}").json()["report"]["equity_curve"]
    assert len(points) == len(curve)
    assert points[0]["value"] == pytest.approx(100000, rel=1e-6)

    # Unknown index rejected.
    assert api_client.get(
        f"/api/v1/runs/{run_id}/benchmark", params={"index": "FOO"}
    ).status_code == 400


def test_compare_runs(api_client: TestClient):
    r1 = _make_run(api_client, params={"capital_parts": 10, "profit_target": 0.06})
    r2 = _make_run(api_client, params={"capital_parts": 10, "profit_target": 0.10})

    resp = api_client.get("/api/v1/runs/compare", params={"ids": f"{r1},{r2}"})
    assert resp.status_code == 200, resp.text
    runs = resp.json()["runs"]
    assert [r["run_id"] for r in runs] == [r1, r2]
    # Rebased growth starts at 100 for each run.
    for r in runs:
        assert r["growth"][0]["value"] == pytest.approx(100.0)
        assert "Total Return %" in r["metrics"]

    # Bounds: need 2–5, and only backtests.
    assert api_client.get("/api/v1/runs/compare", params={"ids": str(r1)}).status_code == 422
    assert api_client.get(
        "/api/v1/runs/compare", params={"ids": f"{r1},999999"}
    ).status_code == 422


def test_run_management_lifecycle(api_client: TestClient):
    body = {
        "strategy_id": "sst_lifo",
        "name": "My backtest",
        "notes": "ramp test",
        "symbols": ["AAA"],
        "start_date": "2020-01-01",
        "end_date": "2021-12-31",
        "capital": 100000,
        "params": {"capital_parts": 10, "profit_target": 0.06},
        "tax_rate": 0.0,
    }
    run_id = api_client.post("/api/v1/backtest", json=body).json()["run_id"]

    # Name + notes persisted; appears under Active (not archived).
    active = api_client.get("/api/v1/runs?status=active").json()
    tile = next(t for t in active if t["run_id"] == run_id)
    assert tile["name"] == "My backtest"
    assert tile["notes"] == "ramp test"
    assert tile["archived"] is False
    assert tile["mode"] == "BACKTEST"

    # Rename / edit notes.
    api_client.patch(f"/api/v1/runs/{run_id}", json={"name": "Renamed", "notes": "n2"})
    detail = api_client.get(f"/api/v1/runs/{run_id}").json()
    assert detail["name"] == "Renamed" and detail["notes"] == "n2"

    # Archive -> drops out of active, shows under archived; unarchive reverses it.
    api_client.post(f"/api/v1/runs/{run_id}/archive")
    assert all(t["run_id"] != run_id for t in api_client.get("/api/v1/runs?status=active").json())
    assert any(t["run_id"] == run_id for t in api_client.get("/api/v1/runs?status=archived").json())
    api_client.post(f"/api/v1/runs/{run_id}/unarchive")
    assert any(t["run_id"] == run_id for t in api_client.get("/api/v1/runs?status=active").json())

    # Delete -> gone everywhere; the AlgoRun row is removed.
    assert api_client.delete(f"/api/v1/runs/{run_id}").status_code == 200
    assert all(t["run_id"] != run_id for t in api_client.get("/api/v1/runs").json())
    assert api_client.get(f"/api/v1/runs/{run_id}").status_code == 404


def test_sweep_batch_grouping(api_client: TestClient):
    # Simulate a sweep: N backtests sharing one batch_id (as the frontend does).
    batch = "batch-abc"
    ids = [
        _make_run(api_client, batch_id=batch, params={"capital_parts": 10, "profit_target": t})
        for t in (0.04, 0.06, 0.08)
    ]
    runs = {r["run_id"]: r for r in api_client.get("/api/v1/runs").json()}
    assert all(runs[i]["batch_id"] == batch for i in ids)
    # Detail endpoint also exposes the batch id.
    assert api_client.get(f"/api/v1/runs/{ids[0]}").json()["batch_id"] == batch


def test_runs_list_excludes_paper(api_client: TestClient):
    """The Runs list is backtests only — paper/live deployments are managed elsewhere."""
    from skas_algo.db.base import session_scope
    from skas_algo.db.enums import TradingMode
    from skas_algo.db.models import Algo, AlgoRun

    with session_scope() as db:
        algo = Algo(name="paper one", strategy_id="sst_lifo", mode=TradingMode.PAPER)
        db.add(algo)
        db.flush()
        db.add(AlgoRun(algo_id=algo.id, mode=TradingMode.PAPER))
        db.flush()
        paper_run_id = db.query(AlgoRun).filter(AlgoRun.algo_id == algo.id).one().id

    runs = api_client.get("/api/v1/runs").json()
    assert all(r["mode"] == "BACKTEST" for r in runs)
    assert all(r["run_id"] != paper_run_id for r in runs)


def test_backtest_unknown_strategy(api_client: TestClient):
    body = {
        "strategy_id": "nope",
        "symbols": ["AAA"],
        "start_date": "2020-01-01",
        "end_date": "2021-12-31",
    }
    resp = api_client.post("/api/v1/backtest", json=body)
    assert resp.status_code == 404


def test_strategy_template_lifecycle(api_client: TestClient):
    """Set a run as its strategy's template → it serves params for new backtests;
    re-setting overwrites; clearing removes. Params are copied (survive run deletion)."""
    body = {
        "strategy_id": "sst_lifo", "symbols": ["RELIANCE"], "instrument_class": "STOCK",
        "start_date": "2020-01-01", "end_date": "2020-04-30", "capital": 500000,
        "params": {"profit_target": 0.1},
    }
    run_id = api_client.post("/api/v1/backtest", json=body).json()["run_id"]

    resp = api_client.post(f"/api/v1/runs/{run_id}/set-template")
    assert resp.status_code == 200
    t = resp.json()
    assert t["strategy_id"] == "sst_lifo" and t["run_id"] == run_id
    assert t["capital"] == 500000 and t["params"]["profit_target"] == 0.1

    templates = api_client.get("/api/v1/strategies/templates").json()["templates"]
    assert templates["sst_lifo"]["run_id"] == run_id

    # Template survives deleting the source run (params were copied).
    api_client.delete(f"/api/v1/runs/{run_id}")
    templates = api_client.get("/api/v1/strategies/templates").json()["templates"]
    assert templates["sst_lifo"]["params"]["profit_target"] == 0.1

    assert api_client.delete("/api/v1/strategies/sst_lifo/template").json()["cleared"] is True
    assert api_client.get("/api/v1/strategies/templates").json()["templates"] == {}
