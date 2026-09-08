"""Historical-cache refresh on the platform's shared Kite session.

Uses ``broker.make_data_session`` so the cache is updated with the *same* login used
for trading — no separate skas-data authentication.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from skas_algo.db.models import BrokerAccount
from skas_algo.services import broker as broker_svc


IST = ZoneInfo("Asia/Kolkata")


import logging

logger = logging.getLogger(__name__)

DEEP_HISTORY_DAYS = 1500   # ~4y: what _seed_supertrend asks the cache for
MIN_HISTORY_DAYS = 900     # under ~2.5y of bars a SuperTrend seed is not the backtest's


def refresh_cache(
    account: BrokerAccount,
    symbols: list[str],
    *,
    start: date | None = None,
    end: date | None = None,
    asset_type: str = "stock",
    deep_days: int = DEEP_HISTORY_DAYS,
    min_history_days: int = MIN_HISTORY_DAYS,
) -> dict[str, dict]:
    """Fetch recent prices for ``symbols`` on the shared session, filling the cache.

    A symbol the cache holds THINLY — fewer than ``min_history_days`` of history, or nothing —
    is backfilled ``deep_days`` back instead of the default 30, because the live SuperTrend is
    computed from this cache and needs years of bars to converge (2026-09-08: the VPS cache
    held 4 MB and 17 symbols; a supertrend deploy there would have read no direction at all,
    then a direction seeded from 30 bars that disagreed with the backtest's). One Kite
    historical call per thin symbol; an explicit ``start`` disables the check.

    Returns ``{symbol: {"rows": n, "last_date": iso, "backfilled": bool} | {"error": msg}}``.
    """
    # NEVER fetch TODAY. This ran with end=today, and the daily refresh fires as soon as a
    # Zerodha session appears — so on any day the owner logged in DURING market hours it
    # cached a half-formed bar for today, and `use_cache=True` means that row is never
    # re-fetched or corrected. The VPS cache carried wrong closes for 26/27/28 Aug 2026 that
    # way (TMPV 314.10 against a real 319.40), which is what value_investing ranks its
    # "biggest faller" on. Settle on the last COMPLETED session instead; today's live price
    # comes from the broker, never from here.
    from skas_algo.live.holidays import previous_trading_day

    today = datetime.now(IST).date()
    end = end or previous_trading_day(today + timedelta(days=1))
    if end >= today:
        end = previous_trading_day(today)
    default_start = start or (end - timedelta(days=30))
    sd = broker_svc.make_data_session(account)
    thin: set[str] = set()
    if not start:
        try:
            thin = thin_symbols(symbols, end, min_history_days=min_history_days)
        except Exception:  # the depth check is an optimisation — never fail the refresh on it
            logger.warning("cache depth check failed; refreshing the default window", exc_info=True)

    out: dict[str, dict] = {}
    for sym in symbols:
        deep = sym in thin
        sym_start = (end - timedelta(days=deep_days)) if deep else default_start
        try:
            df = sd.get_prices(sym, start_date=sym_start, end_date=end, asset_type=asset_type, use_cache=True)
            if df is None or len(df) == 0:
                out[sym] = {"rows": 0, "last_date": None, "backfilled": deep}
            else:
                last = df.iloc[-1]["date"]
                last_iso = last.date().isoformat() if hasattr(last, "date") else str(last)
                out[sym] = {"rows": int(len(df)), "last_date": last_iso, "backfilled": deep}
        except Exception as exc:  # one bad symbol shouldn't abort the batch
            out[sym] = {"error": str(exc)}
    return out


def thin_symbols(symbols: list[str], end: date, *, min_history_days: int = MIN_HISTORY_DAYS,
                 loader=None) -> set[str]:
    """The symbols whose CACHED history does not reach ``min_history_days`` before ``end``
    (read-only, the cache loader — never the broker)."""
    if loader is None:
        from skas_algo.data.provider import get_price_loader

        loader = get_price_loader()
    since = end - timedelta(days=min_history_days)
    out: set[str] = set()
    for sym in symbols:
        try:
            df = loader(sym, since, end)
        except Exception:
            out.add(sym)
            continue
        if df is None or len(df) == 0:
            out.add(sym)
            continue
        first = df.iloc[0]["date"]
        first_d = first.date() if hasattr(first, "date") else first
        # a cache that only starts recently is thin too — the earliest bar must sit near the
        # start of the window, not a few weeks back
        if first_d > since + timedelta(days=45):
            out.add(sym)
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
