"""In-process console sessions, keyed by id.

Module-level and lock-guarded, the shape every other shared-state holder here uses
(``dhan._QUOTES``, ``live_broker.governor_for``, ``live.manager.manager``). Deliberately NOT
``services/replay_jobs``: that is single-flight for batch work, and a console cursor is
long-lived, interactive, and there can be a few at once.

Capped and swept, because a day's tape is ~150k prints held as parallel lists (~20 MB) and
the VPS is a 911 MB swapless box already running the live loop. Three sessions, LRU, with
anything idle past the TTL dropped on touch.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from .session import ConsoleSession

logger = logging.getLogger("skas_algo.console")

MAX_SESSIONS = 3
IDLE_TTL = timedelta(minutes=30)

_LOCK = threading.Lock()
_SESSIONS: dict[str, ConsoleSession] = {}
_TOUCHED: dict[str, datetime] = {}


def _sweep_locked() -> None:
    now = datetime.now()
    for sid in [s for s, t in _TOUCHED.items() if now - t > IDLE_TTL]:
        _SESSIONS.pop(sid, None)
        _TOUCHED.pop(sid, None)
        logger.info("console session %s dropped (idle)", sid)
    while len(_SESSIONS) > MAX_SESSIONS:
        oldest = min(_TOUCHED, key=lambda s: _TOUCHED[s])
        _SESSIONS.pop(oldest, None)
        _TOUCHED.pop(oldest, None)
        logger.info("console session %s evicted (cap %s)", oldest, MAX_SESSIONS)


def create(**kw) -> ConsoleSession:
    session = ConsoleSession(**kw)
    with _LOCK:
        _SESSIONS[session.id] = session
        _TOUCHED[session.id] = datetime.now()
        _sweep_locked()
    return session


def get(session_id: str) -> ConsoleSession:
    """The session, or ``KeyError`` — the route turns that into a 404 so a stale browser
    tab gets told to open a new session rather than a 500."""
    with _LOCK:
        _sweep_locked()
        session = _SESSIONS[session_id]
        _TOUCHED[session_id] = datetime.now()
        return session


def drop(session_id: str) -> bool:
    with _LOCK:
        _TOUCHED.pop(session_id, None)
        return _SESSIONS.pop(session_id, None) is not None


def briefs() -> list[dict]:
    with _LOCK:
        return [{"id": s.id, "underlying": s.underlying, "date": s.day.isoformat(),
                 "clock": s.clock.strftime("%H:%M"),
                 "touched": _TOUCHED.get(sid, s.created_at).isoformat(timespec="seconds")}
                for sid, s in _SESSIONS.items()]


def clear() -> None:
    """Tests only."""
    with _LOCK:
        _SESSIONS.clear()
        _TOUCHED.clear()
