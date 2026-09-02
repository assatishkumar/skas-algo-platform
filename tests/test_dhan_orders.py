"""Dhan Phase B — the real-order surface, exercised against a FAKE http layer.

CLAUDE.md §1: order-path verification is fake-adapter tests, never a real order. Everything
here asserts the REQUEST Dhan would receive and the translation of its replies; nothing
touches the network. The Dhan-specific risks these pin:
  * the double gate (armed + platform flag) must precede every mutating call;
  * Dhan's status vocabulary is NOT the platform's — a TRADED order that did not map to
    COMPLETE would never be recognised as filled and LiveBroker would cancel a live fill;
  * PART_TRADED must NOT be terminal, or the escalation stops early and books nothing;
  * an unresolvable symbol must RAISE, never fall through to a wrong securityId.
"""

from __future__ import annotations

import pytest

from skas_algo.brokers.base import BrokerOrder
from skas_algo.brokers.dhan import DhanAdapter, DhanCredentials, _Master
from skas_algo.brokers.live_broker import adapter_can_execute
from skas_algo.brokers.zerodha import NotArmedError
from skas_algo.db.enums import OrderSide, OrderType

OPT = "NIFTY|2026-08-25|24000|CE"


class FakeHttp:
    """Records every call and replays scripted replies."""

    def __init__(self, replies: dict | None = None):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.replies = replies or {}

    def _reply(self, verb, path):
        return self.replies.get((verb, path), self.replies.get(verb, {}))

    def post(self, path, body):
        self.calls.append(("POST", path, body))
        return self._reply("POST", path)

    def get(self, path):
        self.calls.append(("GET", path, None))
        return self._reply("GET", path)

    def put(self, path, body):
        self.calls.append(("PUT", path, body))
        return self._reply("PUT", path)

    def delete(self, path):
        self.calls.append(("DELETE", path, None))
        return self._reply("DELETE", path)


def _master() -> _Master:
    m = _Master()
    m.option[("NIFTY", "2026-08-25", 24000.0, "CE")] = ("43492", 65)
    m.option_ts[("NIFTY", "2026-08-25", 24000.0, "CE")] = "NIFTY-Aug2026-24000-CE"
    m.equity["ITC"] = "1660"
    return m


def _adapter(http: FakeHttp, *, armed=True, live=True, monkeypatch=None) -> DhanAdapter:
    a = DhanAdapter(DhanCredentials(client_id="1112402726"),
                    armed=armed, live_enabled=live, client=http)
    a._master = lambda: _master()          # no network, no 26 MB CSV
    return a


# ------------------------------------------------------------------ the gate
def test_the_double_gate_precedes_every_mutating_call():
    """Disarmed, or the platform flag off → NOTHING reaches Dhan. Reads stay open."""
    order = BrokerOrder(OPT, OrderSide.BUY, 65, OrderType.LIMIT, price=10.0)
    for armed, live in ((False, True), (True, False), (False, False)):
        http = FakeHttp()
        a = _adapter(http, armed=armed, live=live)
        with pytest.raises(NotArmedError):
            a.place_order(order)
        with pytest.raises(NotArmedError):
            a.modify_order("123", price=11.0)
        with pytest.raises(NotArmedError):
            a.cancel_order("123")
        assert http.calls == [], f"armed={armed} live={live} still hit the API"


def test_read_paths_are_ungated():
    http = FakeHttp({("GET", "/orders/123"): {"orderStatus": "PENDING"}})
    a = _adapter(http, armed=False, live=False)
    assert a.order_status("123")["status"] == "PENDING"


def test_the_adapter_now_satisfies_the_live_broker_surface():
    assert adapter_can_execute(_adapter(FakeHttp())) is True


# --------------------------------------------------------------- place order
def test_place_order_sends_the_documented_option_payload():
    http = FakeHttp({("POST", "/orders"): {"orderId": "112111182198", "orderStatus": "TRANSIT"}})
    a = _adapter(http)
    oid = a.place_order(BrokerOrder(OPT, OrderSide.SELL, 65, OrderType.LIMIT, price=123.45,
                                    tag="dnm_entry"))
    assert oid == "112111182198"
    verb, path, body = http.calls[0]
    assert (verb, path) == ("POST", "/orders")
    assert body == {
        "dhanClientId": "1112402726",
        "transactionType": "SELL",
        "exchangeSegment": "NSE_FNO",     # options segment
        "productType": "MARGIN",          # Dhan's NRML — carry forward, matches Zerodha
        "orderType": "LIMIT",
        "validity": "DAY",
        "securityId": "43492",            # from the scrip master, not guessed
        "quantity": 65,
        "price": 123.45,
        "afterMarketOrder": False,        # present in every payload Dhan documents
        "correlationId": "dnm_entry",
    }


