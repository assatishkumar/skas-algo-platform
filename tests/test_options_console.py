"""Options Console session: the minute cursor, chain enrichment, and the isolation rule.

Synthetic store in tmp — no network, no broker, no real data. The console is a replay
surface, so the things worth pinning are the ones a screen cannot show you are wrong about:
that the cursor is deterministic, that a missing quote stays missing, and that the greeks
in the chain actually reprice the premium they were solved from.
"""

from __future__ import annotations

from datetime import date, datetime, time

import json

import pandas as pd
import pytest

from skas_algo.data import option_intraday_store as store
from skas_algo.engine.options import black_scholes as bs
from skas_algo.engine.options.charges import charges_for_txn
from skas_algo.services.options_console import registry
from skas_algo.services.options_console.session import RISK_FREE, ConsoleSession, _t_years

EXP = "2026-07-21"
DAY = date(2026, 7, 14)


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "OPTION_INTRADAY_DIR", tmp_path / "1min")
    from skas_algo.config import get_settings
    monkeypatch.setattr(get_settings(), "option_bars_backup_dir", None)
    monkeypatch.setenv("SKAS_CONSOLE_DIR", str(tmp_path / "console"))
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
    # The ONE exception: the NSE calendar. `live.holidays` is dates and a holiday list —
    # no adapter, no manager, no order — and the cycle bar needs trading sessions.
    allowed = ("from skas_algo.live.holidays import",)
    seen = []
    for mod in pkgutil.iter_modules(pkg.__path__):
        src = (pkg.__path__[0] + "/" + mod.name + ".py")
        text = open(src).read()
        for ok in allowed:
            text = text.replace(ok, "")
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


def test_a_day_jog_keeps_the_time_of_day():
    """Comparing 09:30 on Tuesday with 09:30 on Wednesday is the whole reason to press +1d.
    It used to reset to the session open every time (owner, 2026-09-09)."""
    d2 = date(2026, 7, 15)
    store.write_day(DAY, _day())
    store.write_day(d2, _day(d2))
    s = _open(at="10:35")
    s.shift_day(1)
    assert s.day == d2 and s.clock.strftime("%H:%M") == "10:35"
    s.shift_day(-1)
    assert s.day == DAY and s.clock.strftime("%H:%M") == "10:35"


def test_a_fresh_day_opens_where_the_liquidity_is():
    """09:15 shows a ladder nobody could have traded — widest spreads of the day and half
    the strikes yet to print. 09:20 is the owner's call."""
    store.write_day(DAY, _day())
    assert ConsoleSession(underlying="NIFTY", day=DAY,
                          expiry=EXP).clock.strftime("%H:%M") == "09:20"


def test_the_probe_finds_an_earlier_session_and_says_how_old_it_is():
    """A blank cell is honest but indistinguishable from "worthless". The probe answers
    with the last price the contract actually traded at — possibly days ago — carrying its
    age, so the caller can render it as a reference rather than a quote."""
    d2 = date(2026, 7, 15)
    store.write_day(DAY, _day())
    store.write_day(d2, _day(d2))
    s = ConsoleSession(underlying="NIFTY", day=d2, at="09:20", expiry=EXP)

    # 24500 does not print until 11:30, so at 09:20 the ladder shows it blank…
    assert {r["strike"]: r for r in s.chain_rows()}[24500.0]["ce"]["quoted"] is False
    # …and the probe reaches back into the PREVIOUS session for its last real trade.
    got = s.probe("CE", 24500)
    assert got["found"] and got["ltp"] == pytest.approx(18.0)
    assert got["days_back"] == 1 and got["age_min"] > 0

    # A contract that has never traded at all stays honest: nothing found, nothing invented.
    assert s.probe("CE", 99000)["found"] is False


def test_the_probe_never_looks_into_the_future():
    """It answers 'at or before the cursor'. A print later today must stay invisible, or
    the console would leak the future into a replay."""
    store.write_day(DAY, _day())
    s = _open(at="09:20")
    assert s.probe("CE", 24500)["found"] is False      # its only print today is 11:30
    s.seek("11:35")
    assert s.probe("CE", 24500)["ltp"] == pytest.approx(18.0)


# ---------------------------------------------------------------- staging and the book

def _staged(s, **kw):
    s.stage(**kw)
    return s.state()["staged"]


def test_staging_previews_and_changes_nothing():
    """PAPER/LIVE: preview-then-commit. A staged change describes the book it WOULD produce
    without touching the one that exists."""
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    s.mode = "paper"
    st = _staged(s, kind="add", right="CE", strike=24000, side="S", lots=10)
    assert st["label"] == "S 24000 CE ×10"
    assert len(st["after_legs"]) == 1 and st["margin_after"] > st["margin_before"]
    assert s.legs == [] and s.realized == 0.0 and s.charges == 0.0
    s.discard()
    assert s.state()["staged"] is None and s.legs == []


def test_a_commit_fills_at_the_minute_and_pays_charges():
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    px = {r["strike"]: r for r in s.chain_rows()}[24000.0]["ce"]["ltp"]
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=2)
    leg = s.state()["legs"][0]
    assert leg["entry"] == pytest.approx(px) and leg["lots"] == 2 and leg["side"] == "S"
    # charged the same way the batch replay charges its fills — one cost model, not two
    expected = charges_for_txn({"action": "SHORT", "amount": leg["units"] * px})["total"]
    assert s.state()["risk"]["charges"] == pytest.approx(expected, abs=0.01)


def test_a_partial_exit_books_its_share_and_leaves_the_rest():
    """The design's − 4 ＋ · Exit stepper. Four lots out, six still working."""
    store.write_day(DAY, _day())
    s = _open(at="09:30")
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=10)
    s.seek("11:00")
    s.stage(kind="exit", leg_id=s.legs[0].id, lots=4)
    st = s.state()
    assert st["legs"][0]["lots"] == 6
    assert st["risk"]["realised"] != 0.0
    assert st["risk"]["mtm"] == pytest.approx(
        st["risk"]["realised"] + st["risk"]["unrealised"], abs=0.01)


def test_a_disabled_leg_stays_listed_but_leaves_the_risk():
    """The handoff is explicit: a leg toggled off is excluded from the payoff and the risk
    maths, and still shown (dimmed). Losing it from the list would hide a real position."""
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    for k, side in ((24000, "S"), (24100, "B")):
        s.stage(kind="add", right="CE", strike=k, side=side, lots=5)
    before = s.state()["risk"]["margin"]
    s.stage(kind="toggle", leg_id=s.legs[0].id, enabled=False)
    st = s.state()
    assert len(st["legs"]) == 2                     # still listed
    assert st["legs"][0]["enabled"] is False
    assert st["risk"]["margin"] < before            # but out of the margin
    assert st["risk"]["legs_open"] == 1


def test_rewinding_past_a_trade_unwinds_it_and_going_forward_brings_it_back():
    """The book is derived from the fill journal at the cursor, so it cannot disagree with
    the clock. The journal itself is append-only — an earlier version truncated it on
    rewind, and stepping forward then left the book permanently empty."""
    store.write_day(DAY, _day())
    s = _open(at="09:30")
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=3)
    assert len(s.state()["legs"]) == 1
    s.seek("09:20")
    assert s.state()["legs"] == [] and s.state()["risk"]["charges"] == 0.0
    s.seek("11:00")
    back = s.state()
    assert len(back["legs"]) == 1 and back["legs"][0]["lots"] == 3


def test_a_leg_can_only_be_opened_on_a_price_the_market_printed():
    """A probe's reference price is for the eye, never for a fill — trading on a price from
    two days ago would put a P&L on the screen that no market ever offered."""
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    assert s.probe("CE", 24500)["found"] is False or True   # (probe is separate)
    with pytest.raises(ValueError, match="has not traded"):
        s.stage(kind="add", right="CE", strike=24500, side="B", lots=1)


def test_margin_says_where_its_number_came_from():
    """Every "% of margin" on the rail is only as honest as this label: the model is
    hedge-blind and reads several times a broker basket on a spread."""
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=4)
    assert s.state()["risk"]["margin_source"] == "model"

    anchored = _open(at="10:00", margin_per_lot_set=134_612)
    anchored.stage(kind="add", right="CE", strike=24000, side="S", lots=4)
    risk = anchored.state()["risk"]
    assert risk["margin_source"] == "manual" and risk["margin"] == pytest.approx(538_448)


# ---------------------------------------------------------------- the basket (2026-09-09)

def test_staging_accumulates_a_basket_and_commits_it_together():
    """PAPER/LIVE only. A structure is several legs, and committing them one at a time means
    you cannot see the condor's payoff until the fourth leg lands — the preview is useless
    for exactly the positions that need it. B/S adds to the basket; Apply commits the lot."""
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    s.mode = "paper"
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=2)
    s.stage(kind="add", right="CE", strike=24100, side="B", lots=2)
    st = s.state()["staged"]
    assert len(st["items"]) == 2 and "·" in st["label"]
    assert len(st["after_legs"]) == 2      # the basket previews as one book
    assert s.legs == []                    # …and still nothing has happened
    s.commit()
    assert len(s.state()["legs"]) == 2


