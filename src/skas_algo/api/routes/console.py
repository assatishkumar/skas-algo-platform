"""Options Console — an interactive minute cursor over the 1-min option store.

REPLAY ONLY at this stage: these routes read the store and place NOTHING. No broker adapter
is constructed, no order path is reachable, and `tests/test_options_console.py` pins that.
When live mode lands it will drive an EXISTING deployment through the already-gated
`POST /live/{id}/manual-order`, never a second order path (CLAUDE.md §1).

Every mutating call returns the WHOLE ConsoleState. One shape, one render path — a dense
screen with a chain, a payoff and a risk rail has no business reconciling partial updates.
"""

from __future__ import annotations

import asyncio
from datetime import date

from fastapi import APIRouter, HTTPException

from skas_algo.api.models import (
    ConsoleAlert,
    ConsoleAnnotate,
    ConsoleBasket,
    ConsoleCommit,
    ConsoleForkOpen,
    ConsoleJump,
    ConsoleLoad,
    ConsoleMarginAnchor,
    ConsoleOpen,
    ConsoleOpenLive,
    ConsoleOps,
    ConsolePreset,
    ConsoleSave,
    ConsoleScale,
    ConsoleStage,
    ConsoleTransport,
    ConsoleUnstage,
)
from skas_algo.data.option_intraday_store import captured_days
from skas_algo.services import (
    atm_iv_history,
    console_fork,
    console_live,
    console_margin,
    console_market,
    peer,
)
from skas_algo.services.options_console import registry
from skas_algo.services.options_console import store as console_store
from skas_algo.services.options_console.session import UNDERLYINGS, ConsoleSession

router = APIRouter(prefix="/console", tags=["console"])


def _wire(session) -> None:
    """The out-of-package hooks a replay session needs (Kite margin, VIX, IV rank) — set
    on every session the routes open AND on one the registry rebuilds from disk."""
    session.margin_fn = console_margin.kite_equivalent
    session.vix_fn = console_market.vix_for_day
    session.iv_rank_fn = atm_iv_history.iv_rank
    console_margin.warm(session.underlying)


registry.hooks = _wire


def _get(session_id: str):
    """A replay session from the registry, or — for a ``live:<run_id>`` id — the console
    over that running deployment. Both answer the same DTO and the same verbs; a verb a
    live console has no meaning for (transport, undo, bookmarks…) answers 409."""
    try:
        if console_live.is_live_id(session_id):
            return console_live.get_console(session_id)
        return registry.get(session_id)
    except KeyError:
        raise HTTPException(status_code=404,
                            detail="console session not found — it expired or the backend "
                                   "restarted; open a new one") from None


def _replay_only(session, what: str) -> ConsoleSession:
    if not isinstance(session, ConsoleSession):
        raise HTTPException(status_code=409,
                            detail=f"{what} is a replay control — a live console runs on the "
                                   "market's own clock")
    return session


@router.get("/live-runs")
def live_runs() -> dict:
    """The DERIV deployments the console can drive (paper and live)."""
    return {"runs": console_live.runs(), "recovering": console_live.recovering()}


@router.post("/sessions/live")
async def open_live(body: ConsoleOpenLive) -> dict:
    """Open the console over a RUNNING deployment. Nothing is ordered by opening it."""
    try:
        c = await asyncio.to_thread(console_live.open_console, body.run_id, expiry=body.expiry)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    console_margin.warm(c.underlying)
    return await asyncio.to_thread(c.state)


@router.get("/days")
def console_days(underlying: str = "NIFTY") -> dict:
    """Days the store can replay. Drives the date picker, so it must be the truth about the
    store rather than a calendar — a holiday or a missed capture is simply absent."""
    if underlying.upper() not in UNDERLYINGS:
        raise HTTPException(status_code=422, detail=f"unknown underlying {underlying!r}")
    days = captured_days()
    return {"underlying": underlying.upper(), "underlyings": list(UNDERLYINGS),
            "days": days, "first": days[0] if days else None,
            "last": days[-1] if days else None}


