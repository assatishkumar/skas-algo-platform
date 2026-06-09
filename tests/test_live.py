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


def test_session_state_roundtrip():
    """A session's state can be exported and reloaded into a fresh session."""
    from skas_algo.engine.live import LiveSession
    from skas_algo.strategies.sst_lifo import SSTLifoStrategy

    def build():
        strat = SSTLifoStrategy(
            ["AAA"], initial_capital=100_000, capital_parts=10, profit_target=0.06
        )
        return LiveSession(strat, initial_capital=100_000, lookback=5, tax_rate=0.0)

    s = build()
    s.warmup({"AAA": [100.0] * 25})
    s.update_quotes({"AAA": 95.0})
    s.run_decision(date(2024, 1, 2))
    s.end_day()
    s.update_quotes({"AAA": 110.0})
    s.run_decision(date(2024, 1, 3))
    s.end_day()
    assert s.portfolio.units("AAA") > 0

    blob = s.export_state()
    s2 = build()
    s2.warmup({"AAA": [100.0] * 25})
    s2.load_state(blob)
    assert s2.portfolio.units("AAA") == s.portfolio.units("AAA")
    assert s2.portfolio.cash == s.portfolio.cash
    assert s2.strategy.tracking == s.strategy.tracking
    assert [lot.price for lot in s2.portfolio.lots("AAA")] == [
        lot.price for lot in s.portfolio.lots("AAA")
    ]


def test_recover_running_sessions(monkeypatch):
    """A running run survives a 'restart': dropped from memory, rebuilt from the DB."""
    monkeypatch.setattr("skas_algo.data.provider.get_price_loader", lambda: _flat_loader)
    fake = FakeQuoteSource()
    config = LiveConfig(
        name="recov",
        strategy_id="sst_lifo",
        symbols=["AAA"],
        capital=100_000,
        params={"capital_parts": 10, "profit_target": 0.06},
        lookback=5,
        tax_rate=0.0,
        ignore_market_hours=True,
    )
    live = manager.start(config, _flat_loader, fake)
    fake.price = 95.0
    live.refresh()
    live.run_decision(date(2024, 1, 2))
    live.end_day()
    fake.price = 110.0
    live.refresh()
    live.run_decision(date(2024, 1, 3))
    live.end_day()
    run_id = live.run_id
    units_before = live.session.portfolio.units("AAA")
    cash_before = live.session.portfolio.cash
    assert units_before > 0

    # Simulate a restart: drop the in-memory run (state remains persisted in the DB).
    manager.runs.pop(run_id)
    assert manager.get(run_id) is None

    from skas_algo.live.recovery import recover_running_sessions

    recover_running_sessions()
    recovered = manager.get(run_id)
    try:
        assert recovered is not None
        assert recovered.session.portfolio.units("AAA") == units_before
        assert recovered.session.portfolio.cash == cash_before
    finally:
        manager.stop(run_id)


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


def test_live_override_injection(api_client: TestClient):
    body = {
        "strategy_id": "sst_lifo",
        "symbols": ["AAA"],
        "capital": 100000,
        "params": {"capital_parts": 10, "profit_target": 0.06},
        "lookback": 5,
        "quote_source": "cache",
        "ignore_market_hours": True,
    }
    run_id = api_client.post("/api/v1/live/start", json=body).json()["run_id"]
    try:
        resp = api_client.post(
            f"/api/v1/live/{run_id}/overrides",
            json={
                "scope": "ALGO",
                "target": None,
                "rule": {
                    "exit": [
                        {"at_pct": 6, "action": "book", "qty_pct": 50},
                        {"action": "trail_sl", "trail_pct": 2},
                    ]
                },
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["overrides"] == 1  # injected into the running resolver
    finally:
        api_client.post(f"/api/v1/live/{run_id}/stop")