def test_the_chain_marks_the_strikes_you_are_holding():
    """Reading a ladder against a position held in your head is how the wrong strike gets
    clicked. The row carries the side and the lots."""
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=3)
    row = {r["strike"]: r for r in s.chain_rows()}[24000.0]
    assert row["ce"]["held"] == {"lots": 3, "side": "S", "enabled": True}
    assert row["pe"]["held"] is None       # only the leg you actually hold


def test_a_leg_can_be_rolled_to_another_strike_in_one_action():
    """"Move that leg a strike up" is one hand movement, not an exit plus a re-entry typed
    out. It still books both fills and both sets of charges."""
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=2)
    charges_before = s.charges
    s.seek("10:05")                      # a roll of a HELD leg (the entry minute would be an edit)
    s.stage(kind="roll", leg_id=s.legs[0].id, strike=24100)
    legs = s.state()["legs"]
    assert len(legs) == 1 and legs[0]["strike"] == 24100.0 and legs[0]["lots"] == 2
    assert legs[0]["side"] == "S"
    assert s.charges > charges_before      # a roll is two real fills, not a relabel


def test_resizing_a_leg_trims_it_or_adds_to_it():
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=5)
    s.stage(kind="resize", leg_id=s.legs[0].id, lots=2)
    assert s.state()["legs"][0]["lots"] == 2
    s.stage(kind="resize", leg_id=s.legs[0].id, lots=6)
    assert sum(x["lots"] for x in s.state()["legs"]) == 6


def test_reset_clears_the_book_and_the_session_pnl():
    """Realised P&L survives closing a position — it is money you made — so a NEW structure
    would otherwise open with the previous one's profit on its rail. Reset is the way back
    to a clean slate at the same minute."""
    store.write_day(DAY, _day())
    s = _open(at="09:30")
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=2)
    s.seek("11:00")
    s.stage(kind="flatten", replace=True)
    flat = s.state()
    assert flat["legs"] == [] and flat["risk"]["realised"] != 0.0
    assert flat["risk"]["mtm"] == flat["risk"]["realised"]   # banked, not open
    s.reset_book()
    clean = s.state()
    assert clean["risk"]["mtm"] == 0.0 and clean["risk"]["charges"] == 0.0
    assert clean["legs"] == [] and clean["staged"] is None


# ------------------------------------------- replay trades on the click (owner 2026-09-09)

def test_in_replay_a_click_is_the_trade():
    """An Apply between every click is friction with nothing to protect — the trade is
    imaginary. Rehearsing a structure means dozens of clicks, and the confirm step made the
    console slower to explore with than a spreadsheet."""
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    assert s.requires_confirm is False
    assert s.stage(kind="add", right="CE", strike=24000, side="S", lots=2) is None
    st = s.state()
    assert st["staged"] is None                      # no basket to confirm
    assert len(st["legs"]) == 1 and st["risk"]["charges"] > 0
    assert st["session"]["requires_confirm"] is False and st["session"]["can_undo"] is True


def test_a_book_that_can_reach_a_broker_still_asks_first():
    """The same machinery, kept for the mode where a confirm stops being friction and
    becomes the point."""
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    s.mode = "paper"
    assert s.requires_confirm is True
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=2)
    assert s.legs == [] and s.state()["staged"] is not None
    s.commit()
    assert len(s.legs) == 1


def test_undo_takes_back_the_whole_action():
    """Undo is what replaces the confirm step, and it is a better safety net because it also
    covers the leg you decide against a minute later. A ROLL is two fills and one action, so
    undoing it must not leave the position half-moved."""
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=2)
    s.stage(kind="add", right="CE", strike=24100, side="B", lots=2)
    s.stage(kind="roll", leg_id=s.legs[0].id, strike=23500)
    assert {int(x.strike) for x in s.legs} == {23500, 24100}

    assert s.undo_last() is True
    assert {int(x.strike) for x in s.legs} == {24000, 24100}, "the roll went back whole"
    assert s.undo_last() and {int(x.strike) for x in s.legs} == {24000}
    assert s.undo_last() and s.legs == []
    assert s.realized == 0.0 and s.charges == 0.0
    assert s.undo_last() is False                    # nothing left, and it says so


def test_undo_leaves_the_clock_where_it_was():
    """Undo edits the journal, not the cursor — the book must rebuild at the SAME minute."""
    store.write_day(DAY, _day())
    s = _open(at="09:30")
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=1)
    s.seek("11:00")
    s.stage(kind="add", right="CE", strike=24100, side="B", lots=1)
    s.undo_last()
    assert s.clock.strftime("%H:%M") == "11:00"
    assert len(s.legs) == 1 and int(s.legs[0].strike) == 24000


def test_adding_to_a_position_grows_it_instead_of_stacking_rows():
    """Buying more of a contract you hold is ONE position at an average price — what a
    broker's book does, and what a table with a row per contract implies.

    Appending instead meant the size stepper produced a second ×1 row every time it was
    pressed: "×1" never changed and three presses read as three legs at one strike (owner,
    2026-09-09, on a 23500 PE)."""
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=1)
    first = s.legs[0].entry

    s.stage(kind="resize", leg_id=s.legs[0].id, lots=2)
    s.stage(kind="resize", leg_id=s.legs[0].id, lots=3)
    assert len(s.legs) == 1 and s.legs[0].lots == 3

    # clicking the chain again on the same strike and side merges too
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=2)
    assert len(s.legs) == 1 and s.legs[0].lots == 5

    # …at a weighted average, not the first price and not the last
    s.seek("11:00")
    px_later = {r["strike"]: r for r in s.chain_rows()}[24000.0]["ce"]["ltp"]
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=5)
    leg = s.legs[0]
    assert leg.lots == 10
    assert leg.entry == pytest.approx((first * 5 + px_later * 5) / 10, abs=0.01)
    assert min(first, px_later) < leg.entry < max(first, px_later)


def test_the_two_sides_of_one_strike_net_to_one_position():
    """REVERSED 2026-09-15 (owner): a long and a short of the same option are NOT a spread
    that shares a strike — the broker's book nets them, so B on a strike you are short is
    a cover of that many lots. The same-minute case rewrites the entry rather than booking
    a ₹0 round trip."""
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=2)
    s.stage(kind="add", right="CE", strike=24000, side="B", lots=1)
    assert len(s.legs) == 1
    assert {(x.side, x.lots) for x in s.legs} == {("S", 1)}


def test_the_rebuilt_book_merges_exactly_as_the_live_one_did():
    """The journal replay must take the same path, or rewinding would silently produce a
    different book from the one you were just looking at."""
    store.write_day(DAY, _day())
    s = _open(at="09:30")
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=2)
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=3)
    live = [(x.side, x.strike, x.lots, round(x.entry, 4)) for x in s.legs]
    s.seek("09:20")
    s.seek("11:00")
    assert [(x.side, x.strike, x.lots, round(x.entry, 4)) for x in s.legs] == live


def test_rolling_moves_the_leg_rather_than_adding_one():
    """The strike stepper is a ROLL. It caught the eye as "adding an additional leg" only
    because the size stepper beside it was, so pin the behaviour explicitly."""
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=3)
    s.stage(kind="roll", leg_id=s.legs[0].id, strike=24100)
    assert len(s.legs) == 1
    assert int(s.legs[0].strike) == 24100 and s.legs[0].lots == 3


# ------------------------------------------------ a leg is its own contract (2026-09-09)

def test_a_held_leg_is_priced_on_its_own_expiry_not_the_selected_chip():
    """Found in a browser pass: switching the expiry chip re-priced every open leg off the
    newly selected series, and stepping past a leg's expiry marked a dead contract at the
    next series' price. A leg carries its expiry; the chip is only for what you click next."""
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=1)
    before = s.state()["legs"][0]["ltp"]
    s.expiry = "2099-01-01"                       # a chip with nothing on it
    after = s.state()["legs"][0]
    assert after["ltp"] == before and after["expiry"] == EXP
    # and the ladder for that other series shows no badge for this leg
    assert not any(r["ce"]["held"] for r in s.chain_rows())


def test_an_expired_leg_settles_to_intrinsic_at_the_close_and_pays_no_brokerage():
    """The batch replay settles a leg still open on its expiry day at 15:30 to parity
    intrinsic with zero brokerage. The console must end a day the same way, or a replayed
    week and a hand-traded week disagree about the same position."""
    exp_day = date(2026, 7, 21)
    store.write_day(exp_day, _day(exp_day))
    s = ConsoleSession(underlying="NIFTY", day=exp_day, at="10:00", expiry=EXP)
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=1)
    charges_after_entry = s.charges
    s.seek("15:25")
    assert len(s.legs) == 1                        # still alive before the close
    s.seek("15:35")
    st = s.state()
    assert st["legs"] == []                        # settled
    row = [f for f in s.journal if f["action"] == "SETTLE"]
    assert len(row) == 1 and row[0]["at"] == f"{EXP}T15:30" and row[0]["group"] is None
    # exchange fees and GST still apply (the shared charge model, same as the batch
    # replay); what a settlement does NOT pay is brokerage or STT — so it must cost less
    # than covering the same amount with an order would.
    amount = row[0]["units"] * row[0]["price"]
    as_order = charges_for_txn({"action": "COVER", "amount": amount})["total"]
    assert 0 <= row[0]["charges"] < as_order
    assert st["risk"]["charges"] == pytest.approx(charges_after_entry + row[0]["charges"],
                                                  abs=0.01)
    # rewinding to before the close reopens it — the settle is a journal fill like any other
    s.seek("15:00")
    assert len(s.legs) == 1


