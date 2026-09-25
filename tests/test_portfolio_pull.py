"""portfolio-pull (services/portfolio_pull.py): only portfolio tables are replaced, broker
account ids are remapped by broker + label (never trusted as ids), a column or table the
peer lacks is tolerated, and the refusal while this box's portfolio maintenance is on."""

from __future__ import annotations

import sqlite3

import pytest

from skas_algo.services import portfolio_pull as pp


def _db(tmp_path):
    p = tmp_path / "local.db"
    c = sqlite3.connect(p)
    c.executescript("""
        create table broker_account (id integer primary key, broker text, label text);
        insert into broker_account values (1,'zerodha','Ops Kite'),(2,'zerodha','Paper Kite'),
                                          (3,'dhan','Ops Dhan');
        create table portfolio_holding (id integer primary key, name text,
                                        broker_account_id integer, units real, extra text);
        create table portfolio_transaction (id integer primary key, holding_id integer,
                                            units real);
        create table portfolio_bids_rule (id integer primary key, holding_id integer);
        create table algo_run (id integer primary key, name text);
        insert into portfolio_holding values (9,'OLD',1,1,'x');
        insert into portfolio_bids_rule values (1, 9);
        insert into algo_run values (7,'keep me');
    """)
    c.commit()
    c.close()
    return p


DUMP = {
    "accounts": [[1, "zerodha", "OpsKite"], [2, "dhan", "ops dhan"], [5, "zerodha", "Gone"]],
    "tables": {
        "portfolio_holding": {"columns": ["id", "name", "broker_account_id", "units", "peer"],
                              "rows": [[1, "INFY", 1, 10, "p"], [2, "ITC", 2, 5, "p"],
                                       [3, "TCS", 5, 1, "p"], [4, "PPF", None, 1, "p"]]},
        "portfolio_transaction": {"columns": ["id", "holding_id", "units"],
                                  "rows": [[1, 1, 10]]},
        "portfolio_only_on_peer": {"columns": ["id"], "rows": [[1]]},
    },
}


def test_only_portfolio_tables_are_replaced_and_accounts_are_remapped(tmp_path):
    p = _db(tmp_path)
    out = pp.apply(DUMP, p)
    c = sqlite3.connect(p)
    rows = c.execute("select id, name, broker_account_id, units, extra from portfolio_holding "
                     "order by id").fetchall()
    # peer 1 OpsKite → local 1 "Ops Kite"; peer 2 (dhan) → local 3, NOT local 2 (a Kite
    # account with the same id); peer 5 has no match → cleared, never guessed
    assert rows == [(1, "INFY", 1, 10, None), (2, "ITC", 3, 5, None),
                    (3, "TCS", None, 1, None), (4, "PPF", None, 1, None)]
    assert out["cleared_accounts"] == 1
    assert c.execute("select count(*) from portfolio_transaction").fetchone() == (1,)
    # a table the peer lacks is emptied (its rows belonged to the old local book)
    assert c.execute("select count(*) from portfolio_bids_rule").fetchone() == (0,)
    assert out["skipped"] == ["portfolio_only_on_peer"]
    # nothing outside portfolio* is touched
    assert c.execute("select * from algo_run").fetchall() == [(7, "keep me")]
    assert c.execute("select count(*) from broker_account").fetchone() == (3,)


def test_dry_run_writes_nothing(tmp_path):
    p = _db(tmp_path)
    out = pp.apply(DUMP, p, dry_run=True)
    assert out["tables"]["portfolio_holding"] == 4
    c = sqlite3.connect(p)
    assert c.execute("select name from portfolio_holding").fetchall() == [("OLD",)]


def test_a_failed_insert_rolls_back_the_whole_pull(tmp_path):
    p = _db(tmp_path)
    bad = {"accounts": [], "tables": {"portfolio_holding": {
        "columns": ["id", "name"], "rows": [[1, "A"], [1, "DUP"]]}}}
    with pytest.raises(sqlite3.IntegrityError):
        pp.apply(bad, p)
    c = sqlite3.connect(p)
    assert c.execute("select name from portfolio_holding").fetchall() == [("OLD",)]
    assert c.execute("select count(*) from portfolio_bids_rule").fetchone() == (1,)


def test_refuses_to_load_while_this_box_maintains_the_portfolio(monkeypatch):
    from skas_algo.config import get_settings

    monkeypatch.setattr(get_settings(), "portfolio_maintenance", True)
    monkeypatch.setattr(pp, "fetch", lambda host: pytest.fail("must not reach the peer"))
    with pytest.raises(RuntimeError, match="SKAS_PORTFOLIO_MAINTENANCE=0"):
        pp.pull("peer")
