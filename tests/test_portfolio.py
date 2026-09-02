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


# ------------------------------------------------------------------ distributions


def test_expected_income_is_derived_from_the_position_not_stored():
    """A forecast typed once ages into fiction the moment the holding changes size. Expected
    income is value x yield, so it moves with the position."""
    rows = [{"id": 1, "name": "REIT", "asset_class": "re", "value": 1_000_000,
             "invested": 800_000, "dividend_yield_pct": 8.0}]
    view = pf.income_view(rows, [], today=date(2026, 9, 1))
    assert view["expected_annual"] == pytest.approx(80_000)
    assert view["expected_monthly"] == pytest.approx(80_000 / 12)
    # Yield ON COST says what the original decision returns; on value only says what today's
    # price would buy.
    assert view["lines"][0]["yield_on_cost_pct"] == pytest.approx(10.0)


def test_a_holding_with_no_yield_is_excluded_not_counted_as_zero():
    """'We don't know' and 'it pays nothing' lead to different decisions, so the unpriced
    share is reported rather than quietly dragging the portfolio yield down."""
    rows = [
        {"id": 1, "name": "REIT", "asset_class": "re", "value": 100_000,
         "invested": 100_000, "dividend_yield_pct": 10.0},
        {"id": 2, "name": "Growth", "asset_class": "stk", "value": 300_000,
         "invested": 300_000, "dividend_yield_pct": None},
    ]
    view = pf.income_view(rows, [], today=date(2026, 9, 1))
    assert view["expected_annual"] == pytest.approx(10_000)
    assert view["unpriced_value"] == pytest.approx(300_000)
    assert view["unpriced_share_pct"] == pytest.approx(75.0)


def test_income_is_bucketed_by_the_INDIAN_financial_year():
    """1 April to 31 March. Keying on the calendar year would be wrong for nine months of
    every twelve, and this is the figure a tax return is filled from."""
    assert pf.financial_year("2026-03-31") == "FY26"
    assert pf.financial_year("2026-04-01") == "FY27"

    rows = [{"id": 1, "name": "REIT", "asset_class": "re", "value": 100_000,
             "invested": 100_000, "dividend_yield_pct": 10.0}]
    divs = [
        {"holding_id": 1, "on_date": "2026-03-20", "amount": 5_000},   # last FY
        {"holding_id": 1, "on_date": "2026-06-20", "amount": 7_000},   # this FY
    ]
    view = pf.income_view(rows, divs, today=date(2026, 9, 1))
    assert view["fy"] == "FY27"
    assert view["received_fy"] == pytest.approx(7_000)
    assert view["received_total"] == pytest.approx(12_000)


def test_recording_and_deleting_a_distribution_round_trips(client: TestClient):
    hid = client.post("/api/v1/portfolio/holdings", json={
        "name": "PGINVIT test", "asset_class": "re", "invested": 100_000, "units": 1000,
        "value": 120_000, "dividend_yield_pct": 11.0,
    }).json()["id"]
    did = client.post("/api/v1/portfolio/dividends", json={
        "holding_id": hid, "on_date": "2026-06-15", "amount": 13_200, "note": "Q1",
    }).json()["id"]

    body = client.get("/api/v1/portfolio").json()
    line = next(x for x in body["income"]["lines"] if x["holding_id"] == hid)
    assert line["expected_annual"] == pytest.approx(13_200)
    assert line["received_fy"] == pytest.approx(13_200)
    assert body["income"]["received_total"] == pytest.approx(13_200)

    client.delete(f"/api/v1/portfolio/dividends/{did}")
    assert client.get("/api/v1/portfolio").json()["income"]["received_total"] == pytest.approx(0)
    client.delete(f"/api/v1/portfolio/holdings/{hid}")


def test_deleting_a_holding_takes_its_distributions_with_it(client: TestClient):
    """An orphan payment would keep inflating 'received all time' with no row to explain it."""
    hid = client.post("/api/v1/portfolio/holdings", json={
        "name": "Doomed payer", "asset_class": "re", "invested": 1000, "value": 1200,
    }).json()["id"]
    client.post("/api/v1/portfolio/dividends", json={
        "holding_id": hid, "on_date": "2026-06-15", "amount": 500,
    })
    client.delete(f"/api/v1/portfolio/holdings/{hid}")
    assert client.get("/api/v1/portfolio").json()["income"]["received_total"] == pytest.approx(0)


