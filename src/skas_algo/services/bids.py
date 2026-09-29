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
import math

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
    # which asset classes BIDS runs on at all (owner 2026-09-29: "exclude all the Indian
    # stocks for now … just ETF, Mutual Funds, US Stocks and Crypto"). Excluding a class
    # expires its pending suggestions; re-including one REJOINS its holdings at that day's
    # price (the joining rule) instead of firing every level the ladder missed meanwhile.
    "classes": ["etf", "mf", "us", "btc"],
}
CLASS_GROUPS = {"us": "US stocks", "mf": "Mutual funds", "etf": "ETFs", "stk": "Stocks",
                "btc": "Crypto"}
ELIGIBLE_CLASSES = ("stk", "etf", "mf", "us", "btc")    # what CAN be included
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
    before = defaults(db)
    merged = {**before, **{k: v for k, v in values.items() if k in DEFAULTS}}
    merged["classes"] = [c for c in ELIGIBLE_CLASSES if c in set(merged.get("classes") or [])]
    row.value = merged
    old, new = set(before["classes"]), set(merged["classes"])
    rules = _rules(db)
    for h in db.execute(select(PortfolioHolding)).scalars().all():
        cls = str(h.asset_class or "").lower()
        if cls in old - new:
            _expire_pending(db, h.id)
        elif cls in new - old and h.id in rules:
            r = rules[h.id]                  # rejoin at the next check's price
            r.peak, r.peak_asof, r.peak_source = None, None, None
            r.levels_fired, r.last_eval_asof = 0, None
    db.commit()
    return merged


def eligible(h: PortfolioHolding, classes=None) -> bool:
    """A class BIDS can run on AND — when ``classes`` is given — one the owner included."""
    cls = str(h.asset_class or "").lower()
    return cls in ELIGIBLE_CLASSES and (classes is None or cls in classes)


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


# ------------------------------------------------------------------ the fund (owner 2026-09-29)
# Every RUPEE buy BIDS suggests is funded by selling the fund-source ETF (LIQUIDCASE…): the
# suggestion names the units to sell, Accept records the buy AND that sale in the ledger (the
# owner places both orders — this module never does), and a fund that cannot cover what is
# pending is flagged, never used to drop a dip. Dollar buys are NOT funded (the owner pays
# them from US broker cash). The fund holding itself is never a BIDS holding: watching the
# fund for dips would suggest buying the fund with the fund.
def fund_holding(db: Session, d: dict | None = None) -> PortfolioHolding | None:
    name = str((d or defaults(db)).get("fund_source") or "").strip().upper()
    if not name:
        return None
    for h in db.execute(select(PortfolioHolding)).scalars().all():
        if name in (str(h.sync_ref or "").strip().upper(), str(h.name or "").strip().upper()):
            return h
    return None


def _held_units(db: Session, h: PortfolioHolding) -> float:
    """Units held now — the ledger's count when one exists (§8a), else the typed units."""
    rows = db.execute(select(PortfolioTransaction).where(
        PortfolioTransaction.holding_id == h.id)).scalars().all()
    if not rows:
        return float(h.units or 0.0)
    held = 0.0
    for t in rows:
        k = str(t.kind or "").lower()
        if k in ("buy", "bonus"):
            held += float(t.units or 0.0)
        elif k == "sell":
            held -= float(t.units or 0.0)
    return max(held, 0.0)


