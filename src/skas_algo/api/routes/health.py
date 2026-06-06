"""Health and readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from skas_algo import __version__
from skas_algo.config import get_settings
from skas_algo.db import get_engine

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    """Liveness + DB connectivity check."""
    settings = get_settings()
    db_ok = True
    db_error: str | None = None
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - exercised when DB is down
        db_ok = False
        db_error = str(exc)

    return {
        "status": "ok" if db_ok else "degraded",
        "version": __version__,
        "environment": settings.environment,
        "database": {"ok": db_ok, "error": db_error},
    }
