"""ZerodhaAdapter: order-arming guard and request-token session exchange (mocked)."""

from __future__ import annotations

import pytest

from skas_algo.brokers.base import BrokerOrder
from skas_algo.brokers.zerodha import (
    BrokerLoginError,
    NotArmedError,
    ZerodhaAdapter,
    ZerodhaCredentials,
)
from skas_algo.db.enums import OrderSide

CREDS = ZerodhaCredentials(api_key="apikey", api_secret="apisecret", user_id="AB1234")


def test_place_order_blocked_when_not_armed():
    adapter = ZerodhaAdapter(CREDS, armed=False, live_enabled=True)
    with pytest.raises(NotArmedError):
        adapter.place_order(BrokerOrder("RELIANCE", OrderSide.BUY, 1))


def test_place_order_blocked_when_live_disabled():
    adapter = ZerodhaAdapter(CREDS, armed=True, live_enabled=False)
    with pytest.raises(NotArmedError):
        adapter.place_order(BrokerOrder("RELIANCE", OrderSide.BUY, 1))


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


def test_login_url():
    adapter = ZerodhaAdapter(CREDS, kite=_FakeKite())
    assert "api_key=apikey" in adapter.login_url()


def test_exchange_request_token():
    kite = _FakeKite()
    adapter = ZerodhaAdapter(CREDS, kite=kite)
    session = adapter.exchange_request_token("RT123")
    assert session.access_token == "ACCESS-XYZ"
    assert adapter.access_token == "ACCESS-XYZ"
    assert kite.access_token == "ACCESS-XYZ"


def test_exchange_failure_gives_clear_error():
    class BadKite:
        def generate_session(self, *a, **k):
            raise ValueError("Invalid `request_token`.")

    adapter = ZerodhaAdapter(CREDS, kite=BadKite())
    with pytest.raises(BrokerLoginError, match="request token exchange failed"):
        adapter.exchange_request_token("bad")
