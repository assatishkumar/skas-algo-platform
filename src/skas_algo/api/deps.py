"""FastAPI dependencies."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Header, HTTPException
from sqlalchemy.orm import Session

from skas_algo.config import get_settings
from skas_algo.db.base import get_session
from skas_algo.security import AuthError, decode_token


def get_db() -> Iterator[Session]:
    """Yield a request-scoped DB session, committing on success."""
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def require_auth(authorization: str | None = Header(default=None)) -> None:
    """Gate a route behind a valid JWT bearer token.

    FAIL-OPEN: a no-op when auth isn't configured (``auth_enabled`` false) — so localhost dev
    and the existing test suite are unchanged. When configured, requires
    ``Authorization: Bearer <jwt>`` and a valid, unexpired token, else 401. Applied per-router
    in app.py to every router except ``health`` and ``auth`` (the WebSocket is gated separately
    — a browser can't set this header on a WS; see api/routes/live.py)."""
    if not get_settings().auth_enabled:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing or malformed bearer token")
    try:
        decode_token(authorization[len("Bearer "):].strip())
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=f"invalid token: {exc}") from exc
