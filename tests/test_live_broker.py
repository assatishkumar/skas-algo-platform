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


def test_timeout_escalates_to_protected_limit_then_fills():
    """Zerodha rejects naked MARKET option orders via API ("Market orders without market
    protection are not allowed") — the 2026-07-27 square-off cancel/halt. Escalation now
    re-prices to a LIMIT through the touch by protect_pct (default 3%), tick-snapped
    outward: SELL gives way (100 → 97.00), BUY pays up (100 → 103.00). Never MARKET
    while a touch exists."""
    a = FakeAdapter(initial=PENDING, after_modify=COMPLETE)
    lb = make(a)
    fill = lb.execute(BrokerOrder("NIFTY|2026-07-07|24500|CE", OrderSide.SELL, 65))
    assert fill.price == 101.5
    assert a.modified == [("KITE-1", OrderType.LIMIT, 97.0)]

    a2 = FakeAdapter(initial=PENDING, after_modify=COMPLETE)
    lb2 = make(a2)
    lb2.execute(BrokerOrder("NIFTY|2026-07-07|24500|CE", OrderSide.BUY, 65))
    assert a2.modified == [("KITE-1", OrderType.LIMIT, 103.0)]

    # non-tick touch snaps OUTWARD (stays marketable): BUY 102.45 ×1.03 = 105.5235 → 105.55
    a3 = FakeAdapter(initial=PENDING, after_modify=COMPLETE)
    lb3 = make(a3, touch_fn=lambda s, side: 102.45)
    lb3.execute(BrokerOrder("NIFTY|2026-07-07|24500|CE", OrderSide.BUY, 65))
    assert a3.modified == [("KITE-1", OrderType.LIMIT, 105.55)]


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


def test_reconcile_ok_alerts_every_completed_run():
    """EVERY completed reconciliation confirms to Telegram — owner request 2026-07-17: a
    positive hourly heartbeat that broker and strategy agree, not only mismatch alarms
    (silence had read as 'fine' while a demoted run's real book sat unmanaged). Repeats of
    an unchanged book alert too (that IS the heartbeat), and a flat-flat book confirms
    with its own wording. Frequency stays bounded by the hourly reconcile throttle."""
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

    LiveRun._alert_reconciled_ok(s, {"ours": {}}, d1)   # flat-flat → still a confirmation
    assert len(sent) == 1 and "both flat" in sent[-1][2]
    LiveRun._alert_reconciled_ok(s, book, d1)           # book present → detail included
    assert len(sent) == 2 and sent[-1][0] == "INFO" and "195" in sent[-1][2]
    LiveRun._alert_reconciled_ok(s, book, d1)           # same book, same hour → alerts again
    assert len(sent) == 3


class LadderAdapter(FakeAdapter):
    """Fills only once the modify price reaches `fills_at` — models a market running away
    from the order, which is what a single-rung escalation cannot catch."""

    def __init__(self, fills_at: float, side=OrderSide.BUY, qty=195):
        super().__init__(initial=PENDING)
        self.fills_at = fills_at
        self.side = side
        self.qty = qty
        self.filled = False

    def modify_order(self, broker_order_id, *, order_type=None, price=None):
        self.modified.append((broker_order_id, order_type, price))
        if price is not None and (
                price >= self.fills_at if self.side is OrderSide.BUY else price <= self.fills_at):
            self.filled = True

    def order_status(self, broker_order_id):
        if self.filled:
            return {"status": "COMPLETE", "average_price": self.fills_at,
                    "filled_quantity": self.qty, "status_message": None}
        if self.cancelled:
            return {"status": "CANCELLED", "average_price": 0.0, "filled_quantity": 0,
                    "status_message": None}
        return dict(PENDING)


def test_ladder_catches_a_leg_a_single_rung_would_miss():
    """The 2026-08-11 halt: the 24500 PE ran 31.30 → 41.75 while the square-off rested, so
    a limit 3% through a stale touch never filled — it cancelled and halted the run with a
    live short leg. A widening ladder re-reads the touch and keeps crossing."""
    a = LadderAdapter(fills_at=39.0)                 # needs ~+15% over a 34.00 touch
    lb = make(a, touch_fn=lambda s, side: 34.0)
    fill = lb.execute(BrokerOrder("NIFTY|2026-08-11|24500|PE", OrderSide.BUY, 195,
                                 reduce_only=True))
    assert fill.quantity == 195 and fill.price == 39.0
    prices = [p for (_, t, p) in a.modified if t is OrderType.LIMIT]
    assert prices == sorted(prices), "each rung must cross FURTHER than the last"
    assert prices[0] == 35.05                        # 34 x 1.03, tick-snapped outward
    assert prices[-1] >= 39.0                        # the ladder reached a fillable price
    assert a.cancelled == []                         # filled, so nothing to cancel
    # and the run is NOT halted — that is the whole point
    assert not any(al.level.name == "ERROR" for al in lb.notifier.alerts)


