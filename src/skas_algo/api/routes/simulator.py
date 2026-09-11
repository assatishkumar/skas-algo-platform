"""The Simulator (/simulator): manual backtests traded in the console, cycle by cycle."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from skas_algo.api.deps import get_db
from skas_algo.api.models import SimBank, SimCreate, SimNextDay, SimSave, SimUpdate
from skas_algo.services import simulator

router = APIRouter(prefix="/simulator", tags=["simulator"])


@router.get("")
def list_strategies(db: Session = Depends(get_db)) -> dict:
    return {"strategies": simulator.list_all(db)}


@router.post("")
def create_strategy(body: SimCreate, db: Session = Depends(get_db)) -> dict:
    try:
        out = simulator.create(db, name=body.name, underlying=body.underlying,
                               capital=body.capital, playbook=body.playbook,
                               start_day=body.start_day)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return out


@router.get("/{sim_id}")
def get_strategy(sim_id: int, db: Session = Depends(get_db)) -> dict:
    try:
        return simulator.get(db, sim_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/{sim_id}")
def update_strategy(sim_id: int, body: SimUpdate, db: Session = Depends(get_db)) -> dict:
    try:
        out = simulator.update(db, sim_id, name=body.name, playbook=body.playbook)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    return out


@router.delete("/{sim_id}")
def delete_strategy(sim_id: int, db: Session = Depends(get_db)) -> dict:
    try:
        simulator.delete(db, sim_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    return {"deleted": sim_id}


@router.get("/{sim_id}/open")
def open_spec(sim_id: int, db: Session = Depends(get_db)) -> dict:
    """What the console opens for this strategy: the open cycle where it stands, else the
    next day at 09:30 with the equity as capital."""
    try:
        return simulator.open_spec(db, sim_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{sim_id}/cycles/{n}")
def cycle_journal(sim_id: int, n: int, db: Session = Depends(get_db)) -> dict:
    """A banked cycle's tape, to replay it in the console read-only."""
    try:
        return simulator.cycle_journal(db, sim_id, n)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{sim_id}/autosave")
def autosave(sim_id: int, body: SimSave, db: Session = Depends(get_db)) -> dict:
    try:
        out = simulator.autosave(db, sim_id, body.payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    return out


@router.post("/{sim_id}/bank")
def bank(sim_id: int, body: SimBank, db: Session = Depends(get_db)) -> dict:
    try:
        out = simulator.bank(db, sim_id, note=body.note, tags=body.tags, margin=body.margin,
                             margin_source=body.margin_source, payload=body.payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return out


@router.post("/{sim_id}/next-day")
def next_day(sim_id: int, body: SimNextDay, db: Session = Depends(get_db)) -> dict:
    try:
        out = simulator.set_next_day(db, sim_id, body.day)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return out
