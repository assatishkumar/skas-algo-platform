"""Broker account API: encrypted storage, login-url, request-token exchange, arming."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

import pytest

from skas_algo.brokers.base import Session as BrokerSession
from skas_algo.brokers.zerodha import BrokerLoginError
from skas_algo.data.provider import get_available_symbols
from skas_algo.db.base import session_scope
from skas_algo.db.models import BrokerAccount
from skas_algo.security import decrypt, encrypt
from skas_algo.services import broker as broker_svc

CONNECT = {
    "broker": "zerodha",
    "label": "primary",
    "api_key": "apikey",
    "api_secret": "supersecret",
    "user_id": "AB1234",
}


def test_connect_stores_encrypted_and_hides_secrets(client: TestClient):
    resp = client.post("/api/v1/brokers", json=CONNECT)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    account_id = body["id"]
    assert body["armed"] is False and body["has_session"] is False
    assert "api_secret" not in body  # never echoed back

    with session_scope() as s:
        acct = s.get(BrokerAccount, account_id)
        assert acct.enc_api_secret != "supersecret"
        assert decrypt(acct.enc_api_secret) == "supersecret"


def test_login_url(client: TestClient):
    account_id = client.post("/api/v1/brokers", json={**CONNECT, "label": "url-test"}).json()["id"]
    resp = client.get(f"/api/v1/brokers/{account_id}/login-url")
    assert resp.status_code == 200
    assert "api_key=apikey" in resp.json()["login_url"]


def test_arm_disarm(client: TestClient):
    account_id = client.post("/api/v1/brokers", json={**CONNECT, "label": "arm"}).json()["id"]
    assert client.post(f"/api/v1/brokers/{account_id}/arm").json()["armed"] is True
    assert client.post(f"/api/v1/brokers/{account_id}/disarm").json()["armed"] is False


def test_login_exchanges_request_token(client: TestClient, monkeypatch):
    account_id = client.post("/api/v1/brokers", json={**CONNECT, "label": "login"}).json()["id"]

    class FakeAdapter:
        def exchange_request_token(self, token):
            assert token == "RT123"
            return BrokerSession(
                access_token="acc-tok", expires_at=datetime.now(UTC) + timedelta(hours=12)
            )

    monkeypatch.setattr(broker_svc, "make_adapter", lambda account: FakeAdapter())

    resp = client.post(f"/api/v1/brokers/{account_id}/login", json={"request_token": "RT123"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["has_session"] is True

    with session_scope() as s:
        assert decrypt(s.get(BrokerAccount, account_id).session_token) == "acc-tok"


# ---- shared Kite session (data + trading on one login) --------------------------


class _FakeKite:
    def __init__(self):
        self.token = None

    def set_access_token(self, tok):
        self.token = tok


class _FakeProvider:
    def __init__(self, api_key=None):
        self.api_key = api_key
        self.kite = _FakeKite()


class _FakeSkasData:
    def __init__(self, provider=None):
        self.provider = provider

    def get_prices(self, symbol, start_date=None, end_date=None, asset_type="stock", use_cache=True):
        import pandas as pd

        return pd.DataFrame({"date": pd.to_datetime(["2024-01-02"]), "close": [100.0]})


def _give_session(account_id: int, token: str = "acc-tok") -> None:
    with session_scope() as s:
        acct = s.get(BrokerAccount, account_id)
        acct.session_token = encrypt(token)
        acct.session_expires_at = datetime.now(UTC) + timedelta(hours=12)


def _patch_skas_data(monkeypatch):
    import skas_data
    import skas_data.providers.kite_provider as kp

    monkeypatch.setattr(skas_data, "SkasData", _FakeSkasData)
    monkeypatch.setattr(kp, "KiteProvider", _FakeProvider)


def test_make_data_session_shares_token(client: TestClient, monkeypatch):
    account_id = client.post("/api/v1/brokers", json={**CONNECT, "label": "share"}).json()["id"]
    _give_session(account_id, "shared-tok")
    _patch_skas_data(monkeypatch)

    with session_scope() as s:
        acct = s.get(BrokerAccount, account_id)
        sd = broker_svc.make_data_session(acct)
        # skas-data runs on the SAME token the platform stored (one login).
        assert sd.provider.kite.token == "shared-tok"
        assert sd.provider.kite.token == decrypt(acct.session_token)


def test_make_data_session_requires_login(client: TestClient):
    account_id = client.post("/api/v1/brokers", json={**CONNECT, "label": "nosess"}).json()["id"]
    with session_scope() as s, pytest.raises(BrokerLoginError):
        broker_svc.make_data_session(s.get(BrokerAccount, account_id))


def test_refresh_cache_endpoint(client: TestClient, monkeypatch):
    app = client.app
    app.dependency_overrides[get_available_symbols] = lambda: {"AAA"}

    account_id = client.post("/api/v1/brokers", json={**CONNECT, "label": "refresh"}).json()["id"]

    # No session yet -> gated.
    assert (
        client.post(f"/api/v1/brokers/{account_id}/refresh-cache", json={"symbols": ["AAA"]}).status_code
        == 400
    )

    _give_session(account_id)
    _patch_skas_data(monkeypatch)
    resp = client.post(f"/api/v1/brokers/{account_id}/refresh-cache", json={"symbols": ["AAA"]})
    assert resp.status_code == 200, resp.text
    refreshed = resp.json()["refreshed"]
    assert refreshed["AAA"]["rows"] == 1 and refreshed["AAA"]["last_date"] == "2024-01-02"

    # "Add symbol" path: an explicit symbol with a backfill start_date works the same way.
    add = client.post(
        f"/api/v1/brokers/{account_id}/refresh-cache",
        json={"symbols": ["NEWSYM"], "start_date": "2010-01-01"},
    )
    assert add.status_code == 200, add.text
    assert add.json()["refreshed"]["NEWSYM"]["rows"] == 1
    app.dependency_overrides.pop(get_available_symbols, None)
