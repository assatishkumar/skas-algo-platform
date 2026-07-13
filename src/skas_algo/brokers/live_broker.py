"""LiveBroker — the platform's ONLY real-order execution path (Phase B, Zerodha first).

Implements the engine's fill contract ``execute(BrokerOrder) -> Fill`` so the entire
shared decision path (SliceExecutor, strategies, resolver) is untouched: a LIVE session
gets THIS injected instead of PaperBroker — and only when mode=="LIVE" AND the account is
armed AND SKAS_LIVE_TRADING_ENABLED is true AND the adapter has order methods
(live/manager._build_session; every other combination keeps PaperBroker).

Execution style (owner decision): LIMIT at touch — a SELL is placed at the current BID,
a BUY at the ASK — then polled ~2s up to ``live_order_timeout_s``; still pending →
modified to MARKET and polled to a terminal state. COMPLETE → Fill at the broker's
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
import threading
import time as _time
import uuid
from datetime import date, datetime

from skas_algo.db.enums import OrderType
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
        self.notifier = notifier or build_notifier()
        self._clock = clock or datetime
        self._orders_day: date | None = None
        self._orders_count = 0
        self._governor = governor_for(account_id)

    # ------------------------------------------------------------------ rails
    def _check_rails(self, order: BrokerOrder, ref_price: float | None) -> None:
        from skas_algo.live.holidays import is_nse_holiday

        now = self._clock.now()
        if (now.weekday() >= 5 or is_nse_holiday(now.date())
                or not ("09:15" <= now.strftime("%H:%M") <= "15:30")):
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
    def execute(self, order: BrokerOrder) -> Fill:
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
        self._governor.wait()
        try:
            broker_id = self.adapter.place_order(req)
        except Exception as exc:
            raise OrderExecutionError(f"order placement failed: {exc}") from exc
        self._orders_count += 1

        st = self._await_terminal(broker_id, deadline_s=self.order_timeout_s)
        if st["status"] not in _TERMINAL and req.order_type is OrderType.LIMIT:
            # Escalate: unfilled at the touch → take the market.
            try:
                self._governor.wait()
                self.adapter.modify_order(broker_id, order_type=OrderType.MARKET)
            except Exception as exc:  # pragma: no cover - modify raced a fill
                logger.warning("modify→MARKET failed for %s: %s", broker_id, exc)
            st = self._await_terminal(broker_id, deadline_s=self.order_timeout_s)

        if st["status"] == "COMPLETE":
            fill = Fill(order.symbol, order.side, st["filled_quantity"] or order.quantity,
                        st["average_price"], broker_order_id=broker_id)
            self._notify(AlertLevel.INFO, "Filled",
                         f"{order.side.value} {fill.quantity} {order.symbol} @ ₹{fill.price:.2f}")
            return fill

        filled = int(st.get("filled_quantity") or 0)
        if st["status"] not in _TERMINAL:
            # Still pending after escalation — cancel what's left, keep what filled.
            try:
                self._governor.wait()
                self.adapter.cancel_order(broker_id)
            except Exception:  # pragma: no cover - cancel raced a fill
                pass
            st = self._await_terminal(broker_id, deadline_s=5.0)
            filled = int(st.get("filled_quantity") or filled)
        if filled > 0:
            self._notify(AlertLevel.WARNING, "Partial fill",
                         f"{order.side.value} {filled}/{order.quantity} {order.symbol} "
                         f"@ ₹{st['average_price']:.2f} — remainder cancelled")
            return Fill(order.symbol, order.side, filled, st["average_price"],
                        broker_order_id=broker_id)
        detail = st.get("status_message") or st["status"]
        self._notify(AlertLevel.ERROR, "Order failed",
                     f"{order.side.value} {order.quantity} {order.symbol}: {detail}")
        raise OrderExecutionError(
            f"{order.side.value} {order.quantity} {order.symbol} → {detail}")

    def _await_terminal(self, broker_id: str, deadline_s: float) -> dict:
        deadline = _time.monotonic() + deadline_s
        st = {"status": "UNKNOWN", "average_price": 0.0, "filled_quantity": 0,
              "status_message": None}
        while _time.monotonic() < deadline:
            try:
                st = self.adapter.order_status(broker_id)
            except Exception:  # pragma: no cover - transient status hiccup
                pass
            if st["status"] in _TERMINAL:
                return st
            _time.sleep(self.poll_interval_s)
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
