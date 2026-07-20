"""Persist a live/paper run's state to the platform DB.

Creates the Algo + AlgoRun on start, writes an Order+Fill per executed trade, keeps
the Position table in sync with the session snapshot, and finalizes metrics on stop.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from skas_algo.db.enums import (
    InstrumentClass,
    OrderSide,
    OrderStatus,
    PositionStatus,
    TradingMode,
)
from skas_algo.db.models import Algo, AlgoRun, Fill, GreeksSnapshot, Order, Position


def start_live_run(
    session: Session, *, name, strategy_id, capital, mode, params, notes=None
) -> AlgoRun:
    ic = str((params or {}).get("instrument_class", "STOCK")).upper()
    algo = Algo(
        name=name,
        notes=notes,
        strategy_id=strategy_id,
        instrument_class=InstrumentClass.DERIV if ic == "DERIV" else InstrumentClass.STOCK,
        mode=TradingMode(mode),
        capital=capital,
        params=params,
    )
    session.add(algo)
    session.flush()
    run = AlgoRun(
        algo_id=algo.id,
        mode=TradingMode(mode),
        started_at=datetime.now(UTC),
        params_snapshot=params,
    )
    session.add(run)
    session.flush()
    return run


def record_trades(session: Session, algo_id: int, events: list[dict]) -> None:
    """One Order (FILLED) + Fill per executed trade event."""
    for ev in events:
        # The broker side of the FILLED order: a short-OPEN is a SELL (its action is "SHORT",
        # NOT "SELL") and a sell-to-close-a-long is "SELL"; a long-OPEN ("BUY") and a
        # short-CLOSE ("COVER") are BUY. Mapping only "SELL"→SELL stored every short as BUY,
        # so the durable Order trail couldn't be paired back into opens/closes (delta_neutral
        # run #203, 2026-07 — every leg looked open). SETTLE (expiry) is a non-traded close;
        # left BUY (the short-premium common case) — it never re-opens, so pairing is intact.
        side = OrderSide.SELL if ev["action"] in ("SELL", "SHORT") else OrderSide.BUY
        order = Order(
            algo_id=algo_id,
            client_order_id=uuid.uuid4().hex,
            symbol=ev["ticker"],
            side=side,
            quantity=ev["units"],
            price=ev["price"],
            status=OrderStatus.FILLED,
            tag=ev["tag"],
        )
        session.add(order)
        session.flush()
        session.add(
            Fill(
                order_id=order.id,
                symbol=ev["ticker"],
                side=side,
                quantity=ev["units"],
                price=ev["price"],
            )
        )


def sync_positions(session: Session, algo_id: int, snapshot: dict) -> None:
    """Upsert open positions from the snapshot; close any that are gone."""
    existing = {
        p.symbol: p
        for p in session.execute(
            select(Position).where(
                Position.algo_id == algo_id, Position.status == PositionStatus.OPEN
            )
        ).scalars()
    }
    seen: set[str] = set()
    for pos in snapshot.get("positions", []):
        seen.add(pos["symbol"])
        row = existing.get(pos["symbol"])
        if row is None:
            row = Position(
                algo_id=algo_id,
                symbol=pos["symbol"],
                status=PositionStatus.OPEN,
                opened_at=datetime.now(UTC),
            )
            session.add(row)
        row.quantity = pos["units"]
        row.lots = pos.get("lots", 0)
        row.avg_price = pos["avg_price"]
        row.unrealized_pnl = pos["unrealized_pnl"]
    for symbol, row in existing.items():
        if symbol not in seen:
            row.status = PositionStatus.CLOSED
            row.closed_at = datetime.now(UTC)
            row.quantity = 0


def persist_state(session: Session, run_id: int, state: dict) -> None:
    """Save the live session snapshot so the run can be rebuilt after a restart."""
    run = session.get(AlgoRun, run_id)
    if run is not None:
        run.state = state


def record_greeks(
    session: Session, run_id: int, snapshot: dict, ts: datetime, spot: float | None = None
) -> None:
    """Append a sampled greeks point (net + per-leg) for an options deployment."""
    legs = [
        {
            "symbol": p["symbol"],
            "iv": p.get("iv"),
            "delta": p.get("delta"),
            "pos_delta": p.get("pos_delta"),
            "units": p.get("units"),
            "dir": p.get("direction"),
        }
        for p in snapshot.get("positions", [])
        if p.get("iv") is not None
    ]
    pnl = sum(p.get("unrealized_pnl", 0.0) for p in snapshot.get("positions", []))
    session.add(
        GreeksSnapshot(
            algo_run_id=run_id,
            ts=ts,
            spot=spot,
            net_delta=snapshot.get("net_delta"),
            net_iv=snapshot.get("net_iv"),
            pnl=pnl,
            legs=legs,
        )
    )


def finalize_live_run(session: Session, run: AlgoRun, *, metrics: dict, trade_log: list) -> None:
    run.stopped_at = datetime.now(UTC)
    run.metrics = metrics
    run.trade_log = trade_log
    from skas_algo.db.models import Algo
    from skas_algo.services.vault_export import export_run_safe
    export_run_safe(run, session.get(Algo, run.algo_id))  # run-card → Obsidian vault (no-op if unset)
    # Keep run.state (the last session snapshot) so Activate can resume the deployment with its
    # realized P&L / trade history / strategy state intact. Recovery on boot still ignores it
    # (it only rebuilds runs with stopped_at IS NULL); only an explicit Activate restores it.
