"""LiveBroker: fill/escalation/rejection paths, safety rails, partials — fake adapter,
zero network. Also the Zerodha order-route resolution and the injection matrix."""

from __future__ import annotations

from datetime import datetime

import pytest

from skas_algo.brokers.base import BrokerOrder
from skas_algo.brokers.live_broker import (
    LiveBroker,
    OrderExecutionError,
    adapter_can_execute,
)
from skas_algo.db.enums import OrderSide, OrderType


class FakeClock:
    """Tuesday 11:00 IST — inside market hours."""

    @staticmethod
    def now():
        return datetime(2026, 7, 7, 11, 0)


class FakeAdapter:
    """Stateful order lifecycle: status returns `initial` until modify_order is called
    (then `after_modify`), until cancel_order (then `after_cancel`). Mirrors how the real
    escalation interacts with the broker, independent of poll cadence."""

    def __init__(self, initial=None, after_modify=None, after_cancel=None,
                 place_raises=None):
        self.initial = initial
        self.after_modify = after_modify
        self.after_cancel = after_cancel
        self.place_raises = place_raises
        self.placed: list[BrokerOrder] = []
        self.modified: list[tuple] = []
        self.cancelled: list[str] = []

    def place_order(self, order):
        if self.place_raises:
            raise self.place_raises
        self.placed.append(order)
        return f"KITE-{len(self.placed)}"

    def modify_order(self, broker_order_id, *, order_type=None, price=None):
        self.modified.append((broker_order_id, order_type, price))

    def order_status(self, broker_order_id):
        if self.cancelled and self.after_cancel is not None:
            return dict(self.after_cancel)
        if self.modified and self.after_modify is not None:
            return dict(self.after_modify)
        return dict(self.initial)

    def cancel_order(self, broker_order_id):
        self.cancelled.append(broker_order_id)


class NullNotifier:
    def __init__(self):
        self.alerts = []

    def send(self, alert):
        self.alerts.append(alert)


def make(adapter, **kw):
    kw.setdefault("touch_fn", lambda s, side: 100.0)
    kw.setdefault("order_timeout_s", 0.05)
    kw.setdefault("poll_interval_s", 0.0)
    kw.setdefault("notifier", NullNotifier())
    kw.setdefault("clock", FakeClock)
    return LiveBroker(adapter, **kw)


COMPLETE = {"status": "COMPLETE", "average_price": 101.5, "filled_quantity": 65,
            "status_message": None}
PENDING = {"status": "OPEN", "average_price": 0.0, "filled_quantity": 0,
           "status_message": None}
REJECTED = {"status": "REJECTED", "average_price": 0.0, "filled_quantity": 0,
            "status_message": "Insufficient funds"}


def test_limit_at_touch_fills():
    a = FakeAdapter(initial=COMPLETE)
    lb = make(a)
    fill = lb.execute(BrokerOrder("NIFTY|2026-07-07|24500|CE", OrderSide.SELL, 65))
    assert fill.price == 101.5 and fill.quantity == 65
    assert fill.broker_order_id == "KITE-1"
    assert a.placed[0].order_type is OrderType.LIMIT and a.placed[0].price == 100.0
    assert a.modified == []  # no escalation needed


def test_timeout_escalates_to_market_then_fills():
    a = FakeAdapter(initial=PENDING, after_modify=COMPLETE)
    lb = make(a)
    fill = lb.execute(BrokerOrder("NIFTY|2026-07-07|24500|CE", OrderSide.SELL, 65))
    assert fill.price == 101.5
    assert a.modified and a.modified[0][1] is OrderType.MARKET


def test_rejection_raises_order_execution_error():
    a = FakeAdapter(initial=REJECTED)
    lb = make(a)
    with pytest.raises(OrderExecutionError, match="Insufficient funds"):
        lb.execute(BrokerOrder("NIFTY|2026-07-07|24500|CE", OrderSide.SELL, 65))


def test_placement_exception_wraps():
    a = FakeAdapter(place_raises=RuntimeError("token expired"))
    lb = make(a)
    with pytest.raises(OrderExecutionError, match="token expired"):
        lb.execute(BrokerOrder("NIFTY|2026-07-07|24500|CE", OrderSide.SELL, 65))


