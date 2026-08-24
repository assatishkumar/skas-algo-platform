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
