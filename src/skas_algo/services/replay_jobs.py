"""Single-flight background job for the intraday replay.

A 2-year replay is ~5 minutes of CPU; as one blocking HTTP request it had no progress and
the preview died with the page (owner, 2026-07-17). This gives the route the same shape
the option-capture flow uses (manager.option_capture_running/_progress): ONE job at a
time (single-user box), a module-level snapshot the progress endpoint reads, and the
finished result retained until the NEXT job starts — so navigating away and back simply
re-attaches.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_current: dict | None = None


def start(work) -> str:
    """Run ``work(progress_cb)`` on a daemon thread; ``progress_cb(done, total, day)``.

    Returns the job id immediately. Raises RuntimeError when a job is already running
    (the route maps it to 409). ``work``'s return value lands in the snapshot's
    ``result`` on success; a ValueError becomes a clean ``error`` string (the replay's
    own validation vocabulary), anything else a generic one (logged with traceback).
    """
    global _current
    with _lock:
        if _current is not None and _current["status"] == "running":
            raise RuntimeError("an intraday backtest is already running — wait for it "
                               "to finish (one at a time)")
        job_id = uuid.uuid4().hex[:12]
        _current = {"id": job_id, "status": "running", "done": 0, "total": 0, "day": None,
                    "result": None, "error": None,
                    "started_at": datetime.now().isoformat(timespec="seconds")}

    def _progress(done: int, total: int, day: str) -> None:
        with _lock:
            if _current is not None and _current["id"] == job_id:
                _current.update(done=done, total=total, day=day)

    def _run() -> None:
        try:
            result = work(_progress)
            with _lock:
                if _current is not None and _current["id"] == job_id:
                    _current.update(status="done", result=result,
                                    done=_current["total"] or _current["done"])
        except ValueError as exc:
            with _lock:
                if _current is not None and _current["id"] == job_id:
                    _current.update(status="error", error=str(exc))
        except Exception as exc:  # pragma: no cover - surfaced, never silently lost
            logger.exception("intraday replay job %s failed", job_id)
            with _lock:
                if _current is not None and _current["id"] == job_id:
                    _current.update(status="error", error=f"replay failed: {exc}")

    threading.Thread(target=_run, daemon=True, name="replay-job").start()
    return job_id


def snapshot() -> dict:
    """The progress endpoint's payload: {"status": "idle"} when nothing ever ran."""
    with _lock:
        if _current is None:
            return {"status": "idle"}
        return dict(_current)
