"""Price-loader provider.

Wraps skas-data's cache into the ``PriceLoader`` the engine expects. Exposed as a
FastAPI dependency so tests can inject synthetic data without the cache.
"""

from __future__ import annotations

import os
from datetime import date
from functools import lru_cache

from skas_algo.engine.market import PriceLoader

# Pin the options store to the canonical home path so it never drifts with CWD
# (skas-data resolves its DB path from a config.yaml relative to the working dir).
OPTIONS_DB_PATH = os.path.expanduser("~/.skas_data/data/nse_options.db")


@lru_cache
def _skas_data():
    import skas_data  # imported lazily so the app starts without the cache present

    return skas_data.SkasData(cache_only=True, options_db_path=OPTIONS_DB_PATH)


def get_price_loader() -> PriceLoader:
    """Return a loader backed by the skas-data cache (FastAPI dependency)."""
    sd = _skas_data()

    def loader(symbol: str, start: date, end: date):
        return sd.get_prices(symbol=symbol, start_date=start, end_date=end)

    return loader


def get_available_symbols() -> set[str]:
    """Set of stock symbols present in the skas-data cache (FastAPI dependency)."""
    return set(_skas_data().list_cached_symbols(asset_type="stock"))


def get_data_cache():
    """The read-only (cache_only) skas-data instance (FastAPI dependency).

    Used by the Data screen to introspect cache coverage. Overridable in tests.
    """
    return _skas_data()
