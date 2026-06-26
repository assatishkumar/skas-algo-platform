"""Obsidian vault export (trading-brain Phase 1): run-cards, idempotent writes, regime, no-op guard."""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import pandas as pd

import skas_algo.services.vault_export as ve

METRICS = {"Total Return %": 25.0, "CAGR %": 11.0, "Max Drawdown %": -8.0,
           "Win Rate %": 60.0, "Total Trades": 120}


def _run(**kw):
    base = dict(id=7, mode="BACKTEST", metrics=METRICS,
                params_snapshot={"start_date": "2024-01-01", "end_date": "2024-12-31", "profit_target": 0.1},
                trade_log=[{"ticker": "RELIANCE", "action": "SELL", "profit": 5000.0},
                           {"ticker": "INFY", "action": "SELL", "profit": -1200.0}],
                started_at=None, stopped_at=datetime(2024, 12, 31))
    base.update(kw)
    return SimpleNamespace(**base)


def _algo(**kw):
    base = dict(name="SST test", strategy_id="sst_lifo", capital=1_000_000, instrument_class="STOCK")
    base.update(kw)
    return SimpleNamespace(**base)


def test_build_run_card_frontmatter_and_body():
    rel, fm, body = ve.build_run_card(_run(), _algo(), sd=None)
    assert rel == "Runs/2024-01-01 sst_lifo #7.md"
    assert fm["return_pct"] == 25.0 and fm["win_rate"] == 60.0 and fm["trades"] == 120
    assert fm["outcome"] == "win" and fm["mode"] == "backtest" and fm["tags"] == ["equity", "sst_lifo"]
    assert "[[sst_lifo]]" in body and "+25.0%" in body
    assert "## Parameters" in body and "profit_target" in body  # params surfaced
    assert "## Trades" in body and "RELIANCE" in body            # trade summary surfaced


def test_strategy_card_has_description_and_params():
    rel, fm, body = ve.build_strategy_card("donchian_strangle_monthly")
    assert rel == "Strategies/donchian_strangle_monthly.md"
    assert "Donchian Strangle Monthly" in body          # description from the strategy docstring
    assert "## Default parameters" in body and "max_flips" in body  # constructor defaults


def test_outcome_open_for_running_live():
    _, fm, _ = ve.build_run_card(_run(mode="LIVE", stopped_at=None), _algo())
    assert fm["outcome"] == "open"


def test_regime_label():
    class FakeSD:
        def get_prices(self, symbol, start_date=None, end_date=None, asset_type="stock"):
            closes = [12.0, 12.5, 11.8] if symbol == ve.VIX_SYMBOL else [100.0, 101.0, 104.0]  # calm + up
            return pd.DataFrame({"date": [date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)], "close": closes})

    assert ve._regime(FakeSD(), "2024-01-01", "2024-01-03") == "calm-up"
    assert ve._regime(None, "2024-01-01", "2024-01-03") is None  # no cache → no label


def test_no_op_without_vault(monkeypatch):
    monkeypatch.setattr(ve, "vault_root", lambda: None)
    assert ve.export_run(_run(), _algo()) is None
    ve.export_run_safe(_run(), _algo())  # must not raise


def test_write_note_idempotent_preserves_user_notes(tmp_path, monkeypatch):
    monkeypatch.setattr(ve, "vault_root", lambda: tmp_path)
    p = ve._write_note("Runs/x.md", {"type": "run", "run_id": 1}, "machine v1")
    # the user appends their own content below the machine region
    p.write_text(p.read_text() + "\nMY OWN NOTE: watch the drawdowns.\n")
    ve._write_note("Runs/x.md", {"type": "run", "run_id": 1}, "machine v2")
    out = p.read_text()
    assert "machine v2" in out and "machine v1" not in out          # machine region refreshed
    assert "MY OWN NOTE: watch the drawdowns." in out                # user content preserved


def test_export_run_writes_card_and_strategy_note(tmp_path, monkeypatch):
    monkeypatch.setattr(ve, "vault_root", lambda: tmp_path)
    monkeypatch.setattr(ve, "_safe_cache", lambda: None)  # hermetic — no real data cache
    path = ve.export_run(_run(), _algo())
    assert path is not None and path.exists()
    assert (tmp_path / "Strategies" / "sst_lifo.md").exists()
    txt = path.read_text()
    assert "strategy: sst_lifo" in txt and "return_pct: 25.0" in txt and "outcome: win" in txt


def test_scaffold_writes_dashboards(tmp_path, monkeypatch):
    monkeypatch.setattr(ve, "vault_root", lambda: tmp_path)
    assert ve.scaffold() == 6
    assert (tmp_path / "Dashboards" / "Consistency.md").exists()
    assert (tmp_path / "Recipes.md").exists() and (tmp_path / "Trading Brain.md").exists()


def test_journal_appends_to_daily_note(tmp_path, monkeypatch):
    monkeypatch.setattr(ve, "vault_root", lambda: tmp_path)
    ve.journal("deploy", "Donchian #175 (paper)", strategy="donchian_strangle_monthly", run_id=175)
    ve.journal("intervene", "Flattened Donchian #175", run_id=175, detail="closed 4 legs")
    notes = list((tmp_path / "Journal").glob("*.md"))
    assert len(notes) == 1
    txt = notes[0].read_text()
    assert "`deploy` Donchian #175 (paper)" in txt and "[[donchian_strangle_monthly]]" in txt
    assert "`intervene` Flattened Donchian #175" in txt and "closed 4 legs" in txt  # both appended


def test_journal_no_op_without_vault(monkeypatch):
    monkeypatch.setattr(ve, "vault_root", lambda: None)
    assert ve.journal("deploy", "x") is None
    ve.journal_safe("deploy", "x")  # must not raise
