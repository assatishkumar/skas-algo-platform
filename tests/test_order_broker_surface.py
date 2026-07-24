"""Order-broker surfacing + resume-after-login (the 2026-07-17 paper-flatten incident).

A restart demotes a LIVE run to PaperBroker (recovery keeps simulated fills unless
SKAS_LIVE_RESUME_ORDERS_ON_RECOVERY — test_recovery.py pins that side). Until 2026-07-17
nothing surfaced the demotion: the tile said "live" while a manual flatten filled on
paper and the real Zerodha book stayed open. These tests pin the guards that now exist:

  * ``LiveRun.order_broker()`` — "live" only when a LiveBroker is installed;
  * the login promotion finishes a resume-pending re-injection through the SAME 4-key
    gate, marks the run reconcile-pending, and clears the pending flag;
  * a gate refusal (disarmed / flag off / not pending) leaves the paper broker alone;
  * ``strategy_pnl`` — the decision-entry-basis measure the exit checks compare —
    and the exit-rule cadence wording ("checked every …") the UI now shows.
"""

from __future__ import annotations

from types import SimpleNamespace

from skas_algo.config import get_settings
from skas_algo.live.manager import LiveRun, manager


class _ExecAdapter:
    """Minimal armed adapter exposing the full order surface (adapter_can_execute)."""

    def __init__(self, armed=True):
        self.armed = armed

    def place_order(self, *a, **k): ...
    def modify_order(self, *a, **k): ...
    def order_status(self, *a, **k): ...
    def cancel_order(self, *a, **k): ...


def _live(pending=True, adapter=None):
    """A LiveRun-shaped stub: _maybe_resume_orders touches session/config/quote_source,
    the pending/reconcile flags, and order_broker (borrowed unbound from LiveRun)."""
    s = SimpleNamespace(
        session=SimpleNamespace(broker="PAPER-SENTINEL", market=None),
        quote_source=SimpleNamespace(adapter=adapter or _ExecAdapter()),
        config=SimpleNamespace(mode="LIVE", broker_account_id=1, name="t"),
        run_id=7,
        resume_orders_pending=pending,
        reconcile_pending=False,
    )
    s.order_broker = lambda: LiveRun.order_broker(s)
    return s


def test_order_broker_predicate():
    from skas_algo.brokers.live_broker import LiveBroker

    assert _live().order_broker() == "paper"
    s = _live()
    s.session.broker = LiveBroker.__new__(LiveBroker)
    assert s.order_broker() == "live"


def test_login_promotion_reinjects_resume_pending_run(monkeypatch):
    from skas_algo.brokers.live_broker import LiveBroker

    monkeypatch.setattr(get_settings(), "live_trading_enabled", True)
    alerts = []
    monkeypatch.setattr("skas_algo.notify.build_notifier",
                        lambda: SimpleNamespace(send=lambda a: alerts.append(a)))

    s = _live(pending=True)
    manager._maybe_resume_orders(s)

    assert isinstance(s.session.broker, LiveBroker)     # re-armed through the real gate
    assert s.resume_orders_pending is False             # done — later logins won't re-fire
    assert s.reconcile_pending is True                  # book verified BEFORE first decision
    assert len(alerts) == 1 and "RESUMED" in alerts[0].title


def test_login_promotion_leaves_non_pending_run_alone(monkeypatch):
    """A LIVE run that is paper for any reason OTHER than a recovery resume (e.g. deployed
    against a disarmed account) must NOT be silently re-armed by a login."""
    monkeypatch.setattr(get_settings(), "live_trading_enabled", True)
    s = _live(pending=False)
    manager._maybe_resume_orders(s)
    assert s.session.broker == "PAPER-SENTINEL"
    assert s.reconcile_pending is False


