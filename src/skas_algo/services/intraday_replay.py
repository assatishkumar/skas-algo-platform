"""intraday_replay — the unified Backtest page's INTRADAY basis.

Replays any deploy-only (Path-B / ``ctx.market.live_chain``-reading) options strategy over
the self-captured 1-min option store, minute by minute, through the ACTUAL strategy class —
the momentum_theta_bt principle (signal parity by construction), now with REAL premiums:

- ONE strategy instance across the whole date range (weekly cycles / done_expiry latches /
  entered_day guards carry exactly like a live deployment's recovery).
- Marks forward-fill within a day (like live), reset at each day open; ``has_print`` = the
  leg traded today. Spot is synthesized by put-call parity (F ≈ K + CE − PE at the strike
  with the smallest |CE−PE| on the nearest expiry) — the store has no index series.
- ``live_chain`` serves any stored expiry with the NIFTY-100 coarsening (parity with the
  live chain's ``_coarsen_chain``).
- Broker-margin pushes are simulated with the engine model margin every minute the book is
  open (the live manager pushes ~1/min and re-bases on a changed book). CAVEAT: the model
  reads ~1.5-2× the real broker straddle margin, so %-of-margin stops are wider in rupees
  than the same settings live.
- Fills at the same minute's close the strategy saw (≤1-min optimistic); **F&O charges**
  (``charges_for_txn`` — brokerage/STT/exchange/stamp/GST) are deducted per fill, so the
  equity curve is net of costs like the EOD engine's.
- A leg still open on its own expiry day is SETTLED at 15:30 to parity-spot intrinsic
  (zero brokerage, like the engine's settlement).

The output adapter emits the EXACT report contract the existing Runs list / RunDetail /
Compare pages render (metrics keys + equity_curve + Trade-shaped trade_log; the "options"
key is deliberately ABSENT — its presence flips ReportView into a layout that requires the
full options sub-report).

``weekly_intraday_straddle`` additionally gets ``set_option_bars_fn`` served straight from
the store, so its x/VWAP/prior-day-low replay from the same bars the live strategy fetches
from Kite — its first-ever backtest.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

import pandas as pd

from skas_algo.data.option_intraday_store import captured_days, load_contract_bars, load_day
from skas_algo.engine.options.charges import charges_for_txn
from skas_algo.engine.options.contract_specs import lot_size_for, strike_allowed
from skas_algo.engine.options.margin import MarginParams, short_option_margin
from skas_algo.strategies.registry import get_strategy

logger = logging.getLogger(__name__)

_OPEN = time(9, 15)
_CLOSE = time(15, 30)

# Strategies the harness can replay (Path-B chain readers). momentum_theta_gainer_intra is
# handled at the route layer by its dedicated BS service — not here.
REPLAYABLE = {"intraday_straddle", "weekly_intraday_straddle", "call_put_ratio_expiry",
              "delta_neutral_monthly", "iron_fly_monthly"}


class _Market:
    """ctx.market for the replay: per-day forward-filled marks + store-built chains."""

    def __init__(self, underlying: str):
        self.underlying = underlying
        self.quotes: dict[str, tuple[float, float]] = {}   # symbol -> (close, oi)
        # expiry_iso -> strike -> {"CE": sym, "PE": sym}; rebuilt per day from stored symbols.
        self.chains: dict[str, dict[float, dict[str, str]]] = {}
        self.current_date: date | None = None
        self.now: datetime | None = None

    def start_day(self, day: date, symbols: list[str]) -> None:
        self.current_date = day
        self.quotes = {}          # live marks don't survive overnight — neither do these
        self.chains = {}
        for sym in symbols:
            _u, e, strike_s, right = sym.split("|")
            self.chains.setdefault(e, {}).setdefault(float(strike_s), {})[right] = sym

    def feed(self, symbol: str, close: float, oi: float) -> None:
        self.quotes[symbol] = (float(close), float(oi))

    def close(self, symbol: str) -> float:
        q = self.quotes.get(symbol)
        if q is None:
            raise KeyError(symbol)
        return q[0]

    def has_print(self, symbol: str) -> bool:
        return symbol in self.quotes

    def _parity(self, expiry_iso: str) -> float | None:
        best = None
        for k, legs in self.chains.get(expiry_iso, {}).items():
            ce = self.quotes.get(legs.get("CE", ""))
            pe = self.quotes.get(legs.get("PE", ""))
            if ce is None or pe is None:
                continue
            diff = abs(ce[0] - pe[0])
            if best is None or diff < best[0]:
                best = (diff, k + ce[0] - pe[0])
        return best[1] if best else None

    def index_spot(self, _u: str) -> float | None:
        for e in sorted(self.chains):   # nearest stored expiry that has a parity pair
            spot = self._parity(e)
            if spot is not None:
                return spot
        return None

    def live_chain(self, _u: str, expiry_iso: str) -> dict | None:
        strikes = self.chains.get(str(expiry_iso)[:10])
        if not strikes:
            return None
        rows = []
        for k in sorted(strikes):
            if not strike_allowed(self.underlying, k):
                continue  # same NIFTY-100 coarsening the LIVE chain applies

            def info(sym: str | None) -> dict | None:
                q = self.quotes.get(sym) if sym else None
                return None if q is None else {"ltp": q[0], "oi": int(q[1])}

            legs = strikes[k]
            rows.append({"strike": k, "ce": info(legs.get("CE")), "pe": info(legs.get("PE"))})
        spot = self._parity(str(expiry_iso)[:10]) or self.index_spot(self.underlying)
        if not rows or spot is None:
            return None
        atm = min((r["strike"] for r in rows), key=lambda s: abs(s - spot))
        try:
            lot = lot_size_for(self.underlying, date.fromisoformat(str(expiry_iso)[:10]))
        except KeyError:
            lot = 0
        return {"spot": spot, "atm_strike": atm, "lot_size": lot, "rows": rows}


class _Chain:
    """ctx.option_chain() — the day's stored expiries."""

    def __init__(self):
        self.days: list[date] = []

    def expiries(self, _u: str, _today: date) -> list[date]:
        return list(self.days)


