"""Live run manager: owns running paper/live sessions and drives them.

Each LiveRun wraps a LiveSession + a QuoteSource + DB persistence + a broadcast bus.
The sync methods (refresh / run_decision / end_day / stop) are the tested, reliable
path and are also exposed via REST for manual control. An optional async loop drives
periodic quote refresh and a once-daily decision near the close.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, time

from skas_algo.db.base import session_scope
from skas_algo.db.models import AlgoRun
from skas_algo.engine.jsonutil import to_native
from skas_algo.engine.live import LiveSession
from skas_algo.engine.market import PriceLoader
from skas_algo.engine.overrides import OverrideRule
from skas_algo.engine.report import build_report
from skas_algo.engine.runner import RunResult
from skas_algo.strategies.registry import get_strategy

from .persistence import (
    finalize_live_run,
    persist_state,
    record_trades,
    start_live_run,
    sync_positions,
)
from .quotes import IST, QuoteSource, is_market_open, warmup_history

logger = logging.getLogger("skas_algo.live")


@dataclass
class LiveConfig:
    name: str
    strategy_id: str
    symbols: list[str]
    capital: float = 2_500_000
    params: dict = field(default_factory=dict)
    tax_rate: float = 0.20
    withdrawal_rate: float = 0.0
    lookback: int = 20
    overrides: list[OverrideRule] = field(default_factory=list)
    mode: str = "PAPER"
    quote_source: str = "cache"  # persisted so the run can be rebuilt after a restart
    broker_account_id: int | None = None
    refresh_seconds: int = 30
    decision_time: str = "15:20"  # IST; daily decision fires at/after this
    ignore_market_hours: bool = False
    auto: bool = False  # whether the background refresh/decision loop runs


def _serialize_event(ev: dict) -> dict:
    out = to_native(dict(ev))
    dt = ev["date"]
    out["date"] = dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)
    return out


class Broadcaster:
    """Tiny pub/sub over asyncio queues for WebSocket fan-out (single-user)."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, message: dict) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(to_native(message))
            except asyncio.QueueFull:  # pragma: no cover - slow consumer
                pass


class LiveRun:
    def __init__(self, run_id, algo_id, config, session, quote_source, broadcaster):
        self.run_id = run_id
        self.algo_id = algo_id
        self.config: LiveConfig = config
        self.session: LiveSession = session
        self.quote_source: QuoteSource = quote_source
        self.broadcaster: Broadcaster = broadcaster
        self.last_decision_day = None
        self.status = "running"

    # ----------------------------------------------------------- actions
    def refresh(self) -> dict:
        """Pull quotes, mark-to-market, persist positions, broadcast snapshot."""
        quotes = self.quote_source.get_quotes(self.config.symbols)
        self.session.update_quotes(quotes)
        snap = self.session.snapshot()
        with session_scope() as db:
            sync_positions(db, self.algo_id, snap)
        self.broadcaster.publish({"type": "snapshot", "run_id": self.run_id, **snap})
        self._persist_state()
        return snap

    def run_decision(self, ts: datetime | None = None) -> list[dict]:
        """Make today's entry/exit decision; persist trades + positions; broadcast."""
        ts = ts or datetime.now(IST)
        events = self.session.run_decision(ts)
        with session_scope() as db:
            if events:
                record_trades(db, self.algo_id, events)
            sync_positions(db, self.algo_id, self.session.snapshot())
        if events:
            self.broadcaster.publish(
                {
                    "type": "trades",
                    "run_id": self.run_id,
                    "events": [_serialize_event(e) for e in events],
                }
            )
        self.broadcaster.publish(
            {"type": "snapshot", "run_id": self.run_id, **self.session.snapshot()}
        )
        self._persist_state()
        return events

    def end_day(self) -> None:
        self.session.end_day()
        self._persist_state()

    def stop(self) -> None:
        self.status = "stopped"
        rr = RunResult(
            history=self.session.history,
            transactions=self.session.transactions,
            monthly_flush_log=self.session.monthly_flush_log,
            portfolio=self.session.portfolio,
        )
        report = build_report(rr, self.config.capital)
        with session_scope() as db:
            run = db.get(AlgoRun, self.run_id)
            if run is not None:
                finalize_live_run(
                    db,
                    run,
                    metrics=report,
                    trade_log=[_serialize_event(t) for t in self.session.transactions],
                )
        self.broadcaster.publish({"type": "stopped", "run_id": self.run_id})

    def snapshot(self) -> dict:
        return to_native(
            {
                "run_id": self.run_id,
                "status": self.status,
                "name": self.config.name,
                "strategy_id": self.config.strategy_id,
                "quote_source": self.config.quote_source,
                "parts_total": self.config.params.get("capital_parts"),
                **self.session.snapshot(),
            }
        )

    def export_state(self) -> dict:
        return {
            **self.session.export_state(),
            "last_decision_day": (
                self.last_decision_day.isoformat() if self.last_decision_day else None
            ),
        }

    def _persist_state(self) -> None:
        try:
            with session_scope() as db:
                persist_state(db, self.run_id, to_native(self.export_state()))
        except Exception:  # pragma: no cover - persistence must never break the loop
            logger.exception("failed to persist state for run %s", self.run_id)


