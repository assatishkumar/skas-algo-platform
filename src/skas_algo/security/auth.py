"""App authentication helpers: operator-password hashing (bcrypt) + JWT bearer tokens.

Separate from crypto.py (which is Fernet-at-rest for broker secrets) — this is the login
concern. A single operator password is bcrypt-hashed into an env var; a successful login
mints a short-lived HS256 JWT that every request/WebSocket presents. See config.Settings
(auth_password_hash / auth_jwt_secret / auth_token_ttl_hours) and api/deps.require_auth.
"""

from __future__ import annotations

import time

import bcrypt
import jwt

from skas_algo.config import get_settings

_ALG = "HS256"


class AuthError(Exception):
    """A token is missing, malformed, expired, or wrongly signed."""


def hash_password(password: str) -> str:
    """Return a bcrypt hash (str) for a plaintext password — stored in SKAS_AUTH_PASSWORD_HASH."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time check of a plaintext password against a bcrypt hash. Never raises."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):  # malformed hash
        return False


def create_token(*, secret: str | None = None, ttl_hours: int | None = None) -> str:
    """Mint a signed JWT for the (single) operator. ``sub`` is fixed — there is one user."""
    settings = get_settings()
    secret = secret or settings.auth_jwt_secret
    if not secret:
        raise AuthError("auth is not configured (no JWT secret)")
    ttl = settings.auth_token_ttl_hours if ttl_hours is None else ttl_hours
    now = int(time.time())
    return jwt.encode(
        {"sub": "operator", "iat": now, "exp": now + int(ttl) * 3600},
        secret, algorithm=_ALG,
    )


def decode_token(token: str, *, secret: str | None = None) -> dict:
    """Verify signature + expiry and return the claims. Raises AuthError on any problem."""
    secret = secret or get_settings().auth_jwt_secret
    if not secret:
        raise AuthError("auth is not configured (no JWT secret)")
    try:
        return jwt.decode(token, secret, algorithms=[_ALG])
    except jwt.PyJWTError as exc:
        raise AuthError(str(exc)) from exc
