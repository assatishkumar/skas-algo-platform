"""ZerodhaAdapter: order-arming guard and the TOTP login orchestration (mocked HTTP)."""

from __future__ import annotations

import pyotp
import pytest

from skas_algo.brokers.base import BrokerOrder
from skas_algo.brokers.zerodha import NotArmedError, ZerodhaAdapter, ZerodhaCredentials
from skas_algo.db.enums import OrderSide

CREDS = ZerodhaCredentials(
    api_key="apikey",
    api_secret="apisecret",
    user_id="AB1234",
    password="pw",
    totp_secret=pyotp.random_base32(),
)


def test_place_order_blocked_when_not_armed():
    adapter = ZerodhaAdapter(CREDS, armed=False, live_enabled=True)
    with pytest.raises(NotArmedError):
        adapter.place_order(BrokerOrder("RELIANCE", OrderSide.BUY, 1))


def test_place_order_blocked_when_live_disabled():
    adapter = ZerodhaAdapter(CREDS, armed=True, live_enabled=False)
    with pytest.raises(NotArmedError):
        adapter.place_order(BrokerOrder("RELIANCE", OrderSide.BUY, 1))


class _Resp:
    def __init__(self, json_body=None, headers=None):
        self._json = json_body or {}
        self.headers = headers or {}

    def json(self):
        return self._json


class _FakeHttp:
    """Scripts the login -> twofa -> oauth-redirect sequence."""

    def __init__(self):
        self.calls = []

    def post(self, url, data=None, **kw):
        self.calls.append(("POST", url, data))
        if url.endswith("/api/login"):
            return _Resp({"status": "success", "data": {"request_id": "req-1"}})
        if url.endswith("/api/twofa"):
            return _Resp({"status": "success", "data": {}})
        raise AssertionError(f"unexpected POST {url}")

    def get(self, url, allow_redirects=True, **kw):
        self.calls.append(("GET", url, None))
        # Kite redirects to the app's redirect_uri carrying the request_token.
        return _Resp(
            headers={"location": "https://app.example/redirect?request_token=RT123&status=success"}
        )


class _FakeKite:
    def __init__(self):
        self.access_token = None

    def login_url(self):
        return "https://kite.zerodha.com/connect/login?api_key=apikey&v=3"

    def generate_session(self, request_token, api_secret):
        assert request_token == "RT123"
        assert api_secret == "apisecret"
        return {"access_token": "ACCESS-XYZ"}

    def set_access_token(self, token):
        self.access_token = token


def test_totp_login_flow():
    http = _FakeHttp()
    kite = _FakeKite()
    adapter = ZerodhaAdapter(CREDS, http_session=http, kite=kite)

    session = adapter.login()

    assert session.access_token == "ACCESS-XYZ"
    assert adapter.access_token == "ACCESS-XYZ"
    assert kite.access_token == "ACCESS-XYZ"
    # The 2FA POST carried a fresh 6-digit TOTP for the right request_id.
    twofa = next(c for c in http.calls if c[1].endswith("/api/twofa"))
    assert twofa[2]["request_id"] == "req-1"
    assert len(twofa[2]["twofa_value"]) == 6