def test_a_market_order_still_carries_a_zero_price_field():
    """Dhan requires ``price`` to be present even for MARKET, where it must be 0."""
    http = FakeHttp({("POST", "/orders"): {"orderId": "1"}})
    _adapter(http).place_order(BrokerOrder(OPT, OrderSide.BUY, 65, OrderType.MARKET))
    body = http.calls[0][2]
    assert body["orderType"] == "MARKET" and body["price"] == 0.0


def test_an_equity_order_routes_cnc_on_the_cash_segment():
    http = FakeHttp({("POST", "/orders"): {"orderId": "1"}})
    _adapter(http).place_order(BrokerOrder("ITC", OrderSide.BUY, 1, OrderType.LIMIT, price=400.0))
    body = http.calls[0][2]
    assert body["exchangeSegment"] == "NSE_EQ" and body["productType"] == "CNC"
    assert body["securityId"] == "1660"


def test_an_unresolvable_symbol_raises_rather_than_ordering_the_wrong_contract():
    a = _adapter(FakeHttp())
    with pytest.raises(ValueError, match="no listed Dhan contract"):
        a.place_order(BrokerOrder("NIFTY|2026-12-30|99000|CE", OrderSide.BUY, 65))
    with pytest.raises(ValueError, match="not a tradable NSE equity"):
        a.place_order(BrokerOrder("NOTALISTEDNAME", OrderSide.BUY, 1))


def test_a_reply_without_an_order_id_fails_loudly():
    """No id = nothing to poll, cancel or reconcile — LiveBroker must not be handed ''."""
    from skas_algo.brokers.zerodha import BrokerLoginError

    http = FakeHttp({("POST", "/orders"): {"orderStatus": "REJECTED"}})
    with pytest.raises(BrokerLoginError):
        _adapter(http).place_order(BrokerOrder(OPT, OrderSide.BUY, 65))


def test_the_correlation_id_is_sanitised_to_dhans_rules():
    http = FakeHttp({("POST", "/orders"): {"orderId": "1"}})
    _adapter(http).place_order(
        BrokerOrder(OPT, OrderSide.BUY, 65, tag="run#203 · roll/exit — " + "x" * 40))
    cid = http.calls[0][2]["correlationId"]
    assert len(cid) <= 30 and all(c.isalnum() or c in "_-" for c in cid)


# -------------------------------------------------------------- status mapping
@pytest.mark.parametrize("native,expected", [
    ("TRADED", "COMPLETE"),        # the fill — LiveBroker books ONLY on COMPLETE
    ("REJECTED", "REJECTED"),
    ("CANCELLED", "CANCELLED"),
    ("EXPIRED", "CANCELLED"),      # terminal + unfilled → same handling as a cancel
    ("TRANSIT", "PENDING"),
    ("PENDING", "PENDING"),
])
def test_dhan_statuses_map_to_the_platform_vocabulary(native, expected):
    http = FakeHttp({("GET", "/orders/9"): {"orderStatus": native}})
    assert _adapter(http).order_status("9")["status"] == expected


def test_part_traded_is_deliberately_not_terminal():
    """If PART_TRADED were mapped into _TERMINAL the escalation would stop early and the
    unfilled remainder would never be cancelled and booked."""
    from skas_algo.brokers.live_broker import _TERMINAL

    http = FakeHttp({("GET", "/orders/9"): {"orderStatus": "PART_TRADED", "filledQty": 30}})
    st = _adapter(http).order_status("9")
    assert st["status"] not in _TERMINAL and st["filled_quantity"] == 30


def test_order_status_translates_every_field_live_broker_reads():
    http = FakeHttp({("GET", "/orders/9"): {
        "orderStatus": "TRADED", "averageTradedPrice": 124.6, "filledQty": 65,
        "price": 123.45, "omsErrorDescription": None}})
    st = _adapter(http).order_status("9")
    assert st == {"status": "COMPLETE", "average_price": 124.6, "filled_quantity": 65,
                  "status_message": None, "price": 123.45}


