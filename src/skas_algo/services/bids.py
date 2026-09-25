"""BIDS (Buy In Dips) over the /portfolio — rules, ladder state, suggestions (2026-09-25).

The owner's design: every market-linked holding (stocks, ETFs, mutual funds, US stocks) is
bought again each time it falls another X% below its recent high — y, then 2y, 3y … up to a
cap — funded from an ETF like value_investing. Holdings at a connected broker with a running
BIDS deployment buy AUTOMATICALLY (strategies/bids.py, phase 2); everything else is a
SUGGESTION here, which the owner accepts (a BUY row in the ledger, their own order) or skips
(the level is consumed; the next X% still fires).

This module never places an order and never imports the order path (§8a: the portfolio is
not part of the trading system). The ladder arithmetic is services/bids_ladder.py — the
same function the strategy calls.

A holding JOINS at today's price (owner decision 2026-09-25): its first peak is the price on
the day it entered BIDS ("joined"), not an old high it may be far below — so joining never
fires a lump of levels at once, and no price history is needed for any asset class. The peak
then rises with every new high ("high") and resets the ladder; the owner can type a
reference high instead ("manual"), which restarts the ladder from it. The panel says which.

A holding quoted in a foreign currency (a US stock) runs its ladder in THAT currency (owner
2026-09-25: "different config for INR and $"): the dip is measured on the dollar price — the
rupee price also moves with USD/INR, and a weaker rupee must not read as a smaller dip — and
its amounts come from the ``usd_*`` defaults, in dollars. Accepting converts the dollar fill
to rupees at the rate of the holding's last sync, because the ledger is kept in rupees.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from skas_algo.db.models import (
    PortfolioBidsRule,
    PortfolioBidsSuggestion,
    PortfolioHolding,
    PortfolioSetting,
    PortfolioTransaction,
)
from skas_algo.services.bids_ladder import LadderRule, LadderState, evaluate, next_trigger

logger = logging.getLogger("skas_algo.bids")

SETTINGS_KEY = "bids"
DEFAULTS = {
    "enabled": True,
    "dip_pct": 5.0,
    "amount": 5000.0,
    "max_levels": 5,
    "fund_source": "LIQUIDBEES",
    # the same three knobs for holdings quoted in dollars (US stocks), in dollars
    "usd_dip_pct": 5.0,
    "usd_amount": 100.0,
    "usd_max_levels": 5,
}
CLASS_GROUPS = {"us": "US stocks", "mf": "Mutual funds", "etf": "ETFs", "stk": "Stocks"}
ELIGIBLE_CLASSES = ("stk", "etf", "mf", "us")
BIDS_NOTE = "BIDS"


# ------------------------------------------------------------------ settings & rules
def defaults(db: Session) -> dict:
    row = db.get(PortfolioSetting, SETTINGS_KEY)
    out = dict(DEFAULTS)
    if row is not None and isinstance(row.value, dict):
        out.update({k: v for k, v in row.value.items() if k in DEFAULTS and v is not None})
    return out


def save_defaults(db: Session, values: dict) -> dict:
    row = db.get(PortfolioSetting, SETTINGS_KEY)
    if row is None:
        row = PortfolioSetting(key=SETTINGS_KEY, value={})
        db.add(row)
    merged = {**defaults(db), **{k: v for k, v in values.items() if k in DEFAULTS}}
    row.value = merged
    db.commit()
    return merged


def eligible(h: PortfolioHolding) -> bool:
    return str(h.asset_class or "").lower() in ELIGIBLE_CLASSES


def currency_of(h: PortfolioHolding) -> str:
    """The currency the holding is QUOTED in — its ladder, rule amounts and suggestions are
    all in it. INR unless the sync recorded a foreign quote with a price."""
    ccy = str(h.native_currency or "").upper()
    if ccy and ccy != "INR" and h.native_price and float(h.native_price) > 0:
        return ccy
    return "INR"


def ladder_price(h: PortfolioHolding) -> float:
    """The price the ladder reads: the native quote for a foreign holding, else last_price."""
    if currency_of(h) != "INR":
        return float(h.native_price or 0.0)
    return float(h.last_price or 0.0)


def fx_to_inr(h: PortfolioHolding) -> float:
    """Rupees per unit of the holding's currency at its last sync (1 for INR)."""
    if currency_of(h) == "INR":
        return 1.0
    return float(h.last_price or 0.0) / float(h.native_price)


