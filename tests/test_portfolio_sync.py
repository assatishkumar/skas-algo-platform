# ruff: noqa: E501 — the NAVAll fixtures below are VERBATIM feed lines. Wrapping them to fit
# the line limit would change the bytes the parser is being tested against, which is the whole
# point of pasting the real format in.
"""AMFI NAV parsing, the sync path, and the recorded growth history.

Two rules are pinned hard here because breaking either would put a wrong number on a
real-money screen without anything looking broken:

* a sync NEVER invents a price or silently overwrites a ledger;
* the growth history is never back-filled — an absent day is absent, not zero.
"""

from __future__ import annotations

from datetime import date

import pytest

from skas_algo.data import amfi
from skas_algo.db.base import session_scope
from skas_algo.db.models import PortfolioHolding, PortfolioSnapshot, PortfolioTransaction
from skas_algo.services.portfolio_history import growth_series, record_snapshot
from skas_algo.services.portfolio_sync import sync_portfolio

# A trimmed NAVAll.txt in the EIGHT-column layout AMFI actually serves today: Plan and Option
# are their own fields, so the scheme name spans three columns. Includes an AMC heading, the
# header row, blanks, two of the owner's real funds, a single-ISIN row, and a suspended scheme
# publishing "N.A.".
NAVALL = """Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date

Open Ended Schemes(Equity Scheme - Flexi Cap Fund)

PPFAS Mutual Fund

122639;INF879O01027;INF879O01019;Parag Parikh Flexi Cap Fund;Direct Plan;Growth;91.18;28-Aug-2026
150835;INF879O01266;-;Parag Parikh Dynamic Asset Allocation Fund;Direct Plan;Growth;11.82;28-Aug-2026

HDFC Mutual Fund

118989;INF179K01XQ0;INF179K01XR8;HDFC Mid Cap Fund;Direct Plan;Growth Option;237.07;28-Aug-2026
999999;INF000000001;-;Suspended Scheme;Direct Plan;Growth;N.A.;28-Aug-2026
"""

# The SIX-column layout AMFI's own documentation describes, and which it has served before.
# Both must parse: a parser keyed on fixed positions silently drops every row of whichever
# layout it wasn't written for.
NAVALL_SIX_COLUMN = """Scheme Code;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date

PPFAS Mutual Fund

122639;INF879O01027;INF879O01019;Parag Parikh Flexi Cap Fund - Direct Plan - Growth;91.18;28-Aug-2026
"""

NAVALL_PRIOR = NAVALL.replace("91.18", "90.00").replace("28-Aug-2026", "27-Aug-2026")


# ------------------------------------------------------------------ AMFI parsing


def test_both_isin_columns_index_the_same_scheme():
    """The owner may hold either the growth or the reinvestment plan, and the sheet they keep
    is keyed by whichever one their statement prints."""
    rows = amfi.parse_navall(NAVALL)
    assert rows["INF879O01027"].nav == pytest.approx(91.18)
    assert rows["INF879O01019"].scheme_code == "122639"
    assert rows["INF879O01027"].as_of == date(2026, 8, 28)


def test_headings_blank_lines_and_unpriced_schemes_are_skipped():
    rows = amfi.parse_navall(NAVALL)
    assert "INF000000001" not in rows  # "N.A." is not a price
    assert "-" not in rows  # AMFI's filler for a missing second ISIN
    assert len({r.scheme_code for r in rows.values()}) == 3


def test_both_amfi_layouts_parse_identically():
    """The eight-column file splits the scheme name across Name/Plan/Option. Reading fields
    by fixed index made the NAV column land on "Direct Plan", float() failed, and EVERY row
    was dropped — a silently empty feed. Fields are read from the ends instead."""
    wide = amfi.parse_navall(NAVALL)["INF879O01027"]
    narrow = amfi.parse_navall(NAVALL_SIX_COLUMN)["INF879O01027"]
    assert wide.nav == narrow.nav == pytest.approx(91.18)
    assert wide.name == narrow.name == "Parag Parikh Flexi Cap Fund - Direct Plan - Growth"
    assert wide.scheme_code == narrow.scheme_code == "122639"