class _Ctx:
    def __init__(self, market: _Market, chain: _Chain):
        self.market = market
        self.chain = chain
        self.positions: dict[str, dict] = {}   # symbol -> {units, dir, entry}
        self._now: datetime | None = None

    def now(self) -> datetime:
        return self._now

    def today(self) -> date:
        return self._now.date()

    def option_chain(self):
        return self.chain

    def lots(self, symbol: str):
        return 1 if symbol in self.positions else 0

    def close(self, symbol: str) -> float:
        return self.market.close(symbol)


def _store_bars_fn(u: str, expiry_iso, strike: float, right: str,
                   from_dt: datetime, to_dt: datetime, minutes: int) -> list[dict]:
    """weekly_intraday_straddle's ``option_bars_fn``, served from the store instead of Kite.
    Caps at ``to_dt`` so the strategy never sees the future (it drops the in-progress bar
    itself, exactly as live)."""
    df = load_contract_bars(u, str(expiry_iso)[:10], float(strike), right,
                            from_dt.date(), to_dt.date(), minutes=minutes)
    if df.empty:
        return []
    df = df[pd.to_datetime(df["start"]) <= to_dt]
    return [{"start": pd.Timestamp(r.start).isoformat(), "o": float(r.open),
             "h": float(r.high), "l": float(r.low), "c": float(r.close),
             "volume": float(r.volume)} for r in df.itertuples()]


def _intrinsic(spot: float, strike: float, right: str) -> float:
    return max(spot - strike, 0.0) if right == "CE" else max(strike - spot, 0.0)


