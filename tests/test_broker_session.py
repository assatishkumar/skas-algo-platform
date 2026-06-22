"""Broker session expiry honesty: a Kite token dies at ~06:00 IST the next morning, so the stored
expiry must reflect that (not a rolling 12h) — otherwise 'session ✓' shows green while the token is
already dead and live deployments freeze."""

from __future__ import annotations

from datetime import UTC, datetime

from skas_algo.brokers.zerodha import _next_kite_expiry


def test_next_kite_expiry_is_next_6am_ist_and_future():
    exp = _next_kite_expiry()
    assert exp.tzinfo is None  # naive-UTC (matches DB storage + has_valid_session's assumption)
    # 06:00 IST == 00:30 UTC.
    assert (exp.hour, exp.minute) == (0, 30)
    assert exp > datetime.now(UTC).replace(tzinfo=None)  # always in the future