def test_a_fund_search_needs_every_word_to_match():
    rows = amfi.parse_navall(NAVALL)
    hits = amfi.search("parag flexi direct", rows=rows)
    assert [h.scheme_code for h in hits] == ["122639"]
    assert len(amfi.search("parag", rows=rows)) == 2


def test_nav_staleness_is_measured_not_assumed():
    """A NAV is a day behind during market hours by design — the UI badges the gap rather
    than implying the number is live."""
    assert amfi.stale_days(date(2026, 8, 28), today=date(2026, 8, 31)) == 3
    assert amfi.stale_days(date(2026, 8, 31), today=date(2026, 8, 31)) == 0


# ------------------------------------------------------------------ sync


@pytest.fixture
def amfi_cache(tmp_path, monkeypatch):
    """Two cached NAV files — yesterday's and today's — so a day change is computable."""
    monkeypatch.setenv("SKAS_DATA_HOME", str(tmp_path))
    d = tmp_path / "amfi"
    d.mkdir(parents=True)
    (d / "NAVAll-2026-08-30.txt").write_text(NAVALL_PRIOR, encoding="utf-8")
    (d / "NAVAll-2026-08-31.txt").write_text(NAVALL, encoding="utf-8")
    monkeypatch.setattr(amfi, "refresh", lambda **_: d / "NAVAll-2026-08-31.txt")
    return d


def _make_fund(db, isin: str, units: float | None = 100.0) -> int:
    row = PortfolioHolding(
        name="Parag Parikh Flexi Cap", asset_class="mf", sync="auto",
        sync_source="amfi", sync_ref=isin, units=units,
    )
    db.add(row)
    db.flush()
    return row.id


def test_a_fund_prices_off_its_isin_and_the_day_change_uses_the_prior_nav(amfi_cache):
    with session_scope() as db:
        hid = _make_fund(db, "INF879O01027")
        db.commit()
        report = sync_portfolio(db, holding_ids=[hid])
        row = db.get(PortfolioHolding, hid)

        assert len(report.updated) == 1
        assert row.last_price == pytest.approx(91.18)
        assert row.price_asof == "2026-08-28"
        assert row.value == pytest.approx(100 * 91.18)
        # 91.18 - 90.00 across 100 units. Not zero, and not invented.
        assert row.day_change == pytest.approx(118.0)
        db.delete(row)
        db.commit()


def test_an_isin_the_feed_does_not_carry_is_reported_and_changes_nothing(amfi_cache):
    with session_scope() as db:
        hid = _make_fund(db, "INF000000404")
        db.commit()
        report = sync_portfolio(db, holding_ids=[hid])
        row = db.get(PortfolioHolding, hid)
        assert row.last_price is None and row.value == 0.0
        assert any("not found" in i["reason"] for i in report.issues)
        db.delete(row)
        db.commit()


def test_a_holding_marked_auto_with_no_source_is_surfaced_not_skipped_silently(amfi_cache):
    """'Auto' with nothing to sync from is a config mistake that would otherwise look like a
    holding that simply never moves."""
    with session_scope() as db:
        row = PortfolioHolding(name="Orphan", asset_class="stk", sync="auto", units=1)
        db.add(row)
        db.commit()
        report = sync_portfolio(db, holding_ids=[row.id])
        assert any("no price source" in i["reason"] for i in report.issues)
        db.delete(row)
        db.commit()


def test_the_ledgers_units_win_over_a_typed_count_when_pricing(amfi_cache):
    """Units are a fact once transactions exist; the typed field is only a fallback."""
    with session_scope() as db:
        hid = _make_fund(db, "INF879O01027", units=999.0)
        db.add(PortfolioTransaction(
            holding_id=hid, on_date="2025-01-01", kind="buy", units=10, price=80.0
        ))
        db.commit()
        sync_portfolio(db, holding_ids=[hid])
        row = db.get(PortfolioHolding, hid)
        assert row.value == pytest.approx(10 * 91.18)  # not 999 x 91.18
        db.execute(PortfolioTransaction.__table__.delete())
        db.delete(row)
        db.commit()


