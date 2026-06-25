"""LiveRun stamps option trade events with the underlying's spot at execution, so the analysis
page can mark the true per-cycle entry/exit spot even when the cached index series lags."""

from __future__ import annotations

import types

from skas_algo.live.manager import LiveRun


class _Market:
    def index_spot(self, u):
        return {"NIFTY": 25000.0}.get(u.upper())


def test_tag_underlying_spot():
    fake = types.SimpleNamespace(session=types.SimpleNamespace(market=_Market()))
    events = [
        {"ticker": "NIFTY|2026-07-28|24550|CE", "action": "SHORT"},
        {"ticker": "RELIANCE", "action": "BUY"},                       # equity → not an option → untagged
        {"ticker": "NIFTY|2026-07-28|24350|CE", "action": "BUY", "underlying_spot": 1.0},  # preset → kept
    ]
    LiveRun._tag_underlying_spot(fake, events)
    assert events[0]["underlying_spot"] == 25000.0
    assert "underlying_spot" not in events[1]
    assert events[2]["underlying_spot"] == 1.0
