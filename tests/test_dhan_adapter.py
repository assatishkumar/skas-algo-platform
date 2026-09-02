"""DhanAdapter (Phase A: session/quotes/chain/margin — NO order code) with a fake HTTP
client, mirroring test_zerodha_adapter's ``kite=`` injection pattern."""

from __future__ import annotations

import base64
import json
import time
from datetime import date, timedelta

import pytest

import skas_algo.brokers.dhan as dhan_mod
from skas_algo.brokers.dhan import DhanAdapter, DhanCredentials
from skas_algo.brokers.zerodha import BrokerLoginError

# Adapters filter expiries to >= today, so fixture expiries must stay in the future —
# hard-coded dates rotted the suite the morning after they passed (2026-07-29).
_STK_EXP = (date.today() + timedelta(days=60)).isoformat()

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
    (f"NSE,D,50001,OPTSTK,0,BAJAJ-AUTO-Jul2026-9000-CE,75.0,BAJAJ AUTO CALL,"
     f"{_STK_EXP} 15:30:00,9000.00000,CE,0.05,M,OPTSTK,,BAJOPT"),
    (f"BSE,D,1136715,OPTSTK,0,RELIANCE-Jul2026-1490-CE,500.0,BSE twin - ignore,"
     f"{_STK_EXP} 15:30:00,1490.00000,CE,0.05,M,OPTSTK,,RELIOPT"),
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
    assert ("BAJAJ-AUTO", _STK_EXP, 9000.0, "CE") in m.option
    assert ("RELIANCE", _STK_EXP, 1490.0, "CE") not in m.option
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
    responses = {"/marketfeed/quote": {"data": {
        "IDX_I": {"13": {"last_price": 24512.5}},
        "NSE_EQ": {"2885": {"last_price": 1402.2}},
        "NSE_FNO": {"49081": {"last_price": 182.4}},
    }}}
    a, http = _adapter(responses=responses)
    out = a.get_quote(["NIFTY", "RELIANCE", "NIFTY|2026-07-28|24500|CE", "UNKNOWN123"])
    assert out == {"NIFTY": 24512.5, "RELIANCE": 1402.2, "NIFTY|2026-07-28|24500|CE": 182.4}
    path, body = http.posts[-1]
    # ONE endpoint for both callers since the shared cache (2026-09-02): /quote and /ltp
    # share the same per-second budget, and /quote's row answers get_quote AND day_quotes.
    assert path == "/marketfeed/quote"
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
    e1 = (date.today() + timedelta(days=7)).isoformat()
    e2 = (date.today() + timedelta(days=35)).isoformat()
    a, _ = _adapter(responses={"/optionchain/expirylist": {"data": [e1, e2]}})
    assert a.option_expiries("NIFTY") == [e1, e2]
    b, _ = _adapter()  # endpoint returns {} → falls back to the master's contracts
    assert b.option_expiries("BAJAJ-AUTO") == [_STK_EXP]


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


# ------------------------------------------------ shared per-account quote cache (2026-09-02)
#
# The redesign after the third round of 429s: N runs on one Dhan account are N adapters,
# each polling on its own clock — traffic-shaping gates only choose who waits, so demand
# still scaled with run count. The cache DEDUPLICATES demand instead: one fetcher per
# account refreshes the union of every wanted symbol, everyone else reads.

_Q = {"/marketfeed/quote": {"data": {
    "IDX_I": {"13": {"last_price": 24512.5, "prev_close_price": 24400.0}},
    "NSE_EQ": {"2885": {"last_price": 1402.2, "prev_close_price": 1390.0}},
}}}


def test_a_second_consumer_rides_the_first_ones_refresh():
    """Adapter B asking inside the TTL is served from adapter A's refresh — zero extra
    HTTP. This is the property that makes the tenth run on the account cost nothing."""
    a, ha = _adapter(responses=_Q)
    b, hb = _adapter(responses=_Q)
    assert a.get_quote(["NIFTY"]) == {"NIFTY": 24512.5}
    assert b.get_quote(["NIFTY"]) == {"NIFTY": 24512.5}
    # …and the SAME cached row answers day_quotes, prev_close included.
    assert b.day_quotes(["NIFTY"]) == {"NIFTY": {"last": 24512.5, "prev_close": 24400.0}}
    assert len(ha.posts) == 1 and len(hb.posts) == 0, "B must ride A's refresh"


def test_a_new_symbol_refreshes_the_union_so_every_consumer_stays_covered():
    """The fetcher asks for the UNION of everything wanted recently — run 27's tick pays
    for run 28's marks, so the two stop being competitors for the 1/s budget."""
    a, ha = _adapter(responses=_Q)
    b, hb = _adapter(responses=_Q)
    a.get_quote(["NIFTY"])                        # fetch #1: NIFTY alone
    b.get_quote(["RELIANCE"])                     # RELIANCE is stale → fetch #2, the UNION
    assert hb.posts[-1][1] == {"IDX_I": [13], "NSE_EQ": [2885]}
    a.get_quote(["NIFTY", "RELIANCE"])            # both fresh now — nobody fetches again
    b.day_quotes(["NIFTY"])
    assert len(ha.posts) + len(hb.posts) == 2


