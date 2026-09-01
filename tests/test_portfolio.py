"""The /portfolio money math: FIFO lots, cost basis, XIRR, per-lot tax, history.

The theme of these tests is that a number the screen shows must be traceable to a rule, not to
a plausible-looking arithmetic accident. The per-lot tax cases matter most — a position built
over years sits on BOTH sides of the 12-month line, and a per-holding shortcut would quietly
tax the whole thing at one rate.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from skas_algo.services import portfolio as pf
from skas_algo.services.portfolio import build_ledger, holding_view


def _txn(on_date: str, kind: str, units: float, price: float, fees: float = 0.0) -> dict:
    return {"on_date": on_date, "kind": kind, "units": units, "price": price, "fees": fees}


# ------------------------------------------------------------------ FIFO ledger


def test_a_partial_sell_consumes_the_oldest_lot_first():
    led = build_ledger([
        _txn("2023-01-10", "buy", 100, 100.0),
        _txn("2024-06-10", "buy", 100, 200.0),
        _txn("2025-01-10", "sell", 120, 250.0),
    ])
    # 100 units off the ₹100 lot, then 20 off the ₹200 lot — 80 units of the newer lot remain.
    assert led.units == pytest.approx(80.0)
    assert led.cost == pytest.approx(80 * 200.0)
    # Realized: (100 x 250 - 100 x 100) + (20 x 250 - 20 x 200) = 15,000 + 1,000
    assert led.realized == pytest.approx(16_000.0)
    assert [d.bought_on for d in led.disposals] == [date(2023, 1, 10), date(2024, 6, 10)]


def test_fees_raise_a_buys_cost_and_reduce_a_sales_proceeds():
    led = build_ledger([
        _txn("2024-01-01", "buy", 10, 100.0, fees=50.0),
        _txn("2025-01-01", "sell", 10, 120.0, fees=30.0),
    ])
    # cost 1,050 ; proceeds 1,170 → 120, not the fee-blind 200.
    assert led.cost == pytest.approx(0.0)
    assert led.realized == pytest.approx(120.0)


def test_selling_more_than_was_ever_bought_is_reported_not_swallowed():
    """The only ways to get here are a missing buy row or a typo. Silently clamping would
    show a plausible cost basis that is wrong, and nothing would ever point at the cause."""
    led = build_ledger([
        _txn("2024-01-01", "buy", 10, 100.0),
        _txn("2024-06-01", "sell", 25, 150.0),
    ])
    assert led.units == pytest.approx(0.0)
    assert led.oversold == pytest.approx(15.0)


def test_transactions_are_replayed_in_date_order_not_insertion_order():
    """An imported tradebook arrives in whatever order the broker exported it."""
    shuffled = build_ledger([
        _txn("2025-01-10", "sell", 50, 300.0),
        _txn("2023-01-10", "buy", 100, 100.0),
    ])
    assert shuffled.units == pytest.approx(50.0)
    assert shuffled.realized == pytest.approx(50 * 300.0 - 50 * 100.0)


# ------------------------------------------------------------------ returns


def test_a_ledger_gives_a_real_xirr_not_a_lump_sum_approximation():
    """Money that went in across years has no single start date, so a cost->value power law
    is the wrong shape. The two answers differ enough to change a decision."""
    view = holding_view(
        {"id": 1, "name": "Staggered", "asset_class": "stk", "last_price": 200.0},
        [_txn("2022-01-01", "buy", 100, 100.0), _txn("2025-01-01", "buy", 100, 180.0)],
        today=date(2026, 1, 1),
    )
    assert view["return_basis"] == "xirr"
    assert view["units"] == pytest.approx(200.0)
    assert view["invested"] == pytest.approx(28_000.0)
    assert view["value"] == pytest.approx(40_000.0)
    # A naive (40000/28000)^(1/4) reads ~9.3%; the real money-weighted answer is well above
    # it, because most of the money only worked for a year.
    assert view["xirr_pct"] > 12.0


def test_a_holding_with_no_ledger_still_gets_a_return_and_says_so():
    view = holding_view(
        {"id": 2, "name": "PPF", "asset_class": "ppf", "invested": 100_000.0,
         "value": 200_000.0, "buy_month": "2016-01"},
        [],
        today=date(2026, 1, 1),
    )
    assert view["basis"] == "summary"
    assert view["return_basis"] == "annualised"
    assert view["xirr_pct"] == pytest.approx(7.18, abs=0.05)  # 2x over 10y


def test_a_stated_return_beats_every_derived_one():
    """A broker statement's own XIRR knows about corporate actions and pre-migration history
    that the ledger here does not."""
    view = holding_view(
        {"id": 3, "name": "Fund", "asset_class": "mf", "xirr_pct": 15.5, "last_price": 100.0},
        [_txn("2024-01-01", "buy", 10, 90.0)],
        today=date(2026, 1, 1),
    )
    assert view["xirr_pct"] == pytest.approx(15.5)
    assert view["return_basis"] == "stated"


def test_units_from_the_ledger_outrank_a_stale_typed_value():
    """Once a ledger exists, units are a fact — value follows units x price, so an old typed
    value can never contradict the transactions."""
    view = holding_view(
        {"id": 4, "name": "X", "asset_class": "stk", "value": 999_999.0, "last_price": 50.0},
        [_txn("2024-01-01", "buy", 10, 40.0)],
        today=date(2026, 1, 1),
    )
    assert view["value"] == pytest.approx(500.0)


# ------------------------------------------------------------------ tax, per lot


def test_one_position_can_be_long_and_short_term_at_the_same_time():
    """The whole reason tax is computed per lot. Bought twice, a year apart: on the valuation
    date one parcel is past 12 months and one is not, so the position owes BOTH rates."""
    view = holding_view(
        {"id": 5, "name": "Split", "asset_class": "stk", "last_price": 200.0},
        [_txn("2024-01-01", "buy", 100, 100.0), _txn("2025-10-01", "buy", 100, 150.0)],
        today=date(2026, 1, 1),
    )
    regimes = {lot["regime"] for lot in view["tax"]["lots"]}
    assert regimes == {"LTCG 12.5%", "STCG 20%"}
    assert view["tax"]["ltcg"] > 0 and view["tax"]["stcg"] > 0


def test_the_equity_ltcg_exemption_is_applied_once_across_the_whole_portfolio():
    """It is a per-taxpayer allowance. Subtracting it per holding would understate a bill by
    lakhs on a portfolio with several equity positions."""
    rows = [
        holding_view(
            {"id": i, "name": f"H{i}", "asset_class": "stk", "last_price": 300.0},
            [_txn("2020-01-01", "buy", 1000, 100.0)],
            today=date(2026, 1, 1),
        )
        for i in (1, 2, 3)
    ]
    gross = sum(r["tax"]["estimate"] for r in rows)
    total = pf.apply_equity_exemption(rows)
    relief = pf.EQUITY_LTCG_EXEMPTION * pf.LTCG_RATE
    assert total == pytest.approx(gross - relief, abs=1.0)


def test_crypto_losses_are_not_harvestable():
    """VDA losses cannot be set off against anything — counting them would promise a tax
    saving that does not exist."""
    view = holding_view(
        {"id": 6, "name": "BTC", "asset_class": "btc", "last_price": 50.0},
        [_txn("2025-01-01", "buy", 10, 100.0)],
        today=date(2026, 1, 1),
    )
    assert view["gain"] < 0
    assert view["tax"]["harvestable"] == pytest.approx(0.0)


def test_a_debt_fund_is_taxed_at_slab_even_though_its_class_is_mf():
    view = holding_view(
        {"id": 7, "name": "Corp Bond", "asset_class": "mf", "kind_override": "debt",
         "last_price": 120.0},
        [_txn("2020-01-01", "buy", 100, 100.0)],
        today=date(2026, 1, 1),
    )
    assert view["kind"] == "debt"
    assert view["tax"]["lots"][0]["regime"] == "Slab rate"


def test_us_equity_needs_twenty_four_months_not_twelve():
    common = {"id": 8, "name": "AAPL", "asset_class": "us", "last_price": 200.0}
    young = holding_view(common, [_txn("2025-01-01", "buy", 10, 100.0)], today=date(2026, 6, 1))
    old = holding_view(common, [_txn("2023-01-01", "buy", 10, 100.0)], today=date(2026, 6, 1))
    assert young["tax"]["lots"][0]["regime"] == "Slab rate"
    assert old["tax"]["lots"][0]["regime"] == "LTCG 12.5%"


def test_ppf_and_epf_are_exempt_and_contribute_no_tax():
    view = holding_view(
        {"id": 9, "name": "EPF", "asset_class": "epf", "invested": 100_000.0,
         "value": 400_000.0, "units": 1, "buy_month": "2014-07"},
        [], today=date(2026, 1, 1),
    )
    assert view["tax"]["estimate"] == pytest.approx(0.0)
    assert view["tax"]["exempt"] > 0


# ------------------------------------------------------------------ API round trip


def test_a_holding_with_an_imported_ledger_reports_its_real_position(client: TestClient):
    created = client.post("/api/v1/portfolio/holdings", json={
        "name": "Test Bank", "asset_class": "stk", "last_price": 250.0,
    })
    assert created.status_code == 200
    hid = created.json()["id"]

    imported = client.post("/api/v1/portfolio/transactions/import", json={
        "holding_id": hid,
        "replace": True,
        "rows": [
            {"on_date": "2023-01-10", "kind": "buy", "units": 100, "price": 100.0},
            {"on_date": "2024-06-10", "kind": "buy", "units": 100, "price": 200.0},
            {"on_date": "2025-01-10", "kind": "sell", "units": 120, "price": 250.0},
        ],
    })
    assert imported.status_code == 200
    assert imported.json()["units"] == pytest.approx(80.0)

    body = client.get("/api/v1/portfolio").json()
    row = next(h for h in body["holdings"] if h["id"] == hid)
    assert row["basis"] == "ledger"
    assert row["value"] == pytest.approx(80 * 250.0)
    assert row["realized"] == pytest.approx(16_000.0)
    assert row["txn_count"] == 3

    client.delete(f"/api/v1/portfolio/holdings/{hid}")


def test_reimporting_with_replace_does_not_double_the_position(client: TestClient):
    """The re-import path is how a corrected export gets loaded; appending would silently
    double every buy and the cost basis would look merely 'a bit high'."""
    hid = client.post("/api/v1/portfolio/holdings", json={
        "name": "Reimport", "asset_class": "etf", "last_price": 100.0,
    }).json()["id"]
    rows = [{"on_date": "2024-01-01", "kind": "buy", "units": 10, "price": 90.0}]
    for _ in range(3):
        client.post("/api/v1/portfolio/transactions/import",
                    json={"holding_id": hid, "replace": True, "rows": rows})
    body = client.get("/api/v1/portfolio").json()
    assert next(h for h in body["holdings"] if h["id"] == hid)["units"] == pytest.approx(10.0)
    client.delete(f"/api/v1/portfolio/holdings/{hid}")


def test_deleting_a_holding_prunes_it_from_buckets_and_goals(client: TestClient):
    """A dangling id would leave a bucket's target share quietly wrong forever."""
    hid = client.post("/api/v1/portfolio/holdings", json={
        "name": "Doomed", "asset_class": "stk", "invested": 100.0, "value": 120.0,
    }).json()["id"]
    bid = client.post("/api/v1/portfolio/buckets", json={
        "name": "B", "target_pct": 50, "holding_ids": [hid],
    }).json()["id"]
    gid = client.post("/api/v1/portfolio/goals", json={
        "name": "G", "target_amount": 1000, "target_year": 2030, "holding_ids": [hid],
    }).json()["id"]

    client.delete(f"/api/v1/portfolio/holdings/{hid}")
    body = client.get("/api/v1/portfolio").json()
    assert next(b for b in body["buckets"] if b["id"] == bid)["holding_ids"] == []
    assert next(g for g in body["goals"] if g["id"] == gid)["holding_ids"] == []
    client.delete(f"/api/v1/portfolio/buckets/{bid}")
    client.delete(f"/api/v1/portfolio/goals/{gid}")


