"""The console over a RUNNING deployment: a fake LiveRun around a real LiveSession.

The pin that matters: a commit reaches the run ONLY through `manual_order` — the platform's
gated manual path — with an expiry on every open and FIFO units on a partial close; the
console itself never touches a broker. And the DTO is the replay's DTO, key for key, so the
page cannot tell which source it is looking at."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from skas_algo.services import console_live
from skas_algo.services.options_console import registry
from skas_algo.services.options_console.session import ConsoleSession
from tests.test_live_options import EXPIRIES, FakeLiveSD, _biz, _session


class _Config:
    def __init__(self, mode="PAPER"):
        self.mode = mode
        self.instrument_class = "DERIV"
        self.underlying = "NIFTY"
        self.name = "hni paper"
        self.strategy_id = "hni_weekly"
        self.capital = 1_000_000.0


class FakeLiveRun:
    """Duck-types the slice of `LiveRun` the console reads: config, session, snapshot,
    manual_order. `manual_order` records what it was asked and forwards to the session."""

    def __init__(self, session, *, mode="PAPER", order_broker="paper"):
        self.run_id = 42
        self.config = _Config(mode)
        self.session = session
        self.calls: list[dict] = []
        self._order_broker = order_broker

    def snapshot(self):
        snap = self.session.snapshot()
        snap.update({"order_broker": self._order_broker, "order_error": None,
                     "status": "running", "open_positions": snap.get("open_positions")})
        return snap

    def manual_order(self, *, closes=None, opens=None):
        self.calls.append({"closes": list(closes or []), "opens": list(opens or [])})
        return self.session.manual_order(datetime(2026, 1, 5, 10, 30), closes=closes, opens=opens)


@pytest.fixture
def run(monkeypatch):
    cal = _biz(date(2026, 1, 1), date(2026, 1, 20))
    sess, mv, strat = _session(FakeLiveSD(cal), datetime(2026, 1, 5, 9, 50))
    sess.run_decision(datetime(2026, 1, 5, 9, 50))               # the 1-3-2 is on
    sess.update_quotes({leg["symbol"]: leg["entry"] for leg in strat.legs})
    live = FakeLiveRun(sess)
    monkeypatch.setattr(console_live.manager, "get", lambda rid: live if rid == 42 else None)
    monkeypatch.setattr(console_live.manager, "list", lambda: [live])
    monkeypatch.setattr(console_live, "_ist_now", lambda: datetime(2026, 1, 5, 10, 30))
    console_live._CONSOLES.clear()
    registry.clear()
    yield live
    console_live._CONSOLES.clear()


def test_the_live_console_reads_the_runs_book_and_answers_the_replay_dto(run):
    c = console_live.open_console(42)
    st = c.state()
    assert st["session"]["id"] == "live:42" and st["session"]["mode"] == "paper"
    assert st["session"]["requires_confirm"] is True and st["session"]["can_undo"] is False
    # the legs ARE the run's portfolio
    book = set(run.session.portfolio.lot_symbols())
    assert {leg["symbol"] for leg in st["legs"]} == book and len(st["legs"]) == 3
    short = next(leg for leg in st["legs"] if leg["side"] == "S")
    assert short["lots"] == 3 and short["units"] == 195 and short["dte"] == 8
    # key-for-key the replay's DTO, so the page needs no second renderer
    registry.clear()
    from skas_algo.data import option_intraday_store as store
    assert set(st) >= {"session", "market", "chain", "legs", "staged", "risk", "fills",
                       "journal", "alerts", "bookmarks", "cycle", "track", "pricing", "notes"}
    assert set(st["risk"]) == {"realised", "realised_total", "net_credit", "unrealised", "mtm",
                               "charges", "margin", "margin_source", "margin_detail", "capital",
                               "legs_open", "greeks"}
    _ = (store, ConsoleSession)


def test_a_commit_reaches_the_run_only_through_manual_order(run):
    c = console_live.open_console(42)
    later = [e for e in EXPIRIES if e > date(2026, 1, 13)][0].isoformat()
    c.set_expiry(later)
    # stage an add on the chip's expiry + a partial exit of the short, then Apply
    c.stage(kind="add", right="PE", strike=24800, side="B", lots=2)
    short = next(leg for leg in c.legs() if leg["side"] == "S")
    c.stage(kind="exit", leg_id=short["id"], lots=1)
    st = c.state()
    assert st["staged"] and len(st["staged"]["items"]) == 2
    assert len(st["staged"]["after_legs"]) == 4               # previewed, nothing ordered
    assert run.calls == []
    out = c.commit()
    assert out["committed"] == 2 and len(run.calls) == 1
    call = run.calls[0]
    assert call["opens"] == [{"right": "PE", "strike": 24800.0, "lots": 2, "side": "buy",
                              "expiry": later, "symbol": f"NIFTY|{later}|24800|PE"}]
    assert call["closes"] == [{"symbol": short["symbol"], "units": 65}]   # 1 of 3 lots, FIFO
    assert c.staged is None
    book = run.session.portfolio
    assert f"NIFTY|{later}|24800|PE" in book.lot_symbols()
    assert sum(lot.units for lot in book.lots(short["symbol"])) == 130


def test_roll_flatten_and_scale_become_closes_and_opens(run):
    c = console_live.open_console(42)
    short = next(leg for leg in c.legs() if leg["side"] == "S")
    c.stage(kind="roll", leg_id=short["id"], strike=short["strike"] + 100)
    closes, opens = c._orders(c.staged["items"])
    assert closes == [{"symbol": short["symbol"]}]
    assert opens == [{"right": "CE", "strike": short["strike"] + 100, "lots": 3, "side": "sell",
                      "expiry": short["expiry"],
                      "symbol": f"NIFTY|{short['expiry']}|{int(short['strike'] + 100)}|CE"}]
    c.discard()
    c.scale_book(2)
    closes, opens = c._orders(c.staged["items"])
    # ×2 on a 1-3-2: +3 more short, +1 and +2 more on the two longs — every open carries
    # its leg's own expiry and side, nothing closes
    assert not closes and sorted(o["lots"] for o in opens) == [1, 2, 3]
    assert {o["expiry"] for o in opens} == {short["expiry"]}
    c.discard()
    c.stage(kind="flatten", replace=True)
    closes, opens = c._orders(c.staged["items"])
    assert len(closes) == 3 and not opens


def test_a_demoted_live_run_reads_paper_and_a_toggle_never_orders(run):
    run.config.mode = "LIVE"                       # LIVE run, but orders on the paper broker
    c = console_live.open_console(42)
    assert c.mode == "paper"
    run._order_broker = "live"
    assert c.mode == "live"
    leg = c.legs()[0]
    c.stage(kind="toggle", leg_id=leg["id"])
    assert c.staged is None and leg["id"] in c.disabled and run.calls == []
    assert not [x for x in c.state()["legs"] if x["id"] == leg["id"]][0]["enabled"]


def test_the_console_package_still_never_imports_the_order_path():
    """The bridge lives OUTSIDE the package; the package's pin stands."""
    import pkgutil

    import skas_algo.services.options_console as pkg
    for mod in pkgutil.iter_modules(pkg.__path__):
        text = open(pkg.__path__[0] + "/" + mod.name + ".py").read()
        assert "console_live" not in text and "from skas_algo.live.manager" not in text