def fund_units_for(amount: float, fund_price: float) -> int:
    """Whole ETF units to sell to raise ``amount`` — rounded UP, an ETF trades in units."""
    if fund_price <= 0 or amount <= 0:
        return 0
    return int(math.ceil(amount / fund_price - 1e-9))


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
    latest price. Returns {evaluated, new: [suggestion dicts], resets}.

    Every call evaluates — there is deliberately NO once-per-price-date latch. One existed
    until 2026-09-28 and it swallowed real dips: the 09:30 pass joined SOUTHBANK at ₹49.95
    and stamped the date, a reprice the same morning moved it to ₹48.94 (through its ₹48.95
    L1) with the SAME price date, and nothing re-checked it — the 16:00 close of an Indian
    stock carries the same date as its 09:30 print, so the close was never evaluated at all.
    Re-evaluating is safe because the ladder is idempotent: ``levels_fired`` is persisted, so
    the same price fires nothing twice. ``last_eval_asof`` now only records when it last ran."""
    today = today or date.today()
    d = defaults(db)
    out = {"evaluated": 0, "new": [], "resets": 0}
    if not d.get("enabled", True):
        return out
    rules = _rules(db)
    included = set(d["classes"])
    fund = fund_holding(db, d)
    for h in db.execute(select(PortfolioHolding)).scalars().all():
        px = ladder_price(h)
        if fund is not None and h.id == fund.id:
            _expire_pending(db, h.id)        # the fund is spent, never bought
            continue
        if eligible(h) and not eligible(h, included):
            _expire_pending(db, h.id)        # a class the owner left out suggests nothing
            continue
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
        rule = rule_for(h, r, d)
        res = evaluate(LadderState(r.peak, int(r.levels_fired or 0)), px, rule)
        out["evaluated"] += 1
        if res.reset or res.state.peak != r.peak:   # a new high: the ladder resets
            if res.reset:
                out["resets"] += 1
            _expire_pending(db, h.id)
            if res.state.peak != r.peak:
                r.peak = res.state.peak
                r.peak_asof = asof
                r.peak_source = "high"
        for t in res.triggers:
            peak_asof = r.peak_asof or asof
            # (holding, peak date, level) is unique: a second ladder the same day (a new
            # high then a fresh dip) meets the row the first one left EXPIRED — revive it
            # rather than let the unique key abort the whole pass. A row the owner resolved
            # (accepted / skipped) is theirs and is never reopened.
            s = db.execute(select(PortfolioBidsSuggestion).where(
                PortfolioBidsSuggestion.holding_id == h.id,
                PortfolioBidsSuggestion.peak_asof == peak_asof,
                PortfolioBidsSuggestion.level == t.level)).scalars().first()
            if s is not None and s.status != "expired":
                continue
            if s is None:
                s = PortfolioBidsSuggestion(holding_id=h.id, peak_asof=peak_asof, level=t.level)
                db.add(s)
            s.peak, s.trigger_price, s.price, s.amount = r.peak, t.trigger_price, px, t.amount
            s.created_on, s.status, s.resolved_at, s.txn_id = asof, "pending", None, None
            out["new"].append({"holding": h.name, "level": t.level, "amount": t.amount,
                               "trigger_price": t.trigger_price, "price": px,
                               "currency": currency_of(h)})
        r.levels_fired = res.state.levels_fired
        r.last_eval_asof = asof
    db.commit()
    if notify and out["new"]:
        _notify(out["new"], _fund_line(db, d))
    return out


def _sym(ccy: str | None) -> str:
    return {"INR": "₹", "USD": "$"}.get(str(ccy or "INR").upper(), f"{ccy} ")


def _fund_line(db: Session, d: dict) -> str:
    """One line on the fund for the alert: what the pending rupee levels need vs what it holds."""
    try:
        f = view(db)["fund"]
    except Exception:  # pragma: no cover - the alert never fails over its footnote
        return ""
    if not f or not f.get("found"):
        return f"Fund {d.get('fund_source')} is not on the portfolio — nothing to sell."
    if f["short"] > 0:
        return (f"Fund {f['name']} is SHORT by ₹{f['short']:,.0f}: pending needs "
                f"₹{f['need']:,.0f}, it holds ₹{f['value']:,.0f}.")
    return f"Funded from {f['name']} (₹{f['value']:,.0f} held, ₹{f['need']:,.0f} pending)."


def _notify(new: list[dict], fund_line: str = "") -> None:
    try:
        from skas_algo.notify import Alert, AlertLevel, build_notifier

        lines = [f"{n['holding']} · L{n['level']} · {_sym(n.get('currency'))}{n['amount']:,.0f} "
                 f"(at {n['price']:,.2f}, trigger {n['trigger_price']:,.2f})" for n in new[:12]]
        more = f"\n… +{len(new) - 12} more" if len(new) > 12 else ""
        build_notifier().send(Alert(
            f"BIDS · {len(new)} dip level(s) to review",
            "\n".join(lines) + more + (f"\n{fund_line}" if fund_line else "")
            + "\nAccept or skip them on Portfolio → BIDS.",
            AlertLevel.INFO))
    except Exception:  # pragma: no cover - a failed push never loses a suggestion
        logger.exception("bids: notification failed")


# ------------------------------------------------------------------ accept / skip
def accept(db: Session, sid: int, *, units: float, price: float, on_date: date,
           fees: float = 0.0, fund: bool = True, fund_units: float | None = None,
           fund_price: float | None = None) -> dict:
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
    # the fund sale, decided and validated BEFORE anything is written — a refused sale must
    # not leave the buy recorded without it
    sale = None
    if fund and ccy == "INR":
        f = fund_holding(db)
        if f is not None and f.id != h.id:
            fp = float(fund_price or f.last_price or 0.0)
            fu = float(fund_units if fund_units is not None
                       else fund_units_for(float(units) * float(price), fp))
            if fu > 0:
                if fp <= 0:
                    raise ValueError(f"{f.name} has no price to record the sale at")
                held = _held_units(db, f)
                if fu > held + 1e-9:
                    raise ValueError(f"{f.name} holds {held:,.3f} units — cannot record a sale "
                                     f"of {fu:,.3f}. Untick the fund sale or top the fund up.")
                sale = (f, fu, fp)
    carry_typed_position(db, h, before=on_date.isoformat())
    row = PortfolioTransaction(
        holding_id=s.holding_id, on_date=on_date.isoformat(), kind="buy", units=float(units),
        price=round(float(price) * fx, 4), fees=float(fees or 0.0), note=note)
    db.add(row)
    db.flush()
    fund_txn = None
    if sale is not None:
        f, fu, fp = sale
        carry_typed_position(db, f, before=on_date.isoformat())
        fund_txn = PortfolioTransaction(
            holding_id=f.id, on_date=on_date.isoformat(), kind="sell", units=fu, price=fp,
            fees=0.0, note=f"{BIDS_NOTE} fund · {h.name} L{s.level}")
        db.add(fund_txn)
        db.flush()
    s.status = "accepted"
    s.resolved_at = datetime.now(UTC)
    s.txn_id = row.id
    db.commit()
    return {"id": s.id, "status": s.status, "txn_id": row.id,
            "fund_txn_id": fund_txn.id if fund_txn is not None else None}


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
    included = set(d["classes"])
    fund = fund_holding(db, d)
    counts: dict[str, int] = {}
    for h in db.execute(select(PortfolioHolding).order_by(PortfolioHolding.name)).scalars().all():
        if not eligible(h) or (fund is not None and h.id == fund.id):
            continue
        cls = str(h.asset_class or "").lower()
        counts[cls] = counts.get(cls, 0) + 1
        if cls not in included:
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
    shown = {h.id for h in hs if eligible(h, included) and (fund is None or h.id != fund.id)}
    sugg = db.execute(select(PortfolioBidsSuggestion).order_by(
        PortfolioBidsSuggestion.created_on.desc(), PortfolioBidsSuggestion.id.desc())
    ).scalars().all()
    pending = [_sdict(s, names, ccys) for s in sugg
               if s.status == "pending" and s.holding_id in shown]
    fund_view = _fund_view(db, d, fund, recs, pending)
    return {
        "defaults": d,
        # every class BIDS can run on, with how many holdings each has and whether it is in
        "classes": [{"key": c, "label": CLASS_GROUPS[c], "count": counts.get(c, 0),
                     "included": c in included} for c in ELIGIBLE_CLASSES],
        "rows": rows,
        # an excluded class's pending rows are expired at the next check; never shown before
        "pending": pending,
        "fund": fund_view,
        "recent": [_sdict(s, names, ccys) for s in sugg if s.status != "pending"][:30],
    }


def _fund_view(db: Session, d: dict, fund: PortfolioHolding | None, recs: dict,
               pending: list[dict]) -> dict:
    """The fund against what is pending: each RUPEE suggestion gets the units to sell and,
    walking oldest first, how much of it the fund cannot cover (``fund_short``). Dollar
    suggestions are not funded (``fund_units`` None)."""
    name = str(d.get("fund_source") or "")
    if fund is None:
        for p in pending:
            p["fund_units"], p["fund_short"] = None, 0.0
        return {"found": False, "name": name}
    price = float(fund.last_price or 0.0)
    units = _held_units(db, fund)
    value = units * price
    left, need = value, 0.0
    for p in sorted(pending, key=lambda x: (x["created_on"] or "", x["id"])):
        if str(p["currency"]).upper() != "INR":
            p["fund_units"], p["fund_short"] = None, 0.0
            continue
        need += p["amount"]
        p["fund_units"] = fund_units_for(p["amount"], price)
        p["fund_short"] = round(max(p["amount"] - max(left, 0.0), 0.0), 2)
        left -= p["amount"]
    return {"found": True, "name": fund.name, "holding_id": fund.id, "units": units,
            "price": price or None, "value": round(value, 2), "need": round(need, 2),
            "short": round(max(need - value, 0.0), 2)}


def _sdict(s: PortfolioBidsSuggestion, names: dict, ccys: dict | None = None) -> dict:
    return {"id": s.id, "holding_id": s.holding_id, "holding": names.get(s.holding_id),
            "currency": (ccys or {}).get(s.holding_id, "INR"),
            "level": s.level, "peak": s.peak, "trigger_price": s.trigger_price,
            "price": s.price, "amount": s.amount,
            "units_hint": round(s.amount / s.price, 4) if s.price else None,
            "created_on": s.created_on, "status": s.status, "txn_id": s.txn_id,
            "resolved_at": s.resolved_at.isoformat() if s.resolved_at else None}
