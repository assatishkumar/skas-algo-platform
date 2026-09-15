"""Kite's basket margin for a console book — the REAL figure, priced on today's chain.

The console's own margin is `options_console.margin.span_like`, a SPAN-shaped scan that gets
the ORDER of structures right but not the rupees: on the owner's iron fly (2026-09-10) it
read ₹74,779 against Zerodha's calculator's ₹90,828 (SPAN 28,435 + exposure 62,393 −
premium 20,105), because a real SPAN scan is 16 scenarios with a volatility-of-volatility
term and calendar-spread charges that a ±6% × ±25% grid does not carry. There is no way
to replicate SPAN honestly (CLAUDE.md §8d: a plausible-looking wrong number is worse than
a labelled rough one), so this asks the one calculator that is right — Kite's basket
margin API, the same call the Live tile and `/trade/options/margin` use — read-only, on
any logged-in Zerodha account (`manager._kite_reference_margin` precedent).

A replayed book cannot be priced as-is: its contracts EXPIRED years ago. So the book is
mapped to TODAY'S EQUIVALENT structure — the same moneyness and the same days to expiry
on the chain Kite lists now — and the answer is labelled exactly that:

* strike → `round(K × spot_today / spot_replay)` snapped to the listing grid, so a strike
  2% OTM in 2024 at 22,000 is priced 2% OTM today (a point offset would make it ATM);
* expiry → the listed expiry whose DTE is nearest the leg's DTE at the cursor, distinct
  replay expiries mapped to distinct listed ones so a calendar stays a calendar;
* quantity → the leg's LOTS × today's lot size (units would be a 2024 lot size).

Margin is a function of moneyness, DTE, vol and structure, not of the calendar year, so
this is the closest available figure — closer than any model — and still an estimate
across a vol regime (`margin_note` carries the mapped legs so the screen can say what was
priced). A live/paper console passes its own strikes and spot, so the mapping is the
identity there and the figure is exact. Never on an order path; a failure returns None and
the caller falls back to the model. Sits OUTSIDE `services/options_console/` because that
package may not import the broker layer (pinned by `tests/test_options_console.py`).
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta

from skas_algo.db.base import session_scope
from skas_algo.engine.options.instrument import make

logger = logging.getLogger(__name__)

# The exchange's listing grid — Kite lists NIFTY in 50s; the console's NIFTY-100s rule is
# a SELECTION rule (§8), and a replayed 100-strike stays on a 100 through the rounding.
_LISTING_STEP = {"NIFTY": 50.0, "BANKNIFTY": 100.0, "SENSEX": 100.0, "FINNIFTY": 50.0}
CACHE_TTL = timedelta(minutes=15)   # a basket's margin barely moves inside a quarter hour
SPOT_TTL = timedelta(seconds=60)

_LOCK = threading.Lock()
_CACHE: dict[tuple, tuple[datetime, dict]] = {}
_SPOT: dict[str, tuple[datetime, float, list[str]]] = {}
# One adapter per account TOKEN: `make_adapter` mints a fresh adapter every call and each
# one re-downloads the instruments dump (~1.5 s), which made every leg click wait 2 s.
# Keyed on the token so the daily ~06:00 rollover mints a new one (the order-adapter
# drift rule, manager `_maybe_remint_order_adapter`) instead of riding a dead session.
_ADAPTERS: dict[tuple[int, str], object] = {}


def _account():
    """Any logged-in Zerodha account (label, adapter), or None. Read-only use."""
    from skas_algo.services import broker as broker_svc

    with session_scope() as db:
        for a in broker_svc.list_accounts(db):
            if "zerodha" in str(a.broker).lower() and broker_svc.has_valid_session(a):
                key = (int(a.id), str(a.session_token))
                with _LOCK:
                    adapter = _ADAPTERS.get(key)
                    if adapter is None:
                        _ADAPTERS.clear()
                        adapter = _ADAPTERS[key] = broker_svc.make_adapter(a)
                return str(a.label), adapter
    return None


def _today_chain(underlying: str, adapter) -> tuple[float, list[str]] | None:
    now = datetime.now()
    hit = _SPOT.get(underlying)
    if hit and now - hit[0] < SPOT_TTL:
        return hit[1], hit[2]
    spot = adapter.underlying_ltp(underlying)
    expiries = adapter.option_expiries(underlying)
    if not spot or not expiries:
        return None
    _SPOT[underlying] = (now, float(spot), list(expiries))
    return float(spot), list(expiries)


def map_legs(legs: list[dict], *, underlying: str, spot_replay: float, spot_today: float,
             day: date, today: date, expiries_today: list[str]) -> list[dict]:
    """Today's equivalent of a replayed book — pure, so it is testable without a broker.
    ``legs``: [{right, strike, expiry, side, lots}]."""
    step = _LISTING_STEP.get(underlying.upper(), 100.0)
    # A book built on the console's 100-grid stays on it (a 100-step fly mapped to 50s came
    # out 23,050 / 23,800 around 23,400 — an asymmetry the replay never had); only a book
    # that itself holds a 50-strike is mapped on the 50s.
    if all(float(leg["strike"]) % 100 == 0 for leg in legs):
        step = max(step, 100.0)
    scale = spot_today / spot_replay if spot_replay > 0 else 1.0
    listed = sorted(expiries_today)
    # distinct replay expiries → distinct listed expiries, nearest DTE first
    replay_exp = sorted({leg["expiry"] for leg in legs})
    chosen: dict[str, str] = {}
    free = list(listed)
    for e in replay_exp:
        dte = (date.fromisoformat(e) - day).days
        pool = free or listed          # more replay expiries than listed ones: reuse
        best = min(pool, key=lambda x: abs((date.fromisoformat(x) - today).days - dte))
        chosen[e] = best
        if best in free:
            free.remove(best)
    out = []
    for leg in legs:
        k = round(float(leg["strike"]) * scale / step) * step
        out.append({"right": leg["right"], "strike": float(k), "expiry": chosen[leg["expiry"]],
                    "side": leg["side"], "lots": int(leg["lots"])})
    return out


def kite_equivalent(underlying: str, legs: list[dict], *, spot: float, day: date,
                    today: date | None = None) -> dict | None:
    """Kite's net basket margin for today's equivalent of ``legs`` at ``spot`` on ``day``.
    Returns ``{"total", "account", "spot_today", "legs", "shifted"}`` or None."""
    shorts = [leg for leg in legs if leg["side"] == "S" and leg["lots"] > 0]
    if not shorts or spot <= 0:
        return None
    try:
        acct = _account()
    except Exception:  # pragma: no cover - a DB hiccup must not break the console
        logger.exception("console margin: account lookup failed")
        return None
    if acct is None:
        return None
    label, adapter = acct
    try:
        chain = _today_chain(underlying, adapter)
    except Exception:
        logger.warning("console margin: today's chain unavailable", exc_info=True)
        return None
    if chain is None:
        return None
    spot_today, expiries = chain
    today = today or date.today()
    mapped = map_legs(legs, underlying=underlying, spot_replay=spot, spot_today=spot_today,
                      day=day, today=today, expiries_today=expiries)
    if not mapped:
        return None
    sig = (underlying, tuple(sorted((m["right"], m["strike"], m["expiry"], m["side"], m["lots"])
                                    for m in mapped)))
    now = datetime.now()
    with _LOCK:
        hit = _CACHE.get(sig)
        if hit and now - hit[0] < CACHE_TTL:
            return dict(hit[1])
    basket = []
    for m in mapped:
        inst = make(underlying, date.fromisoformat(m["expiry"]), m["strike"], m["right"])
        basket.append({"symbol": inst.symbol, "direction": 1 if m["side"] == "B" else -1,
                       "units": m["lots"] * inst.lot_size})
    try:
        total = adapter.basket_margin(basket)
    except Exception:
        logger.warning("console margin: basket call failed", exc_info=True)
        return None
    if total is None:
        return None
    shifted = day != today or any(
        abs(m["strike"] - float(leg["strike"])) > 1e-9 or m["expiry"] != leg["expiry"]
        for m, leg in zip(mapped, legs, strict=True))
    result = {"total": round(float(total), 2), "account": label, "spot_today": spot_today,
              "legs": mapped, "shifted": shifted}
    with _LOCK:
        _CACHE[sig] = (now, dict(result))
    return result


def warm(underlying: str) -> None:
    """Fetch the account, its instruments dump and today's chain in the background, so the
    first leg click does not wait the ~10 s a cold adapter takes after a restart."""
    def _run() -> None:
        try:
            acct = _account()
            if acct is not None:
                _today_chain(underlying, acct[1])
        except Exception:  # pragma: no cover - a warm-up may fail silently
            logger.debug("console margin: warm-up failed", exc_info=True)

    threading.Thread(target=_run, name="console-margin-warm", daemon=True).start()


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()
        _SPOT.clear()
        _ADAPTERS.clear()
