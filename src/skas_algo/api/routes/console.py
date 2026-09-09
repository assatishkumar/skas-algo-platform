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

from skas_algo.api.models import ConsoleOpen, ConsoleTransport
from skas_algo.data.option_intraday_store import captured_days
from skas_algo.services.options_console import registry
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
