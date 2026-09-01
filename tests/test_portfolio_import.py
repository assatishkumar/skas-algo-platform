"""The pasted-history parser.

This is the only place a typo in a spreadsheet becomes a wrong cost basis, so the tests are
about REFUSING ambiguity rather than being clever with it. Two properties matter most: a line
that cannot be read is reported (never skipped), and an ambiguous date is read day-first
because every Indian source is day-first.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from skas_algo.services.portfolio import build_ledger
from skas_algo.services.portfolio_import import (
    guess_asset_class,
    parse_date,
    parse_ledger_paste,
    parse_paste,
)


def test_the_date_formats_a_real_export_actually_contains():
    assert parse_date("2024-06-10") == date(2024, 6, 10)
    assert parse_date("10-Jun-2024") == date(2024, 6, 10)   # AMFI / spreadsheet style
    assert parse_date("28-Aug-2026") == date(2026, 8, 28)
    assert parse_date("10/06/2024") == date(2024, 6, 10)    # day-first, per the docstring
    assert parse_date("10/06/24") == date(2024, 6, 10)
    assert parse_date("1 Apr 2020") == date(2020, 4, 1)


def test_a_month_first_date_is_refused_rather_than_swapped():
    """12/25/2024 can only be month-first. Silently swapping it would move a trade by up to
    eleven months — enough to cross the 12-month LTCG line and change the tax owed."""
    assert parse_date("12/25/2024") is None


def test_a_header_row_is_skipped_but_a_broken_row_is_reported():
    rows, errors = parse_paste(
        "Date,Type,Units,Price\n"
        "2024-01-01,buy,10,100\n"
        "not-a-date,buy,10,100\n"
    )
    assert len(rows) == 1
    assert len(errors) == 1
    assert "Line 3" in errors[0]


def test_tab_comma_and_wide_space_separations_all_parse():
    for text in (
        "2024-01-01\tbuy\t10\t100.5",
        "2024-01-01,buy,10,100.5",
        "2024-01-01   buy   10   100.5",
    ):
        rows, errors = parse_paste(text)
        assert errors == []
        assert rows[0].units == 10 and rows[0].price == 100.5


def test_broker_vocabulary_and_formatting_are_understood():
    """Real exports write 'Purchase'/'Redemption', group digits, and bracket a sold quantity."""
    rows, errors = parse_paste(
        "2024-01-01,Purchase,1,000,\"1,234.50\"\n"
        "2024-02-01,Redemption,(120),250\n"
    )
    assert errors == []
    assert rows[1].kind == "sell" and rows[1].units == 120


def test_an_unreadable_type_is_an_error_not_a_guessed_buy():
    rows, errors = parse_paste("2024-01-01,dividend,10,100")
    assert rows == []
    assert "neither a buy nor a sell" in errors[0]


def test_a_short_row_names_what_is_missing():
    _, errors = parse_paste("2024-01-01,buy,10")
    assert "found 3 column(s)" in errors[0]


def test_the_preview_endpoint_returns_what_the_import_would_do(client: TestClient):
    resp = client.post("/api/v1/portfolio/transactions/parse", json={
        "text": "2023-01-10\tbuy\t100\t100\n10-Jun-2024\tbuy\t100\t200\n10/01/2025\tsell\t120\t250",
    })
    body = resp.json()
    assert body["errors"] == []
    assert body["summary"] == {
        "rows": 3, "buys": 2, "sells": 1,
        "earliest": "2023-01-10", "latest": "2025-01-10",
    }


def test_a_paste_with_any_bad_line_still_reports_the_good_ones_for_context(client: TestClient):
    """The UI blocks the import while errors exist, but showing what DID parse is how you
    spot that the file has the wrong column order rather than one bad row."""
    body = client.post("/api/v1/portfolio/transactions/parse", json={
        "text": "2024-01-01,buy,10,100\ngarbage line here\n2024-02-01,sell,5,120",
    }).json()
    assert body["summary"]["rows"] == 2
    assert len(body["errors"]) == 1


def test_a_broken_first_row_is_reported_not_mistaken_for_a_header():
    """The tempting header rule — 'line 1 isn't a date, so skip it' — silently eats a real
    first trade whose date is malformed. That is the exact failure this module exists to
    prevent, so a line carrying numbers is data no matter how bad its date is."""
    rows, errors = parse_paste("32-Jan-2024,buy,10,100\n2024-02-01,buy,5,120")
    assert len(rows) == 1
    assert len(errors) == 1 and "Line 1" in errors[0]


def test_an_unlabelled_first_trade_is_not_swallowed():
    """A paste with no header at all must import every line."""
    rows, errors = parse_paste("2024-01-01,buy,10,100\n2024-02-01,sell,5,120")
    assert errors == [] and len(rows) == 2


def test_a_wordy_header_row_is_still_recognised():
    rows, errors = parse_paste(
        "Trade Date\tTransaction Type\tQuantity\tRate\n2024-01-01\tbuy\t10\t100")
    assert errors == [] and len(rows) == 1


# ------------------------------------------------------------------ the wide tracking sheet

_HEADER = ("Stock / ETF code\tBuy / Sell Date\tBuy Price\tBought Units\tBID Number\t"
           "Next BID Number\tInvested Amount\tUnits Sold\tSell Price\tBooked Profit\tNotes")


def _sheet(*rows: str) -> str:
    return "\n".join((_HEADER, *rows))


def _row(sym="", date="", bp="", bu="", bid="", nbid="", inv="", su="", sp="", pr="", note=""):
    return "\t".join([sym, date, bp, bu, bid, nbid, inv, su, sp, pr, note])


def test_the_wide_sheet_splits_buys_and_sells_by_COLUMN_not_by_a_type_word():
    """Buys and sells share a row shape and differ only in which columns are filled."""
    rows, errors, warnings = parse_ledger_paste(_sheet(
        _row(sym="NSE:ITC", date="26-May-25", bp="442.85", bu="113",
             bid="0", nbid="1", inv="50,042"),
        _row(sym="NSE:ITC", date="23-Oct-25", nbid="1", su="6", sp="3,773.80", pr="22,643"),
    ))
    assert errors == [] and warnings == []
    assert [(r.kind, r.units, r.price) for r in rows] == [
        ("buy", 113.0, 442.85), ("sell", 6.0, 3773.80),
    ]
    assert rows[0].symbol == "ITC"  # the NSE: prefix is dropped


def test_a_paste_without_tabs_is_refused_rather_than_guessed_at():
    """Tabs are the only thing preserving EMPTY cells, and an empty cell is what distinguishes
    a buy row from a sell row. Collapse them and the two are indistinguishable — the position
    would import inverted and look entirely plausible."""
    rows, errors, _ = parse_ledger_paste(
        "NSE:ITC  26-May-25  442.85  113  0  1  50,042\n"
        "NSE:ITC  23-Oct-25  1  6  3,773.80  22,643"
    )
    assert rows == []
    # No symbol: this one is GLOBAL and blocks the whole paste, as it must.
    assert errors[0].symbol is None
    assert "no tab characters" in errors[0].message


def test_a_sell_with_no_price_is_refused_because_it_would_book_a_phantom_loss():
    """The owner's real sheet contains exactly this: 15,161 NIFTYBEES units sold on
    2026-03-23 with the price typed into the notes instead of the price column. Importing it
    at zero would book a ~₹38.7 L loss that never happened."""
    rows, errors, _ = parse_ledger_paste(_sheet(
        _row(sym="NSE:NIFTYBEES", date="23-Mar-26", bid="3", nbid="4",
             su="15,161", sp="0.00", pr="0", note="255.15 sell price"),
    ))
    assert rows == []
    # Scoped to NIFTYBEES — every other symbol in the same paste stays seedable.
    assert errors[0].symbol == "NIFTYBEES"
    assert "phantom loss" in errors[0].message and "255.15" in errors[0].message


def test_a_zero_price_buy_is_a_corporate_action_that_rebases_the_open_lots():
    """Bajaj Finance's June-2025 event lands in the sheet as 1,908 units at ₹0 against 212 held
    — exactly 1:10 (a 4:1 bonus then a 1:2 split).

    Appending those as a NEW lot is the trap: the older lots keep their PRE-split ₹7,192
    per-unit cost while every later sale is of POST-split units, so FIFO matches a ₹972 sale
    against ₹7,192 and books a ₹2.2 L loss that never happened. Re-basing keeps total cost
    identical, preserves FIFO order and lot dates, and prices the sale correctly."""
    rows, errors, _ = parse_ledger_paste(_sheet(
        _row(sym="NSE:BAJFINANCE", date="30-Jun-24", bp="7,192.10", bu="212", inv="15,24,725"),
        _row(sym="NSE:BAJFINANCE", date="13-Jun-25", bp="0.00", bu="1,908", inv="0"),
        _row(sym="NSE:BAJFINANCE", date="11-Sep-25", su="36", sp="972.15", pr="34,997"),
    ))
    assert errors == []
    assert [r.kind for r in rows] == ["buy", "bonus", "sell"]

    led = build_ledger([r.as_dict() for r in rows])
    assert led.actions[0]["ratio"] == pytest.approx(10.0)
    assert led.actions[0]["units_before"] == pytest.approx(212.0)
    # Cost is untouched by the action, and only the sale's own cost leaves it.
    assert led.cost == pytest.approx(212 * 7192.10 - 36 * 719.21, abs=1.0)
    assert led.units == pytest.approx(2120 - 36)
    # The sale is a PROFIT: 972.15 against a re-based 719.21, not the raw 7,192.10.
    assert led.realized == pytest.approx(36 * (972.15 - 719.21), abs=1.0)
    assert led.realized > 0


def test_free_units_against_nothing_held_are_recorded_but_never_applied():
    """A bonus with no open lots means a buy row is missing before it. Applying it would
    divide by zero; inventing a lot would put a fabricated cost basis into the tax lots."""
    led = build_ledger([{"on_date": "2025-06-13", "kind": "bonus", "units": 100, "price": 0}])
    assert led.units == 0 and led.cost == 0
    assert led.actions[0]["ratio"] is None
    assert "missing" in led.actions[0]["problem"]


def test_a_stated_total_that_disagrees_with_price_times_units_warns_but_imports():
    """"Invested Amount" and "Booked Profit" are DERIVED columns (verified against the real
    file), so a mismatch means a typo in one of the three — worth flagging, not worth
    blocking, because quantity and price are the fields actually used."""
    rows, errors, warnings = parse_ledger_paste(_sheet(
        _row(sym="NSE:ITC", date="26-May-25", bp="442.85", bu="113", inv="99,999"),
    ))
    assert errors == [] and len(rows) == 1
    assert "mistyped" in warnings[0]


def test_a_row_claiming_both_a_buy_and_a_sell_is_refused_as_ambiguous():
    """There is no way to know which side happened first, and the FIFO result differs."""
    rows, errors, _ = parse_ledger_paste(_sheet(
        _row(sym="NSE:ITC", date="26-May-25", bp="442", bu="10", su="5", sp="500"),
    ))
    assert rows == []
    assert errors[0].symbol == "ITC"
    assert "both bought and sold" in errors[0].message


def test_bid_bookkeeping_spacers_and_broken_excel_refs_are_ignored():
    """The sheet carries BID/Next-BID tracking columns, blank spacer rows, and at least one
    #REF!. None of it is cost-basis data; none of it may break the import."""
    rows, errors, _ = parse_ledger_paste(_sheet(
        _row(bid="0", nbid="0"),
        _row(sym="NSE:MIDSMALL", date="23-May-25", bp="49.01", bu="1,020",
             bid="0", nbid="#REF!", inv="49,990"),
        "\t\t\t\t\t\t\t\t\t\t",
    ))
    assert errors == []
    assert len(rows) == 1 and rows[0].units == 1020


