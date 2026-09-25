"""BIDS over the portfolio (services/bids.py): eligibility and mode, honest peak seeding,
suggestions once per level per ladder, accept → a ledger BUY, skip consumes the level, a new
high expires what was pending, and the API round trip."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from skas_algo.db.base import session_scope
from skas_algo.db.models import (
    PortfolioBidsRule,
    PortfolioBidsSuggestion,
    PortfolioHolding,
    PortfolioSetting,
    PortfolioTransaction,
)
from skas_algo.services import bids

D0 = date(2026, 9, 1)


@pytest.fixture(autouse=True)
def _clean():
    yield
    with session_scope() as db:
        db.execute(delete(PortfolioBidsSuggestion))
        db.execute(delete(PortfolioBidsRule))
        db.execute(delete(PortfolioTransaction))
        db.execute(delete(PortfolioHolding))
        db.execute(delete(PortfolioSetting).where(PortfolioSetting.key == bids.SETTINGS_KEY))


def _holding(db, name, cls, price, *, source=None, ref=None, account=None, units=10.0,
             invested=1000.0, asof="2026-09-01"):
    h = PortfolioHolding(name=name, asset_class=cls, last_price=price, price_asof=asof,
                         sync_source=source, sync_ref=ref, broker_account_id=account,
                         units=units, invested=invested, value=units * price, buy_month="2025-01")
    db.add(h)
    db.flush()
    return h


def _reprice(db, hid, price, asof):
    h = db.get(PortfolioHolding, hid)
    h.last_price, h.price_asof = price, asof
    db.commit()




def test_only_market_linked_classes_are_eligible_and_mode_is_derived():
    with session_scope() as db:
        stk = _holding(db, "INFY", "stk", 1500.0, source="broker", ref="INFY", account=3)
        mf = _holding(db, "Small Cap Fund", "mf", 100.0, source="amfi", ref="INF000")
        ppf = _holding(db, "PPF", "ppf", 1.0)
        db.commit()
        assert bids.eligible(stk) and bids.eligible(mf) and not bids.eligible(ppf)
        assert bids.mode_of(stk, None) == "suggest"                      # no run → suggest
        assert bids.mode_of(stk, None, {3: {"INFY"}}) == "auto"          # a bids run on acct 3
        assert bids.mode_of(stk, None, {4: {"INFY"}}) == "suggest"       # another account
        assert bids.mode_of(mf, None, {3: {"INFY"}}) == "suggest"        # MFs never auto
        off = PortfolioBidsRule(holding_id=stk.id, enabled=False)
        assert bids.mode_of(stk, off, {3: {"INFY"}}) == "excluded"


def test_a_holding_joins_at_todays_price_whatever_its_old_high():
    """Owner decision 2026-09-25: a stock already 11% under its 52-week high joins at today's
    price — no lump of levels on day one, and the next day's small move fires nothing."""
    with session_scope() as db:
        stk = _holding(db, "TCS", "stk", 3000.0, source="broker", ref="TCS", account=3)
        mf = _holding(db, "Flexi", "mf", 50.0, source="amfi", ref="INF1")
        db.commit()
        assert bids.evaluate_portfolio(db, D0, notify=False)["new"] == []
        rules = {r.holding_id: r for r in db.execute(select(PortfolioBidsRule)).scalars()}
        assert rules[stk.id].peak == 3000.0 and rules[stk.id].peak_source == "joined"
        assert rules[mf.id].peak == 50.0 and rules[mf.id].peak_source == "joined"
        _reprice(db, stk.id, 2900.0, "2026-09-02")                       # -3.3%: nothing
        assert bids.evaluate_portfolio(db, date(2026, 9, 2), notify=False)["new"] == []


def test_a_level_is_suggested_once_and_a_gap_suggests_every_level_crossed():
    with session_scope() as db:
        h = _holding(db, "Fund", "mf", 100.0, source="amfi", ref="INF2")
        db.commit()
        bids.save_defaults(db, {"dip_pct": 5.0, "amount": 1000.0, "max_levels": 4})
        bids.evaluate_portfolio(db, D0, notify=False)   # seed 100
        _reprice(db, h.id, 94.0, "2026-09-02")
        out = bids.evaluate_portfolio(db, date(2026, 9, 2), notify=False)
        assert [(n["level"], n["amount"]) for n in out["new"]] == [(1, 1000.0)]
        # the same price date again (the 16:00 pass after the 09:30 one) adds nothing
        assert bids.evaluate_portfolio(db, date(2026, 9, 2), notify=False)["new"] == []
        _reprice(db, h.id, 84.0, "2026-09-03")                          # -16%: L2 and L3
        out = bids.evaluate_portfolio(db, date(2026, 9, 3), notify=False)
        assert [(n["level"], n["amount"]) for n in out["new"]] == [(2, 2000.0), (3, 3000.0)]
        v = bids.view(db)
        row = next(r for r in v["rows"] if r["holding_id"] == h.id)
        assert row["levels_fired"] == 3 and row["next"]["level"] == 4
        assert row["next"]["trigger_price"] == pytest.approx(80.0)
        assert len(v["pending"]) == 3


