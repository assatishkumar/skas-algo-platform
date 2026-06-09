"""Broker account API: encrypted storage, login-url, request-token exchange, arming."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from skas_algo.brokers.base import Session as BrokerSession
from skas_algo.db.base import session_scope
from skas_algo.db.models import BrokerAccount
from skas_algo.security import decrypt
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