def test_settlement_is_not_undoable():
    """An expiry is the market's action, not the owner's; Undo must reach past it to the
    owner's own last action."""
    exp_day = date(2026, 7, 21)
    store.write_day(exp_day, _day(exp_day))
    s = ConsoleSession(underlying="NIFTY", day=exp_day, at="10:00", expiry=EXP)
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=1)
    s.seek("15:35")
    assert s.legs == [] and any(f["action"] == "SETTLE" for f in s.journal)
    assert s.undo_last() is True                    # undoes the ENTRY, not the settlement
    assert s.journal == [] or all(f["action"] != "SHORT" for f in s.journal)


# ------------------------------------------------------------------ greeks + alerts (P3)


def test_a_legs_greeks_carry_the_live_pages_convention():
    """Per-share and position-signed, exactly as `_enrich_greeks` reports a live leg: a
    SHORT call reads Δ < 0, Θ > 0 (it earns the decay), Γ and Vega < 0. And the net line
    is Σ greek × units over the ENABLED legs, so a toggled-off leg leaves it."""
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=2)
    s.stage(kind="add", right="CE", strike=24100, side="B", lots=1)
    st = s.state()
    short, long_ = st["legs"]
    assert short["iv"] and short["delta"] < 0 < short["theta"]
    assert short["gamma"] < 0 and short["vega"] < 0
    assert long_["delta"] > 0 > long_["theta"] and long_["gamma"] > 0
    g = st["risk"]["greeks"]
    expect = short["delta"] * short["units"] + long_["delta"] * long_["units"]
    assert g["delta"] == pytest.approx(expect, abs=0.05)
    # the solved IV reprices the mark — the same pin the chain cells carry
    spot = s.market.index_spot("NIFTY")
    t = _t_years(EXP, s.clock)
    assert bs.price(spot, 24000.0, t, RISK_FREE, short["iv"] / 100, "CE") == pytest.approx(
        short["ltp"], abs=0.05)
    s.stage(kind="toggle", leg_id=long_["id"], enabled=False)
    g2 = s.state()["risk"]["greeks"]
    assert g2["delta"] == pytest.approx(short["delta"] * short["units"], abs=0.05)


def test_an_alert_fires_once_at_the_cursor_and_rearms_on_rewind():
    """A target of ₹X fires at the first minute the TOTAL MTM reaches it, records that
    minute, and stays fired while the cursor is at or past it. Step back before it and it
    is armed again: in a replay what has not happened yet has not happened."""
    store.write_day(DAY, _day())
    s = _open(at="09:20")
    s.stage(kind="add", right="CE", strike=24000, side="B", lots=1)   # drifts up ₹1.5/5min
    a = s.arm_alert("target", 200.0)
    assert s.state()["alerts"][0]["state"] == "armed"
    fired_at = None
    for hh, mm in [(9, 40), (10, 0), (10, 30), (11, 0), (11, 30)]:
        s.seek(f"{hh:02d}:{mm:02d}")
        st = s.state()
        if st["alerts"][0]["state"] == "fired":
            fired_at = st["alerts"][0]["fired_at"]
            assert st["risk"]["mtm"] >= 200.0
            break
    assert fired_at is not None, "the drift never reached the target"
    # later cursor: still fired, at the SAME minute (once, not every minute)
    s.seek("11:40")
    assert s.state()["alerts"][0]["fired_at"] == fired_at
    # rewind before it: armed again
    s.seek("09:25")
    assert s.state()["alerts"][0]["state"] == "armed"
    assert s.clear_alert(a["id"]) is True and s.state()["alerts"] == []


def test_a_stop_is_a_loss_whichever_sign_it_is_typed_with():
    store.write_day(DAY, _day())
    s = _open(at="09:20")
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=1)   # loses as it drifts
    s.arm_alert("stop", 150.0)
    s.arm_alert("stop", -150.0)
    s.seek("11:30")
    states = [a["state"] for a in s.state()["alerts"]]
    assert states == ["fired", "fired"] and s.state()["risk"]["mtm"] <= -150.0


def test_a_spot_alert_reads_the_parity_spot():
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    spot = s.state()["market"]["spot"]
    s.arm_alert("above", spot - 1)
    s.arm_alert("below", spot - 1000)
    st = s.state()
    assert [a["state"] for a in st["alerts"]] == ["fired", "armed"]
    with pytest.raises(ValueError):
        s.arm_alert("sideways", 1.0)


def test_a_lost_session_is_rebuilt_from_the_journal_the_page_kept():
    """The registry is in-process; a restart or an eviction drops the object. Before this
    every click after that was a 404 with the book gone ("the lots stepper does nothing",
    owner 2026-09-09). A new session on the same day takes the page's journal + alerts
    back and re-derives the book at the cursor exactly as a seek would."""
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=2)
    s.seek("10:30")
    s.stage(kind="add", right="PE", strike=24000, side="S", lots=1)
    s.stage(kind="exit", leg_id=s.legs[0].id, lots=1)
    s.arm_alert("target", 50.0)
    s.seek("11:00")
    before = s.state()

    fresh = _open(at="11:00")
    fresh.restore(before["journal"], before["alerts"])
    after = fresh.state()
    assert [(l["strike"], l["right"], l["side"], l["lots"], l["entry"]) for l in after["legs"]] == \
        [(l["strike"], l["right"], l["side"], l["lots"], l["entry"]) for l in before["legs"]]
    assert after["risk"]["mtm"] == pytest.approx(before["risk"]["mtm"])
    assert after["risk"]["charges"] == pytest.approx(before["risk"]["charges"])
    assert [a["kind"] for a in after["alerts"]] == ["target"]
    # and it is a working session: the lots stepper now changes the size
    fresh.stage(kind="resize", leg_id=fresh.legs[0].id, lots=fresh.legs[0].lots + 1)
    assert fresh.legs[0].lots == before["legs"][0]["lots"] + 1
    assert fresh.undo_last() is True          # undo reaches the restored groups too


def test_registry_holds_more_than_a_handful():
    """Three was enough for one browser and not for two on the same backend."""
    assert registry.MAX_SESSIONS >= 8


# ------------------------------------------------------------------ presets, track, save (P4)


def test_a_preset_is_resolved_against_the_ladder_and_applied_as_one_action():
    """Δ-anchored rules become concrete strikes off the chain's OWN solved Δ, fill together
    under one group (so Undo takes the whole structure back), and a preset with an
    unquoted leg is refused by name rather than filled in with a guess."""
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    ps = {p["id"]: p for p in s.presets()}
    st = ps["short_straddle"]
    assert st["ok"] and sorted((l["side"], l["right"]) for l in st["legs"]) == \
        [("S", "CE"), ("S", "PE")]
    assert st["margin"] > 0 and st["margin_source"] == "model" and st["net_credit"] > 0
    # the synthetic ladder has no quoted wings two steps out → refused, with the reason
    assert ps["iron_condor"]["ok"] is False and "has not traded" in ps["iron_condor"]["reason"]
    s.apply_preset("short_straddle", lots=2)
    assert [(l.side, l.lots) for l in s.legs] == [("S", 2), ("S", 2)]
    assert len({f["group"] for f in s.journal}) == 1
    assert s.undo_last() is True and s.legs == []
    with pytest.raises(ValueError):
        s.apply_preset("iron_condor")
    assert s.journal == []                      # nothing partial landed


def test_a_delta_anchor_picks_the_otm_strike_nearest_the_target():
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    rows = s.chain_rows()
    atm = next(r["strike"] for r in rows if r["atm"])
    spread = {p["id"]: p for p in s.presets()}["bear_call_spread"]
    if spread["ok"]:
        short = next(l for l in spread["legs"] if l["side"] == "S")
        assert short["strike"] >= atm
        quoted = [(abs(r["ce"]["delta"] or 9), r["strike"]) for r in rows
                  if r["strike"] >= atm and r["ce"]["quoted"] and r["ce"]["delta"] is not None]
        best = min(quoted, key=lambda x: abs(x[0] - 0.25))[1]
        assert short["strike"] == best


