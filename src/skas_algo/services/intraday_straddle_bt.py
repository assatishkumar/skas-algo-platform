"""intraday_straddle_bt — replay the ACTUAL IntradayStraddleStrategy over the self-captured
1-min option store (the GFD-replacement dataset).

The strategy is deploy-only with no engine backtest (the EOD slice can't model intraday
stops) — but the option store now holds real 1-min premiums with volume/OI, so this service
replays a day minute-by-minute through the REAL strategy class (the momentum_theta_bt
pattern: signal parity by construction, no strategy fork):

- **Marks**: each leg's LTP = the latest 1-min close ≤ now, forward-filled within the day
  (live marks forward-fill the same way); ``has_print`` = the leg traded today.
- **Chain**: built from the store at ``now`` for the nearest listed expiry. **Spot is
  synthesized by put-call parity** (the store has no index series): at the strike with the
  smallest |CE−PE|, F ≈ K + CE − PE — plenty accurate for ATM selection.
- **Fills**: at the same minute's close the strategy saw (≤ 1-min optimistic, same as the
  momentum_theta replay).
- **Margin**: the engine's model margin (``short_option_margin``, span+exposure % of
  notional, both legs). CAVEAT: the model reads ~1.5-2× the real broker straddle margin, so
  a −2%-of-margin stop here corresponds to a WIDER rupee stop than the same setting live
  (live uses the pushed broker margin). Tune via params["margin"] if calibrating.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from skas_algo.data.option_intraday_store import captured_days, load_day
from skas_algo.engine.options.contract_specs import lot_size_for, strike_allowed
from skas_algo.engine.options.margin import MarginParams, short_option_margin
from skas_algo.strategies.intraday_straddle import IntradayStraddleStrategy

logger = logging.getLogger(__name__)

_OPEN = time(9, 15)
_CLOSE = time(15, 30)


@dataclass
class _Quote:
    close: float
    oi: float
    minute: str


class _ReplayMarket:
    """ctx.market for the replay: forward-filled marks + a store-built live chain."""

    def __init__(self, underlying: str, expiry_iso: str, lot_size: int):
        self.underlying = underlying
        self.expiry_iso = expiry_iso
        self.lot_size = lot_size
        self.now: datetime | None = None
        self.quotes: dict[str, _Quote] = {}      # symbol -> latest bar ≤ now
        self.by_strike: dict[float, dict[str, str]] = {}  # strike -> {"CE": sym, "PE": sym}
        self.current_date: date | None = None

    def feed(self, symbol: str, close: float, oi: float, minute: str) -> None:
        self.quotes[symbol] = _Quote(close, oi, minute)

    def close(self, symbol: str) -> float:
        q = self.quotes.get(symbol)
        if q is None:
            raise KeyError(symbol)
        return q.close

    def has_print(self, symbol: str) -> bool:
        return symbol in self.quotes

    def parity_spot(self) -> float | None:
        """F ≈ K + CE − PE at the strike where |CE−PE| is smallest (both legs printed)."""
        best = None
        for k, legs in self.by_strike.items():
            ce, pe = self.quotes.get(legs.get("CE", "")), self.quotes.get(legs.get("PE", ""))
            if ce is None or pe is None:
                continue
            diff = abs(ce.close - pe.close)
            if best is None or diff < best[0]:
                best = (diff, k + ce.close - pe.close)
        return best[1] if best else None

    def index_spot(self, _u: str) -> float | None:
        return self.parity_spot()

    def live_chain(self, _u: str, _e: str) -> dict | None:
        # Same NIFTY 100-strike coarsening the LIVE chain applies (_coarsen_chain) — the
        # replay must not pick a 50-strike live would never see (parity, CLAUDE.md §8).
        rows = []
        for k in sorted(self.by_strike):
            if not strike_allowed(self.underlying, k):
                continue
            legs = self.by_strike[k]

            def info(sym: str | None) -> dict | None:
                q = self.quotes.get(sym) if sym else None
                return None if q is None else {"ltp": q.close, "oi": int(q.oi)}

            rows.append({"strike": k, "ce": info(legs.get("CE")), "pe": info(legs.get("PE"))})
        spot = self.parity_spot()
        if not rows or spot is None:
            return None
        atm = min((r["strike"] for r in rows), key=lambda s: abs(s - spot))
        return {"spot": spot, "atm_strike": atm, "lot_size": self.lot_size, "rows": rows}


class _ReplayChain:
    def __init__(self, expiries: list[date]):
        self._e = expiries

    def expiries(self, _u: str, today: date) -> list[date]:
        return [e for e in self._e if e >= today]


@dataclass
class _ReplayCtx:
    market: _ReplayMarket
    chain: _ReplayChain
    positions: dict[str, float] = field(default_factory=dict)
    _now: datetime | None = None

    def now(self) -> datetime:
        return self._now

    def today(self) -> date:
        return self._now.date()

    def option_chain(self):
        return self.chain

    def lots(self, symbol: str):
        return self.positions.get(symbol, 0)

    def close(self, symbol: str) -> float:
        return self.market.close(symbol)


def replay_day(underlying: str, day: date, params: dict | None = None) -> dict | None:
    """Replay one (underlying, day) through IntradayStraddleStrategy. None when the store
    has no bars for that underlying/day."""
    p = dict(params or {})
    mp = MarginParams.from_dict(p.pop("margin", None))
    df = load_day(day)
    if df.empty:
        return None
    df = df[df["symbol"].str.startswith(f"{underlying}|")]
    if df.empty:
        return None

    # Nearest listed expiry ≥ day (from the day's own symbols), and its per-minute bars.
    expiries = sorted({s.split("|")[1] for s in df["symbol"].unique()})
    expiries_d = [date.fromisoformat(e) for e in expiries]
    nearest = min((e for e in expiries_d if e >= day), default=None)
    if nearest is None:
        return None
    day_df = df[df["symbol"].str.split("|").str[1] == nearest.isoformat()].copy()
    lot = lot_size_for(underlying, nearest)

    market = _ReplayMarket(underlying, nearest.isoformat(), lot)
    for sym in day_df["symbol"].unique():
        _u, _e, strike_s, right = sym.split("|")
        market.by_strike.setdefault(float(strike_s), {})[right] = sym
    # minute -> [(symbol, close, oi)] for the bar-feed loop. NOTE: normalize via strftime —
    # the parquet datetime stringifies with a SPACE ("2026-07-15 09:15:00"), not ISO "T".
    import pandas as pd
    day_df["minute"] = pd.to_datetime(day_df["start"]).dt.strftime("%Y-%m-%dT%H:%M")
    feed: dict[str, list[tuple[str, float, float]]] = {}
    for sym, minute, close_px, oi in zip(day_df["symbol"], day_df["minute"],
                                         day_df["close"], day_df["oi"], strict=True):
        feed.setdefault(minute, []).append((sym, float(close_px), float(oi)))

    strategy = IntradayStraddleStrategy(underlying=underlying, **p)
    ctx = _ReplayCtx(market=market, chain=_ReplayChain(expiries_d))
    market.current_date = day

    fills: list[dict] = []
    entries: dict[str, float] = {}
    pnl = 0.0
    entry_minute = exit_minute = exit_reason = None
    cur = datetime.combine(day, _OPEN)
    end = datetime.combine(day, _CLOSE)
    while cur <= end:
        minute_key = cur.strftime("%Y-%m-%dT%H:%M")
        for sym, close_px, oi in feed.get(minute_key, []):
            market.feed(sym, close_px, oi, minute_key)
        ctx._now = cur
        market.now = cur
        for sig in strategy.on_slice(ctx):
            px = market.close(sig.symbol)
            if sig.action.name == "ENTER_SHORT":
                ctx.positions[sig.symbol] = float(sig.quantity)
                entries[sig.symbol] = px
                entry_minute = minute_key
                fills.append({"minute": minute_key[-5:], "side": "SELL",
                              "symbol": sig.symbol, "px": px, "units": sig.quantity})
            elif sig.action.name == "EXIT_ALL" and sig.symbol in entries:
                units = ctx.positions.pop(sig.symbol, 0.0)
                pnl += (entries[sig.symbol] - px) * units  # short: entry − exit
                exit_minute, exit_reason = minute_key, sig.reason
                fills.append({"minute": minute_key[-5:], "side": "BUY",
                              "symbol": sig.symbol, "px": px, "units": units})
        # Broker-margin push: live, the manager pushes the REAL basket margin within a
        # tick of the fill; here the model stands in (documented caveat above).
        if entries and strategy.margin_source == "pending" and strategy._broker_margin is None:
            spot = market.parity_spot() or 0.0
            base = sum(short_option_margin(spot, int(u), 1, mp)
                       for u in [ctx.positions.get(s, 0) for s in entries] if u)
            if base > 0:
                strategy.set_broker_margin(base)
        cur += timedelta(minutes=1)

    if not entries:
        return {"underlying": underlying, "day": day.isoformat(),
                "expiry": nearest.isoformat(), "entered": False}
    credit = sum(entries.values())
    return {
        "underlying": underlying, "day": day.isoformat(), "expiry": nearest.isoformat(),
        "entered": True, "lot_size": lot,
        "strikes": sorted({float(s.split("|")[2]) for s in entries}),
        "entry_time": entry_minute[-5:] if entry_minute else None,
        "entry_credit_per_share": round(credit, 2),
        "exit_time": exit_minute[-5:] if exit_minute else None,
        "exit_reason": exit_reason,
        "margin_base": round(strategy.margin_base, 0),
        "peak_pct": round(strategy.peak_pct, 2),
        "pnl_rupees": round(pnl, 0),
        "pnl_pct_of_margin": round(100.0 * pnl / strategy.margin_base, 2)
        if strategy.margin_base else None,
        "fills": fills,
    }


def run_backtest(underlyings: list[str] | None = None, days: list[str] | None = None,
                 params: dict | None = None) -> dict:
    """Replay every (underlying, stored day). Returns {results, totals}."""
    unders = [u.upper() for u in (underlyings or ["NIFTY", "BANKNIFTY", "SENSEX"])]
    all_days = days or captured_days()
    results = []
    for d in all_days:
        for u in unders:
            try:
                r = replay_day(u, date.fromisoformat(d), params)
            except Exception:
                logger.exception("replay failed for %s %s", u, d)
                r = None
            if r is not None:
                results.append(r)
    entered = [r for r in results if r.get("entered")]
    totals = {
        "days_tested": len(all_days), "entries": len(entered),
        "pnl_rupees": round(sum(r["pnl_rupees"] for r in entered), 0),
        "wins": sum(1 for r in entered if r["pnl_rupees"] > 0),
        "losses": sum(1 for r in entered if r["pnl_rupees"] <= 0),
    }
    return {"results": results, "totals": totals}