# ------------------------------------------------------------------ goals as a schedule


def test_a_goals_entered_amount_is_todays_money_and_is_inflated_to_when_it_is_needed():
    """School fees of 7 L a year 2031-2036 are 42 L as entered and 65 L when actually paid.
    Planning against the raw figure understates the goal by more than half."""
    goal = {
        "schedule": [{"year": y, "amount": 700_000} for y in range(2031, 2037)],
        "inflation_pct": 6.0, "monthly_sip": 0,
    }
    p = pf.goal_projection(goal, current_value=0, return_pct=12.0, today=date(2026, 9, 1))
    assert p["total_today"] == pytest.approx(4_200_000)
    assert p["total_nominal"] == pytest.approx(6_534_185, rel=1e-4)
    assert p["total_nominal"] > p["total_today"] * 1.5


def test_a_goal_can_be_fully_funded_in_total_and_still_run_out_early():
    """The failure a single target number cannot express: enough money overall, needed before
    it has finished compounding."""
    goal = {
        "schedule": [{"year": 2027, "amount": 5_000_000}, {"year": 2045, "amount": 1_000_000}],
        "inflation_pct": 6.0, "monthly_sip": 0,
    }
    p = pf.goal_projection(goal, current_value=3_000_000, return_pct=12.0,
                           today=date(2026, 9, 1))
    assert p["first_shortfall_year"] == 2027
    # The gap is stated in the rupees of the year it happens, and both years count.
    y27 = next(r for r in p["rows"] if r["year"] == 2027)
    y45 = next(r for r in p["rows"] if r["year"] == 2045)
    assert y27["shortfall"] > 0 and y45["shortfall"] > 0
    assert p["shortfall_total"] == pytest.approx(y27["shortfall"] + y45["shortfall"])


def test_the_current_year_only_earns_the_months_that_are_left_of_it():
    """On 1 September, four months remain. Crediting a full year of growth and twelve SIPs
    would hand the plan money it never receives, and near-term goals are exactly where that
    error is least affordable."""
    goal = {"schedule": [{"year": 2027, "amount": 1_000_000}], "inflation_pct": 0.0,
            "monthly_sip": 100_000}
    sept = pf.goal_projection(goal, current_value=1_000_000, return_pct=12.0,
                              today=date(2026, 9, 1))
    january = pf.goal_projection(goal, current_value=1_000_000, return_pct=12.0,
                                 today=date(2026, 1, 1))
    first_sept = next(r for r in sept["rows"] if r["year"] == 2026)
    first_jan = next(r for r in january["rows"] if r["year"] == 2026)
    assert first_sept["corpus_after"] < first_jan["corpus_after"]
    # Four SIPs plus a third of a year's growth, not twelve and a full year.
    assert first_sept["corpus_after"] == pytest.approx(
        1_000_000 * (1.12 ** (4 / 12)) + 100_000 * 4 * (1 + 0.12 * (4 / 12) / 2), rel=1e-9)


def test_a_shortfall_does_not_compound_at_the_investment_return():
    """Money you do not have cannot grow at 12%. Letting the corpus go negative turned a
    ~19 L gap in 2027 into 1.48 Cr of fictional debt by 2045 — frightening enough to talk
    someone out of a plan that was one year tight."""
    goal = {
        "schedule": [{"year": 2027, "amount": 5_000_000}, {"year": 2045, "amount": 1_000_000}],
        "inflation_pct": 6.0, "monthly_sip": 0,
    }
    p = pf.goal_projection(goal, current_value=3_000_000, return_pct=12.0,
                           today=date(2026, 9, 1))
    assert all(r["corpus_after"] >= 0 for r in p["rows"])
    assert p["final_corpus"] >= 0


def test_a_recurring_goal_survives_when_the_sip_and_growth_cover_each_year():
    goal = {
        "schedule": [{"year": y, "amount": 800_000} for y in range(2027, 2049)],
        "inflation_pct": 6.0, "monthly_sip": 200_000,
    }
    p = pf.goal_projection(goal, current_value=20_000_000, return_pct=12.0,
                           today=date(2026, 9, 1))
    assert p["first_shortfall_year"] is None
    assert p["years"] == 2048 - 2026 + 1


