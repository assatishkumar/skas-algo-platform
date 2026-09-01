"""The Growth tab's history — recorded forward, never back-filled.

There is no stored series of what this portfolio was worth before tracking began, and none can
be reconstructed honestly: a holding's row carries today's units and today's value, so
"reconstructing" 2024 would mean pricing today's position at old prices and calling the result
history. It isn't — it is a chart of a portfolio that never existed.

So the Growth tab shows what was actually observed. ``record_snapshot`` writes one row a day
from the manager's maintenance pass; the chart starts on the first one and says how thin it is
until there is enough to read. This is the same stance the Analyze page takes with the 1-min
store — an amber "not enough data" beats a confident fabrication.

``by_holding`` is keyed by holding id, so CLASS and BUCKET series are summed against CURRENT
membership: reclassify a fund today and its whole history moves with it, which is what someone
comparing classes actually wants to see.
"""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from skas_algo.db.models import PortfolioHolding, PortfolioSnapshot, PortfolioTransaction
from skas_algo.services.portfolio import holding_view

logger = logging.getLogger(__name__)

# Below this the chart is a couple of dots — the UI says so rather than drawing a "trend".
MIN_POINTS_FOR_TREND = 8


def _txn_map(db: Session) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for t in db.execute(select(PortfolioTransaction)).scalars().all():
        out.setdefault(t.holding_id, []).append({
            "id": t.id, "on_date": t.on_date, "kind": t.kind,
            "units": t.units, "price": t.price, "fees": t.fees,
        })
    return out


def current_views(db: Session, *, today: date | None = None) -> list[dict]:
    """Every holding as a derived record — the one place the ORM rows meet the money math."""
    txns = _txn_map(db)
    rows = db.execute(
        select(PortfolioHolding).order_by(PortfolioHolding.sort_order, PortfolioHolding.id)
    ).scalars().all()
    return [
        holding_view(
            {
                "id": h.id, "name": h.name, "asset_class": h.asset_class,
                "kind_override": h.kind_override, "invested": h.invested, "units": h.units,
                "value": h.value, "last_price": h.last_price, "price_asof": h.price_asof,
                "native_currency": h.native_currency, "native_price": h.native_price,
                "native_invested": h.native_invested,
                "day_change": h.day_change, "xirr_pct": h.xirr_pct, "buy_month": h.buy_month,
                "sync": h.sync, "sync_source": h.sync_source, "sync_ref": h.sync_ref,
                "broker_account_id": h.broker_account_id,
                "last_synced_at": h.last_synced_at.isoformat() if h.last_synced_at else None,
                "units_locked": h.units_locked, "broker_units": h.broker_units,
                "excluded_from_buckets": h.excluded_from_buckets, "note": h.note,
                "dividend_yield_pct": h.dividend_yield_pct,
            },
            txns.get(h.id, []),
            today=today,
        )
        for h in rows
    ]


def record_snapshot(db: Session, *, on_date: date | None = None) -> PortfolioSnapshot | None:
    """Write (or update) today's snapshot. Returns None when there is nothing to record.

    Same-day re-runs OVERWRITE rather than append: the row means "what it was worth on this
    date", and the last read of the day is the closest thing to a close."""
    day = on_date or date.today()
    views = current_views(db, today=day)
    if not views:
        return None

    total = round(sum(v["value"] for v in views), 2)
    invested = round(sum(v["invested"] for v in views), 2)
    by_holding = {str(v["id"]): round(v["value"], 2) for v in views if v["id"] is not None}

    row = db.execute(
        select(PortfolioSnapshot).where(PortfolioSnapshot.on_date == day.isoformat())
    ).scalar_one_or_none()
    if row is None:
        row = PortfolioSnapshot(on_date=day.isoformat())
        db.add(row)
    row.value = total
    row.invested = invested
    row.by_holding = by_holding
    db.commit()
    return row


def growth_series(db: Session, *, limit_days: int = 1500) -> dict:
    """The recorded history, in the shape the chart consumes.

    ``dates`` is the shared x-axis; ``total``/``invested`` are the whole-portfolio lines; and
    ``by_holding`` gives one aligned array per holding, with **null** — not zero — for a date
    before that holding was tracked. The distinction matters: a zero would draw a line rising
    off the floor, implying the money was there and worthless."""
    rows = db.execute(
        select(PortfolioSnapshot).order_by(PortfolioSnapshot.on_date)
    ).scalars().all()
    rows = rows[-limit_days:]
    dates = [r.on_date for r in rows]

    ids: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for hid in (r.by_holding or {}):
            if hid not in seen:
                seen.add(hid)
                ids.append(hid)

    by_holding = {
        hid: [(r.by_holding or {}).get(hid) for r in rows]
        for hid in ids
    }
    return {
        "dates": dates,
        "total": [r.value for r in rows],
        "invested": [r.invested for r in rows],
        "by_holding": by_holding,
        "points": len(rows),
        "enough_for_trend": len(rows) >= MIN_POINTS_FOR_TREND,
        "since": dates[0] if dates else None,
    }