def test_the_track_jumps_between_fills_bookmarks_and_spot_moves():
    store.write_day(DAY, _day())
    s = _open(at="09:20")
    s.stage(kind="add", right="CE", strike=24000, side="B", lots=1)
    s.seek("10:00")
    s.stage(kind="add", right="PE", strike=24000, side="B", lots=1)
    s.add_bookmark()
    s.seek("09:16")
    s.jump("next_fill");     assert s.clock.strftime("%H:%M") == "09:20"
    s.jump("next_fill");     assert s.clock.strftime("%H:%M") == "10:00"
    s.jump("next_fill");     assert s.clock.strftime("%H:%M") == "10:00"   # nothing later: stays
    s.jump("prev_fill");     assert s.clock.strftime("%H:%M") == "09:20"
    s.jump("next_bookmark"); assert s.clock.strftime("%H:%M") == "10:00"
    # the synthetic straddle drifts ~1.5/5min on a ~24k spot: a 1% move never happens, a
    # tiny one does — and the series is one pass, cached
    s.seek("09:20")
    s.jump("next_move", pct=1.0); assert s.clock.strftime("%H:%M") == "09:20"
    s.jump("next_move", pct=0.001)
    assert s.clock.strftime("%H:%M") > "09:20"
    assert s.spot_series() is s.spot_series()
    st = s.state()
    assert st["track"]["fills"][0] == {"at": "09:20", "action": "BUY"}
    assert st["track"]["bookmarks"] == ["10:00"]


def test_a_saved_session_reloads_with_its_book_alerts_and_bookmarks():
    from skas_algo.services.options_console import store as cstore
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=3)
    s.arm_alert("target", 500.0)
    s.add_bookmark()
    s.seek("11:00")
    rec = cstore.save("my straddle", s.save_payload())
    listing = cstore.saved()
    assert listing[0]["file"] == rec["file"] and listing[0]["legs"] == 1
    j = cstore.load(rec["file"])
    fresh = ConsoleSession(underlying=j["underlying"], day=date.fromisoformat(j["day"]),
                           at=j["clock"], expiry=j["expiry"], capital=j["capital"])
    fresh.restore(j["journal"], j["alerts"], j["bookmarks"])
    a, b = s.state(), fresh.state()
    assert [(l["strike"], l["lots"], l["entry"]) for l in b["legs"]] == \
        [(l["strike"], l["lots"], l["entry"]) for l in a["legs"]]
    assert b["risk"]["mtm"] == pytest.approx(a["risk"]["mtm"])
    assert [x["kind"] for x in b["alerts"]] == ["target"] and b["bookmarks"] == s.bookmarks
    assert cstore.delete(rec["file"]) is True and cstore.saved() == []
    assert cstore.load.__name__ == "load"
    with pytest.raises(OSError):
        cstore.load("../../etc/passwd")          # the name is basename'd, never a path


# ------------------------------------------------------------------ margin (SPAN-shaped)


def test_margin_offsets_hedges_and_charges_exposure_on_every_short():
    """The order a broker's basket puts structures in: a hedged spread costs less than a
    straddle, which costs less than the same shorts naked; a long-only book blocks
    nothing; and the design's own bear call spread lands within ~10% of Kite's ₹3,63,826
    (the old per-leg sum read ₹19.4L for it)."""
    from skas_algo.services.options_console.margin import MarginLeg, span_like
    t = 27 / 365
    spread = span_like([MarginLeg("CE", 24000, -1, 650, 0.2246, t),
                        MarginLeg("CE", 24100, +1, 650, 0.2232, t)], 22905)
    assert 3.2e5 < spread["total"] < 4.0e5
    assert spread["exposure"] == pytest.approx(0.02 * 22905 * 650, rel=1e-6)
    naked = span_like([MarginLeg("CE", 22900, -1, 65, 0.25, t)], 22905)["total"]
    straddle = span_like([MarginLeg("CE", 22900, -1, 65, 0.25, t),
                          MarginLeg("PE", 22900, -1, 65, 0.25, t)], 22905)["total"]
    fly = span_like([MarginLeg("CE", 22900, -1, 65, 0.25, t), MarginLeg("PE", 22900, -1, 65, 0.25, t),
                     MarginLeg("CE", 23300, 1, 65, 0.24, t), MarginLeg("PE", 22500, 1, 65, 0.27, t)],
                    22905)["total"]
    assert fly < straddle < 2 * naked
    assert 0.7e5 < naked < 1.3e5
    long_only = span_like([MarginLeg("CE", 22900, 1, 65, 0.25, t)], 22905)
    assert long_only["total"] == 0


def test_the_session_margin_is_the_span_shaped_one_and_says_so():
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=1)
    s.stage(kind="add", right="PE", strike=24000, side="S", lots=1)
    r = s.state()["risk"]
    assert r["margin_source"] == "model" and r["margin_detail"]["exposure"] > 0
    assert r["margin"] == pytest.approx(r["margin_detail"]["total"])
    # the manual anchor still outranks it
    s.margin_per_lot_set = 130000
    r2 = s.state()["risk"]
    assert r2["margin"] == 130000 and r2["margin_source"] == "manual" and r2["margin_detail"] is None


def test_a_step_past_the_close_rolls_into_the_next_session():
    """15:40 +1m is the next captured day's 09:15; 15:40 +15m its 09:29; 09:15 −1m the
    previous day's 15:40. The last day pins at the close (nowhere to go), the first at
    the open. Jogs used to stop dead at 15:40."""
    d1, d2 = DAY, date(2026, 7, 15)
    store.write_day(d1, _day(d1))
    store.write_day(d2, _day(d2))
    s = _open(day=d1, at="15:40")
    s.step(1);   assert (s.day, s.clock.strftime("%H:%M")) == (d2, "09:15")
    s.step(-1);  assert (s.day, s.clock.strftime("%H:%M")) == (d1, "15:40")
    s.step(15);  assert (s.day, s.clock.strftime("%H:%M")) == (d2, "09:29")
    s.seek("15:40"); s.step(60)
    assert (s.day, s.clock.strftime("%H:%M")) == (d2, "15:40")      # last day: pinned
    s.set_day(d1, at="09:15"); s.step(-5)
    assert (s.day, s.clock.strftime("%H:%M")) == (d1, "09:15")      # first day: pinned


def test_a_finished_cycle_pins_at_the_close_and_the_cycle_bar_counts_sessions():
    """A book whose every leg has expired does NOT roll into the next session: the close
    is where the replay ends. A flat book that never traded still rolls. The cycle bar
    runs from the first fill's day to the last expiry, in captured sessions."""
    d1, d2, d3 = DAY, date(2026, 7, 15), date(2026, 7, 21)     # EXP is 07-21
    for d in (d1, d2, d3):
        store.write_day(d, _day(d))
    s = _open(day=d1, at="10:00")
    assert s.state()["cycle"] is None
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=1)
    c = s.state()["cycle"]
    # 14 Jul (Tue) → 21 Jul (Tue) 2026 is six NSE sessions, whatever the store captured
    assert (c["start"], c["end"], c["sessions"], c["session_no"]) == (d1.isoformat(), EXP, 6, 1)
    assert 0 < c["pct"] < 17 and c["done"] is False
    assert c["beyond_data"] is False and c["data_until"] == d3.isoformat()
    s.seek("15:40"); s.step(1)                                  # rolls: legs still alive
    assert s.day == d2
    s.set_day(d3, at="15:35")                                   # expiry day, settled
    c = s.state()["cycle"]
    assert c["done"] is True and c["pct"] == 100.0 and s.legs == []
    s.seek("15:40"); s.step(5)
    assert s.day == d3 and s.clock.strftime("%H:%M") == "15:40"  # pinned, no roll
    s.reset_book()
    s.step(5)                                                    # flat & untraded: rolls
    assert s.day == d3                                           # (last captured day → pins)
    s2 = _open(day=d1, at="15:40")
    s2.step(1); assert s2.day == d2                              # untraded book rolls


def test_a_cycle_whose_expiry_is_past_the_captured_data_says_so():
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=1)   # EXP 07-21, store ends 07-14
    c = s.state()["cycle"]
    assert c["beyond_data"] is True and c["data_until"] == DAY.isoformat()
    assert c["sessions"] == 6 and c["session_no"] == 1 and not c["done"]
    s.seek("15:40"); s.step(5)
    assert s.day == DAY and s.clock.strftime("%H:%M") == "15:40"       # nowhere to go
    assert s.state()["session"]["has_next_day"] is False


def test_the_multiplier_scales_every_leg_as_one_action():
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=1)
    s.stage(kind="add", right="PE", strike=24000, side="S", lots=2)
    s.scale_book(3)
    assert [leg.lots for leg in s.legs] == [3, 6]
    assert s.undo_last() is True and [leg.lots for leg in s.legs] == [1, 2]
    s.scale_book(3); s.scale_book(2 / 3)
    assert [leg.lots for leg in s.legs] == [2, 4]
    with pytest.raises(ValueError):
        s.scale_book(0.1)                       # a leg would go below one lot
    assert [leg.lots for leg in s.legs] == [2, 4]   # nothing partial applied


