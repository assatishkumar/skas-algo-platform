"""Multi-underlying options run for basket backtests (donchian_strangle_bt).

There is NO cached history for stock options (the bhavcopy cache holds index chains
only), so stock contracts are priced with plain Black-Scholes off the stock's cached
daily closes: sigma = rolling realized HV(``vol_window``) × ``vol_multiplier``, q = 0
(no dividend data — flagged model bias), r caller-supplied. NIFTY contracts — the
basket's tail hedge — read the REAL cached chain (2020+), so hedge costs are market
prices, not model prices. The loader routes per contract symbol; everything downstream
(fills, marks, expiry settlement to intrinsic, margin) is the unchanged shared engine.

Parallel to ``synthetic_options.build_synthetic_options_run`` (the single-underlying
GOLD/Black-76 variant) — kept separate so the GOLD path stays byte-identical.

Model caveats (surface these wherever results are shown): BS-with-realized-HV has no
vol-risk premium, smile, or term structure — ``vol_multiplier`` is the single blunt
knob (calibrate it against the live chain via /research/bs-calibration); lot sizes are
today's flat snapshot (contract_specs); strike steps are a price-band heuristic.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

import pandas as pd

from skas_algo.data.options_provider import (
    INDEX_SYMBOL,
    get_options_loader,
    index_calendar,
    make_chain_provider,
    make_spot_provider,
)
from skas_algo.engine.options import black_scholes as bs
from skas_algo.engine.options.chain import OptionChainView
from skas_algo.engine.options.contract_specs import expected_monthly_expiry
from skas_algo.engine.options.instrument import parse
from skas_algo.engine.options.margin import MarginModel, MarginParams
from skas_algo.engine.options.market import OptionMarketView
from skas_algo.engine.options.realized_vol import realized_vol_provider
from skas_algo.engine.options.settlement import ExpirySettler

# A contract only needs prices over its life; OptionMarketView asks the loader for the
# WHOLE run window, so an unclamped BS loader would price ~6 years per contract.
_CONTRACT_LIFE_DAYS = 60

# NSE strike steps follow the price band (convention, not a published table) — the strike
# grid a stock's synthetic chain lists. Approximate; the schedule builder uses the same
# step so picked strikes always exist on the grid.
_STRIKE_BANDS: list[tuple[float, float]] = [
    (50, 1.0), (100, 2.5), (250, 5.0), (500, 10.0),
    (1000, 20.0), (2500, 50.0), (5000, 100.0),
]


def stock_strike_step(spot: float) -> float:
    for ceiling, step in _STRIKE_BANDS:
        if spot < ceiling:
            return step
    return 250.0


def stock_strike_grid(spot: float, step: float, n: int = 30) -> list[float]:
    atm = round(spot / step) * step
    return [atm + i * step for i in range(-n, n + 1) if atm + i * step > 0]


def _close_series(sd, underlying: str) -> pd.Series:
    """The underlying's cached close series (index symbol for indices, else the stock)."""
    sym = INDEX_SYMBOL.get(underlying.upper()) or underlying.upper()
    df = sd.get_prices(symbol=sym, asset_type="stock")
    if df is None or len(df) == 0:
        return pd.Series(dtype=float)
    s = df.copy()
    s["date"] = pd.to_datetime(s["date"])
    return s.set_index("date")["close"].sort_index()


def _stock_chain_df(underlying: str, on_date: date, expiry: date, spot: float, vol: float,
                    r: float, step: float, n: int) -> pd.DataFrame:
    """A plain-BS chain frame for one expiry, in the shape ``get_option_chain`` returns.
    open_interest=1 marks the synthetic rows tradable (see synthetic_options — oi=0 would
    make chain-guarded strategies skip every leg)."""
    t = max((expiry - on_date).days, 0) / 365.0
    rows = []
    for k in stock_strike_grid(spot, step, n):
        for right in ("CE", "PE"):
            px = bs.price(spot, k, t, r, vol, right)
            rows.append({
                "trade_date": on_date, "symbol": underlying.upper(), "expiry_date": expiry,
                "strike_price": float(k), "option_type": right,
                "open": px, "high": px, "low": px, "close": px, "ltp": px, "settle_price": px,
                "contracts": 0, "value_in_lakh": 0.0, "open_interest": 1, "change_in_oi": 0,
            })
    return pd.DataFrame(rows)


