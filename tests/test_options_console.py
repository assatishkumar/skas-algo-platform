"""Options Console session: the minute cursor, chain enrichment, and the isolation rule.

Synthetic store in tmp — no network, no broker, no real data. The console is a replay
surface, so the things worth pinning are the ones a screen cannot show you are wrong about:
that the cursor is deterministic, that a missing quote stays missing, and that the greeks
in the chain actually reprice the premium they were solved from.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from skas_algo.data import option_intraday_store as store
from skas_algo.engine.options import black_scholes as bs
from skas_algo.services.options_console import registry
from skas_algo.services.options_console.session import RISK_FREE, ConsoleSession, _t_years

EXP = "2026-07-21"
DAY = date(2026, 7, 14)


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "OPTION_INTRADAY_DIR", tmp_path / "1min")
    from skas_algo.config import get_settings
    monkeypatch.setattr(get_settings(), "option_bars_backup_dir", None)
    registry.clear()
    yield
    registry.clear()


def _rows(day, strike, right, by_minute, exp=EXP):
    sym = f"NIFTY|{exp}|{strike}|{right}"
    return [{"symbol": sym, "start": datetime(day.year, day.month, day.day, hh, mm),
             "open": px, "high": px, "low": px, "close": px, "volume": 100.0, "oi": 4200.0}
            for (hh, mm), px in by_minute.items()]


def _day(day=DAY):
    """A drifting straddle: the ATM pair prints every 5 minutes and drifts, plus one wing
    that prints ONLY at the open (stale later) and one that does not print until 11:30."""
    minutes = [(9, m) for m in range(15, 60, 5)] + [(h, m) for h in (10, 11)
                                                    for m in range(0, 60, 5)]
    out = []
    for i, (hh, mm) in enumerate(minutes):
        drift = i * 1.5
        out += _rows(day, 24000, "CE", {(hh, mm): 150.0 + drift})
        out += _rows(day, 24000, "PE", {(hh, mm): 152.0 - drift})
        out += _rows(day, 24100, "CE", {(hh, mm): 105.0 + drift})
        out += _rows(day, 24100, "PE", {(hh, mm): 200.0 - drift})
    out += _rows(day, 23500, "CE", {(9, 15): 640.0})       # prints once, then goes stale
    out += _rows(day, 24500, "CE", {(11, 30): 18.0})       # a wing nobody trades till 11:30
    return pd.DataFrame(out, columns=store.COLUMNS)


def _open(**kw) -> ConsoleSession:
    kw.setdefault("underlying", "NIFTY")
    kw.setdefault("day", DAY)
    kw.setdefault("expiry", EXP)
    return ConsoleSession(**kw)


def _stable(state: dict) -> dict:
    """The state minus its random id, so two sessions are comparable."""
    out = dict(state)
    out["session"] = {k: v for k, v in state["session"].items() if k != "id"}
    return out


def test_stepping_and_seeking_land_in_the_same_place():
    """Seeking is rewind-and-replay-forward, not a checkpoint restore, so 'step 45 times'
    and 'seek once' must be the SAME state — not merely close. That identity is the whole
    reason the design's backward jog chips can be trusted."""
    store.write_day(DAY, _day())
    stepped = _open(at="09:15")
    for _ in range(45):
        stepped.step(1)
    sought = _open(at="10:00")
    assert _stable(stepped.state()) == _stable(sought.state())


def test_a_rewind_really_rewinds():
    """Going forward to the close and back must not leave anything behind — no
    forward-filled mark from the future, no widened day range."""
    store.write_day(DAY, _day())
    s = _open(at="09:15")
    s.seek("11:55")
    s.seek("10:00")
    assert _stable(s.state()) == _stable(_open(at="10:00").state())


def test_the_day_range_is_the_forming_bar_not_the_settled_one():
    """The settled daily bar contains the future: at 09:30 its high and low have not
    happened yet. The strip reads the bar FORMING up to the cursor, so the range can only
    ever widen as the session runs — never jump to the day's final extremes."""
    store.write_day(DAY, _day())
    s = _open(at="09:20")
    early = s.state()["market"]
    s.seek("11:55")
    late = s.state()["market"]
    assert late["day_high"] >= early["day_high"]
    assert late["day_low"] <= early["day_low"]
    assert early["day_high"] > early["day_low"]      # a real range, not a single sample


def test_a_strike_that_has_not_traded_yet_is_unquoted_not_invented():
    """The store is trades only, so before a wing's first print of the day there is no
    price. Black-Scholes could happily manufacture one, which would put a number on the
    screen the market never showed. It stays null and unquoted until it actually trades.

    (A strike that never trades AT ALL on a day has no row: the store's symbol list is the
    ladder, so an untraded contract is simply not listed — the same way the batch replay
    sees it, and honest for the same reason.)"""
    store.write_day(DAY, _day())
    s = _open(at="10:00", strike_window=20)
    rows = {r["strike"]: r for r in s.chain_rows()}
    assert 24500.0 in rows, "listed, because it trades later today"
    quiet = rows[24500.0]["ce"]
    assert quiet["quoted"] is False and quiet["ltp"] is None
    assert quiet["iv"] is None and quiet["delta"] is None
    live = rows[24000.0]["ce"]
    assert live["quoted"] is True and live["ltp"] is not None
    st = s.state()
    assert 0 < st["chain"]["quoted"] < st["chain"]["total"]

    # …and once it prints, it is quoted.
    s.seek("11:35")
    after = {r["strike"]: r for r in s.chain_rows()}[24500.0]["ce"]
    assert after["quoted"] is True and after["ltp"] == pytest.approx(18.0)