# ------------------------------------------------------------------ history


@pytest.fixture
def clean_history():
    """Snapshots are keyed by DATE and the suite shares one DB, so a leftover row from
    another test shifts every index in the series these tests assert on."""
    with session_scope() as db:
        db.execute(PortfolioSnapshot.__table__.delete())
        db.commit()
    yield
    with session_scope() as db:
        db.execute(PortfolioSnapshot.__table__.delete())
        db.commit()


def test_a_second_snapshot_the_same_day_overwrites_rather_than_appends(clean_history):
    with session_scope() as db:
        row = PortfolioHolding(name="Snap", asset_class="stk", invested=100.0, value=150.0)
        db.add(row)
        db.commit()

        first = record_snapshot(db, on_date=date(2026, 3, 2))
        assert first.value == pytest.approx(150.0)
        row.value = 175.0
        db.commit()
        second = record_snapshot(db, on_date=date(2026, 3, 2))

        assert second.id == first.id  # same row, restated
        assert second.value == pytest.approx(175.0)
        assert growth_series(db)["points"] == 1
        db.delete(row)
        db.commit()


def test_a_holding_gets_null_not_zero_before_it_was_tracked(clean_history):
    """Zero would draw a line rising off the floor, implying the money was there and
    worthless. Null leaves the gap the data actually has."""
    with session_scope() as db:
        a = PortfolioHolding(name="Old", asset_class="stk", invested=100.0, value=100.0)
        db.add(a)
        db.commit()
        record_snapshot(db, on_date=date(2026, 4, 1))

        b = PortfolioHolding(name="New", asset_class="etf", invested=50.0, value=60.0)
        db.add(b)
        db.commit()
        record_snapshot(db, on_date=date(2026, 4, 2))

        series = growth_series(db)
        assert series["by_holding"][str(b.id)][0] is None
        assert series["by_holding"][str(b.id)][1] == pytest.approx(60.0)
        assert series["since"] == "2026-04-01"
        assert series["enough_for_trend"] is False  # two points is not a trend

        for row in (a, b):
            db.delete(row)
        db.commit()


# ------------------------------------------------------------------ broker sync


class _FakeAdapter:
    """A broker that holds 12 units of ITC and 5 of a fund we don't track."""

    def __init__(self, units: float = 12.0):
        self._units = units

    def holdings(self):
        return {
            "ITC": {"units": self._units, "avg_price": 400.0},
            "GOLDBEES": {"units": 5.0, "avg_price": 60.0},
        }

    def day_quotes(self, symbols):
        # Quotes every symbol asked for, including ones the book no longer holds.
        return {s: {"last": 450.0, "prev_close": 445.0} for s in symbols}


@pytest.fixture
def fake_broker(monkeypatch):
    from skas_algo.db.models import BrokerAccount

    holder = {"adapter": _FakeAdapter()}
    monkeypatch.setattr(
        "skas_algo.services.broker.make_adapter", lambda account: holder["adapter"]
    )
    with session_scope() as db:
        account = BrokerAccount(broker="zerodha", label="test", user_id="X1")
        db.add(account)
        db.commit()
        account_id = account.id
    yield holder, account_id
    with session_scope() as db:
        db.delete(db.get(BrokerAccount, account_id))
        db.commit()


def _broker_holding(db, account_id: int, units: float | None) -> int:
    row = PortfolioHolding(
        name="ITC", asset_class="stk", sync="auto", sync_source="broker",
        sync_ref="ITC", broker_account_id=account_id, units=units,
    )
    db.add(row)
    db.flush()
    return row.id


def test_a_broker_holding_prices_and_day_changes_off_one_quote_call(fake_broker):
    _, account_id = fake_broker
    with session_scope() as db:
        hid = _broker_holding(db, account_id, None)
        db.commit()
        sync_portfolio(db, holding_ids=[hid])
        row = db.get(PortfolioHolding, hid)
        assert row.units == pytest.approx(12.0)  # adopted from the broker
        assert row.value == pytest.approx(12 * 450.0)
        assert row.day_change == pytest.approx(12 * 5.0)
        assert row.invested == pytest.approx(12 * 400.0)  # broker's average cost
        db.delete(row)
        db.commit()