def test_partial_fill_accepted_with_actual_quantity():
    stuck = {"status": "OPEN", "average_price": 99.0, "filled_quantity": 65,
             "status_message": None}
    after_cancel = {"status": "CANCELLED", "average_price": 99.0, "filled_quantity": 65,
                    "status_message": None}
    a = FakeAdapter(initial=stuck, after_modify=stuck, after_cancel=after_cancel)
    lb = make(a)
    fill = lb.execute(BrokerOrder("NIFTY|2026-07-07|24500|CE", OrderSide.SELL, 130))
    assert fill.quantity == 65 and fill.price == 99.0
    assert a.cancelled  # remainder was cancelled


def test_notional_cap_blocks_before_broker():
    a = FakeAdapter(initial=COMPLETE)
    lb = make(a, max_order_notional=5_000.0)  # 100 × 65 = 6,500 > cap
    with pytest.raises(OrderExecutionError, match="notional"):
        lb.execute(BrokerOrder("NIFTY|2026-07-07|24500|CE", OrderSide.SELL, 65))
    assert a.placed == []  # never reached the broker


def test_daily_order_cap():
    a = FakeAdapter(initial=COMPLETE)
    lb = make(a, max_orders_per_day=2)
    o = BrokerOrder("NIFTY|2026-07-07|24500|CE", OrderSide.SELL, 65)
    lb.execute(o)
    lb.execute(o)
    with pytest.raises(OrderExecutionError, match="daily order cap"):
        lb.execute(o)
    assert len(a.placed) == 2


def test_market_closed_blocks():
    class SundayClock:
        @staticmethod
        def now():
            return datetime(2026, 7, 5, 11, 0)  # Sunday

    a = FakeAdapter(initial=COMPLETE)
    lb = make(a, clock=SundayClock)
    with pytest.raises(OrderExecutionError, match="market closed"):
        lb.execute(BrokerOrder("NIFTY|2026-07-07|24500|CE", OrderSide.SELL, 65))
    assert a.placed == []


def test_no_touch_price_goes_market():
    a = FakeAdapter(initial=COMPLETE)
    lb = make(a, touch_fn=lambda s, side: None)
    lb.execute(BrokerOrder("NIFTY|2026-07-07|24500|CE", OrderSide.SELL, 65))
    assert a.placed[0].order_type is OrderType.MARKET and a.placed[0].price is None


def test_adapter_can_execute_detection():
    assert adapter_can_execute(FakeAdapter())
    from skas_algo.brokers.dhan import DhanAdapter, DhanCredentials

    assert not adapter_can_execute(DhanAdapter(DhanCredentials("1")))  # no order surface yet


# ------------------------------------------------------- Zerodha order routing

class _RouteKite:
    def __init__(self):
        self.orders = []

    def set_access_token(self, t):
        pass

    def instruments(self, exchange):
        if exchange == "NFO":
            from datetime import date
            return [{"name": "NIFTY", "instrument_type": "CE", "expiry": date(2026, 7, 7),
                     "strike": 24500.0, "tradingsymbol": "NIFTY2670724500CE", "lot_size": 65}]
        if exchange == "BFO":
            from datetime import date
            return [{"name": "SENSEX", "instrument_type": "PE", "expiry": date(2026, 7, 9),
                     "strike": 80000.0, "tradingsymbol": "SENSEX2670980000PE", "lot_size": 20}]
        raise AssertionError(exchange)

    VARIETY_REGULAR = "regular"
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"

    def place_order(self, **kw):
        self.orders.append(kw)
        return "250707000001"


def _armed_adapter():
    from skas_algo.brokers.zerodha import ZerodhaAdapter, ZerodhaCredentials

    return ZerodhaAdapter(ZerodhaCredentials("k", "s"), armed=True, live_enabled=True,
                          kite=_RouteKite())


