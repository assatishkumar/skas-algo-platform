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

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from skas_algo.db.enums import OrderSide, OrderType

from .base import BrokerOrder, Funds, Session

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")


def _next_kite_expiry() -> datetime:
    """Kite access tokens are invalidated at ~06:00 IST the next morning (NOT a rolling 12h) —
    return that instant as naive-UTC so ``has_valid_session`` (which treats naive as UTC) goes red
    when the token actually dies, instead of showing a misleading 'session ✓'."""
    now = datetime.now(_IST)
    exp = now.replace(hour=6, minute=0, second=0, microsecond=0)
    if now >= exp:
        exp += timedelta(days=1)
    return exp.astimezone(UTC).replace(tzinfo=None)


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
        self._ts_exchange: dict = {}  # tradingsymbol -> exchange, NFO omitted (the default)
        self._loaded_exchanges: set[str] = set()  # F&O dumps loaded OK (per exchange, sticky)

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
        # Kite access tokens are invalidated at ~06:00 IST the next morning.
        return Session(access_token=self.access_token, expires_at=_next_kite_expiry())

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

    def _order_route(self, symbol: str) -> tuple[str, str, str]:
        """(exchange, tradingsymbol, product) for an internal symbol. Options resolve via
        the NFO/BFO instruments LUT (same one quotes/margins use) and trade NRML; a plain
        symbol is an NSE equity and trades CNC. Raises for an unresolvable option — an
        order must NEVER fall through to a wrong contract."""
        from skas_algo.engine.options.instrument import parse

        inst = parse(symbol)
        if inst is None:
            return "NSE", symbol, "CNC"
        ts = self._option_tradingsymbol(inst)
        if not ts:
            raise ValueError(f"no listed contract for {symbol} in the NFO/BFO dump")
        return self._exchange_of(ts), ts, "NRML"

    _ORDER_TYPE_MAP = {
        OrderType.LIMIT: "LIMIT", OrderType.MARKET: "MARKET",
        OrderType.SL: "SL", OrderType.SL_M: "SL-M",
    }

    def place_order(self, order: BrokerOrder) -> str:
        """Place a REAL order; returns the Kite order id. Double-gated (armed + platform
        flag). F&O routes NFO/BFO + NRML via the instruments LUT; equity NSE + CNC."""
        self._ensure_armed()
        kite = self._kite_client()
        exchange, tradingsymbol, product = self._order_route(order.symbol)
        return kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            transaction_type=(
                kite.TRANSACTION_TYPE_BUY
                if order.side is OrderSide.BUY
                else kite.TRANSACTION_TYPE_SELL
            ),
            quantity=int(order.quantity),
            product=product,
            order_type=self._ORDER_TYPE_MAP.get(order.order_type, "MARKET"),
            price=order.price,
            tag=(order.tag or order.client_order_id or None),
        )

    def modify_order(self, broker_order_id: str, *, order_type: OrderType | None = None,
                     price: float | None = None) -> None:
        """Modify a pending order — the LIMIT→MARKET escalation path."""
        self._ensure_armed()
        kite = self._kite_client()
        kwargs: dict = {}
        if order_type is not None:
            kwargs["order_type"] = self._ORDER_TYPE_MAP.get(order_type, "MARKET")
        if price is not None:
            kwargs["price"] = price
        kite.modify_order(variety=kite.VARIETY_REGULAR, order_id=broker_order_id, **kwargs)

    def order_status(self, broker_order_id: str) -> dict:
        """{status, average_price, filled_quantity, status_message} for one order —
        terminal Kite statuses are COMPLETE / REJECTED / CANCELLED. Read-only (ungated)."""
        hist = self._kite_client().order_history(order_id=broker_order_id)
        last = hist[-1] if hist else {}
        return {
            "status": last.get("status", "UNKNOWN"),
            "average_price": float(last.get("average_price") or 0.0),
            "filled_quantity": int(last.get("filled_quantity") or 0),
            "status_message": last.get("status_message"),
        }

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

    def _ltp_keys(self, symbols: list[str]) -> dict[str, str]:
        """Map each of OUR symbols to its Kite ltp/subscribe key (``EXCH:TRADINGSYMBOL``).
        Equities map to ``NSE:<symbol>`` (SENSEX/BANKEX → ``BSE:``); option contracts
        (``UNDERLYING|EXPIRY|STRIKE|RIGHT``) map to ``NFO:<tradingsymbol>`` via the Kite
        NFO/BFO instruments dump (so we don't hand-encode weekly/monthly tradingsymbols)."""
        from skas_algo.engine.options.instrument import parse

        key_of: dict[str, str] = {}  # my_symbol -> kite key
        for s in symbols:
            inst = parse(s)
            if inst is None:
                exch = "BSE" if s.upper() in self._BSE_SERIES else "NSE"
                key_of[s] = f"{exch}:{s}"
            else:
                ts = self._option_tradingsymbol(inst)
                if ts:
                    key_of[s] = f"{self._exchange_of(ts)}:{ts}"
        return key_of

    def get_quote(self, symbols: list[str]) -> dict[str, float]:
        """LTP per symbol (batched into one ``kite.ltp()`` call)."""
        key_of = self._ltp_keys(symbols)
        if not key_of:
            return {}
        data = self._kite_client().ltp(list(key_of.values()))
        return {s: data[k]["last_price"] for s, k in key_of.items() if k in data}

    def instrument_tokens(self, symbols: list[str]) -> dict[str, int]:
        """Resolve OUR symbols to Kite ``instrument_token``s for a WebSocket subscription.
        One ``kite.ltp()`` call — its response carries the token per key. Used by the
        KiteTicker price feed (live/pricefeed.py)."""
        key_of = self._ltp_keys(symbols)
        if not key_of:
            return {}
        data = self._kite_client().ltp(list(key_of.values()))
        return {s: int(data[k]["instrument_token"]) for s, k in key_of.items()
                if k in data and data[k].get("instrument_token")}

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
                "exchange": self._exchange_of(ts),
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
        """Cache the option instruments dumps for the session: a (name,expiry,strike,type)
        → tradingsymbol map, a name→expiry→strike→{CE,PE} index, and the per-name lot size.
        Loads NFO **and BFO** (SENSEX/BANKEX options list on BSE F&O) into one LUT; the
        exchange travels per-tradingsymbol in ``_ts_exchange`` so quote/chain keys prefix
        correctly (``NFO:``/``BFO:``).

        Each exchange loads AT MOST ONCE (success is sticky, `_loaded_exchanges`); a FAILED
        exchange is NOT cached — it's retried on the next call. Critically, a transient BFO
        failure (e.g. a Kite "Too many requests" during a login/promote storm) must not
        permanently dead-end SENSEX/BANKEX: the old code set `_nfo_lut` up-front and swallowed
        the BFO error, so BFO stayed missing for the adapter's whole lifetime while spot (a
        plain LTP) still worked — a SENSEX options run then silently never entered (2026-07-10)."""
        if self._nfo_lut is None:
            self._nfo_lut = {}
        for exchange in ("NFO", "BFO"):
            if exchange in self._loaded_exchanges:
                continue
            try:
                rows = self._kite_client().instruments(exchange)
            except Exception as exc:  # BFO hiccup must not kill NFO — and must stay retryable
                if exchange == "NFO":
                    raise
                logger.warning(
                    "BFO instruments dump failed (%s) — SENSEX/BANKEX options are unavailable "
                    "until it reloads; will retry on the next chain/expiry call", exc)
                continue
            for r in rows:
                if r.get("instrument_type") not in ("CE", "PE"):
                    continue
                name, it = r["name"], r["instrument_type"]
                exp = r.get("expiry")
                exp_iso = exp.isoformat() if hasattr(exp, "isoformat") else str(exp)[:10]
                strike = float(r["strike"])
                ts = r["tradingsymbol"]
                self._nfo_lut[(name, exp_iso, strike, it)] = ts
                by_strike = self._nfo_index.setdefault(name, {}).setdefault(exp_iso, {})
                by_strike.setdefault(strike, {})[it] = ts
                if exchange != "NFO":
                    self._ts_exchange[ts] = exchange
                if r.get("lot_size"):
                    self._nfo_lot[name] = int(r["lot_size"])
            self._loaded_exchanges.add(exchange)  # sticky: don't re-fetch a good dump

    def _exchange_of(self, tradingsymbol: str) -> str:
        return self._ts_exchange.get(tradingsymbol, "NFO")

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
        "SENSEX": "SENSEX",
    }
    # Index series that live on BSE — their ltp/quote keys need the BSE: prefix.
    _BSE_SERIES = {"SENSEX", "BANKEX"}

    def _spot_key(self, underlying: str) -> str:
        series = self._INDEX_SPOT.get(underlying.upper(), underlying.upper())
        exch = "BSE" if series.upper() in self._BSE_SERIES else "NSE"
        return f"{exch}:{series}"

    def option_underlyings(self) -> list[str]:
        """All F&O option underlyings Kite currently lists (indices + stocks)."""
        self._build_nfo()
        return sorted(self._nfo_index)

    def option_expiries(self, underlying: str) -> list[str]:
        """Listed expiries (ISO) for an underlying, today onward."""
        self._build_nfo()
        today = datetime.now().date().isoformat()
        return sorted(e for e in self._nfo_index.get(underlying.upper(), {}) if e >= today)

    def underlying_ltp(self, underlying: str) -> float | None:
        """Live spot for an option underlying (its index series, or the stock itself)."""
        key = self._spot_key(underlying)
        try:
            return self._kite_client().ltp([key]).get(key, {}).get("last_price")
        except Exception:  # pragma: no cover - network hiccup
            return None

    def live_option_chain(self, underlying: str, expiry: str, window: int = 40) -> dict | None:
        """Live chain for one expiry: per-strike CE/PE last price + OI, the live underlying
        spot, ATM, and contract lot size — all from Kite (real-time). Strikes are windowed
        ±``window`` around ATM to keep the quote() batch small. None if the contract isn't listed."""
        self._build_nfo()
        name = underlying.upper()
        chain = self._nfo_index.get(name, {}).get(expiry)
        if not chain:
            return None
        kite = self._kite_client()
        spot_key = self._spot_key(name)
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
        keys = [f"{self._exchange_of(ts)}:{ts}"
                for k in sel for ts in (chain[k].get("CE"), chain[k].get("PE")) if ts]
        try:
            q = kite.quote(keys) if keys else {}
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(f"live quote failed: {exc}") from exc

        def info(ts: str | None) -> dict | None:
            d = q.get(f"{self._exchange_of(ts)}:{ts}") if ts else None
            if not d:
                return None
            depth = d.get("depth") or {}
            buy = depth.get("buy") or []
            sell = depth.get("sell") or []
            return {"ltp": d.get("last_price"), "close": (d.get("ohlc") or {}).get("close"),
                    "oi": d.get("oi"), "change_in_oi": None,
                    "bid": (buy[0].get("price") if buy else None),
                    "ask": (sell[0].get("price") if sell else None)}

        rows = [{"strike": k, "ce": info(chain[k].get("CE")), "pe": info(chain[k].get("PE"))} for k in sel]
        return {"spot": spot, "atm_strike": atm, "lot_size": self._nfo_lot.get(name, 0), "rows": rows}

    # ------------------------------------------------------- intraday bars
    def intraday_bars(self, underlying: str, days: int = 7, minutes: int = 15) -> list[dict]:
        """Recent intraday OHLC candles for an index/stock underlying via Kite historical
        data — the warmup feed for intraday strategies (momentum_theta). The instrument
        token is resolved from a live ltp() call (works for NSE and BSE series alike), so
        no token table is maintained. Returns [{start, open, high, low, close}, ...] oldest
        first; [] on any failure (warmup is best-effort — the strategy cold-starts)."""
        key = self._spot_key(underlying)
        kite = self._kite_client()
        try:
            token = kite.ltp([key]).get(key, {}).get("instrument_token")
            if not token:
                return []
            end = datetime.now()
            bars = kite.historical_data(
                token, end - timedelta(days=days), end, f"{minutes}minute"
            )
        except Exception:  # pragma: no cover - network/permission hiccup → cold start
            return []
        out = []
        for b in bars:
            ts = b.get("date")
            start = ts.replace(tzinfo=None).isoformat() if hasattr(ts, "isoformat") else str(ts)
            out.append({"start": start, "open": float(b["open"]), "high": float(b["high"]),
                        "low": float(b["low"]), "close": float(b["close"])})
        return out

    # ------------------------------------------------------- daily bars
    def daily_bars(self, underlying: str, days: int = 30) -> list[dict]:
        """Recent DAILY OHLC candles for an index/stock underlying via Kite historical data
        (interval='day') — the FRESH prior-day source for live pivots / EMA bands, so live
        never depends on the (manually-refreshed) skas-data cache. Mirrors intraday_bars:
        token resolved from a live ltp() (NSE + BSE series alike, so SENSEX — which has no
        cached daily series — also gets official pivots). Returns [{date, open, high, low,
        close}, ...] oldest-first with the candle's calendar DATE surfaced (callers detect
        staleness); [] on any failure (caller falls back to the cache / its own bars)."""
        key = self._spot_key(underlying)
        kite = self._kite_client()
        try:
            token = kite.ltp([key]).get(key, {}).get("instrument_token")
            if not token:
                return []
            end = datetime.now()
            bars = kite.historical_data(token, end - timedelta(days=days), end, "day")
        except Exception:  # pragma: no cover - network/permission hiccup → cache fallback
            return []
        out = []
        for b in bars:
            ts = b.get("date")
            d = ts.date().isoformat() if hasattr(ts, "date") else str(ts)[:10]
            out.append({"date": d, "open": float(b["open"]), "high": float(b["high"]),
                        "low": float(b["low"]), "close": float(b["close"])})
        return out
