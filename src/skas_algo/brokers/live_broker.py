"""LiveBroker — the platform's ONLY real-order execution path (Phase B, Zerodha first).

Implements the engine's fill contract ``execute(BrokerOrder) -> Fill`` so the entire
shared decision path (SliceExecutor, strategies, resolver) is untouched: a LIVE session
gets THIS injected instead of PaperBroker — and only when mode=="LIVE" AND the account is
armed AND SKAS_LIVE_TRADING_ENABLED is true AND the adapter has order methods
(live/manager._build_session; every other combination keeps PaperBroker).

Execution style (owner decision): LIMIT at touch — a SELL is placed at the current BID,
a BUY at the ASK — then polled ~2s up to ``live_order_timeout_s``; still pending → walked
up an escalation LADDER of protected crossing limits (touch re-read each rung) until it
fills or the ladder is exhausted. COMPLETE → Fill at the broker's
average price. REJECTED / CANCELLED / stuck → best-effort cancel, then
``OrderExecutionError`` — the live loop catches it, sets the run's ``order_error`` halt
(no further decisions until acknowledged), and notifies.

Safety rails (before the broker ever sees the order):
  * market-hours check (NSE 09:15–15:30; the engine never decides off-hours anyway —
    this is defense in depth);
  * per-order notional cap (``live_max_order_notional``);
  * per-run daily order counter (``live_max_orders_per_day``);
  * an ACCOUNT-level rate governor shared by all runs on the same account (~5 orders/s;
    Kite caps at 10/s) so two strategies deciding simultaneously queue, not error.

Partial fills ≥ 1 unit at timeout are accepted as a Fill with the ACTUAL quantity (the
engine books what really happened); the shortfall is the strategy's next decision's
problem, and a WARNING notification flags it.
"""

from __future__ import annotations

import logging
import math
import threading
import time as _time
import uuid
from datetime import date, datetime

from skas_algo.db.enums import OrderSide, OrderType
from skas_algo.notify import Alert, AlertLevel, build_notifier

from .base import BrokerOrder, Fill

logger = logging.getLogger("skas_algo.live")

_TERMINAL = {"COMPLETE", "REJECTED", "CANCELLED"}


class OrderExecutionError(RuntimeError):
    """A real order failed (rejected / cancelled / unfillable) — the run must halt."""


class _RateGovernor:
    def __init__(self, min_interval_s: float = 0.25):
        self.min_interval_s = min_interval_s
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        # Reserve this call's slot under the lock, then sleep OUTSIDE it. Sleeping while
        # holding the lock serialized every waiter behind a full ~0.25s each; now the lock
        # is held only for the arithmetic, so N simultaneous entries on one account still
        # pace ~1/0.25s at the broker but don't stack their sleeps into seconds of latency.
        with self._lock:
            now = _time.monotonic()
            scheduled = max(now, self._last + self.min_interval_s)
            self._last = scheduled
        delay = scheduled - _time.monotonic()
        if delay > 0:
            _time.sleep(delay)


# One governor per broker account, shared across every LiveBroker in the process —
# simultaneous entries from multiple deployments queue instead of tripping rate limits.
_governors: dict[int, _RateGovernor] = {}
_governors_lock = threading.Lock()


def governor_for(account_id: int | None) -> _RateGovernor:
    with _governors_lock:
        key = int(account_id or 0)
        if key not in _governors:
            _governors[key] = _RateGovernor()
        return _governors[key]


