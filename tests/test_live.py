"""Live/paper run: manager drives a session, persists trades/positions, REST + WS."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from skas_algo.api import create_app
from skas_algo.data.provider import get_available_symbols, get_price_loader
from skas_algo.db.base import session_scope
from skas_algo.db.enums import OrderSide, PositionStatus
from skas_algo.db.models import AlgoRun, BrokerAccount, Fill, Order, Position
from skas_algo.live.manager import Broadcaster, LiveConfig, manager
from skas_algo.security import encrypt


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


def test_run_analysis_surfaces_live_session_trades(api_client: TestClient):
    """A seeded/warmed forward-test holds its replayed trades only in the live session's
    memory (no persisted trade_log, no Order rows for the replay). The /runs/{id}/analysis
    endpoint must surface those in-memory transactions — otherwise the Analyze page is empty."""
    body = {
        "strategy_id": "sst_lifo",
        "name": "warmed forward test",
        "symbols": ["AAA"],
        "capital": 100000,
        "params": {"capital_parts": 10, "profit_target": 0.06},
        "lookback": 5,
        "quote_source": "cache",
        "ignore_market_hours": True,
        "auto": False,
    }
    run_id = api_client.post("/api/v1/live/start", json=body).json()["run_id"]
    try:
        live = manager.get(run_id)
        # Simulate a seed-replayed trade that exists only in the session (no Order row).
        live.session.transactions.append({
            "date": date(2026, 4, 15), "ticker": "AAA", "action": "BUY",
            "units": 10, "price": 100.0, "amount": 1000.0,
            "profit": 0.0, "pnl_pct": 0.0, "lots": 1, "tag": "STRATEGY",
        })
        j = api_client.get(f"/api/v1/runs/{run_id}/analysis").json()
        assert j["strategy_id"] == "sst_lifo" and j["instrument_class"] == "STOCK"
        assert any(t["ticker"] == "AAA" and t["date"] == "2026-04-15" for t in j["trades"])
    finally:
        api_client.post(f"/api/v1/live/{run_id}/stop")


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


# ---------------------------------------------------------------- Live page rules
def _start_equity_paper(api_client: TestClient, *, auto: bool = False) -> int:
    body = {
        "strategy_id": "custom_equity", "name": "rules-test", "symbols": ["AAA"], "capital": 100000,
        "params": {"symbol": "AAA", "qty": 5, "entry_mode": "immediate"},
        "lookback": 5, "quote_source": "cache", "ignore_market_hours": True, "auto": auto,
    }
    r = api_client.post("/api/v1/live/start", json=body)
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


def test_stop_blocked_until_positions_exited(api_client: TestClient):
    rid = _start_equity_paper(api_client)
    try:
        api_client.post(f"/api/v1/live/{rid}/refresh")
        api_client.post(f"/api/v1/live/{rid}/run-decision")  # immediate → BUY 5
        assert api_client.get(f"/api/v1/live/{rid}").json()["open_positions"] == 1
        blocked = api_client.post(f"/api/v1/live/{rid}/stop")
        assert blocked.status_code == 409  # has an open position
        api_client.post(f"/api/v1/live/{rid}/flatten")  # exit it
        assert api_client.post(f"/api/v1/live/{rid}/stop").status_code == 200  # now flat → stops
    finally:
        api_client.delete(f"/api/v1/live/{rid}")


def test_refresh_with_decide_runs_a_decision(api_client: TestClient):
    rid = _start_equity_paper(api_client)
    try:
        snap = api_client.post(f"/api/v1/live/{rid}/refresh?decide=true").json()
        assert snap["open_positions"] == 1  # refresh re-priced AND entered
    finally:
        api_client.post(f"/api/v1/live/{rid}/flatten")
        api_client.post(f"/api/v1/live/{rid}/stop")
        api_client.delete(f"/api/v1/live/{rid}")


def test_activate_restarts_a_stopped_run(api_client: TestClient):
    rid = _start_equity_paper(api_client)  # flat (no decision run yet)
    try:
        assert api_client.post(f"/api/v1/live/{rid}/stop").status_code == 200
        assert any(d["run_id"] == rid for d in api_client.get("/api/v1/live/deployments?status=stopped").json())
        assert api_client.post(f"/api/v1/live/{rid}/activate").status_code == 200
        assert api_client.get(f"/api/v1/live/{rid}").status_code == 200  # back in memory (active)
        assert api_client.post(f"/api/v1/live/{rid}/activate").status_code == 400  # already active
    finally:
        api_client.post(f"/api/v1/live/{rid}/stop")
        api_client.delete(f"/api/v1/live/{rid}")


def test_activate_preserves_trade_history(api_client: TestClient):
    """Stop must keep the session state so Activate resumes with realized P&L / trades intact."""
    rid = _start_equity_paper(api_client)
    try:
        api_client.post(f"/api/v1/live/{rid}/refresh")
        api_client.post(f"/api/v1/live/{rid}/run-decision")  # BUY 5
        api_client.post(f"/api/v1/live/{rid}/flatten")       # SELL 5 → flat, 2 transactions
        before = api_client.get(f"/api/v1/live/{rid}/trades").json()["trades"]
        assert len(before) >= 2
        assert api_client.post(f"/api/v1/live/{rid}/stop").status_code == 200
        with session_scope() as db:
            assert db.get(AlgoRun, rid).state is not None  # state kept (not nulled) for resume
        assert api_client.post(f"/api/v1/live/{rid}/activate").status_code == 200
        after = api_client.get(f"/api/v1/live/{rid}/trades").json()["trades"]
        assert len(after) == len(before)  # trade history survived the stop → activate round-trip
    finally:
        api_client.post(f"/api/v1/live/{rid}/stop")
        api_client.delete(f"/api/v1/live/{rid}")


def test_go_live_blocked_without_armed_account(api_client: TestClient):
    rid = _start_equity_paper(api_client)
    try:
        # An account with a valid session but NOT armed → blocked with the arm message.
        with session_scope() as db:
            acct = BrokerAccount(
                broker="zerodha", label="Test", api_key="k", enc_api_secret=encrypt("s"),
                session_token=encrypt("tok"), session_expires_at=datetime.now(UTC) + timedelta(hours=2),
                armed=False,
            )
            db.add(acct)
            db.flush()
            aid = acct.id
        r = api_client.post(f"/api/v1/live/{rid}/go-live", json={"broker_account_id": aid})
        assert r.status_code == 400 and "arm" in r.json()["detail"].lower()
        # Unknown account → 404.
        assert api_client.post(f"/api/v1/live/{rid}/go-live", json={"broker_account_id": 999999}).status_code == 404
    finally:
        api_client.post(f"/api/v1/live/{rid}/stop")
        api_client.delete(f"/api/v1/live/{rid}")


# --- broker-first daily bars (Part 2: decouple live index-daily from the cache) ---

def test_broker_daily_df_uses_broker_and_excludes_today():
    from skas_algo.live.manager import _broker_daily_df

    today = date.today()

    class _Adapter:
        def daily_bars(self, u, days):
            return [
                {"date": (today - timedelta(days=3)).isoformat(),
                 "open": 1, "high": 12, "low": 8, "close": 10},
                {"date": (today - timedelta(days=2)).isoformat(),
                 "open": 1, "high": 14, "low": 9, "close": 11},
                # today's forming daily candle — must be EXCLUDED (caller adds it separately)
                {"date": today.isoformat(), "open": 1, "high": 99, "low": 1, "close": 50},
            ]

    df = _broker_daily_df(_Adapter(), "NIFTY", today - timedelta(days=10), today)
    assert df is not None
    assert df["high"].tolist() == [12, 14]            # complete prior days only
    assert today.isoformat() not in list(df["date"])  # today excluded


def test_broker_daily_df_none_without_daily_bars():
    from skas_algo.live.manager import _broker_daily_df

    class _NoHist:  # e.g. Dhan — no historical API → caller uses the cache
        pass

    span = (date.today() - timedelta(days=5), date.today())
    assert _broker_daily_df(_NoHist(), "NIFTY", *span) is None
    assert _broker_daily_df(None, "NIFTY", *span) is None


def test_lot_sets_attr_routes_dict_lot_sizing():
    """The per-underlying lot-set control must route to the right attribute: momentum_theta names
    its dict `lots`, call_put_ratio_expiry names it `sets`; scalar-`lots` strategies return None
    (they use the plain scalar lots control), and equity returns None."""
    from skas_algo.live.manager import _lot_sets_attr
    from skas_algo.strategies.call_put_ratio_expiry import CallPutRatioExpiryStrategy
    from skas_algo.strategies.momentum_theta_intra import MomentumThetaGainerIntra

    mt = MomentumThetaGainerIntra(underlyings=["NIFTY", "SENSEX"], lots={"NIFTY": 2, "SENSEX": 2})
    cp = CallPutRatioExpiryStrategy(underlyings=["SENSEX"], sets={"SENSEX": 3})
    assert _lot_sets_attr(mt) == "lots"
    assert _lot_sets_attr(cp) == "sets"
    assert _lot_sets_attr(type("Scalar", (), {"lots": 10})()) is None
    assert _lot_sets_attr(None) is None


def test_update_params_hot_edits_running_strategy():
    """Edit-params: rebuild the strategy with ctor logic re-run, state carried over,
    snapshot persisted (a restart keeps the edit); infra + unknown keys rejected."""
    from skas_algo.db.models import Algo

    fake = FakeQuoteSource()
    config = LiveConfig(
        name="edit-params-test",
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
        # Build some in-flight state: dip → tracking, breakout → BUY.
        fake.price = 95.0
        live.refresh()
        live.run_decision(date(2024, 1, 2))
        live.end_day()
        fake.price = 110.0
        live.refresh()
        buys = live.run_decision(date(2024, 1, 3))
        live.end_day()
        assert any(e["action"] == "BUY" for e in buys)
        old_strategy = live.session.strategy
        old_tracking = dict(old_strategy.tracking)

        # Infra keys are refused outright…
        with pytest.raises(ValueError):
            live.update_params({"mode": "LIVE", "capital": 1})
        # …and so are typos (strategy_kwargs would silently drop them → phantom "applied").
        with pytest.raises(ValueError):
            live.update_params({"profit_targett": 0.09})

        result = live.update_params({"profit_target": 0.09, "capital_parts": 8})
        assert sorted(result["applied"]) == ["capital_parts", "profit_target"]

        s = live.session.strategy
        assert s is not old_strategy  # rebuilt, not mutated
        assert s.profit_target == 0.09
        # Ctor-derived field recomputed — proves ctor logic re-ran (setattr would miss it).
        assert s.allocation_amount == pytest.approx(100_000 / 8)
        # State carried over: the tracked/held book survives the swap.
        assert s.tracking == old_tracking

        # Persisted: recovery would rebuild with the SAME edited params.
        with session_scope() as db:
            run = db.get(AlgoRun, live.run_id)
            assert run.params_snapshot["profit_target"] == 0.09
            assert run.params_snapshot["capital_parts"] == 8
            algo = db.get(Algo, live.algo_id)
            assert algo.params["profit_target"] == 0.09

        # The new target actually drives decisions: +8% (< old 6%→sell? no — NEW 9%) holds…
        fake.price = 118.0  # +~7.3% from the 110 entry
        live.refresh()
        events = live.run_decision(date(2024, 1, 4))
        live.end_day()
        assert not any(e["action"] == "SELL" for e in events)
        # …and +10% sells under the new 9% target.
        fake.price = 122.0
        live.refresh()
        events = live.run_decision(date(2024, 1, 5))
        live.end_day()
        assert any(e["action"] == "SELL" for e in events)
    finally:
        manager.stop(live.run_id)


def test_update_params_route():
    fake = FakeQuoteSource()
    config = LiveConfig(
        name="edit-params-route",
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
        client = TestClient(create_app())
        r = client.post(f"/api/v1/live/{live.run_id}/params",
                        json={"params": {"profit_target": 0.08}})
        assert r.status_code == 200
        body = r.json()
        assert body["applied"] == ["profit_target"]
        assert live.session.strategy.profit_target == 0.08
        # Snapshot (the modal's data source) carries the updated params.
        snap = client.get(f"/api/v1/live/{live.run_id}").json()
        assert snap["params"]["profit_target"] == 0.08
        # Infra key → 422, not silently ignored.
        r = client.post(f"/api/v1/live/{live.run_id}/params",
                        json={"params": {"quote_source": "zerodha"}})
        assert r.status_code == 422
    finally:
        manager.stop(live.run_id)


def test_summary_prefers_real_runs():
    """/live/summary aggregates the REAL (LIVE-mode) fleet when any is running — the VPS
    Home page showed paper stats while real money traded (2026-07-31) — else paper."""
    fake = FakeQuoteSource()
    base = dict(symbols=["AAA"], capital=100_000,
                params={"capital_parts": 10, "profit_target": 0.06},
                lookback=5, tax_rate=0.0, ignore_market_hours=True)
    paper = manager.start(
        LiveConfig(name="sum-paper", strategy_id="sst_lifo", mode="PAPER", **base),
        _flat_loader, fake)
    # LIVE mode WITHOUT an armed account keeps PaperBroker (the §1 gate) — safe in tests.
    real = manager.start(
        LiveConfig(name="sum-real", strategy_id="sst_lifo", mode="LIVE", **base),
        _flat_loader, fake)
    client = TestClient(create_app())
    try:
        s = client.get("/api/v1/live/summary").json()
        assert s["basis"] == "live"
        manager.stop(real.run_id)
        s = client.get("/api/v1/live/summary").json()
        assert s["basis"] == "paper"
    finally:
        for lr in (paper, real):
            try:
                manager.stop(lr.run_id)
            except Exception:
                pass


def test_snapshot_offers_params_added_after_deploy():
    """The Edit-params surface must include ctor knobs the run never stored — a param
    added to a strategy AFTER a deploy started was invisible, so it could never be
    switched on for that run (owner report on runs 211/212, 2026-08-07)."""
    fake = FakeQuoteSource()
    config = LiveConfig(
        name="surface-test",
        strategy_id="sst_lifo",
        symbols=["AAA"],
        capital=100_000,
        params={"capital_parts": 10},   # profit_target deliberately NOT stored
        lookback=5,
        tax_rate=0.0,
        ignore_market_hours=True,
    )
    live = manager.start(config, _flat_loader, fake)
    try:
        snap = live.snapshot()
        # stored params stay exactly what the run set…
        assert "profit_target" not in snap["params"]
        # …but the editable surface offers it at the ctor default, flagged as defaulted.
        assert snap["editable_params"]["capital_parts"] == 10
        assert snap["editable_params"]["profit_target"] == 0.06
        assert "profit_target" in snap["param_defaulted"]
        assert "capital_parts" not in snap["param_defaulted"]
        # Infra never leaks into the editable surface.
        for k in ("capital", "symbols", "mode", "quote_source"):
            assert k not in snap["editable_params"]

        # And the offered knob actually applies.
        live.update_params({"profit_target": 0.09})
        assert live.session.strategy.profit_target == 0.09
        assert live.snapshot()["editable_params"]["profit_target"] == 0.09
        assert "profit_target" not in live.snapshot()["param_defaulted"]
    finally:
        manager.stop(live.run_id)
