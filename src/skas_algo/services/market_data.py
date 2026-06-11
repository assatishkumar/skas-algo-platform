"""Historical-cache refresh on the platform's shared Kite session.

Uses ``broker.make_data_session`` so the cache is updated with the *same* login used
for trading — no separate skas-data authentication.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from skas_algo.db.models import BrokerAccount
from skas_algo.services import broker as broker_svc


def refresh_cache(
    account: BrokerAccount,
    symbols: list[str],
    *,
    start: date | None = None,
    end: date | None = None,
    asset_type: str = "stock",
) -> dict[str, dict]:
    """Fetch recent prices for ``symbols`` on the shared session, filling the cache.

    Returns ``{symbol: {"rows": n, "last_date": iso} | {"error": msg}}``.
    """
    end = end or datetime.now(UTC).date()
    start = start or (end - timedelta(days=30))
    sd = broker_svc.make_data_session(account)

    out: dict[str, dict] = {}
    for sym in symbols:
        try:
            df = sd.get_prices(sym, start_date=start, end_date=end, asset_type=asset_type, use_cache=True)
            if df is None or len(df) == 0:
                out[sym] = {"rows": 0, "last_date": None}
            else:
                last = df.iloc[-1]["date"]
                last_iso = last.date().isoformat() if hasattr(last, "date") else str(last)
                out[sym] = {"rows": int(len(df)), "last_date": last_iso}
        except Exception as exc:  # one bad symbol shouldn't abort the batch
            out[sym] = {"error": str(exc)}
    return out


def refresh_gold(
    account: BrokerAccount,
    *,
    start: date | None = None,
    end: date | None = None,
    store_as: str = "GOLD",
) -> dict:
    """Fetch the MCX GOLD futures series on the shared session and cache it as ``store_as``
    (so the synthetic GOLD option chain has its underlying). Returns rows + last_date."""
    end = end or datetime.now(UTC).date()
    start = start or date(2020, 1, 1)
    sd = broker_svc.make_data_session(account)
    df = sd.fetch_gold_futures(start, end, store_as=store_as)
    if df is None or len(df) == 0:
        return {"rows": 0, "last_date": None}
    last = df.iloc[-1]["date"]
    last_iso = last.date().isoformat() if hasattr(last, "date") else str(last)
    return {"rows": int(len(df)), "last_date": last_iso}
