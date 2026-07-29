"""Single-flight background jobs (named slots) for heavy compute.

A 2-year replay is ~5 minutes of CPU; as one blocking HTTP request it had no progress and
the preview died with the page (owner, 2026-07-17). This gives routes the same shape
the option-capture flow uses (manager.option_capture_running/_progress): ONE job at a
time PER SLOT (single-user box), a module-level snapshot the progress endpoint reads,
and the finished result retained until the NEXT job in that slot starts — so navigating
away and back simply re-attaches.

Slots (2026-07-28): the original single global slot serialized the intraday backtest AND
the loss study — correct, they're both replay-weights. The Analyze page's per-run
analytics bundle is a different workload and must not block (or be blocked by) a replay,
so ``start``/``snapshot`` take a ``slot`` name; the default ``"replay"`` slot keeps every
existing caller byte-identical.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_slots: dict[str, dict | None] = {}


def start(work, *, slot: str = "replay", busy_msg: str | None = None) -> str:
    """Run ``work(progress_cb)`` on a daemon thread; ``progress_cb(done, total, day)``.

    Returns the job id immediately. Raises RuntimeError when this slot's job is already
    running (the route maps it to 409). ``work``'s return value lands in the snapshot's
    ``result`` on success; a ValueError becomes a clean ``error`` string (the replay's
    own validation vocabulary), anything else a generic one (logged with traceback).
    """
    with _lock:
        cur = _slots.get(slot)
        if cur is not None and cur["status"] == "running":
            raise RuntimeError(busy_msg or "an intraday backtest is already running — "
                               "wait for it to finish (one at a time)")
        job_id = uuid.uuid4().hex[:12]
        _slots[slot] = {"id": job_id, "status": "running", "done": 0, "total": 0,
                        "day": None, "result": None, "error": None,
                        "started_at": datetime.now().isoformat(timespec="seconds")}

    def _progress(done: int, total: int, day: str) -> None:
        with _lock:
            cur = _slots.get(slot)
            if cur is not None and cur["id"] == job_id:
                cur.update(done=done, total=total, day=day)

    def _run() -> None:
        try:
            result = work(_progress)
            with _lock:
                cur = _slots.get(slot)
                if cur is not None and cur["id"] == job_id:
                    cur.update(status="done", result=result,
                               done=cur["total"] or cur["done"])
        except ValueError as exc:
            with _lock:
                cur = _slots.get(slot)
                if cur is not None and cur["id"] == job_id:
                    cur.update(status="error", error=str(exc))
        except Exception as exc:  # pragma: no cover - surfaced, never silently lost
            logger.exception("%s job %s failed", slot, job_id)
            with _lock:
                cur = _slots.get(slot)
                if cur is not None and cur["id"] == job_id:
                    cur.update(status="error", error=f"{slot} failed: {exc}")

    threading.Thread(target=_run, daemon=True, name=f"{slot}-job").start()
    return job_id


def snapshot(slot: str = "replay") -> dict:
    """The progress endpoint's payload: {"status": "idle"} when nothing ever ran."""
    with _lock:
        cur = _slots.get(slot)
        if cur is None:
            return {"status": "idle"}
        return dict(cur)