def test_money_still_flowing_into_a_linked_holding_funds_the_goal():
    """A PPF being paid ₹12,500/month is not a static pot (owner, 2026-09-02 — Arya College
    read "short from 2036" because the projection grew the balance but ignored every future
    instalment). The linked holding's own contribution stream must ride the walk."""
    goal = {"schedule": [{"year": 2036, "amount": 4_200_000}], "inflation_pct": 0.0}
    without = pf.goal_projection(goal, current_value=1_300_000, return_pct=8.0,
                                 today=date(2026, 9, 1))
    with_ppf = pf.goal_projection(
        goal, current_value=1_300_000, return_pct=8.0, today=date(2026, 9, 1),
        contributions=[{"monthly": 12_500, "until_year": None}],
    )
    # Ten years of ₹12.5k/month at 8% is ~₹22 L — the difference between short and funded.
    assert without["first_shortfall_year"] == 2036 and without["shortfall_total"] > 0
    assert with_ppf["first_shortfall_year"] is None and with_ppf["shortfall_total"] == 0
    assert with_ppf["final_corpus"] > 1_000_000    # pays the ₹42 L and still has money left


def test_a_contribution_stream_stops_at_its_maturity_year():
    """The mirror of the holding's own accrual: a stream with ``until_year`` pays through
    that year and not a rupee after — a matured deposit must not keep funding the plan."""
    goal = {"schedule": [{"year": 2040, "amount": 1}], "inflation_pct": 0.0}
    forever = pf.goal_projection(
        goal, current_value=0, return_pct=0.0, today=date(2026, 1, 1),
        contributions=[{"monthly": 1_000, "until_year": None}],
    )
    matured = pf.goal_projection(
        goal, current_value=0, return_pct=0.0, today=date(2026, 1, 1),
        contributions=[{"monthly": 1_000, "until_year": 2028}],
    )
    # At 0% the arithmetic is exact: 15 years vs 3 years of ₹12k/yr (minus the ₹1 outflow).
    assert forever["final_corpus"] == pytest.approx(15 * 12_000 - 1)
    assert matured["final_corpus"] == pytest.approx(3 * 12_000 - 1)


def test_the_owners_growth_assumption_outranks_the_derived_return(client: TestClient):
    """The derived rate is honest but brittle — one linked holding with a short history
    drags a 15-year plan to +0.4%/yr (owner, 2026-09-02). A typed assumption wins; blank
    keeps the derivation, and the response says which one it used."""
    hid = client.post("/api/v1/portfolio/holdings", json={
        "name": "NIFTYBEES", "asset_class": "stk", "invested": 3_000_000, "value": 3_680_000,
    }).json()["id"]
    gid = client.post("/api/v1/portfolio/goals", json={
        "name": "Marriage", "schedule": [{"year": 2040, "amount": 3_000_000}],
        "allocations": [{"holding_id": hid, "pct": 100}],
        "expected_return_pct": 11.0,
    }).json()["id"]
    goal = next(g for g in client.get("/api/v1/portfolio").json()["goals"] if g["id"] == gid)
    assert goal["return_pct"] == 11.0 and goal["return_source"] == "assumed"

    # Clearing it falls back to the derived record (or the benchmark when none exists).
    body = {k: goal[k] for k in ("name", "schedule", "allocations", "inflation_pct",
                                 "monthly_sip", "holding_ids", "benchmark")}
    client.put(f"/api/v1/portfolio/goals/{gid}", json={**body, "expected_return_pct": None})
    goal = next(g for g in client.get("/api/v1/portfolio").json()["goals"] if g["id"] == gid)
    assert goal["return_source"] in ("holdings", "benchmark")
    client.delete(f"/api/v1/portfolio/goals/{gid}")
    client.delete(f"/api/v1/portfolio/holdings/{hid}")


def test_the_api_share_weights_a_linked_holdings_contribution(client: TestClient):
    """The goal never types the SIP again — the holding's ``monthly_contribution`` is the
    source of truth, and only the ALLOCATED share of it funds this goal."""
    hid = client.post("/api/v1/portfolio/holdings", json={
        "name": "PPF", "asset_class": "ppf", "invested": 1_000_000, "value": 1_300_000,
        "interest_rate_pct": 8.0, "monthly_contribution": 12_500,
    }).json()["id"]
    gid = client.post("/api/v1/portfolio/goals", json={
        "name": "College", "inflation_pct": 7,
        "schedule": [{"year": 2036, "amount": 700_000}],
        "allocations": [{"holding_id": hid, "pct": 40}],
    }).json()["id"]
    goal = next(g for g in client.get("/api/v1/portfolio").json()["goals"] if g["id"] == gid)
    assert goal["linked_monthly"] == pytest.approx(5_000)      # 40% of ₹12,500
    client.delete(f"/api/v1/portfolio/goals/{gid}")
    client.delete(f"/api/v1/portfolio/holdings/{hid}")