def test_a_ledger_that_disagrees_with_the_broker_is_reported_not_overwritten(fake_broker):
    """Only the owner can say which side is missing a row. Silently taking the broker's
    number would destroy the one piece of evidence that a transaction is missing."""
    _, account_id = fake_broker
    with session_scope() as db:
        hid = _broker_holding(db, account_id, None)
        db.add(PortfolioTransaction(
            holding_id=hid, on_date="2025-01-01", kind="buy", units=10, price=400.0
        ))
        db.commit()

        report = sync_portfolio(db, holding_ids=[hid])
        row = db.get(PortfolioHolding, hid)

        assert any("missing on one side" in i["reason"] for i in report.issues)
        # Priced off the LEDGER's 10 units, not the broker's 12 — and the ledger is intact.
        assert row.value == pytest.approx(10 * 450.0)
        assert db.query(PortfolioTransaction).filter_by(holding_id=hid).count() == 1

        db.execute(PortfolioTransaction.__table__.delete())
        db.delete(row)
        db.commit()


def test_untracked_broker_positions_are_offered_never_auto_added(fake_broker):
    """Adding them silently would put money on the net-worth tile that the owner never
    chose to track here — and might already be tracked under another name."""
    _, account_id = fake_broker
    with session_scope() as db:
        hid = _broker_holding(db, account_id, None)
        db.commit()
        report = sync_portfolio(db, holding_ids=[hid])
        assert [d["symbol"] for d in report.discovered] == ["GOLDBEES"]
        assert db.query(PortfolioHolding).filter_by(name="GOLDBEES").count() == 0
        db.delete(db.get(PortfolioHolding, hid))
        db.commit()


def test_an_aggregate_holding_is_repriced_but_its_units_are_never_rewritten(fake_broker):
    """The same stock sits in several broker accounts and the row is the TOTAL. No single
    account's book equals those units, so adopting one would silently replace the total with
    a fraction of it — and comparing against one would report a mismatch forever."""
    _, account_id = fake_broker
    with session_scope() as db:
        row = PortfolioHolding(
            name="ITC", asset_class="stk", sync="auto", sync_source="broker",
            sync_ref="ITC", broker_account_id=account_id,
            units=7781.0,           # 7,757 at this broker + 24 at another
            units_locked=True,
        )
        db.add(row)
        db.commit()

        report = sync_portfolio(db, holding_ids=[row.id])
        row = db.get(PortfolioHolding, row.id)

        assert row.units == pytest.approx(7781.0)          # NOT the broker's 12
        assert row.value == pytest.approx(7781.0 * 450.0)  # priced at the live quote
        assert row.day_change == pytest.approx(7781.0 * 5.0)
        # And no mismatch is raised: the account legitimately holds only part of it.
        assert not any("missing on one side" in i["reason"] for i in report.issues)

        db.delete(row)
        db.commit()


def test_an_unlocked_holding_still_adopts_the_brokers_units(fake_broker):
    """The lock is opt-in: a single-broker holding keeps the old behaviour of taking the
    broker's count, which is how a typed guess gets corrected."""
    _, account_id = fake_broker
    with session_scope() as db:
        row = PortfolioHolding(
            name="ITC", asset_class="stk", sync="auto", sync_source="broker",
            sync_ref="ITC", broker_account_id=account_id, units=None,
        )
        db.add(row)
        db.commit()
        sync_portfolio(db, holding_ids=[row.id])
        assert db.get(PortfolioHolding, row.id).units == pytest.approx(12.0)
        db.delete(db.get(PortfolioHolding, row.id))
        db.commit()


# ------------------------------------------------------------------ US equities & crypto


