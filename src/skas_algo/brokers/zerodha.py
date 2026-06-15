"""Zerodha (Kite Connect) broker adapter.

Login is done by the user out-of-band: they open the Kite login URL, authenticate
(password + 2FA) themselves, and the redirect gives a ``request_token``. The platform
exchanges that token (+ api_secret) for the daily access token via
``generate_session`` — the standard, ToS-compliant Kite Connect flow. No password or
TOTP is stored; only api_key + api_secret (encrypted).

Safety: real orders only fire when the account is *armed* AND live trading is enabled
at the platform level (SKAS_LIVE_TRADING_ENABLED). Otherwise place_order raises
NotArmedError. Forward-testing uses PaperBroker and never reaches this class.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from skas_algo.db.enums import OrderSide, OrderType

from .base import BrokerOrder, Funds, Session


class BrokerLoginError(RuntimeError):
    """Raised when exchanging the request token fails."""


class NotArmedError(RuntimeError):
    """Raised when a real order is attempted on an un-armed / disabled account."""


@dataclass
class ZerodhaCredentials:
    api_key: str
    api_secret: str
    user_id: str = ""


class ZerodhaAdapter:
    """BrokerAdapter implementation for Zerodha Kite Connect."""

    def __init__(
        self,
        creds: ZerodhaCredentials,
        *,
        armed: bool = False,
        live_enabled: bool = False,
        kite=None,
    ):
        self.creds = creds
        self.armed = armed
        self.live_enabled = live_enabled
        self._kite = kite  # injectable KiteConnect (lazily built if None)
        self.access_token: str | None = None
        self._nfo_lut: dict | None = None  # (name, expiry, strike, CE/PE) -> tradingsymbol

    # ------------------------------------------------------------------ kite
    def _kite_client(self):
        if self._kite is None:
            from kiteconnect import KiteConnect

            self._kite = KiteConnect(api_key=self.creds.api_key)
        return self._kite

    # ----------------------------------------------------------------- login
    def login_url(self) -> str:
        """The Kite URL the user visits to authenticate and obtain a request_token."""
        return self._kite_client().login_url()

    def exchange_request_token(self, request_token: str) -> Session:
        """Exchange a user-supplied request_token for the daily access token."""
        kite = self._kite_client()
        try:
            data = kite.generate_session(request_token, api_secret=self.creds.api_secret)
        except Exception as exc:
            raise BrokerLoginError(f"request token exchange failed: {exc}") from exc
        self.access_token = data["access_token"]
        kite.set_access_token(self.access_token)
        # Kite access tokens expire at the next ~06:00 IST; treat as end-of-day.
        return Session(
            access_token=self.access_token,
            expires_at=datetime.now() + timedelta(hours=12),
        )

    def set_access_token(self, token: str) -> None:
        """Resume a previously-exchanged session (for quotes/orders) without re-login."""
        self.access_token = token
        self._kite_client().set_access_token(token)

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
        """LTP per symbol. Equities map to ``NSE:<symbol>``; option contracts
        (``UNDERLYING|EXPIRY|STRIKE|RIGHT``) map to ``NFO:<tradingsymbol>`` via the Kite
        NFO instruments dump (so we don't hand-encode weekly/monthly tradingsymbols)."""
        from skas_algo.engine.options.instrument import parse

        key_of: dict[str, str] = {}  # my_symbol -> kite ltp key
        for s in symbols:
            inst = parse(s)
            if inst is None:
                key_of[s] = f"NSE:{s}"
            else:
                ts = self._option_tradingsymbol(inst)
                if ts:
                    key_of[s] = f"NFO:{ts}"
        if not key_of:
            return {}
        data = self._kite_client().ltp(list(key_of.values()))
        return {s: data[k]["last_price"] for s, k in key_of.items() if k in data}

    def _option_tradingsymbol(self, inst) -> str | None:
        """Resolve an option instrument to its Kite NFO tradingsymbol via the instruments
        dump (cached for the session)."""
        if self._nfo_lut is None:
            self._nfo_lut = {}
            for r in self._kite_client().instruments("NFO"):
                if r.get("instrument_type") not in ("CE", "PE"):
                    continue
                exp = r.get("expiry")
                exp_iso = exp.isoformat() if hasattr(exp, "isoformat") else str(exp)[:10]
                self._nfo_lut[(r["name"], exp_iso, float(r["strike"]), r["instrument_type"])] = \
                    r["tradingsymbol"]
        return self._nfo_lut.get(
            (inst.underlying, inst.expiry.isoformat(), float(inst.strike), inst.right)
        )
