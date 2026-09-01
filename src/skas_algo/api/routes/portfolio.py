"""The personal net-worth tracker's API (/portfolio).

Thin on purpose. Everything that needs a ledger or a solver — FIFO lots, XIRR, per-lot tax —
is computed in ``services/portfolio`` and returned per holding; everything that is plain
arithmetic over those records — allocation, drift, bucket targets, goal progress — is done in
the browser so a keystroke in a target box updates every tile without a round trip.

None of this touches the trading path. No order is ever placed here, and the only broker calls
are ``holdings()`` and quotes — both read-only, both already used elsewhere.
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from skas_algo.api.deps import get_db
from skas_algo.api.models import (
    PortfolioBucketInput,
    PortfolioGoalInput,
    PortfolioHoldingInput,
    PortfolioPasteInput,
    PortfolioSeedInput,
    PortfolioSettingsInput,
    PortfolioSyncInput,
    PortfolioTransactionImport,
    PortfolioTransactionInput,
)
from skas_algo.db.models import (
    PortfolioBucket,
    PortfolioGoal,
    PortfolioHolding,
    PortfolioSetting,
    PortfolioTransaction,
)
from skas_algo.services import portfolio as pf
from skas_algo.services.portfolio import build_ledger
from skas_algo.services.portfolio_history import current_views, growth_series, record_snapshot
from skas_algo.services.portfolio_import import (
    guess_asset_class,
    parse_ledger_paste,
    parse_paste,
)
from skas_algo.services.portfolio_sync import sync_portfolio

router = APIRouter(tags=["portfolio"], prefix="/portfolio")

_CLASS_TARGETS_KEY = "class_targets"
_KIND_TARGETS_KEY = "kind_targets"


def _setting(db: Session, key: str, default: dict) -> dict:
    row = db.get(PortfolioSetting, key)
    return dict(row.value) if row and row.value else dict(default)


def _bucket_out(b: PortfolioBucket) -> dict:
    return {
        "id": b.id, "name": b.name, "target_pct": b.target_pct,
        "holding_ids": list(b.holding_ids or []), "sort_order": b.sort_order,
    }


def _goal_out(g: PortfolioGoal) -> dict:
    return {
        "id": g.id, "name": g.name, "target_amount": g.target_amount,
        "target_year": g.target_year, "monthly_sip": g.monthly_sip,
        "holding_ids": list(g.holding_ids or []), "benchmark": g.benchmark,
        "sort_order": g.sort_order,
    }


def _next_order(db: Session, model) -> int:
    rows = db.execute(select(model.sort_order)).scalars().all()
    return (max(rows) + 1) if rows else 0


# ------------------------------------------------------------------ read


@router.get("")
def get_portfolio(db: Session = Depends(get_db)) -> dict:
    """Everything the page renders from, in one call.

    One request rather than six because every tab derives from the same two lists — splitting
    them would let the KPI strip and the Allocation tab disagree for a frame."""
    holdings = current_views(db)
    buckets = db.execute(
        select(PortfolioBucket).order_by(PortfolioBucket.sort_order, PortfolioBucket.id)
    ).scalars().all()
    goals = db.execute(
        select(PortfolioGoal).order_by(PortfolioGoal.sort_order, PortfolioGoal.id)
    ).scalars().all()

    default_class = {k: v["target"] for k, v in pf.ASSET_CLASSES.items()}
    return {
        "holdings": holdings,
        "buckets": [_bucket_out(b) for b in buckets],
        "goals": [_goal_out(g) for g in goals],
        "asset_classes": {
            k: {"label": v["label"], "kind": v["kind"], "color": v["color"]}
            for k, v in pf.ASSET_CLASSES.items()
        },
        "class_targets": _setting(db, _CLASS_TARGETS_KEY, default_class),
        "kind_targets": _setting(db, _KIND_TARGETS_KEY, pf.KIND_TARGETS),
        "benchmarks": pf.BENCHMARKS,
        "tax": {
            "estimate_total": pf.apply_equity_exemption(holdings),
            "equity_ltcg_exemption": pf.EQUITY_LTCG_EXEMPTION,
        },
        "growth": growth_series(db),
        "server_time": datetime.now().astimezone().isoformat(),
    }


@router.get("/transactions/{holding_id}")
def list_transactions(holding_id: int, db: Session = Depends(get_db)) -> dict:
    rows = db.execute(
        select(PortfolioTransaction)
        .where(PortfolioTransaction.holding_id == holding_id)
        .order_by(PortfolioTransaction.on_date, PortfolioTransaction.id)
    ).scalars().all()
    return {
        "holding_id": holding_id,
        "transactions": [
            {"id": t.id, "on_date": t.on_date, "kind": t.kind, "units": t.units,
             "price": t.price, "fees": t.fees, "note": t.note}
            for t in rows
        ],
    }


# ------------------------------------------------------------------ holdings


@router.post("/holdings")
def create_holding(body: PortfolioHoldingInput, db: Session = Depends(get_db)) -> dict:
    row = PortfolioHolding(**body.model_dump(), sort_order=_next_order(db, PortfolioHolding))
    db.add(row)
    db.commit()
    return {"id": row.id}


@router.put("/holdings/{holding_id}")
def update_holding(
    holding_id: int, body: PortfolioHoldingInput, db: Session = Depends(get_db)
) -> dict:
    row = db.get(PortfolioHolding, holding_id)
    if row is None:
        raise HTTPException(404, "holding not found")
    for key, value in body.model_dump().items():
        setattr(row, key, value)
    db.commit()
    return {"id": row.id}


@router.delete("/holdings/{holding_id}")
def delete_holding(holding_id: int, db: Session = Depends(get_db)) -> dict:
    row = db.get(PortfolioHolding, holding_id)
    if row is None:
        raise HTTPException(404, "holding not found")
    db.execute(delete(PortfolioTransaction).where(PortfolioTransaction.holding_id == holding_id))
    # Leaving a deleted holding's id inside a bucket or goal would make its target share
    # silently wrong — the membership lists are pruned in the same transaction.
    for bucket in db.execute(select(PortfolioBucket)).scalars().all():
        if holding_id in (bucket.holding_ids or []):
            bucket.holding_ids = [i for i in bucket.holding_ids if i != holding_id]
    for goal in db.execute(select(PortfolioGoal)).scalars().all():
        if holding_id in (goal.holding_ids or []):
            goal.holding_ids = [i for i in goal.holding_ids if i != holding_id]
    db.delete(row)
    db.commit()
    return {"deleted": holding_id}


# ------------------------------------------------------------------ transactions


@router.post("/holdings/{holding_id}/transactions")
def add_transaction(
    holding_id: int, body: PortfolioTransactionInput, db: Session = Depends(get_db)
) -> dict:
    if db.get(PortfolioHolding, holding_id) is None:
        raise HTTPException(404, "holding not found")
    row = PortfolioTransaction(
        holding_id=holding_id, on_date=body.on_date.isoformat(), kind=body.kind,
        units=body.units, price=body.price, fees=body.fees, note=body.note,
    )
    db.add(row)
    db.commit()
    return {"id": row.id}


@router.delete("/transactions/{txn_id}")
def delete_transaction(txn_id: int, db: Session = Depends(get_db)) -> dict:
    row = db.get(PortfolioTransaction, txn_id)
    if row is None:
        raise HTTPException(404, "transaction not found")
    db.delete(row)
    db.commit()
    return {"deleted": txn_id}


@router.post("/transactions/parse")
def parse_transactions(body: PortfolioPasteInput) -> dict:
    """Turn a pasted history into ledger rows, WITHOUT saving anything.

    The page previews exactly this result and then imports the rows it returns, so the
    preview and the import can never disagree — a parser in the browser and a parser here
    would drift, and a partial import that looks complete is the worst outcome on this
    screen."""
    rows, errors = parse_paste(body.text)
    buys = sum(1 for r in rows if r.kind == "buy")
    return {
        "rows": [r.as_dict() for r in rows],
        "errors": errors,
        "summary": {
            "rows": len(rows), "buys": buys, "sells": len(rows) - buys,
            "earliest": min((r.on_date.isoformat() for r in rows), default=None),
            "latest": max((r.on_date.isoformat() for r in rows), default=None),
        },
    }


@router.post("/parse-ledger")
def parse_ledger(body: PortfolioPasteInput) -> dict:
    """Preview a WIDE multi-symbol tracking sheet, WITHOUT saving anything.

    Returns one entry per symbol so the owner can set each one's asset class and see what the
    seed would create, plus the errors that block it and the warnings that don't."""
    rows, errors, warnings = parse_ledger_paste(body.text)

    # Errors are scoped to the symbol whose row failed. One unreadable NIFTYBEES line must not
    # block eleven clean symbols — that made the seed button permanently dead on a 396-trade
    # paste with a single bad row.
    per_symbol_errors: dict[str, list[dict]] = {}
    global_errors: list[dict] = []
    for e in errors:
        if e.symbol:
            per_symbol_errors.setdefault(e.symbol, []).append(e.as_dict())
        else:
            global_errors.append(e.as_dict())

    by_symbol: dict[str, list] = {}
    for r in rows:
        by_symbol.setdefault(r.symbol, []).append(r)
    for symbol in per_symbol_errors:
        by_symbol.setdefault(symbol, [])

    symbols = []
    for symbol, group in sorted(by_symbol.items()):
        blocking = per_symbol_errors.get(symbol, [])
        # The FIFO result is shown BEFORE anything is written: a net position that looks wrong
        # here is a paste problem, and it is far cheaper to see it now than after the import.
        led = build_ledger([r.as_dict() for r in group]) if group else None
        symbols.append({
            "symbol": symbol,
            "name": symbol.title(),
            "asset_class": guess_asset_class(symbol),
            "trades": len(group),
            "buys": sum(1 for r in group if r.kind == "buy"),
            "sells": sum(1 for r in group if r.kind == "sell"),
            "units": round(led.units, 4) if led else 0.0,
            "invested": round(led.cost, 2) if led else 0.0,
            "realized": round(led.realized, 2) if led else 0.0,
            "oversold_units": round(led.oversold, 4) if led else 0.0,
            "first_trade": min(r.on_date for r in group).isoformat() if group else None,
            "last_trade": max(r.on_date for r in group).isoformat() if group else None,
            # Splits and bonuses, with the ratio the ledger derived from units held at the
            # time. Surfaced prominently: a 1:10 that reads 1:9.6 means a trade is missing
            # before it, and the cost basis of everything after would be quietly wrong.
            "actions": led.actions if led else [],
            "errors": blocking,
            "seedable": not blocking and bool(group),
        })

    return {
        "symbols": symbols,
        "errors": global_errors,
        "warnings": warnings,
        "summary": {
            "symbols": len(symbols),
            "seedable": sum(1 for s in symbols if s["seedable"]),
            "trades": len(rows),
        },
    }


