"""BrokerAdapter for Dhan (DhanHQ v2 REST API) — Phase A: quotes / chains / margin only.

Auth model differs from Kite: there is NO request-token exchange. The user generates an
access token (a JWT, ~24h validity) on web.dhan.co and pastes it — so this adapter's
``exchange_request_token`` simply adopts the pasted token and reads its real expiry from
the JWT ``exp`` claim. Every request carries ``access-token`` + ``client-id`` headers.

Instrument mapping: Dhan addresses contracts by numeric ``securityId`` (per exchange
segment), resolved from the public scrip-master CSV — the Dhan analogue of Kite's NFO
instruments dump. The master is ~26 MB and adapters are constructed per-request, so the
parsed maps are cached at MODULE level (daily TTL) rather than per instance.

Column ground truth (verified against the live CSV, 2026-07-03):
  SEM_EXM_EXCH_ID, SEM_SEGMENT (D=derivatives, E=equity, I=index), SEM_SMST_SECURITY_ID,
  SEM_INSTRUMENT_NAME (OPTIDX/OPTSTK/...), SEM_TRADING_SYMBOL (e.g.
  ``BAJAJ-AUTO-Jul2026-9000-CE`` — the underlying must be recovered with rsplit("-", 3),
  NOT split, or hyphenated names break), SEM_LOT_UNITS, SEM_EXPIRY_DATE
  ("2026-07-30 15:30:00"), SEM_STRIKE_PRICE, SEM_OPTION_TYPE (CE/PE), SEM_SERIES.
  BSE derivatives rows exist for the same names — filter to NSE.

Phase B (real orders, 2026-08-21) adds place/modify/cancel/status + positions behind
``_ensure_armed`` — the SAME double gate as Zerodha (armed account AND
SKAS_LIVE_TRADING_ENABLED), and LiveBroker still owns every rail above it. Dhan statuses are
normalised to the platform's vocabulary here (TRADED→COMPLETE, EXPIRED→CANCELLED) so
LiveBroker stays broker-agnostic. HARD PREREQUISITE: Dhan only accepts order API calls from
a STATIC IP whitelisted on the DhanHQ portal — quotes and the scrip master work without it,
so an unwhitelisted account looks healthy until the first order is rejected.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import threading
import time as _time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from skas_algo.db.enums import OrderSide, OrderType

from .base import BrokerOrder, Funds, Session
from .zerodha import BrokerLoginError, NotArmedError

DHAN_BASE = "https://api.dhan.co/v2"
SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
# Token generation lives in the Dhan web console (Profile → DhanHQ Trading APIs).
DHAN_TOKEN_PAGE = "https://web.dhan.co"

# Index underlyings: engine name -> the scrip master's index SEM_TRADING_SYMBOL. Their
# IDX_I security ids are read from the master (NIFTY=13, INDIA VIX=21, ... — resolved,
# not hardcoded, so listing changes don't bite).
_INDEX_NAME = {
    "NIFTY": "NIFTY",
    "NIFTY 50": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "NIFTY BANK": "BANKNIFTY",
    "FINNIFTY": "FINNIFTY",
    "MIDCPNIFTY": "MIDCPNIFTY",
    "INDIA VIX": "INDIA VIX",
}


@dataclass
class DhanCredentials:
    client_id: str


class DhanApiError(RuntimeError):
    """A Dhan HTTP error WITH its response body. requests' raise_for_status() gives only
    "400 Client Error for url: ...", which on the order path is useless — Dhan puts the
    actual complaint (errorType / errorCode / errorMessage) in the JSON body, and that
    string is the whole diagnosis. Losing it cost a live smoke-test round trip on
    2026-08-21."""

    def __init__(self, verb: str, path: str, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"Dhan {verb} {path} → HTTP {status}: {body}")


class _DhanHttp:
    """Thin requests wrapper with the two Dhan headers — injectable for tests."""

    def __init__(self, client_id: str):
        self.client_id = client_id
        self.token: str | None = None

    def _headers(self) -> dict:
        return {
            "access-token": self.token or "",
            "client-id": self.client_id,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _check(r, verb: str, path: str) -> dict:
        if r.status_code >= 400:
            body = (r.text or "").strip()[:600]
            raise DhanApiError(verb, path, r.status_code, body or "<empty body>")
        try:
            out: dict = r.json()
        except ValueError:
            return {}
        return out

    def _call(self, verb: str, path: str, body: dict | None = None) -> dict:
        import requests

        r = requests.request(verb, f"{DHAN_BASE}{path}", json=body,
                             headers=self._headers(), timeout=15)
        return self._check(r, verb, path)

    def post(self, path: str, body: dict) -> dict:
        return self._call("POST", path, body)

    def get(self, path: str) -> dict:
        return self._call("GET", path)

    def put(self, path: str, body: dict) -> dict:
        return self._call("PUT", path, body)

    def delete(self, path: str) -> dict:
        return self._call("DELETE", path)

    def fetch_master(self) -> str:
        import requests

        r = requests.get(SCRIP_MASTER_URL, timeout=120)
        r.raise_for_status()
        return r.text


# ------------------------------------------------------------- scrip master (module cache)

class _Master:
    """Parsed scrip-master maps. option: (underlying, expiry_iso, strike, right) ->
    (security_id, lot); equity: symbol -> id; index: name -> id."""

    def __init__(self) -> None:
        self.option: dict[tuple[str, str, float, str], tuple[str, int]] = {}
        self.equity: dict[str, str] = {}
        self.index: dict[str, str] = {}
        self.lot: dict[str, int] = {}          # underlying -> lot size (latest seen)
        # Dhan's own tradingSymbol per contract ("NIFTY-Aug2026-24000-CE"). Reconciliation
        # compares OUR book against positions() keyed by exactly this string, so it has to
        # come from the master rather than be reconstructed.
        self.option_ts: dict[tuple[str, str, float, str], str] = {}
        self.expiries: dict[str, set[str]] = {}  # underlying -> {expiry_iso}

    @classmethod
    def parse(cls, text: str) -> _Master:
        m = cls()
        for row in csv.DictReader(io.StringIO(text)):
            if row.get("SEM_EXM_EXCH_ID") != "NSE":
                continue  # BSE lists twin contracts for the same names — NSE only
            seg = row.get("SEM_SEGMENT")
            sid = row.get("SEM_SMST_SECURITY_ID") or ""
            ts = row.get("SEM_TRADING_SYMBOL") or ""
            if seg == "I":
                m.index[ts.upper()] = sid
            elif seg == "E" and (row.get("SEM_SERIES") or "") == "EQ":
                m.equity[ts.upper()] = sid
            elif seg == "D" and row.get("SEM_OPTION_TYPE") in ("CE", "PE"):
                # BAJAJ-AUTO-Jul2026-9000-CE → rsplit keeps hyphenated underlyings whole.
                parts = ts.rsplit("-", 3)
                if len(parts) != 4:
                    continue
                underlying = parts[0].upper()
                expiry = (row.get("SEM_EXPIRY_DATE") or "")[:10]
                try:
                    strike = float(row.get("SEM_STRIKE_PRICE") or 0)
                    lot = int(float(row.get("SEM_LOT_UNITS") or 0))
                except ValueError:
                    continue
                right = row["SEM_OPTION_TYPE"]
                m.option[(underlying, expiry, strike, right)] = (sid, lot)
                m.option_ts[(underlying, expiry, strike, right)] = ts
                m.lot[underlying] = lot
                m.expiries.setdefault(underlying, set()).add(expiry)
        return m


_master_lock = threading.Lock()
_master_cache: _Master | None = None
_master_at: float = 0.0
_MASTER_TTL = 20 * 3600  # refresh daily — contracts list changes overnight


def _get_master(http: _DhanHttp) -> _Master:
    global _master_cache, _master_at
    with _master_lock:
        if _master_cache is None or (_time.time() - _master_at) > _MASTER_TTL:
            _master_cache = _Master.parse(http.fetch_master())
            _master_at = _time.time()
        return _master_cache


def _jwt_expiry(token: str) -> datetime:
    """The token's real ``exp`` claim as naive-UTC (has_valid_session treats naive as
    UTC), falling back to +24h when the JWT can't be decoded — Dhan tokens are
    portal-generated with ~daily validity."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        if exp:
            return datetime.fromtimestamp(int(exp), tz=UTC).replace(tzinfo=None)
    except Exception:  # noqa: BLE001 - malformed token → conservative default below
        pass
    return (datetime.now(UTC) + timedelta(hours=24)).replace(tzinfo=None)


