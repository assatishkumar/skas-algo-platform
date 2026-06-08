"""Broker account API: encrypted storage, arm/disarm, login (mocked adapter)."""

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
    "password": "pw123",
    "totp_secret": "JBSWY3DPEHPK3PXP",
}


def test_connect_stores_encrypted_and_hides_secrets(client: TestClient):
    resp = client.post("/api/v1/brokers", json=CONNECT)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    account_id = body["id"]
    assert body["armed"] is False
    assert body["has_session"] is False
    # Secrets are never echoed back.
    assert "api_secret" not in body and "password" not in body and "totp_secret" not in body

    # Stored ciphertext != plaintext, but decrypts correctly.
    with session_scope() as s:
        acct = s.get(BrokerAccount, account_id)
        assert acct.enc_api_secret != "supersecret"
        assert decrypt(acct.enc_api_secret) == "supersecret"
        assert decrypt(acct.enc_totp_secret) == "JBSWY3DPEHPK3PXP"


def test_arm_disarm(client: TestClient):
    account_id = client.post("/api/v1/brokers", json={**CONNECT, "label": "arm-test"}).json()["id"]

    armed = client.post(f"/api/v1/brokers/{account_id}/arm").json()
    assert armed["armed"] is True
    disarmed = client.post(f"/api/v1/brokers/{account_id}/disarm").json()
    assert disarmed["armed"] is False


def test_login_persists_session(client: TestClient, monkeypatch):
    account_id = client.post("/api/v1/brokers", json={**CONNECT, "label": "login-test"}).json()[
        "id"
    ]

    class FakeAdapter:
        def login(self):
            return BrokerSession(
                access_token="tok-123", expires_at=datetime.now(UTC) + timedelta(hours=12)
            )

    monkeypatch.setattr(broker_svc, "make_adapter", lambda account: FakeAdapter())

    resp = client.post(f"/api/v1/brokers/{account_id}/login")
    assert resp.status_code == 200, resp.text
    assert resp.json()["has_session"] is True

    with session_scope() as s:
        acct = s.get(BrokerAccount, account_id)
        assert decrypt(acct.session_token) == "tok-123"