@router.post("/seed")
def seed_from_ledger(body: PortfolioSeedInput, db: Session = Depends(get_db)) -> dict:
    """Create holdings from a pasted sheet and load each one's transactions.

    Refuses the WHOLE paste if any line failed to parse — a partial seed across a dozen symbols
    is far harder to unpick than a rejected one, and the cost basis it produces looks
    perfectly reasonable."""
    rows, errors, warnings = parse_ledger_paste(body.text)
    wanted = {s.symbol.upper(): s for s in body.symbols}

    # Refuse only what is actually broken. A global error (no tabs, a row with no symbol)
    # still blocks everything; a bad row blocks ITS symbol, and the caller cannot seed that
    # symbol even by asking — a partial history within one holding is the dangerous case,
    # because its cost basis looks perfectly reasonable and is wrong forever.
    global_errors = [e for e in errors if not e.symbol]
    if global_errors:
        raise HTTPException(422, global_errors[0].message)
    broken = {e.symbol for e in errors if e.symbol}
    if broken & set(wanted):
        names = ", ".join(sorted(broken & set(wanted)))
        raise HTTPException(422, f"{names} still has unreadable rows — fix or deselect it")
    by_symbol: dict[str, list] = {}
    for r in rows:
        if r.symbol in wanted:
            by_symbol.setdefault(r.symbol, []).append(r)

    results = []
    for symbol, group in sorted(by_symbol.items()):
        spec = wanted[symbol]
        # Match an existing holding on the sync ref first, then the name — so re-seeding
        # updates the same row instead of creating a duplicate beside it.
        existing = db.execute(
            select(PortfolioHolding).where(PortfolioHolding.sync_ref == symbol)
        ).scalars().first() or db.execute(
            select(PortfolioHolding).where(PortfolioHolding.name == spec.name)
        ).scalars().first()

        if existing is None:
            existing = PortfolioHolding(
                name=spec.name, asset_class=spec.asset_class,
                sort_order=_next_order(db, PortfolioHolding),
            )
            db.add(existing)
            db.flush()
            created = True
        else:
            created = False

        existing.asset_class = spec.asset_class
        existing.sync = body.sync
        existing.sync_source = "broker" if body.sync == "auto" else None
        existing.sync_ref = symbol
        existing.broker_account_id = body.broker_account_id if body.sync == "auto" else None

        if body.replace:
            db.execute(
                delete(PortfolioTransaction).where(
                    PortfolioTransaction.holding_id == existing.id
                )
            )
        for r in group:
            db.add(PortfolioTransaction(
                holding_id=existing.id, on_date=r.on_date.isoformat(), kind=r.kind,
                units=r.units, price=r.price, fees=0.0, note=r.note,
            ))
        existing.buy_month = min(r.on_date for r in group).strftime("%Y-%m")
        results.append({
            "symbol": symbol, "holding_id": existing.id,
            "created": created, "trades": len(group),
        })

    db.commit()

    # Echo the resulting positions so a bad paste is visible immediately, not next quarter.
    views = {v["id"]: v for v in current_views(db)}
    for res in results:
        view = views.get(res["holding_id"])
        if view:
            res.update({
                "units": view["units"], "invested": view["invested"],
                "realized": view["realized"], "oversold_units": view["oversold_units"],
            })

    return {
        "seeded": results,
        "warnings": warnings,
        "summary": {
            "holdings": len(results),
            "created": sum(1 for r in results if r["created"]),
            "trades": sum(r["trades"] for r in results),
        },
    }


