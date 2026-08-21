"""In-app alert sink — persists alerts for the mobile app's Alerts screen.

Every alert the platform already emits (order errors, book-mismatch halts, watchdog
restarts, stale pivots, backup failures, …) flows through ``build_notifier()``; this sink
tees each one into the previously-dead ``alert`` table (channel IN_APP; ``delivered_at``
doubles as the read-at marker — NULL = unread) and broadcasts a WS ``{"type": "alert"}``
event so the app's bell badge updates live. Best-effort by design: persistence problems are
logged, never raised (FanOutNotifier isolates channels anyway), and the table is pruned to
the newest ``KEEP`` rows on each write (alerts are rare — the prune is cheap).
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, select

from .base import Alert

logger = logging.getLogger(__name__)

KEEP = 500  # newest rows retained
THREAD_NAME = "in-app-alert"  # see wait_for_delivery()


def wait_for_delivery(timeout: float = 5.0) -> None:
    """Block until every in-flight alert write has finished. TEST HELPER — production
    never waits, that is the whole point of the daemon thread."""
    import threading
    import time

    deadline = time.monotonic() + timeout
    for th in [t for t in threading.enumerate() if t.name == THREAD_NAME]:
        th.join(max(0.0, deadline - time.monotonic()))


class InAppNotifier:
    def send(self, alert: Alert) -> None:
        """Fire-and-forget, on a daemon thread — the same treatment TelegramNotifier gets and
        for the same reason. This sink opens its OWN session, so a caller that notifies while
        holding an uncommitted write (services/broker.set_armed did) blocked here for SQLite's
        full 15s busy_timeout waiting on its own lock, then lost the alert. Off-thread, the
        write simply queues behind the caller's commit and lands a moment later."""
        import threading

        # Named so tests can join the writers deterministically instead of sleeping.
        threading.Thread(target=self._deliver, args=(alert,), daemon=True,
                         name=THREAD_NAME).start()

    def _deliver(self, alert: Alert) -> None:
        try:
            from skas_algo.db.base import session_scope
            from skas_algo.db.enums import AlertChannel
            from skas_algo.db.models import Alert as AlertRow

            with session_scope() as db:
                db.add(AlertRow(
                    type=alert.level.value,
                    channel=AlertChannel.IN_APP,
                    payload={"title": alert.title, "message": alert.message,
                             "level": alert.level.value},
                ))
                db.flush()
                stale = db.execute(
                    select(AlertRow.id).order_by(AlertRow.id.desc()).offset(KEEP)
                ).scalars().all()
                if stale:
                    db.execute(delete(AlertRow).where(AlertRow.id.in_(stale)))
        except Exception:  # pragma: no cover - persistence must never block an alert
            logger.exception("in-app alert persist failed")
            return
        try:  # live badge push — lazy import (manager imports notify at call sites)
            from skas_algo.live.manager import manager

            manager.broadcaster.publish({"type": "alert", "title": alert.title,
                                         "level": alert.level.value})
        except Exception:  # pragma: no cover - badge push is best-effort
            pass