def test_asset_class_is_guessed_from_the_symbol_and_stays_a_hint():
    assert guess_asset_class("NIFTYBEES") == "etf"
    assert guess_asset_class("LOWVOLIETF") == "etf"
    assert guess_asset_class("HDFCMOMENT") == "etf"
    assert guess_asset_class("BAJFINANCE") == "stk"
    assert guess_asset_class("ITC") == "stk"


def test_seeding_creates_holdings_and_a_reseed_updates_them_in_place(client: TestClient):
    """Re-pasting a corrected sheet must land on the same rows — a second set of duplicate
    holdings beside the first would double the net worth."""
    sheet = _sheet(
        _row(sym="NSE:SEEDTEST", date="26-May-25", bp="100", bu="50", inv="5,000"),
        _row(sym="NSE:SEEDTEST", date="26-Aug-25", su="20", sp="150", pr="3,000"),
    )
    body = {
        "text": sheet,
        "symbols": [{"symbol": "SEEDTEST", "name": "SEEDTEST", "asset_class": "stk"}],
        "broker_account_id": None, "sync": "manual", "replace": True,
    }
    first = client.post("/api/v1/portfolio/seed", json=body).json()
    assert first["summary"] == {"holdings": 1, "created": 1, "trades": 2}
    assert first["seeded"][0]["units"] == pytest.approx(30.0)
    assert first["seeded"][0]["realized"] == pytest.approx(1000.0)  # 20 x (150 - 100)

    second = client.post("/api/v1/portfolio/seed", json=body).json()
    assert second["summary"]["created"] == 0
    assert second["seeded"][0]["units"] == pytest.approx(30.0)  # not 60

    client.delete(f"/api/v1/portfolio/holdings/{second['seeded'][0]['holding_id']}")