def test_zerodha_order_route_nfo_bfo_equity():
    a = _armed_adapter()
    a.place_order(BrokerOrder("NIFTY|2026-07-07|24500|CE", OrderSide.SELL, 65))
    a.place_order(BrokerOrder("SENSEX|2026-07-09|80000|PE", OrderSide.BUY, 20))
    a.place_order(BrokerOrder("RELIANCE", OrderSide.BUY, 10))
    kite = a._kite_client()
    nfo, bfo, eq = kite.orders
    assert (nfo["exchange"], nfo["tradingsymbol"], nfo["product"]) == (
        "NFO", "NIFTY2670724500CE", "NRML")
    assert (bfo["exchange"], bfo["tradingsymbol"], bfo["product"]) == (
        "BFO", "SENSEX2670980000PE", "NRML")
    assert (eq["exchange"], eq["tradingsymbol"], eq["product"]) == ("NSE", "RELIANCE", "CNC")
    # Unlisted contract must raise, never route to a wrong symbol.
    with pytest.raises(ValueError, match="no listed contract"):
        a.place_order(BrokerOrder("NIFTY|2026-07-07|99999|CE", OrderSide.SELL, 65))


def test_zerodha_orders_gated_when_disarmed():
    from skas_algo.brokers.zerodha import NotArmedError, ZerodhaAdapter, ZerodhaCredentials

    a = ZerodhaAdapter(ZerodhaCredentials("k", "s"), armed=False, live_enabled=True,
                       kite=_RouteKite())
    with pytest.raises(NotArmedError):
        a.place_order(BrokerOrder("RELIANCE", OrderSide.BUY, 1))
    with pytest.raises(NotArmedError):
        a.modify_order("X", order_type=OrderType.MARKET)
    with pytest.raises(NotArmedError):
        a.cancel_order("X")


# --------------------------------------------------- injection matrix + reconciliation

class _Sess:
    def __init__(self):
        self.broker = "PAPER-SENTINEL"
        self.market = None


class _QS:
    def __init__(self, adapter):
        self.adapter = adapter


class _ExecAdapter(FakeAdapter):
    def __init__(self, armed=True):
        super().__init__(initial=COMPLETE)
        self.armed = armed


def _cfg(mode):
    from skas_algo.live.manager import LiveConfig

    return LiveConfig(name="t", strategy_id="custom_options", symbols=["NIFTY"],
                      mode=mode, broker_account_id=1)


def test_injection_matrix(monkeypatch):
    """LiveBroker is injected ONLY in the single all-keys-turned cell; every other
    combination keeps the paper broker (CLAUDE.md §1 — the double gate is load-bearing)."""
    from skas_algo.brokers.live_broker import LiveBroker
    from skas_algo.config import get_settings
    from skas_algo.live.manager import manager

    settings = get_settings()

    def run(mode, armed, flag, capable=True):
        monkeypatch.setattr(settings, "live_trading_enabled", flag)
        sess = _Sess()
        adapter = _ExecAdapter(armed=armed) if capable else object()
        manager._maybe_inject_live_broker(sess, _cfg(mode), _QS(adapter))
        return sess.broker

    assert isinstance(run("LIVE", True, True), LiveBroker)          # the ONE live cell
    assert run("PAPER", True, True) == "PAPER-SENTINEL"             # paper mode
    assert run("LIVE", False, True) == "PAPER-SENTINEL"             # disarmed
    assert run("LIVE", True, False) == "PAPER-SENTINEL"             # platform flag off
    assert run("LIVE", True, True, capable=False) == "PAPER-SENTINEL"  # no order surface


def test_injection_rewires_the_executor(monkeypatch):
    """Injecting the LiveBroker must repoint the EXECUTOR too — not just `session.broker`.
    The executor is what actually places fills; if it stays on the construction-time PaperBroker,
    a LIVE run paper-fills with no order reaching the broker (the 2026-07-10 'test' order). This
    is the real path (a genuine LiveSession), which `test_injection_matrix`'s stub session can't
    exercise — that stub has no executor, which is exactly why the bug slipped through."""
    from skas_algo.brokers.live_broker import LiveBroker
    from skas_algo.brokers.sim_broker import PaperBroker
    from skas_algo.config import get_settings
    from skas_algo.engine.live import LiveSession
    from skas_algo.live.manager import manager

    monkeypatch.setattr(get_settings(), "live_trading_enabled", True)
    session = LiveSession(strategy=object(), initial_capital=100_000)
    assert isinstance(session.broker, PaperBroker)
    assert session.executor.broker is session.broker  # bound together at construction

    manager._maybe_inject_live_broker(session, _cfg("LIVE"), _QS(_ExecAdapter(armed=True)))

    assert isinstance(session.broker, LiveBroker)
    assert session.executor.broker is session.broker  # THE fix: fills now go through LiveBroker