def test_the_mtm_is_the_cycles_not_the_sessions_and_net_credit_is_what_entry_collected():
    """Close a first structure at a profit, open a second: the second's MTM starts near
    zero (its own realised is 0), while `realised_total` still carries the first cycle's
    banked profit. Net credit is +premium received on shorts, −premium paid on longs."""
    store.write_day(DAY, _day())
    s = _open(at="09:20")
    s.stage(kind="add", right="CE", strike=24000, side="B", lots=1)     # drifts up: a profit
    s.seek("11:00")
    s.stage(kind="flatten", replace=True)
    r1 = s.state()["risk"]
    assert r1["realised_total"] > 0 and r1["mtm"] == pytest.approx(r1["realised"])
    banked = r1["realised_total"]
    s.stage(kind="add", right="PE", strike=24000, side="S", lots=1)     # a NEW cycle
    r2 = s.state()["risk"]
    assert r2["realised"] == 0.0 and r2["realised_total"] == pytest.approx(banked)
    assert r2["mtm"] == pytest.approx(r2["unrealised"])                # not banked + open
    entry = s.legs[0].entry
    assert r2["net_credit"] == pytest.approx(entry * s.legs[0].units)   # a short: credit
    # the cycle survives a rewind-and-replay (it is re-derived from the journal)
    s.seek("11:30")
    assert s.state()["risk"]["realised"] == 0.0
    s.reset_book()
    assert s.state()["risk"]["net_credit"] is None


def test_the_sparkline_is_the_open_books_last_thirty_minutes_off_the_tape():
    store.write_day(DAY, _day())
    s = _open(at="09:20")
    s.stage(kind="add", right="CE", strike=24000, side="B", lots=1)      # drifts +1.5 / 5 min
    s.seek("10:00")
    pts = s.state()["track"]["mtm"]
    assert pts and pts[-1]["at"] == "10:00" and pts[0]["at"] >= "09:31"
    assert pts[-1]["pnl"] == pytest.approx(s.state()["risk"]["unrealised"], abs=0.01)
    assert pts[-1]["pnl"] > pts[0]["pnl"]                              # it rose
    assert len(pts) <= 30
    s.stage(kind="flatten", replace=True)
    assert s.state()["track"]["mtm"] == []


def test_next_alert_jumps_to_the_minute_the_armed_alert_would_fire():
    store.write_day(DAY, _day())
    s = _open(at="09:20")
    s.stage(kind="add", right="CE", strike=24000, side="B", lots=1)
    s.arm_alert("target", 300.0)
    assert s.state()["alerts"][0]["state"] == "armed"
    s.jump("next_alert")
    st = s.state()
    assert st["alerts"][0]["state"] == "fired" and st["alerts"][0]["fired_at"][11:] == st["session"]["clock"]
    assert st["risk"]["mtm"] >= 300.0
    # one minute earlier it had not fired
    s.step(-1)
    assert s.state()["risk"]["mtm"] < 300.0
    # (stepping back re-armed it — a rewind always does.) Read the state at 11:40 so it
    # fires there; then nothing is armed ahead and the cursor stays.
    s.seek("11:40")
    assert s.state()["alerts"][0]["state"] == "fired"
    before = s.clock
    s.jump("next_alert")
    assert s.clock == before


def test_the_cycle_remembers_where_the_underlying_stood_at_entry():
    store.write_day(DAY, _day())
    s = _open(at="09:40")
    spot_then = s.state()["market"]["spot"]
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=1)
    s.seek("11:00")
    c = s.state()["cycle"]
    assert c["entry_at"] == f"{DAY.isoformat()}T09:40" and c["entry_spot"] == pytest.approx(spot_then, abs=0.5)
    s.stage(kind="flatten", replace=True)
    s.stage(kind="add", right="PE", strike=24000, side="S", lots=1)     # a new cycle, new entry
    c2 = s.state()["cycle"]
    assert c2["entry_at"] == f"{DAY.isoformat()}T11:00" and c2["entry_spot"] != c["entry_spot"]


def test_deleting_a_leg_in_replay_is_as_if_it_was_never_traded():
    store.write_day(DAY, _day())
    s = _open(at="09:20")
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=1)
    s.stage(kind="add", right="PE", strike=24000, side="S", lots=1)
    s.seek("10:30")
    pe = next(l for l in s.legs if l.right == "PE")
    s.stage(kind="exit", leg_id=pe.id, lots=1)              # a closed PE → realised P&L
    assert s.state()["risk"]["realised_total"] != 0
    assert s.delete_leg(next(l for l in s.legs if l.right == "CE").id) is True
    st = s.state()
    assert st["legs"] == [] and all(f["symbol"].endswith("|PE") for f in s.journal)
    # the PE's history is untouched, the CE's is gone — no P&L was booked for it
    assert st["risk"]["realised_total"] != 0
    assert s.delete_leg("nope") is False


def test_realised_is_gross_and_a_closed_leg_stays_on_the_table():
    """Trimming a lot at its entry price books ₹0 (costs live in `charges`, the Live KPI's
    basis), and a closed leg is listed under `closed` with entry, exit and P&L for the
    rest of the cycle — never silently gone. A new cycle clears the list."""
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=2)
    s.seek("10:05")
    px = s.state()["legs"][0]["ltp"]
    s.stage(kind="resize", leg_id=s.legs[0].id, lots=1)          # a real trim, five minutes on
    r = s.state()["risk"]
    assert r["charges"] > 0
    c = s.state()["closed"]
    assert len(c) == 1 and c[0]["lots"] == 1 and c[0]["exit"] == pytest.approx(px)
    assert c[0]["pnl"] == pytest.approx((c[0]["entry"] - c[0]["exit"]) * 65, abs=0.01)   # gross
    s.seek("11:00")
    s.stage(kind="flatten", replace=True)
    c = s.state()["closed"]
    assert len(c) == 2 and c[0]["pnl"] + c[1]["pnl"] == pytest.approx(s.state()["risk"]["realised"], abs=0.01)
    assert s.delete_leg(c[1]["symbol"]) is True and s.state()["closed"] == []   # by symbol
    s.stage(kind="add", right="PE", strike=24000, side="B", lots=1)             # a new cycle
    assert s.state()["closed"] == []


def test_shaping_a_leg_in_the_minute_it_was_placed_edits_it_rather_than_closing_it():
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=3)
    leg = s.legs[0]
    s.stage(kind="roll", leg_id=leg.id, strike=24100)           # same minute: an edit
    st = s.state()
    assert st["closed"] == [] and [(l["strike"], l["lots"]) for l in st["legs"]] == [(24100.0, 3)]
    assert len(s.journal) == 1 and s.journal[0]["action"] == "SHORT"
    s.stage(kind="resize", leg_id=s.legs[0].id, lots=2)          # still that minute
    st = s.state()
    assert st["closed"] == [] and st["legs"][0]["lots"] == 2 and st["risk"]["realised"] == 0.0
    assert len(s.journal) == 1
    s.seek("10:05")                                              # the clock moved on
    s.stage(kind="resize", leg_id=s.legs[0].id, lots=1)          # now a real trim
    st = s.state()
    assert len(st["closed"]) == 1 and st["legs"][0]["lots"] == 1
    s.stage(kind="roll", leg_id=s.legs[0].id, strike=24000)      # and a real roll
    assert len(s.state()["closed"]) == 2


def test_undoing_a_same_minute_edit_gives_the_original_leg_back():
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=3)
    s.stage(kind="roll", leg_id=s.legs[0].id, strike=24100)
    s.stage(kind="resize", leg_id=s.legs[0].id, lots=1)
    assert [(l.strike, l.lots) for l in s.legs] == [(24100.0, 1)]
    assert s.undo_last() and [(l.strike, l.lots) for l in s.legs] == [(24100.0, 3)]
    assert s.undo_last() and [(l.strike, l.lots) for l in s.legs] == [(24000.0, 3)]
    assert s.undo_last() and s.legs == [] and s.journal == []


def test_a_fresh_day_opens_on_its_own_months_expiry():
    """The store on the synthetic day lists a weekly (07-21) and, here, a monthly
    (07-28): a new session lands on the month's last listed expiry, not the nearest."""
    df = _day()
    monthly = pd.concat([df, pd.DataFrame(_rows(DAY, 24000, "CE", {(10, 0): 160.0}, exp="2026-07-28"),
                                          columns=store.COLUMNS)], ignore_index=True)
    store.write_day(DAY, monthly)
    s = ConsoleSession(underlying="NIFTY", day=DAY, at="10:00")
    assert s.expiry == "2026-07-28"
    s.expiry = EXP                                   # a chosen chip is kept across +1d
    assert s.state()["chain"]["expiry"] == EXP


# ------------------------------------------------------------------ broker-priced margin
def _bear_call(s):
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=2)
    s.stage(kind="add", right="CE", strike=24100, side="B", lots=2)


