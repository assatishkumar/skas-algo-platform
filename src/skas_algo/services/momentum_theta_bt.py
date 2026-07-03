"""Dedicated intraday backtest for momentum_theta_gainer_intra (NIFTY only).

Deliberately NOT the shared engine: the engine's slice is one trading day (expiry
settlement, report cadence and trade serialization all assume it — see CLAUDE.md), and no
intraday option premiums exist anywhere. Instead this service:

- replays REAL 15-min NIFTY spot bars (Kite-fetched, locally cached — data/intraday_bars),
- drives the ACTUAL strategy class by replaying each bar as its o→h→l→c ticks, so the
  candle aggregation, SuperTrend, pivots, caps and exits are byte-for-byte the live code
  path (the aggregated candle equals the source bar exactly: open is first, close last,
  high/low are order-free max/min),
- prices the ATM weekly premium with Black-Scholes at BOTH entry and exit:
  σ = prior-day HV20 of the bar-derived daily closes × ``vol_multiplier`` (calibrate on the
  /research BS-vs-live panel), t = (expiry 15:30 − now). A model, not the tape: no smile,
  no event crush — flagged in the UI like donchian's synthetic stock premiums.

The strategy is flat by 15:20 every day by construction, so the daily equity curve needs
no overnight mark-to-market.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pandas as pd

from skas_algo.data.intraday_bars import load_intraday_bars
from skas_algo.engine.options import black_scholes as bs
from skas_algo.engine.options.instrument import parse
from skas_algo.engine.options.margin import MarginParams, short_option_margin
from skas_algo.strategies.momentum_theta_intra import MomentumThetaGainerIntra

_EXPIRY_TIME = {"hour": 15, "minute": 30}
_WARMUP_DAYS = 45  # calendar padding for HV20 + ST warmup + prior-day pivots


@dataclass
class MtgBtParams:
    start: date
    end: date
    lots: int = 1
    st_period: int = 7
    st_multiplier: float = 3.0
    max_trades_per_day: int = 3
    entry_cutoff: str = "15:00"
    eod_exit: str = "15:20"
    min_dte: int = 0
    vol_multiplier: float = 1.1
    r: float = 0.065
    slippage_bps: float = 5.0     # applied against us on both entry and exit premiums
    capital: float = 500_000
    underlying: str = "NIFTY"


class _BtCtx:
    """The minimal AlgoContext surface the strategy consumes, BS-priced."""

    def __init__(self, sigma_of, params: MtgBtParams):
        self._sigma_of = sigma_of
        self._p = params
        self.spot: float = 0.0
        self._now: datetime | None = None
        self.book: dict[str, float] = {}  # symbol -> held units
        self.market = self  # index_spot lives on ctx.market

    # market surface
    def index_spot(self, _u: str) -> float | None:
        return self.spot or None

    # ctx surface
    def now(self) -> datetime:
        return self._now

    def today(self) -> date:
        return self._now.date()

    def lots(self, symbol: str) -> float:
        return self.book.get(symbol, 0.0)

    def option_chain(self):
        return None  # calendar weekly fallback (synthetic pricing anyway)

    def close(self, symbol: str) -> float:
        px = self.premium(symbol)
        if px is None:
            raise KeyError(symbol)
        return px

    def premium(self, symbol: str) -> float | None:
        inst = parse(symbol)
        if inst is None:
            return self.spot or None
        sigma = self._sigma_of(self._now.date())
        if sigma is None or not self.spot:
            return None
        expiry_dt = datetime(inst.expiry.year, inst.expiry.month, inst.expiry.day,
                             **_EXPIRY_TIME)
        t = max((expiry_dt - self._now).total_seconds(), 0.0) / (365.0 * 86400.0)
        return float(bs.price(self.spot, float(inst.strike), t, self._p.r, sigma, inst.right))


def run_backtest(params: MtgBtParams, adapter=None) -> dict:
    """Run the replay; ``adapter`` (logged-in ZerodhaAdapter) tops up the bar store."""
    fetch_start = params.start - timedelta(days=_WARMUP_DAYS)
    bars = load_intraday_bars(fetch_start, params.end, adapter=adapter)
    if bars.empty:
        return {"error": "no 15-min bars cached for this range — run once with a "
                         "logged-in Zerodha account to fetch them"}

    sigma_of = _sigma_provider(bars, params.vol_multiplier)
    strat = MomentumThetaGainerIntra(
        underlyings=[params.underlying], lots={params.underlying: params.lots},
        st_period=params.st_period, st_multiplier=params.st_multiplier,
        max_trades_per_day=params.max_trades_per_day,
        eod_exit=params.eod_exit, entry_cutoff=params.entry_cutoff, min_dte=params.min_dte,
    )
    strat._seeded = True  # bars arrive via replay, not the live warmup hook
    ctx = _BtCtx(sigma_of, params)
    slip = params.slippage_bps / 10_000.0
    mp = MarginParams()

    open_pos: dict | None = None
    trades: list[dict] = []
    skipped_entries = 0

    def handle(signals: list) -> None:
        nonlocal open_pos, skipped_entries
        for sig in signals:
            px = ctx.premium(sig.symbol)
            if px is None:
                skipped_entries += 1
                continue
            if sig.action.name == "ENTER_SHORT":
                entry = px * (1 - slip)
                units = float(sig.quantity)
                open_pos = {
                    "symbol": sig.symbol, "units": units, "entry_premium": entry,
                    "entry_time": ctx.now(), "entry_reason": sig.reason,
                    "entry_spot": ctx.spot,
                    "margin": float(short_option_margin(ctx.spot, units, 1, mp)),
                }
                ctx.book[sig.symbol] = units
            else:  # EXIT_ALL
                if open_pos is None or sig.symbol != open_pos["symbol"]:
                    ctx.book.pop(sig.symbol, None)
                    continue
                exit_px = px * (1 + slip)
                pnl = (open_pos["entry_premium"] - exit_px) * open_pos["units"]
                trades.append({
                    "entry_time": open_pos["entry_time"].isoformat(),
                    "exit_time": ctx.now().isoformat(),
                    "symbol": open_pos["symbol"],
                    "side": "bull_put" if open_pos["entry_reason"] == "mtg_bull" else "bear_call",
                    "exit_reason": sig.reason,
                    "entry_premium": round(open_pos["entry_premium"], 2),
                    "exit_premium": round(exit_px, 2),
                    "units": open_pos["units"],
                    "entry_spot": open_pos["entry_spot"],
                    "exit_spot": ctx.spot,
                    "margin": round(open_pos["margin"]),
                    "pnl": round(pnl, 2),
                })
                ctx.book.pop(sig.symbol, None)
                open_pos = None

    # ---- replay: each bar as o→h→l→c ticks; the next bar's open tick closes the candle.
    report_start = pd.Timestamp(params.start)
    offsets = [timedelta(seconds=1), timedelta(minutes=5),
               timedelta(minutes=10), timedelta(minutes=14)]
    for row in bars.itertuples(index=False):
        start = row.start.to_pydatetime() if hasattr(row.start, "to_pydatetime") else row.start
        in_report = row.start >= report_start
        for off, px in zip(offsets, (row.open, row.high, row.low, row.close), strict=True):
            ctx._now = start + off
            ctx.spot = float(px)
            sigs = strat.on_slice(ctx)
            if in_report:
                handle(sigs)
            else:
                # Warmup period: build candles/pivots but discard any would-be trades.
                for s in sigs:
                    ctx.book.pop(s.symbol, None)
                strat.open_leg[params.underlying] = None
                strat.entries_today[params.underlying] = 0

    return _report(trades, bars, params, skipped_entries)


def _sigma_provider(bars: pd.DataFrame, vol_multiplier: float):
    """Prior-day HV20 (annualized, floored 5%) × multiplier, from bar-derived daily closes
    — shift(1) so an entry on day d never sees day d's own close (no lookahead)."""
    daily = bars.assign(day=bars["start"].dt.date).groupby("day")["close"].last()
    rets = pd.Series(daily).pipe(lambda s: pd.Series(
        [math.log(b / a) for a, b in zip(s.iloc[:-1], s.iloc[1:], strict=True)],
        index=s.index[1:]))
    hv = (rets.rolling(20).std() * math.sqrt(252)).shift(1).clip(lower=0.05)
    table = {d: (None if pd.isna(v) else float(v) * vol_multiplier) for d, v in hv.items()}
    last_valid = None

    def sigma_of(d: date):
        nonlocal last_valid
        v = table.get(d)
        if v is not None:
            last_valid = v
        return v if v is not None else last_valid

    return sigma_of


