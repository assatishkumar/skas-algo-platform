"""App authentication: fail-open when unconfigured, enforced when both secrets are set.

Auth is turned on by patching the cached Settings singleton's two auth attrs (auto-reverted
by monkeypatch), so the rest of the suite stays fail-open with zero edits.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from skas_algo.api.app import create_app
from skas_algo.config import get_settings
from skas_algo.security import create_token, decode_token, hash_password, verify_password

PASSWORD = "s3cret-operator-pw"
JWT_SECRET = "test-jwt-secret-0123456789-abcdefghijklmnop"


@pytest.fixture
def authed(monkeypatch):
    """A TestClient whose app has auth ENABLED (both secrets set on the settings singleton)."""
    s = get_settings()
    monkeypatch.setattr(s, "auth_password_hash", hash_password(PASSWORD))
    monkeypatch.setattr(s, "auth_jwt_secret", JWT_SECRET)
    return TestClient(create_app())


# --- helper unit tests ---------------------------------------------------------------

def test_password_hash_roundtrip():
    h = hash_password(PASSWORD)
    assert verify_password(PASSWORD, h)
    assert not verify_password("wrong", h)
    assert not verify_password(PASSWORD, "not-a-bcrypt-hash")  # malformed → False, no raise


def test_token_roundtrip_and_tamper():
    from skas_algo.security import AuthError

    tok = create_token(secret=JWT_SECRET, ttl_hours=1)
    assert decode_token(tok, secret=JWT_SECRET)["sub"] == "operator"
    with pytest.raises(AuthError):
        decode_token(tok, secret="a-different-secret-of-sufficient-length-xxxx")
    with pytest.raises(AuthError):
        decode_token("garbage.token.value", secret=JWT_SECRET)


def test_expired_token_rejected():
    from skas_algo.security import AuthError

    expired = create_token(secret=JWT_SECRET, ttl_hours=-1)  # already past exp
    with pytest.raises(AuthError):
        decode_token(expired, secret=JWT_SECRET)


# --- fail-open (default: no secrets) -------------------------------------------------

def test_fail_open_when_unconfigured(client):
    assert not get_settings().auth_enabled
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/brokers").status_code == 200          # open, no token needed
    assert client.post("/api/v1/auth/login", json={"password": "x"}).status_code == 400


# --- enforced (both secrets set) -----------------------------------------------------

def test_health_open_but_protected_routes_401(authed):
    assert authed.get("/api/v1/health").status_code == 200           # always open
    assert authed.get("/api/v1/brokers").status_code == 401          # needs a token now
    assert authed.get("/api/v1/brokers",
                      headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_login_then_authed_request(authed):
    assert authed.post("/api/v1/auth/login", json={"password": "wrong"}).status_code == 401
    r = authed.post("/api/v1/auth/login", json={"password": PASSWORD})
    assert r.status_code == 200
    token = r.json()["access_token"]
    assert r.json()["token_type"] == "bearer"
    ok = authed.get("/api/v1/brokers", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200


def test_websocket_requires_token(authed):
    # No token → server closes before accept (policy violation).
    with pytest.raises(WebSocketDisconnect):
        with authed.websocket_connect("/api/v1/live/ws"):
            pass
    # Valid token → connection is accepted.
    token = create_token(secret=JWT_SECRET, ttl_hours=1)
    with authed.websocket_connect(f"/api/v1/live/ws?token={token}") as ws:
        assert ws is not None