def rule_for(h: PortfolioHolding, r: PortfolioBidsRule | None, d: dict) -> LadderRule:
    pre = "" if currency_of(h) == "INR" else "usd_"
    return LadderRule(
        dip_pct=float(r.dip_pct if r is not None and r.dip_pct is not None
                      else d[f"{pre}dip_pct"]),
        amount=float(r.amount if r is not None and r.amount is not None else d[f"{pre}amount"]),
        max_levels=int(r.max_levels if r is not None and r.max_levels is not None
                       else d[f"{pre}max_levels"]),
    )


def mode_of(h: PortfolioHolding, r: PortfolioBidsRule | None,
            auto_accounts: dict[int, set[str]] | None = None) -> str:
    """auto | suggest | excluded. AUTO only when a running BIDS deployment on the holding's
    quote account trades that symbol (``auto_accounts`` = {account_id: {symbols}}, supplied by
    the caller from the live manager); everything else is a suggestion, so a dip is never
    silently dropped because no run exists."""
    if r is not None and not r.enabled:
        return "excluded"
    if (auto_accounts and h.sync_source == "broker" and h.broker_account_id is not None
            and str(h.asset_class or "").lower() in ("stk", "etf")
            and str(h.sync_ref or "").upper() in auto_accounts.get(h.broker_account_id, set())):
        return "auto"
    return "suggest"


def _rules(db: Session) -> dict[int, PortfolioBidsRule]:
    return {r.holding_id: r for r in db.execute(select(PortfolioBidsRule)).scalars().all()}


def update_rule(db: Session, holding_id: int, values: dict) -> PortfolioBidsRule:
    """Owner edits from the tab. ``peak`` typed by hand becomes the ladder's peak ("manual")
    and resets the levels — a new reference is a new ladder."""
    h = db.get(PortfolioHolding, holding_id)
    if h is None:
        raise KeyError(holding_id)
    r = db.execute(select(PortfolioBidsRule).where(PortfolioBidsRule.holding_id == holding_id)
                   ).scalars().first()
    if r is None:
        r = PortfolioBidsRule(holding_id=holding_id, enabled=True, levels_fired=0)
        db.add(r)
    for key in ("enabled", "dip_pct", "amount", "max_levels"):
        if key in values:
            setattr(r, key, values[key])
    if values.get("peak"):
        r.peak = float(values["peak"])
        r.peak_asof = date.today().isoformat()
        r.peak_source = "manual"
        r.levels_fired = 0
        r.last_eval_asof = None             # a new reference is judged at the next check
        _expire_pending(db, holding_id)
    db.commit()
    return r


# ------------------------------------------------------------------ joining
def seed(h: PortfolioHolding, r: PortfolioBidsRule, today: date) -> None:
    """First sight of a holding: it joins at today's price. Never overwrites a typed peak."""
    if r.peak is not None:
        return
    px = ladder_price(h)
    if px <= 0:
        return
    r.peak = px
    r.peak_source = "joined"
    r.peak_asof = today.isoformat()
    r.levels_fired = 0


# ------------------------------------------------------------------ evaluation
def _expire_pending(db: Session, holding_id: int) -> int:
    rows = db.execute(select(PortfolioBidsSuggestion).where(
        PortfolioBidsSuggestion.holding_id == holding_id,
        PortfolioBidsSuggestion.status == "pending")).scalars().all()
    for s in rows:
        s.status = "expired"
        s.resolved_at = datetime.now(UTC)
    return len(rows)