def test_the_present_value_answers_what_would_finish_this_today():
    """Discounted at the expected return — the one number that makes two goals in different
    decades comparable."""
    goal = {"schedule": [{"year": 2036, "amount": 1_000_000}], "inflation_pct": 0.0}
    p = pf.goal_projection(goal, current_value=0, return_pct=10.0, today=date(2026, 9, 1))
    assert p["pv_required"] == pytest.approx(1_000_000 / (1.10 ** 10), rel=1e-6)


def test_a_legacy_single_target_goal_still_reads_as_a_one_year_schedule():
    """An older goal must not silently become unfunded because it predates the schedule."""
    rows = pf.goal_schedule({"target_amount": 500_000, "target_year": 2030, "schedule": []})
    assert rows == [{"year": 2030, "amount": 500_000.0}]


def test_two_costs_in_the_same_year_are_added_not_replaced():
    """Marriage and travel can both land in 2033."""
    rows = pf.goal_schedule({"schedule": [
        {"year": 2033, "amount": 3_000_000}, {"year": 2033, "amount": 800_000},
    ]})
    assert rows == [{"year": 2033, "amount": 3_800_000.0}]


def test_a_goal_round_trips_through_the_api_with_its_projection(client: TestClient):
    gid = client.post("/api/v1/portfolio/goals", json={
        "name": "Travel", "inflation_pct": 6,
        "schedule": [{"year": y, "amount": 800_000} for y in range(2026, 2049)],
        "monthly_sip": 0, "holding_ids": [], "benchmark": "NIFTY 50 TRI",
    }).json()["id"]
    goal = next(g for g in client.get("/api/v1/portfolio").json()["goals"] if g["id"] == gid)
    assert len(goal["schedule"]) == 23
    assert goal["projection"]["total_today"] == pytest.approx(23 * 800_000)
    assert goal["projection"]["total_nominal"] > goal["projection"]["total_today"]
    # Nothing linked and no SIP: it fails in the very first year, and says so.
    assert goal["projection"]["first_shortfall_year"] == 2026
    client.delete(f"/api/v1/portfolio/goals/{gid}")


def test_missing_portfolio_columns_are_added_on_startup(tmp_path):
    """A box whose portfolio tables predate a new field boots healthy and then fails on the
    first query — the backend up, the engine running, and one screen broken in a way nothing
    surfaces until someone opens it. It happened twice on the VPS before this existed."""
    from sqlalchemy import create_engine, inspect, text

    from skas_algo.db.portfolio_schema import ensure_columns

    engine = create_engine(f"sqlite:///{tmp_path}/old.db")
    with engine.begin() as c:
        # The table as it looked before native currency, unit slices and yields existed.
        c.execute(text(
            "CREATE TABLE portfolio_holding (id INTEGER PRIMARY KEY, name VARCHAR(120),"
            " invested FLOAT, value FLOAT)"
        ))
        c.execute(text("INSERT INTO portfolio_holding VALUES (1, 'ITC', 100.0, 120.0)"))

    added = ensure_columns(engine)
    assert "portfolio_holding.dividend_yield_pct" in added
    assert "portfolio_holding.broker_units" in added
    cols = {c["name"] for c in inspect(engine).get_columns("portfolio_holding")}
    assert {"native_currency", "native_price", "native_invested",
            "units_locked", "broker_units", "dividend_yield_pct"} <= cols

    # The existing row survives, and a second pass is a no-op.
    with engine.begin() as c:
        assert c.execute(text("SELECT name FROM portfolio_holding")).scalar_one() == "ITC"
    assert ensure_columns(engine) == []


def test_a_table_that_does_not_exist_yet_is_left_to_create_all(tmp_path):
    from sqlalchemy import create_engine

    from skas_algo.db.portfolio_schema import ensure_columns

    assert ensure_columns(create_engine(f"sqlite:///{tmp_path}/empty.db")) == []


# ------------------------------------------------------------------ user-defined tags


