"""In-process console sessions, keyed by id.

Module-level and lock-guarded, the shape every other shared-state holder here uses
(``dhan._QUOTES``, ``live_broker.governor_for``, ``live.manager.manager``). Deliberately NOT
``services/replay_jobs``: that is single-flight for batch work, and a console cursor is
long-lived, interactive, and there can be a few at once.

Capped and swept, because a day's tape is ~150k prints held as parallel lists (~20 MB) and
the VPS is a 911 MB swapless box already running the live loop. LRU, with anything idle
past the TTL dropped on touch. The cap was THREE until 2026-09-09: a second browser on the
same backend (a Chrome test tab beside the owner's) evicted the owner's session mid-trade,
and every click after that 404'd with the book gone — "the lots stepper does nothing".
Eight sessions and a three-hour idle window; and a lost session is now RESTORABLE from the
journal the page holds (`ConsoleOpen.restore`), so eviction costs a reopen, not the book.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

from .session import ConsoleSession
from .store import console_dir

logger = logging.getLogger("skas_algo.console")

MAX_SESSIONS = 8
IDLE_TTL = timedelta(hours=3)
# A session is WRITTEN THROUGH to disk (`<console_dir>/sessions/<id>.json`, the same payload
# the save button writes) whenever `state()` sees the book change, and at most once every
# CURSOR_WRITE_S when only the cursor moved — so a restart, an eviction or the idle sweep
# costs a reload from the tape, not the book (owner, 2026-09-15). `get()` rehydrates a
# missing id from that file under the SAME id, so the page's URL survives. Files older
# than KEEP_DAYS are pruned when a session is created; `drop` (an explicit close) deletes.
CURSOR_WRITE_S = 2.0
KEEP_DAYS = 7
_ID = re.compile(r"^[0-9a-f]{12}$")

_LOCK = threading.Lock()
_SESSIONS: dict[str, ConsoleSession] = {}
_TOUCHED: dict[str, datetime] = {}
_WRITTEN: dict[str, float] = {}
# set by the route layer: wires the out-of-package hooks (Kite margin, VIX, IV rank) onto a
# session the registry builds itself on rehydrate
hooks: Callable[[ConsoleSession], None] | None = None


def _dir() -> Path:
    return console_dir() / "sessions"


def _path(session_id: str) -> Path:
    return _dir() / f"{session_id}.json"


def persist(session: ConsoleSession, book_changed: bool = True) -> bool:
    """Write the session's payload. A cursor-only change is throttled."""
    now = time.monotonic()
    if not book_changed and now - _WRITTEN.get(session.id, 0.0) < CURSOR_WRITE_S:
        return False
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    body = {**session.save_payload(), "id": session.id,
            "written_at": datetime.now().isoformat(timespec="seconds")}
    tmp = _path(session.id).with_suffix(".tmp")
    tmp.write_text(json.dumps(body))
    tmp.replace(_path(session.id))          # atomic on POSIX; not a Drive folder
    _WRITTEN[session.id] = now
    return True


def _attach(session: ConsoleSession) -> None:
    session.on_change = persist


def _rehydrate(session_id: str) -> ConsoleSession:
    """Rebuild a session from its file under the same id — ``KeyError`` when there is none
    (the route turns that into the 404 the page knows)."""
    if not _ID.match(session_id):
        raise KeyError(session_id)
    p = _path(session_id)
    if not p.exists():
        raise KeyError(session_id)
    j = json.loads(p.read_text())
    session = ConsoleSession(underlying=j["underlying"], day=date.fromisoformat(j["day"]),
                             at=j.get("clock"), expiry=j.get("expiry"),
                             capital=float(j.get("capital", 500_000)),
                             strike_window=int(j.get("strike_window", 20)),
                             allow_fifty_strikes=bool(j.get("allow_fifty_strikes", False)),
                             margin_per_lot_set=float(j.get("margin_per_lot_set", 0.0)))
    session.id = session_id
    if hooks is not None:
        hooks(session)
    session.restore(j.get("journal", []), j.get("alerts", []), j.get("bookmarks", []),
                    j.get("discarded", []))
    session.prime_change_key()
    _attach(session)
    logger.info("console session %s rehydrated from disk (%d fills)", session_id,
                len(session.journal))
    return session


def prune(days: int = KEEP_DAYS) -> int:
    d = _dir()
    if not d.exists():
        return 0
    cutoff = time.time() - days * 86400
    n = 0
    for p in d.glob("*.json"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                n += 1
        except OSError:
            continue
    return n


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
    _attach(session)
    with _LOCK:
        _SESSIONS[session.id] = session
        _TOUCHED[session.id] = datetime.now()
        _sweep_locked()
    try:
        prune()
    except OSError:  # pragma: no cover
        logger.exception("console: prune failed")
    return session


def get(session_id: str) -> ConsoleSession:
    """The session — from memory, else rebuilt from its file under the same id — or
    ``KeyError`` (the route turns that into a 404 so a stale browser tab gets told to
    open a new session rather than a 500)."""
    with _LOCK:
        _sweep_locked()
        session = _SESSIONS.get(session_id)
        if session is not None:
            _TOUCHED[session_id] = datetime.now()
            return session
    session = _rehydrate(session_id)            # the tape loads outside the lock
    with _LOCK:
        _SESSIONS[session_id] = session
        _TOUCHED[session_id] = datetime.now()
        _sweep_locked()
    return session


def drop(session_id: str) -> bool:
    """An explicit close: gone from memory AND from disk."""
    with _LOCK:
        _TOUCHED.pop(session_id, None)
        _WRITTEN.pop(session_id, None)
        had = _SESSIONS.pop(session_id, None) is not None
    p = _path(session_id) if _ID.match(session_id) else None
    if p is not None and p.exists():
        p.unlink()
        had = True
    return had


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
        _WRITTEN.clear()