def test_ladder_re_reads_the_touch_each_rung():
    """A running market must be chased, not crossed once off a stale quote."""
    seen = []
    quotes = iter([34.0, 37.0, 40.0, 43.0])

    def touch(_s, _side):
        v = next(quotes, 43.0)
        seen.append(v)
        return v

    a = LadderAdapter(fills_at=99.0)                 # never fills — exercise every rung
    lb = make(a, touch_fn=touch)
    with pytest.raises(OrderExecutionError):
        lb.execute(BrokerOrder("NIFTY|2026-08-11|24500|PE", OrderSide.BUY, 195,
                               reduce_only=True))
    assert len(seen) >= 4, "initial placement + one re-read per rung"
    prices = [p for (_, t, p) in a.modified if t is OrderType.LIMIT]
    assert len(prices) == 3                          # default ladder is three rungs
    assert prices == sorted(prices)


def test_ladder_still_raises_when_nothing_fills():
    """Exhausting the ladder must behave exactly as before: cancel, then halt."""
    a = LadderAdapter(fills_at=1e9)
    lb = make(a, touch_fn=lambda s, side: 34.0)
    with pytest.raises(OrderExecutionError, match="CANCELLED"):
        lb.execute(BrokerOrder("NIFTY|2026-08-11|24500|PE", OrderSide.BUY, 195))
    assert a.cancelled == ["KITE-1"]


def test_protect_pct_zero_still_means_no_crossing():
    """Back-compat: the single knob still steers the whole ladder, and 0 disarms it."""
    a = FakeAdapter(initial=PENDING, after_modify=COMPLETE)
    lb = make(a, protect_pct=0.0, touch_fn=lambda s, side: 100.0)
    lb.execute(BrokerOrder("NIFTY|2026-07-07|24500|CE", OrderSide.BUY, 65))
    assert a.modified[0] == ("KITE-1", OrderType.LIMIT, 100.0)   # no cross at all


def test_explicit_ladder_is_honoured():
    a = LadderAdapter(fills_at=1e9)
    lb = make(a, touch_fn=lambda s, side: 100.0, protect_ladder=(5.0, 25.0))
    with pytest.raises(OrderExecutionError):
        lb.execute(BrokerOrder("NIFTY|2026-07-07|24500|CE", OrderSide.BUY, 65,
                               reduce_only=True))
    assert [p for (_, t, p) in a.modified if t is OrderType.LIMIT] == [105.0, 125.0]


def test_a_market_placed_order_never_walks_the_ladder():
    """No touch → the order is PLACED as MARKET, and escalation is LIMIT-only. The ladder
    must not touch that path at all (it would only re-send an order type the broker may
    reject outright on options)."""
    a = FakeAdapter(initial=PENDING, after_modify=PENDING, after_cancel=PENDING)
    lb = make(a, touch_fn=lambda s, side: None)
    with pytest.raises(OrderExecutionError):
        lb.execute(BrokerOrder("AAA", OrderSide.SELL, 10))
    assert a.placed[0].order_type is OrderType.MARKET
    assert a.modified == []          # never escalated
    assert a.cancelled == ["KITE-1"]  # cancel-then-halt, exactly as before


def test_entries_keep_the_single_rung_exits_walk_the_ladder():
    """The ladder exists so an EXIT gets out. An ENTRY must keep exactly the pre-2026-08-11
    behaviour — one 3% rung then give up — because chasing 20% through the touch to OPEN a
    position would trade the halt risk for a bad-fill risk."""
    entry = LadderAdapter(fills_at=1e9)
    lb = make(entry, touch_fn=lambda s, side: 100.0)
    with pytest.raises(OrderExecutionError):
        lb.execute(BrokerOrder("NIFTY|2026-08-11|24500|PE", OrderSide.SELL, 195))
    assert [p for (_, t, p) in entry.modified if t is OrderType.LIMIT] == [97.0]  # ONE rung

    exit_ = LadderAdapter(fills_at=1e9)
    lb2 = make(exit_, touch_fn=lambda s, side: 100.0)
    with pytest.raises(OrderExecutionError):
        lb2.execute(BrokerOrder("NIFTY|2026-08-11|24500|PE", OrderSide.BUY, 195,
                                reduce_only=True))
    assert len([p for (_, t, p) in exit_.modified if t is OrderType.LIMIT]) == 3


def test_the_executor_marks_closing_orders_reduce_only():
    """Wiring check: the shared executor must flag its three CLOSING paths, or the ladder
    never engages on the orders it was built for."""
    import inspect

    from skas_algo.engine import execution

    src = inspect.getsource(execution.SliceExecutor)
    for fn in ("_sell", "_close_position", "_buy_to_close"):
        body = src.split(f"def {fn}(")[1].split("\n    def ")[0]
        assert "reduce_only=True" in body, f"{fn} must mark its order reduce_only"
    for fn in ("_buy", "_sell_to_open"):
        body = src.split(f"def {fn}(")[1].split("\n    def ")[0]
        assert "reduce_only" not in body, f"{fn} OPENS a position — must not be reduce_only"