class LiveBroker:
    """Real-order broker satisfying the SimBroker ``execute`` contract."""

    def __init__(
        self,
        adapter,
        *,
        account_id: int | None = None,
        run_name: str = "",
        touch_fn=None,                      # fn(symbol, side) -> limit price | None
        max_order_notional: float = 500_000.0,
        max_orders_per_day: int = 20,
        order_timeout_s: float = 10.0,
        poll_interval_s: float = 2.0,
        protect_pct: float = 3.0,           # OPTIONS: first rung crosses the touch by this %
        # EQUITY crossings are an order of magnitude tighter, because equity spreads are
        # basis points where option spreads are percents — one constant cannot serve both
        # (3% of a ₹14 premium is 43 paise; 3% of a ₹2,300 stock is ₹69). The crossing is a
        # CEILING, not a price: a marketable limit fills at the ask, so 1% only binds when
        # the book has run away since the decision — and an equity entry that stale is one
        # the owner would rather skip than chase (owner call, 2026-08-24).
        protect_pct_equity: float = 1.0,
        protect_ladder: tuple[float, ...] | None = None,  # explicit override — BOTH segments
        # NSE per-order quantity freeze, {UNDERLYING: units} (settings.freeze_quantities()).
        # An order above it is placed as consecutive children of at most that size — see
        # execute(). None/{} = never split (every pre-2026-09 caller and the tests).
        freeze_qty: dict[str, int] | None = None,
        notifier=None,
        clock=None,                          # injectable for tests (datetime-like)
    ):
        self.adapter = adapter
        self.account_id = account_id
        self.run_name = run_name
        self.touch_fn = touch_fn
        self.max_order_notional = float(max_order_notional)
        self.max_orders_per_day = int(max_orders_per_day)
        self.order_timeout_s = float(order_timeout_s)
        self.poll_interval_s = float(poll_interval_s)
        self.protect_pct = float(protect_pct)
        self.freeze_qty = {str(k).upper(): int(v) for k, v in (freeze_qty or {}).items()}
        # Escalation ladder. ONE 3% rung is far too thin for a near-the-money option on
        # expiry day: on 2026-08-11 the 24500 PE ran 31.30 → 41.75 (+33%) during the ~20s
        # run 10's square-off was resting, so a limit 3% through a touch read seconds
        # earlier never stood a chance — it cancelled, halted the run, and left a short
        # leg to be closed by hand. Each rung RE-READS the touch and crosses it further.
        # Derived from protect_pct so the single knob still steers the whole ladder, and
        # so an explicit protect_pct=0 still means "no crossing".
        self.protect_pct_equity = float(protect_pct_equity)

        def _derive(base: float) -> tuple[float, ...]:
            return (base, base * 2.7, base * 6.7)

        # Two ladders, ONE shape: the rung multipliers are shared, only the base % differs
        # by segment. An explicit `protect_ladder` overrides both — the operator escape
        # hatch, and the only way to get a non-proportional ladder.
        self.protect_ladder: tuple[float, ...] = tuple(
            float(x) for x in (protect_ladder if protect_ladder is not None
                               else _derive(protect_pct))
        )
        self.protect_ladder_equity: tuple[float, ...] = tuple(
            float(x) for x in (protect_ladder if protect_ladder is not None
                               else _derive(self.protect_pct_equity))
        )
        self.notifier = notifier or build_notifier()
        self._clock = clock or datetime
        self._orders_day: date | None = None
        self._orders_count = 0
        self._governor = governor_for(account_id)

    def rebind_adapter(self, adapter) -> None:
        """Swap to a freshly-built adapter (same account, CURRENT Kite token).

        The broker freezes ``self.adapter`` at injection; the daily ~06:00 Kite token
        rollover rebuilds the QUOTE adapter but used to leave this one holding the dead
        token — reads reconciled green all day while the first real order was rejected
        with "Incorrect api_key or access_token" (the 2026-07-24 hni_weekly halt). The
        manager rebinds through the same armed+order-surface keys as injection; nothing
        else moves (rails, caps and the rate governor are per-account, not per-adapter).
        """
        self.adapter = adapter

    # ------------------------------------------------------------------ trace
    def _trace(self, cid: str, event: str, **fields) -> None:
        """One greppable line per order-lifecycle event, keyed by a single id.

        The order path used to log ONLY its escalation — three statements, all inside one
        branch — so reconstructing what happened to a real order meant reading ``execute()``
        and inferring which branch ran. On 2026-08-24 that produced a confidently wrong
        answer: the absence of an ``escalating`` line was read as "we never cancelled", when
        in fact an equity MARKET order had skipped the ladder entirely and gone straight to
        cancel. The broker had to correct us. Absence of a log is not evidence; every branch
        now says what it did.

        Deliberately at INFO and one line per event (status is logged on CHANGE, not per
        poll), so a whole order costs ~5 lines. Grep an order's life with its cid, or the
        whole day's real orders with "ORDER ".
        """
        kv = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None and v != "")
        logger.info("ORDER %-9s cid=%s %s", event, cid, kv)

    # ------------------------------------------------------------------ rails
    def _check_rails(self, order: BrokerOrder, ref_price: float | None) -> None:
        # ONE definition of the session window, shared with the live loop. This used to be a
        # second inline copy of the weekday/holiday/09:15-15:30 triplet — and that is exactly
        # how it went stale: SEBI's Closing Auction Session (2026-08-03) pushed index F&O to
        # 15:40 while this rail still refused every real order after 15:30, so any exit or
        # manual flatten in that window raised → the run halted holding an open position.
        # Segment is per-ORDER (an option leg and an equity leg can be on the same account).
        from skas_algo.engine.options.instrument import is_option_symbol
        from skas_algo.live.quotes import is_market_open

        now = self._clock.now()
        segment = "DERIV" if is_option_symbol(order.symbol) else "EQUITY"
        if not is_market_open(now, segment=segment):
            raise OrderExecutionError("market closed — refusing to place a real order")
        today = now.date()
        if self._orders_day != today:
            self._orders_day, self._orders_count = today, 0
        if self._orders_count >= self.max_orders_per_day:
            raise OrderExecutionError(
                f"daily order cap hit ({self.max_orders_per_day}) — run halted; "
                "raise SKAS_LIVE_MAX_ORDERS_PER_DAY if this was intended"
            )
        if ref_price and ref_price > 0:
            notional = float(ref_price) * float(order.quantity)
            if notional > self.max_order_notional:
                raise OrderExecutionError(
                    f"order notional ₹{notional:,.0f} exceeds the "
                    f"₹{self.max_order_notional:,.0f} cap (SKAS_LIVE_MAX_ORDER_NOTIONAL)"
                )

    # ---------------------------------------------------------------- execute
    def _freeze_for(self, symbol: str) -> int | None:
        """NSE's per-order quantity freeze for this contract's underlying, or None for
        anything that is not an index option (equity has no such cap)."""
        from skas_algo.engine.options.instrument import is_option_symbol as _is_opt

        if not self.freeze_qty or not _is_opt(symbol):
            return None
        cap = self.freeze_qty.get(str(symbol).split("|")[0].upper())
        return int(cap) if cap and cap > 0 else None

    def execute(self, order: BrokerOrder) -> Fill:
        """Place ``order``; when it is larger than the exchange's per-order freeze, place
        it as consecutive children of at most that size and return ONE combined fill.

        The freeze is an exchange control, per ORDER — the account can hold far more, it
        just cannot establish it in one instruction, and a 20 lot-set BANKNIFTY body
        (1,200 units against a 600 freeze, 2026-09) would otherwise be rejected on the
        first real order and halt the run. Children run the full single-order path each
        (rails, touch, ladder, trace), in sequence, so a fill is booked before the next
        child goes out. A child that fails AFTER earlier children filled is a partial —
        the earlier fills are real and are returned, never raised away; only a first
        child failing raises. Same semantics the single-order partial path already has."""
        cap = self._freeze_for(order.symbol)
        qty = int(order.quantity)
        if cap is None or qty <= cap:
            return self._execute_one(order)
        parts: list[int] = []
        left = qty
        while left > 0:
            parts.append(min(cap, left))
            left -= parts[-1]
        gid = uuid.uuid4().hex[:16]
        self._trace(gid, "split", symbol=order.symbol, side=order.side.value, qty=qty,
                    freeze=cap, children=len(parts))
        fills: list[Fill] = []
        for i, n in enumerate(parts):
            child = BrokerOrder(symbol=order.symbol, side=order.side, quantity=n,
                                order_type=order.order_type, price=order.price,
                                reduce_only=bool(getattr(order, "reduce_only", False)))
            try:
                fills.append(self._execute_one(child))
            except OrderExecutionError as exc:
                if not fills:
                    raise
                self._trace(gid, "splitpartial", child=f"{i + 1}/{len(parts)}",
                            filled=f"{sum(f.quantity for f in fills)}/{qty}",
                            error=str(exc)[:200])
                self._notify(AlertLevel.WARNING, "Partial fill (split order)",
                             f"{order.side.value} {sum(f.quantity for f in fills)}/{qty} "
                             f"{order.symbol}: child {i + 1} failed — {exc}")
                break
        fq = sum(f.quantity for f in fills)
        avg = sum(f.quantity * f.price for f in fills) / fq
        return Fill(order.symbol, order.side, fq, avg, broker_order_id=fills[0].broker_order_id)

    def _execute_one(self, order: BrokerOrder) -> Fill:
        touch = None
        if self.touch_fn is not None:
            try:
                touch = self.touch_fn(order.symbol, order.side)
            except Exception:  # pragma: no cover - no book → market order below
                touch = None
        self._check_rails(order, touch)

        client_id = uuid.uuid4().hex[:16]
        req = BrokerOrder(
            symbol=order.symbol, side=order.side, quantity=order.quantity,
            order_type=OrderType.LIMIT if touch else OrderType.MARKET,
            price=float(touch) if touch else None,
            client_order_id=client_id, tag=client_id,
        )
        started = _time.monotonic()
        # WHY the order looks the way it does — a MARKET here means touch_fn had no price,
        # which also disables the escalation below. That was invisible until 2026-08-24.
        self._trace(client_id, "place", symbol=order.symbol, side=order.side.value,
                    qty=order.quantity, type=req.order_type.value,
                    price=f"{touch:.2f}" if touch else "none",
                    touch="book" if touch else "MISSING",
                    reduce_only=bool(getattr(order, "reduce_only", False)))
        self._governor.wait()
        try:
            broker_id = self.adapter.place_order(req)
        except Exception as exc:
            self._trace(client_id, "rejected", error=str(exc)[:300])
            raise OrderExecutionError(f"order placement failed: {exc}") from exc
        self._orders_count += 1
        self._trace(client_id, "accepted", broker_id=broker_id,
                    orders_today=self._orders_count)

        st = self._await_terminal(broker_id, deadline_s=self.order_timeout_s,
                                  cid=client_id, phase="initial")
        if st["status"] not in _TERMINAL and req.order_type is not OrderType.LIMIT:
            # The branch that silently ate the 2026-08-24 smoke test: no touch → MARKET →
            # no ladder. Say so, rather than leaving a 10-second hole in the log.
            self._trace(client_id, "noescal", reason="order is MARKET (touch_fn gave no "
                        "price) — the escalation ladder only applies to LIMIT orders")
        if st["status"] not in _TERMINAL and req.order_type is OrderType.LIMIT:
            # Escalate: unfilled at the touch → re-price to a PROTECTED crossing LIMIT
            # (touch RE-READ each rung, pushed further THROUGH the spread). NOT a MARKET
            # modify: Zerodha rejects naked MARKET orders on options via API ("Market
            # orders without market protection are not allowed") — on 2026-07-27 that
            # rejection left run 10's PE square-off resting at a stale touch until it
            # CANCELLED and halted the run. A limit through the touch is the same thing a
            # market order with protection does, and is accepted.
            #
            # The LADDER (2026-08-11): a single rung fails whenever the option is moving
            # faster than the crossing is wide, which is the norm for a near-the-money leg
            # on expiry day — the very leg you most need out of. Later rungs are shorter
            # (the market is clearly running) so the whole ladder still fits inside roughly
            # the old two-step budget.
            # An EXIT walks the whole ladder — not being flat is the expensive outcome.
            # An ENTRY gets the single original rung: there a bad fill is worse than no
            # fill, and chasing 20% through the touch to OPEN a position would be a new
            # risk introduced by the fix for a different one.
            # Segment per ORDER, exactly as _check_rails picks its session window — an
            # option leg and an equity leg can share an account.
            from skas_algo.engine.options.instrument import is_option_symbol as _is_opt

            full = (self.protect_ladder if _is_opt(order.symbol)
                    else self.protect_ladder_equity)
            ladder = full if getattr(order, "reduce_only", False) else full[:1]
            for i, pct in enumerate(ladder):
                fresh = None
                if self.touch_fn is not None:
                    try:
                        fresh = self.touch_fn(order.symbol, order.side)
                    except Exception:  # pragma: no cover - no book → fall back below
                        fresh = None
                base = float(fresh or touch or 0.0)
                want = self._protected_price(base, order.side, pct=pct) if base > 0 else None
                try:
                    self._governor.wait()
                    if base > 0:
                        self._trace(client_id, "escalate", rung=f"{i + 1}/{len(ladder)}",
                                    pct=f"{pct:.1f}%", touch=f"{base:.2f}",
                                    limit=f"{want:.2f}")
                        self.adapter.modify_order(
                            broker_id, order_type=OrderType.LIMIT, price=want)
                    else:
                        # no price basis at all (book vanished) — MARKET is the only lever
                        # left; equities accept it, and an option order always had a touch.
                        self._trace(client_id, "escalate", rung=f"{i + 1}/{len(ladder)}",
                                    to="MARKET", reason="no price basis")
                        self.adapter.modify_order(broker_id, order_type=OrderType.MARKET)
                except Exception as exc:  # pragma: no cover - modify raced a fill
                    self._trace(client_id, "modifyerr", error=str(exc)[:300])
                    logger.warning("escalation modify failed for %s: %s", broker_id, exc)
                # first rung keeps the full timeout (unchanged behaviour); the rest are
                # halved — if it has not filled by now, waiting longer only drifts further.
                st = self._await_terminal(
                    broker_id,
                    deadline_s=self.order_timeout_s if i == 0 else self.order_timeout_s / 2,
                    cid=client_id, phase=f"rung{i + 1}")
                # Did the re-price actually land? A modify that is silently ignored looks
                # exactly like one that filled nothing, and on 2026-08-11 we could not tell
                # the two apart after the fact. Say so in the log while the order is live.
                got = float(st.get("price") or 0.0)
                if want and got and abs(got - want) > 0.011:
                    logger.warning(
                        "escalation did NOT take effect on %s: asked %.2f, broker still "
                        "shows %.2f — the re-price was ignored or rejected",
                        broker_id, want, got)
                if st["status"] in _TERMINAL:
                    break
                if base <= 0:
                    break     # MARKET modify already sent; more rungs cannot add anything

        if st["status"] == "COMPLETE":
            fill = Fill(order.symbol, order.side, st["filled_quantity"] or order.quantity,
                        st["average_price"], broker_order_id=broker_id)
            self._trace(client_id, "filled", qty=fill.quantity, avg=f"{fill.price:.2f}",
                        elapsed=f"{_time.monotonic() - started:.1f}s")
            self._notify(AlertLevel.INFO, "Filled",
                         f"{order.side.value} {fill.quantity} {order.symbol} @ ₹{fill.price:.2f}")
            return fill

        filled = int(st.get("filled_quantity") or 0)
        if st["status"] not in _TERMINAL:
            # Still pending after escalation — cancel what's left, keep what filled.
            # THIS is the cancel Dhan reported as "initiated from your end" on 2026-08-24,
            # and nothing in the log said we had sent it.
            self._trace(client_id, "cancel", reason="unfilled after escalation",
                        status=st["status"], filled=f"{filled}/{order.quantity}")
            try:
                self._governor.wait()
                self.adapter.cancel_order(broker_id)
            except Exception as exc:  # pragma: no cover - cancel raced a fill
                self._trace(client_id, "cancelerr", error=str(exc)[:300])
            st = self._await_terminal(broker_id, deadline_s=5.0,
                                      cid=client_id, phase="post-cancel")
            filled = int(st.get("filled_quantity") or filled)
        if filled > 0:
            self._trace(client_id, "partial", qty=f"{filled}/{order.quantity}",
                        avg=f"{st['average_price']:.2f}",
                        elapsed=f"{_time.monotonic() - started:.1f}s")
            self._notify(AlertLevel.WARNING, "Partial fill",
                         f"{order.side.value} {filled}/{order.quantity} {order.symbol} "
                         f"@ ₹{st['average_price']:.2f} — remainder cancelled")
            return Fill(order.symbol, order.side, filled, st["average_price"],
                        broker_order_id=broker_id)
        detail = st.get("status_message") or st["status"]
        self._trace(client_id, "failed", broker_id=broker_id, status=st["status"],
                    detail=detail, filled=f"{filled}/{order.quantity}",
                    elapsed=f"{_time.monotonic() - started:.1f}s")
        self._notify(AlertLevel.ERROR, "Order failed",
                     f"{order.side.value} {order.quantity} {order.symbol}: {detail}")
        raise OrderExecutionError(
            f"{order.side.value} {order.quantity} {order.symbol} → {detail}")

    def _protected_price(self, touch: float, side, pct: float | None = None) -> float:
        """The escalation limit: cross the touch by ``pct`` (default ``protect_pct``) —
        BUY pays up, SELL gives way — snapped OUTWARD to the ₹0.05 tick so the price
        stays marketable."""
        frac = (self.protect_pct if pct is None else pct) / 100.0
        mult = 1 + frac if side is OrderSide.BUY else 1 - frac
        raw = touch * mult
        ticks = raw / 0.05
        snapped = (math.ceil(ticks) if side is OrderSide.BUY else math.floor(ticks)) * 0.05
        return max(0.05, round(snapped, 2))

    def _await_terminal(self, broker_id: str, deadline_s: float, *,
                        cid: str = "-", phase: str = "") -> dict:
        """Poll until terminal or the deadline. Logs each status CHANGE (not each poll — a
        10s window is ~5 polls and a quiet order would otherwise drown the log), plus any
        status-read failure, which used to be swallowed entirely: a broker whose status
        endpoint was erroring looked identical to one reporting a healthy pending order."""
        deadline = _time.monotonic() + deadline_s
        st = {"status": "UNKNOWN", "average_price": 0.0, "filled_quantity": 0,
              "status_message": None}
        seen: str | None = None
        while _time.monotonic() < deadline:
            try:
                st = self.adapter.order_status(broker_id)
            except Exception as exc:  # pragma: no cover - transient status hiccup
                self._trace(cid, "statuserr", phase=phase, error=str(exc)[:200])
            if st["status"] != seen:
                seen = st["status"]
                self._trace(cid, "status", phase=phase, value=seen,
                            filled=st.get("filled_quantity"),
                            msg=st.get("status_message"))
            if st["status"] in _TERMINAL:
                return st
            _time.sleep(self.poll_interval_s)
        self._trace(cid, "timeout", phase=phase, after=f"{deadline_s:.0f}s",
                    status=st["status"])
        return st

    def _notify(self, level, title: str, message: str) -> None:
        try:
            prefix = f"[{self.run_name}] " if self.run_name else ""
            self.notifier.send(Alert(f"{prefix}{title}", message, level))
        except Exception:  # pragma: no cover - notification must never break execution
            logger.exception("order notification failed")


def adapter_can_execute(adapter) -> bool:
    """Does this adapter expose the full real-order surface LiveBroker needs?"""
    return all(hasattr(adapter, m)
               for m in ("place_order", "modify_order", "order_status", "cancel_order"))
