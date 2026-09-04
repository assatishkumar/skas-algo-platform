"""A Dhan run's margin is Kite's basket for the same legs when a Kite session exists.

Dhan has no basket API: its figure adds each short leg's standalone margin, so run 27's
five-leg volcano read ₹13.44L against the ₹5.7L the owner measured. Owner call 2026-09-04:
price the legs through Kite when it is there, fall back to Dhan's own figure when it is not,
and keep Dhan's sum as a footnote either way."""

from types import SimpleNamespace

from skas_algo.live import manager as mgr
from skas_algo.live.manager import LiveRun


def _fake_run(source: str, dhan_margin: float, pushed: list):
    pf = SimpleNamespace(
        lot_symbols=lambda: ["NIFTY|2026-09-29|23700|PE", "NIFTY|2026-09-29|24100|PE"],
        lots=lambda s: [SimpleNamespace(direction=-1 if "23700" in s else 1, units=390)],
    )

    class _Adapter:
        def basket_margin(self, legs):
            return dhan_margin

    return SimpleNamespace(
        config=SimpleNamespace(instrument_class="DERIV", quote_source=source, segment="DERIV"),
        session=SimpleNamespace(
            portfolio=pf,
            strategy=SimpleNamespace(set_broker_margin=lambda m: pushed.append(m)),
            set_margin_override=lambda m: None,
        ),
        quote_source=SimpleNamespace(adapter=_Adapter()),
        _margin_symbols=[], _last_margin_at=None, _margin=None,
        _margin_via=None, _margin_via_label=None, _margin_dhan_sum=None, run_id=27,
    )


def test_a_dhan_run_shows_and_pushes_kites_figure_and_keeps_dhans_sum_as_the_footnote(monkeypatch):
    monkeypatch.setattr(mgr, "is_market_open", lambda *a, **k: True)
    asked: list = []

    def kite(legs):
        asked.append(legs)
        return (588_000.0, "Satish Kite")

    monkeypatch.setattr(mgr, "_kite_reference_margin", kite)
    pushed: list = []
    run = _fake_run("dhan", 1_344_016.25, pushed)
    LiveRun._maybe_refresh_margin(run)
    assert run._margin == 588_000.0 and run._margin_via == "reference"
    assert run._margin_via_label == "Satish Kite" and run._margin_dhan_sum == 1_344_016.25
    assert pushed == [588_000.0]  # the %-rules that freeze off the broker push get Kite's number
    # the same legs, direction and size, were what Kite priced
    assert {(l["symbol"], l["direction"], l["units"]) for l in asked[0]} == {
        ("NIFTY|2026-09-29|23700|PE", -1, 390), ("NIFTY|2026-09-29|24100|PE", 1, 390),
    }


def test_without_a_kite_session_dhans_own_figure_stands(monkeypatch):
    monkeypatch.setattr(mgr, "is_market_open", lambda *a, **k: True)
    monkeypatch.setattr(mgr, "_kite_reference_margin", lambda legs: None)
    pushed: list = []
    run = _fake_run("dhan", 1_344_016.25, pushed)
    LiveRun._maybe_refresh_margin(run)
    assert run._margin == 1_344_016.25 and run._margin_via is None and run._margin_dhan_sum is None
    assert pushed == [1_344_016.25]


def test_a_zerodha_run_never_asks_for_a_reference(monkeypatch):
    monkeypatch.setattr(mgr, "is_market_open", lambda *a, **k: True)
    calls = []
    monkeypatch.setattr(mgr, "_kite_reference_margin", lambda legs: calls.append(1) or (1.0, "x"))
    pushed: list = []
    run = _fake_run("zerodha", 570_000.0, pushed)
    LiveRun._maybe_refresh_margin(run)
    assert calls == [] and run._margin == 570_000.0 and run._margin_via is None


def test_the_kite_reference_pricer_needs_a_logged_in_zerodha_account(monkeypatch):
    """No session → None (the caller falls back); a session → Kite's basket + the label."""
    from contextlib import contextmanager

    from skas_algo.services import broker as broker_svc

    @contextmanager
    def fake_scope():
        yield None

    monkeypatch.setattr(mgr, "session_scope", fake_scope)
    dhan = SimpleNamespace(broker="dhan", label="SatishDhan")
    kite = SimpleNamespace(broker="zerodha", label="Satish Kite")
    monkeypatch.setattr(broker_svc, "list_accounts", lambda db: [dhan, kite])
    monkeypatch.setattr(broker_svc, "has_valid_session", lambda a: a is kite and getattr(a, "ok", False))
    monkeypatch.setattr(broker_svc, "make_adapter", lambda a: SimpleNamespace(basket_margin=lambda legs: 588_000.0))
    assert mgr._kite_reference_margin([]) is None
    kite.ok = True
    assert mgr._kite_reference_margin([]) == (588_000.0, "Satish Kite")