def _report(trades: list[dict], bars: pd.DataFrame, params: MtgBtParams,
            skipped: int) -> dict:
    if not trades:
        return {"params": _params_out(params), "trades": [], "stats": {"trades": 0},
                "equity": [], "note": "no trades in range", "skipped_entries": skipped}
    df = pd.DataFrame(trades)
    df["day"] = df["exit_time"].str[:10]
    daily_pnl = df.groupby("day")["pnl"].sum()

    # Daily equity (flat overnight by construction — 15:20 exit).
    equity, cum = [], params.capital
    for d, v in daily_pnl.items():
        cum += v
        equity.append({"date": d, "equity": round(cum, 2), "pnl": round(v, 2)})
    peak, max_dd = -1e18, 0.0
    for row in equity:
        peak = max(peak, row["equity"])
        max_dd = max(max_dd, (peak - row["equity"]) / peak * 100 if peak > 0 else 0)

    total_days = bars[bars["start"] >= pd.Timestamp(params.start)]["start"].dt.date.nunique()
    wins = df[df["pnl"] > 0]
    by_reason = df.groupby("exit_reason")["pnl"].agg(["count", "sum"])
    by_side = df.groupby("side")["pnl"].agg(["count", "sum"])
    entries_per_day = df.groupby(df["entry_time"].str[:10]).size()
    stats = {
        "trades": int(len(df)),
        "win_rate": round(len(wins) / len(df) * 100, 1),
        "total_pnl": round(float(df["pnl"].sum()), 2),
        "return_pct": round(float(df["pnl"].sum()) / params.capital * 100, 2),
        "avg_pnl": round(float(df["pnl"].mean()), 2),
        "avg_win": round(float(wins["pnl"].mean()), 2) if len(wins) else 0.0,
        "avg_loss": round(float(df[df["pnl"] <= 0]["pnl"].mean()), 2)
        if len(df) > len(wins) else 0.0,
        "worst_day": round(float(daily_pnl.min()), 2),
        "best_day": round(float(daily_pnl.max()), 2),
        "max_drawdown_pct": round(max_dd, 2),
        "peak_margin": int(df["margin"].max()),
        "trading_days": int(len(daily_pnl)),
        "days_with_trades_pct": round(len(daily_pnl) / max(1, total_days) * 100, 1),
        "cap_saturated_days": int((entries_per_day >= params.max_trades_per_day).sum()),
        "by_exit_reason": {k: {"count": int(v["count"]), "pnl": round(float(v["sum"]), 2)}
                           for k, v in by_reason.iterrows()},
        "by_side": {k: {"count": int(v["count"]), "pnl": round(float(v["sum"]), 2)}
                    for k, v in by_side.iterrows()},
    }
    return {"params": _params_out(params), "stats": stats, "equity": equity,
            "trades": trades[-2000:], "skipped_entries": skipped}


def _params_out(p: MtgBtParams) -> dict:
    return {"start": p.start.isoformat(), "end": p.end.isoformat(), "lots": p.lots,
            "st_period": p.st_period, "st_multiplier": p.st_multiplier,
            "max_trades_per_day": p.max_trades_per_day, "entry_cutoff": p.entry_cutoff,
            "eod_exit": p.eod_exit, "min_dte": p.min_dte,
            "vol_multiplier": p.vol_multiplier, "r": p.r,
            "slippage_bps": p.slippage_bps, "capital": p.capital}