def test_a_holding_carries_many_tags_and_a_tag_many_holdings(client: TestClient):
    """Many-to-many on purpose: a fund is legitimately 'child's education' AND 'long term'."""
    a = client.post("/api/v1/portfolio/holdings", json={
        "name": "Fund A", "asset_class": "mf", "invested": 100, "value": 120}).json()["id"]
    b = client.post("/api/v1/portfolio/holdings", json={
        "name": "Fund B", "asset_class": "mf", "invested": 100, "value": 120}).json()["id"]
    t1 = client.post("/api/v1/portfolio/tags", json={"name": "Child education"}).json()
    t2 = client.post("/api/v1/portfolio/tags", json={"name": "Long term"}).json()

    client.put(f"/api/v1/portfolio/holdings/{a}/tags", json={"tag_ids": [t1["id"], t2["id"]]})
    client.put(f"/api/v1/portfolio/holdings/{b}/tags", json={"tag_ids": [t1["id"]]})

    body = client.get("/api/v1/portfolio").json()
    rows = {h["id"]: h for h in body["holdings"]}
    assert {t["name"] for t in rows[a]["tags"]} == {"Child education", "Long term"}
    assert {t["name"] for t in rows[b]["tags"]} == {"Child education"}
    assert next(t for t in body["tags"] if t["name"] == "Child education")["count"] == 2

    for hid in (a, b):
        client.delete(f"/api/v1/portfolio/holdings/{hid}")
    for t in (t1, t2):
        client.delete(f"/api/v1/portfolio/tags/{t['id']}")


def test_creating_a_tag_that_exists_returns_it_rather_than_failing(client: TestClient):
    """The caller is typing a name in order to APPLY it — a duplicate-name error mid-flow
    helps nobody and loses what they typed."""
    first = client.post("/api/v1/portfolio/tags", json={"name": "Retirement"}).json()
    again = client.post("/api/v1/portfolio/tags", json={"name": "Retirement"}).json()
    assert first["created"] is True and again["created"] is False
    assert again["id"] == first["id"]
    client.delete(f"/api/v1/portfolio/tags/{first['id']}")


def test_setting_tags_replaces_the_whole_set(client: TestClient):
    """Sent whole, so removing is the same call as adding and no partial path leaves a stale
    link behind."""
    hid = client.post("/api/v1/portfolio/holdings", json={
        "name": "Tagged", "asset_class": "stk", "invested": 100, "value": 120}).json()["id"]
    t1 = client.post("/api/v1/portfolio/tags", json={"name": "One"}).json()
    t2 = client.post("/api/v1/portfolio/tags", json={"name": "Two"}).json()

    client.put(f"/api/v1/portfolio/holdings/{hid}/tags", json={"tag_ids": [t1["id"], t2["id"]]})
    out = client.put(f"/api/v1/portfolio/holdings/{hid}/tags", json={"tag_ids": [t2["id"]]})
    assert [t["name"] for t in out.json()["tags"]] == ["Two"]

    cleared = client.put(f"/api/v1/portfolio/holdings/{hid}/tags", json={"tag_ids": []})
    assert cleared.json()["tags"] == []

    client.delete(f"/api/v1/portfolio/holdings/{hid}")
    for t in (t1, t2):
        client.delete(f"/api/v1/portfolio/tags/{t['id']}")


def test_deleting_a_tag_keeps_the_holdings(client: TestClient):
    """A label is not the thing it labels."""
    hid = client.post("/api/v1/portfolio/holdings", json={
        "name": "Survivor", "asset_class": "stk", "invested": 100, "value": 120}).json()["id"]
    tid = client.post("/api/v1/portfolio/tags", json={"name": "Doomed"}).json()["id"]
    client.put(f"/api/v1/portfolio/holdings/{hid}/tags", json={"tag_ids": [tid]})

    client.delete(f"/api/v1/portfolio/tags/{tid}")
    rows = client.get("/api/v1/portfolio").json()["holdings"]
    survivor = next(h for h in rows if h["id"] == hid)
    assert survivor["tags"] == []
    client.delete(f"/api/v1/portfolio/holdings/{hid}")


# ------------------------------------------------------------------ cash and fixed deposits


