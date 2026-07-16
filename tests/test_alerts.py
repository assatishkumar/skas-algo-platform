"""In-app alerts: InAppNotifier persists + prunes; GET /alerts feed + unread count;
mark-read; the notifier is wired into build_notifier; snapshot carries mode."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from skas_algo.api import create_app
from skas_algo.notify import in_app as in_app_mod
from skas_algo.notify.base import Alert, AlertLevel
from skas_algo.notify.factory import build_notifier
from skas_algo.notify.in_app import InAppNotifier


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_in_app_notifier_persists_and_feed_reads(client):
    InAppNotifier().send(Alert("BOOK MISMATCH", "run 7: extra 65 units", AlertLevel.ERROR))
    InAppNotifier().send(Alert("Backup done", level=AlertLevel.SUCCESS))
    out = client.get("/api/v1/alerts").json()
    assert out["unread"] == 2
    top = out["alerts"][0]                      # newest first
    assert top["title"] == "Backup done" and top["level"] == "SUCCESS" and not top["read"]
    assert out["alerts"][1]["message"] == "run 7: extra 65 units"
    assert top["ts"]                            # ISO timestamp present


def test_mark_read_clears_unread(client):
    InAppNotifier().send(Alert("t1"))
    assert client.post("/api/v1/alerts/mark-read").json()["marked"] >= 1
    out = client.get("/api/v1/alerts").json()
    assert out["unread"] == 0 and all(a["read"] for a in out["alerts"])


def test_prune_keeps_newest(monkeypatch, client):
    monkeypatch.setattr(in_app_mod, "KEEP", 5)
    for i in range(8):
        InAppNotifier().send(Alert(f"a{i}"))
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