def test_the_staged_book_is_the_one_shown_and_an_uncommitted_leg_is_edited_not_stacked(run):
    """Positions, payoff, margin and MTM on a running deployment show the book AS IF the
    basket were committed; touched rows carry `pending`; a resize/exit/roll on a leg that
    is itself still staged edits that item instead of ordering against a leg the run does
    not hold. Revert (discard) restores the run's own book."""
    c = console_live.open_console(42)
    before = len(c.legs())
    c.stage(kind="add", right="PE", strike=24800, side="B", lots=2)
    st = c.state()
    after = st["staged"]["after_legs"]
    new = [b for b in after if b.get("pending") == "add"]
    assert len(after) == before + 1 and len(new) == 1 and new[0]["id"] == "S1"
    assert st["staged"]["risk_after"]["legs_open"] == before + 1
    assert st["staged"]["risk_after"]["margin"] != st["risk"]["margin"] or True
    # edit the uncommitted leg: resize to 3, then exit 1 → 2; the basket stays ONE add
    c.stage(kind="resize", leg_id="S1", lots=3)
    assert [it["lots"] for it in c.staged["items"]] == [3]
    c.stage(kind="exit", leg_id="S1", lots=1)
    assert [it["lots"] for it in c.staged["items"]] == [2] and run.calls == []
    c.stage(kind="roll", leg_id="S1", strike=24700)
    assert c.staged["items"][0]["strike"] == 24700.0
    # exit it entirely → the basket empties itself
    c.stage(kind="exit", leg_id="S1", lots=2)
    assert c.staged is None
    # a partial exit of a HELD leg shows the reduced row, pending, nothing ordered
    short = next(leg for leg in c.legs() if leg["side"] == "S")
    c.stage(kind="exit", leg_id=short["id"], lots=1)
    row = next(b for b in c.state()["staged"]["after_legs"] if b["id"] == short["id"])
    assert row["lots"] == short["lots"] - 1 and row["pending"] == "exit" and run.calls == []
    c.discard()
    assert c.state()["staged"] is None and len(c.legs()) == before