@pytest.fixture
def fake_global(monkeypatch):
    from skas_algo.data import global_quotes as gq

    fx = gq.Quote(symbol="USDINR=X", price=100.0, prev_close=90.0, currency="INR")
    table = {
        "GOOG": gq.Quote("GOOG", price=200.0, prev_close=150.0, currency="USD"),
        "BTC-INR": gq.Quote("BTC-INR", price=7_000_000.0, prev_close=7_100_000.0, currency="INR"),
    }
    monkeypatch.setattr(gq, "usd_inr", lambda *a, **k: fx)
    monkeypatch.setattr(
        gq, "quotes",
        lambda syms, **k: ({s: table[s] for s in syms if s in table},
                           {s: "not carried by the feed" for s in syms if s not in table}),
    )
    return table


def _global_holding(db, name, ref, units, cls="us") -> int:
    row = PortfolioHolding(
        name=name, asset_class=cls, sync="auto", sync_source="global",
        sync_ref=ref, units=units, units_locked=True,
    )
    db.add(row)
    db.flush()
    return row.id


def test_a_usd_holding_converts_and_its_day_change_carries_the_currency_move(fake_global):
    """Both ends of the day change are converted at their OWN rate. Using today's rate for
    yesterday too would report the share's move and silently drop the rupee's — which on a
    day the currency moves 1% is the larger half of what actually happened."""
    with session_scope() as db:
        hid = _global_holding(db, "GOOG", "GOOG", 10.0)
        db.commit()
        sync_portfolio(db, holding_ids=[hid])
        row = db.get(PortfolioHolding, hid)

        assert row.last_price == pytest.approx(200.0 * 100.0)      # priced in INR
        assert row.value == pytest.approx(10 * 20_000.0)
        # today 200 x 100 = 20,000 ; yesterday 150 x 90 = 13,500 -> 6,500 a share.
        assert row.day_change == pytest.approx(10 * 6_500.0)
        db.delete(row)
        db.commit()


def test_an_inr_quoted_pair_is_not_converted_twice(fake_global):
    """Yahoo carries BTC-INR directly. Multiplying it by USD/INR would report a bitcoin at
    a hundred times its price, and the error is large enough to swamp the whole portfolio."""
    with session_scope() as db:
        hid = _global_holding(db, "BTC", "BTC-INR", 0.5, cls="btc")
        db.commit()
        sync_portfolio(db, holding_ids=[hid])
        row = db.get(PortfolioHolding, hid)

        assert row.last_price == pytest.approx(7_000_000.0)
        assert row.day_change == pytest.approx(0.5 * -100_000.0)
        db.delete(row)
        db.commit()


def test_a_symbol_the_feed_does_not_carry_is_reported_and_left_alone(fake_global):
    with session_scope() as db:
        hid = _global_holding(db, "Nonsense", "ZZZZ", 5.0)
        db.commit()
        report = sync_portfolio(db, holding_ids=[hid])
        row = db.get(PortfolioHolding, hid)
        assert row.last_price is None and row.value == 0.0
        assert any("No quote for ZZZZ" in i["reason"] for i in report.issues)
        db.delete(row)
        db.commit()


def test_the_quote_parser_reads_yahoos_shape_without_touching_the_network():
    """Price and currency come from `meta`; the previous close comes from the candle series
    (see the next test for why that distinction matters)."""
    import requests

    from skas_algo.data import global_quotes as gq

    class _Resp:
        status_code = 200
        def raise_for_status(self): return None
        def json(self):
            return {"chart": {"result": [{
                "meta": {"regularMarketPrice": 335.41, "currency": "USD",
                         "exchangeName": "NMS"},
                "indicators": {"quote": [{"close": [330.0, 342.88, 335.41]}]},
            }]}}

    class _Sess(requests.Session):
        def get(self, *a, **k): return _Resp()

    got, errors = gq.quotes(["GOOG"], session=_Sess())
    assert errors == {}
    q = got["GOOG"]
    assert (q.price, q.prev_close, q.currency) == (335.41, 342.88, "USD")