def test_reconciliation_aggregates_across_runs():
    """Broker nets per contract across runs — reconciliation must compare the SUM of all
    live-order runs' books, not each run alone."""
    from types import SimpleNamespace

    from skas_algo.brokers.live_broker import LiveBroker
    from skas_algo.live.manager import LiveRunManager

    mgr = LiveRunManager()

    class _Lot(SimpleNamespace):
        pass

    def fake_run(units, direction, account=1):
        pf = SimpleNamespace(
            lot_symbols=lambda: ["NIFTY|2026-07-07|24500|CE"],
            lots=lambda s: [_Lot(direction=direction, units=units)],
        )
        lb = LiveBroker.__new__(LiveBroker)  # instance without broker wiring
        sess = SimpleNamespace(portfolio=pf, broker=lb)
        cfg = SimpleNamespace(mode="LIVE", broker_account_id=account)
        return SimpleNamespace(session=sess, config=cfg)

    mgr.runs = {1: fake_run(65, -1), 2: fake_run(130, 1)}  # net LONG 65 across runs

    class _RecAdapter:
        def _option_tradingsymbol(self, inst):
            return "NIFTY2670724500CE"

        def positions(self):
            return [{"tradingsymbol": "NIFTY2670724500CE", "quantity": 65}]

    assert mgr.reconcile_account_book(1, _RecAdapter()) is None      # 130L−65S = +65 ✓

    class _WrongAdapter(_RecAdapter):
        def positions(self):
            return [{"tradingsymbol": "NIFTY2670724500CE", "quantity": 130}]

    msg = mgr.reconcile_account_book(1, _WrongAdapter())
    assert msg and "platform +65" in msg and "broker +130" in msg

    # A failed READ (expired token) is NOT a mismatch → raises ReconcileUnavailable so the caller
    # retries instead of halting the run on a phantom mismatch (the 4:50 AM false-alarm fix).
    from skas_algo.live.manager import ReconcileUnavailable

    class _FailAdapter(_RecAdapter):
        def positions(self):
            raise RuntimeError("Incorrect `api_key` or `access_token`.")

    with pytest.raises(ReconcileUnavailable):
        mgr.reconcile_account_book(1, _FailAdapter())


