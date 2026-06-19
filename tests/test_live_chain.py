"""Live option chain via the Zerodha adapter (real-time premiums) + the /data/options/live/*
endpoints. Uses a fake Kite client / fake adapter — no network, no real session."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from skas_algo.api import create_app
from skas_algo.brokers.zerodha import ZerodhaAdapter, ZerodhaCredentials

EXP = (date.today() + timedelta(days=20))
EXP_ISO = EXP.isoformat()


class FakeKite:
    def instruments(self, segment):
        rows = []
        for name, lot, lo, hi, step in [("NIFTY", 65, 22800, 23200, 50),
                                        ("BAJFINANCE", 125, 900, 1000, 20)]:
            k = lo
            while k <= hi:
                for it in ("CE", "PE"):
                    rows.append({"name": name, "expiry": EXP, "strike": float(k),
                                 "instrument_type": it, "tradingsymbol": f"{name}{k}{it}",
                                 "lot_size": lot})
                k += step
        return rows

    def ltp(self, keys):
        prices = {"NSE:NIFTY 50": 23000.0, "NSE:BAJFINANCE": 950.0}
        return {k: {"last_price": prices[k]} for k in keys if k in prices}

    def quote(self, keys):
        return {k: {"last_price": 100.0, "oi": 12345, "ohlc": {"close": 98.0}} for k in keys}


def _adapter():
    return ZerodhaAdapter(ZerodhaCredentials("key", "secret"), kite=FakeKite())


def test_adapter_lists_underlyings_and_expiries():
    a = _adapter()
    unders = a.option_underlyings()
    assert "NIFTY" in unders and "BAJFINANCE" in unders
    assert EXP_ISO in a.option_expiries("NIFTY")


def test_adapter_live_chain_premiums_lot_and_spot():
    a = _adapter()
    ch = a.live_option_chain("NIFTY", EXP_ISO, window=2)
    assert ch["spot"] == 23000.0 and ch["lot_size"] == 65 and ch["atm_strike"] == 23000.0
    row = next(r for r in ch["rows"] if r["strike"] == 23000.0)
    assert row["ce"]["ltp"] == 100.0 and row["ce"]["oi"] == 12345 and row["pe"]["ltp"] == 100.0


def test_adapter_live_chain_stock_uses_own_spot():
    a = _adapter()
    ch = a.live_option_chain("BAJFINANCE", EXP_ISO)
    assert ch["spot"] == 950.0 and ch["lot_size"] == 125


def test_adapter_live_chain_unknown_contract_is_none():
    assert _adapter().live_option_chain("NIFTY", "2099-01-01") is None


# ----------------------------------------------------------------- endpoints
class FakeAdapter:
    def option_underlyings(self):
        return ["NIFTY", "BAJFINANCE"]

    def option_expiries(self, underlying):
        return [EXP_ISO]

    def live_option_chain(self, underlying, expiry, window=25):
        return {"spot": 23000.0, "atm_strike": 23000.0, "lot_size": 65,
                "rows": [{"strike": 23000.0,
                          "ce": {"ltp": 100.0, "close": 98.0, "oi": 1, "change_in_oi": None},
                          "pe": {"ltp": 90.0, "close": 88.0, "oi": 2, "change_in_oi": None}}]}


@pytest.fixture
def api_client(monkeypatch) -> TestClient:
    # Bypass DB/session resolution — return a fake adapter directly.
    monkeypatch.setattr("skas_algo.api.routes.data._live_adapter", lambda bid, db: FakeAdapter())
    return TestClient(create_app())


def test_live_chain_endpoints(api_client: TestClient):
    u = api_client.get("/api/v1/data/options/live/underlyings?broker_account_id=1").json()
    assert "NIFTY" in u["underlyings"]
    e = api_client.get(f"/api/v1/data/options/live/NIFTY/expiries?broker_account_id=1").json()
    assert e["expiries"] == [EXP_ISO]
    c = api_client.get(f"/api/v1/data/options/live/NIFTY/chain?expiry={EXP_ISO}&broker_account_id=1").json()
    assert c["live"] is True and c["spot"] == 23000.0 and c["lot_size"] == 65
    assert c["rows"][0]["ce"]["ltp"] == 100.0