def test_the_previous_close_is_the_prior_SESSION_not_the_prior_window():
    """Yahoo's `chartPreviousClose` is the close before the requested WINDOW, so on a 5-day
    range it is a week old. Trusting it reported MSFT at +5.65% on a day it fell 1.22% — the
    wrong SIGN, not just the wrong size. The candle series settles it."""
    import requests

    from skas_algo.data import global_quotes as gq

    class _Resp:
        status_code = 200
        def raise_for_status(self): return None
        def json(self):
            return {"chart": {"result": [{
                "meta": {"regularMarketPrice": 507.29, "chartPreviousClose": 487.31,
                         "currency": "USD"},
                "indicators": {"quote": [{"close": [491.71, 496.37, 505.06, 513.53, 507.29]}]},
            }]}}

    class _Sess(requests.Session):
        def get(self, *a, **k): return _Resp()

    q = gq.quotes(["MSFT"], session=_Sess())[0]["MSFT"]
    assert q.prev_close == pytest.approx(513.53)     # yesterday, not the 487.31 week-ago close
    assert q.day_move == pytest.approx(-6.24)
    assert q.day_move < 0


def test_a_price_newer_than_the_last_candle_uses_that_candle_as_the_prior_close():
    """Before the day's candle forms, the live price is ahead of the series — the last close
    IS then the prior session and must not be skipped over."""
    import requests

    from skas_algo.data import global_quotes as gq

    class _Resp:
        status_code = 200
        def raise_for_status(self): return None
        def json(self):
            return {"chart": {"result": [{
                "meta": {"regularMarketPrice": 110.0, "currency": "USD"},
                "indicators": {"quote": [{"close": [98.0, 100.0]}]},
            }]}}

    class _Sess(requests.Session):
        def get(self, *a, **k): return _Resp()

    q = gq.quotes(["X"], session=_Sess())[0]["X"]
    assert q.prev_close == pytest.approx(100.0)
    assert q.day_move == pytest.approx(10.0)


def test_a_live_account_slice_updates_while_the_other_brokers_hold(fake_broker):
    """value_investing buys at Dhan every day, so that slice must move — but Zerodha's and
    IIFL's positions live only in a statement no sync can see, and zeroing them would erase
    most of the holding. Only the slice for the account just read is rewritten."""
    holder, account_id = fake_broker
    with session_scope() as db:
        row = PortfolioHolding(
            name="ITC", asset_class="stk", sync="auto", sync_source="broker",
            sync_ref="ITC", broker_account_id=account_id, units=7781.0, units_locked=True,
            broker_units={"static": 7769.0, f"account:{account_id}": 12.0},
        )
        db.add(row)
        db.commit()

        holder["adapter"] = _FakeAdapter(units=20.0)      # the strategy bought 8 more
        sync_portfolio(db, holding_ids=[row.id])
        row = db.get(PortfolioHolding, row.id)

        assert row.broker_units["static"] == pytest.approx(7769.0)   # untouched
        assert row.broker_units[f"account:{account_id}"] == pytest.approx(20.0)
        assert row.units == pytest.approx(7789.0)                    # re-totalled
        assert row.value == pytest.approx(7789.0 * 450.0)
        db.delete(row)
        db.commit()


def test_selling_out_at_one_broker_zeroes_only_that_slice(fake_broker):
    """A successful book read that doesn't list the symbol means zero held THERE — a real
    reduction, not a failed read, and skipping it would leave phantom units forever."""
    holder, account_id = fake_broker
    with session_scope() as db:
        row = PortfolioHolding(
            name="SOLDOUT", asset_class="stk", sync="auto", sync_source="broker",
            sync_ref="SOLDOUT", broker_account_id=account_id, units=110.0, units_locked=True,
            broker_units={"static": 100.0, f"account:{account_id}": 10.0},
        )
        db.add(row)
        db.commit()
        sync_portfolio(db, holding_ids=[row.id])
        row = db.get(PortfolioHolding, row.id)
        assert row.broker_units[f"account:{account_id}"] == pytest.approx(0.0)
        assert row.units == pytest.approx(100.0)
        db.delete(row)
        db.commit()


