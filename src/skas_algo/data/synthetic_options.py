"""Synthetic option chain for underlyings with no traded-option data (MCX GOLD).

GOLD options aren't in the NSE bhavcopy, so we **synthesize** the chain: the underlying
is the cached GOLD futures price series, priced with **Black-76** (options on futures —
the futures price already carries the cost of carry, so plain BS would bias calls rich /
puts cheap), with volatility = rolling realized vol of that series × a configurable
implied premium (see ``engine/options/realized_vol``). The same generator feeds the
Data-tab chain viewer and a backtestable market view, so a strategy sees a coherent
(if model-priced) chain.

IMPORTANT: these are MODEL prices (no smile/skew). Label them "synthetic" everywhere.
The real NIFTY/BANKNIFTY paths (`options_provider`) are untouched.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from skas_algo.data.options_provider import INDEX_SYMBOL, index_calendar, make_spot_provider
from skas_algo.engine.options import black_scholes as bs
from skas_algo.engine.options.chain import OptionChainView
from skas_algo.engine.options.instrument import parse
from skas_algo.engine.options.margin import MarginModel, MarginParams
from skas_algo.engine.options.market import OptionMarketView
from skas_algo.engine.options.realized_vol import realized_vol_provider
from skas_algo.engine.options.settlement import ExpirySettler

# GOLD synthetic defaults, modelled on MCX **GOLDM** (the contract the user trades).
GOLD_STRIKE_STEP = 500.0   # ₹ between listed strikes (MCX GOLDM strikes are in 500s)
GOLD_STRIKE_COUNT = 20     # strikes each side of ATM (→ 41 strikes × CE/PE)
GOLD_LOT_SIZE = 10         # GOLDM: 100 g quoted ₹/10g → per-lot multiplier 10 (big GOLD 1kg = 100)
# Options trade at implied > realized vol (vol-risk premium). Observed Jun-2026: GOLDM
# implied ≈ 22-24% vs 20d realized 16% (~1.4×); 1.25 is a conservative default knob.
GOLD_VOL_PREMIUM = 1.25
SYNTHETIC_UNDERLYINGS = ["GOLD"]


def is_synthetic(underlying: str) -> bool:
    return underlying.upper() in SYNTHETIC_UNDERLYINGS


def gold_strike_grid(spot: float, step: float = GOLD_STRIKE_STEP, n: int = GOLD_STRIKE_COUNT) -> list[float]:
    atm = round(spot / step) * step
    return [atm + i * step for i in range(-n, n + 1) if atm + i * step > 0]


def gold_monthly_expiries(on_date: date, ahead: int = 3, day: int = 26) -> list[date]:
    """The next ``ahead`` monthly option expiries on/after ``on_date``.

    MCX GOLDM options expire in the LAST week of the month, days before the underlying
    futures' tender period (e.g. the JUL-2026 contract's options expired 26 Jun) — NOT on
    the 5th, which is the FUTURES expiry. Approximated as the ``day``-th of each month
    rolled back to a weekday; exact per-contract dates would need the Kite instruments
    dump (NEEDS-CONFIRM for history).
    """
    out: list[date] = []
    y, m = on_date.year, on_date.month
    while len(out) < ahead:
        try:
            exp = date(y, m, day)
        except ValueError:
            exp = date(y, m, 28)
        while exp.weekday() >= 5:  # weekend → prior business day
            exp -= timedelta(days=1)
        if exp >= on_date:
            out.append(exp)
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def synthetic_chain_df(underlying: str, on_date: date, expiry: date, spot: float, vol: float,
                       r: float = 0.065, step: float = GOLD_STRIKE_STEP,
                       n: int = GOLD_STRIKE_COUNT) -> pd.DataFrame:
    """A Black-76-priced chain for one expiry, in the shape OptionChainView /
    `_pivot_chain` consume (expiry_date/strike_price/option_type/close/settle/oi/...).

    open_interest=1 is a nominal tradability marker: strategies use oi>0 to guard
    against frozen bhavcopy quotes, a hazard that can't occur on model prices — oi=0
    would make them silently skip every synthetic leg."""
    t = max((expiry - on_date).days, 0) / 365.0
    rows = []
    for k in gold_strike_grid(spot, step, n):
        for right in ("CE", "PE"):
            px = bs.black76_price(spot, k, t, r, vol, right)
            rows.append({
                "trade_date": on_date, "symbol": underlying.upper(), "expiry_date": expiry,
                "strike_price": float(k), "option_type": right,
                "open": px, "high": px, "low": px, "close": px, "ltp": px, "settle_price": px,
                "contracts": 0, "value_in_lakh": 0.0, "open_interest": 1, "change_in_oi": 0,
            })
    return pd.DataFrame(rows)


def _close_series(sd, underlying: str) -> pd.Series:
    idx = INDEX_SYMBOL.get(underlying.upper())
    df = sd.get_prices(symbol=idx, asset_type="stock") if idx else None
    if df is None or len(df) == 0:
        return pd.Series(dtype=float)
    s = df.copy()
    s["date"] = pd.to_datetime(s["date"])
    return s.set_index("date")["close"].sort_index()


def synthetic_expiries(underlying: str, on_date: date, ahead: int = 3) -> list[date]:
    return gold_monthly_expiries(on_date, ahead=ahead)


def synthetic_chain_for_view(sd, underlying: str, on_date: date, expiry: date,
                             r: float = 0.065, vol_window: int = 20,
                             step: float = GOLD_STRIKE_STEP, n: int = GOLD_STRIKE_COUNT,
                             vol_premium: float = GOLD_VOL_PREMIUM):
    """(spot, vol, DataFrame) for the Data-tab chain viewer — or (None, None, empty) if no spot."""
    spot = make_spot_provider(sd)(underlying.upper(), on_date)
    if spot is None:
        return None, None, pd.DataFrame()
    vol = realized_vol_provider(_close_series(sd, underlying), window=vol_window)(on_date) * vol_premium
    return spot, vol, synthetic_chain_df(underlying, on_date, expiry, spot, vol, r, step, n)


def build_synthetic_options_run(sd, underlying: str, start: date, end: date,
                                lot_overrides: dict | None = None, margin_params: dict | None = None,
                                r: float = 0.065, vol_window: int = 20,
                                strike_step: float = GOLD_STRIKE_STEP,
                                strike_count: int = GOLD_STRIKE_COUNT, expiries_ahead: int = 3,
                                vol_premium: float = GOLD_VOL_PREMIUM, equity_loader=None):
    """Assemble (market_view, chain_view, settler, margin_model) for a SYNTHETIC options
    backtest, parallel to ``options_provider.build_options_run`` but Black-76-generated.

    Loader and chain price off the same spot+vol per date, so a contract entered from the
    chain at premium P is marked at exactly P that day (no drift); expiry settles to
    intrinsic (Black-76 returns intrinsic at t≤0).
    """
    u = underlying.upper()
    lot_overrides = lot_overrides or {u: GOLD_LOT_SIZE}
    spot_provider = make_spot_provider(sd)
    _rv_on = realized_vol_provider(_close_series(sd, u), window=vol_window)

    def vol_on(d: date) -> float:
        return _rv_on(d) * vol_premium

    def chain_provider(uu: str, on_date: date):
        spot = spot_provider(uu, on_date)
        if spot is None:
            return None
        vol = vol_on(on_date)
        frames = [synthetic_chain_df(uu, on_date, exp, spot, vol, r, strike_step, strike_count)
                  for exp in gold_monthly_expiries(on_date, ahead=expiries_ahead)]
        return pd.concat(frames, ignore_index=True) if frames else None

    chain_view = OptionChainView(chain_provider, spot_provider, lot_overrides=lot_overrides)
    calendar = index_calendar(sd, u, start, end)

    def loader(symbol: str, lo: date, hi: date):
        inst = parse(symbol, lot_overrides=lot_overrides)
        if inst is None:
            return None
        rows = []
        for ts in calendar:
            d = ts.date() if hasattr(ts, "date") else ts
            if d < lo or d > hi:
                continue
            spot = spot_provider(inst.underlying, d)
            if spot is None:
                continue
            t = max((inst.expiry - d).days, 0) / 365.0
            rows.append({"date": d, "close": bs.black76_price(spot, inst.strike, t, r, vol_on(d), inst.right)})
        return pd.DataFrame(rows)

    market_view = OptionMarketView(loader, chain_view, calendar, lot_overrides=lot_overrides,
                                   equity_loader=equity_loader)
    settler = ExpirySettler(spot_provider, lot_overrides=lot_overrides)
    margin_model = MarginModel(spot_provider, MarginParams.from_dict(margin_params),
                               lot_overrides=lot_overrides)
    return market_view, chain_view, settler, margin_model