def test_a_rejection_reason_reaches_the_halt_message():
    http = FakeHttp({("GET", "/orders/9"): {
        "orderStatus": "REJECTED", "omsErrorDescription": "RMS: margin shortfall"}})
    assert _adapter(http).order_status("9")["status_message"] == "RMS: margin shortfall"


# --------------------------------------------------------------- modify/cancel
def test_modify_restates_the_order_because_dhan_rejects_a_partial_body():
    http = FakeHttp({
        ("GET", "/orders/9"): {"orderType": "LIMIT", "quantity": 65, "validity": "DAY",
                               "price": 123.45},
        ("PUT", "/orders/9"): {"orderId": "9", "orderStatus": "PENDING"}})
    _adapter(http).modify_order("9", order_type=OrderType.LIMIT, price=130.0)
    assert http.calls[0][0] == "GET"                 # reads the live order first
    verb, path, body = http.calls[1]
    assert (verb, path) == ("PUT", "/orders/9")
    assert body == {"dhanClientId": "1112402726", "orderId": "9", "orderType": "LIMIT",
                    "quantity": 65, "validity": "DAY", "price": 130.0}


def test_cancel_uses_delete():
    http = FakeHttp()
    _adapter(http).cancel_order("9")
    assert http.calls == [("DELETE", "/orders/9", None)]


# ------------------------------------------------------------- reconciliation
def test_positions_are_reshaped_for_the_reconciler():
    """manager._book_mismatch reads p["tradingsymbol"] / p["quantity"] and compares against
    _option_tradingsymbol(); Dhan calls them tradingSymbol / netQty."""
    http = FakeHttp({("GET", "/positions"): [
        {"tradingSymbol": "NIFTY-Aug2026-24000-CE", "netQty": -65},
        {"tradingSymbol": "ITC", "netQty": 1},
        {"netQty": 5},                                  # malformed row is dropped
    ]})
    assert _adapter(http).positions() == [
        {"tradingsymbol": "NIFTY-Aug2026-24000-CE", "quantity": -65.0},
        {"tradingsymbol": "ITC", "quantity": 1.0},
    ]


def test_the_reconciler_can_name_our_contracts_the_way_dhan_does():
    from skas_algo.engine.options.instrument import parse

    a = _adapter(FakeHttp())
    assert a._option_tradingsymbol(parse(OPT)) == "NIFTY-Aug2026-24000-CE"


def test_a_rejected_order_reports_both_what_we_sent_and_what_dhan_said():
    """A bare "400 Client Error" cost a live smoke-test round trip (2026-08-21): Dhan puts the
    real complaint in the response BODY, and diagnosing a rejection needs the payload beside
    it. Both must reach the halt message."""
    from skas_algo.brokers.dhan import DhanApiError

    class Rejecting(FakeHttp):
        def post(self, path, body):
            self.calls.append(("POST", path, body))
            raise DhanApiError("POST", path, 400,
                               '{"errorType":"Input_Exception","errorMessage":"Invalid Security Id"}')

    http = Rejecting()
    with pytest.raises(DhanApiError) as ei:
        _adapter(http).place_order(BrokerOrder(OPT, OrderSide.BUY, 65, OrderType.MARKET))
    msg = str(ei.value)
    assert "Invalid Security Id" in msg          # what Dhan said
    assert '"securityId": "43492"' in msg        # what we sent
    assert '"orderType": "MARKET"' in msg


def test_a_benign_oms_description_is_not_reported_as_a_failure():
    """Dhan fills ``omsErrorDescription`` on healthy orders too. A cancelled ITC buy on
    2026-08-24 carried omsErrorCode "0" + description "CONFIRMED"; LiveBroker prefers
    status_message over status when reporting a failure, so the halt read
    "BUY 1 ITC → CONFIRMED" — a word that names neither the outcome nor the remedy."""
    http = FakeHttp({("GET", "/orders/221260824507203"): {
        "orderStatus": "CANCELLED", "filledQty": 0, "averageTradedPrice": 0.0,
        "price": 267.3, "omsErrorCode": "0", "omsErrorDescription": "CONFIRMED"}})
    st = _adapter(http).order_status("221260824507203")
    assert st["status"] == "CANCELLED"
    assert st["status_message"] is None       # → the caller falls back to the real status

    # A real error code still carries its reason through.
    http2 = FakeHttp({("GET", "/orders/9"): {
        "orderStatus": "REJECTED", "omsErrorCode": "DH-906",
        "omsErrorDescription": "Insufficient funds"}})
    assert _adapter(http2).order_status("9")["status_message"] == "Insufficient funds"