def run_intraday_backtest(strategy_id: str, underlying: str, start: date, end: date,
                          capital: float, params: dict | None = None) -> dict:
    """Replay ``strategy_id`` over the store days in [start, end]. Returns
    {"report", "trades"} in the standard run contract (see module docstring)."""
    if strategy_id not in REPLAYABLE:
        raise ValueError(f"{strategy_id} is not intraday-replayable (supported: "
                         f"{sorted(REPLAYABLE)})")
    p = dict(params or {})
    mp = MarginParams.from_dict(p.pop("margin", None))
    u = underlying.upper()
    days = [date.fromisoformat(d) for d in captured_days()
            if start.isoformat() <= d <= end.isoformat()]
    if not days:
        raise ValueError("the option store has no captured days in this window — "
                         "see Data → Options for coverage")

    factory = get_strategy(strategy_id)
    if strategy_id == "call_put_ratio_expiry":
        p.setdefault("underlyings", [u])   # cpre ignores ``universe`` — takes underlyings
    strategy = factory(universe=[u], initial_capital=capital, **p)
    market = _Market(u)
    chain = _Chain()
    ctx = _Ctx(market, chain)
    if hasattr(strategy, "set_option_bars_fn"):
        strategy.set_option_bars_fn(_store_bars_fn)

    trades: list[dict] = []
    equity_curve: list[dict] = []
    positions: list[dict] = []       # per-leg round trips → options.positions / legs_detail
    cycles: list[dict] = []          # flat→open→flat episodes → options.cycles
    charges_bd = {k: 0.0 for k in ("brokerage", "stt", "exchange", "sebi", "stamp", "gst",
                                   "total")}
    margin_by_day: dict[str, float] = {}    # day → peak pushed margin
    premium_by_day: dict[str, float] = {}   # day → net credit collected that day
    episode: dict | None = None              # the open cycle being assembled
    realized = 0.0
    days_with_bars = 0

    def _charge(action: str, units: float, px: float) -> float:
        c = charges_for_txn({"action": action, "amount": units * px})
        for k, v in c.items():
            charges_bd[k] += v
        return c["total"]

    def _holding_days(entry_minute: str, exit_minute: str) -> float:
        """Calendar days + the intraday fraction of a 6.25h session (0.11 = ~40 min)."""
        e = datetime.fromisoformat(entry_minute.replace(" ", "T"))
        x = datetime.fromisoformat(exit_minute.replace(" ", "T"))
        whole = (x.date() - e.date()).days
        intraday = (x - datetime.combine(x.date(), e.time())).total_seconds() / (6.25 * 3600)
        return round(max(0.0, whole + max(0.0, min(1.0, intraday))), 2)

    def _fill(sig, minute: str) -> None:
        nonlocal realized, episode
        sym = sig.symbol
        px = market.close(sym)
        act = sig.action.name
        if act in ("ENTER_SHORT", "ENTER_LONG"):
            d = -1 if act == "ENTER_SHORT" else 1
            units = float(sig.quantity or 0)
            ctx.positions[sym] = {"units": units, "dir": d, "entry": px, "entered": minute}
            _charge("SHORT" if d < 0 else "BUY", units, px)
            day_key = minute[:10]
            premium_by_day[day_key] = premium_by_day.get(day_key, 0.0) + d * -1 * px * units
            if episode is None:
                episode = {"entry_minute": minute, "closed": [], "premium": 0.0}
            if d < 0:
                episode["premium"] += px * units
            trades.append({"date": minute, "ticker": sym,
                           "action": "SHORT" if d < 0 else "BUY", "units": units,
                           "price": px, "profit": None, "tag": sig.reason})
        elif act in ("EXIT_ALL", "SETTLE") and sym in ctx.positions:
            pos = ctx.positions.pop(sym)
            close_act = ("SETTLE" if act == "SETTLE"
                         else "COVER" if pos["dir"] < 0 else "SELL")
            pnl = (px - pos["entry"]) * pos["units"] * pos["dir"]
            pnl -= _charge(close_act, pos["units"], px)
            realized += pnl
            basis = pos["entry"] * pos["units"]
            trades.append({"date": minute, "ticker": sym, "action": close_act,
                           "units": pos["units"], "price": px, "profit": round(pnl, 2),
                           "pnl_pct": round(100 * pnl / basis, 2) if basis else None,
                           "tag": sig.reason})
            _u, e_iso, strike_s, right = sym.split("|")
            try:
                per_lot = lot_size_for(_u, date.fromisoformat(e_iso))
            except KeyError:
                per_lot = 0
            leg = {"symbol": sym, "underlying": _u, "strike": float(strike_s),
                   "right": right, "side": "short" if pos["dir"] < 0 else "long",
                   "expiry": e_iso, "entry_date": pos["entered"],
                   "entry_premium": pos["entry"], "exit_date": minute, "exit_price": px,
                   "exit_action": close_act, "exit_reason": sig.reason or "",
                   "units": pos["units"],
                   "lots": int(pos["units"] // per_lot) if per_lot else 0,
                   "multiplier": 1, "holding_days": _holding_days(pos["entered"], minute),
                   "pnl": round(pnl, 2)}
            positions.append(leg)
            if episode is not None:
                episode["closed"].append(leg)
                if not ctx.positions:   # back to flat → the cycle is complete
                    legs = episode["closed"]
                    ce = next((x for x in legs if x["right"] == "CE"), None)
                    pe = next((x for x in legs if x["right"] == "PE"), None)
                    cycles.append({
                        "underlying": u, "entry_date": episode["entry_minute"],
                        "expiry": legs[-1]["expiry"],
                        "legs": [x["symbol"] for x in legs],
                        "legs_detail": legs,
                        "premium_collected": round(episode["premium"], 2),
                        "realized_pnl": round(sum(x["pnl"] for x in legs), 2),
                        "net_pnl": round(sum(x["pnl"] for x in legs), 2),
                        "holding_days": _holding_days(episode["entry_minute"], minute),
                        "exit_reason": sig.reason or "",
                        "ce": ce if len(legs) == 2 else None,
                        "pe": pe if len(legs) == 2 else None,
                    })
                    episode = None

    for day in days:
        df = load_day(day)
        df = df[df["symbol"].str.startswith(f"{u}|")]
        if df.empty:
            continue
        days_with_bars += 1
        market.start_day(day, list(df["symbol"].unique()))
        chain.days = sorted({date.fromisoformat(s.split("|")[1])
                             for s in df["symbol"].unique()})
        feed: dict[str, list[tuple[str, float, float]]] = {}
        minutes = pd.to_datetime(df["start"]).dt.strftime("%Y-%m-%dT%H:%M")
        for sym, minute, close_px, oi in zip(df["symbol"], minutes, df["close"], df["oi"],
                                             strict=True):
            feed.setdefault(minute, []).append((sym, float(close_px), float(oi)))

        cur = datetime.combine(day, _OPEN)
        end_dt = datetime.combine(day, _CLOSE)
        while cur <= end_dt:
            minute_key = cur.strftime("%Y-%m-%dT%H:%M")
            for sym, close_px, oi in feed.get(minute_key, []):
                market.feed(sym, close_px, oi)
            ctx._now = cur
            market.now = cur
            try:
                signals = strategy.on_slice(ctx)
            except Exception:  # a bad minute must not void the whole range
                logger.exception("replay on_slice failed at %s (%s)", minute_key, strategy_id)
                signals = []
            for sig in signals:
                try:
                    _fill(sig, minute_key.replace("T", " "))
                except KeyError:
                    logger.warning("replay: no mark to fill %s at %s", sig.symbol, minute_key)
            # Simulated broker-margin push (~the manager's 1/min refresh, re-based on the book).
            if ctx.positions and hasattr(strategy, "set_broker_margin"):
                spot = market.index_spot(u) or 0.0
                base = sum(short_option_margin(spot, int(pos["units"]), 1, mp)
                           for pos in ctx.positions.values() if pos["dir"] < 0)
                if base > 0:
                    strategy.set_broker_margin(base)
                    dk = day.isoformat()
                    margin_by_day[dk] = max(margin_by_day.get(dk, 0.0), base)
            cur += timedelta(minutes=1)

        # Expiry settlement: a leg still open on its own expiry settles to intrinsic (the
        # engine's settler equivalent; SETTLE pays no brokerage). Routed through _fill so
        # the leg lands in positions/cycles like any other close.
        from types import SimpleNamespace

        for sym in [s for s in list(ctx.positions) if s.split("|")[1] == day.isoformat()]:
            spot = market.index_spot(u) or 0.0
            px = _intrinsic(spot, float(sym.split("|")[2]), sym.split("|")[3])
            market.feed(sym, px, 0.0)
            _fill(SimpleNamespace(symbol=sym, quantity=None, reason="expiry_settle",
                                  action=SimpleNamespace(name="SETTLE")),
                  f"{day.isoformat()} 15:30")

        # Daily close: mark any open book at the day's last (forward-filled) closes.
        unreal = 0.0
        for sym, pos in ctx.positions.items():
            try:
                unreal += (market.close(sym) - pos["entry"]) * pos["units"] * pos["dir"]
            except KeyError:
                pass  # never printed today — carry at entry (flat contribution)
        equity_curve.append({"date": day.isoformat(),
                             "equity": round(capital + realized + unreal, 2)})

    options = _options_report(cycles, positions, charges_bd, margin_by_day, premium_by_day,
                              realized)
    report = _to_report(equity_curve, trades, capital, charges_bd["total"], days_with_bars,
                        cycles=cycles, options=options)
    return {"report": report, "trades": trades}


def _options_report(cycles: list[dict], positions: list[dict], charges_bd: dict,
                    margin_by_day: dict, premium_by_day: dict, realized: float) -> dict:
    """The FULL options sub-report ReportView/OptionsReport render (types.OptionsReportData).
    Its mere presence flips ReportView into the options layout, so every non-optional
    summary field must exist (CLAUDE.md footgun: absent-or-complete, never partial)."""
    collected = sum(c["premium_collected"] for c in cycles)
    net = round(realized, 2)
    wins = sum(1 for c in cycles if c["realized_pnl"] > 0)
    margins = sorted(margin_by_day.values())
    max_margin = margins[-1] if margins else 0.0
    exit_reasons: dict[str, dict] = {}
    for c in cycles:
        s = exit_reasons.setdefault(c["exit_reason"] or "other",
                                    {"count": 0, "pnl": 0.0, "wins": 0, "losses": 0})
        s["count"] += 1
        s["pnl"] = round(s["pnl"] + c["realized_pnl"], 2)
        s["wins" if c["realized_pnl"] > 0 else "losses"] += 1
    by_expiry: dict[str, dict] = {}
    for c in cycles:
        e = by_expiry.setdefault(c["expiry"], {"expiry": c["expiry"], "entries": 0,
                                               "premium_collected": 0.0, "realized_pnl": 0.0})
        e["entries"] += 1
        e["premium_collected"] = round(e["premium_collected"] + c["premium_collected"], 2)
        e["realized_pnl"] = round(e["realized_pnl"] + c["realized_pnl"], 2)
    per_expiry = [{**e, "win": e["realized_pnl"] > 0}
                  for e in sorted(by_expiry.values(), key=lambda x: x["expiry"])]
    cum, premium_curve = 0.0, []
    for d in sorted(premium_by_day):
        cum += premium_by_day[d]
        premium_curve.append({"date": d, "premium": round(cum, 2)})
    return {
        "summary": {
            "total_premium_collected": round(collected, 2),
            "total_premium_captured": net,
            "premium_capture_pct": round(100.0 * net / collected, 1) if collected else 0.0,
            "avg_holding_days": round(sum(c["holding_days"] for c in cycles) / len(cycles), 2)
            if cycles else 0.0,
            "num_positions": len(positions),
            "num_cycles": len(cycles),
            "winning_cycles": wins,
            "win_rate_pct": round(100.0 * wins / len(cycles), 1) if cycles else 0.0,
            "max_margin_used": round(max_margin, 2),
            "avg_margin_used": round(sum(margins) / len(margins), 2) if margins else 0.0,
            "capital_efficiency": round(100.0 * net / max_margin, 2) if max_margin else 0.0,
            "avg_premium_per_cycle": round(collected / len(cycles), 2) if cycles else 0.0,
            "total_charges": round(charges_bd["total"], 2),
            "net_after_charges": net,
        },
        "charges": {k: round(v, 2) for k, v in charges_bd.items()},
        "exit_reasons": exit_reasons,
        "per_expiry_cycle": per_expiry,
        "positions": positions,
        "cycles": cycles,
        "margin_series": [{"date": d, "margin": round(m, 2)}
                          for d, m in sorted(margin_by_day.items())],
        "premium_curve": premium_curve,
    }


def run_mtg_backtest(start: date, end: date, capital: float, params: dict | None = None) -> dict:
    """momentum_theta_gainer_intra on the unified page: dispatch to its dedicated BS-premium
    service (real 15-min spot bars, synthetic premiums — long history) and adapt the output
    to the standard run contract. Real-store premiums are a later increment."""
    from dataclasses import fields

    from skas_algo.services.momentum_theta_bt import MtgBtParams, run_backtest

    p = dict(params or {})
    allowed = {f.name for f in fields(MtgBtParams)} - {"start", "end", "capital"}
    kw = {k: v for k, v in p.items() if k in allowed}
    out = run_backtest(MtgBtParams(start=start, end=end, capital=capital, **kw))
    if out.get("error"):
        raise ValueError(out["error"])
    stats = out.get("stats") or {}
    curve = [{"date": pt["date"], "equity": pt["equity"]} for pt in out.get("equity", [])]
    final = curve[-1]["equity"] if curve else capital
    trades: list[dict] = []
    for t in out.get("trades", []):
        trades.append({"date": str(t.get("entry_time", ""))[:16].replace("T", " "),
                       "ticker": t.get("symbol"), "action": "SHORT",
                       "units": t.get("units"), "price": t.get("entry_premium"),
                       "profit": None, "tag": "entry"})
        trades.append({"date": str(t.get("exit_time", ""))[:16].replace("T", " "),
                       "ticker": t.get("symbol"), "action": "COVER",
                       "units": t.get("units"), "price": t.get("exit_premium"),
                       "profit": t.get("pnl"), "tag": t.get("exit_reason")})
    report = {"metrics": {
        "Total Return %": stats.get("return_pct"),
        "Final Equity": round(final, 2),
        "Max Drawdown %": stats.get("max_drawdown_pct"),
        "Total Trades": stats.get("trades"),
        "Win Rate %": stats.get("win_rate"),
        "Net Realized P&L": stats.get("total_pnl"),
        "Cash Balance": round(final, 2),
        "Max Margin Used": stats.get("peak_margin"),
        "Total Taxes": 0.0,
        "Total Withdrawals": 0.0,
    }, "equity_curve": curve}
    return {"report": report, "trades": trades}


def _to_report(curve: list[dict], trades: list[dict], capital: float,
               charges_total: float, days_with_bars: int,
               cycles: list[dict] | None = None, options: dict | None = None) -> dict:
    """The run contract RunsPage/ReportView/Compare render (module docstring). Win Rate and
    Total Trades count CYCLES (options semantics — a straddle's two legs are one trade),
    matching the EOD options reports."""
    final = curve[-1]["equity"] if curve else capital
    total_ret = 100.0 * (final - capital) / capital if capital else 0.0
    peak, max_dd = -1e18, 0.0
    for pt in curve:
        peak = max(peak, pt["equity"])
        if peak > 0:
            max_dd = max(max_dd, 100.0 * (peak - pt["equity"]) / peak)
    if cycles:
        n_trades = len(cycles)
        wins = sum(1 for c in cycles if c["realized_pnl"] > 0)
    else:
        closes = [t for t in trades if t["action"] in ("COVER", "SELL", "SETTLE")]
        n_trades = len(closes)
        wins = sum(1 for t in closes if (t.get("profit") or 0) > 0)
    span_days = max(1, (date.fromisoformat(curve[-1]["date"])
                        - date.fromisoformat(curve[0]["date"])).days) if len(curve) > 1 else 1
    years = span_days / 365.0
    cagr = (100.0 * ((final / capital) ** (1 / years) - 1)
            if capital > 0 and final > 0 and years >= 0.25 else None)
    metrics = {
        "Total Return %": round(total_ret, 2),
        "Final Equity": round(final, 2),
        "Max Drawdown %": round(max_dd, 2),
        "Total Trades": n_trades,
        "Win Rate %": round(100.0 * wins / n_trades, 1) if n_trades else 0.0,
        "Net Realized P&L": round(final - capital, 2),
        "Cash Balance": round(final, 2),
        "Total Charges": round(charges_total, 2),
        "Days Replayed": days_with_bars,
    }
    if options:
        metrics["Max Margin Used"] = options["summary"]["max_margin_used"]
    if cagr is not None:   # a 4-day window annualized is noise — only emit on ≥3 months
        metrics["CAGR %"] = round(cagr, 2)
    report: dict = {"metrics": metrics, "equity_curve": curve}
    if options is not None:
        report["options"] = options
    return report