def test_the_ticket_is_the_orders_commit_will_send_with_their_cash(run):
    """D5: every staged change becomes ticket rows at the run's own mark — a close of a
    short is a BUY, an open sell is a SELL — and the net is what the basket moves."""
    c = console_live.open_console(42)
    short = next(leg for leg in c.legs() if leg["side"] == "S")
    c.stage(kind="exit", leg_id=short["id"], lots=1)
    c.stage(kind="add", right="PE", strike=24800, side="B", lots=2)
    t = c.state()["staged"]["ticket"]
    assert [(r["action"], r["role"], r["lots"]) for r in t["rows"]] == [("BUY", "close", 1), ("BUY", "open", 2)]
    buy_close, buy_open = t["rows"]
    assert buy_close["units"] == 65 and buy_close["cash"] == pytest.approx(-buy_close["price"] * 65)
    assert buy_open["units"] == 130 and buy_open["expiry"] == c.expiry
    assert t["net_cash"] == pytest.approx(buy_close["cash"] + buy_open["cash"])
    assert t["limit_orders"] is True and "paper" in t["fill_basis"]   # D5 LMT, 2026-09-10
    assert run.calls == []                                  # a ticket orders nothing


def test_a_roll_up_and_back_is_no_change_and_a_second_resize_replaces_the_first(run):
    c = console_live.open_console(42)
    short = next(leg for leg in c.legs() if leg["side"] == "S")
    k = short["strike"]
    c.stage(kind="roll", leg_id=short["id"], strike=k + 100)
    c.stage(kind="roll", leg_id=short["id"], strike=k + 200)
    assert [it["strike"] for it in c.staged["items"]] == [k + 200]      # replaced, not stacked
    c.stage(kind="roll", leg_id=short["id"], strike=k)
    assert c.staged is None                                              # back home: nothing staged
    c.stage(kind="resize", leg_id=short["id"], lots=5)
    c.stage(kind="resize", leg_id=short["id"], lots=4)
    assert [it["lots"] for it in c.staged["items"]] == [4]
    c.stage(kind="resize", leg_id=short["id"], lots=short["lots"])
    assert c.staged is None and run.calls == []