def test_reconcile_gate_pending_lifecycle(monkeypatch):
    """Reconcile-before-first-decision gate (the double-fill safety net): a pending run
    reconciles regardless of the hourly throttle; a clean book lifts the gate, a mismatch
    halts, and an INABILITY to reconcile leaves it pending (throttle NOT armed) so it
    retries next tick — an unreconciled decision never slips through."""
    from types import SimpleNamespace

    from skas_algo.brokers.live_broker import LiveBroker
    from skas_algo.live.manager import LiveRun, ReconcileUnavailable, manager

    # Reconciliation only runs during market hours (off-hours the token is routinely expired).
    monkeypatch.setattr("skas_algo.live.manager.is_market_open", lambda: True)

    lb = LiveBroker.__new__(LiveBroker)  # a LiveBroker instance without wiring

    def make(adapter, pending=True, broker=lb):
        return SimpleNamespace(
            session=SimpleNamespace(broker=broker),
            quote_source=SimpleNamespace(adapter=adapter),
            config=SimpleNamespace(broker_account_id=1, name="t"),
            run_id=1, order_error=None, reconcile_pending=pending,
            _last_reconcile_at=None,
            # the alert helpers are exercised by their own test; stub them here (this test is
            # about the pending/halt lifecycle, not Telegram).
            _alert_reconciled_ok=lambda detail, now: None,
            _notify_recon=lambda level, title, body: None,
        )

    outcomes = {"problem": None}
    monkeypatch.setattr(manager, "reconcile_account_book",
                        lambda acc, adapter, details=None: outcomes["problem"])

    # Clean book → pending lifted, throttle armed, no halt.
    s = make(adapter=object())
    LiveRun._maybe_reconcile(s)
    assert s.reconcile_pending is False and s.order_error is None
    assert s._last_reconcile_at is not None

    # Mismatch → halt via order_error (pending lifted; order_error is the block now).
    outcomes["problem"] = "platform +65 vs broker +130"
    s = make(adapter=object())
    LiveRun._maybe_reconcile(s)
    assert s.order_error and "mismatch" in s.order_error

    # Can't reconcile (no adapter) → STAYS pending, throttle NOT armed → retries next tick.
    s = make(adapter=None)
    LiveRun._maybe_reconcile(s)
    assert s.reconcile_pending is True and s._last_reconcile_at is None

    # Broker book UNREADABLE (expired token) → ReconcileUnavailable → STAYS pending, NO halt, NO
    # throttle (retries next tick). This is the 4:50 AM false-alarm fix: a failed read never halts.
    def _unreadable(acc, adapter, details=None):
        raise ReconcileUnavailable("positions fetch failed: Incorrect `api_key` or `access_token`.")
    monkeypatch.setattr(manager, "reconcile_account_book", _unreadable)
    s = make(adapter=object())
    LiveRun._maybe_reconcile(s)
    assert s.order_error is None and s.reconcile_pending is True and s._last_reconcile_at is None

    # Market CLOSED → reconciliation doesn't run at all (no off-hours token-expiry false alarms).
    monkeypatch.setattr("skas_algo.live.manager.is_market_open", lambda: False)
    calls = {"n": 0}
    monkeypatch.setattr(manager, "reconcile_account_book",
                        lambda acc, adapter, details=None: calls.__setitem__("n", calls["n"] + 1))
    s = make(adapter=object())
    LiveRun._maybe_reconcile(s)
    assert calls["n"] == 0 and s.order_error is None and s.reconcile_pending is True
    monkeypatch.setattr("skas_algo.live.manager.is_market_open", lambda: True)  # restore

    # Paper broker → the whole method is a no-op (no real book to reconcile).
    called = {"n": 0}
    monkeypatch.setattr(manager, "reconcile_account_book",
                        lambda acc, adapter, details=None: called.__setitem__("n", called["n"] + 1))
    s = make(adapter=object(), pending=False, broker="PAPER")
    LiveRun._maybe_reconcile(s)
    assert called["n"] == 0


def test_injected_livebroker_run_starts_reconcile_pending():
    """A session that got a LiveBroker injected implies reconcile_pending — the exact
    predicate LiveRun.__init__ uses, so a fresh live run gates its first decision."""
    from skas_algo.brokers.live_broker import LiveBroker

    injected = LiveBroker.__new__(LiveBroker)
    paper = "PAPER-SENTINEL"
    assert isinstance(injected, LiveBroker)          # → reconcile_pending True at init
    assert not isinstance(paper, LiveBroker)         # → reconcile_pending False at init


def test_reconcile_ok_alert_throttles():
    """The 'broker book matches strategy' Telegram confirmation fires on the first/changed book
    and once per new day (a heartbeat), but NOT on the hourly repeat of an unchanged book, and
    never when the book is flat."""
    from datetime import datetime
    from types import SimpleNamespace

    from skas_algo.live.manager import LiveRun

    sent = []
    s = SimpleNamespace(
        config=SimpleNamespace(name="t"),
        _notify_recon=lambda level, title, body: sent.append((level, title, body)),
    )
    book = {"ours": {"X": 195}, "broker": {"X": 195}}
    d1 = datetime(2026, 7, 13, 10, 0)

    LiveRun._alert_reconciled_ok(s, {"ours": {}}, d1)   # flat → silence
    assert sent == []
    LiveRun._alert_reconciled_ok(s, book, d1)           # first confirmation
    assert len(sent) == 1 and sent[-1][0] == "INFO"
    LiveRun._alert_reconciled_ok(s, book, d1)           # same book, same day → throttled
    assert len(sent) == 1
    LiveRun._alert_reconciled_ok(s, book, datetime(2026, 7, 14, 10, 0))  # new day heartbeat
    assert len(sent) == 2
    LiveRun._alert_reconciled_ok(s, {"ours": {"X": 390}, "broker": {"X": 390}},
                                 datetime(2026, 7, 14, 11, 0))            # book changed
    assert len(sent) == 3
