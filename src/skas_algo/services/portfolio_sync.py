"""Refresh the auto-tracked /portfolio holdings from their price sources.

Two sources exist, and they behave nothing alike:

* **broker** — Zerodha or Dhan. A ``holdings()`` call gives real UNITS and average cost, a
  ``day_quotes()`` call gives the last price and the previous close. Both are live to the
  second during market hours.
* **amfi** — mutual funds, matched by ISIN. One NAV per day, published after the close, so
  during a trading session the newest NAV is YESTERDAY'S. Every fund therefore carries the
  date its price is as of, and the UI shows it; nothing here pretends a NAV is live.
* **global** — US equities and crypto, which no Indian broker quotes. Prices arrive in USD
  and are converted here, at both ends of the day change so the currency's move is counted
  too (see data/global_quotes).

Rules that hold across both:

**Units are the broker's, cost basis is not.** When a holding has a transaction ledger the
sync updates PRICE only — the ledger is the owner's record of what was paid and when, and a
broker's average cost silently folds in corporate actions and pre-migration history. Units are
still compared, and a mismatch is REPORTED rather than quietly written over: only the owner
can say whether the ledger or the broker is missing a row.

**A sync never invents.** A symbol the broker doesn't hold, a NAV that isn't published, a
missing prior file — each leaves the holding untouched and lands in ``issues``. Stale is
visible; wrong is not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from skas_algo.db.models import BrokerAccount, PortfolioHolding, PortfolioTransaction
from skas_algo.services.portfolio import build_ledger

logger = logging.getLogger(__name__)

# A units gap this small is float noise from a fractional MF allotment, not a missing trade.
_UNITS_EPSILON = 1e-4


@dataclass
class SyncReport:
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    discovered: list[dict] = field(default_factory=list)
    synced_at: str = ""

    def as_dict(self) -> dict:
        return {
            "updated": self.updated,
            "unchanged": self.unchanged,
            "issues": self.issues,
            "discovered": self.discovered,
            "synced_at": self.synced_at,
            "counts": {
                "updated": len(self.updated),
                "issues": len(self.issues),
                "discovered": len(self.discovered),
            },
        }


def _ledger_units(db: Session, holding_id: int) -> float | None:
    rows = db.execute(
        select(PortfolioTransaction).where(PortfolioTransaction.holding_id == holding_id)
    ).scalars().all()
    if not rows:
        return None
    return build_ledger([
        {"on_date": r.on_date, "kind": r.kind, "units": r.units, "price": r.price,
         "fees": r.fees, "id": r.id}
        for r in rows
    ]).units


def _apply_price(
    h: PortfolioHolding, *, last: float, prev_close: float | None, asof: str, units: float | None
) -> None:
    """Write a fresh price onto a holding, deriving value and the day change from the units
    we are confident about. ``units`` None means we have no unit count — value is left alone
    because price alone cannot produce one."""
    h.last_price = last
    h.price_asof = asof
    h.last_synced_at = datetime.now(UTC)
    if units is None or units <= 0:
        return
    h.value = round(units * last, 2)
    h.day_change = round(units * (last - prev_close), 2) if prev_close else 0.0


def _sync_amfi(db: Session, holdings: list[PortfolioHolding], report: SyncReport) -> None:
    from skas_algo.data import amfi

    try:
        latest, previous = amfi.load()
    except Exception as exc:  # pragma: no cover - network guard
        logger.warning("AMFI load failed", exc_info=True)
        for h in holdings:
            report.issues.append({"holding": h.name, "reason": f"AMFI feed unavailable: {exc}"})
        return
    if not latest:
        for h in holdings:
            report.issues.append({"holding": h.name, "reason": "No AMFI NAV data cached yet"})
        return

    for h in holdings:
        isin = (h.sync_ref or "").strip().upper()
        row = latest.get(isin)
        if row is None:
            report.issues.append({
                "holding": h.name,
                "reason": f"ISIN {isin or '(blank)'} not found in today's AMFI NAV file",
            })
            continue
        units = _ledger_units(db, h.id)
        if units is None:
            units = h.units
        prev = previous.get(isin)
        _apply_price(
            h,
            last=row.nav,
            prev_close=prev.nav if prev else None,
            asof=row.as_of.isoformat(),
            units=units,
        )
        if prev is None:
            report.issues.append({
                "holding": h.name,
                "reason": "No earlier NAV cached — day change unknown until tomorrow",
                "severity": "info",
            })
        report.updated.append(h.name)


def _sync_global(db: Session, holdings: list[PortfolioHolding], report: SyncReport) -> None:
    """US equities and crypto. One FX read serves every USD holding on the pass."""
    from skas_algo.data import global_quotes as gq

    symbols = [(h.sync_ref or "").strip() for h in holdings]
    try:
        found, errors = gq.quotes([s for s in symbols if s])
    except Exception as exc:  # pragma: no cover - network guard
        logger.warning("global quote fetch failed", exc_info=True)
        for h in holdings:
            report.issues.append({"holding": h.name, "reason": f"quote feed unavailable: {exc}"})
        return

    # Fetched once, and ONLY if something actually needs converting.
    fx = None
    if any(q.currency not in ("INR", "") for q in found.values()):
        try:
            fx = gq.usd_inr()
        except Exception as exc:
            logger.warning("USD/INR fetch failed", exc_info=True)
            report.issues.append({"holding": "USD/INR", "reason": f"rate unavailable: {exc}"})

    today = date.today().isoformat()
    for h in holdings:
        sym = (h.sync_ref or "").strip()
        quote = found.get(sym)
        if quote is None:
            report.issues.append({
                "holding": h.name,
                "reason": f"No quote for {sym or '(blank)'}"
                          + (f" — {errors[sym]}" if sym in errors else ""),
            })
            continue
        try:
            price, prev = gq.in_inr(quote, fx)
        except gq.QuoteUnavailable as exc:
            report.issues.append({"holding": h.name, "reason": str(exc)})
            continue
        units = _ledger_units(db, h.id)
        if units is None:
            units = h.units
        _apply_price(h, last=price, prev_close=prev, asof=today, units=units)
        # Keep the quote as it was actually made. A US position is read in dollars, and
        # dividing the rupee figure back by today's rate would NOT recover it.
        h.native_currency = quote.currency or None
        h.native_price = quote.price
        report.updated.append(h.name)


def _slice_accounts(h: PortfolioHolding) -> set[int]:
    """Broker accounts whose UNITS this holding tracks, from its per-source breakdown."""
    out: set[int] = set()
    for key in (h.broker_units or {}):
        if key.startswith("account:"):
            try:
                out.add(int(key.split(":", 1)[1]))
            except ValueError:  # a malformed key is data, not a crash
                continue
    return out


def _sync_broker(
    db: Session, account: BrokerAccount, holdings: list[PortfolioHolding], report: SyncReport
) -> None:
    """One account's pass. An account plays TWO independent roles here and they must not be
    conflated: it may be the holding's QUOTE source (``broker_account_id``, exactly one per
    holding), and it may hold some of its UNITS (a slice in ``broker_units``, any number).

    A merged position is priced by Zerodha while part of it sits at Dhan — group only by the
    quote account and Dhan's slice never refreshes, which is precisely the daily update this
    exists to provide."""
    from skas_algo.services.broker import make_adapter

    prices_here = [h for h in holdings if h.broker_account_id == account.id]
    holds_here = [h for h in holdings if account.id in _slice_accounts(h)]

    try:
        adapter = make_adapter(account)
        book = adapter.holdings()
    except Exception as exc:
        logger.warning("broker holdings read failed for account %s", account.id, exc_info=True)
        for h in holdings:
            report.issues.append({"holding": h.name, "reason": f"{account.broker}: {exc}"})
        return

    # --- units first: only the slice this account owns, then re-total.
    key = f"account:{account.id}"
    for h in holds_here:
        sym = (h.sync_ref or "").strip().upper()
        held = book.get(sym)
        # A successful read that omits the symbol means zero held HERE — a real sell-out.
        updated = dict(h.broker_units or {})
        updated[key] = float(held["units"]) if held else 0.0
        h.broker_units = updated
        h.units = round(sum(float(v) for v in updated.values()), 4)
        if h not in prices_here:
            report.updated.append(h.name)

    if not prices_here:
        return

    # --- then prices, for the holdings this account is the quote source for.
    symbols = [(h.sync_ref or "").strip().upper() for h in prices_here]
    quotes: dict[str, dict] = {}
    fetch = getattr(adapter, "day_quotes", None)
    if fetch is not None and symbols:
        try:
            quotes = fetch([s for s in symbols if s])
        except Exception:  # LTP-only fallback below; day change is then simply unknown
            logger.warning("day_quotes failed for account %s", account.id, exc_info=True)
    if not quotes and symbols:
        try:
            quotes = {
                s: {"last": p, "prev_close": None}
                for s, p in adapter.get_quote([s for s in symbols if s]).items()
            }
        except Exception as exc:
            for h in prices_here:
                report.issues.append({"holding": h.name, "reason": f"quote failed: {exc}"})
            return

    today = date.today().isoformat()
    for h in prices_here:
        sym = (h.sync_ref or "").strip().upper()
        quote = quotes.get(sym)
        if quote is None or not quote.get("last"):
            report.issues.append({"holding": h.name, "reason": f"No quote for {sym}"})
            continue

        # Units already settled above for a sliced holding; locked ones are never touched.
        if h.broker_units or h.units_locked:
            _apply_price(
                h, last=float(quote["last"]), prev_close=quote.get("prev_close"),
                asof=today, units=h.units,
            )
            report.updated.append(h.name)
            continue

        held = book.get(sym)
        ledger_units = _ledger_units(db, h.id)
        broker_units = float(held["units"]) if held else None
        units = ledger_units if ledger_units is not None else broker_units

        # The ledger is the owner's record; the broker is the broker's. Where they disagree,
        # say so and change nothing — a silent overwrite of either would destroy the only
        # evidence of which one is missing a row.
        if ledger_units is not None and broker_units is not None:
            if abs(ledger_units - broker_units) > _UNITS_EPSILON:
                report.issues.append({
                    "holding": h.name,
                    "reason": (
                        f"Ledger says {ledger_units:g} units, {account.broker} says "
                        f"{broker_units:g} — a transaction is missing on one side"
                    ),
                    "severity": "warning",
                })
        elif ledger_units is None and broker_units is not None:
            h.units = broker_units
            units = broker_units
            if held.get("avg_price") and not h.invested:
                h.invested = round(broker_units * float(held["avg_price"]), 2)
        elif broker_units is None:
            report.issues.append({
                "holding": h.name,
                "reason": f"{sym} is not in the {account.broker} holdings book",
                "severity": "warning",
            })

        _apply_price(
            h, last=float(quote["last"]), prev_close=quote.get("prev_close"),
            asof=today, units=units,
        )
        report.updated.append(h.name)

    # Anything the broker holds that isn't tracked here — offered, never auto-added.
    tracked = {(h.sync_ref or "").strip().upper() for h in holdings}
    for sym, row in book.items():
        if sym not in tracked:
            report.discovered.append({
                "symbol": sym,
                "units": row.get("units"),
                "avg_price": row.get("avg_price"),
                "broker": account.broker,
                "broker_account_id": account.id,
            })


def sync_portfolio(db: Session, *, holding_ids: list[int] | None = None) -> SyncReport:
    """Refresh every auto holding (or just ``holding_ids``). One call per source, never per
    holding — Dhan's market-data endpoints are rate-gated per account and a 20-symbol loop
    would trip the throttle for everything else running on the box."""
    report = SyncReport(synced_at=datetime.now(UTC).isoformat())
    stmt = select(PortfolioHolding).where(PortfolioHolding.sync == "auto")
    if holding_ids:
        stmt = stmt.where(PortfolioHolding.id.in_(holding_ids))
    rows = db.execute(stmt).scalars().all()

    by_amfi = [h for h in rows if (h.sync_source or "") == "amfi"]
    by_global = [h for h in rows if (h.sync_source or "") == "global"]
    by_broker: dict[int, list[PortfolioHolding]] = {}
    for h in rows:
        if (h.sync_source or "") == "broker":
            # A holding is visited by its QUOTE account and by every account holding a slice
            # of its units — those are different roles and often different accounts.
            for account_id in ({h.broker_account_id} | _slice_accounts(h)) - {None}:
                by_broker.setdefault(account_id, []).append(h)
            if not h.broker_account_id and not _slice_accounts(h):
                report.issues.append({
                    "holding": h.name, "reason": "Broker-priced but no account is set",
                })
        elif (h.sync_source or "") not in ("amfi", "broker", "global"):
            report.issues.append({
                "holding": h.name,
                "reason": "Marked auto but has no price source — set one, or make it manual",
            })

    if by_amfi:
        _sync_amfi(db, by_amfi, report)
    if by_global:
        _sync_global(db, by_global, report)
    for account_id, group in by_broker.items():
        account = db.get(BrokerAccount, account_id)
        if account is None:
            for h in group:
                report.issues.append({"holding": h.name, "reason": "Broker account is gone"})
            continue
        _sync_broker(db, account, group, report)

    db.commit()
    return report
