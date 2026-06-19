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
        self._nfo_index: dict = {}  # name -> {expiry_iso -> {strike -> {"CE": ts, "PE": ts}}}
        self._nfo_lot: dict = {}    # name -> contract lot size

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

    def basket_margin(self, legs: list[dict]) -> float | None:
        """Net margin (₹) Zerodha would block for a basket built from THESE legs.

        Built from our own option legs (NOT ``kite.positions()`` — in PAPER mode the broker
        holds no such positions). ``legs``: [{"symbol", "direction", "units"}]. Returns the
        spread-benefit net total, or None if it can't be computed (no mapping / API error)."""
        from skas_algo.engine.options.instrument import parse

        kite = self._kite_client()
        basket: list[dict] = []
        for leg in legs:
            inst = parse(leg["symbol"])
            if inst is None:
                continue
            ts = self._option_tradingsymbol(inst)
            qty = abs(int(leg.get("units", 0)))
            if not ts or qty <= 0:
                continue
            basket.append({
                "exchange": "NFO",
                "tradingsymbol": ts,
                "transaction_type": "SELL" if leg.get("direction", 1) < 0 else "BUY",
                "variety": "regular",
                "product": "NRML",
                "order_type": "MARKET",
                "quantity": qty,
                "price": 0,
                "trigger_price": 0,
            })
        if not basket:
            return None
        try:
            # consider_positions=False → margin for THIS basket alone (with its own spread
            # benefit), matching Sensibull's "Margin Needed" — not netted against the
            # account's unrelated real positions.
            result = kite.basket_order_margins(basket, consider_positions=False)
        except Exception:  # pragma: no cover - network/API hiccup → caller falls back
            return None
        final = result.get("final", {}) if isinstance(result, dict) else {}
        total = final.get("total")
        return float(total) if total is not None else None

    def _build_nfo(self) -> None:
        """Cache the NFO option instruments dump for the session: a (name,expiry,strike,type)
        → tradingsymbol map, a name→expiry→strike→{CE,PE} index, and the per-name lot size."""
        if self._nfo_lut is not None:
            return
        self._nfo_lut = {}
        for r in self._kite_client().instruments("NFO"):
            if r.get("instrument_type") not in ("CE", "PE"):
                continue
            name, it = r["name"], r["instrument_type"]
            exp = r.get("expiry")
            exp_iso = exp.isoformat() if hasattr(exp, "isoformat") else str(exp)[:10]
            strike = float(r["strike"])
            ts = r["tradingsymbol"]
            self._nfo_lut[(name, exp_iso, strike, it)] = ts
            self._nfo_index.setdefault(name, {}).setdefault(exp_iso, {}).setdefault(strike, {})[it] = ts
            if r.get("lot_size"):
                self._nfo_lot[name] = int(r["lot_size"])

    def _option_tradingsymbol(self, inst) -> str | None:
        """Resolve an option instrument to its Kite NFO tradingsymbol via the instruments dump."""
        self._build_nfo()
        return self._nfo_lut.get(
            (inst.underlying, inst.expiry.isoformat(), float(inst.strike), inst.right)
        )

    # ----------------------------------------------------- live option chain
    # Index underlyings quote their index symbol for spot; a stock F&O underlying quotes itself.
    _INDEX_SPOT = {
        "NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK",
        "FINNIFTY": "NIFTY FIN SERVICE", "MIDCPNIFTY": "NIFTY MIDCAP SELECT",
    }

    def option_underlyings(self) -> list[str]:
        """All F&O option underlyings Kite currently lists (indices + stocks)."""
        self._build_nfo()
        return sorted(self._nfo_index)

    def option_expiries(self, underlying: str) -> list[str]:
        """Listed expiries (ISO) for an underlying, today onward."""
        self._build_nfo()
        today = datetime.now().date().isoformat()
        return sorted(e for e in self._nfo_index.get(underlying.upper(), {}) if e >= today)

    def live_option_chain(self, underlying: str, expiry: str, window: int = 25) -> dict | None:
        """Live chain for one expiry: per-strike CE/PE last price + OI, the live underlying
        spot, ATM, and contract lot size — all from Kite (real-time). Strikes are windowed
        ±``window`` around ATM to keep the quote() batch small. None if the contract isn't listed."""
        self._build_nfo()
        name = underlying.upper()
        chain = self._nfo_index.get(name, {}).get(expiry)
        if not chain:
            return None
        kite = self._kite_client()
        spot_key = f"NSE:{self._INDEX_SPOT.get(name, name)}"
        try:
            spot = kite.ltp([spot_key]).get(spot_key, {}).get("last_price")
        except Exception:  # pragma: no cover - network hiccup
            spot = None
        strikes = sorted(chain)
        if spot:
            atm = min(strikes, key=lambda k: abs(k - spot))
            ai = strikes.index(atm)
            sel = strikes[max(0, ai - window): ai + window + 1]
        else:
            atm, sel = None, strikes
        keys = [f"NFO:{ts}" for k in sel for ts in (chain[k].get("CE"), chain[k].get("PE")) if ts]
        try:
            q = kite.quote(keys) if keys else {}
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(f"live quote failed: {exc}") from exc

        def info(ts: str | None) -> dict | None:
            d = q.get(f"NFO:{ts}") if ts else None
            if not d:
                return None
            return {"ltp": d.get("last_price"), "close": (d.get("ohlc") or {}).get("close"),
                    "oi": d.get("oi"), "change_in_oi": None}

        rows = [{"strike": k, "ce": info(chain[k].get("CE")), "pe": info(chain[k].get("PE"))} for k in sel]
        return {"spot": spot, "atm_strike": atm, "lot_size": self._nfo_lot.get(name, 0), "rows": rows}