def test_a_stale_mark_keeps_its_price_but_reports_its_age():
    """A leg that printed at 09:15 and not since is still worth SOMETHING — dropping the
    price would be as much a lie as inventing one. It keeps the last print and reports how
    old it is, which is what the design's Near/Illiquid toggle reads."""
    store.write_day(DAY, _day())
    s = _open(at="09:20")
    fresh = {r["strike"]: r for r in s.chain_rows()}[23500.0]["ce"]
    assert fresh["quoted"] and fresh["stale_min"] == 5
    s.seek("11:00")
    old = {r["strike"]: r for r in s.chain_rows()}[23500.0]["ce"]
    assert old["ltp"] == pytest.approx(640.0) and old["stale_min"] == 105


def test_a_quoted_cell_reprices_its_own_premium():
    """The chain's IV and Δ are solved server-side so the ladder and the payoff cannot
    disagree. The test of that solve is circular on purpose: feeding the IV back through
    Black-Scholes must return the premium it came from."""
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    t = _t_years(EXP, s.clock)
    spot = s.market.live_chain("NIFTY", EXP)["spot"]
    checked = 0
    for row in s.chain_rows():
        for right in ("ce", "pe"):
            cell = row[right]
            if not cell["quoted"] or cell["iv"] is None:
                continue
            iv = cell["iv"] / 100.0
            repriced = bs.price(spot, row["strike"], t, RISK_FREE, iv, right.upper())
            # IV is rounded to 2dp for display, which is worth a few paise on a ₹600
            # premium — so this is a relative check, not an exact one.
            assert repriced == pytest.approx(cell["ltp"], rel=5e-3)
            assert cell["delta"] == pytest.approx(
                bs.delta(spot, row["strike"], t, RISK_FREE, iv, right.upper()), abs=1e-3)
            checked += 1
    assert checked >= 4


def test_the_pricing_contract_travels_with_the_state():
    """The frontend must never hardcode a second risk-free rate — that is how a chain's Δ
    and a payoff's Δ drift apart on the same screen."""
    store.write_day(DAY, _day())
    p = _open(at="10:00").state()["pricing"]
    assert p["r"] == RISK_FREE and p["expiry_time"] == "15:30" and p["t_floor_s"] == 120.0


def test_the_console_says_what_it_cannot_know():
    """Spot is a parity forward, not an index; the day range is a forming bar; an unquoted
    strike is unquoted. Those are footnotes on the screen, not lore in a docstring."""
    store.write_day(DAY, _day())
    notes = " ".join(_open(at="10:00").state()["notes"]).lower()
    assert "parity" in notes and "lookahead" in notes and "interpolated" in notes


def test_an_unknown_underlying_is_refused_by_name():
    store.write_day(DAY, _day())
    with pytest.raises(ValueError, match="FINNIFTY"):
        _open(underlying="FINNIFTY")


def test_the_registry_caps_and_evicts_the_oldest():
    """A day's tape is ~20 MB of parallel lists and the VPS is a 911 MB swapless box, so
    sessions are capped rather than accumulated."""
    store.write_day(DAY, _day())
    made = [registry.create(underlying="NIFTY", day=DAY, expiry=EXP)
            for _ in range(registry.MAX_SESSIONS + 2)]
    assert len(registry.briefs()) == registry.MAX_SESSIONS
    with pytest.raises(KeyError):
        registry.get(made[0].id)
    assert registry.get(made[-1].id) is made[-1]


def test_the_console_cannot_reach_the_order_path():
    """CLAUDE.md §1 and §8a: this is a replay surface. It must not import the live manager,
    a broker adapter or LiveBroker — not because it would use them, but because a later
    edit could, and nobody would notice until it placed something."""
    import pkgutil

    import skas_algo.services.options_console as pkg

    banned = ("skas_algo.live", "skas_algo.brokers")
    seen = []
    for mod in pkgutil.iter_modules(pkg.__path__):
        src = (pkg.__path__[0] + "/" + mod.name + ".py")
        text = open(src).read()
        seen.append(mod.name)
        for bad in banned:
            assert f"import {bad}" not in text and f"from {bad}" not in text, (
                f"{mod.name} reaches into {bad} — the console is not an order path")
    assert "session" in seen and "registry" in seen


def test_the_strip_and_the_ladder_agree_on_spot():
    """The header's spot and the chain's ATM must come from ONE number. They did not: the
    strip read the NEAREST expiry's parity while the ladder anchored on the SELECTED one,
    so on 2026-08-04 the header said 24,620 and the chain 24,599 — and at the expiry-day
    close the gap printed a 'basis' of −110, which no seven-day future has. What is left is
    the carry we removed, and it is labelled as such rather than as a futures premium we
    have no cash index to measure."""
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    st = s.state()
    assert st["market"]["spot"] == pytest.approx(s.market.live_chain("NIFTY", EXP)["spot"])
    assert st["market"]["carry"] == pytest.approx(
        st["market"]["fut"] - st["market"]["spot"], abs=1e-6)
    assert st["market"]["carry"] > 0        # de-carrying only ever removes carry