def test_a_broken_symbol_is_refused_without_blocking_the_clean_ones(client: TestClient):
    """One unreadable NIFTYBEES row must not hold eleven good symbols hostage — that made the
    seed button permanently dead on a 396-trade paste. The broken symbol is refused if asked
    for, and simply omitted otherwise."""
    sheet = _sheet(
        _row(sym="NSE:GOODSYM", date="26-May-25", bp="100", bu="50"),
        _row(sym="NSE:BADSYM", date="26-May-25", su="10", sp="0"),
    )
    preview = client.post("/api/v1/portfolio/parse-ledger", json={"text": sheet}).json()
    assert preview["errors"] == []  # nothing GLOBAL is wrong
    by_symbol = {s["symbol"]: s for s in preview["symbols"]}
    assert by_symbol["GOODSYM"]["seedable"] is True
    assert by_symbol["BADSYM"]["seedable"] is False
    assert preview["summary"]["seedable"] == 1

    # Asking for the broken one is refused outright...
    refused = client.post("/api/v1/portfolio/seed", json={
        "text": sheet,
        "symbols": [{"symbol": "BADSYM", "name": "BADSYM", "asset_class": "stk"}],
        "broker_account_id": None, "sync": "manual", "replace": True,
    })
    assert refused.status_code == 422

    # ...while the clean one seeds on its own.
    ok = client.post("/api/v1/portfolio/seed", json={
        "text": sheet,
        "symbols": [{"symbol": "GOODSYM", "name": "GOODSYM", "asset_class": "stk"}],
        "broker_account_id": None, "sync": "manual", "replace": True,
    }).json()
    assert ok["summary"]["holdings"] == 1
    names = [h["name"] for h in client.get("/api/v1/portfolio").json()["holdings"]]
    assert "BADSYM" not in names

    client.delete(f"/api/v1/portfolio/holdings/{ok['seeded'][0]['holding_id']}")


def test_a_tabless_paste_still_blocks_every_symbol(client: TestClient):
    """A global failure has no symbol to scope to, so it must stop the whole seed."""
    resp = client.post("/api/v1/portfolio/seed", json={
        "text": "NSE:ITC  26-May-25  442.85  113",
        "symbols": [{"symbol": "ITC", "name": "ITC", "asset_class": "stk"}],
        "broker_account_id": None, "sync": "manual", "replace": True,
    })
    assert resp.status_code == 422
    assert "tab characters" in resp.json()["detail"]
