"""Market context the console's package cannot fetch for itself: India VIX.

`services/options_console` must not open the skas-data cache (a DuckDB, single-writer,
and tests never touch the real one) or the broker layer, so the two VIX readers live
here and are injected / called by the ROUTE and live-console layers.

* `vix_for_day(day)` — what a REPLAYED day could know: the prior session's close and the
  day's open, never its settled close (lookahead). From the cached "INDIA VIX" daily
  series (`data.options_provider.VIX_SYMBOL`); None when the cache lacks the day.
* `vix_live()` — the live print, `NSE:INDIA VIX` via any logged-in Zerodha account
  (read-only), remembered for a minute.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta

import pandas as pd

from skas_algo.data.options_provider import VIX_SYMBOL

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_DAY: dict[str, dict | None] = {}
_LIVE: tuple[datetime, float | None] | None = None
_LIVE_TTL = timedelta(seconds=60)


def vix_for_day(day: date) -> dict | None:
    key = day.isoformat()
    with _LOCK:
        if key in _DAY:
            return _DAY[key]
    out: dict | None = None
    try:
        from skas_algo.data.provider import get_data_cache

        sd = get_data_cache()
        # ~400 calendar days back: the prior close AND its rank over the last year of
        # closes (the console plan's "VIX rank" in place of a real IVR, labelled as such)
        df = sd.get_prices(symbol=VIX_SYMBOL, asset_type="stock",
                           start_date=day - timedelta(days=400), end_date=day)
        if df is not None and len(df):
            f = df.copy()
            f["date"] = pd.to_datetime(f["date"]).dt.date
            before = f[f["date"] < day].sort_values("date")
            today = f[f["date"] == day]
            prev_close = float(before["close"].iloc[-1]) if len(before) else None
            open_ = float(today["open"].iloc[0]) if len(today) and "open" in today else None
            rank = None
            if prev_close is not None and len(before) >= 60:
                window = before["close"].astype(float).tail(252)
                rank = round(100.0 * float((window < prev_close).mean()), 1)
            if prev_close is not None or open_ is not None:
                out = {"prev_close": prev_close, "open": open_,
                       "prev_date": before["date"].iloc[-1].isoformat() if len(before) else None,
                       # percentile of the prior close within the last 252 closes (≥60 needed)
                       "rank_1y": rank, "rank_basis": "vix_rank_1y" if rank is not None else None}
    except Exception:
        logger.debug("console: VIX for %s unavailable", key, exc_info=True)
    with _LOCK:
        _DAY[key] = out
    return out


def vix_live() -> float | None:
    global _LIVE
    now = datetime.now()
    with _LOCK:
        if _LIVE and now - _LIVE[0] < _LIVE_TTL:
            return _LIVE[1]
    val: float | None = None
    try:
        from skas_algo.services.console_margin import _account

        acct = _account()
        if acct is not None:
            v = acct[1].underlying_ltp(VIX_SYMBOL)
            val = float(v) if v else None
    except Exception:
        logger.debug("console: live VIX unavailable", exc_info=True)
    with _LOCK:
        _LIVE = (now, val)
    return val


def clear_cache() -> None:
    global _LIVE
    with _LOCK:
        _DAY.clear()
        _LIVE = None
