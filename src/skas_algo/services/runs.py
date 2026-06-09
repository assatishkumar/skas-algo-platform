"""Shared run/algo lifecycle helpers used by both the live and backtest routes."""

from __future__ import annotations

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.orm import Session

from skas_algo.db.models import Algo, AlgoRun, Fill, Order, Position


def delete_algo_cascade(db: Session, algo_id: int) -> None:
    """Permanently remove an Algo and everything hanging off it.

    Deletes the algo's Fills -> Orders -> Positions -> AlgoRuns -> the Algo itself.
    Backtests have no orders/fills/positions, so those deletes are no-ops there;
    paper/live deployments do.
    """
    order_ids = db.execute(select(Order.id).where(Order.algo_id == algo_id)).scalars().all()
    if order_ids:
        db.execute(sa_delete(Fill).where(Fill.order_id.in_(order_ids)))
    db.execute(sa_delete(Order).where(Order.algo_id == algo_id))
    db.execute(sa_delete(Position).where(Position.algo_id == algo_id))
    db.execute(sa_delete(AlgoRun).where(AlgoRun.algo_id == algo_id))
    db.execute(sa_delete(Algo).where(Algo.id == algo_id))
