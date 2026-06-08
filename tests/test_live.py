"""Live/paper run: manager drives a session, persists trades/positions, REST + WS."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from skas_algo.api import create_app
from skas_algo.data.provider import get_available_symbols, get_price_loader
from skas_algo.db.base import session_scope
from skas_algo.db.enums import OrderSide, PositionStatus
from skas_algo.db.models import AlgoRun, Fill, Order, Position
from skas_algo.live.manager import Broadcaster, LiveConfig, manager


class FakeQuoteSource:
    def __init__(self):
        self.price = 100.0

    def get_quotes(self, symbols):
        return {s: self.price for s in symbols}


def _flat_loader(_sym, _start, _end):
    # 25 flat closes at 100 — seeds rolling levels for warmup.
    dates = pd.bdate_range("2023-11-01", periods=25)
    closes = [100.0] * 25
    return pd.DataFrame(
        {"date": dates, "open": closes, "high": closes, "low": closes, "close": closes, "volume": 1}
    )


def test_broadcaster_pubsub():
    b = Broadcaster()
    q = b.subscribe()
    b.publish({"type": "x", "v": 1})
    assert q.get_nowait() == {"type": "x", "v": 1}
    b.unsubscribe(q)
    b.publish({"type": "y"})  # no subscribers -> no error
    assert q.empty()


def test_paper_run_persists_trades_and_positions():
    fake = FakeQuoteSource()
    config = LiveConfig(
        name="paper-test",
        strategy_id="sst_lifo",
        symbols=["AAA"],
        capital=100_000,
        params={"capital_parts": 10, "profit_target": 0.06},
        lookback=5,
        tax_rate=0.0,
        ignore_market_hours=True,
    )
    live = manager.start(config, _flat_loader, fake)
    try:
        days = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
        # Day 1: dip below the 5-day low -> start tracking (no buy).
        fake.price = 95.0
        live.refresh()
        live.run_decision(days[0])
        live.end_day()
        # Day 2: breakout above the high -> BUY.
        fake.price = 110.0
        live.refresh()
        buy_events = live.run_decision(days[1])
        live.end_day()
        # Day 3: up >6% from entry -> SELL.
        fake.price = 120.0
        live.refresh()
        sell_events = live.run_decision(days[2])
        live.end_day()

        assert any(e["action"] == "BUY" for e in buy_events)
        assert any(e["action"] == "SELL" for e in sell_events)

        with session_scope() as db:
            orders = db.query(Order).filter(Order.algo_id == live.algo_id).all()
            sides = {o.side for o in orders}
            assert OrderSide.BUY in sides and OrderSide.SELL in sides
            assert db.query(Fill).count() >= 2
            # Position opened then closed (all units sold).
            pos = db.query(Position).filter(Position.algo_id == live.algo_id).all()
            assert pos and all(p.status == PositionStatus.CLOSED for p in pos)
    finally:
        manager.stop(live.run_id)

    with session_scope() as db:
        run = db.get(AlgoRun, live.run_id)
        assert run.stopped_at is not None
        assert run.metrics["metrics"]["Total Trades"] >= 1


@pytest.fixture
def api_client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_price_loader] = lambda: _flat_loader
    app.dependency_overrides[get_available_symbols] = lambda: {"AAA"}
    return TestClient(app)


def test_live_rest_lifecycle(api_client: TestClient):
    body = {
        "strategy_id": "sst_lifo",
        "symbols": ["AAA"],
        "capital": 100000,
        "params": {"capital_parts": 10, "profit_target": 0.06},
        "lookback": 5,
        "quote_source": "cache",
        "ignore_market_hours": True,
        "auto": False,
    }
    start = api_client.post("/api/v1/live/start", json=body)
    assert start.status_code == 200, start.text
    run_id = start.json()["run_id"]
    try:
        assert any(r["run_id"] == run_id for r in api_client.get("/api/v1/live").json())
        assert api_client.get(f"/api/v1/live/{run_id}").json()["status"] == "running"
        assert api_client.post(f"/api/v1/live/{run_id}/refresh").status_code == 200
    finally:
        assert api_client.post(f"/api/v1/live/{run_id}/stop").status_code == 200
    # After stop it's removed from the registry.
    assert api_client.get(f"/api/v1/live/{run_id}").status_code == 404