def test_a_fixed_deposit_accrues_instead_of_being_re_typed():
    """The one manual holding whose value is not a matter of opinion — principal, rate and
    dates determine it exactly. Compounded QUARTERLY, as Indian banks do: annual compounding
    understates a five-year deposit by thousands."""
    quarterly = pf.accrued_fd_value(500_000, 7.4, date(2021, 9, 1), today=date(2026, 9, 1))
    annual = 500_000 * (1.074 ** 5)
    assert quarterly == pytest.approx(721_388, rel=1e-4)
    assert quarterly > annual


def test_accrual_stops_at_maturity():
    """A matured deposit sits in the account earning nothing until it is renewed. Compounding
    past that date invents money."""
    matured = pf.accrued_fd_value(
        500_000, 7.4, date(2024, 9, 1), maturity=date(2025, 9, 1), today=date(2026, 9, 1))
    one_year = pf.accrued_fd_value(500_000, 7.4, date(2024, 9, 1), today=date(2025, 9, 1))
    assert matured == pytest.approx(one_year)


def test_an_fd_with_no_rate_keeps_the_value_you_typed():
    """Nothing is invented from a blank field."""
    view = pf.holding_view(
        {"id": 1, "name": "FD", "asset_class": "fd", "invested": 100_000, "value": 111_111,
         "buy_month": "2024-01"}, [], today=date(2026, 9, 1))
    assert view["value"] == pytest.approx(111_111)


def test_cash_and_fd_are_separate_classes_and_both_tag_as_debt(client: TestClient):
    """A savings balance and a locked deposit behave differently enough to want apart, but
    both are debt for allocation and both are taxed as interest at slab."""
    assert pf.ASSET_CLASSES["cash"]["kind"] == "debt"
    assert pf.ASSET_CLASSES["fd"]["kind"] == "debt"
    assert pf.regime_for("cash", "debt", 60).label.startswith("Interest")
    assert pf.regime_for("fd", "debt", 60).label.startswith("Interest")

    hid = client.post("/api/v1/portfolio/holdings", json={
        "name": "SBI FD @ 7.4%", "asset_class": "fd", "invested": 500_000,
        "buy_month": "2024-09", "interest_rate_pct": 7.4, "maturity_date": "2029-09-01",
    }).json()["id"]
    row = next(h for h in client.get("/api/v1/portfolio").json()["holdings"] if h["id"] == hid)
    assert row["kind"] == "debt"
    assert row["value"] > 500_000          # accrued, not the zero that was posted
    assert row["gain"] > 0                 # the gain IS the interest earned
    client.delete(f"/api/v1/portfolio/holdings/{hid}")


def test_a_holding_on_a_removed_class_is_migrated_not_left_stranded(tmp_path):
    """A class the code no longer knows makes its holding INVISIBLE to allocation and
    rebalancing while still counting toward net worth — and any UI that indexes the class
    table by it crashes. "bank" split into cash + fd; a bare "Bank" is more likely a balance
    than a locked deposit, so it lands on cash and can be moved in one edit."""
    from sqlalchemy import create_engine, text

    from skas_algo.db.portfolio_schema import migrate_classes

    engine = create_engine(f"sqlite:///{tmp_path}/legacy.db")
    with engine.begin() as c:
        c.execute(text(
            "CREATE TABLE portfolio_holding (id INTEGER PRIMARY KEY, name VARCHAR(120),"
            " asset_class VARCHAR(16))"
        ))
        c.execute(text("INSERT INTO portfolio_holding VALUES (1, 'ICICI Bank', 'bank')"))
        c.execute(text("INSERT INTO portfolio_holding VALUES (2, 'ITC', 'stk')"))

    moved = migrate_classes(engine)
    assert moved == ["ICICI Bank (bank -> cash)"]
    with engine.begin() as c:
        classes = dict(c.execute(text("SELECT name, asset_class FROM portfolio_holding")).all())
    assert classes == {"ICICI Bank": "cash", "ITC": "stk"}       # only the orphan moved
    assert migrate_classes(engine) == []                         # and it is idempotent


def test_the_api_refuses_a_class_the_screen_cannot_render(client: TestClient):
    """Accepting "bank" after it was split is what stranded a holding in the first place."""
    resp = client.post("/api/v1/portfolio/holdings", json={
        "name": "Legacy", "asset_class": "bank", "invested": 100, "value": 100,
    })
    assert resp.status_code == 422


# ------------------------------------------------------------------ accrual & goal shares


