"""Options data wiring: turn the skas-data options cache into the engine's option
loader, chain view, settlement spot provider, and margin model.

Index spot for settlement / ATM selection comes from the cached index EOD series
(``NIFTY 50`` for NIFTY, ``NIFTY BANK`` for BANKNIFTY), already present in the equity
cache and used by the benchmark feature.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from skas_algo.engine.options.chain import OptionChainView
from skas_algo.engine.options.instrument import parse
from skas_algo.engine.options.margin import MarginModel, MarginParams
from skas_algo.engine.options.market import OptionMarketView
from skas_algo.engine.options.settlement import ExpirySettler

# Underlying -> cached price series symbol used for spot/settlement. For NSE indices this
# is the index EOD series; for GOLD it's the MCX futures series cached via
# SkasData.fetch_gold_futures(store_as="GOLD").
INDEX_SYMBOL = {
    "NIFTY": "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
    "FINNIFTY": "NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NIFTY MIDCAP SELECT",
    "GOLD": "GOLD",
}


def get_options_loader(sd, lot_overrides: dict | None = None):
    """Loader mapping a contract symbol -> its daily series (date + close)."""
    def loader(symbol: str, start: date, end: date):
        inst = parse(symbol, lot_overrides=lot_overrides)
        if inst is None:
            return None
        df = sd.get_option_series(inst.underlying, inst.expiry, inst.strike, inst.right,
                                  start_date=start, end_date=end)
        if df is None or len(df) == 0:
            return None
        out = df.rename(columns={"trade_date": "date"})
        return out[["date", "close"]]
    return loader


def make_spot_provider(sd):
    """Spot/settlement provider backed by the cached index EOD series (forward-filled)."""
    cache: dict[str, pd.Series] = {}

    def _series(underlying: str) -> pd.Series | None:
        u = underlying.upper()
        if u not in cache:
            # Index → its EOD index series; a stock F&O underlying → its own equity series.
            idx = INDEX_SYMBOL.get(u) or u
            df = sd.get_prices(symbol=idx, asset_type="stock")
            if df is None or len(df) == 0:
                cache[u] = None
            else:
                s = df.copy()
                s["date"] = pd.to_datetime(s["date"])
                cache[u] = s.set_index("date")["close"].sort_index()
        return cache[u]

    def spot(underlying: str, on_date: date):
        s = _series(underlying)
        if s is None or s.empty:
            return None
        ts = pd.Timestamp(on_date)
        upto = s.loc[:ts]               # forward-fill: last close on/before the date
        return float(upto.iloc[-1]) if len(upto) else None

    return spot


def make_chain_provider(sd):
    def chain_provider(underlying: str, on_date: date):
        return sd.get_option_chain(underlying, on_date)
    return chain_provider


VIX_SYMBOL = "INDIA VIX"


def _ffill_lookup(sd, symbol: str):
    """A forward-filled date→close lookup over a cached series (None if absent)."""
    df = sd.get_prices(symbol=symbol, asset_type="stock") if symbol else None
    if df is None or len(df) == 0:
        return lambda d: None
    s = df.copy()
    s["date"] = pd.to_datetime(s["date"])
    ser = s.set_index("date")["close"].sort_index()

    def fn(d):
        upto = ser.loc[: pd.Timestamp(d)]
        return float(upto.iloc[-1]) if len(upto) else None

    return fn


def enrich_with_market(sd, options_report: dict, underlying: str) -> None:
    """Tag each position/cycle with the underlying spot + India VIX at entry & exit.

    Spot comes from the underlying's cached series (NIFTY 50 / NIFTY BANK / GOLD); VIX
    from the cached ``INDIA VIX`` series (NSE index underlyings only). Mutates in place.
    """
    idx = INDEX_SYMBOL.get(underlying.upper())
    spot_fn = _ffill_lookup(sd, idx)
    vix_fn = _ffill_lookup(sd, VIX_SYMBOL) if underlying.upper() in ("NIFTY", "BANKNIFTY") \
        else (lambda d: None)

    def _as_date(s):
        return date.fromisoformat(s[:10]) if isinstance(s, str) else s

    def _enrich(item: dict) -> None:
        ed, xd = item.get("entry_date"), item.get("exit_date")
        if ed:
            item["underlying_entry"] = spot_fn(_as_date(ed))
            item["vix_entry"] = vix_fn(_as_date(ed))
        if xd:
            item["underlying_exit"] = spot_fn(_as_date(xd))
            item["vix_exit"] = vix_fn(_as_date(xd))
        ue, ux = item.get("underlying_entry"), item.get("underlying_exit")
        item["underlying_pct"] = ((ux - ue) / ue * 100.0) if (ue and ux) else None

    for c in options_report.get("cycles", []):
        _enrich(c)
    for p in options_report.get("positions", []):
        _enrich(p)


def attach_underlying_timeline(sd, options_report: dict, underlying: str,
                               start: date, end: date) -> None:
    """Attach the underlying's daily close series over the run window to the options
    report — feeds the covered-call campaign timeline charts (price line + tranche/call
    markers share the index/strike axis). No-op unless the report has campaigns."""
    if not options_report or not options_report.get("campaigns"):
        return
    idx = INDEX_SYMBOL.get(underlying.upper())
    df = sd.get_prices(symbol=idx, start_date=start, end_date=end, asset_type="stock") if idx else None
    prices: list[dict] = []
    if df is not None and len(df) > 0:
        s = df.copy()
        s["date"] = pd.to_datetime(s["date"]).sort_values()
        prices = [{"date": d.strftime("%Y-%m-%d"), "close": float(c)}
                  for d, c in zip(s["date"], s["close"])]
    options_report["timeline"] = {"underlying": underlying.upper(), "prices": prices}


def index_calendar(sd, underlying: str, start: date, end: date) -> list[pd.Timestamp]:
    """Trading-day calendar for the backtest = the underlying index's print dates."""
    idx = INDEX_SYMBOL.get(underlying.upper())
    df = sd.get_prices(symbol=idx, start_date=start, end_date=end, asset_type="stock") if idx else None
    if df is None or len(df) == 0:
        return []
    return [pd.Timestamp(d) for d in pd.to_datetime(df["date"]).sort_values().tolist()]