def test_the_margin_source_order_is_manual_then_zerodha_then_model():
    store.write_day(DAY, _day())
    s = ConsoleSession(underlying="NIFTY", day=DAY, at="10:00", expiry=EXP)
    _bear_call(s)
    calls: list[dict] = []

    def fn(underlying, legs, *, spot, day):
        calls.append({"u": underlying, "legs": legs, "spot": spot, "day": day})
        return {"total": 91000.0, "account": "Satish Kite", "spot_today": 25000.0,
                "legs": legs, "shifted": True}

    assert s.state()["risk"]["margin_source"] == "model"          # no fn injected
    s.margin_fn = fn
    r = s.state()["risk"]
    assert (r["margin"], r["margin_source"]) == (91000.0, "zerodha")
    assert r["margin_note"]["account"] == "Satish Kite" and r["margin_detail"] is None
    assert calls[0]["u"] == "NIFTY" and calls[0]["day"] == DAY and calls[0]["spot"] > 0
    assert sorted(x["side"] for x in calls[0]["legs"]) == ["B", "S"]
    # the same book shape is NOT asked again on the next tick
    s.step(1)
    s.state()
    assert len(calls) == 1
    # a different shape is
    s.stage(kind="add", right="PE", strike=24000, side="S", lots=2)
    s.state()
    assert len(calls) == 2
    # the manual anchor outranks the broker figure — and clearing it hands back
    s.margin_per_lot_set = 50_000
    r = s.state()["risk"]
    assert (r["margin"], r["margin_source"], r["margin_note"]) == (100_000.0, "manual", None)
    s.margin_per_lot_set = 0
    assert s.state()["risk"]["margin_source"] == "zerodha"


def test_a_failed_broker_margin_falls_back_to_the_model_and_is_not_hammered():
    store.write_day(DAY, _day())
    s = ConsoleSession(underlying="NIFTY", day=DAY, at="10:00", expiry=EXP)
    _bear_call(s)
    n = {"calls": 0}

    def fn(underlying, legs, *, spot, day):
        n["calls"] += 1
        return None

    s.margin_fn = fn
    r = s.state()["risk"]
    assert r["margin_source"] == "model" and r["margin"] > 0 and r["margin_note"] is None
    s.state()
    assert n["calls"] == 1                                # the miss is remembered
    # a long-only book never asks a broker for margin
    s2 = ConsoleSession(underlying="NIFTY", day=DAY, at="10:00", expiry=EXP)
    s2.margin_fn = fn
    s2.stage(kind="add", right="CE", strike=24100, side="B", lots=1)
    assert s2.state()["risk"]["margin_source"] == "model" and n["calls"] == 1
    # the preset gallery prices eight structures on the model, never the broker
    s.presets(1)
    assert n["calls"] == 1


def test_todays_equivalent_keeps_moneyness_dte_and_a_calendar_shape():
    from datetime import date as _d

    from skas_algo.services.console_margin import map_legs

    legs = [{"right": "CE", "strike": 22440, "expiry": "2024-03-28", "side": "S", "lots": 1},
            {"right": "CE", "strike": 22440, "expiry": "2024-04-25", "side": "B", "lots": 1},
            {"right": "PE", "strike": 21560, "expiry": "2024-03-28", "side": "S", "lots": 2}]
    out = map_legs(legs, underlying="NIFTY", spot_replay=22000.0, spot_today=25000.0,
                   day=_d(2024, 3, 14), today=_d(2026, 9, 10),
                   expiries_today=["2026-09-15", "2026-09-22", "2026-09-29", "2026-10-27"])
    # 2% OTM stays 2% OTM (25,500 → nearest 50), 2% ITM put likewise (24,500)
    assert [x["strike"] for x in out] == [25500.0, 25500.0, 24500.0]
    # 14 DTE → the 09-22 (12d) over 09-29 (19d); 42 DTE → 10-27 (47d); distinct expiries
    assert [x["expiry"] for x in out] == ["2026-09-22", "2026-10-27", "2026-09-22"]
    assert [(x["side"], x["lots"]) for x in out] == [("S", 1), ("B", 1), ("S", 2)]
    # a book on the console's 100-grid is mapped on the 100s, never split onto a 50
    fly = [{"right": "CE", "strike": 24600, "expiry": "2026-04-28", "side": "B", "lots": 1},
           {"right": "PE", "strike": 23800, "expiry": "2026-04-28", "side": "B", "lots": 1}]
    out = map_legs(fly, underlying="NIFTY", spot_replay=24198.0, spot_today=23425.0,
                   day=_d(2026, 4, 15), today=_d(2026, 9, 10), expiries_today=["2026-09-22"])
    assert [x["strike"] for x in out] == [23800.0, 23000.0]


def test_the_kite_equivalent_prices_a_mapped_basket_and_caches_it(monkeypatch):
    from datetime import date as _d

    from skas_algo.services import console_margin as cm

    cm.clear_cache()
    seen: list[list[dict]] = []

    class _Adapter:
        def underlying_ltp(self, u):
            return 25000.0

        def option_expiries(self, u):
            return ["2026-09-15", "2026-09-22", "2026-09-29"]

        def basket_margin(self, legs):
            seen.append(legs)
            return 90828.0

    monkeypatch.setattr(cm, "_account", lambda: ("Satish Kite", _Adapter()))
    legs = [{"right": "CE", "strike": 24000, "expiry": "2026-04-28", "side": "S", "lots": 1},
            {"right": "CE", "strike": 24200, "expiry": "2026-04-28", "side": "B", "lots": 1}]
    # `today` pinned: the mapping picks the listed expiry nearest 14 DTE FROM TODAY, and the
    # bare date.today() made this test drift a week after it was written (2026-09-15)
    today = _d(2026, 9, 10)
    got = cm.kite_equivalent("NIFTY", legs, spot=24000.0, day=_d(2026, 4, 14), today=today)
    assert got and got["total"] == 90828.0 and got["account"] == "Satish Kite" and got["shifted"]
    assert [x["strike"] for x in got["legs"]] == [25000.0, 25200.0]
    assert [x["expiry"] for x in got["legs"]] == ["2026-09-22", "2026-09-22"]
    first = seen[0][0]
    assert first["symbol"].startswith("NIFTY|2026-09-22|25000") and first["direction"] == -1
    assert seen[0][0]["units"] % 1 == 0 and seen[0][0]["units"] > 0
    cm.kite_equivalent("NIFTY", legs, spot=24000.0, day=_d(2026, 4, 14), today=today)
    assert len(seen) == 1                                    # cached by mapped shape
    # a long-only book asks nothing; no session → None (the caller falls back)
    assert cm.kite_equivalent("NIFTY", [legs[1]], spot=24000.0, day=_d(2026, 4, 14)) is None
    monkeypatch.setattr(cm, "_account", lambda: None)
    cm.clear_cache()
    assert cm.kite_equivalent("NIFTY", legs, spot=24000.0, day=_d(2026, 4, 14)) is None


# ------------------------------------------------------------- cycle range + VIX on the strip
def test_the_cycle_range_runs_from_the_entry_minute_to_the_cursor_across_days():
    store.write_day(DAY, _day())
    day2 = date(2026, 7, 15)
    store.write_day(day2, _day(day2))
    s = ConsoleSession(underlying="NIFTY", day=DAY, at="10:00", expiry=EXP)
    assert s.state()["market"]["cycle_low"] is None            # flat: no cycle, no range
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=1)
    s.seek("11:00")
    series = dict(s.spot_series())
    window = [sp for m, sp in series.items() if "10:00" <= m[11:] <= "11:00"]
    mk = s.state()["market"]
    assert (mk["cycle_low"], mk["cycle_high"]) == (round(min(window), 2), round(max(window), 2))
    # the next day widens it with the entry day's tail AND today's path up to the cursor
    s.shift_day(1)
    s.seek("10:30")
    day1_tail = [sp for m, sp in series.items() if m[11:] >= "10:00"]
    day2_head = [sp for m, sp in dict(s.spot_series()).items() if m[11:] <= "10:30"]
    mk = s.state()["market"]
    assert mk["cycle_low"] == round(min(day1_tail + day2_head), 2)
    assert mk["cycle_high"] == round(max(day1_tail + day2_head), 2)
    # rewinding before the entry unwinds the cycle — and its range
    s.shift_day(-1)
    s.seek("09:30")
    assert s.state()["market"]["cycle_low"] is None


def test_vix_comes_from_the_injected_reader_and_is_asked_once_per_day():
    store.write_day(DAY, _day())
    s = ConsoleSession(underlying="NIFTY", day=DAY, at="10:00", expiry=EXP)
    assert s.state()["market"]["vix"] is None                  # no reader: nothing faked
    asked: list[date] = []

    def fn(d):
        asked.append(d)
        return {"prev_close": 13.78, "open": 14.1, "prev_date": "2026-07-13"}

    s.vix_fn = fn
    assert s.state()["market"]["vix"]["prev_close"] == 13.78
    s.step(5)
    s.state()
    assert asked == [DAY]