def test_a_rejection_reason_survives_dhans_zero_error_code():
    """THE STATUS DECIDES, NOT THE CODE (2026-08-27 regression).

    The first version of the benign-description gate keyed off ``omsErrorCode == "0"``
    meaning "no error" — but Dhan sends "0" for BOTH a healthy fill AND a genuine rejection
    whose description IS the reason. A live KTKBANK buy was rejected with code "0" and
    "RMS:…insufficient funds. Please add Rs.83.48 to trade", the gate threw it away, the
    halt read a bare "BUY 1 KTKBANK → REJECTED", and the owner had to ask Dhan support for
    a reason our own API response already held."""
    http = FakeHttp({("GET", "/orders/34126082749003"): {
        "orderStatus": "REJECTED", "filledQty": 0, "omsErrorCode": "0",
        "omsErrorDescription":
            "RMS:34126082749003:You have insufficient funds. Please add Rs.83.48 to trade."}})
    st = _adapter(http).order_status("34126082749003")
    assert st["status"] == "REJECTED"
    assert "insufficient funds" in st["status_message"]
    assert "83.48" in st["status_message"], "the actionable number must reach the halt banner"

    # …while a HEALTHY order's confirmation text is still suppressed (the 08-24 fix holds).
    for benign in ("TRADE CONFIRMED", "CONFIRMED"):
        h = FakeHttp({("GET", "/orders/9"): {
            "orderStatus": "TRADED", "filledQty": 1, "omsErrorCode": "0",
            "omsErrorDescription": benign}})
        assert _adapter(h).order_status("9")["status_message"] is None, benign


def test_funds_reads_the_real_available_balance():
    """value_investing's T+1 funding asks the BROKER what it can spend, because its own cash
    ledger is whatever `capital` was typed at deploy — ₹1,00,00,000 against a ₹146.03 balance
    on live run 23. Note Dhan really does spell it "availabelBalance"."""
    http = FakeHttp({("GET", "/fundlimit"): {
        "availabelBalance": 146.03, "utilizedAmount": 387.0, "sodLimit": 533.03}})
    f = _adapter(http).funds()
    assert f.available == 146.03 and f.used == 387.0

    # A missing/empty payload must read as ZERO, never as "unknown" — a strategy that treats
    # None as "no cap" would go right back to overdrawing.
    assert _adapter(FakeHttp({("GET", "/fundlimit"): {}})).funds().available == 0.0