def evaluate_portfolio(db: Session, today: date | None = None, *, auto_accounts=None,
                       notify=True) -> dict:
    """Run every eligible, non-excluded holding in SUGGEST mode through the ladder on its
    latest price — once per new price date (``last_eval_asof``), so the 09:30 and 16:00 passes
    never double-count a close. Returns {evaluated, new: [suggestion dicts], resets}."""
    today = today or date.today()
    d = defaults(db)
    out = {"evaluated": 0, "new": [], "resets": 0}
    if not d.get("enabled", True):
        return out
    rules = _rules(db)
    for h in db.execute(select(PortfolioHolding)).scalars().all():
        px = ladder_price(h)
        if not eligible(h) or px <= 0:
            continue
        r = rules.get(h.id)
        if mode_of(h, r, auto_accounts) != "suggest":
            continue
        if r is None:
            r = PortfolioBidsRule(holding_id=h.id, enabled=True, levels_fired=0)
            db.add(r)
            rules[h.id] = r
        asof = str(h.price_asof or today.isoformat())[:10]
        if r.peak is None:
            seed(h, r, today)
            r.last_eval_asof = asof
            continue                         # the joining price is the reference, not a dip
        if r.last_eval_asof == asof:
            continue
        rule = rule_for(h, r, d)
        res = evaluate(LadderState(r.peak, int(r.levels_fired or 0)), px, rule)
        out["evaluated"] += 1
        if res.state.peak != r.peak:          # a new high: the ladder resets
            if res.reset:
                out["resets"] += 1
            _expire_pending(db, h.id)
            r.peak = res.state.peak
            r.peak_asof = asof
            r.peak_source = "high"
        for t in res.triggers:
            s = PortfolioBidsSuggestion(
                holding_id=h.id, peak=r.peak, peak_asof=r.peak_asof or asof, level=t.level,
                trigger_price=t.trigger_price, price=px, amount=t.amount,
                created_on=asof, status="pending")
            db.add(s)
            out["new"].append({"holding": h.name, "level": t.level, "amount": t.amount,
                               "trigger_price": t.trigger_price, "price": px,
                               "currency": currency_of(h)})
        r.levels_fired = res.state.levels_fired
        r.last_eval_asof = asof
    db.commit()
    if notify and out["new"]:
        _notify(out["new"])
    return out


def _sym(ccy: str | None) -> str:
    return {"INR": "₹", "USD": "$"}.get(str(ccy or "INR").upper(), f"{ccy} ")


def _notify(new: list[dict]) -> None:
    try:
        from skas_algo.notify import Alert, AlertLevel, build_notifier

        lines = [f"{n['holding']} · L{n['level']} · {_sym(n.get('currency'))}{n['amount']:,.0f} "
                 f"(at {n['price']:,.2f}, trigger {n['trigger_price']:,.2f})" for n in new[:12]]
        more = f"\n… +{len(new) - 12} more" if len(new) > 12 else ""
        build_notifier().send(Alert(
            f"BIDS · {len(new)} dip level(s) to review",
            "\n".join(lines) + more + "\nAccept or skip them on Portfolio → BIDS.",
            AlertLevel.INFO))
    except Exception:  # pragma: no cover - a failed push never loses a suggestion
        logger.exception("bids: notification failed")


# ------------------------------------------------------------------ accept / skip
def accept(db: Session, sid: int, *, units: float, price: float, on_date: date,
           fees: float = 0.0) -> dict:
    """Record the owner's buy for a suggestion: a BUY row in the holding's ledger (the typed
    position carried in first when the ledger is empty — §8a), and the suggestion closed.
    ``price`` is in the holding's currency; a foreign fill is booked in rupees at the rate of
    the holding's last sync (the ledger is kept in rupees), and the note keeps the original."""
    from skas_algo.services.portfolio_history import carry_typed_position

    s = db.get(PortfolioBidsSuggestion, sid)
    if s is None:
        raise KeyError(sid)
    if s.status != "pending":
        raise ValueError(f"suggestion is {s.status}, not pending")
    if units <= 0 or price <= 0:
        raise ValueError("units and price must be positive")
    h = db.get(PortfolioHolding, s.holding_id)
    ccy = currency_of(h)
    fx = fx_to_inr(h)
    note = f"{BIDS_NOTE} L{s.level} · dip {_sym(ccy)}{s.trigger_price:,.2f}"
    if ccy != "INR":
        note += f" · {_sym(ccy)}{float(price):,.2f} at {fx:,.2f}"
    carry_typed_position(db, h, before=on_date.isoformat())
    row = PortfolioTransaction(
        holding_id=s.holding_id, on_date=on_date.isoformat(), kind="buy", units=float(units),
        price=round(float(price) * fx, 4), fees=float(fees or 0.0), note=note)
    db.add(row)
    db.flush()
    s.status = "accepted"
    s.resolved_at = datetime.now(UTC)
    s.txn_id = row.id
    db.commit()
    return {"id": s.id, "status": s.status, "txn_id": row.id}


def skip(db: Session, sid: int) -> dict:
    s = db.get(PortfolioBidsSuggestion, sid)
    if s is None:
        raise KeyError(sid)
    if s.status != "pending":
        raise ValueError(f"suggestion is {s.status}, not pending")
    s.status = "skipped"
    s.resolved_at = datetime.now(UTC)
    db.commit()
    return {"id": s.id, "status": s.status}