# ------------------------------------------------------------------ the tag taxonomy


def test_every_asset_class_maps_to_a_real_tag_and_the_targets_sum_to_100():
    """The tag is the level rebalancing happens at, so an unmapped class would be money with
    no target — invisible to the one screen meant to catch drift."""
    assert set(pf.KIND_TARGETS) == set(pf.KINDS)
    assert sum(pf.KIND_TARGETS.values()) == pytest.approx(100.0)
    for key, meta in pf.ASSET_CLASSES.items():
        assert meta["kind"] in pf.KINDS, key
        assert key in {k: v for k, v in pf.ASSET_CLASSES.items()}
    assert set(pf.KIND_LABELS) == set(pf.KINDS) == set(pf.KIND_COLORS)


def test_gold_and_property_are_their_own_tags_not_an_alternatives_bucket():
    """Lumping them together hides the two positions most worth stating separately: one is a
    liquid inflation hedge, the other is a house."""
    assert pf.ASSET_CLASSES["gold"]["kind"] == "gold"
    assert pf.ASSET_CLASSES["re"]["kind"] == "realestate"
    assert pf.ASSET_CLASSES["btc"]["kind"] == "crypto"


def test_an_unrecognised_tag_falls_back_to_the_class_default():
    """A typo in an override must not create a sixth tag that no target covers."""
    assert pf.kind_of("mf", "debt") == "debt"
    assert pf.kind_of("mf", "nonsense") == "equity"
    assert pf.kind_of("gold", None) == "gold"


def test_a_debt_tagged_fund_is_still_taxed_at_slab(client: TestClient):
    """The tag drives BOTH allocation and the tax regime, so retagging a fund as debt must
    keep taxing it at slab — silently changing one and not the other would misstate a bill."""
    hid = client.post("/api/v1/portfolio/holdings", json={
        "name": "Debt fund", "asset_class": "mf", "kind_override": "debt",
        "invested": 100_000, "units": 1000, "value": 130_000, "buy_month": "2020-01",
    }).json()["id"]
    row = next(h for h in client.get("/api/v1/portfolio").json()["holdings"] if h["id"] == hid)
    assert row["kind"] == "debt"
    assert row["tax"]["lots"][0]["regime"] == "Slab rate"
    client.delete(f"/api/v1/portfolio/holdings/{hid}")
