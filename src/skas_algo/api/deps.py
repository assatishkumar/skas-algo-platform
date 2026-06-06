"""FastAPI dependencies."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from skas_algo.db.base import get_session


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