def test_epf_compounds_annually_and_ppf_takes_a_monthly_contribution():
    """EPF and PPF are credited once a year, not quarterly like a bank FD — and a PPF is
    still being paid into, so the contribution has to be part of the growth."""
    epf = pf.accrued_value(14_545_698, 8.0, date(2025, 9, 2),
                           compounds_per_year=1, today=date(2026, 9, 2))
    assert epf == pytest.approx(14_545_698 * 1.08, rel=1e-3)

    ppf = pf.accrued_value(1_300_000, 8.0, date(2025, 9, 2), monthly_contribution=12_500,
                           compounds_per_year=1, today=date(2026, 9, 2))
    # The year's 1.5 L of contributions, credited through the year so earning about half a
    # year's return — not a full one, which would flatter every such account.
    assert ppf > 1_300_000 * 1.08 + 150_000
    assert ppf < 1_300_000 * 1.08 + 150_000 * 1.08


def test_an_accruing_holding_with_no_rate_keeps_its_typed_value():
    view = pf.holding_view(
        {"id": 1, "name": "EPF", "asset_class": "epf", "invested": 100, "value": 14_545_698,
         "buy_month": "2020-01"}, [], today=date(2026, 9, 2))
    assert view["value"] == pytest.approx(14_545_698)


def test_a_holding_can_fund_several_goals_by_percentage():
    """All-or-nothing forced a false choice: a 40 L fund backs some of the fees AND some of
    the wedding, and saying it backs only one of them is simply untrue."""
    school = {"allocations": [{"holding_id": 1, "pct": 60}]}
    wedding = {"allocations": [{"holding_id": 1, "pct": 40}]}
    assert pf.allocation_conflicts([school, wedding]) == {1: 100.0}


def test_the_older_whole_holding_form_reads_as_a_hundred_percent():
    assert pf.goal_allocations({"holding_ids": [7]}) == [{"holding_id": 7, "pct": 100.0}]


def test_the_api_refuses_to_fund_two_goals_with_the_same_rupee(client: TestClient):
    """Both goals would look funded and only one could be — invisible from either card."""
    hid = client.post("/api/v1/portfolio/holdings", json={
        "name": "Shared fund", "asset_class": "mf", "invested": 100, "value": 4_000_000,
    }).json()["id"]
    g1 = client.post("/api/v1/portfolio/goals", json={
        "name": "School", "schedule": [{"year": 2030, "amount": 100000}],
        "allocations": [{"holding_id": hid, "pct": 70}],
    }).json()["id"]

    clash = client.post("/api/v1/portfolio/goals", json={
        "name": "Wedding", "schedule": [{"year": 2035, "amount": 100000}],
        "allocations": [{"holding_id": hid, "pct": 50}],
    })
    assert clash.status_code == 422
    assert "already claimed" in clash.json()["detail"]

    # …but the remaining 30% is fine.
    g2 = client.post("/api/v1/portfolio/goals", json={
        "name": "Wedding", "schedule": [{"year": 2035, "amount": 100000}],
        "allocations": [{"holding_id": hid, "pct": 30}],
    })
    assert g2.status_code == 200

    body = client.get("/api/v1/portfolio").json()
    assert body["goal_allocated_pct"][str(hid)] == pytest.approx(100.0)
    school = next(g for g in body["goals"] if g["id"] == g1)
    assert school["current_value"] == pytest.approx(4_000_000 * 0.70)

    for gid in (g1, g2.json()["id"]):
        client.delete(f"/api/v1/portfolio/goals/{gid}")
    client.delete(f"/api/v1/portfolio/holdings/{hid}")


def test_editing_a_goal_does_not_collide_with_its_own_allocation(client: TestClient):
    """Re-saving a goal at the same percentage must not count its existing claim twice."""
    hid = client.post("/api/v1/portfolio/holdings", json={
        "name": "Solo fund", "asset_class": "mf", "invested": 100, "value": 500_000,
    }).json()["id"]
    gid = client.post("/api/v1/portfolio/goals", json={
        "name": "Only goal", "schedule": [{"year": 2030, "amount": 100000}],
        "allocations": [{"holding_id": hid, "pct": 100}],
    }).json()["id"]

    again = client.put(f"/api/v1/portfolio/goals/{gid}", json={
        "name": "Only goal", "schedule": [{"year": 2030, "amount": 100000}],
        "allocations": [{"holding_id": hid, "pct": 100}],
    })
    assert again.status_code == 200

    client.delete(f"/api/v1/portfolio/goals/{gid}")
    client.delete(f"/api/v1/portfolio/holdings/{hid}")


