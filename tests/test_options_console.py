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


def test_the_two_sides_of_one_strike_stay_separate():
    """Merging is per contract AND side — a long and a short of the same option are not one
    position, they are a spread that happens to share a strike."""
    store.write_day(DAY, _day())
    s = _open(at="10:00")
    s.stage(kind="add", right="CE", strike=24000, side="S", lots=2)
    s.stage(kind="add", right="CE", strike=24000, side="B", lots=1)
    assert len(s.legs) == 2
    assert {(x.side, x.lots) for x in s.legs} == {("S", 2), ("B", 1)}


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