def test_login_promotion_respects_the_gate(monkeypatch):
    """Resume-pending but the 4-key gate says no (disarmed / platform flag off) → stays
    paper AND stays pending, so a later promotion can retry once the key turns."""
    monkeypatch.setattr(get_settings(), "live_trading_enabled", True)
    s = _live(pending=True, adapter=_ExecAdapter(armed=False))
    manager._maybe_resume_orders(s)
    assert s.session.broker == "PAPER-SENTINEL" and s.resume_orders_pending is True

    monkeypatch.setattr(get_settings(), "live_trading_enabled", False)
    s = _live(pending=True)
    manager._maybe_resume_orders(s)
    assert s.session.broker == "PAPER-SENTINEL" and s.resume_orders_pending is True


def test_quote_rebuild_rebinds_the_order_adapter():
    """The 2026-07-24 halt: after the ~06:00 Kite token rollover the login promotion
    rebuilt the QUOTE adapter but the LiveBroker kept yesterday's dead token — hourly
    reconcile read green all day while the 15:15 calendar exit's place_order failed
    auth. A quote-source rebuild must repoint the ORDER path too, reconcile-first."""
    from skas_algo.brokers.live_broker import LiveBroker

    old, fresh = _ExecAdapter(), _ExecAdapter()
    s = _live(pending=False, adapter=fresh)
    s.session.broker = LiveBroker(old, account_id=1, run_name="t")
    manager._rebind_order_adapter(s)
    assert s.session.broker.adapter is fresh
    assert s.reconcile_pending is True                  # book verified BEFORE next order


def test_rebind_respects_the_gate_and_identity():
    from skas_algo.brokers.live_broker import LiveBroker

    # Fresh adapter fails the gate (disarmed) → old adapter kept (halts safely later).
    old = _ExecAdapter()
    s = _live(pending=False, adapter=_ExecAdapter(armed=False))
    s.session.broker = LiveBroker(old, account_id=1, run_name="t")
    manager._rebind_order_adapter(s)
    assert s.session.broker.adapter is old and s.reconcile_pending is False

    # Same adapter object (nothing was rebuilt) → no spurious reconcile churn.
    s2 = _live(pending=False)
    s2.session.broker = LiveBroker(s2.quote_source.adapter, account_id=1, run_name="t")
    manager._rebind_order_adapter(s2)
    assert s2.reconcile_pending is False

    # Paper-broker run → untouched (rebind is a real-order concern only).
    s3 = _live(pending=False)
    manager._rebind_order_adapter(s3)
    assert s3.session.broker == "PAPER-SENTINEL" and s3.reconcile_pending is False


def test_rebind_sweep_covers_running_real_order_runs(monkeypatch):
    """The 5-min maintenance sweep is the safety net: even if a future quote-source
    swap forgets the rebind hook, the sweep converges the order path within a tick."""
    from skas_algo.brokers.live_broker import LiveBroker

    old, fresh = _ExecAdapter(), _ExecAdapter()
    s = _live(pending=False, adapter=fresh)
    s.session.broker = LiveBroker(old, account_id=1, run_name="t")
    monkeypatch.setattr(manager, "runs", {s.run_id: s})
    manager._rebind_order_sweep()
    assert s.session.broker.adapter is fresh and s.reconcile_pending is True