@router.post("/sessions")
async def open_session(body: ConsoleOpen) -> dict:
    try:
        session = await asyncio.to_thread(
            registry.create,
            underlying=body.underlying,
            day=date.fromisoformat(body.day) if body.day else None,
            at=body.at, expiry=body.expiry, capital=body.capital,
            strike_window=body.strike_window,
            allow_fifty_strikes=body.allow_fifty_strikes,
            margin_per_lot_set=body.margin_per_lot_set,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _wire(session)
    if body.restore and (body.restore.journal or body.restore.alerts):
        try:
            await asyncio.to_thread(session.restore, body.restore.journal, body.restore.alerts,
                                    body.restore.bookmarks, body.restore.discarded,
                                    body.restore.fork)
        except (KeyError, ValueError, TypeError) as exc:
            registry.drop(session.id)
            raise HTTPException(status_code=422, detail=f"restore failed: {exc}") from exc
    return session.state()


def _local_fork_inputs(run_id: int, index: int) -> tuple[dict, list[dict], float | None, str]:
    """A local deployment's cycle (by the SAME index the cycle-detail page uses — see
    `routes.backtest.run_cycles`), its trades, capital and label."""
    from skas_algo.api.routes.backtest import _resolve_run_trades, cycle_briefs, run_cycles
    from skas_algo.db.base import session_scope
    from skas_algo.db.models import Algo, AlgoRun

    with session_scope() as db:
        run = db.get(AlgoRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        algo = db.get(Algo, run.algo_id)
        trades = _resolve_run_trades(run, db)
        briefs = cycle_briefs(run_cycles(run, algo, trades))
        if not (0 <= index < len(briefs)):
            raise HTTPException(status_code=404, detail="cycle index out of range")
        name = algo.name if algo else f"run #{run_id}"
        capital = float(algo.capital) if algo and algo.capital else None
        return briefs[index], trades, capital, name


def _peer_fork_inputs(run_id: int, index: int) -> tuple[dict, list[dict], float | None, str]:
    try:
        cyc = peer.run_cycles(run_id)
        trades = peer.trades(run_id)
    except peer.PeerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    briefs = cyc.get("cycles") or []
    if not (0 <= index < len(briefs)):
        raise HTTPException(status_code=404, detail="cycle index out of range on the peer")
    return briefs[index], trades, cyc.get("capital"), str(cyc.get("name") or f"run #{run_id}")


@router.post("/sessions/fork")
async def open_fork(body: ConsoleForkOpen) -> dict:
    """Open the console AS a deployment's cycle (services/console_fork): the run's actual
    fills restored on the cycle's entry day at the entry minute, `state.fork` carrying the
    actual outcome for the comparison strip. Read-only on the run — its trades are read,
    never touched; a LIVE run's cycle forks exactly like a paper one."""
    inputs = _local_fork_inputs if body.source == "local" else _peer_fork_inputs
    brief, trades, capital, name = await asyncio.to_thread(inputs, body.run_id, body.index)
    label = f"#{body.run_id} {name}" + (" · VPS" if body.source == "peer" else "")
    try:
        spec = await asyncio.to_thread(
            console_fork.fork_spec, cycle=console_fork.normalise_cycle(brief), trades=trades,
            capital=body.capital or capital or 500_000, source=body.source,
            run_id=body.run_id, label=label, run_name=name)
        session = await asyncio.to_thread(
            registry.create, underlying=spec["underlying"],
            day=date.fromisoformat(spec["day"]), at=spec["at"], expiry=spec["expiry"],
            capital=spec["capital"], strike_window=body.strike_window,
            allow_fifty_strikes=body.allow_fifty_strikes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _wire(session)
    try:
        await asyncio.to_thread(session.restore, spec["restore"]["journal"], [], [], [],
                                spec["fork"])
    except (KeyError, ValueError, TypeError) as exc:
        registry.drop(session.id)
        raise HTTPException(status_code=422, detail=f"fork failed: {exc}") from exc
    return session.state()


@router.get("/fork-sources")
async def fork_sources() -> dict:
    """What can be forked: the local paper/live runs with their cycles, and the peer's
    (the VPS over Tailscale) when one is configured. A peer failure fills `error`."""
    from skas_algo.api.routes.backtest import _resolve_run_trades, cycle_briefs, run_cycles
    from skas_algo.db.base import session_scope
    from skas_algo.db.enums import TradingMode
    from skas_algo.db.models import Algo, AlgoRun
    from sqlalchemy import select

    def _local() -> list[dict]:
        out = []
        with session_scope() as db:
            rows = db.execute(
                select(AlgoRun, Algo).join(Algo, AlgoRun.algo_id == Algo.id)
                .where(AlgoRun.mode != TradingMode.BACKTEST).order_by(AlgoRun.id.desc())
            ).all()
            for run, algo in rows:
                if run.archived:
                    continue
                trades = _resolve_run_trades(run, db)
                cycles = cycle_briefs(run_cycles(run, algo, trades)) if trades else []
                if not cycles:
                    continue
                out.append({"run_id": run.id, "name": algo.name, "mode": run.mode.value,
                            "strategy_id": algo.strategy_id,
                            "underlying": (run.params_snapshot or {}).get("underlying"),
                            "stopped": run.stopped_at is not None, "cycles": cycles})
        return out

    def _peer() -> dict:
        if not peer.configured():
            return {"configured": False, "url": None, "ok": False, "error": None, "runs": []}
        try:
            runs = []
            for d in peer.deployments()[:20]:
                rid = int(d.get("run_id"))
                cyc = peer.run_cycles(rid)
                if not cyc.get("cycles"):
                    continue
                runs.append({"run_id": rid, "name": cyc.get("name") or d.get("name"),
                             "mode": d.get("mode"), "strategy_id": d.get("strategy_id"),
                             "underlying": cyc.get("underlying") or d.get("underlying"),
                             "stopped": d.get("status") != "active",
                             "cycles": cyc["cycles"]})
            return {"configured": True, "url": peer.base_url(), "ok": True, "error": None,
                    "runs": runs}
        except peer.PeerError as exc:
            return {"configured": True, "url": peer.base_url(), "ok": False,
                    "error": str(exc), "runs": []}

    local, remote = await asyncio.gather(asyncio.to_thread(_local), asyncio.to_thread(_peer))
    return {"local": local, "peer": remote}


@router.post("/sessions/{session_id}/fork")
def fork_here(session_id: str) -> dict:
    """FORK HERE: drop the tape after the cursor (on a forked cycle, the run's actual later
    fills) so the rest can be traded differently. Replay only."""
    session = _replay_only(_get(session_id), "fork")
    session.truncate_after_cursor()
    return session.state()


@router.get("/sessions")
def list_sessions() -> dict:
    return {"sessions": registry.briefs()}


@router.get("/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    return _get(session_id).state()


@router.delete("/sessions/{session_id}")
def close_session(session_id: str) -> dict:
    if console_live.is_live_id(session_id):
        return {"closed": console_live.drop_console(session_id)}
    return {"closed": registry.drop(session_id)}


@router.post("/sessions/{session_id}/transport")
async def transport(session_id: str, body: ConsoleTransport) -> dict:
    """Move the cursor. A seek rebuilds the day from its open (see ConsoleSession) — 17 ms
    for a whole session — so it runs off the event loop like every other blocking read."""
    session = _replay_only(_get(session_id), "transport")

    def _move() -> dict:
        if body.op == "step":
            session.step(body.minutes)
        elif body.op == "seek":
            session.seek(body.at or "09:15")
        elif body.op == "sod":
            session.seek(session.state()["session"]["range"][0])
        elif body.op == "eod":
            session.seek(session.state()["session"]["range"][1])
        elif body.op == "day":
            session.shift_day(body.days)
        return session.state()

    try:
        return await asyncio.to_thread(_move)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/stage")
async def stage(session_id: str, body: ConsoleStage) -> dict:
    """Change the book. In REPLAY this applies straight away (undo is the safety net); in
    PAPER/LIVE it accumulates into a basket to be confirmed, and the response carries the
    book it WOULD produce so the chart can preview it. Either way the answer is the whole
    state, so the caller does not need to know which happened."""
    session = _get(session_id)
    try:
        await asyncio.to_thread(lambda: session.stage(**body.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return session.state()


@router.get("/sessions/{session_id}/presets")
def presets(session_id: str, lots: int = 1) -> dict:
    """Every preset resolved against the chain at the cursor."""
    session = _get(session_id)
    return {"presets": session.presets(lots), "lots": lots}


@router.post("/sessions/{session_id}/preset")
def apply_preset(session_id: str, body: ConsolePreset) -> dict:
    session = _get(session_id)
    try:
        session.apply_preset(body.preset, body.lots)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return session.state()


@router.post("/sessions/{session_id}/basket")
def apply_basket(session_id: str, body: ConsoleBasket) -> dict:
    session = _get(session_id)
    try:
        session.apply_basket(body.legs, label=body.label)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return session.state()


@router.post("/sessions/{session_id}/unstage")
async def unstage(session_id: str, body: ConsoleUnstage) -> dict:
    """Drop the pending change(s) on one leg (live/paper), or in replay delete the leg as
    if it was never traded."""
    session = _get(session_id)
    if isinstance(session, ConsoleSession):
        # replay: nothing is staged — the 🗑 removes the leg as if it was never traded
        session.delete_leg(body.leg_id)
        return await asyncio.to_thread(session.state)
    session.unstage(body.leg_id, body.kind)
    return session.state()


@router.post("/sessions/{session_id}/scale")
def scale_book(session_id: str, body: ConsoleScale) -> dict:
    session = _get(session_id)
    try:
        session.scale_book(body.factor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return session.state()


@router.post("/sessions/{session_id}/margin")
def set_margin_anchor(session_id: str, body: ConsoleMarginAnchor) -> dict:
    """Set (or clear, with 0) the manual margin anchor for one lot-set. Replay only — a
    live console's anchor is its strategy's own `margin_per_set`."""
    session = _replay_only(_get(session_id), "the margin anchor")
    if body.margin_per_lot_set < 0:
        raise HTTPException(status_code=422, detail="margin per lot-set cannot be negative")
    session.margin_per_lot_set = float(body.margin_per_lot_set)
    return session.state()


@router.post("/sessions/{session_id}/jump")
def jump(session_id: str, body: ConsoleJump) -> dict:
    session = _replay_only(_get(session_id), "jump")
    before = session.clock
    session.jump(body.kind, pct=body.pct)
    st = session.state()
    st["jumped"] = session.clock != before
    return st


@router.get("/sessions/{session_id}/what-if")
def what_if(session_id: str) -> dict:
    """Candidate adjustments at the cursor, priced off the chain and measured side by side
    (max loss, breakevens, POP, greeks, model margin, cash moved). Read-only; ranked by
    max loss and labelled so — the choice stays the owner's."""
    session = _replay_only(_get(session_id), "what-if")
    return session.what_if()


@router.post("/sessions/{session_id}/what-if")
def apply_what_if(session_id: str, body: ConsoleOps) -> dict:
    """Apply one candidate's operations as ONE undo group."""
    session = _replay_only(_get(session_id), "what-if")
    try:
        session.apply_ops(body.ops, label=body.label)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return session.state()


@router.post("/sessions/{session_id}/annotate")
def annotate(session_id: str, body: ConsoleAnnotate) -> dict:
    """Stamp the owner's 'why' on one action (undo group) of a replay session's tape."""
    session = _replay_only(_get(session_id), "annotate")
    if not session.annotate(body.group, body.why):
        raise HTTPException(status_code=404, detail=f"no action group {body.group}")
    return session.state()


@router.post("/sessions/{session_id}/bookmark")
def add_bookmark(session_id: str) -> dict:
    session = _replay_only(_get(session_id), "bookmark")
    session.add_bookmark()
    return session.state()


@router.delete("/sessions/{session_id}/bookmark/{minute}")
def remove_bookmark(session_id: str, minute: str) -> dict:
    session = _replay_only(_get(session_id), "bookmark")
    session.remove_bookmark(minute)
    return session.state()


@router.post("/sessions/{session_id}/save")
def save_session(session_id: str, body: ConsoleSave) -> dict:
    session = _replay_only(_get(session_id), "save")
    return console_store.save(body.name, session.save_payload())


@router.get("/saved")
def saved_sessions() -> dict:
    return {"saved": console_store.saved()}


@router.delete("/saved/{file}")
def delete_saved(file: str) -> dict:
    return {"deleted": console_store.delete(file)}


@router.post("/sessions/load")
async def load_session(body: ConsoleLoad) -> dict:
    """A saved file → a NEW session at its day/clock with the journal restored."""
    try:
        j = console_store.load(body.file)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=f"saved session not found: {exc}") from exc
    try:
        session = await asyncio.to_thread(
            registry.create, underlying=j["underlying"], day=date.fromisoformat(j["day"]),
            at=body.at or j.get("clock"), expiry=j.get("expiry"),
            capital=j.get("capital", 500_000),
            allow_fifty_strikes=j.get("allow_fifty_strikes", False),
            margin_per_lot_set=j.get("margin_per_lot_set", 0.0))
        _wire(session)
        await asyncio.to_thread(session.restore, j.get("journal", []), j.get("alerts", []),
                                j.get("bookmarks", []), j.get("discarded", []), j.get("fork"))
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return session.state()


@router.post("/sessions/{session_id}/alerts")
def arm_alert(session_id: str, body: ConsoleAlert) -> dict:
    """Arm a level. It trips ONCE at the cursor's minute, draws on the chart and the rail,
    and the page pauses autoplay on it — the "would I have caught that" moment a replay
    exists for. A rewind past the minute re-arms it."""
    session = _get(session_id)
    try:
        session.arm_alert(body.kind, body.value, note=body.note)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return session.state()


@router.delete("/sessions/{session_id}/alerts/{alert_id}")
def clear_alert(session_id: str, alert_id: str) -> dict:
    session = _get(session_id)
    session.clear_alert(alert_id)
    return session.state()


@router.post("/sessions/{session_id}/undo")
def undo(session_id: str) -> dict:
    """Undo the last action — all of it, so a roll's two fills and a basket's four legs go
    together. In replay this is what stands in for a confirm step."""
    session = _replay_only(_get(session_id), "undo")
    session.undo_last()
    return session.state()


@router.post("/sessions/{session_id}/commit")
async def commit(session_id: str, body: ConsoleCommit | None = None) -> dict:
    session = _get(session_id)
    limits = body.limits if body is not None else None
    try:
        if limits and hasattr(session, "_apply_limits"):      # a live console; replay ignores
            await asyncio.to_thread(session.commit, limits)
        else:
            await asyncio.to_thread(session.commit)
    except ValueError as exc:
        # includes LimitNotMarketable: refused before anything executed, never a halt
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return session.state()


@router.post("/sessions/{session_id}/reset")
def reset_book(session_id: str) -> dict:
    """Clear the book, the journal and the session's realised P&L — a clean slate at the
    same minute, without reopening the day."""
    session = _replay_only(_get(session_id), "reset")
    session.reset_book()
    return session.state()


@router.post("/sessions/{session_id}/discard")
def discard(session_id: str) -> dict:
    session = _get(session_id)
    session.discard()
    return session.state()


@router.get("/sessions/{session_id}/probe")
async def probe_price(session_id: str, right: str, strike: float) -> dict:
    """The last price this contract printed at or before the cursor, looking back through
    earlier sessions. On demand only, and the answer carries its own age — the ladder shows
    it as a reference beside a blank cell, never as a live mark."""
    session = _replay_only(_get(session_id), "the probe")
    try:
        return await asyncio.to_thread(session.probe, right, strike)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/chain")
async def set_chain(session_id: str, expiry: str | None = None,
                    window: int | None = None,
                    allow_fifty_strikes: bool | None = None) -> dict:
    session = _get(session_id)
    if not isinstance(session, ConsoleSession):          # live: the chip is the only knob
        if expiry is not None:
            session.set_expiry(expiry)
        if window is not None:
            session.strike_window = max(4, int(window))
        return await asyncio.to_thread(session.state)
    if expiry is not None:
        if expiry not in session.tape.expiries:
            raise HTTPException(status_code=422,
                                detail=f"{expiry} is not listed in the store on "
                                       f"{session.day.isoformat()}")
        session.expiry = expiry
    if window is not None:
        session.strike_window = max(4, int(window))
    if allow_fifty_strikes is not None:
        # The manual escape hatch from the NIFTY-100 rule (§8). OFF by default: this console
        # exists to rehearse what the automated strategies would do, and they can never place
        # a 50-strike, so offering one by default would invite an untradeable rehearsal.
        session.allow_fifty_strikes = bool(allow_fifty_strikes)
        session.market.allow_fifty_strikes = bool(allow_fifty_strikes)
    return await asyncio.to_thread(session.state)