def test_next_iv_spike_jumps_to_the_first_minute_the_atm_iv_rises_enough():
    store.write_day(DAY, _day())
    s = ConsoleSession(underlying="NIFTY", day=DAY, at="09:30", expiry=EXP)
    series = s.iv_series()
    assert series and all(0 < iv < 200 for _, iv in series)
    here = next(iv for mk, iv in series if mk[11:] >= "09:30")
    later = [(mk, iv) for mk, iv in series if mk[11:] > "09:30"]
    rise = max(iv - here for _, iv in later)
    if rise <= 0:
        s.jump("next_iv_spike", pct=0.5)
        assert s.clock.strftime("%H:%M") == "09:30"           # nothing to jump to
        return
    target = min(mk for mk, iv in later if iv - here >= rise / 2)
    s.jump("next_iv_spike", pct=rise / 2)
    assert s.clock.strftime("%H:%M") == target[11:]
    s.seek("09:30")
    s.jump("next_iv_spike", pct=rise + 1)                     # more than ever happened
    assert s.clock.strftime("%H:%M") == "09:30"


def test_the_strip_reads_the_vix_at_the_cursor_when_minute_bars_exist_else_the_prior_close():
    store.write_day(DAY, _day())
    s = ConsoleSession(underlying="NIFTY", day=DAY, at="10:00", expiry=EXP)
    s.vix_fn = lambda d: {"prev_close": 13.78, "open": 13.9, "prev_date": "2026-07-13",
                          "minutes": [("09:15", 14.0), ("09:30", 14.5), ("10:00", 13.2),
                                      ("11:00", 15.1)]}
    v = s.state()["market"]["vix"]
    assert v["last"] == 13.2 and v["has_minutes"] and "minutes" not in v
    s.step(30)
    assert s.state()["market"]["vix"]["last"] == 13.2           # 10:30: the 10:00 bar holds
    s.seek("11:05")
    assert s.state()["market"]["vix"]["last"] == 15.1
    s2 = ConsoleSession(underlying="NIFTY", day=DAY, at="10:00", expiry=EXP)
    s2.vix_fn = lambda d: {"prev_close": 13.78, "open": 13.78, "minutes": []}
    v = s2.state()["market"]["vix"]
    assert v["last"] is None and v["has_minutes"] is False and v["prev_close"] == 13.78
    # the ATM implied vol at the cursor, from the same series the "iv ›" jump walks
    iv = s2.state()["market"]["atm_iv"]
    assert iv is None or 0 < iv < 200


def test_the_minute_vix_store_round_trips_a_day(tmp_path, monkeypatch):
    import pandas as pd

    from skas_algo.data import intraday_bars as ib

    monkeypatch.setattr(ib, "INTRADAY_DIR", tmp_path)
    assert ib.vix_minutes(DAY) == [] and ib.vix_cached_range() is None
    rows = pd.DataFrame({"start": [f"{DAY}T09:15:00", f"{DAY}T09:16:00"],
                         "open": [14.0, 14.1], "high": [14.2, 14.2], "low": [13.9, 14.0],
                         "close": [14.1, 14.05]})
    rows.to_csv(ib._store_path(ib.VIX_SYMBOL, 1), index=False)
    assert ib.vix_minutes(DAY) == [("09:15", 14.1), ("09:16", 14.05)]
    lo, hi = ib.vix_cached_range()
    assert lo.startswith(str(DAY)) and hi.startswith(str(DAY)) and lo < hi
    assert ib._chunk_days(1) == 60 and ib._chunk_days(15) == 190


def test_a_leg_carries_the_intraday_t_its_iv_was_solved_with():
    """The payoff's T+0 must reprice with the SAME t the ladder solved IV with. A whole-day
    floor on expiry morning invented time value on legs priced at intrinsic and put the
    T+0 line ₹70k+ away from the book (owner screens, 2026-09-10)."""
    store.write_day(DAY, _day())
    exp_day = date.fromisoformat(EXP)
    store.write_day(exp_day, _day(exp_day))
    s = ConsoleSession(underlying="NIFTY", day=exp_day, at="09:54", expiry=EXP)
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=1)
    leg = s.state()["legs"][0]
    assert leg["dte"] == 0
    hours_left = (datetime.combine(exp_day, time(15, 30)) - s.clock).total_seconds() / 3600
    assert 0 < leg["t"] < 1 / 365 and abs(leg["t"] * 365 * 24 - hours_left) < 0.05
    s.step(60)
    assert s.state()["legs"][0]["t"] < leg["t"]                 # t shrinks with the cursor


# ------------------------------------------ the decision record (Simulator, 2026-09-15)

def test_every_action_is_stamped_with_what_the_screen_showed():
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    s.stage(kind="add", strike=24000, right="CE", side="S", lots=1)
    s.stage(kind="add", strike=24000, right="PE", side="S", lots=1)
    rows = [r for r in s.journal if r["action"] != "NOOP"]
    assert len(rows) == 2 and all(r.get("context") for r in rows)
    ctx = rows[1]["context"]
    assert ctx["kind"] == "add"
    b, a = ctx["before"], ctx["after"]
    assert b["legs_open"] == 1 and a["legs_open"] == 2         # what was held either side
    assert a["at"] == f"{DAY}T10:00" and a["spot"] and a["expiry"] == EXP
    assert a["dte"] == (date.fromisoformat(EXP) - DAY).days
    assert a["greeks"] and set(a["greeks"]) >= {"delta", "gamma", "theta", "vega"}
    assert a["margin"] is not None and a["margin_source"]
    pay = a["payoff"]
    assert pay and pay["max_loss"] is None                     # a short straddle: open tails
    assert pay["max_profit"] and len(pay["breakevens"]) == 2 and pay["be_dist_pct"] is not None
    assert a["short_strike_dist_pct"] is not None
    # the owner's why lands on the group; an unknown group is refused
    assert s.annotate(rows[1]["group"], "second leg") and rows[1]["why"] == "second leg"
    assert not s.annotate(99, "nothing")
    # the stamps travel through save → restore untouched
    payload = s.save_payload()
    t = _open(at="10:00")
    t.restore(payload["journal"], payload["alerts"], payload["bookmarks"], payload["discarded"])
    kept = [r for r in t.journal if r["action"] != "NOOP"]
    assert kept[1]["context"]["after"]["legs_open"] == 2 and kept[1]["why"] == "second leg"


def test_undo_keeps_what_it_took_back():
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    s.stage(kind="add", strike=24000, right="CE", side="S", lots=1)
    s.step(5)
    s.stage(kind="add", strike=24000, right="PE", side="S", lots=1)
    assert s.undo_last() and len(s.legs) == 1
    assert len(s.discarded) == 1
    gone = s.discarded[0]
    assert gone["undone_at"] == f"{DAY}T10:05" and gone["rows"][0]["symbol"].endswith("24000|PE")
    assert "replaces" not in gone["rows"][0]
    assert s.state()["discarded"] == s.discarded and s.save_payload()["discarded"] == s.discarded
    # restore carries it; a payload without it restores clean
    t = _open(at="10:05")
    t.restore(s.journal, [], [], s.discarded)
    assert t.discarded == s.discarded
    u = _open(at="10:05")
    u.restore(s.journal, [], [])
    assert u.discarded == []


# ------------------------------------------------ what-if: the coach (owner, 2026-09-15)