def test_a_holding_priced_by_one_account_still_has_its_slice_at_another_refreshed(monkeypatch):
    """ITC is priced from Zerodha but part of it sits at Dhan, where value_investing buys it
    daily. Grouping only by the quote account means Dhan's pass never visits the row and the
    slice goes stale forever — the exact daily update this exists to provide."""
    from skas_algo.db.models import BrokerAccount

    class _Zerodha:
        def holdings(self): return {"ITC": {"units": 7769.0, "avg_price": 300.0}}
        def day_quotes(self, syms): return {s: {"last": 260.0, "prev_close": 250.0} for s in syms}

    class _Dhan:
        def holdings(self): return {"ITC": {"units": 25.0, "avg_price": 269.0}}
        def day_quotes(self, syms): return {}

    with session_scope() as db:
        z = BrokerAccount(broker="zerodha", label="z", user_id="Z1")
        d = BrokerAccount(broker="dhan", label="d", user_id="D1")
        db.add_all([z, d])
        db.commit()
        zid, did = z.id, d.id

    monkeypatch.setattr(
        "skas_algo.services.broker.make_adapter",
        lambda a: _Zerodha() if (a.broker or "") == "zerodha" else _Dhan(),
    )
    with session_scope() as db:
        row = PortfolioHolding(
            name="ITC", asset_class="stk", sync="auto", sync_source="broker", sync_ref="ITC",
            broker_account_id=zid,                       # priced by Zerodha…
            units=7781.0, units_locked=True,
            broker_units={"static": 7769.0, f"account:{did}": 12.0},   # …units at BOTH
        )
        db.add(row)
        db.commit()

        sync_portfolio(db, holding_ids=[row.id])
        row = db.get(PortfolioHolding, row.id)

        assert row.broker_units[f"account:{did}"] == pytest.approx(25.0)  # Dhan's pass ran
        assert row.broker_units["static"] == pytest.approx(7769.0)        # statement held
        assert row.units == pytest.approx(7794.0)
        assert row.last_price == pytest.approx(260.0)                     # Zerodha priced it

        db.delete(row)
        for acct_id in (zid, did):
            db.delete(db.get(BrokerAccount, acct_id))
        db.commit()


def test_a_weekend_nav_file_does_not_shadow_the_last_business_day(tmp_path, monkeypatch):
    """AMFI publishes on weekends for overnight and liquid schemes only — ~630 of 14,137. If
    the newest older file is taken as *the* prior wholesale, every equity fund loses its
    comparison and reports a day change of zero. The prior is assembled per ISIN instead."""
    monkeypatch.setenv("SKAS_DATA_HOME", str(tmp_path))
    d = tmp_path / "amfi"
    d.mkdir(parents=True)

    friday = NAVALL.replace("91.18", "90.00").replace("28-Aug-2026", "28-Aug-2026")
    # Sunday: only the one liquid-ish scheme publishes.
    sunday = "\n".join([
        NAVALL.splitlines()[0],
        "118989;INF179K01XQ0;INF179K01XR8;HDFC Mid Cap Fund;Direct Plan;Growth Option;238.00;30-Aug-2026",
    ])
    (d / "NAVAll-2026-08-28.txt").write_text(friday, encoding="utf-8")
    (d / "NAVAll-2026-08-30.txt").write_text(sunday, encoding="utf-8")
    (d / "NAVAll-2026-08-31.txt").write_text(
        NAVALL.replace("28-Aug-2026", "31-Aug-2026"), encoding="utf-8")

    _, prev = amfi.load(fetch=False)
    # The equity fund falls through the thin Sunday file to Friday's NAV…
    assert prev["INF879O01027"].nav == pytest.approx(90.00)
    assert prev["INF879O01027"].as_of == date(2026, 8, 28)
    # …while the one Sunday DID publish uses Sunday's, being the newer of the two.
    assert prev["INF179K01XQ0"].nav == pytest.approx(238.00)
    assert prev["INF179K01XQ0"].as_of == date(2026, 8, 30)