# ------------------------------------------------------------------ the tab's view
def _position(h: PortfolioHolding, rec: dict | None, ccy: str) -> dict:
    """Units, average cost, LTP, value and return — in the ladder's currency where the
    holding's own cost is known in it (a typed US position), else in rupees. A US holding
    with a ledger has only a rupee cost basis, and dividing it by today's rate would not
    recover the dollars paid (§8a), so it is shown in rupees and says so."""
    rec = rec or {}
    units = float(rec.get("units") or 0.0)
    inv_inr = float(rec.get("invested") or 0.0)
    val_inr = float(rec.get("value") or 0.0)
    out = {"units": units or None, "value_inr": val_inr, "invested_inr": inv_inr,
           "gain_inr": val_inr - inv_inr}
    native_ok = (ccy != "INR" and not rec.get("txn_count") and h.native_invested
                 and units > 0)
    if native_ok:
        ltp = float(h.native_price)
        inv = float(h.native_invested)
        val = units * ltp
        out.update({"currency": ccy, "ltp": ltp, "invested": inv, "value": val,
                    "avg_price": inv / units})
    else:
        out.update({"currency": "INR", "ltp": float(h.last_price or 0.0) or None,
                    "invested": inv_inr, "value": val_inr,
                    "avg_price": inv_inr / units if units > 0 and inv_inr > 0 else None})
    out["gain"] = out["value"] - out["invested"]
    out["gain_pct"] = (round(out["gain"] / out["invested"] * 100.0, 2)
                       if out["invested"] > 0 else None)
    return out


def view(db: Session, *, auto_accounts=None) -> dict:
    from skas_algo.services.portfolio_history import current_views

    d = defaults(db)
    rules = _rules(db)
    recs = {v["id"]: v for v in current_views(db)}
    rows = []
    for h in db.execute(select(PortfolioHolding).order_by(PortfolioHolding.name)).scalars().all():
        if not eligible(h):
            continue
        r = rules.get(h.id)
        rule = rule_for(h, r, d)
        state = LadderState(r.peak if r else None, int(r.levels_fired or 0) if r else 0)
        nxt = next_trigger(state, rule)
        px = ladder_price(h)
        ccy = currency_of(h)
        cls = str(h.asset_class or "").lower()
        rows.append({
            "holding_id": h.id, "name": h.name, "asset_class": h.asset_class,
            "group": CLASS_GROUPS.get(cls, cls), "currency": ccy,
            "position": _position(h, recs.get(h.id), ccy),
            "sync_source": h.sync_source, "symbol": h.sync_ref,
            "broker_account_id": h.broker_account_id,
            "mode": mode_of(h, r, auto_accounts),
            "dip_pct": r.dip_pct if r else None, "amount": r.amount if r else None,
            "max_levels": r.max_levels if r else None,
            "rule": {"dip_pct": rule.dip_pct, "amount": rule.amount,
                     "max_levels": rule.max_levels},
            "peak": state.peak, "peak_asof": r.peak_asof if r else None,
            "peak_source": r.peak_source if r else None,
            "levels_fired": state.levels_fired, "price": px or None,
            "price_asof": h.price_asof,
            "drawdown_pct": (round((px / state.peak - 1.0) * 100.0, 2)
                             if state.peak and px else None),
            "next": ({"level": nxt.level, "trigger_price": nxt.trigger_price,
                      "amount": nxt.amount} if nxt else None),
        })
    hs = db.execute(select(PortfolioHolding)).scalars().all()
    names = {h.id: h.name for h in hs}
    ccys = {h.id: currency_of(h) for h in hs}
    sugg = db.execute(select(PortfolioBidsSuggestion).order_by(
        PortfolioBidsSuggestion.created_on.desc(), PortfolioBidsSuggestion.id.desc())
    ).scalars().all()
    return {
        "defaults": d,
        "rows": rows,
        "pending": [_sdict(s, names, ccys) for s in sugg if s.status == "pending"],
        "recent": [_sdict(s, names, ccys) for s in sugg if s.status != "pending"][:30],
    }


def _sdict(s: PortfolioBidsSuggestion, names: dict, ccys: dict | None = None) -> dict:
    return {"id": s.id, "holding_id": s.holding_id, "holding": names.get(s.holding_id),
            "currency": (ccys or {}).get(s.holding_id, "INR"),
            "level": s.level, "peak": s.peak, "trigger_price": s.trigger_price,
            "price": s.price, "amount": s.amount,
            "units_hint": round(s.amount / s.price, 4) if s.price else None,
            "created_on": s.created_on, "status": s.status, "txn_id": s.txn_id,
            "resolved_at": s.resolved_at.isoformat() if s.resolved_at else None}