def test_the_ttl_expires_and_a_fresh_fetch_happens(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(dhan_mod._time, "monotonic", lambda: clock["t"])
    a, http = _adapter(responses=_Q)
    a.get_quote(["NIFTY"])
    a.get_quote(["NIFTY"])
    assert len(http.posts) == 1, "inside the TTL the cache answers"
    clock["t"] += 6.0                             # past the 5s default TTL
    a.get_quote(["NIFTY"])
    assert len(http.posts) == 2


def test_ttl_zero_disables_the_cache(monkeypatch):
    monkeypatch.setenv("SKAS_DHAN_QUOTE_TTL", "0")
    a, http = _adapter(responses=_Q)
    a.get_quote(["NIFTY"])
    a.get_quote(["NIFTY"])
    assert len(http.posts) == 2, "TTL 0 is the escape hatch — every call fetches"


def test_an_unresolvable_symbol_does_not_defeat_the_cache():
    """A symbol with no scrip-master row returns no data — but is STAMPED as covered, so
    it cannot force a fresh HTTP call on every tick (a stealth cache bypass)."""
    a, http = _adapter(responses=_Q)
    assert a.get_quote(["NIFTY", "UNKNOWN123"]) == {"NIFTY": 24512.5}
    assert a.get_quote(["NIFTY", "UNKNOWN123"]) == {"NIFTY": 24512.5}
    assert len(http.posts) == 1


def test_accounts_do_not_share_a_quote_cache():
    """The store is per client id — one account's marks must never answer for another's."""
    a, ha = _adapter(responses=_Q)
    other = FakeHttp(responses=_Q)
    b = DhanAdapter(DhanCredentials(client_id="2000999999"), client=other)
    a.get_quote(["NIFTY"])
    b.get_quote(["NIFTY"])
    assert len(ha.posts) == 1 and len(other.posts) == 1, "separate accounts, separate fetches"


def test_single_flight_one_fetch_for_n_concurrent_askers():
    """Ticks run on real threads (manager._tick_pool), so N runs genuinely race. Exactly
    ONE becomes the fetcher; the rest wait on its event and read the result."""
    import threading

    class SlowHttp(FakeHttp):
        def __init__(self, responses):
            super().__init__(responses=responses)
            self.gate = threading.Event()
            self._l = threading.Lock()
            self.count = 0

        def post(self, path, body):
            with self._l:
                self.count += 1
            self.gate.wait(5.0)               # hold the fetch open so the others pile up
            return super().post(path, body)

    http = SlowHttp(_Q)
    a = DhanAdapter(DhanCredentials(client_id="1000123456"), client=http)
    results: list[dict] = []
    threads = [threading.Thread(target=lambda: results.append(a.get_quote(["NIFTY"])))
               for _ in range(5)]
    for t in threads:
        t.start()
    import time as _t
    _t.sleep(0.25)                            # let every thread reach the cache
    http.gate.set()
    for t in threads:
        t.join(timeout=10.0)
    assert http.count == 1, "five concurrent askers, ONE fetch"
    assert results == [{"NIFTY": 24512.5}] * 5


def test_budget_proof_three_pollers_collapse_to_one_paced_stream(monkeypatch):
    """The 2026-09-02 verification harness in miniature: a fast tick (run 27), a slow tick
    (run 28) and a portfolio sync hammering one account must cost ≤ one fetch per TTL
    window, spaced ~TTL apart — never the same-instant pairs that tripped Dhan's 1/s
    budget. (Time compressed 100×: TTL 0.25s, ticks 30–200ms.)"""
    import threading
    import time as _t

    monkeypatch.setenv("SKAS_DHAN_QUOTE_TTL", "0.25")

    class CountingHttp(FakeHttp):
        def __init__(self, responses):
            super().__init__(responses=responses)
            self.times: list[float] = []
            self._l = threading.Lock()

        def post(self, path, body):
            with self._l:
                self.times.append(_t.monotonic())
            return super().post(path, body)

    http = CountingHttp(_Q)
    creds = DhanCredentials(client_id="1000123456")
    adapters = [DhanAdapter(creds, client=http) for _ in range(3)]
    adapters[0].get_quote(["NIFTY", "RELIANCE"])  # prime: union registered + fresh

    def poll(adapter, symbols, every, n):
        for _ in range(n):
            adapter.get_quote(symbols)
            _t.sleep(every)

    threads = [
        threading.Thread(target=poll, args=(adapters[0], ["NIFTY"], 0.03, 20)),
        threading.Thread(target=poll, args=(adapters[1], ["NIFTY", "RELIANCE"], 0.05, 12)),
        threading.Thread(target=poll, args=(adapters[2], ["RELIANCE"], 0.20, 3)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # ~0.6s of polling on a 0.25s TTL → the prime + at most ~3 refreshes (35 calls asked).
    assert 2 <= len(http.times) <= 5, http.times
    gaps = [b - a for a, b in zip(http.times, http.times[1:], strict=False)]
    assert all(g >= 0.2 for g in gaps), f"refreshes must be TTL-spaced, got gaps {gaps}"