@router.post("/transactions/import")
def import_transactions(body: PortfolioTransactionImport, db: Session = Depends(get_db)) -> dict:
    """Bulk-load a holding's history. ``replace`` wipes the existing ledger first — the safe
    way to re-import a corrected export, and the only way that doesn't double every buy."""
    if db.get(PortfolioHolding, body.holding_id) is None:
        raise HTTPException(404, "holding not found")
    if body.replace:
        db.execute(
            delete(PortfolioTransaction).where(
                PortfolioTransaction.holding_id == body.holding_id
            )
        )
    for r in body.rows:
        db.add(PortfolioTransaction(
            holding_id=body.holding_id, on_date=r.on_date.isoformat(), kind=r.kind,
            units=r.units, price=r.price, fees=r.fees, note=r.note,
        ))
    db.commit()
    ledger = current_views(db)
    view = next((v for v in ledger if v["id"] == body.holding_id), None)
    return {
        "imported": len(body.rows),
        "replaced": body.replace,
        # Echo the resulting position so a bad import is visible immediately, not next week.
        "units": view["units"] if view else None,
        "invested": view["invested"] if view else None,
        "oversold_units": view["oversold_units"] if view else 0.0,
    }


# ------------------------------------------------------------------ buckets & goals


@router.post("/buckets")
def create_bucket(body: PortfolioBucketInput, db: Session = Depends(get_db)) -> dict:
    row = PortfolioBucket(**body.model_dump(), sort_order=_next_order(db, PortfolioBucket))
    db.add(row)
    db.commit()
    return _bucket_out(row)


