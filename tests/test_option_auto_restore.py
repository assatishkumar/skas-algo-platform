"""The Mac's automatic gap-fill from the VPS store (owner 2026-10-03): at boot, then once a
day after the peer's capture; off unless configured; a peer outage never breaks maintenance."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from skas_algo.live import manager as mgr


@pytest.fixture
def cfg(monkeypatch):
    from skas_algo.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "option_bars_auto_restore", True)
    monkeypatch.setattr(s, "peer_api_url", "https://peer.example")
    monkeypatch.setattr(s, "peer_api_token", "tok")
    monkeypatch.setattr(s, "option_bars_backup_dir", None)
    return s


def _run(monkeypatch, now, calls, result=None, boom=False):
    m = mgr.LiveRunManager()

    def fake_restore(url, *, token, days):
        calls.append((url, token, days))
        if boom:
            raise RuntimeError("peer down")
        return result or {"remote": 3, "already": 1, "restored": [], "skipped": [], "errors": []}

    monkeypatch.setattr("skas_algo.services.option_restore.restore_from", fake_restore)

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(mgr, "datetime", _DT)
    return m


def test_restores_at_boot_then_once_a_day_after_the_peers_capture(cfg, monkeypatch):
    calls: list = []
    morning = datetime(2026, 10, 15, 9, 0, tzinfo=mgr.IST)
    m = _run(monkeypatch, morning, calls)
    asyncio.run(m._maybe_restore_option_bars())
    assert calls == [("https://peer.example", "tok", 45)]           # boot: at once
    asyncio.run(m._maybe_restore_option_bars())
    assert len(calls) == 1                                          # same day: no repeat
    m._last_option_restore = datetime(2026, 10, 14, 9, 0, tzinfo=mgr.IST)
    asyncio.run(m._maybe_restore_option_bars())
    assert len(calls) == 1                                          # next day, before 16:30
    monkeypatch.setattr(mgr, "datetime", type("_D", (datetime,), {
        "now": classmethod(lambda c, tz=None: datetime(2026, 10, 15, 16, 35, tzinfo=mgr.IST))}))
    asyncio.run(m._maybe_restore_option_bars())
    assert len(calls) == 2                                          # after the peer's capture


def test_off_unless_configured_and_a_peer_outage_is_swallowed(cfg, monkeypatch):
    calls: list = []
    now = datetime(2026, 10, 15, 17, 0, tzinfo=mgr.IST)
    m = _run(monkeypatch, now, calls, boom=True)
    asyncio.run(m._maybe_restore_option_bars())                     # raises inside → logged
    assert calls and not m.option_restore_running
    monkeypatch.setattr(cfg, "option_bars_auto_restore", False)
    m2 = _run(monkeypatch, now, calls)
    asyncio.run(m2._maybe_restore_option_bars())
    assert len(calls) == 1                                          # off → never called
