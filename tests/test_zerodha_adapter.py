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


class _QuoteKite(_FakeKite):
    """Adds instruments() + ltp() so option/equity quote mapping can be tested."""
    def instruments(self, exchange):
        assert exchange == "NFO"
        from datetime import date
        return [
            {"name": "NIFTY", "expiry": date(2026, 1, 13), "strike": 25400.0,
             "instrument_type": "CE", "tradingsymbol": "NIFTY2611325400CE"},
            {"name": "NIFTY", "expiry": date(2026, 1, 13), "strike": 25200.0,
             "instrument_type": "CE", "tradingsymbol": "NIFTY2611325200CE"},
        ]

    def ltp(self, keys):
        prices = {"NSE:RELIANCE": 1500.0, "NFO:NIFTY2611325400CE": 88.5,
                  "NFO:NIFTY2611325200CE": 142.0}
        return {k: {"last_price": prices[k]} for k in keys if k in prices}


def test_get_quote_maps_equity_and_option_symbols():
    adapter = ZerodhaAdapter(CREDS, kite=_QuoteKite())
    out = adapter.get_quote([
        "RELIANCE",
        "NIFTY|2026-01-13|25400|CE",
        "NIFTY|2026-01-13|25200|CE",
    ])
    assert out["RELIANCE"] == 1500.0
    assert out["NIFTY|2026-01-13|25400|CE"] == 88.5      # resolved via the NFO dump
    assert out["NIFTY|2026-01-13|25200|CE"] == 142.0


def test_get_quote_skips_unlisted_option():
    adapter = ZerodhaAdapter(CREDS, kite=_QuoteKite())
    out = adapter.get_quote(["NIFTY|2026-01-13|99000|CE"])  # strike not in the dump
    assert out == {}


class _MarginKite(_QuoteKite):
    """Adds basket_margins() so basket-margin building can be tested."""

    def __init__(self):
        super().__init__()
        self.last_basket = None

    def basket_order_margins(self, basket, consider_positions=True):
        self.last_basket = basket
        self.last_consider_positions = consider_positions
        return {"initial": {"total": 200000.0}, "final": {"total": 132000.0}}


def test_basket_margin_builds_basket_from_own_legs():
    kite = _MarginKite()
    adapter = ZerodhaAdapter(CREDS, kite=kite)
    total = adapter.basket_margin([
        {"symbol": "NIFTY|2026-01-13|25400|CE", "direction": -1, "units": 195},
        {"symbol": "NIFTY|2026-01-13|25200|CE", "direction": 1, "units": 65},
    ])
    assert total == 132000.0  # the spread-benefit "final" net, not "initial"
    assert kite.last_consider_positions is False  # basket-alone margin (Sensibull-style)
    sell, buy = kite.last_basket
    assert sell["transaction_type"] == "SELL" and sell["tradingsymbol"] == "NIFTY2611325400CE"
    assert sell["quantity"] == 195 and sell["exchange"] == "NFO" and sell["product"] == "NRML"
    assert buy["transaction_type"] == "BUY" and buy["tradingsymbol"] == "NIFTY2611325200CE"


def test_basket_margin_none_when_nothing_maps():
    adapter = ZerodhaAdapter(CREDS, kite=_MarginKite())
    # Strike not in the NFO dump → no orders → None (caller falls back to the model).
    assert adapter.basket_margin(
        [{"symbol": "NIFTY|2026-01-13|99000|CE", "direction": -1, "units": 75}]
    ) is None
