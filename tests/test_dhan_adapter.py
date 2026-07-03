"""DhanAdapter (Phase A: session/quotes/chain/margin — NO order code) with a fake HTTP
client, mirroring test_zerodha_adapter's ``kite=`` injection pattern."""

from __future__ import annotations

import base64
import json
import time

import pytest

import skas_algo.brokers.dhan as dhan_mod
from skas_algo.brokers.dhan import DhanAdapter, DhanCredentials
from skas_algo.brokers.zerodha import BrokerLoginError

_HDR = ("SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,"
        "SEM_EXPIRY_CODE,SEM_TRADING_SYMBOL,SEM_LOT_UNITS,SEM_CUSTOM_SYMBOL,"
        "SEM_EXPIRY_DATE,SEM_STRIKE_PRICE,SEM_OPTION_TYPE,SEM_TICK_SIZE,"
        "SEM_EXPIRY_FLAG,SEM_EXCH_INSTRUMENT_TYPE,SEM_SERIES,SM_SYMBOL_NAME")
MASTER_CSV = "\n".join([
    _HDR,
    "NSE,I,13,INDEX,0,NIFTY,1.0,Nifty 50,0001-01-01,,XX,0.05,,INDEX,X,NIFTY",
    "NSE,I,21,INDEX,0,INDIA VIX,1.0,India VIX,0001-01-01,,XX,0.05,,INDEX,X,INDIA VIX",
    "NSE,E,2885,EQUITY,0,RELIANCE,1.0,Reliance,0001-01-01,,XX,0.05,,ES,EQ,RELIANCE",
    ("NSE,D,49081,OPTIDX,0,NIFTY-Jul2026-24500-CE,65.0,NIFTY 28 JUL 24500 CALL,"
     "2026-07-28 14:30:00,24500.00000,CE,0.05,M,OP,,"),
    ("NSE,D,49082,OPTIDX,0,NIFTY-Jul2026-24500-PE,65.0,NIFTY 28 JUL 24500 PUT,"
     "2026-07-28 14:30:00,24500.00000,PE,0.05,M,OP,,"),
    ("NSE,D,50001,OPTSTK,0,BAJAJ-AUTO-Jul2026-9000-CE,75.0,BAJAJ AUTO CALL,"
     "2026-07-30 15:30:00,9000.00000,CE,0.05,M,OPTSTK,,BAJOPT"),
    ("BSE,D,1136715,OPTSTK,0,RELIANCE-Jul2026-1490-CE,500.0,BSE twin - ignore,"
     "2026-07-30 15:30:00,1490.00000,CE,0.05,M,OPTSTK,,RELIOPT"),
])


def _jwt(exp_epoch: int) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp_epoch}).encode()).decode().rstrip("=")
    return f"hdr.{body}.sig"


class FakeHttp:
    """Stub of _DhanHttp: canned responses per path + request capture."""

    def __init__(self, responses: dict | None = None, fail_get: bool = False):
        self.client_id = "1000123456"
        self.token = None
        self.responses = responses or {}
        self.fail_get = fail_get
        self.posts: list[tuple[str, dict]] = []

    def post(self, path, body):
        self.posts.append((path, body))
        return self.responses.get(path, {})

    def get(self, path):
        if self.fail_get:
            raise RuntimeError("401 unauthorized")
        return {}

    def fetch_master(self):
        return MASTER_CSV


@pytest.fixture(autouse=True)
def _fresh_master():
    # The scrip master is module-cached (adapters are per-request); reset per test so
    # each fake's CSV is what gets parsed.
    dhan_mod._master_cache = None
    dhan_mod._master_at = 0.0
    yield
    dhan_mod._master_cache = None


def _adapter(**kw) -> tuple[DhanAdapter, FakeHttp]:
    http = FakeHttp(**kw)
    return DhanAdapter(DhanCredentials(client_id="1000123456"), client=http), http


def test_master_parse_hyphenated_underlying_and_bse_filter():
    a, _ = _adapter()
    m = a._master()
    # rsplit keeps BAJAJ-AUTO whole; BSE twin rows never land.
    assert ("BAJAJ-AUTO", "2026-07-30", 9000.0, "CE") in m.option
    assert ("RELIANCE", "2026-07-30", 1490.0, "CE") not in m.option
    assert m.index["NIFTY"] == "13" and m.index["INDIA VIX"] == "21"
    assert m.equity["RELIANCE"] == "2885"
    assert m.lot["NIFTY"] == 65 and m.lot["BAJAJ-AUTO"] == 75


def test_token_adopts_jwt_expiry_and_validates():
    a, _ = _adapter()
    exp = int(time.time()) + 20 * 3600
    sess = a.exchange_request_token(_jwt(exp))
    assert sess.access_token.startswith("hdr.")
    assert abs(sess.expires_at.timestamp() - (exp - time.timezone * 0)) < 24 * 3600  # naive-UTC
    # A rejected token must NOT be adopted (fundlimit probe fails).
    b, _ = _adapter(fail_get=True)
    with pytest.raises(BrokerLoginError):
        b.exchange_request_token(_jwt(exp))
    assert b.access_token is None


