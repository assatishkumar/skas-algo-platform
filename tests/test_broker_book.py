"""The broker mirror: a read-only book view over the reconciler the hourly halt already runs.

The 2026-09-02 shape: iron_fly SHORT a contract, fv_call LONG more of the same contract on
the same account. The broker nets them and BOOKS the matched quantity as a closed trade —
₹48,496 realised on its side, open on ours — and no screen could say so."""

from types import SimpleNamespace

from skas_algo.brokers.live_broker import LiveBroker
from skas_algo.live.manager import LiveRunManager
from skas_algo.services.broker_book import build_book

SYM = "NIFTY|2026-09-29|24300|CE"
TS = "NIFTY26SEP24300CE"


def _run(run_id, name, units, direction, price, *, account=1, mode="LIVE", live_orders=True,
         symbol=SYM):
    pf = SimpleNamespace(
        lot_symbols=lambda: [symbol],
        lots=lambda s: [SimpleNamespace(direction=direction, units=units, price=price)],
    )
    broker = LiveBroker.__new__(LiveBroker) if live_orders else object()
    sess = SimpleNamespace(portfolio=pf, broker=broker)
    cfg = SimpleNamespace(mode=mode, broker_account_id=account, name=name, strategy_id=name.split("_")[0])
    return SimpleNamespace(run_id=run_id, session=sess, config=cfg)


class _Adapter:
    def __init__(self, positions):
        self._pos = positions

    def _option_tradingsymbol(self, inst):
        return TS if inst is not None else None

    def positions(self):
        return list(self._pos)


def test_two_runs_on_opposite_sides_net_to_the_brokers_figure_and_show_what_it_booked():
    """Short 195 @390.05 (iron fly) + long 780 @141.35 (fv calendar) = long 585 at the broker,
    and the broker has already booked (390.05 − 141.35) × 195 = ₹48,496 on the matched 195."""
    mgr = LiveRunManager()
    mgr.runs = {
        11: _run(11, "iron_fly_nifty", 195, -1, 390.05),
        15: _run(15, "fv_call_1_1_4", 780, 1, 141.35),
    }
    book = build_book(mgr, 1, _Adapter([{"tradingsymbol": TS, "quantity": 585}]),
                      account={"id": 1, "label": "Satish Kite", "broker": "zerodha"})
    assert book["ok"] is True and book["mismatch"] is None
    [row] = book["rows"]
    assert row["tradingsymbol"] == TS and row["symbol"] == SYM and row["segment"] == "fno"
    assert row["platform_net"] == 585.0 and row["broker_net"] == 585.0 and row["status"] == "match"
    assert [(lot["name"], lot["direction"], lot["units"], lot["price"]) for lot in row["lots"]] == [
        ("iron_fly_nifty", -1, 195.0, 390.05), ("fv_call_1_1_4", 1, 780.0, 141.35),
    ]
    assert row["booked_at_broker"] == {
        "matched_units": 195.0, "avg_long": 141.35, "avg_short": 390.05, "amount": 48496.5,
    }
    assert book["totals"] == {"rows": 1, "mismatches": 0, "booked_at_broker": 48496.5}
    assert [r["run_id"] for r in book["runs_counted"]] == [11, 15] and book["runs_skipped"] == []


def test_a_mismatch_is_the_reconcilers_own_words_and_the_row_says_which_side():
    mgr = LiveRunManager()
    mgr.runs = {11: _run(11, "iron_fly_nifty", 195, -1, 390.05)}
    book = build_book(mgr, 1, _Adapter([{"tradingsymbol": TS, "quantity": -130}]))
    assert book["mismatch"] and "platform -195" in book["mismatch"] and "broker -130" in book["mismatch"]
    [row] = book["rows"]
    assert row["status"] == "mismatch" and row["diff"] == 65.0
    assert row["booked_at_broker"] is None                    # one side only — nothing netted
    assert book["totals"]["mismatches"] == 1


def test_paper_runs_and_demoted_live_runs_are_not_in_the_aggregate_but_the_demotion_is_named():
    """Runs 6/9/12/25 mirror the live books position for position and place nothing; counting
    them invents mismatches. A LIVE run whose orders are on PAPER after a restart is not
    managing its broker book either — it is listed as skipped, never silently absent."""
    mgr = LiveRunManager()
    mgr.runs = {
        11: _run(11, "iron_fly_nifty", 195, -1, 390.05),
        6: _run(6, "iron_fly_paper", 195, -1, 390.05, mode="PAPER"),
        28: _run(28, "value_investing", 12, 1, 100.0, live_orders=False, symbol="WIPRO"),
    }
    book = build_book(mgr, 1, _Adapter([{"tradingsymbol": TS, "quantity": -195}]))
    assert book["mismatch"] is None and [r["run_id"] for r in book["runs_counted"]] == [11]
    assert book["runs_skipped"] == [
        {"run_id": 28, "name": "value_investing", "strategy_id": "value", "reason": "orders on paper"},
    ]
    assert len(book["rows"]) == 1 and book["rows"][0]["lots"][0]["run_id"] == 11


def test_what_the_broker_holds_and_the_platform_does_not_is_surfaced_not_hidden():
    """A manual trade, or a leg from a stopped run: exactly the rows the owner wants to see."""
    mgr = LiveRunManager()
    mgr.runs = {}
    book = build_book(mgr, 1, _Adapter([
        {"tradingsymbol": "BANKNIFTY26SEP57500PE", "quantity": -30},
        {"tradingsymbol": "INFY", "quantity": 3},
    ]))
    assert book["ok"] is True
    by_ts = {r["tradingsymbol"]: r for r in book["rows"]}
    assert by_ts["BANKNIFTY26SEP57500PE"]["status"] == "broker_only"
    assert by_ts["BANKNIFTY26SEP57500PE"]["segment"] == "fno"
    assert by_ts["INFY"]["segment"] == "equity" and by_ts["INFY"]["lots"] == []
    # mismatches sort first, then by contract
    assert [r["status"] for r in book["rows"]] == ["broker_only", "broker_only"]


def test_a_failed_read_says_so_and_never_shows_an_empty_book_as_flat():
    """Off-hours the token is routinely dead; an empty table would read as 'no positions' —
    the same false comfort as the old 04:50 phantom halt, in the other direction."""
    class _Dead(_Adapter):
        def positions(self):
            raise RuntimeError("Incorrect `api_key` or `access_token`.")

    mgr = LiveRunManager()
    mgr.runs = {11: _run(11, "iron_fly_nifty", 195, -1, 390.05)}
    book = build_book(mgr, 1, _Dead([]))
    assert book["ok"] is False and "access_token" in book["error"] and book["rows"] == []


def test_the_view_is_the_reconcilers_own_aggregate_not_a_second_copy():
    """Pinned so nobody re-implements the netting in the service: the rows come from the
    `details` the reconciler fills, which is the SAME loop that decides a halt."""
    import inspect

    from skas_algo.services import broker_book

    src = inspect.getsource(broker_book.build_book)
    assert "reconcile_account_book(" in src
    assert "lot_symbols" not in src and ".lots(" not in src   # no portfolio walking here
