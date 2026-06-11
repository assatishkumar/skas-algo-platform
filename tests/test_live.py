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


def test_promote_quote_source_revives_cache_fallback(monkeypatch):
    """A cache-fallback run wanting Zerodha is promoted to live quotes once a session exists."""
    from datetime import UTC, datetime, timedelta

    from skas_algo.brokers.zerodha import ZerodhaAdapter
    from skas_algo.db.base import session_scope as scope
    from skas_algo.db.models import BrokerAccount
    from skas_algo.live.manager import LiveConfig, LiveRun, manager
    from skas_algo.live.quotes import ZerodhaQuoteSource
    from skas_algo.security import encrypt
    from skas_algo.services import broker as broker_svc

    with scope() as s:
        acct = BrokerAccount(broker="zerodha", label="promo", user_id="AB1",
                             enc_api_secret=encrypt("sec"), api_key="k")
        s.add(acct)
        s.flush()
        acct_id = acct.id

    cfg = LiveConfig(name="promo", strategy_id="sst_lifo", symbols=["AAA"], capital=100_000,
                     quote_source="zerodha", broker_account_id=acct_id)

    class _StubSession:
        excluded_symbols = []

        def snapshot(self):
            return {}

    live = LiveRun(9911, 1, cfg, session=_StubSession(), quote_source=FakeQuoteSource(),
                   broadcaster=manager.broadcaster)
    live.on_cache_fallback = True
    manager.runs[9911] = live
    try:
        monkeypatch.setattr(broker_svc, "has_valid_session", lambda a: True)
        monkeypatch.setattr(broker_svc, "make_adapter", lambda a: object())
        with scope() as s:
            assert manager.promote_quote_source(9911, s) is True
        assert live.on_cache_fallback is False
        assert isinstance(live.quote_source, ZerodhaQuoteSource)
    finally:
        manager.runs.pop(9911, None)


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


def test_start_ignores_backtest_bookkeeping_params():
    """Forward-testing replays a backtest's params, which carry universe/start_date/
    end_date bookkeeping. start() must drop those rather than pass them to the strategy
    constructor (which would collide with the explicit universe= arg)."""
    config = LiveConfig(
        name="from-backtest",
        strategy_id="sst_fifo",
        symbols=["AAA"],
        capital=100_000,
        params={
            "capital_parts": 10,
            "profit_target_1": 0.10,
            "universe": "nifty_200",
            "start_date": "2020-01-01",
            "end_date": "2024-01-01",
        },
        lookback=5,
        ignore_market_hours=True,
    )
    live = manager.start(config, _flat_loader, FakeQuoteSource())
    try:
        assert live.run_id is not None
    finally:
        manager.stop(live.run_id)


def test_excluded_symbol_blocks_new_entry():
    """An excluded symbol gets no new entries even on a breakout that would buy."""
    fake = FakeQuoteSource()
    config = LiveConfig(
        name="excl-test",
        strategy_id="sst_lifo",
        symbols=["AAA"],
        capital=100_000,
        params={"capital_parts": 10, "profit_target": 0.06},
        lookback=5,
        tax_rate=0.0,
        ignore_market_hours=True,
        excluded_symbols=["AAA"],
    )
    live = manager.start(config, _flat_loader, fake)
    try:
        # Same dip-then-breakout sequence that buys in the persists test...
        fake.price = 95.0
        live.refresh()
        live.run_decision(date(2024, 1, 2))
        live.end_day()
        fake.price = 110.0
        live.refresh()
        events = live.run_decision(date(2024, 1, 3))
        # ...but AAA is excluded, so no BUY is produced.
        assert not any(e["action"] == "BUY" for e in events)
        row = next(r for r in live.session.watchlist() if r["symbol"] == "AAA")
        assert row["excluded"] is True
    finally:
        manager.stop(live.run_id)


def test_update_controls_toggles_and_persists():
    """update_controls edits the live config, exclusion set, and params_snapshot."""
    config = LiveConfig(
        name="ctrl-test",
        strategy_id="sst_lifo",
        symbols=["AAA", "BBB"],
        capital=100_000,
        params={"capital_parts": 10, "profit_target": 0.06},
        lookback=5,
        ignore_market_hours=False,
        refresh_seconds=30,
    )
    live = manager.start(config, _flat_loader, FakeQuoteSource())
    try:
        manager.update_controls(
            live.run_id,
            ignore_market_hours=True,
            refresh_seconds=120,
            excluded_symbols=["bbb"],  # lower-case → normalized
        )
        assert live.config.ignore_market_hours is True
        assert live.config.refresh_seconds == 120
        assert live.session.excluded_symbols == ["BBB"]
        with session_scope() as db:
            snap = db.get(AlgoRun, live.run_id).params_snapshot
            assert snap["ignore_market_hours"] is True
            assert snap["refresh_seconds"] == 120
            assert snap["excluded_symbols"] == ["BBB"]
    finally:
        manager.stop(live.run_id)


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


def test_deployment_lifecycle(api_client: TestClient):
    body = {
        "strategy_id": "sst_lifo",
        "name": "My SST forward test",
        "notes": "testing the dip-breakout on a few names",
        "symbols": ["AAA"],
        "capital": 100000,
        "params": {"capital_parts": 10, "profit_target": 0.06},
        "lookback": 5,
        "quote_source": "cache",
        "ignore_market_hours": True,
    }
    run_id = api_client.post("/api/v1/live/start", json=body).json()["run_id"]

    # Appears under Active with name + notes.
    active = api_client.get("/api/v1/live/deployments?status=active").json()
    tile = next(t for t in active if t["run_id"] == run_id)
    assert tile["name"] == "My SST forward test"
    assert tile["notes"].startswith("testing")
    assert tile["status"] == "active"

    # Edit name/notes.
    api_client.patch(f"/api/v1/live/{run_id}", json={"name": "Renamed", "notes": "n2"})
    assert api_client.get(f"/api/v1/live/{run_id}").json()["name"] == "Renamed"

    # Stop -> moves to Stopped.
    api_client.post(f"/api/v1/live/{run_id}/stop")
    stopped = api_client.get("/api/v1/live/deployments?status=stopped").json()
    assert any(t["run_id"] == run_id for t in stopped)

    # Archive -> Archived; unarchive -> back to Stopped.
    api_client.post(f"/api/v1/live/{run_id}/archive")
    assert any(
        t["run_id"] == run_id
        for t in api_client.get("/api/v1/live/deployments?status=archived").json()
    )
    api_client.post(f"/api/v1/live/{run_id}/unarchive")
    assert any(
        t["run_id"] == run_id
        for t in api_client.get("/api/v1/live/deployments?status=stopped").json()
    )

    # Delete -> gone everywhere, and the AlgoRun row is removed.
    assert api_client.delete(f"/api/v1/live/{run_id}").status_code == 200
    assert all(t["run_id"] != run_id for t in api_client.get("/api/v1/live/deployments").json())
    with session_scope() as s:
        assert s.get(AlgoRun, run_id) is None


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