def test_get_quote_buckets_and_maps_back():
    responses = {"/marketfeed/ltp": {"data": {
        "IDX_I": {"13": {"last_price": 24512.5}},
        "NSE_EQ": {"2885": {"last_price": 1402.2}},
        "NSE_FNO": {"49081": {"last_price": 182.4}},
    }}}
    a, http = _adapter(responses=responses)
    out = a.get_quote(["NIFTY", "RELIANCE", "NIFTY|2026-07-28|24500|CE", "UNKNOWN123"])
    assert out == {"NIFTY": 24512.5, "RELIANCE": 1402.2, "NIFTY|2026-07-28|24500|CE": 182.4}
    path, body = http.posts[-1]
    assert path == "/marketfeed/ltp"
    assert body == {"IDX_I": [13], "NSE_EQ": [2885], "NSE_FNO": [49081]}  # one batched call


def test_live_option_chain_adapts_to_zerodha_shape():
    oc = {
        "24400.000000": {"ce": {"last_price": 240.0, "oi": 100, "top_bid_price": 239.0,
                                "top_ask_price": 241.0, "previous_close_price": 250.0},
                         "pe": {"last_price": 120.0, "oi": 200, "top_bid_price": 119.5,
                                "top_ask_price": 120.5, "previous_close_price": 118.0}},
        "24500.000000": {"ce": {"last_price": 182.4, "oi": 300, "top_bid_price": 182.0,
                                "top_ask_price": 183.0, "previous_close_price": 190.0},
                         "pe": {"last_price": 160.0, "oi": 400, "top_bid_price": 159.0,
                                "top_ask_price": 161.0, "previous_close_price": 158.0}},
    }
    a, http = _adapter(responses={"/optionchain": {"data": {"last_price": 24512.5, "oc": oc}}})
    chain = a.live_option_chain("NIFTY", "2026-07-28")
    assert chain["spot"] == 24512.5 and chain["atm_strike"] == 24500.0
    assert chain["lot_size"] == 65
    row = next(r for r in chain["rows"] if r["strike"] == 24500.0)
    assert row["ce"] == {"ltp": 182.4, "close": 190.0, "oi": 300, "bid": 182.0, "ask": 183.0}
    # underlying scrip resolved to the IDX_I index id
    assert http.posts[-1][1]["UnderlyingScrip"] == 13
    assert http.posts[-1][1]["UnderlyingSeg"] == "IDX_I"


def test_option_expiries_endpoint_then_master_fallback():
    a, _ = _adapter(responses={"/optionchain/expirylist": {"data": ["2026-07-28", "2026-08-25"]}})
    assert a.option_expiries("NIFTY") == ["2026-07-28", "2026-08-25"]
    b, _ = _adapter()  # endpoint returns {} → falls back to the master's contracts
    assert b.option_expiries("BAJAJ-AUTO") == ["2026-07-30"]


def test_basket_margin_sums_short_legs_only():
    a, http = _adapter(responses={"/margincalculator": {"data": {"totalMargin": 123000.0}}})
    legs = [
        {"symbol": "NIFTY|2026-07-28|24500|CE", "direction": -1, "units": 130},
        {"symbol": "NIFTY|2026-07-28|24500|PE", "direction": -1, "units": 130},
        {"symbol": "NIFTY|2026-07-28|24500|CE", "direction": 1, "units": 65},  # long — skipped
    ]
    assert a.basket_margin(legs) == 246000.0
    assert len([p for p in http.posts if p[0] == "/margincalculator"]) == 2
    assert all(b["transactionType"] == "SELL" for _, b in http.posts if _ == "/margincalculator")


def test_make_adapter_dispatches_on_broker():
    from skas_algo.brokers.zerodha import ZerodhaAdapter
    from skas_algo.services.broker import make_adapter

    class Acct:  # duck-typed BrokerAccount
        broker = "dhan"
        user_id = "1000123456"
        api_key = None
        enc_api_secret = None
        session_token = None
        armed = False

    assert isinstance(make_adapter(Acct()), DhanAdapter)
    Acct.broker = "zerodha"
    assert isinstance(make_adapter(Acct()), ZerodhaAdapter)


def test_api_dhan_account_guards():
    """Connect a dhan account via the API: refresh-cache must 400 (data is Kite-coupled)
    and a broker quote source must match the account's broker."""
    from fastapi import HTTPException
    from fastapi.testclient import TestClient

    from skas_algo.api import create_app
    from skas_algo.api.routes.live import _build_quote_source
    from skas_algo.db.base import get_session

    client = TestClient(create_app())
    resp = client.post("/api/v1/brokers", json={
        "broker": "dhan", "label": "Dhan Test", "api_key": "",
        "api_secret": "", "user_id": "1000123456",
    })
    assert resp.status_code == 200, resp.text
    acct_id = resp.json()["id"]

    # Data refresh rides the Kite session — a dhan account gets a clear 4xx, not a crash
    # (the route's session pre-check fires first here; the broker guard is below).
    r = client.post(f"/api/v1/brokers/{acct_id}/refresh-cache", json={"symbols": ["RELIANCE"]})
    assert r.status_code in (400, 422), r.text
    from skas_algo.db.models import BrokerAccount
    from skas_algo.services import broker as broker_svc
    db0 = get_session()
    try:
        with pytest.raises(BrokerLoginError, match="Zerodha session"):
            broker_svc.make_data_session(db0.get(BrokerAccount, acct_id))
    finally:
        db0.close()

    # quote_source "zerodha" must not ride a dhan account (and vice versa).
    db = get_session()
    try:
        with pytest.raises(HTTPException) as ei:
            _build_quote_source("zerodha", acct_id, lambda *a: None, db)
        assert ei.value.status_code == 400 and "zerodha account" in ei.value.detail
    finally:
        db.close()