def build_live_options_run(sd, underlying: str, lot_overrides: dict | None = None,
                           margin_params: dict | None = None, now=None):
    """Assemble (market_view, chain_view, settler, margin_model) for a LIVE/paper options
    deployment — the real-time analogue of ``build_options_run``. Strike/expiry selection
    reads the cache chain; contract marks come from live quotes fed to the market view,
    with a cache fallback. The session feeds quotes via ``update_quotes`` and advances the
    cursor each decision."""
    from skas_algo.engine.live_options_market import LiveOptionsMarketView

    loader = get_options_loader(sd, lot_overrides)
    cache_spot = make_spot_provider(sd)
    # Strike selection prefers the LIVE index spot (fed from the index LTP) and falls back
    # to the cached close; settlement/margin stay on the official cached close.
    index_spots: dict[str, float] = {}

    def live_spot(u: str, on_date: date):
        s = index_spots.get(u.upper())
        return s if s is not None else cache_spot(u, on_date)

    # Today's bhavcopy isn't in the cache during a live session (it's EOD data), so the
    # chain provider walks back to the most recent available day for strike/expiry listing
    # (those are forward-looking and stable intraday; the live spot drives selection).
    base_chain = make_chain_provider(sd)

    def live_chain_provider(u: str, on_date: date):
        for back in range(0, 8):
            df = base_chain(u, on_date - timedelta(days=back))
            if df is not None and len(df) > 0:
                return df
        return base_chain(u, on_date)

    chain_view = OptionChainView(live_chain_provider, live_spot, lot_overrides=lot_overrides)
    market_view = LiveOptionsMarketView(chain_view, loader=loader, current_datetime=now,
                                        index_spots=index_spots)
    settler = ExpirySettler(cache_spot, lot_overrides=lot_overrides)
    margin_model = MarginModel(cache_spot, MarginParams.from_dict(margin_params),
                               lot_overrides=lot_overrides)
    return market_view, chain_view, settler, margin_model


def build_options_run(sd, underlying: str, start: date, end: date,
                      lot_overrides: dict | None = None, margin_params: dict | None = None,
                      equity_loader=None):
    """Assemble the (market_view, chain_view, settler, margin_model) for an options backtest.

    ``equity_loader`` (optional ``loader(symbol, start, end)``) lets the market view
    price PLAIN cached symbols too (e.g. GOLDBEES held against a sold call)."""
    loader = get_options_loader(sd, lot_overrides)
    spot_provider = make_spot_provider(sd)
    chain_view = OptionChainView(make_chain_provider(sd), spot_provider, lot_overrides=lot_overrides)
    calendar = index_calendar(sd, underlying, start, end)
    market_view = OptionMarketView(loader, chain_view, calendar, lot_overrides=lot_overrides,
                                   equity_loader=equity_loader)
    settler = ExpirySettler(spot_provider, lot_overrides=lot_overrides)
    margin_model = MarginModel(spot_provider, MarginParams.from_dict(margin_params),
                               lot_overrides=lot_overrides)
    return market_view, chain_view, settler, margin_model
