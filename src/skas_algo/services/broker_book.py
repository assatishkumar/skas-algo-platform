"""broker_book — the broker's net book beside the platform's, per contract, with the runs
behind each line (the "broker mirror", planned 2026-09-03).

A READ-ONLY view over ``manager.reconcile_account_book`` — the function the hourly
reconciliation already runs. It nets every LIVE real-order run's lots per contract, reads the
broker's net positions (holdings win for delivery equity), and halts a run on a mismatch. Until
now it spoke only when it was angry, through an alert; this lets the owner look at what it
sees. It calls THAT function rather than aggregating on its own: a second copy drifts, and then
a green screen sits beside a halted run.

Why it exists: on 2026-09-02 Kite showed −₹60k while the VPS tiles showed −₹12.6k. The gap was
one contract two runs sat on opposite sides of — iron_fly short 195 @390.05, fv_call long 780
@141.35 — which the broker nets to long 585 and, being a broker, BOOKS the matched 195 as a
closed trade: (390.05 − 141.35) × 195 = ₹48,496 realised on its side, still open on ours.
That number existed nowhere in the platform, which is why neither screen could explain it.
``booked_at_broker`` is that number, per contract.

Three rules, from the plan:
  * paper runs are excluded (the reconciler already does that — runs 6/9/12/25 mirror the live
    ones position for position and place nothing; counting them invents mismatches);
  * NO P&L subtraction is offered — Zerodha rebases overnight F&O to the previous close, so
    its P&L is day-based while ours is since-entry; the two are not comparable and a column
    that subtracted them would be read as a bug in whichever side looked worse;
  * a failed read SAYS SO (``ok: False`` + the reason) — off-hours the token is routinely dead,
    and an empty table reads as "flat", the same false comfort as the old 04:50 halt.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from skas_algo.engine.options.instrument import parse as parse_option

IST = timezone(timedelta(hours=5, minutes=30))

# A broker tradingsymbol that is an option or a future (NIFTY26SEP24300CE, BANKNIFTY26SEPFUT).
_FNO_TS = re.compile(r"\d.*(CE|PE|FUT)$")


def _segment(ts: str, lots: list[dict]) -> str:
    if any(parse_option(str(lot.get("symbol") or "")) is not None for lot in lots):
        return "fno"
    return "fno" if _FNO_TS.search(ts.upper()) else "equity"


def _booked_at_broker(lots: list[dict]) -> dict | None:
    """When runs sit on BOTH sides of one contract the broker has already netted the matched
    quantity and booked it: (avg short entry − avg long entry) × matched units."""
    longs = [lot for lot in lots if lot["direction"] > 0 and lot.get("price") is not None]
    shorts = [lot for lot in lots if lot["direction"] < 0 and lot.get("price") is not None]
    lu = sum(lot["units"] for lot in longs)
    su = sum(lot["units"] for lot in shorts)
    matched = min(lu, su)
    if matched <= 0:
        return None
    avg_long = sum(lot["price"] * lot["units"] for lot in longs) / lu
    avg_short = sum(lot["price"] * lot["units"] for lot in shorts) / su
    return {
        "matched_units": matched,
        "avg_long": round(avg_long, 2),
        "avg_short": round(avg_short, 2),
        "amount": round((avg_short - avg_long) * matched, 2),
    }


def build_book(manager, account_id: int, adapter, *, account: dict | None = None) -> dict:
    """The book view for one account. Never raises for a broker problem — it reports it."""
    from skas_algo.live.manager import ReconcileUnavailable

    stamp = datetime.now(IST).isoformat()
    details: dict = {}
    try:
        mismatch = manager.reconcile_account_book(account_id, adapter, details)
    except ReconcileUnavailable as exc:
        return {"ok": False, "error": str(exc), "as_of": stamp, "account": account, "rows": []}
    ours: dict[str, float] = details.get("ours") or {}
    broker: dict[str, float] = details.get("broker") or {}
    lots_by_ts: dict[str, list[dict]] = details.get("lots") or {}

    rows: list[dict] = []
    for ts in sorted(set(ours) | set(broker)):
        p = float(ours.get(ts, 0.0))
        b = float(broker.get(ts, 0.0))
        lots = lots_by_ts.get(ts, [])
        if abs(p) < 1e-6 and abs(b) < 1e-6 and not lots:
            continue
        if abs(b - p) <= 1e-6:
            status = "match"
        elif abs(p) < 1e-6:
            status = "broker_only"
        elif abs(b) < 1e-6:
            status = "platform_only"
        else:
            status = "mismatch"
        rows.append({
            "tradingsymbol": ts,
            "symbol": next((lot["symbol"] for lot in lots), None),  # our internal form, if known
            "segment": _segment(ts, lots),
            "platform_net": p,
            "broker_net": b,
            "diff": round(b - p, 4),
            "status": status,
            "lots": sorted(lots, key=lambda lot: (lot["direction"], str(lot.get("name") or ""))),
            "booked_at_broker": _booked_at_broker(lots),
        })
    order = {"mismatch": 0, "platform_only": 1, "broker_only": 2, "match": 3}
    rows.sort(key=lambda r: (order[r["status"]], r["tradingsymbol"]))
    booked_total = sum((r["booked_at_broker"] or {}).get("amount", 0.0) for r in rows)
    return {
        "ok": True,
        "as_of": stamp,
        "account": account,
        "mismatch": mismatch,  # the reconciler's own words, None when the books agree
        "rows": rows,
        "runs_counted": details.get("runs_counted") or [],
        "runs_skipped": details.get("runs_skipped") or [],
        "totals": {
            "rows": len(rows),
            "mismatches": sum(1 for r in rows if r["status"] != "match"),
            "booked_at_broker": round(booked_total, 2),
        },
    }
