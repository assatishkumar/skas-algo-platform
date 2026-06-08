"""Zerodha (Kite Connect) live broker adapter.

Login is TOTP-automated: user_id + password + TOTP secret are posted to Kite's web
login + 2FA endpoints to obtain a request_token, which is exchanged (with the
api_secret) for the daily Kite Connect access_token.

⚠️ ToS: automating Zerodha's username/password login is against the Kite Terms of
Service. This is provided because it was an explicit project choice; Angel One's
SmartAPI supports TOTP login officially and is the recommended path to de-risk.

Safety: real orders only fire when the account is *armed* AND live trading is
enabled at the platform level (SKAS_LIVE_TRADING_ENABLED). Otherwise place_order
raises NotArmedError. Forward-testing uses PaperBroker and never reaches this class.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pyotp
import requests

from skas_algo.db.enums import OrderSide, OrderType

from .base import BrokerOrder, Funds, Session

KITE_LOGIN_URL = "https://kite.zerodha.com/api/login"
KITE_TWOFA_URL = "https://kite.zerodha.com/api/twofa"
HTTP_TIMEOUT = 15  # seconds — never let a login network call hang forever


class BrokerLoginError(RuntimeError):
    """Raised when the automated login flow fails."""


class NotArmedError(RuntimeError):
    """Raised when a real order is attempted on an un-armed / disabled account."""


@dataclass
class ZerodhaCredentials:
    api_key: str
    api_secret: str
    user_id: str
    password: str
    totp_secret: str


class ZerodhaAdapter:
    """BrokerAdapter implementation for Zerodha Kite Connect."""

    def __init__(
        self,
        creds: ZerodhaCredentials,
        *,
        armed: bool = False,
        live_enabled: bool = False,
        http_session: requests.Session | None = None,
        kite=None,
    ):
        self.creds = creds
        self.armed = armed
        self.live_enabled = live_enabled
        self._http = http_session or requests.Session()
        self._kite = kite  # injectable KiteConnect (lazily built if None)
        self.access_token: str | None = None

    # ------------------------------------------------------------------ kite
    def _kite_client(self):
        if self._kite is None:
            from kiteconnect import KiteConnect

            self._kite = KiteConnect(api_key=self.creds.api_key)
        return self._kite

    # ----------------------------------------------------------------- login
    def login(self) -> Session:
        """Run the TOTP login flow and return an authenticated Session."""
        request_id = self._password_login()
        self._submit_totp(request_id)
        request_token = self._resolve_request_token()
        kite = self._kite_client()
        data = kite.generate_session(request_token, api_secret=self.creds.api_secret)
        self.access_token = data["access_token"]
        kite.set_access_token(self.access_token)
        # Kite access tokens expire at the next ~06:00 IST; treat as end-of-day.
        return Session(
            access_token=self.access_token,
            expires_at=datetime.now() + timedelta(hours=12),
        )

    def _password_login(self) -> str:
        resp = self._http.post(
            KITE_LOGIN_URL,
            data={"user_id": self.creds.user_id, "password": self.creds.password},
            timeout=HTTP_TIMEOUT,
        )
        body = resp.json()
        if body.get("status") != "success" or "data" not in body:
            raise BrokerLoginError(f"password login failed: {body}")
        request_id = body["data"].get("request_id")
        if not request_id:
            raise BrokerLoginError("login response missing request_id")
        return request_id

    def _totp_now(self) -> str:
        # Authenticator apps show the secret in spaced 4-char groups; strip whitespace
        # and uppercase before decoding (base32 is A-Z, 2-7).
        secret = re.sub(r"\s+", "", self.creds.totp_secret or "").upper()
        try:
            return pyotp.TOTP(secret).now()
        except Exception as exc:
            raise BrokerLoginError(
                "Invalid TOTP secret — paste the base32 'secret key' shown when you set up "
                "the external 2FA authenticator (letters A-Z and digits 2-7), not a 6-digit code."
            ) from exc

    def _submit_totp(self, request_id: str) -> None:
        code = self._totp_now()
        resp = self._http.post(
            KITE_TWOFA_URL,
            data={
                "user_id": self.creds.user_id,
                "request_id": request_id,
                "twofa_value": code,
                "twofa_type": "totp",
            },
            timeout=HTTP_TIMEOUT,
        )
        body = resp.json()
        if body.get("status") != "success":
            raise BrokerLoginError(f"2FA failed: {body}")

    def _resolve_request_token(self) -> str:
        """Follow the Kite Connect OAuth redirect to capture the request_token."""
        url = self._kite_client().login_url()
        for _ in range(10):
            resp = self._http.get(url, allow_redirects=False, timeout=HTTP_TIMEOUT)
            location = resp.headers.get("location") or resp.headers.get("Location")
            if location and "request_token=" in location:
                qs = parse_qs(urlparse(location).query)
                return qs["request_token"][0]
            if location:
                url = location
                continue
            break
        raise BrokerLoginError("could not obtain request_token from redirect")

    # ------------------------------------------------------------ execution
    def _ensure_armed(self) -> None:
        if not (self.armed and self.live_enabled):
            raise NotArmedError(
                "Refusing to place a live order: account is not armed or "
                "SKAS_LIVE_TRADING_ENABLED is false."
            )

    def place_order(self, order: BrokerOrder) -> str:
        self._ensure_armed()
        kite = self._kite_client()
        return kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_NSE,
            tradingsymbol=order.symbol,
            transaction_type=(
                kite.TRANSACTION_TYPE_BUY
                if order.side is OrderSide.BUY
                else kite.TRANSACTION_TYPE_SELL
            ),
            quantity=order.quantity,
            product=kite.PRODUCT_CNC,
            order_type=(
                kite.ORDER_TYPE_LIMIT
                if order.order_type is OrderType.LIMIT
                else kite.ORDER_TYPE_MARKET
            ),
            price=order.price,
            tag=order.tag,
        )

    def cancel_order(self, broker_order_id: str) -> None:
        self._ensure_armed()
        kite = self._kite_client()
        kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=broker_order_id)

    # ---------------------------------------------------------------- data
    def positions(self) -> list[dict]:
        return self._kite_client().positions().get("net", [])

    def funds(self) -> Funds:
        margins = self._kite_client().margins(segment="equity")
        available = margins.get("available", {}).get("live_balance", 0.0)
        used = margins.get("utilised", {}).get("debits", 0.0)
        return Funds(available=available, used=used)

    def get_quote(self, symbols: list[str]) -> dict[str, float]:
        keys = [f"NSE:{s}" for s in symbols]
        data = self._kite_client().ltp(keys)
        return {s: data[f"NSE:{s}"]["last_price"] for s in symbols if f"NSE:{s}" in data}
