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
    ConsoleBasket,
    ConsoleJump,
    ConsoleLoad,
    ConsoleOpen,
    ConsolePreset,
    ConsoleSave,
    ConsoleStage,
    ConsoleTransport,
)
from skas_algo.data.option_intraday_store import captured_days
from skas_algo.services.options_console import registry
from skas_algo.services.options_console import store as console_store
from skas_algo.services.options_console.session import UNDERLYINGS, ConsoleSession

router = APIRouter(prefix="/console", tags=["console"])


def _get(session_id: str) -> ConsoleSession:
    try:
        return registry.get(session_id)
    except KeyError:
        raise HTTPException(status_code=404,
                            detail="console session not found — it expired or the backend "
                                   "restarted; open a new one") from None


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
    if body.restore and (body.restore.journal or body.restore.alerts):
        try:
            await asyncio.to_thread(session.restore, body.restore.journal, body.restore.alerts,
                                    body.restore.bookmarks)
        except (KeyError, ValueError, TypeError) as exc:
            registry.drop(session.id)
            raise HTTPException(status_code=422, detail=f"restore failed: {exc}") from exc
    return session.state()


@router.get("/sessions")
def list_sessions() -> dict:
    return {"sessions": registry.briefs()}


@router.get("/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    return _get(session_id).state()


@router.delete("/sessions/{session_id}")
def close_session(session_id: str) -> dict:
    return {"closed": registry.drop(session_id)}


@router.post("/sessions/{session_id}/transport")
async def transport(session_id: str, body: ConsoleTransport) -> dict:
    """Move the cursor. A seek rebuilds the day from its open (see ConsoleSession) — 17 ms
    for a whole session — so it runs off the event loop like every other blocking read."""
    session = _get(session_id)

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


@router.post("/sessions/{session_id}/jump")
def jump(session_id: str, body: ConsoleJump) -> dict:
    session = _get(session_id)
    before = session.clock
    session.jump(body.kind, pct=body.pct)
    st = session.state()
    st["jumped"] = session.clock != before
    return st


@router.post("/sessions/{session_id}/bookmark")
def add_bookmark(session_id: str) -> dict:
    session = _get(session_id)
    session.add_bookmark()
    return session.state()


@router.delete("/sessions/{session_id}/bookmark/{minute}")
def remove_bookmark(session_id: str, minute: str) -> dict:
    session = _get(session_id)
    session.remove_bookmark(minute)
    return session.state()


@router.post("/sessions/{session_id}/save")
def save_session(session_id: str, body: ConsoleSave) -> dict:
    session = _get(session_id)
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
        await asyncio.to_thread(session.restore, j.get("journal", []), j.get("alerts", []),
                                j.get("bookmarks", []))
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
    session = _get(session_id)
    session.undo_last()
    return session.state()


@router.post("/sessions/{session_id}/commit")
async def commit(session_id: str) -> dict:
    session = _get(session_id)
    try:
        await asyncio.to_thread(session.commit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return session.state()


@router.post("/sessions/{session_id}/reset")
def reset_book(session_id: str) -> dict:
    """Clear the book, the journal and the session's realised P&L — a clean slate at the
    same minute, without reopening the day."""
    session = _get(session_id)
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
    session = _get(session_id)
    try:
        return await asyncio.to_thread(session.probe, right, strike)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/chain")
async def set_chain(session_id: str, expiry: str | None = None,
                    window: int | None = None,
                    allow_fifty_strikes: bool | None = None) -> dict:
    session = _get(session_id)
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