def test_sweep_remints_on_db_token_drift(monkeypatch):
    """The WS-masked rollover hole (2026-07-24 review): if the KiteTicker keeps serving
    marks after the ~06:00 token kill, no quote_error fires, the quote source is never
    rebuilt, and rebind converges stale→stale. The sweep must detect DB-token drift
    directly and re-mint BOTH adapters, without waiting for a read to fail."""
    from skas_algo.brokers.live_broker import LiveBroker
    from skas_algo.db.base import session_scope as scope
    from skas_algo.db.models import BrokerAccount
    from skas_algo.security import encrypt
    from skas_algo.services import broker as broker_svc

    with scope() as s_db:
        acct = BrokerAccount(broker="zerodha", label="drift", user_id="AB2",
                             enc_api_secret=encrypt("sec"), api_key="k",
                             session_token=encrypt("FRESH-TOKEN"))
        s_db.add(acct)
        s_db.flush()
        acct_id = acct.id

    stale, fresh = _ExecAdapter(), _ExecAdapter()
    stale.access_token, fresh.access_token = "DEAD-TOKEN", "FRESH-TOKEN"
    s = _live(pending=False, adapter=stale)  # quote adapter ALSO stale — the masked case
    s.config.broker_account_id = acct_id
    s.session.broker = LiveBroker(stale, account_id=acct_id, run_name="t")
    s.on_cache_fallback = False
    s.quote_error = None
    s._wire_quote_source = lambda: None

    monkeypatch.setattr(broker_svc, "has_valid_session", lambda a: True)
    monkeypatch.setattr(broker_svc, "make_adapter", lambda a: fresh)
    monkeypatch.setattr("skas_algo.live.pricefeed.build_quote_source",
                        lambda account, adapter: SimpleNamespace(adapter=adapter))

    manager._maybe_remint_order_adapter(s)
    assert s.quote_source.adapter is fresh              # read path re-minted…
    assert s.session.broker.adapter is fresh            # …and the ORDER path with it
    assert s.reconcile_pending is True

    # No drift (DB token == held token) → nothing rebuilt, no reconcile churn.
    s2 = _live(pending=False, adapter=stale)
    s2.config.broker_account_id = acct_id
    fresh2 = _ExecAdapter()
    fresh2.access_token = "FRESH-TOKEN"
    s2.session.broker = LiveBroker(fresh2, account_id=acct_id, run_name="t")
    s2.on_cache_fallback = False
    s2.quote_error = None
    s2._wire_quote_source = lambda: None
    q_before = s2.quote_source
    manager._maybe_remint_order_adapter(s2)
    assert s2.quote_source is q_before and s2.reconcile_pending is False


def test_strategy_pnl_is_the_decision_basis_measure():
    """legs_mtm_pnl marks from the strategy's OWN entries (decision premiums), the exact
    sum its exit checks compare — run-7's short leg filled 0.49 better than its decision
    price, putting the book P&L ~₹286 above this measure while the UI implied 'target hit'."""
    from skas_algo.strategies.hni_weekly import HniWeeklyStrategy

    strat = HniWeeklyStrategy(universe=["NIFTY"], initial_capital=1_000_000)
    strat.legs = [
        {"symbol": "NIFTY|2026-07-21|24300|CE", "dir": 1, "units": 195, "entry": 87.5},
        {"symbol": "NIFTY|2026-07-21|24500|CE", "dir": -1, "units": 585, "entry": 39.2},
        {"symbol": "NIFTY|2026-07-21|24700|CE", "dir": 1, "units": 390, "entry": 16.3},
    ]
    closes = {
        "NIFTY|2026-07-21|24300|CE": 77.05,
        "NIFTY|2026-07-21|24500|CE": 23.65,
        "NIFTY|2026-07-21|24700|CE": 6.4,
    }
    # (77.05−87.5)·195 + (23.65−39.2)·585·(−1)·(−1 sign via dir) + (6.4−16.3)·390
    assert strat.strategy_pnl(closes) == (
        (77.05 - 87.5) * 195 + (23.65 - 39.2) * 585 * -1 + (6.4 - 16.3) * 390
    )
    # A leg without a mark → None (matches the strategies' own missing-print bail-outs).
    assert strat.strategy_pnl({"NIFTY|2026-07-21|24300|CE": 77.05}) is None
    strat.legs = []
    assert strat.strategy_pnl(closes) is None           # flat → no measure


def test_exit_rules_surface_the_check_cadence():
    """The UI banner must say HOW OFTEN each exit is sampled — the 15-min profit samples
    straddling a 19-min target breach is exactly what run-7's owner couldn't see."""
    from skas_algo.strategies.hni_weekly import HniWeeklyStrategy

    rules = HniWeeklyStrategy(universe=["NIFTY"], initial_capital=1_000_000).exit_rules()
    assert any("checked every 15 min" in r for r in rules)          # profit (HNI default)
    assert any("checked at EOD 15:15" in r for r in rules)          # stop
    assert any(r.startswith("Calendar exit from Fri") for r in rules)

    rules_1m = HniWeeklyStrategy(
        universe=["NIFTY"], initial_capital=1_000_000, profit_check="1min").exit_rules()
    assert any("checked every 1 min" in r for r in rules_1m)