@router.put("/buckets/{bucket_id}")
def update_bucket(
    bucket_id: int, body: PortfolioBucketInput, db: Session = Depends(get_db)
) -> dict:
    row = db.get(PortfolioBucket, bucket_id)
    if row is None:
        raise HTTPException(404, "bucket not found")
    for key, value in body.model_dump().items():
        setattr(row, key, value)
    db.commit()
    return _bucket_out(row)


@router.delete("/buckets/{bucket_id}")
def delete_bucket(bucket_id: int, db: Session = Depends(get_db)) -> dict:
    row = db.get(PortfolioBucket, bucket_id)
    if row is None:
        raise HTTPException(404, "bucket not found")
    db.delete(row)
    db.commit()
    return {"deleted": bucket_id}


@router.post("/goals")
def create_goal(body: PortfolioGoalInput, db: Session = Depends(get_db)) -> dict:
    row = PortfolioGoal(**body.model_dump(), sort_order=_next_order(db, PortfolioGoal))
    db.add(row)
    db.commit()
    return _goal_out(row)


@router.put("/goals/{goal_id}")
def update_goal(goal_id: int, body: PortfolioGoalInput, db: Session = Depends(get_db)) -> dict:
    row = db.get(PortfolioGoal, goal_id)
    if row is None:
        raise HTTPException(404, "goal not found")
    for key, value in body.model_dump().items():
        setattr(row, key, value)
    db.commit()
    return _goal_out(row)


