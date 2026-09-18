"""services/peer — the read-only client for the VPS. The pin that matters: it can only
GET, and only the handful of read surfaces the console fork needs (CLAUDE.md §1)."""

from __future__ import annotations

import inspect

import httpx
import pytest

from skas_algo.services import peer


@pytest.fixture
def configured(monkeypatch):
    from skas_algo.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "peer_api_url", "https://algo-ubuntu.example.ts.net/")
    monkeypatch.setattr(s, "peer_api_token", "tok")
    yield s


def test_every_helper_issues_only_gets_to_whitelisted_paths(configured, monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/live/deployments"):
            return httpx.Response(200, json=[{"run_id": 31, "name": "bn", "mode": "PAPER",
                                              "status": "active"}])
        if path.endswith("/live/31/trades"):
            return httpx.Response(200, json={"run_id": 31, "trades": [{"ticker": "x"}]})
        if path.endswith("/runs/31/cycles"):
            return httpx.Response(200, json={"run_id": 31, "name": "bn", "capital": 1.0,
                                             "cycles": [{"index": 0}]})
        return httpx.Response(404)

    monkeypatch.setattr(peer, "_transport", httpx.MockTransport(handler))
    assert peer.configured() and peer.base_url() == "https://algo-ubuntu.example.ts.net"
    assert peer.deployments()[0]["run_id"] == 31
    assert peer.trades(31) == [{"ticker": "x"}]
    assert peer.run_cycles(31)["cycles"] == [{"index": 0}]
    assert {r.method for r in seen} == {"GET"}
    assert all(r.headers["authorization"] == "Bearer tok" for r in seen)
    assert all(r.url.path.startswith("/api/v1/") for r in seen)
    for r in seen:
        assert any(p.match(r.url.path[len("/api/v1"):]) for p in peer._ALLOWED), r.url.path


def test_a_path_outside_the_whitelist_raises_before_any_io(configured, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("I/O happened")

    monkeypatch.setattr(peer, "_transport", httpx.MockTransport(handler))
    with pytest.raises(peer.PeerError, match="not allowed"):
        peer._get("/live/1/flatten")
    with pytest.raises(peer.PeerError, match="not allowed"):
        peer._get("/live/1/manual-order")


def test_errors_are_named_not_swallowed(configured, monkeypatch):
    monkeypatch.setattr(peer, "_transport",
                        httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(peer.PeerError, match="token rejected"):
        peer.deployments()
    monkeypatch.setattr(peer, "_transport",
                        httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(peer.PeerError, match="update the peer"):
        peer.run_cycles(31)

    def boom(r):
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(peer, "_transport", httpx.MockTransport(boom))
    with pytest.raises(peer.PeerError, match="unreachable"):
        peer.trades(31)


def test_unconfigured_is_an_error_not_a_call(monkeypatch):
    from skas_algo.config import get_settings

    monkeypatch.setattr(get_settings(), "peer_api_url", None)
    assert not peer.configured()
    with pytest.raises(peer.PeerError, match="no peer configured"):
        peer.deployments()


def test_the_module_source_has_no_other_http_verb():
    """GET-only by construction: a later edit that adds a POST must trip this."""
    src = inspect.getsource(peer)
    for verb in (".post(", ".put(", ".delete(", ".patch(", ".request(", ".stream("):
        assert verb not in src, f"peer.py uses {verb}"
    assert src.count("c.get(") == 1