def test_the_account_rate_gate_holds_no_matter_how_many_runs_ask():
    """Dhan's limits are per ACCOUNT, so per-run throttles cannot enforce them: two runs on
    60s timers are independent clocks and nothing stops them firing in the same second —
    more strategies makes a collision MORE likely, not less (owner, 2026-08-31). One paper
    run polling the chain every 15s pushed the account to HTTP 429 and took the LIVE equity
    run's quotes down with it. The budget is enforced at the single choke point instead."""
    import threading

    from skas_algo.brokers.dhan import _RATE_LAST, DhanThrottled, _rate_gate, _rate_kind

    assert _rate_kind("/orders") is None, "the ORDER path is never gated"
    assert _rate_kind("/orders/123") is None
    assert _rate_kind("/v2/optionchain") == "chain"
    assert _rate_kind("/marketfeed/ltp") == "quote"
    assert _rate_kind("/holdings") == "data"

    _RATE_LAST.clear()
    ok, shed = [], []

    def ask():
        try:
            _rate_gate("CID", "/v2/optionchain")
            ok.append(1)
        except DhanThrottled:
            shed.append(1)

    threads = [threading.Thread(target=ask) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 8 simultaneous askers, a 3s gap and a 4s queue cap → a couple pace through, the rest
    # SHED. Shedding is the point: callers fall back to their last mark, and the account
    # stays under budget so the live run keeps its quotes.
    assert ok and shed, f"expected pacing AND shedding, got {len(ok)}/{len(shed)}"
    assert len(ok) <= 3, "the account must not exceed roughly one chain call per 3s"

    _RATE_LAST.clear()
    for _ in range(5):                       # orders never queue behind a data poll
        _rate_gate("CID", "/orders")


def test_the_http_client_keeps_every_method():
    """A module-level block was once inserted INSIDE this class body, which silently ended
    the class early and stripped every method after it — Dhan quotes died on the VPS with
    "'_DhanHttp' object has no attribute 'fetch_master'" (2026-09-01). Imports still passed,
    so nothing caught it until a live run tried to read a price. Second time this shape of
    edit has bitten (volcano's _size_multiple, 2026-08-25); pin the surface."""
    from skas_algo.brokers.dhan import _DhanHttp

    for name in ("_headers", "_check", "_call", "post", "get", "put", "delete", "fetch_master"):
        assert callable(getattr(_DhanHttp, name, None)), f"_DhanHttp lost {name}()"


# ------------------------------------------------------------------ rate gate & retries


def test_the_account_gate_serialises_calls_of_DIFFERENT_kinds(monkeypatch):
    """The per-kind gate alone gave quotes and /holdings their own 1/s budget each, so both
    left in the SAME SECOND — which is precisely what the 2026-09-02 logs show (run 28,
    11:10:57: a marketfeed call and a holdings call, both rejected). Dhan meters the Data API
    family per client, so the account floor has to bind across kinds."""
    from skas_algo.brokers import dhan

    monkeypatch.setattr(dhan, "_RATE_LAST", {})
    slept: list[float] = []
    monkeypatch.setattr(dhan._time, "sleep", lambda s: slept.append(s))
    clock = {"t": 1000.0}
    monkeypatch.setattr(dhan._time, "monotonic", lambda: clock["t"])

    dhan._rate_gate("C1", "/marketfeed/ltp")     # first call: free
    assert slept == []
    dhan._rate_gate("C1", "/holdings")           # a DIFFERENT kind, same instant
    assert slept and slept[0] >= 1.0, "the second call must wait on the account floor"


def test_a_different_account_is_not_held_up_by_another(monkeypatch):
    """The budget is per client id; one account's traffic must not throttle another's."""
    from skas_algo.brokers import dhan

    monkeypatch.setattr(dhan, "_RATE_LAST", {})
    slept: list[float] = []
    monkeypatch.setattr(dhan._time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(dhan._time, "monotonic", lambda: 1000.0)

    dhan._rate_gate("C1", "/marketfeed/ltp")
    dhan._rate_gate("C2", "/marketfeed/ltp")
    assert slept == []


def test_the_order_path_is_never_gated(monkeypatch):
    """An exit that waits on a quote budget is a position left open."""
    from skas_algo.brokers import dhan

    monkeypatch.setattr(dhan, "_RATE_LAST", {})
    slept: list[float] = []
    monkeypatch.setattr(dhan._time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(dhan._time, "monotonic", lambda: 1000.0)

    for _ in range(5):
        dhan._rate_gate("C1", "/orders")
    assert slept == []


def test_a_gateway_5xx_is_retried_once_but_a_429_is_not(monkeypatch):
    """A 502 is Dhan's edge having a moment and says nothing about the account. A 429 says
    the account is over budget — asking again immediately is the opposite of the right move."""
    from skas_algo.brokers import dhan

    monkeypatch.setattr(dhan, "_RATE_LAST", {})
    monkeypatch.setattr(dhan._time, "sleep", lambda s: None)
    monkeypatch.setattr(dhan._time, "monotonic", lambda: 1000.0)

    class _Resp:
        def __init__(self, code):
            self.status_code = code
            self.text = "{}"
        def json(self):
            return {"ok": True}

    def responder(codes: list[int], seen: list[str]):
        """Bound explicitly — a closure over the loop variables would read whatever the LAST
        iteration left behind, which is how a test like this quietly stops testing."""
        def fake(verb, url, **kw):
            seen.append(url)
            return _Resp(codes[len(seen) - 1] if len(seen) <= len(codes) else 200)
        return fake

    for codes, expected_calls in (([502, 200], 2), ([429, 200], 1), ([200], 1)):
        seen: list[str] = []
        monkeypatch.setattr("requests.request", responder(codes, seen))
        http = dhan._DhanHttp("C1")
        try:
            http.get("/holdings")
        except Exception:
            pass                      # a 429 raises; the call COUNT is what matters here
        assert len(seen) == expected_calls, codes