@router.delete("/goals/{goal_id}")
def delete_goal(goal_id: int, db: Session = Depends(get_db)) -> dict:
    row = db.get(PortfolioGoal, goal_id)
    if row is None:
        raise HTTPException(404, "goal not found")
    db.delete(row)
    db.commit()
    return {"deleted": goal_id}


# ------------------------------------------------------------------ settings & sync


@router.put("/settings")
def update_settings(body: PortfolioSettingsInput, db: Session = Depends(get_db)) -> dict:
    for key, value in (
        (_CLASS_TARGETS_KEY, body.class_targets),
        (_KIND_TARGETS_KEY, body.kind_targets),
    ):
        if value is None:
            continue
        row = db.get(PortfolioSetting, key)
        if row is None:
            row = PortfolioSetting(key=key, value={})
            db.add(row)
        row.value = value
    db.commit()
    default_class = {k: v["target"] for k, v in pf.ASSET_CLASSES.items()}
    return {
        "class_targets": _setting(db, _CLASS_TARGETS_KEY, default_class),
        "kind_targets": _setting(db, _KIND_TARGETS_KEY, pf.KIND_TARGETS),
    }


@router.post("/sync")
def sync(body: PortfolioSyncInput, db: Session = Depends(get_db)) -> dict:
    """Refresh auto holdings from their price sources, then stamp today's snapshot.

    The snapshot is recorded here as well as from maintenance so a manual sync leaves a point
    on the growth chart — otherwise a day the box was asleep would simply have no history."""
    report = sync_portfolio(db, holding_ids=body.holding_ids)
    record_snapshot(db)
    return report.as_dict()


@router.get("/funds/search")
def search_funds(q: str, limit: int = 20) -> dict:
    """AMFI scheme lookup for the Add-holding modal — name in, ISIN out.

    Reads the CACHED NAV file only (no fetch): this is typed into with every keystroke, and a
    30-second download behind an autocomplete would make the field feel broken."""
    from skas_algo.data import amfi

    rows = amfi.search(q, limit=limit)
    return {
        "results": [
            {"isin": r.isin, "scheme_code": r.scheme_code, "name": r.name,
             "nav": r.nav, "as_of": r.as_of.isoformat()}
            for r in rows
        ]
    }


@router.post("/funds/refresh")
def refresh_funds() -> dict:
    """Pull today's AMFI NAV file. Separate from ``/sync`` so the fund list can be populated
    (and searched) before any holding references it."""
    from skas_algo.data import amfi

    amfi.refresh()
    latest, previous = amfi.load(fetch=False)
    as_of = next(iter(latest.values())).as_of if latest else None
    return {
        "schemes": len({r.scheme_code for r in latest.values()}),
        "as_of": as_of.isoformat() if as_of else None,
        "stale_days": pf_stale(as_of),
        "has_previous": bool(previous),
    }


def pf_stale(as_of: date | None) -> int | None:
    from skas_algo.data import amfi

    return None if as_of is None else amfi.stale_days(as_of)


@router.post("/snapshot")
def snapshot_now(db: Session = Depends(get_db)) -> dict:
    row = record_snapshot(db)
    if row is None:
        return {"recorded": False, "reason": "no holdings tracked yet"}
    return {"recorded": True, "on_date": row.on_date, "value": row.value}
