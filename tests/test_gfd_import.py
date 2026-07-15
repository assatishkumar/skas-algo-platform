"""gfd_import: GFD ticker → internal symbol parse (DDMMMYY), minute-END → minute-START,
futures rows skipped, per-day parquet written, idempotent re-run, and existing captured
rows winning the merge — fixture CSV, store redirected to tmp."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from skas_algo.data import option_intraday_store as store
from skas_algo.data.gfd_import import _ticker_to_symbol, import_gfd, import_gfd_file

HEADER = "Ticker,Date,Time,Open,High,Low,Close,Volume,Open Interest\n"
ROWS = (
    "NIFTY03JUL2522800CE.NFO,01/07/2025,10:58:59,100,102,99,101,35,6155\n"
    "NIFTY03JUL2522800CE.NFO,01/07/2025,10:59:59,101,103,100,102,10,6160\n"
    "BANKNIFTY24DEC2551000PE.NFO,01/07/2025,12:10:59,254,254,254,254,35,6125\n"
    "NIFTY-I.NFO,01/07/2025,10:58:59,25500,25510,25490,25505,1000,0\n"  # futures → skipped
)


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "OPTION_INTRADAY_DIR", tmp_path / "1min")
    # Never mirror into the operator's REAL backup dir from a test (.env leaks in).
    from skas_algo.config import get_settings
    monkeypatch.setattr(get_settings(), "option_bars_backup_dir", None)


@pytest.fixture()
def gfd_csv(tmp_path):
    p = tmp_path / "GFDLNFO_NIFTY_BANKNIFTY_01072025.csv"
    p.write_text(HEADER + ROWS)
    return p


def test_ticker_parse():
    assert _ticker_to_symbol("NIFTY03JUL2522800CE.NFO") == "NIFTY|2025-07-03|22800|CE"
    assert _ticker_to_symbol("BANKNIFTY24DEC2551000PE.NFO") == "BANKNIFTY|2025-12-24|51000|PE"
    assert _ticker_to_symbol("NIFTY-I.NFO") is None            # continuous futures
    assert _ticker_to_symbol("BANKNIFTY-III.NFO") is None
    assert _ticker_to_symbol("garbage") is None


def test_import_writes_day_and_converts_times(gfd_csv):
    out = import_gfd_file(gfd_csv)
    assert out["rows"] == 3 and out["skipped_tickers"] == 1
    assert out["days"] == {"2025-07-01": 3}
    df = store.load_day("2025-07-01")
    assert len(df) == 3
    ce = df[df["symbol"] == "NIFTY|2025-07-03|22800|CE"].sort_values("start")
    # GFD stamps minute END (…:59) → stored as minute START.
    assert list(pd.to_datetime(ce["start"])) == [
        pd.Timestamp(2025, 7, 1, 10, 58), pd.Timestamp(2025, 7, 1, 10, 59)]
    assert list(ce["close"]) == [101.0, 102.0]
    assert list(ce["oi"]) == [6155.0, 6160.0]


def test_import_is_idempotent(gfd_csv):
    import_gfd_file(gfd_csv)
    again = import_gfd_file(gfd_csv)
    assert again["days"] == {"2025-07-01": 3}      # merged row count unchanged
    assert len(store.load_day("2025-07-01")) == 3


def test_existing_captured_rows_win_merge(gfd_csv):
    # A self-captured row for the same (symbol, minute) with a DIFFERENT close pre-exists.
    store.write_day("2025-07-01", pd.DataFrame([{
        "symbol": "NIFTY|2025-07-03|22800|CE", "start": datetime(2025, 7, 1, 10, 58),
        "open": 999.0, "high": 999.0, "low": 999.0, "close": 999.0,
        "volume": 1.0, "oi": 1.0}], columns=store.COLUMNS))
    import_gfd_file(gfd_csv)
    df = store.load_day("2025-07-01")
    row = df[(df["symbol"] == "NIFTY|2025-07-03|22800|CE")
             & (pd.to_datetime(df["start"]) == pd.Timestamp(2025, 7, 1, 10, 58))]
    assert len(row) == 1 and row.iloc[0]["close"] == 999.0   # capture beats import
    assert len(df) == 3                                       # 1 kept + 2 new


def test_import_gfd_expands_dirs(gfd_csv):
    out = import_gfd([str(gfd_csv.parent)])
    assert out["files"] == 1 and out["days"] == {"2025-07-01": 3}