def _next_monthly_expiries(on_date: date, ahead: int = 2) -> list[date]:
    """The next ``ahead`` calendar-expected NIFTY monthly expiries on/after ``on_date``
    (stock options settle on the same monthly day as NIFTY)."""
    out: list[date] = []
    y, m = on_date.year, on_date.month
    while len(out) < ahead:
        e = expected_monthly_expiry("NIFTY", y, m)
        if e is not None and e >= on_date:
            out.append(e)
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def build_basket_options_run(
    sd, names: list[str], start: date, end: date, *,
    r: float = 0.065, vol_window: int = 20, vol_multiplier: float = 1.0,
    lot_overrides: dict | None = None, margin_params: dict | None = None,
    strike_count: int = 30, equity_loader=None,
) -> tuple[OptionMarketView, OptionChainView, ExpirySettler, MarginModel]:
    """(market_view, chain_view, settler, margin_model) for a stock-basket options
    backtest: BS-priced stock contracts + the REAL cached NIFTY chain, one calendar
    (NIFTY 50 print dates), shared settler/margin (multi-underlying already)."""
    spot_provider = make_spot_provider(sd)
    real_loader = get_options_loader(sd, lot_overrides)
    real_chain = make_chain_provider(sd)
    calendar = index_calendar(sd, "NIFTY", start, end)
    cal_dates = [ts.date() for ts in calendar]

    # Lazy per-underlying vol curves — only names the run actually touches get built.
    _vol_fns: dict[str, Callable[[date], float]] = {}

    def vol_on(underlying: str, d: date) -> float:
        fn = _vol_fns.get(underlying)
        if fn is None:
            fn = realized_vol_provider(_close_series(sd, underlying), window=vol_window)
            _vol_fns[underlying] = fn
        return float(fn(d)) * vol_multiplier

    def loader(symbol: str, lo: date, hi: date):
        inst = parse(symbol, lot_overrides=lot_overrides)
        if inst is None:
            return None
        if inst.underlying == "NIFTY":  # the hedge trades REAL cached premiums
            return real_loader(symbol, lo, hi)
        lo = max(lo, inst.expiry - timedelta(days=_CONTRACT_LIFE_DAYS))
        hi = min(hi, inst.expiry)  # BS(t=0) = intrinsic — the mark converges to settlement
        rows = []
        for d in cal_dates:
            if d < lo or d > hi:
                continue
            spot = spot_provider(inst.underlying, d)
            if spot is None:
                continue
            t = max((inst.expiry - d).days, 0) / 365.0
            rows.append({"date": d,
                         "close": bs.price(spot, inst.strike, t, r,
                                           vol_on(inst.underlying, d), inst.right)})
        return pd.DataFrame(rows)

    def chain_provider(underlying: str, on_date: date):
        if underlying.upper() == "NIFTY":
            return real_chain(underlying, on_date)
        spot = spot_provider(underlying, on_date)
        if spot is None:
            return None
        step = stock_strike_step(spot)
        vol = vol_on(underlying, on_date)
        frames = [_stock_chain_df(underlying, on_date, exp, spot, vol, r, step, strike_count)
                  for exp in _next_monthly_expiries(on_date)]
        return pd.concat(frames, ignore_index=True) if frames else None

    # Daily H/L per underlying, for touch-basis breach checks on daily bars. Lazy like vol.
    _ohlc: dict[str, pd.DataFrame] = {}

    def day_range(underlying: str, d: date):
        u = underlying.upper()
        df = _ohlc.get(u)
        if df is None:
            sym = INDEX_SYMBOL.get(u) or u
            raw = sd.get_prices(symbol=sym, start_date=start, end_date=end,
                                asset_type="stock")
            if raw is None or len(raw) == 0:
                df = pd.DataFrame(columns=["high", "low"])
            else:
                df = raw.copy()
                df["date"] = pd.to_datetime(df["date"]).dt.date
                df = df.set_index("date")[["high", "low"]]
            _ohlc[u] = df
        try:
            row = df.loc[d]
        except KeyError:
            return None
        return float(row["high"]), float(row["low"])

    chain_view = OptionChainView(chain_provider, spot_provider, lot_overrides=lot_overrides)
    market_view = OptionMarketView(loader, chain_view, calendar, lot_overrides=lot_overrides,
                                   equity_loader=equity_loader, day_range_provider=day_range)
    settler = ExpirySettler(spot_provider, lot_overrides=lot_overrides)
    margin_model = MarginModel(spot_provider, MarginParams.from_dict(margin_params),
                               lot_overrides=lot_overrides)
    return market_view, chain_view, settler, margin_model
