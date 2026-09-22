"""demo-seed: synthetic days land only in a throwaway store the env points at, look like
captured days to the store's readers, and the real store is refused by construction."""

from __future__ import annotations

from datetime import date

import pytest

from skas_algo.data import option_intraday_store as store
from skas_algo.services import demo_seed


@pytest.fixture
def demo_store(tmp_path, monkeypatch):
    target = tmp_path / "1min"
    monkeypatch.setenv("SKAS_OPTION_INTRADAY_DIR", str(target))
    monkeypatch.setattr(store, "OPTION_INTRADAY_DIR", target)
    return target


def test_seeded_days_read_like_captured_days(demo_store):
    lines = demo_seed.seed(days=2, end="2026-09-18")
    assert store.captured_days() == ["2026-09-17", "2026-09-18"]
    assert lines[-1].startswith("store: ")
    df = store.load_day(date(2026, 9, 18), underlying="NIFTY")
    assert len(df) == 375 * 41 * 2                      # a print a minute, 41 strikes, both rights
    syms = set(df["symbol"])
    assert all(s.startswith("NIFTY|") and s.split("|")[3] in ("CE", "PE") for s in syms)
    assert {int(s.split("|")[2]) % 100 for s in syms} == {0}
    expiry = {s.split("|")[1] for s in syms}
    assert len(expiry) == 1 and date.fromisoformat(next(iter(expiry))).weekday() == 1  # a Tuesday
    # convex surface: the ATM call's time value beats the 500-OTM's, both above intrinsic
    first = df[df["start"] == df["start"].min()].set_index("symbol")["close"]
    exp = next(iter(expiry))
    atm = first[f"NIFTY|{exp}|24000|CE"]
    otm = first[f"NIFTY|{exp}|24500|CE"]
    assert atm > otm > 0.05


def test_the_real_store_is_refused(monkeypatch, tmp_path):
    monkeypatch.delenv("SKAS_OPTION_INTRADAY_DIR", raising=False)
    monkeypatch.setattr(store, "OPTION_INTRADAY_DIR", tmp_path)
    with pytest.raises(SystemExit, match="refusing to seed the REAL"):
        demo_seed.seed(days=1)
    monkeypatch.setenv("SKAS_OPTION_INTRADAY_DIR", str(demo_seed._REAL_STORE))
    with pytest.raises(SystemExit, match="refusing"):
        demo_seed.seed(days=1)
