"""In-app alerts feed (the mobile app's Alerts screen + bell badge).

Rows are written by ``notify/in_app.InAppNotifier`` from the platform's existing alert
emitters. ``delivered_at`` is the read-at marker: NULL = unread."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from skas_algo.api.deps import get_db
from skas_algo.api.models import iso_utc
from skas_algo.db.enums import AlertChannel
from skas_algo.db.models import Alert

router = APIRouter(tags=["alerts"], prefix="/alerts")


@router.get("")
def list_alerts(limit: int = 100, db: Session = Depends(get_db)) -> dict:
    limit = max(1, min(int(limit), 500))
    rows = db.execute(
        select(Alert).where(Alert.channel == AlertChannel.IN_APP)
        .order_by(Alert.id.desc()).limit(limit)
    ).scalars().all()
    unread = db.execute(
        select(func.count()).select_from(Alert)
        .where(Alert.channel == AlertChannel.IN_APP, Alert.delivered_at.is_(None))
    ).scalar_one()
    return {
        "unread": int(unread),
        "alerts": [{
            "id": r.id,
            "ts": iso_utc(r.created_at),
            "title": (r.payload or {}).get("title", ""),
            "message": (r.payload or {}).get("message", ""),
            "level": (r.payload or {}).get("level", r.type),
            "read": r.delivered_at is not None,
        } for r in rows],
    }


@router.post("/mark-read")
def mark_all_read(db: Session = Depends(get_db)) -> dict:
    res = db.execute(
        update(Alert)
        .where(Alert.channel == AlertChannel.IN_APP, Alert.delivered_at.is_(None))
        .values(delivered_at=datetime.now(UTC))
    )
    db.commit()
    return {"marked": int(res.rowcount or 0)}
