"""Donchian Strangle Monthly trade endpoints: /trade/options/donchian/{analyze,portfolio,deploy}.
Hermetic — a fake broker adapter for the live chain + a fake cache (OHLC + option series)."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from skas_algo.api import create_app
from skas_algo.data.provider import get_available_symbols, get_data_cache, get_price_loader

TODAY = date.today()
EXPIRY = TODAY + timedelta(days=10)
SPOTS = {"AAA": 1000.0, "NIFTY": 25000.0, "NIFTY 50": 25000.0}


def _spot(sym: str) -> float:
    return SPOTS.get(sym.upper(), 1000.0)


def _prem(strike, dte, spot, right):
    dist = (strike - spot) if right == "CE" else (spot - strike)
    return round(spot * 0.03 * math.exp(-dist / (spot * 0.8)) * max(0.05, dte / 30.0), 2)


def _flat_loader(_sym, _start, _end):
    dates = pd.bdate_range(end=TODAY, periods=40)
    closes = [100.0] * len(dates)
    return pd.DataFrame({"date": dates, "open": closes, "high": closes, "low": closes, "close": closes, "volume": 1})


class FakeSD:
    """Cache stand-in: OHLC per name + option chains/series per underlying."""

    def _strikes(self, u):
        s = _spot(u)
        step = 50 if u.upper() in ("NIFTY", "NIFTY 50") else 10
        base = round(s * 0.8 / step) * step
        return [float(base + step * i) for i in range(0, 80)]

    def get_prices(self, symbol, start_date=None, end_date=None, asset_type="stock"):
        s = _spot(symbol)
        dates = [d.date() for d in pd.bdate_range(end=TODAY, periods=160)]
        # A small shared wiggle → names co-move with NIFTY, so HV and beta are well-defined.
        closes = [s * (1 + 0.005 * math.sin(i / 3.0)) for i in range(len(dates))]
        return pd.DataFrame({"date": dates, "open": closes, "high": [c * 1.02 for c in closes],
                             "low": [c * 0.98 for c in closes], "close": closes})

    def get_option_chain(self, underlying, on_date, expiry=None):
        s = _spot(underlying)
        rows = [dict(trade_date=on_date, symbol=underlying.upper(), expiry_date=EXPIRY, strike_price=k,
                     option_type=r, close=_prem(k, (EXPIRY - on_date).days, s, r),
                     settle_price=_prem(k, (EXPIRY - on_date).days, s, r), open_interest=1000)
                for k in self._strikes(underlying) for r in ("CE", "PE")]
        return pd.DataFrame(rows)

    def get_option_series(self, underlying, expiry, strike, option_type, start_date=None, end_date=None):
        s = _spot(underlying)
        dates = [d.date() for d in pd.bdate_range(end=EXPIRY, periods=60)]
        return pd.DataFrame([{"trade_date": d, "close": _prem(float(strike), (EXPIRY - d).days, s, option_type.upper())}
                             for d in dates])


class FakeAdapter:
    sd = FakeSD()

    def option_expiries(self, underlying):
        return [EXPIRY.isoformat()]

    def live_option_chain(self, underlying, expiry, window: int = 40):
        s = _spot(underlying)
        strikes = self.sd._strikes(underlying)
        rows = [{"strike": k,
                 "ce": {"ltp": _prem(k, (EXPIRY - TODAY).days, s, "CE"), "oi": 1000, "bid": None, "ask": None},
                 "pe": {"ltp": _prem(k, (EXPIRY - TODAY).days, s, "PE"), "oi": 1000, "bid": None, "ask": None}}
                for k in strikes]
        lot = 50 if underlying.upper() == "NIFTY" else 100
        return {"spot": s, "atm_strike": min(strikes, key=lambda k: abs(k - s)), "lot_size": lot, "rows": rows}

    def basket_margin(self, legs):
        return 123456.0


@pytest.fixture
def api_client(monkeypatch) -> TestClient:
    monkeypatch.setattr("skas_algo.api.routes.trade._live_adapter", lambda *_a, **_k: FakeAdapter())
    app = create_app()
    app.dependency_overrides[get_data_cache] = lambda: FakeSD()
    app.dependency_overrides[get_price_loader] = lambda: _flat_loader
    app.dependency_overrides[get_available_symbols] = lambda: {"AAA", "NIFTY"}
    return TestClient(app)


def test_donchian_analyze(api_client: TestClient):
    body = {"broker_account_id": 1,
            "names": [{"symbol": "AAA", "atm_iv": 40, "ivp": 70, "event": None},
                      {"symbol": "BBB", "atm_iv": 40, "ivp": 10, "event": None}],  # BBB: IVP<50 → filtered
            "sell_expiry": EXPIRY.isoformat()}
    r = api_client.post("/api/v1/trade/options/donchian/analyze", json=body)
    assert r.status_code == 200, r.text
    rows = {row["symbol"]: row for row in r.json()["rows"]}
    assert rows["AAA"]["status"] in ("strangle", "CE-only", "PE-only")
    assert rows["AAA"].get("strike_step") == 10 and rows["AAA"].get("beta") is not None
    assert rows["BBB"]["status"] == "excluded:filter"


def test_donchian_portfolio(api_client: TestClient):
    body = {"broker_account_id": 1, "sell_expiry": EXPIRY.isoformat(),
            "selected": [{"symbol": "AAA", "spot": 1000.0, "lot_size": 100, "lots": 20,
                          "ce": {"strike": 1050, "premium": 25.0}, "pe": {"strike": 950, "premium": 22.0}}]}
    r = api_client.post("/api/v1/trade/options/donchian/portfolio", json=body)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["agg_notional"] == 2_000_000 and p["basket_margin"] == 123456.0
    assert p["hedge"]["nifty_lots"] >= 1


LEGS = [
    {"underlying": "AAA", "right": "CE", "strike": 1050, "side": "sell", "lots": 1, "spot": 1000, "lot_size": 100, "strike_step": 10},
    {"underlying": "AAA", "right": "PE", "strike": 950, "side": "sell", "lots": 1, "spot": 1000, "lot_size": 100, "strike_step": 10},
    {"underlying": "NIFTY", "right": "CE", "strike": 26000, "side": "buy", "lots": 1, "spot": 25000, "lot_size": 50},
    {"underlying": "NIFTY", "right": "PE", "strike": 24000, "side": "buy", "lots": 1, "spot": 25000, "lot_size": 50},
]


def test_donchian_deploy(api_client: TestClient, monkeypatch):
    monkeypatch.setattr("skas_algo.data.provider.get_data_cache", lambda: FakeSD())
    body = {"name": "basket", "sell_expiry": EXPIRY.isoformat(), "legs": LEGS, "capital": 5_000_000,
            "mode": "PAPER", "quote_source": "cache", "auto": False}
    r = api_client.post("/api/v1/trade/options/donchian/deploy", json=body)
    assert r.status_code == 200, r.text
    rid = r.json()["run_id"]
    try:
        mine = next(d for d in api_client.get("/api/v1/live/deployments").json() if d["run_id"] == rid)
        assert mine["strategy_id"] == "donchian_strangle_monthly" and mine["mode"] == "PAPER"
        dec = api_client.post(f"/api/v1/live/{rid}/run-decision").json()
        acts = {t["action"] for t in dec["trades"]}
        assert "SHORT" in acts and "BUY" in acts  # stock shorts + NIFTY hedge longs
    finally:
        api_client.post(f"/api/v1/live/{rid}/stop")


def test_donchian_deploy_requires_legs(api_client: TestClient):
    r = api_client.post("/api/v1/trade/options/donchian/deploy", json={
        "name": "empty", "sell_expiry": EXPIRY.isoformat(), "legs": [], "mode": "PAPER", "quote_source": "cache"})
    assert r.status_code == 422


def test_donchian_deploy_capital_guard(api_client: TestClient, monkeypatch):
    monkeypatch.setattr("skas_algo.data.provider.get_data_cache", lambda: FakeSD())
    body = {"name": "underfunded", "sell_expiry": EXPIRY.isoformat(), "legs": LEGS, "capital": 1000,
            "mode": "PAPER", "quote_source": "cache", "auto": False}  # no session → model margin on shorts
    r = api_client.post("/api/v1/trade/options/donchian/deploy", json=body)
    assert r.status_code == 422 and "margin" in r.json()["detail"].lower()
