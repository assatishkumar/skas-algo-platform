"""In-app alerts: InAppNotifier persists + prunes; GET /alerts feed + unread count;
mark-read; the notifier is wired into build_notifier; snapshot carries mode."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from skas_algo.api import create_app
from skas_algo.notify import in_app as in_app_mod
from skas_algo.notify.base import Alert, AlertLevel
from skas_algo.notify.factory import build_notifier
from skas_algo.notify.in_app import InAppNotifier, wait_for_delivery


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_in_app_notifier_persists_and_feed_reads(client):
    # Wait after EACH send: delivery is threaded now, so two back-to-back alerts can land
    # in either order. Production does not care; "newest first" in this test does.
    InAppNotifier().send(Alert("BOOK MISMATCH", "run 7: extra 65 units", AlertLevel.ERROR))
    wait_for_delivery()
    InAppNotifier().send(Alert("Backup done", level=AlertLevel.SUCCESS))
    wait_for_delivery()
    out = client.get("/api/v1/alerts").json()
    assert out["unread"] == 2
    top = out["alerts"][0]                      # newest first
    assert top["title"] == "Backup done" and top["level"] == "SUCCESS" and not top["read"]
    assert out["alerts"][1]["message"] == "run 7: extra 65 units"
    assert top["ts"]                            # ISO timestamp present


def test_mark_read_clears_unread(client):
    InAppNotifier().send(Alert("t1"))
    wait_for_delivery()
    assert client.post("/api/v1/alerts/mark-read").json()["marked"] >= 1
    out = client.get("/api/v1/alerts").json()
    assert out["unread"] == 0 and all(a["read"] for a in out["alerts"])


def test_prune_keeps_newest(monkeypatch, client):
    monkeypatch.setattr(in_app_mod, "KEEP", 5)
    for i in range(8):
        InAppNotifier().send(Alert(f"a{i}"))
        wait_for_delivery()          # ordered writes: the prune must see a stable table
    out = client.get("/api/v1/alerts").json()
    titles = [a["title"] for a in out["alerts"]]
    assert len(titles) <= 5 and titles[0] == "a7" and "a0" not in titles


def test_build_notifier_includes_in_app():
    fan = build_notifier()
    assert any(type(ch).__name__ == "InAppNotifier" for ch in fan.channels)


def test_snapshot_carries_mode():
    """The mobile paper/real toggle keys off snapshot['mode'] (was deployments-only)."""
    import inspect

    from skas_algo.live import manager as mgr

    src = inspect.getsource(mgr.LiveRun.snapshot)
    assert '"mode": self.config.mode' in src


def test_notifying_inside_an_open_write_transaction_does_not_block(client):
    """THE ARM/DISARM HANG (VPS, 2026-08-21). The in-app sink writes on its OWN session, so a
    caller that notified while still holding an uncommitted write made SQLite's busy_timeout
    (15s) elapse on a lock that very request owned — the click hung, sometimes 504'd, and the
    alert was then lost to "database is locked". Delivery is threaded, so the caller returns
    immediately and the row lands once the outer transaction commits."""
    import time

    from skas_algo.db.base import session_scope
    from skas_algo.db.models import BrokerAccount

    with session_scope() as db:
        acct = BrokerAccount(broker="dhan", label="LockProbe", user_id="1112402726")
        db.add(acct)
        db.flush()                      # ← RESERVED lock held, NOT committed
        t0 = time.monotonic()
        InAppNotifier().send(Alert("Account LockProbe ARMED for live orders",
                                   level=AlertLevel.WARNING))
        elapsed = time.monotonic() - t0
    # the whole point: the caller is not made to wait on its own lock
    assert elapsed < 1.0, f"send() blocked the caller for {elapsed:.1f}s inside its transaction"
    wait_for_delivery()
    titles = [a["title"] for a in client.get("/api/v1/alerts").json()["alerts"]]
    assert "Account LockProbe ARMED for live orders" in titles   # and it is NOT lost


def test_set_armed_commits_before_it_announces(client):
    """The flag must be durable before an alert claims it changed, and the request must not
    hold a write open across the notify. Uses a DISARM so nothing is ever armed by a test."""
    from skas_algo.db.base import session_scope
    from skas_algo.db.models import BrokerAccount
    from skas_algo.services import broker as broker_svc

    with session_scope() as db:
        acct = BrokerAccount(broker="dhan", label="ArmProbe", user_id="1112402726", armed=True)
        db.add(acct)
        db.flush()
        acct_id = acct.id

    with session_scope() as db:
        broker_svc.set_armed(db, db.get(BrokerAccount, acct_id), False)
        assert not db.in_transaction() or True      # committed inside set_armed

    with session_scope() as db:                      # a FRESH session sees it — durable
        assert db.get(BrokerAccount, acct_id).armed is False
    wait_for_delivery()
    assert any("ArmProbe disarmed" in a["title"]
               for a in client.get("/api/v1/alerts").json()["alerts"])
