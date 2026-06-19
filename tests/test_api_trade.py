"""Trade deploy endpoints: build a custom position from the Trade UI's structured input and
deploy it through the same path as /live/start. Hermetic (cache quotes, fake option source)."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from skas_algo.api import create_app
from skas_algo.data.provider import get_available_symbols, get_price_loader

TODAY = date.today()
EXPIRY = TODAY + timedelta(days=10)
SPOT = 25000.0


def _flat_loader(_sym, _start, _end):
    dates = pd.bdate_range(end=TODAY, periods=40)
    closes = [100.0] * len(dates)
    return pd.DataFrame(
        {"date": dates, "open": closes, "high": closes, "low": closes, "close": closes, "volume": 1}
    )


def _prem(strike, dte, spot, right="CE"):
    dist = (strike - spot) if right == "CE" else (spot - strike)
    return round(100.0 * math.exp(-dist / 800.0) * max(0.05, dte / 30.0), 2)


class FakeOptSD:
    strikes = [24000.0 + 50 * i for i in range(0, 60)]

    def get_prices(self, symbol, start_date=None, end_date=None, asset_type="stock"):
        dates = [d.date() for d in pd.bdate_range(end=EXPIRY + timedelta(days=5), periods=90)]
        return pd.DataFrame({"date": dates, "close": [SPOT] * len(dates)})

    def get_option_chain(self, underlying, on_date, expiry=None):
        rows = [dict(trade_date=on_date, symbol="NIFTY", expiry_date=EXPIRY, strike_price=k,
                     option_type=r, close=_prem(k, (EXPIRY - on_date).days, SPOT, r),
                     settle_price=_prem(k, (EXPIRY - on_date).days, SPOT, r), open_interest=1000)
                for k in self.strikes for r in ("CE", "PE")]
        return pd.DataFrame(rows)

    def get_option_series(self, underlying, expiry, strike, option_type, start_date=None, end_date=None):
        dates = [d.date() for d in pd.bdate_range(end=EXPIRY, periods=60)]
        return pd.DataFrame([{"trade_date": d,
                              "close": _prem(float(strike), (EXPIRY - d).days, SPOT, option_type.upper())}
                             for d in dates])


@pytest.fixture
def api_client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_price_loader] = lambda: _flat_loader
    app.dependency_overrides[get_available_symbols] = lambda: {"AAA", "NIFTY"}
    return TestClient(app)


def test_deploy_equity_trade(api_client: TestClient):
    body = {
        "name": "my eq trade", "symbol": "AAA", "qty": 10, "capital": 100000,
        "entry_mode": "immediate", "target_pct": 10, "stop_pct": 5,
        "mode": "PAPER", "quote_source": "cache", "auto": False,
    }
    r = api_client.post("/api/v1/trade/equity/deploy", json=body)
    assert r.status_code == 200, r.text
    rid = r.json()["run_id"]
    try:
        mine = next(d for d in api_client.get("/api/v1/live/deployments").json() if d["run_id"] == rid)
        assert mine["strategy_id"] == "custom_equity" and mine["mode"] == "PAPER"
        # Immediate entry: a single decision buys the configured quantity.
        api_client.post(f"/api/v1/live/{rid}/refresh")
        dec = api_client.post(f"/api/v1/live/{rid}/run-decision").json()
        assert any(t["action"] == "BUY" and t["units"] == 10 for t in dec["trades"])
    finally:
        api_client.post(f"/api/v1/live/{rid}/stop")


def test_deploy_option_trade(api_client: TestClient, monkeypatch):
    monkeypatch.setattr("skas_algo.data.provider.get_data_cache", lambda: FakeOptSD())
    body = {
        "name": "my call spread", "underlying": "NIFTY", "expiry": EXPIRY.isoformat(),
        "legs": [{"right": "CE", "strike": 25000, "side": "sell", "lots": 1},
                 {"right": "CE", "strike": 25200, "side": "buy", "lots": 1}],
        "capital": 1000000, "target_pct": 50, "mode": "PAPER", "quote_source": "cache", "auto": False,
    }
    r = api_client.post("/api/v1/trade/options/deploy", json=body)
    assert r.status_code == 200, r.text
    rid = r.json()["run_id"]
    try:
        mine = next(d for d in api_client.get("/api/v1/live/deployments").json() if d["run_id"] == rid)
        assert mine["strategy_id"] == "custom_options" and mine["mode"] == "PAPER"
        # A decision enters both legs: a short (sell 25000 CE) and a long (buy 25200 CE).
        dec = api_client.post(f"/api/v1/live/{rid}/run-decision").json()
        acts = {t["action"] for t in dec["trades"]}
        assert "SHORT" in acts and "BUY" in acts
    finally:
        api_client.post(f"/api/v1/live/{rid}/stop")


def test_deploy_option_trade_requires_legs(api_client: TestClient):
    r = api_client.post("/api/v1/trade/options/deploy", json={
        "name": "empty", "underlying": "NIFTY", "expiry": EXPIRY.isoformat(), "legs": [],
        "mode": "PAPER", "quote_source": "cache", "auto": False,
    })
    assert r.status_code == 422
