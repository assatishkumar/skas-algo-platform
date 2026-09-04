"""The Live tile's cycle reader: entry stamp + entry spot + realized-before, off the log."""

from datetime import datetime, timedelta, timezone

from skas_algo.api.routes.live import index_rows
from skas_algo.services.live_cycles import IST, cycle_info, realized_cumulative

CE = "BANKNIFTY|2026-09-29|57500|CE"
UP = "BANKNIFTY|2026-09-29|57900|CE"
SX = "SENSEX|2026-09-10|81000|PE"


def _t(day: int, hh: int, mm: int, ticker: str, action: str, units: int, *, profit=0.0, spot=None):
    return {
        "date": datetime(2026, 9, day, hh, mm, tzinfo=IST), "ticker": ticker, "action": action,
        "units": units, "price": 100.0, "profit": profit, "underlying_spot": spot,
    }


def test_an_open_cycle_reports_its_entry_stamp_spot_and_the_realized_before_it():
    """Run 30's shape: a first cycle closed on 'target' (−9,765), then a fresh entry. The tile
    must say WHEN the open cycle started, the index level then, and how much was already
    banked — so its sparkline can subtract that and show the cycle alone."""
    log = [
        _t(4, 9, 15, UP, "BUY", 300, spot=57515.55),
        _t(4, 9, 15, CE, "SHORT", 600, spot=57515.55),
        _t(4, 9, 17, CE, "COVER", 600, profit=20580.0, spot=57480.0),
        _t(4, 9, 17, UP, "SELL", 300, profit=-30345.0, spot=57480.0),
        _t(7, 9, 31, UP, "BUY", 300, spot=57100.0),
        _t(7, 9, 31, CE, "SHORT", 600, spot=57100.0),
    ]
    c = cycle_info(log, "BANKNIFTY")
    assert c["open"] is True
    assert c["entry_at"] == "2026-09-07T09:31:00+05:30"
    assert c["entry_spot"] == 57100.0
    assert c["realized_before"] == -9765.0
    assert c["last"]["pnl"] == -9765.0 and c["last"]["entry_spot"] == 57515.55
    assert c["last"]["exit_spot"] == 57480.0 and c["last"]["exit_at"].startswith("2026-09-04T09:17")


def test_a_flat_run_keeps_its_last_cycle_and_reads_realized_before_as_the_total():
    log = [
        _t(1, 9, 20, CE, "SHORT", 30, spot=57000.0),
        _t(1, 15, 20, CE, "COVER", 30, profit=1500.0, spot=57050.0),
    ]
    c = cycle_info(log, "BANKNIFTY")
    assert c["open"] is False and c["entry_at"] is None and c["entry_spot"] is None
    assert c["realized_before"] == 1500.0
    assert c["last"] == {
        "entry_at": "2026-09-01T09:20:00+05:30", "exit_at": "2026-09-01T15:20:00+05:30",
        "entry_spot": 57000.0, "exit_spot": 57050.0, "pnl": 1500.0, "realized_before": 0.0,
    }


def test_the_entry_spot_is_the_tiles_own_underlying_in_a_two_index_run():
    """cp_ratio opens NIFTY and SENSEX books in one decision; a NIFTY tile shows NIFTY."""
    log = [
        _t(2, 9, 20, SX, "BUY", 20, spot=81000.0),
        _t(2, 9, 20, "NIFTY|2026-09-08|24000|CE", "BUY", 75, spot=24012.0),
    ]
    assert cycle_info(log, "NIFTY")["entry_spot"] == 24012.0
    assert cycle_info(log, "SENSEX")["entry_spot"] == 81000.0
    assert cycle_info(log, None)["entry_spot"] == 81000.0  # first stamp when no index is named


def test_a_settlement_closes_the_cycle_whichever_way_the_lot_faced():
    log = [
        _t(3, 9, 20, CE, "SHORT", 30, spot=57000.0),
        _t(8, 15, 30, CE, "SETTLE", 30, profit=900.0, spot=56900.0),
    ]
    c = cycle_info(log, "BANKNIFTY")
    assert c["open"] is False and c["last"]["pnl"] == 900.0


def test_persisted_iso_stamps_and_naive_ones_are_read_as_ist():
    log = [
        {"date": "2026-09-04T09:15:03+05:30", "ticker": CE, "action": "SHORT", "units": 30,
         "underlying_spot": 57515.55},
        {"date": "2026-09-04T09:17:20", "ticker": CE, "action": "COVER", "units": 30, "profit": -5.0},
    ]
    c = cycle_info(log, "BANKNIFTY")
    assert c["last"]["entry_at"] == "2026-09-04T09:15:03+05:30"
    assert c["last"]["exit_at"] == "2026-09-04T09:17:20+05:30"


def test_realized_cumulative_gives_each_sample_what_was_booked_by_then():
    """The greeks samples are UNREALIZED-only; this is what makes them an overall series."""
    log = [
        _t(4, 9, 17, CE, "COVER", 600, profit=20580.0),
        _t(4, 9, 17, UP, "SELL", 300, profit=-30345.0),
        _t(4, 14, 0, CE, "COVER", 30, profit=100.0),
    ]
    utc = timezone.utc
    stamps = [
        datetime(2026, 9, 4, 3, 46, tzinfo=utc),   # 09:16 IST — nothing booked
        datetime(2026, 9, 4, 3, 48, tzinfo=utc),   # 09:18 IST — the first cycle
        datetime(2026, 9, 4, 8, 30, tzinfo=utc) + timedelta(minutes=1),  # 14:01 IST
    ]
    assert realized_cumulative(log, stamps) == [0.0, -9765.0, -9665.0]


def test_index_rows_carry_a_day_change_and_skip_an_index_with_no_print():
    rows = index_rows({
        "NIFTY 50": {"last": 24012.35, "prev_close": 23912.35},
        "NIFTY BANK": {"last": 57391.0, "prev_close": None},
        "SENSEX": {"last": 0.0, "prev_close": 81000.0},
    })
    assert [r["name"] for r in rows] == ["NIFTY", "BANKNIFTY"]
    assert rows[0]["change"] == 100.0 and rows[0]["change_pct"] == 0.42
    assert rows[1]["change"] is None and rows[1]["change_pct"] is None
