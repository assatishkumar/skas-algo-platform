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