class LiveRunManager:
    def __init__(self) -> None:
        self.runs: dict[int, LiveRun] = {}
        self.broadcaster = Broadcaster()
        self._tasks: dict[int, asyncio.Task] = {}

    def start(self, config: LiveConfig, loader: PriceLoader, quote_source: QuoteSource) -> LiveRun:
        factory = get_strategy(config.strategy_id)
        strategy = factory(universe=config.symbols, initial_capital=config.capital, **config.params)
        session = LiveSession(
            strategy,
            initial_capital=config.capital,
            lookback=config.lookback,
            tax_rate=config.tax_rate,
            withdrawal_rate=config.withdrawal_rate,
            overrides=config.overrides,
        )
        session.warmup(warmup_history(loader, config.symbols, config.lookback))

        params_snapshot = {
            "symbols": config.symbols,
            "lookback": config.lookback,
            "tax_rate": config.tax_rate,
            "withdrawal_rate": config.withdrawal_rate,
            "quote_source": config.quote_source,
            "broker_account_id": config.broker_account_id,
            "auto": config.auto,
            "refresh_seconds": config.refresh_seconds,
            "decision_time": config.decision_time,
            "ignore_market_hours": config.ignore_market_hours,
            **config.params,
        }
        with session_scope() as db:
            run = start_live_run(
                db,
                name=config.name,
                strategy_id=config.strategy_id,
                capital=config.capital,
                mode=config.mode,
                params=params_snapshot,
            )
            run_id, algo_id = run.id, run.algo_id

        live = LiveRun(run_id, algo_id, config, session, quote_source, self.broadcaster)
        self.runs[run_id] = live
        live._persist_state()  # initial snapshot so a restart can recover it immediately
        return live

    def register(self, live: LiveRun) -> None:
        """Register a run rebuilt by recovery (already has its DB row + state)."""
        self.runs[live.run_id] = live

    def get(self, run_id: int) -> LiveRun | None:
        return self.runs.get(run_id)

    def list(self) -> list[LiveRun]:
        return list(self.runs.values())

    def stop(self, run_id: int) -> LiveRun | None:
        task = self._tasks.pop(run_id, None)
        if task is not None:
            task.cancel()
        live = self.runs.pop(run_id, None)
        if live is not None:
            live.stop()
        return live

    # ----------------------------------------------------- async driver
    def start_loop(self, run_id: int) -> None:
        """Kick off the background refresh/decision loop (call from an event loop)."""
        live = self.runs[run_id]
        self._tasks[run_id] = asyncio.create_task(self._loop(live))

    async def _loop(self, live: LiveRun) -> None:
        try:
            decide_at = time.fromisoformat(live.config.decision_time)
            while True:
                if live.config.ignore_market_hours or is_market_open():
                    try:
                        live.refresh()
                        now = datetime.now(IST)
                        if now.time() >= decide_at and live.last_decision_day != now.date():
                            live.run_decision(now)
                            live.end_day()
                            live.last_decision_day = now.date()
                    except Exception:  # pragma: no cover - keep the loop alive
                        logger.exception("live loop tick failed for run %s", live.run_id)
                await asyncio.sleep(live.config.refresh_seconds)
        except asyncio.CancelledError:  # pragma: no cover
            pass


# Process-wide singleton.
manager = LiveRunManager()
