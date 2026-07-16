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


class InAppNotifier:
    def send(self, alert: Alert) -> None:
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