def test_accept_writes_a_ledger_buy_with_the_typed_position_carried_in_and_skip_consumes():
    with session_scope() as db:
        h = _holding(db, "Fund", "mf", 100.0, source="amfi", ref="INF3", units=50.0,
                     invested=4000.0)
        db.commit()
        bids.save_defaults(db, {"dip_pct": 5.0, "amount": 1000.0, "max_levels": 3})
        bids.evaluate_portfolio(db, D0, notify=False)
        _reprice(db, h.id, 89.0, "2026-09-02")                          # L1 + L2
        bids.evaluate_portfolio(db, date(2026, 9, 2), notify=False)
        pend = bids.view(db)["pending"]
        l1 = next(p for p in pend if p["level"] == 1)
        l2 = next(p for p in pend if p["level"] == 2)
        r = bids.accept(db, l1["id"], units=11.2, price=89.0, on_date=date(2026, 9, 2))
        rows = db.execute(select(PortfolioTransaction).where(
            PortfolioTransaction.holding_id == h.id).order_by(PortfolioTransaction.on_date)
        ).scalars().all()
        # the typed position carried in as the opening row, then the BIDS buy
        assert [(t.kind, t.units) for t in rows] == [("buy", 50.0), ("buy", 11.2)]
        assert rows[1].id == r["txn_id"] and rows[1].note.startswith("BIDS L1")
        assert bids.skip(db, l2["id"])["status"] == "skipped"
        with pytest.raises(ValueError):
            bids.skip(db, l2["id"])                                     # already resolved
        # the skipped level stays consumed: the next close at the same depth fires nothing
        _reprice(db, h.id, 88.0, "2026-09-03")
        assert bids.evaluate_portfolio(db, date(2026, 9, 3), notify=False)["new"] == []


def test_a_new_high_resets_the_ladder_and_expires_what_was_pending():
    with session_scope() as db:
        h = _holding(db, "US", "us", 200.0, source="global", ref="MSFT")
        db.commit()
        bids.evaluate_portfolio(db, D0, notify=False)
        _reprice(db, h.id, 188.0, "2026-09-02")
        bids.evaluate_portfolio(db, date(2026, 9, 2), notify=False)
        assert len(bids.view(db)["pending"]) == 1
        _reprice(db, h.id, 205.0, "2026-09-03")
        out = bids.evaluate_portfolio(db, date(2026, 9, 3), notify=False)
        assert out["resets"] == 1
        v = bids.view(db)
        assert v["pending"] == [] and v["recent"][0]["status"] == "expired"
        row = v["rows"][0]
        assert row["peak"] == 205.0 and row["levels_fired"] == 0 and row["peak_source"] == "high"


def test_excluded_and_manual_peak_via_the_api(client: TestClient):
    with session_scope() as db:
        h = _holding(db, "Fund", "mf", 100.0, source="amfi", ref="INF4")
        db.commit()
        hid = h.id
    r = client.put(f"/api/v1/portfolio/bids/rules/{hid}", json={"peak": 125.0, "dip_pct": 10})
    assert r.status_code == 200
    row = next(x for x in r.json()["rows"] if x["holding_id"] == hid)
    assert row["peak"] == 125.0 and row["peak_source"] == "manual"
    assert row["drawdown_pct"] == pytest.approx(-20.0)
    assert row["next"]["trigger_price"] == pytest.approx(112.5)
    ev = client.post("/api/v1/portfolio/bids/evaluate").json()   # a typed high is judged now
    assert [n["level"] for n in ev["new"]] == [1, 2]     # -20% at 10% steps: L1 112.5, L2 100
    r = client.put(f"/api/v1/portfolio/bids/rules/{hid}", json={"enabled": False})
    assert next(x for x in r.json()["rows"] if x["holding_id"] == hid)["mode"] == "excluded"
    sid = client.get("/api/v1/portfolio/bids").json()["pending"][0]["id"]
    body = {"units": 5, "price": 100.0, "on_date": "2026-09-02"}
    assert client.post(f"/api/v1/portfolio/bids/suggestions/{sid}/accept",
                       json=body).status_code == 200
    assert client.post(f"/api/v1/portfolio/bids/suggestions/{sid}/skip").status_code == 409
    assert client.put("/api/v1/portfolio/bids/defaults",
                      json={"amount": 2500}).json()["amount"] == 2500