class DhanAdapter:
    """BrokerAdapter for Dhan: session, quotes, chains, margin, and real orders."""

    def __init__(
        self,
        creds: DhanCredentials,
        *,
        armed: bool = False,
        live_enabled: bool = False,
        client: _DhanHttp | None = None,
    ):
        self.creds = creds
        self.armed = armed
        self.live_enabled = live_enabled
        self._http = client or _DhanHttp(creds.client_id)
        self.access_token: str | None = None

    # ----------------------------------------------------------------- login
    def login_url(self) -> str:
        """Dhan has no OAuth hop — tokens are generated in the web console."""
        return DHAN_TOKEN_PAGE

    def exchange_request_token(self, request_token: str) -> Session:
        """Adopt a pasted access token (Dhan's 'request token' IS the access token) and
        validate it with a cheap authenticated call before persisting."""
        token = request_token.strip()
        self.set_access_token(token)
        try:
            self._http.get("/fundlimit")  # any authenticated endpoint proves the token
        except Exception as exc:
            self.access_token = None
            raise BrokerLoginError(f"Dhan token rejected: {exc}") from exc
        return Session(access_token=token, expires_at=_jwt_expiry(token))

    def set_access_token(self, token: str) -> None:
        self.access_token = token
        self._http.token = token

    # ---------------------------------------------------------------- master
    def _master(self) -> _Master:
        return _get_master(self._http)

    def _index_id(self, name: str) -> str | None:
        idx = _INDEX_NAME.get(name.upper())
        return self._master().index.get(idx) if idx else None

    def _option_id(self, inst) -> tuple[str, int] | None:
        return self._master().option.get(
            (inst.underlying.upper(), inst.expiry.isoformat(), float(inst.strike), inst.right)
        )

    def _option_tradingsymbol(self, inst) -> str | None:
        """Dhan's own tradingSymbol for a contract — the key reconciliation compares the
        broker's net book on (manager._book_mismatch calls this by name, same as Zerodha)."""
        return self._master().option_ts.get(
            (inst.underlying.upper(), inst.expiry.isoformat(), float(inst.strike), inst.right)
        )

    # ------------------------------------------------------------- real orders
    # PREREQUISITE, and it is not optional: Dhan requires the account's STATIC IP to be
    # whitelisted on the DhanHQ portal before /orders will accept anything. Quotes and the
    # scrip master work without it, so a mis-set account looks perfectly healthy right up
    # until the first order is rejected.
    def _ensure_armed(self) -> None:
        if not (self.armed and self.live_enabled):
            raise NotArmedError(
                "Refusing to place a live order: account is not armed or "
                "SKAS_LIVE_TRADING_ENABLED is false."
            )

    _ORDER_TYPE_MAP = {
        OrderType.LIMIT: "LIMIT", OrderType.MARKET: "MARKET",
        OrderType.SL: "STOP_LOSS", OrderType.SL_M: "STOP_LOSS_MARKET",
    }
    # Dhan's vocabulary → the platform's (LiveBroker._TERMINAL is COMPLETE/REJECTED/
    # CANCELLED and it fills on COMPLETE). Mapping here keeps LiveBroker broker-agnostic.
    #   TRADED      → COMPLETE     the fill
    #   EXPIRED     → CANCELLED    terminal, unfilled — same handling as a cancel
    #   PART_TRADED → left as-is   deliberately NOT terminal, so the escalation keeps
    #                              working and the remainder is cancelled and booked
    #   TRANSIT/PENDING → PENDING  still live
    _STATUS_MAP = {
        "TRADED": "COMPLETE", "REJECTED": "REJECTED", "CANCELLED": "CANCELLED",
        "EXPIRED": "CANCELLED", "TRANSIT": "PENDING", "PENDING": "PENDING",
    }

    def _order_route(self, symbol: str) -> tuple[str, str, str]:
        """(exchangeSegment, securityId, productType) for an internal symbol. Options
        resolve through the SAME scrip master quotes use and carry as MARGIN (Dhan's NRML);
        a plain symbol is an NSE equity and trades CNC. RAISES for anything unresolvable —
        an order must NEVER fall through to a wrong contract."""
        from skas_algo.engine.options.instrument import parse

        inst = parse(symbol)
        if inst is not None:
            hit = self._option_id(inst)
            if not hit:
                raise ValueError(f"no listed Dhan contract for {symbol} in the scrip master")
            return "NSE_FNO", str(hit[0]), "MARGIN"
        eq = self._master().equity.get(symbol.upper())
        if not eq:
            raise ValueError(f"{symbol} is not a tradable NSE equity in the Dhan scrip master")
        return "NSE_EQ", str(eq), "CNC"

    @staticmethod
    def _correlation_id(order: BrokerOrder) -> str:
        """Dhan caps correlationId at 30 chars and rejects punctuation."""
        raw = order.tag or order.client_order_id or ""
        return "".join(c for c in raw if c.isalnum() or c in "_-")[:30]

    def place_order(self, order: BrokerOrder) -> str:
        """Place a REAL order; returns the Dhan orderId. Double-gated (armed + platform
        flag), exactly like Zerodha."""
        self._ensure_armed()
        segment, security_id, product = self._order_route(order.symbol)
        body = {
            "dhanClientId": self.creds.client_id,
            "transactionType": "BUY" if order.side is OrderSide.BUY else "SELL",
            "exchangeSegment": segment,
            "productType": product,
            "orderType": self._ORDER_TYPE_MAP.get(order.order_type, "MARKET"),
            "validity": "DAY",
            "securityId": security_id,
            "quantity": int(order.quantity),
            # Dhan wants the field present even for MARKET, where it must be 0.
            "price": float(order.price or 0.0) if order.order_type is OrderType.LIMIT else 0.0,
            # Documented "conditionally required" and present in every sample payload Dhan
            # publishes — omitting it is a candidate for the bare 400 seen on 2026-08-21.
            "afterMarketOrder": False,
        }
        if (cid := self._correlation_id(order)):
            body["correlationId"] = cid
        try:
            out = self._http.post("/orders", body) or {}
        except DhanApiError as exc:
            # Re-raise WITH the payload: a rejected order is diagnosed from the pair (what we
            # sent, what Dhan said), and one live retry is expensive. No secrets here — the
            # body is order fields only; the token lives in the headers.
            raise DhanApiError(
                "POST", f"/orders sent={json.dumps(body, sort_keys=True)}",
                exc.status, exc.body) from exc
        out = out or {}
        oid = out.get("orderId")
        if not oid:
            # No id back = nothing to poll, cancel or reconcile against. Fail loudly rather
            # than hand LiveBroker an empty string it would then chase forever.
            raise BrokerLoginError(f"Dhan accepted no order id: {out}")
        return str(oid)

    def modify_order(self, broker_order_id: str, *, order_type: OrderType | None = None,
                     price: float | None = None) -> None:
        """Modify a pending order — the LIMIT→protected-LIMIT escalation path. Dhan's PUT
        wants the order restated, so the CURRENT order is read first and the changed fields
        overlaid; sending a partial body drops quantity/validity and the modify is rejected."""
        self._ensure_armed()
        cur = self._http.get(f"/orders/{broker_order_id}") or {}
        if isinstance(cur, list):          # order-by-id can come back as a 1-element list
            cur = cur[0] if cur else {}
        body = {
            "dhanClientId": self.creds.client_id,
            "orderId": str(broker_order_id),
            "orderType": (self._ORDER_TYPE_MAP.get(order_type, "MARKET")
                          if order_type is not None else cur.get("orderType", "LIMIT")),
            "quantity": int(cur.get("quantity") or 0),
            "validity": cur.get("validity") or "DAY",
            "price": float(price if price is not None else (cur.get("price") or 0.0)),
        }
        if cur.get("disclosedQuantity"):
            body["disclosedQuantity"] = int(cur["disclosedQuantity"])
        self._http.put(f"/orders/{broker_order_id}", body)

    def order_status(self, broker_order_id: str) -> dict:
        """{status, average_price, filled_quantity, status_message, price} for one order,
        in the PLATFORM's vocabulary. Read-only (ungated), like Zerodha's."""
        raw = self._http.get(f"/orders/{broker_order_id}") or {}
        if isinstance(raw, list):
            raw = raw[0] if raw else {}
        native = str(raw.get("orderStatus") or "UNKNOWN").upper()
        return {
            "status": self._STATUS_MAP.get(native, native),
            "average_price": float(raw.get("averageTradedPrice") or 0.0),
            "filled_quantity": int(raw.get("filledQty") or 0),
            "status_message": self._error_message(raw),
            # the order's CURRENT limit price — lets the escalation verify its own modify
            # actually landed (the 2026-08-11 Zerodha lesson, same check applies here).
            "price": float(raw.get("price") or 0.0),
        }

    #: Descriptions Dhan attaches to HEALTHY orders. They are not failure reasons, and
    #: LiveBroker prefers status_message over status when it reports one — so left alone
    #: they produce halts like "BUY 1 ITC → CONFIRMED" (2026-08-24), which names neither
    #: the outcome nor the remedy.
    _BENIGN_OMS = ("CONFIRMED", "TRADE CONFIRMED", "TRADED", "OK", "SUCCESS")

    @classmethod
    def _error_message(cls, raw: dict) -> str | None:
        """Dhan's OMS description, but ONLY when it describes a FAILURE.

        THE STATUS DECIDES, NOT THE CODE (2026-08-27). The first cut of this gate keyed off
        ``omsErrorCode == "0"`` meaning "no error" — but Dhan sends code "0" for BOTH a
        healthy fill ("TRADE CONFIRMED") AND a genuine rejection carrying the real reason.
        So a KTKBANK buy rejected with

            omsErrorCode "0" / "RMS:…:You have insufficient funds. Please add Rs.83.48…"

        surfaced as a bare "BUY 1 KTKBANK → REJECTED" and the owner had to ask Dhan support
        for a reason our own API response already contained. Fixing an uninformative halt
        must not create a differently uninformative one.

        A REJECTED order's description is ALWAYS the reason — surface it whatever the code
        says. Otherwise suppress only text that is a known benign confirmation.
        """
        desc = (raw.get("omsErrorDescription") or "").strip()
        status = str(raw.get("orderStatus") or "").strip().upper()
        if status == "REJECTED":
            return desc or str(raw.get("omsErrorCode") or "").strip() or None
        if desc.upper() in cls._BENIGN_OMS:
            return None
        code = str(raw.get("omsErrorCode") or "").strip().upper()
        if code in ("0", "NA", "NONE") and not desc:
            return None
        return desc or (code if code not in ("0", "NA", "NONE") else None) or None

    def cancel_order(self, broker_order_id: str) -> None:
        self._ensure_armed()
        self._http.delete(f"/orders/{broker_order_id}")

    def funds(self) -> Funds:
        """The account's REAL available balance. Read-only (ungated) — /fundlimit is already
        the endpoint the token check uses. Dhan's key really is spelled "availabelBalance".

        Consumed by value_investing's T+1 funding: an equity CNC sale settles overnight, so a
        strategy that trusts its own ledger will overdraw (live run 23, 2026-08-27 — a ₹1cr
        ledger against a ₹146.03 balance)."""
        raw = self._http.get("/fundlimit") or {}
        return Funds(
            available=float(raw.get("availabelBalance") or 0.0),
            used=float(raw.get("utilizedAmount") or 0.0),
        )

    def positions(self) -> list[dict]:
        """Net book in the shape reconciliation expects: ``tradingsymbol`` + ``quantity``
        per contract (Dhan calls them tradingSymbol / netQty)."""
        raw = self._http.get("/positions") or []
        rows = raw if isinstance(raw, list) else (raw.get("data") or [])
        return [
            {"tradingsymbol": r.get("tradingSymbol"), "quantity": float(r.get("netQty") or 0)}
            for r in rows
            if r.get("tradingSymbol")
        ]

    # ---------------------------------------------------------------- quotes
    def get_quote(self, symbols: list[str]) -> dict[str, float]:
        """LTP per engine symbol via ONE batched marketfeed call. Options resolve to
        NSE_FNO security ids; known indices to IDX_I; other plain symbols to NSE_EQ."""
        from skas_algo.engine.options.instrument import parse

        buckets: dict[str, list[int]] = {}
        back: dict[tuple[str, str], str] = {}  # (segment, sid) -> engine symbol
        for s in symbols:
            inst = parse(s)
            if inst is not None:
                hit = self._option_id(inst)
                if hit:
                    seg, sid = "NSE_FNO", hit[0]
                else:
                    continue
            elif (idx := self._index_id(s)) is not None:
                seg, sid = "IDX_I", idx
            elif (eq := self._master().equity.get(s.upper())) is not None:
                seg, sid = "NSE_EQ", eq
            else:
                continue
            buckets.setdefault(seg, []).append(int(sid))
            back[(seg, str(sid))] = s
        if not buckets:
            return {}
        # Let failures PROPAGATE (mirrors ZerodhaAdapter.get_quote): the live loop turns a
        # raising quote source into quote_error → degraded badge → self-heal on re-login.
        # Notably Dhan 401s with "806: Data APIs not Subscribed" when the account lacks the
        # paid Data plan — swallowing that would look like a silent dead feed.
        data = (self._http.post("/marketfeed/ltp", buckets) or {}).get("data") or {}
        out: dict[str, float] = {}
        for seg, per_id in data.items():
            for sid, q in (per_id or {}).items():
                sym = back.get((seg, str(sid)))
                ltp = (q or {}).get("last_price")
                if sym and ltp is not None:
                    out[sym] = float(ltp)
        return out

    def underlying_ltp(self, underlying: str) -> float | None:
        q = self.get_quote([underlying.upper()])
        return q.get(underlying.upper())

    # ----------------------------------------------------------------- chain
    def _underlying_scrip(self, underlying: str) -> tuple[int, str] | None:
        """(UnderlyingScrip, UnderlyingSeg) — index underlyings quote on IDX_I, stock
        F&O underlyings on their NSE_EQ equity id."""
        u = underlying.upper()
        idx = self._index_id(u)
        if idx is not None:
            return int(idx), "IDX_I"
        eq = self._master().equity.get(u)
        return (int(eq), "NSE_EQ") if eq else None

    def option_underlyings(self) -> list[str]:
        return sorted(self._master().expiries)

    def option_expiries(self, underlying: str) -> list[str]:
        """Listed expiries (ISO), today onward — from the expirylist endpoint, falling
        back to the scrip master's contracts."""
        today = datetime.now().date().isoformat()
        scrip = self._underlying_scrip(underlying)
        if scrip is not None:
            try:
                res = self._http.post("/optionchain/expirylist",
                                      {"UnderlyingScrip": scrip[0], "UnderlyingSeg": scrip[1]})
                exps = [str(e)[:10] for e in (res or {}).get("data") or []]
                if exps:
                    return sorted(e for e in exps if e >= today)
            except Exception:  # pragma: no cover - fall through to the master
                pass
        return sorted(e for e in self._master().expiries.get(underlying.upper(), set())
                      if e >= today)

    def live_option_chain(self, underlying: str, expiry: str, window: int = 40) -> dict | None:
        """Live chain in the Zerodha-adapter shape the screeners/strategies consume:
        {spot, atm_strike, lot_size, rows: [{strike, ce: {ltp, close, oi, bid, ask}, pe}]}.
        NOTE Dhan throttles this endpoint (~1 call / 3 s) — fine for deploy-time pricing,
        NOT for 50-name screener sweeps (those stay on Zerodha)."""
        scrip = self._underlying_scrip(underlying)
        if scrip is None:
            return None
        try:
            res = self._http.post("/optionchain", {
                "UnderlyingScrip": scrip[0], "UnderlyingSeg": scrip[1],
                "Expiry": expiry[:10],
            })
        except Exception:  # pragma: no cover - network/throttle → caller falls back
            return None
        data = (res or {}).get("data") or {}
        spot = data.get("last_price")
        oc = data.get("oc") or {}
        if not spot or not oc:
            return None

        def side(d: dict | None) -> dict | None:
            if not d:
                return None
            return {
                "ltp": d.get("last_price"),
                "close": d.get("previous_close_price") or d.get("last_price"),
                "oi": int(d.get("oi") or 0),
                "bid": d.get("top_bid_price") or None,
                "ask": d.get("top_ask_price") or None,
            }

        strikes = sorted(float(k) for k in oc)
        atm = min(strikes, key=lambda k: abs(k - float(spot)))
        idx_atm = strikes.index(atm)
        keep = strikes[max(0, idx_atm - window): idx_atm + window + 1]  # ±window around ATM

        def row_of(k: float) -> dict:
            d = oc.get(f"{k:.6f}") or oc.get(str(k)) or {}
            return {"strike": k, "ce": side(d.get("ce")), "pe": side(d.get("pe"))}

        rows = [row_of(k) for k in keep]
        lot = self._master().lot.get(underlying.upper(), 0)
        return {"spot": float(spot), "atm_strike": atm, "lot_size": lot, "rows": rows}

    # ---------------------------------------------------------------- margin
    def basket_margin(self, legs: list[dict]) -> float | None:
        """Σ per-leg margin for the SHORT legs via the margin calculator. Dhan has NO
        basket API, so this OVERSTATES the real requirement (no hedge/spread benefit) —
        conservative for %-of-margin stops. None on any failure → callers fall back to
        the model estimate."""
        from skas_algo.engine.options.instrument import parse

        total = 0.0
        seen = False
        for leg in legs:
            if leg.get("direction", 1) >= 0:
                continue  # longs cost premium, not margin — and Dhan can't net them anyway
            inst = parse(leg["symbol"])
            hit = self._option_id(inst) if inst is not None else None
            qty = abs(int(leg.get("units", 0)))
            if not hit or qty <= 0:
                continue
            try:
                res = self._http.post("/margincalculator", {
                    "dhanClientId": self.creds.client_id,
                    "exchangeSegment": "NSE_FNO",
                    "transactionType": "SELL",
                    "quantity": qty,
                    "productType": "MARGIN",
                    "securityId": str(hit[0]),
                    "price": 0,
                })
            except Exception:  # pragma: no cover - any failure → let callers fall back
                return None
            block = res.get("data") if isinstance(res.get("data"), dict) else res
            val = (block or {}).get("totalMargin")
            if val is None:
                return None
            total += float(val)
            seen = True
        return total if seen else None