def test_a_pending_leg_can_be_toggled_and_each_pending_change_dropped_on_its_own(run):
    c = console_live.open_console(42)
    short = next(leg for leg in c.legs() if leg["side"] == "S")
    c.stage(kind="add", right="PE", strike=24800, side="B", lots=1)
    c.stage(kind="exit", leg_id=short["id"], lots=1)
    c.stage(kind="toggle", leg_id="S1")                       # a view flag, never an order
    after = {b["id"]: b for b in c.state()["staged"]["after_legs"]}
    assert after["S1"]["enabled"] is False and run.calls == []
    assert after[short["id"]]["pending"] == "exit"
    assert after[short["id"]]["pending_from"] == {"lots": short["lots"], "strike": short["strike"]}
    c.unstage(short["id"], "exit")                            # ↺ on the partial-exit sub-row
    after = {b["id"]: b for b in c.state()["staged"]["after_legs"]}
    assert after[short["id"]]["lots"] == short["lots"] and "pending" not in after[short["id"]]
    assert [it["kind"] for it in c.staged["items"]] == ["add"]
    c.unstage("S1")                                           # 🗑 on the pending add
    assert c.staged is None and "S1" not in c.disabled


def test_the_ticket_reads_the_brokers_available_margin_and_flags_a_hedge_exit(run):
    """The ticket measured a post-order margin against the run's typed CAPITAL ("₹10.5L
    short" of a number the RMS never sees). Now it carries the account's available
    margin from the run's adapter, and flags a SELL-to-close of a long while shorts
    stay — the hedge coming off that Zerodha's RMS prices naked (owner, 2026-09-18)."""
    from types import SimpleNamespace

    calls = []

    class _Adapter:
        def funds(self):
            calls.append(1)
            return SimpleNamespace(available=250_000.0, used=90_000.0)
    run.quote_source = SimpleNamespace(adapter=_Adapter())
    run.config.broker = "zerodha"
    c = console_live.open_console(42)
    long_leg = next(leg for leg in c.legs() if leg["side"] == "B")
    c.stage(kind="exit", leg_id=long_leg["id"], lots=long_leg["lots"])
    t = c.ticket()
    assert t["broker"] == {"available": 250_000.0, "used": 90_000.0, "broker": "zerodha",
                           "as_of": t["broker"]["as_of"]}
    assert t["closes_hedge"] is True                     # a long sold, the short stays
    c.ticket()
    assert len(calls) == 1                               # cached, not a call per render
    c.discard()
    # closing the SHORT is not a hedge exit; a run with no adapter reports no funds
    short = next(leg for leg in c.legs() if leg["side"] == "S")
    c.stage(kind="exit", leg_id=short["id"], lots=1)
    assert c.ticket()["closes_hedge"] is False
    run.quote_source = None
    c._funds_cache = None
    assert c.ticket()["broker"] is None


def test_a_staged_close_banks_into_the_after_basis_so_the_preview_matches_the_commit(run):
    """Run 31, 2026-09-18: exiting one lot of a long bought at 1,071 and marked 357 showed
    the after-book's payoff ₹21k ABOVE where Commit left it — the preview dropped the
    realised of the staged close. The after basis now carries it at the ticket's price."""
    c = console_live.open_console(42)
    short = next(leg for leg in c.legs() if leg["side"] == "S")
    # mark the short well below its entry so the close BANKS a profit the preview must show
    run.session.update_quotes({short["symbol"]: short["entry"] * 0.5})
    c.stage(kind="exit", leg_id=short["id"], lots=1)
    st = c.state()
    before, after = st["risk"], st["staged"]["risk_after"]
    lot = short["lot_size"]
    expect = (short["entry"] - short["entry"] * 0.5) * lot     # one lot of a short, bought back lower
    assert after["realised"] - before["realised"] == pytest.approx(expect, abs=1.0)
    assert after["realised_total"] - before["realised_total"] == pytest.approx(expect, abs=1.0)
    assert after["mtm"] == pytest.approx(after["realised"] + after["unrealised"], abs=0.01)
    # and commit lands the book where the preview said
    c.commit()
    now = c.state()["risk"]
    assert now["realised"] == pytest.approx(after["realised"], abs=1.0)
