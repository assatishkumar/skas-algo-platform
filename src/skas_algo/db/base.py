"""SQLAlchemy engine, session factory, and declarative Base."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from skas_algo.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Return a lazily-created singleton engine."""
    global _engine
    if _engine is None:
        url = get_settings().database_url
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        # Pool sized for the threaded live world: ~22 run-tick threads + API requests
        # can hold short sessions concurrently; the default 5+10 drained under busy-wait
        # pileups and starved read-only routes (2026-07-07).
        _engine = create_engine(url, future=True, connect_args=connect_args,
                                pool_size=15, max_overflow=25, pool_timeout=10)
        if url.startswith("sqlite"):
            # The live app writes from many threads (loop ticks, API requests, greeks
            # sampling). Without WAL + a busy timeout, SQLite writers FAIL instantly
            # ("database is locked") whenever transactions overlap — the 2026-07-07
            # lock-storm incident. WAL lets readers and the writer coexist; the busy
            # timeout makes contending writers queue instead of raising.
            from sqlalchemy import event

            @event.listens_for(_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - env wiring
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=15000")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.close()
    return _engine


def _get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), class_=Session, expire_on_commit=False)
    return _SessionFactory


def get_session() -> Session:
    """Return a new Session. Caller is responsible for closing it."""
    return _get_session_factory()()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a transactional scope around a series of operations."""
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
