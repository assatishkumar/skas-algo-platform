"""Recovery gating for the owner-gated live_resume_orders_on_recovery flag.

Default OFF: a recovered LIVE run keeps PaperBroker (a restart pauses real orders).
ON: recovery re-injects the LiveBroker (still behind the 4-key gate) BEFORE the LiveRun
is built, so the run starts reconcile_pending. No real orders are placed — the injection
itself is fake-adapter matrix-tested in test_live_broker.py.
"""

from __future__ import annotations

from types import SimpleNamespace

from skas_algo.config import get_settings
from skas_algo.live import recovery


def _drive_rebuild(monkeypatch, resume_flag: bool):
    """Run recovery._rebuild with its heavy deps mocked; return (inject_calls, session)."""
    fake_session = SimpleNamespace(broker="PAPER-SENTINEL", load_state=lambda s: None)
    # _build_session is imported locally inside _rebuild → patch it at its source module.
    monkeypatch.setattr("skas_algo.live.manager._build_session", lambda *a, **k: fake_session)
    monkeypatch.setattr(recovery, "_quote_source", lambda db, cfg, loader: ("QS", False))
    monkeypatch.setattr(recovery, "get_strategy", lambda sid: (lambda **k: object()))

    calls = {"inject": 0, "session_at_inject": None, "session_at_liverun": None}

    def spy_inject(session, config, qs):
        calls["inject"] += 1
        calls["session_at_inject"] = session

    def fake_live_run(rid, aid, config, session, qs, bc):
        calls["session_at_liverun"] = session
        return SimpleNamespace(on_cache_fallback=False, run_id=rid, config=config)

    monkeypatch.setattr(recovery.manager, "_maybe_inject_live_broker", spy_inject)
    monkeypatch.setattr(recovery, "LiveRun", fake_live_run)
    monkeypatch.setattr(recovery.manager, "register", lambda live: None)
    monkeypatch.setattr(recovery.manager, "start_loop", lambda rid: None)
    monkeypatch.setattr(get_settings(), "live_resume_orders_on_recovery", resume_flag)

    algo = SimpleNamespace(strategy_id="custom_options", name="t", capital=1_000_000)
    run = SimpleNamespace(
        id=1, algo_id=1, state=None, mode=SimpleNamespace(value="LIVE"),
        params_snapshot={"instrument_class": "DERIV", "underlying": "NIFTY",
                         "symbols": ["NIFTY"], "auto": False},
    )
    db = SimpleNamespace(get=lambda model, _id: algo)
    recovery._rebuild(db, run, loader=None)
    return calls


def test_recovery_keeps_paper_by_default(monkeypatch):
    calls = _drive_rebuild(monkeypatch, resume_flag=False)
    assert calls["inject"] == 0                       # no re-injection → stays PaperBroker


def test_recovery_resumes_live_when_flag_on(monkeypatch):
    calls = _drive_rebuild(monkeypatch, resume_flag=True)
    assert calls["inject"] == 1                       # LiveBroker injection attempted
    # Injection happens on the SAME session the LiveRun is then built from, and BEFORE it,
    # so the run's reconcile_pending reflects the (possibly) real-order broker.
    assert calls["session_at_inject"] is calls["session_at_liverun"]


def test_lot_opened_at_round_trips_to_datetime():
    """export_state stringifies Lot.opened_at; load_state must parse it BACK to a datetime — else
    a recovered lot (str) plus a lot opened AFTER recovery (datetime) form a mixed set that crashes
    snapshot()'s min() ("'<' not supported between datetime and str") and 500s the whole /live list
    → a blank Live page (2026-07-10, an equity FIFO run)."""
    from datetime import datetime, timezone

    from skas_algo.engine.portfolio import Portfolio

    p = Portfolio(cash=1_000_000)
    p.buy("RELIANCE", 10, 1500.0, when=datetime(2026, 7, 9, 15, 20, tzinfo=timezone.utc))

    p2 = Portfolio(cash=1_000_000)
    p2.load_state(p.export_state())
    assert isinstance(p2.lots("RELIANCE")[0].opened_at, datetime)   # parsed back, not a raw str

    # a lot opened AFTER recovery (a real datetime) must not break min() over the mixed set
    p2.buy("RELIANCE", 5, 1510.0, when=datetime(2026, 7, 10, 15, 22, tzinfo=timezone.utc))
    lots = p2.lots("RELIANCE")
    assert min(lot.opened_at for lot in lots if lot.opened_at is not None)  # no TypeError