def test_a_deposit_grows_from_its_principal_and_a_pf_from_its_balance():
    """Getting this backwards silently zeroes a holding. An FD is entered as a principal with
    no value yet; a PF is entered as a balance with no contribution history."""
    fd = pf.holding_view(
        {"id": 1, "name": "FD", "asset_class": "fd", "invested": 500_000, "value": 0,
         "buy_month": "2024-09", "interest_rate_pct": 7.4}, [], today=date(2026, 9, 2))
    assert fd["value"] > 500_000

    epf = pf.holding_view(
        {"id": 2, "name": "EPF", "asset_class": "epf", "invested": 0, "value": 14_545_698,
         "buy_month": "2025-09", "interest_rate_pct": 8.0}, [], today=date(2026, 9, 2))
    assert epf["value"] == pytest.approx(14_545_698 * 1.08, rel=1e-3)


def test_only_things_that_can_distribute_appear_on_the_income_screen():
    """An index ETF on a growth plan and a bar of gold pay nothing. Listing them with an empty
    yield box invites filling one in, and every fabricated yield lands in a planning figure."""
    rows = [
        {"id": 1, "name": "ITC", "asset_class": "stk", "value": 100, "invested": 100,
         "dividend_yield_pct": None},
        {"id": 2, "name": "PGINVIT", "asset_class": "re", "value": 100, "invested": 100,
         "dividend_yield_pct": None},
        {"id": 3, "name": "GOLDBEES", "asset_class": "gold", "value": 100, "invested": 100,
         "dividend_yield_pct": None},
        {"id": 4, "name": "NIFTYBEES", "asset_class": "etf", "value": 100, "invested": 100,
         "dividend_yield_pct": None},
    ]
    view = pf.income_view(rows, [], today=date(2026, 9, 2))
    assert {line["name"] for line in view["lines"]} == {"ITC", "PGINVIT"}
    # …and only THEIR value counts as "yield not set", not the whole book.
    assert view["unpriced_value"] == pytest.approx(200)


def test_a_fund_that_has_actually_paid_stays_on_the_screen():
    """A payment is proof it distributes, whatever its class says."""
    rows = [{"id": 1, "name": "IDCW fund", "asset_class": "mf", "value": 100, "invested": 100,
             "dividend_yield_pct": None}]
    divs = [{"holding_id": 1, "on_date": "2026-06-01", "amount": 500}]
    view = pf.income_view(rows, divs, today=date(2026, 9, 2))
    assert [line["name"] for line in view["lines"]] == ["IDCW fund"]


def test_rent_is_rupees_a_month_not_a_yield():
    """A flat's rent has nothing to do with what the flat is worth this month, so deriving one
    from the other would move the income every time the valuation was touched."""
    rows = [{"id": 1, "name": "Pune flat", "asset_class": "property", "value": 9_000_000,
             "invested": 6_000_000, "dividend_yield_pct": None, "monthly_income": 45_000}]
    view = pf.income_view(rows, [], today=date(2026, 9, 2))
    assert view["expected_annual"] == pytest.approx(540_000)
    assert view["expected_monthly"] == pytest.approx(45_000)
    # Yield on COST still works — 9% on what was paid for it.
    assert view["lines"][0]["yield_on_cost_pct"] == pytest.approx(9.0)
    assert view["unpriced_value"] == pytest.approx(0)


def test_a_verified_zero_yield_is_not_reported_as_unknown():
    """Amazon and DMart have never paid a dividend. 0.0 is an ANSWER, and treating it as
    "not set" — which truthiness does — hides the very distinction this screen exists for."""
    rows = [
        {"id": 1, "name": "AMZN", "asset_class": "us", "value": 600_000, "invested": 500_000,
         "dividend_yield_pct": 0.0},
        {"id": 2, "name": "Unknown Co", "asset_class": "stk", "value": 400_000,
         "invested": 400_000, "dividend_yield_pct": None},
    ]
    view = pf.income_view(rows, [], today=date(2026, 9, 2))
    amzn = next(line for line in view["lines"] if line["name"] == "AMZN")
    assert amzn["expected_annual"] == 0.0            # answered, not absent
    # Only the genuinely unknown one counts toward the caveat.
    assert view["unpriced_value"] == pytest.approx(400_000)