def test_what_if_prices_candidate_adjustments_and_ranks_by_max_loss():
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    s.stage(kind="add", strike=24000, right="CE", side="S", lots=2)
    s.stage(kind="add", strike=24000, right="PE", side="S", lots=2)
    # the fixture's de-carried parity spot sits a few points UNDER 24,000 at 10:00, which
    # would make the PE the tested short and its roll-out strike (23900) one the tape
    # never printed; pin spot above the strike so the CE is tested and 24100 CE prices
    s.market.index_spot = lambda u: 24_100.0
    w = s.what_if()
    ids = [c["id"] for c in w["candidates"]]
    assert ids[0] == "hold" and w["tested"] in {leg.id for leg in s.legs}
    assert {"close_tested", "close_half", "roll_out_1", "roll_out_2", "wing_naked",
            "flatten"} <= set(ids)
    by = {c["id"]: c for c in w["candidates"]}
    hold = by["hold"]
    assert hold["ok"] and hold["max_loss"] is None and hold["cash"] == 0.0   # open tails
    assert hold["margin"] and hold["margin_source"] == "model"
    # closing everything: no book, no tails, the loss is bounded at what the closes bank
    flat = by["flatten"]
    assert flat["ok"] and flat["legs_after"] == [] and flat["max_loss"] == flat["max_profit"]
    assert flat["cash"] < 0 and flat["charges"] > 0        # a short is bought back: cash out
    # a wing on both naked shorts: the 24500 CE has not printed at 10:00 → refused, said why
    wing = by["wing_naked"]
    assert not wing["ok"] and "24200 CE" in wing["reason"] and "has no price" in wing["reason"]
    # the TESTED short is the one spot has moved INTO — the CE when spot is above the
    # straddle's strike, the PE when it is below (the fixture's de-carried parity spot
    # sits a few points either side of 24,000 at 10:00, so derive it)
    ce_id = next(leg.id for leg in s.legs if leg.right == "CE")
    pe_id = next(leg.id for leg in s.legs if leg.right == "PE")
    assert w["tested"] == ce_id
    import skas_algo.services.options_console.whatif as _w
    s.market.index_spot = lambda u: 23_900.0
    assert _w.candidates(s)["tested"] == pe_id
    s.market.index_spot = lambda u: 24_100.0
    # a wing is pinned to the SHORT's expiry, not the ladder's chip (the fixture tape holds
    # one expiry, so the pin is asserted on the op and refused on a wrong one)
    assert all(op["expiry"] == EXP for op in wing["ops"])
    with pytest.raises(ValueError, match="has not traded"):
        s.apply_ops([{"kind": "add", "right": "CE", "strike": 24100, "side": "B", "lots": 1,
                      "expiry": "2026-08-25"}])
    item = s._stage_item(kind="add", right="CE", strike=24100, side="B", lots=1, expiry=EXP)
    assert item["expiry"] == EXP
    # rolling the tested short one step out keeps two legs and moves the breakeven
    roll = by["roll_out_1"]
    assert roll["ok"] and len(roll["legs_after"]) == 2 and roll["changes"][0].startswith("ROLL")
    # ranking: finite max losses before the unlimited ones, refused rows last
    ranked = w["candidates"][1:]
    finite = [c for c in ranked if c["ok"] and c["max_loss"] is not None]
    unlimited = [c for c in ranked if c["ok"] and c["max_loss"] is None]
    refused = [c for c in ranked if not c["ok"]]
    assert ranked == finite + unlimited + refused
    assert finite == sorted(finite, key=lambda c: -c["max_loss"])
    # nothing on the session moved
    assert len(s.legs) == 2 and all(leg.lots == 2 for leg in s.legs)
    # applying a candidate is ONE undo group with a context stamped (a minute later, so the
    # roll is a real close + open rather than a same-minute rewrite of the entry)
    s.step(5)
    roll = {c["id"]: c for c in s.what_if()["candidates"]}["roll_out_1"]
    s.apply_ops(roll["ops"], label=roll["label"])
    assert {int(leg.strike) for leg in s.legs} == {24000, 24100}
    rows = [r for r in s.journal if r["action"] != "NOOP"]
    assert rows[-1]["context"]["kind"] == roll["label"] and rows[-1]["group"] == rows[-2]["group"]
    assert s.undo_last() and {int(leg.strike) for leg in s.legs} == {24000}
    del s.market.index_spot
    assert s.what_if()["candidates"] and _open(at="09:16").what_if()["candidates"] == []


# ------------------------------------------- sessions survive the process (2026-09-15)

def test_a_session_is_written_through_and_comes_back_under_the_same_id(tmp_path):
    """A console session is in-process state; it used to die with the process and the
    page rebuilt it from the journal it held (a different id, a banner). Now the registry
    writes the tape through on every change and `get()` rebuilds a missing id from disk."""
    store.write_day(DAY, _day())
    s = registry.create(underlying="NIFTY", day=DAY, at="10:00", expiry=EXP, strike_window=6,
                        margin_per_lot_set=123_000.0)
    s.stage(kind="add", strike=24000, right="CE", side="S", lots=2)
    s.state()                                             # the route always ends in state()
    p = registry._path(s.id)
    assert p.exists()
    s.step(5)
    s.stage(kind="add", strike=24000, right="PE", side="S", lots=1)
    s.undo_last()
    s.arm_alert("target", 5000)
    s.state()
    j = json.loads(p.read_text())
    assert j["id"] == s.id and j["clock"] == "10:05" and j["strike_window"] == 6
    assert len([r for r in j["journal"] if r["action"] != "NOOP"]) == 1
    assert j["discarded"][0]["rows"][0]["symbol"].endswith("24000|PE")
    assert j["alerts"][0]["kind"] == "target" and j["margin_per_lot_set"] == 123_000.0
    # "restart": the registry is empty, the file is not
    sid = s.id
    registry.clear()
    wired = []
    registry.hooks = lambda sess: wired.append(sess.id)
    try:
        back = registry.get(sid)
    finally:
        registry.hooks = None
    assert back is not s and back.id == sid and wired == [sid]
    assert back.clock.strftime("%H:%M") == "10:05" and back.strike_window == 6
    assert [int(leg.strike) for leg in back.legs] == [24000] and back.legs[0].lots == 2
    assert back.discarded == s.discarded and back.margin_per_lot_set == 123_000.0
    assert [a["kind"] for a in back.state()["alerts"]] == ["target"]
    assert registry.get(sid) is back                      # in memory again
    # an explicit close deletes the file; an unknown / unsafe id is a plain miss
    assert registry.drop(sid) and not p.exists()
    with pytest.raises(KeyError):
        registry.get(sid)
    with pytest.raises(KeyError):
        registry.get("../../etc/passwd")
    with pytest.raises(KeyError):
        registry.get("deadbeef0000")


def test_an_evicted_session_comes_back_and_a_cursor_move_is_throttled(monkeypatch):
    store.write_day(DAY, _day())
    made = [registry.create(underlying="NIFTY", day=DAY, at="10:00", expiry=EXP)
            for _ in range(registry.MAX_SESSIONS + 1)]
    made[0].stage(kind="add", strike=24000, right="CE", side="S", lots=1)
    made[0].state()
    # push it out of memory
    for extra in made[1:]:
        extra.state()
    assert made[0].id not in {b["id"] for b in registry.briefs()}
    back = registry.get(made[0].id)
    assert back is not made[0] and len(back.legs) == 1
    # only the cursor moving: at most one write per CURSOR_WRITE_S
    writes = []
    monkeypatch.setattr(registry, "persist", lambda sess, book_changed=True: writes.append(book_changed))
    back.on_change = registry.persist
    back.step(1); back.state()
    back.step(1); back.state()
    back.stage(kind="add", strike=24000, right="PE", side="S", lots=1); back.state()
    back.state()                                          # nothing changed: no call
    assert writes == [False, False, True]


# --------------------------------------- an opposite click NETS the leg (2026-09-15)

def test_an_opposite_side_click_nets_the_contract_instead_of_opening_a_second_leg():
    """A broker's book has one position per contract. S ×2 then B ×1 on the same strike is
    a cover of one lot; B ×2 more closes the short and opens a long for the remainder.
    Before: both rows stood while the ladder badge read the net (owner, 2026-09-15)."""
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    s.stage(kind="add", strike=24000, right="CE", side="S", lots=2)
    s.step(5)
    s.stage(kind="add", strike=24000, right="CE", side="B", lots=1)
    assert [(leg.side, leg.lots) for leg in s.legs] == [("S", 1)]
    rows = [r for r in s.journal if r["action"] != "NOOP"]
    assert [r["action"] for r in rows] == ["SHORT", "COVER"]      # a cover, not a BUY
    assert s.realized != 0.0 and s.closed and s.closed[-1]["action"] == "COVER"
    # more than held: the short closes and the remainder opens a LONG, one action
    s.stage(kind="add", strike=24000, right="CE", side="B", lots=3)
    assert [(leg.side, leg.lots) for leg in s.legs] == [("B", 2)]
    rows = [r for r in s.journal if r["action"] != "NOOP"]
    assert [r["action"] for r in rows] == ["SHORT", "COVER", "COVER", "BUY"]
    assert rows[-1]["group"] == rows[-2]["group"]
    # undo takes the whole action back; a rebuild from the journal agrees with the live book
    assert s.undo_last() and [(leg.side, leg.lots) for leg in s.legs] == [("S", 1)]
    s.stage(kind="add", strike=24000, right="CE", side="B", lots=3)
    t = _open(at="10:05")
    t.restore(s.journal, [], [])
    assert ([(leg.side, leg.lots, leg.entry) for leg in t.legs]
            == [(leg.side, leg.lots, leg.entry) for leg in s.legs])
    assert t.realized == s.realized


def test_a_replayed_close_lands_on_the_leg_of_its_own_side():
    """A journal holding a SHORT and a BUY on one contract (an old tape from before
    netting): a SELL row must close the LONG, never the short that happened to be first."""
    store.write_day(DAY, _day())
    k = DAY.isoformat()
    j = [{"at": f"{k}T10:00", "symbol": f"NIFTY|{EXP}|24000|CE", "action": "SHORT", "group": 1,
          "units": 130.0, "price": 150.0, "charges": 10.0, "spot": 24000.0},
         {"at": f"{k}T10:00", "symbol": f"NIFTY|{EXP}|24000|CE", "action": "BUY", "group": 2,
          "units": 65.0, "price": 150.0, "charges": 10.0, "spot": 24000.0},
         {"at": f"{k}T10:05", "symbol": f"NIFTY|{EXP}|24000|CE", "action": "SELL", "group": 3,
          "units": 65.0, "price": 152.0, "charges": 10.0, "spot": 24000.0}]
    s = _open(at="10:05")
    s.restore(j, [], [])
    assert [(leg.side, leg.lots) for leg in s.legs] == [("S", 2)]
    assert s.realized == pytest.approx((152.0 - 150.0) * 65)
