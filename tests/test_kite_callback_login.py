"""One-tap Zerodha login (owner 2026-10-03): the login URL carries the account and the
browser's origin through Kite (redirect_params), the origin is allow-listed, and with no
return_to the URL is exactly as before."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from skas_algo.brokers.zerodha import ZerodhaAdapter, ZerodhaCredentials
from skas_algo.db.base import session_scope
from skas_algo.db.models import BrokerAccount


def test_the_adapter_appends_redirect_params_only_when_given():
    a = ZerodhaAdapter(ZerodhaCredentials(api_key="k", api_secret="s", user_id="U"),
                       armed=False, live_enabled=False)
    plain = a.login_url()
    assert "redirect_params" not in plain
    url = a.login_url(redirect_params={"account": "1", "return_to": "https://x.y.ts.net"})
    rp = parse_qs(urlparse(url).query)["redirect_params"][0]
    assert parse_qs(rp) == {"account": ["1"], "return_to": ["https://x.y.ts.net"]}
    assert url.startswith(plain)


@pytest.fixture
def account(monkeypatch):
    from skas_algo.services import broker as svc

    class _Fake:
        def login_url(self, redirect_params=None):
            base = "https://kite.zerodha.com/connect/login?api_key=k&v=3"
            if redirect_params:
                from urllib.parse import quote, urlencode

                base += "&redirect_params=" + quote(urlencode(redirect_params), safe="")
            return base

    monkeypatch.setattr(svc, "make_adapter", lambda acct: _Fake())
    with session_scope() as db:
        acct = BrokerAccount(broker="zerodha", label="Ops Kite", user_id="X1")
        db.add(acct)
        db.commit()
        aid = acct.id
    yield aid
    with session_scope() as db:
        db.delete(db.get(BrokerAccount, aid))
        db.commit()


def test_login_url_carries_account_and_an_allowed_origin(client: TestClient, account):
    r = client.get(f"/api/v1/brokers/{account}/login-url",
                   params={"return_to": "https://algo-box.tailnet0.ts.net/"})
    assert r.status_code == 200
    rp = parse_qs(urlparse(r.json()["login_url"]).query)["redirect_params"][0]
    assert parse_qs(rp) == {"account": [str(account)],
                            "return_to": ["https://algo-box.tailnet0.ts.net"]}
    r = client.get(f"/api/v1/brokers/{account}/login-url",
                   params={"return_to": "http://localhost:5173"})
    assert r.status_code == 200


def test_a_foreign_origin_is_refused_and_no_origin_is_the_old_url(client: TestClient, account):
    for bad in ("https://evil.com", "https://evil.ts.net.attacker.io", "javascript:alert(1)"):
        r = client.get(f"/api/v1/brokers/{account}/login-url", params={"return_to": bad})
        assert r.status_code == 422, bad
    r = client.get(f"/api/v1/brokers/{account}/login-url")
    assert r.status_code == 200 and "redirect_params" not in r.json()["login_url"]


def test_a_huge_short_run_ratio_does_not_overflow_the_report():
    """2026-10-03: BIDS run 38 adopted ₹1.07Cr of ETFs on ₹30k of capital and was four days
    old — the CAGR line computed ~357**90, OverflowError, and every report read 500'd."""
    from datetime import date, timedelta

    from skas_algo.engine.metrics import compute_metrics
    from skas_algo.engine.runner import RunResult

    d0 = date(2026, 9, 29)
    hist = [{"date": d0 + timedelta(days=i), "total_equity": 30_000.0 if i == 0 else 1.07e7,
             "cash": 30_000.0} for i in range(4)]
    m = compute_metrics(RunResult(history=hist, transactions=[]), 30_000.0)
    assert m["CAGR %"] == 0.0
