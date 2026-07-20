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

import numpy as np
import pandas as pd

from skas_algo.data.option_intraday_store import captured_days, load_contract_bars, load_day
from skas_algo.engine.options.charges import charges_for_txn
from skas_algo.engine.options.contract_specs import lot_size_for, strike_allowed
from skas_algo.engine.options.margin import MarginParams, short_option_margin
from skas_algo.strategies.registry import get_strategy

logger = logging.getLogger(__name__)

_OPEN = time(9, 15)
_CLOSE = time(15, 30)

# Strategies the harness can replay. momentum_theta_gainer_intra is handled at the route
# layer by its dedicated BS service — not here. The positional family (ratio/hni/ema21)
# joined 2026-07-18 via the _Chain cached-chain adapter: ALL index-options strategies now
# backtest on the 1-min store (the EOD options basis left the UI; equity keeps the cache).
REPLAYABLE = {"intraday_straddle", "weekly_intraday_straddle", "call_put_ratio_expiry",
              "delta_neutral_monthly", "iron_fly_monthly", "call_ratio_monthly",
              "put_ratio_monthly", "batman_ratio_monthly", "hni_weekly", "21_ema_momentum"}


class _Market:
    """ctx.market for the replay: per-day forward-filled marks + store-built chains."""

    def __init__(self, underlying: str, lot_overrides: dict | None = None,
                 allow_fifty_strikes: bool = False):
        self.underlying = underlying
        self.lot_overrides = lot_overrides   # params["contract_specs"] — parity w/ engine
        # BACKTEST-ONLY escape hatch: lift the NIFTY 100-multiples rule so a replay can
        # mirror pre-2026-07-14 live history (which legitimately traded 50s) or probe
        # 50-strike variants. A harness param — strategies and LIVE paths never see it.
        self.allow_fifty_strikes = allow_fifty_strikes
        self.quotes: dict[str, tuple[float, float]] = {}   # symbol -> (close, oi)
        # expiry_iso -> strike -> {"CE": sym, "PE": sym}; rebuilt per day from stored symbols.
        self.chains: dict[str, dict[float, dict[str, str]]] = {}
        self.current_date: date | None = None
        self.now: datetime | None = None
        # index_spot memo: quotes are stable between mutations, but index_spot is called
        # several times per minute (sizing, note_spot, margin push, strategy internals),
        # each re-scanning every strike via _parity. Cache it; any quote write dirties it.
        self._spot_dirty = True
        self._spot_cache: float | None = None

    def start_day(self, day: date, symbols: list[str]) -> None:
        self.current_date = day
        self.quotes = {}          # live marks don't survive overnight — neither do these
        self._spot_dirty = True
        self.chains = {}
        # Today's FORMING index bar (running O/H/L off the parity spot) — feeds ema21's
        # bands with chart-at-decision-time semantics instead of the settled bar.
        self.spot_open: float | None = None
        self.spot_high: float | None = None
        self.spot_low: float | None = None
        for sym in symbols:
            _u, e, strike_s, right = sym.split("|")
            self.chains.setdefault(e, {}).setdefault(float(strike_s), {})[right] = sym

    def note_spot(self) -> float | None:
        """Sample the current parity spot into today's forming bar (called per minute
        only when a strategy consumes daily bars — the parity scan isn't free)."""
        s = self.index_spot(self.underlying)
        if s is None:
            return None
        if self.spot_open is None:
            self.spot_open = s
        self.spot_high = s if self.spot_high is None else max(self.spot_high, s)
        self.spot_low = s if self.spot_low is None else min(self.spot_low, s)
        return s

    def feed(self, symbol: str, close: float, oi: float) -> None:
        self.quotes[symbol] = (float(close), float(oi))
        self._spot_dirty = True

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

    # Put-call parity yields the FUTURES-implied level (cash + cost-of-carry). The
    # strategies compare "spot" against CASH-index strikes, and the ~20-pt carry bias
    # flipped the 2026-07-16 ATM pick to 24200 while live (real index spot) picked 24100
    # — a 100-pt strike miss that decided the day. Discount at the same flat r the
    # strategies price with; dividends ignored (residual bias is a few points, not ~20).
    _CARRY_R = 0.065

    def _decarry(self, f: float, expiry_iso: str) -> float:
        try:
            t_days = (date.fromisoformat(str(expiry_iso)[:10]) - self.current_date).days
        except (TypeError, ValueError):  # pragma: no cover - malformed expiry → leave as-is
            return f
        if t_days <= 0:
            return f  # expiry day: F ≈ S (also keeps settlement intrinsic exact)
        return f / (1.0 + self._CARRY_R * t_days / 365.0)

    def index_spot(self, _u: str) -> float | None:
        if not self._spot_dirty:
            return self._spot_cache
        val = None
        for e in sorted(self.chains):   # nearest stored expiry that has a parity pair
            spot = self._parity(e)
            if spot is not None:
                val = self._decarry(spot, e)
                break
        self._spot_cache = val
        self._spot_dirty = False
        return val

    def live_chain(self, _u: str, expiry_iso: str) -> dict | None:
        strikes = self.chains.get(str(expiry_iso)[:10])
        if not strikes:
            return None
        rows = []
        for k in sorted(strikes):
            if not self.allow_fifty_strikes and not strike_allowed(self.underlying, k):
                continue  # same NIFTY-100 coarsening the LIVE chain applies

            def info(sym: str | None) -> dict | None:
                q = self.quotes.get(sym) if sym else None
                return None if q is None else {"ltp": q[0], "oi": int(q[1])}

            legs = strikes[k]
            rows.append({"strike": k, "ce": info(legs.get("CE")), "pe": info(legs.get("PE"))})
        own = self._parity(str(expiry_iso)[:10])
        spot = (self._decarry(own, expiry_iso) if own is not None
                else self.index_spot(self.underlying))
        if not rows or spot is None:
            return None
        atm = min((r["strike"] for r in rows), key=lambda s: abs(s - spot))
        try:
            lot = lot_size_for(self.underlying, date.fromisoformat(str(expiry_iso)[:10]),
                               overrides=self.lot_overrides)
        except KeyError:
            lot = 0
        return {"spot": spot, "atm_strike": atm, "lot_size": lot, "rows": rows}


class _ChainRow:
    """OptionChainView-row lookalike built from the store's forward-filled marks. The
    ratio family + ema21 read exactly {strike,right,close,oi,symbol}; ``symbol`` is the
    STORE symbol ("U|iso|strike|right") — LOAD-BEARING: it flows straight into Signal
    and the replay's _fill splits it on "|" (the engine's instrument symbols would not
    round-trip)."""

    __slots__ = ("underlying", "expiry", "strike", "right", "close", "settle", "oi", "symbol")

    def __init__(self, underlying, expiry, strike, right, close, oi, symbol):
        self.underlying = underlying
        self.expiry = expiry
        self.strike = strike
        self.right = right
        self.close = close
        self.settle = None
        self.oi = oi
        self.symbol = symbol


class _Chain:
    """ctx.option_chain() — the cached-chain interface (OptionChainView subset) the
    positional strategies consume, served from the store's minute marks. Only contracts
    that have PRINTED so far today appear (the store is sparse — untraded far wings are
    simply absent, and the strategies' own oi>0/_bad guards skip them, exactly as they
    skip a phantom bhavcopy strike)."""

    def __init__(self, market: "_Market | None" = None):
        self.market = market
        self.days: list[date] = []

    def expiries(self, _u: str, _today: date) -> list[date]:
        return list(self.days)

    def spot(self, u: str, _today: date) -> float | None:
        return self.market.index_spot(u) if self.market is not None else None

    def expiry_for_dte(self, _u: str, today: date, dte_target: int) -> date | None:
        """Expiry nearest ``dte_target`` days out (ties → sooner) — mirrors
        engine/options/chain.py so hni picks the same weekly here as in live."""
        future = [e for e in self.days if e >= today]
        if not future:
            return None
        return min(future, key=lambda e: (abs((e - today).days - dte_target), (e - today).days))

    def chain(self, u: str, _today: date, expiry: date) -> list["_ChainRow"]:
        if self.market is None:
            return []
        e_iso = expiry.isoformat() if hasattr(expiry, "isoformat") else str(expiry)[:10]
        rows: list[_ChainRow] = []
        for strike, legs in sorted((self.market.chains.get(e_iso) or {}).items()):
            if not self.market.allow_fifty_strikes and not strike_allowed(u, strike):
                continue   # same NIFTY-100 coarsening as live_chain and the EOD view
            for right, sym in legs.items():
                q = self.market.quotes.get(sym)
                if q is None:
                    continue   # never printed today — absent, like an untraded strike
                rows.append(_ChainRow(u, expiry, strike, right, q[0], int(q[1]), sym))
        return rows


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


# SHORT-leg lot-multiples per lot-set of each strategy's structure — the margin push sums
# over short legs, so a user-keyed "margin per lot-set" must be spread across them
# (straddle: CE+PE short = 2; cpre: 3 short lots per side x 2 sides = 6; the fly/strangle
# families keep 2 shorts through their adjustments; ratio wings sell 2 [x2 wings for
# batman], hni's 1-3-2 body sells 3; ema21's credit spread sells 1). FALLBACK ONLY —
# the run function prefers the instance's own sell_lots when it has one.
_SHORT_UNITS_PER_SET = {"intraday_straddle": 2, "weekly_intraday_straddle": 2,
                        "delta_neutral_monthly": 2, "iron_fly_monthly": 2,
                        "call_put_ratio_expiry": 6, "call_ratio_monthly": 2,
                        "put_ratio_monthly": 2, "batman_ratio_monthly": 4,
                        "hni_weekly": 3, "21_ema_momentum": 1}


def _daily_bars_with_forming(u: str, market: "_Market"):
    """ema21's ``set_daily_bars_fn``: cache bars STRICTLY BEFORE the replay's current day
    (settled history — the same daily bars live reads from the broker), plus TODAY as the
    forming bar built from the replay's running parity spot. No settled-bar lookahead."""
    from skas_algo.data.options_provider import INDEX_SYMBOL
    from skas_algo.data.provider import get_data_cache

    sd = get_data_cache()
    sym = INDEX_SYMBOL.get(u.upper()) or u.upper()

    def fn(_u: str, start, end):
        today = market.current_date
        hist = sd.get_prices(symbol=sym, start_date=start, end_date=end)
        if today is not None and hist is not None and not hist.empty:
            hist = hist[pd.to_datetime(hist["date"]).dt.date < today]
        spot = market.note_spot()   # ensure the forming bar includes THIS minute
        if today is not None and spot is not None and (end is None or today <= end):
            forming = pd.DataFrame([{"date": pd.Timestamp(today),
                                     "open": market.spot_open, "high": market.spot_high,
                                     "low": market.spot_low, "close": spot}])
            hist = pd.concat([hist, forming], ignore_index=True) if hist is not None else forming
        return hist

    return fn


def _nearest_expiry_lot(u: str, day: date, expiries: list[date],
                        lot_overrides: dict | None) -> int:
    """Lot size of the CONTRACT actually traded that day (lot revisions bind to new
    contracts, so the era key is the nearest EXPIRY, not the trade date — sizing off the
    trade date under-/over-counts units across a revision boundary)."""
    exp = min((e for e in expiries if e >= day), default=(expiries[0] if expiries else day))
    return lot_size_for(u, exp, overrides=lot_overrides)


def _ref_spot(u: str, days_pool: list[date],
              lot_overrides: dict | None) -> tuple[float, date, int] | None:
    """(parity spot, day, nearest-expiry lot) near the open of the LATEST store day with
    ``u`` bars — the reference notional for the user-keyed margin ("TODAY'S broker margin
    per lot"). Walks back a few days so a data-hole tail can't void the run."""
    for day in list(reversed(days_pool))[:10]:
        df = load_day(day, underlying=u, columns=["symbol", "start", "close", "oi"])
        if df.empty:
            continue
        m = _Market(u)
        m.start_day(day, list(df["symbol"].unique()))
        expiries = sorted({date.fromisoformat(s.split("|")[1])
                           for s in df["symbol"].unique()})
        minutes = np.datetime_as_string(
            pd.to_datetime(df["start"]).values.astype("datetime64[m]"))
        by_min: dict[str, list] = {}
        for sym, minute, close_px, oi in zip(df["symbol"].to_numpy(), minutes,
                                             df["close"].to_numpy(), df["oi"].to_numpy(),
                                             strict=True):
            by_min.setdefault(minute, []).append((sym, float(close_px), float(oi)))
        for mk in sorted(by_min):
            for sym, c, oi in by_min[mk]:
                m.feed(sym, c, oi)
            spot = m.index_spot(u)
            if spot:
                return float(spot), day, _nearest_expiry_lot(u, day, expiries, lot_overrides)
    return None


def run_intraday_backtest(strategy_id: str, underlying: str, start: date, end: date,
                          capital: float, params: dict | None = None,
                          progress=None) -> dict:
    """Replay ``strategy_id`` over the store days in [start, end]. Returns
    {"report", "trades"} in the standard run contract (see module docstring).
    ``progress(done, total, day_iso)`` is called at the top of each day (job UI)."""
    if strategy_id not in REPLAYABLE:
        raise ValueError(f"{strategy_id} is not intraday-replayable (supported: "
                         f"{sorted(REPLAYABLE)})")
    p = dict(params or {})
    mp = MarginParams.from_dict(p.pop("margin", None))
    u = underlying.upper()
    all_days = [date.fromisoformat(d) for d in captured_days()]
    days = [d for d in all_days if start <= d <= end]
    if not days:
        raise ValueError("the option store has no captured days in this window — "
                         "see Data → Options for coverage")

    # ---- owner-keyed margin + capital sizing (2026-07-17; defaults preserve old behavior)
    # margin_per_lot = TODAY'S broker margin for one lot-set of THIS strategy's structure
    # (straddle pair ~Rs2L, spread ~Rs50k). Converted to a % of notional against the
    # LATEST store day and applied era-true: 2021 margins shrink with 2021 spots and lot
    # sizes automatically. 0 = keep the (span+exposure)% model margin.
    margin_per_lot = float(p.pop("margin_per_lot", 0) or 0)
    sizing = str(p.pop("sizing", "fixed") or "fixed")
    buffer_pct = float(p.pop("sizing_buffer_pct", 10) or 0)
    allow_fifty = bool(p.pop("allow_fifty_strikes", False))
    lot_overrides = p.get("contract_specs")   # same override surface as the engine paths

    # Strategy FIRST: the margin math below reads the instance's real leg ratios.
    # NOTE the "sizing" name collision resolves in our favor by construction: the harness
    # popped it above, so a ratio-family strategy is ALWAYS built with its own ctor
    # default sizing="fixed" — its internal auto-size can never fight the harness's
    # external lots refit (pinned by test).
    factory = get_strategy(strategy_id)
    if strategy_id == "call_put_ratio_expiry":
        p.setdefault("underlyings", [u])   # cpre ignores ``universe`` — takes underlyings
    strategy = factory(universe=[u], initial_capital=capital, **p)
    market = _Market(u, lot_overrides=lot_overrides, allow_fifty_strikes=allow_fifty)
    chain = _Chain(market)
    ctx = _Ctx(market, chain)
    if hasattr(strategy, "set_option_bars_fn"):
        strategy.set_option_bars_fn(_store_bars_fn)
    # ema21's EMA channel needs daily INDEX bars: prior days from the cache (settled —
    # matches live's broker daily history), TODAY as a FORMING bar from the replay's
    # running parity spot (chart-at-decision-time semantics; the owner vetoed the
    # settled-bar's ~10-min lookahead, 2026-07-18).
    track_spot = hasattr(strategy, "set_daily_bars_fn")
    if track_spot:
        strategy.set_daily_bars_fn(_daily_bars_with_forming(u, market))

    # Short lot-multiples per lot-set: prefer the instance's own leg ratios (the form can
    # change sell_lots) over the static table — a mismatch would mis-scale margin_pct vs
    # what the per-minute push sums over short legs.
    short_per_set = _SHORT_UNITS_PER_SET.get(strategy_id, 2)
    sell_lots = getattr(strategy, "sell_lots", None)
    if sell_lots:
        wings = 2 if strategy_id == "batman_ratio_monthly" else 1
        short_per_set = int(sell_lots) * wings

    margin_pct: float | None = None
    sizing_echo: dict | None = None
    if margin_per_lot > 0:
        ref = _ref_spot(u, all_days, lot_overrides)
        if ref is None:
            raise ValueError(f"cannot derive a reference spot for {u} from the store — "
                             "margin_per_lot needs at least one stored day with bars")
        ref_spot, ref_day, ref_lot = ref
        # Spread the lot-SET margin across the structure's short legs: the per-minute push
        # sums short_option_margin over shorts, so one lot-set pushes exactly
        # margin_per_lot x (spot/ref_spot) x (lot_size/ref_lot).
        margin_pct = margin_per_lot / (ref_spot * ref_lot * short_per_set)
        mp = MarginParams(span_pct=margin_pct, exposure_pct=0.0)
        sizing_echo = {"margin_per_lot": margin_per_lot, "margin_pct": round(margin_pct, 6),
                       "ref_spot": round(ref_spot, 2), "ref_day": ref_day.isoformat(),
                       "ref_lot_size": ref_lot, "sizing": sizing,
                       "sizing_buffer_pct": buffer_pct}
    if sizing == "capital" and margin_pct is None:
        raise ValueError("capital-based sizing needs margin_per_lot — key in today's "
                         "broker margin for one lot-set of this strategy")

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
                episode = {"entry_minute": minute, "closed": [], "premium": 0.0,
                           # minute-accurate parity spot — the cycles table's
                           # Spot(entry→exit) and the payoff chart's entry marker
                           "entry_spot": market.index_spot(u)}
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
                per_lot = lot_size_for(_u, date.fromisoformat(e_iso), overrides=lot_overrides)
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
                    exit_spot = market.index_spot(u)
                    e_spot = episode.get("entry_spot")
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
                        "exit_date": minute,
                        "daily_pnl": episode.get("daily", []),
                        "underlying_entry": e_spot,
                        "underlying_exit": exit_spot,
                        "underlying_pct": (100.0 * (exit_spot - e_spot) / e_spot
                                           if e_spot and exit_spot else None),
                        "exit_reason": sig.reason or "",
                        "ce": ce if len(legs) == 2 else None,
                        "pe": pe if len(legs) == 2 else None,
                    })
                    episode = None

    sizing_skipped_days = 0
    for day_i, day in enumerate(days):
        if progress is not None:
            progress(day_i, len(days), day.isoformat())
        df = load_day(day, underlying=u, columns=["symbol", "start", "close", "oi"])
        if df.empty:
            continue
        days_with_bars += 1
        market.start_day(day, list(df["symbol"].unique()))
        chain.days = sorted({date.fromisoformat(s.split("|")[1])
                             for s in df["symbol"].unique()})
        # Single-pass sweep: sort the day's prints by minute, then a pointer advances with
        # the clock so each print is TOUCHED ONCE. The old design bucketed every print into
        # a per-minute dict up front and re-walked them in the loop — 2× the work over ~10M
        # prints/quarter (63% of runtime was this Python churn, profiled 2026-07-20).
        # numpy datetime64[m] → "YYYY-MM-DDTHH:MM" via C code (pandas .dt.strftime was ~45%
        # of runtime on 300k-row days); Python lists index far faster than numpy scalars.
        mins_arr = pd.to_datetime(df["start"]).values.astype("datetime64[m]")
        order = np.argsort(mins_arr, kind="stable")   # within-minute order preserved
        min_strs = np.datetime_as_string(mins_arr[order]).tolist()
        p_syms = df["symbol"].to_numpy()[order].tolist()
        p_close = df["close"].to_numpy()[order].astype(float).tolist()
        p_oi = df["oi"].to_numpy()[order].astype(float).tolist()
        n_prints = len(p_syms)
        pi = 0

        # Capital-based sizing: refit lots to CURRENT equity on FLAT days only (an open
        # multi-day book is never resized). Sized at the first minute with a parity spot;
        # equity < one buffered lot-set => the day's entries are skipped, never 0-unit
        # orders. Strategies read self.lots/self.sets fresh at entry, so this takes
        # effect the same day.
        day_sized = sizing != "capital" or bool(ctx.positions)
        day_blocked = False

        cur = datetime.combine(day, _OPEN)
        end_dt = datetime.combine(day, _CLOSE)
        while cur <= end_dt:
            minute_key = cur.strftime("%Y-%m-%dT%H:%M")
            while pi < n_prints and min_strs[pi] < minute_key:
                pi += 1            # strays before the open (the old dict never visited them)
            if pi < n_prints and min_strs[pi] == minute_key:
                q = market.quotes
                while pi < n_prints and min_strs[pi] == minute_key:
                    q[p_syms[pi]] = (p_close[pi], p_oi[pi])   # == market.feed, no dispatch
                    pi += 1
                market._spot_dirty = True  # invalidate the per-minute parity cache
            ctx._now = cur
            market.now = cur
            if track_spot:
                market.note_spot()   # today's forming index bar (ema21's bands)
            if not day_sized:
                spot = market.index_spot(u)
                if spot:
                    day_sized = True
                    lot_t = _nearest_expiry_lot(u, day, chain.days, lot_overrides)
                    mpl_t = margin_pct * spot * lot_t * short_per_set  # era-true Rs/lot-set
                    equity = equity_curve[-1]["equity"] if equity_curve else capital
                    n = int(equity // (mpl_t * (1 + buffer_pct / 100.0))) if mpl_t > 0 else 0
                    if n < 1:
                        day_blocked = True
                        sizing_skipped_days += 1
                    elif strategy_id == "call_put_ratio_expiry":
                        strategy.sets = {k: n for k in strategy.sets}
                    elif hasattr(strategy, "lots"):
                        strategy.lots = n
            if day_blocked and not ctx.positions:
                cur += timedelta(minutes=1)
                continue  # equity can't fund one lot-set — no entries today
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
        if episode is not None:
            # The open cycle's MTM at this close (owner ask: per-cycle EOD P&L) —
            # legs already closed within the episode + the open book's unreal.
            closed_pnl = sum(x["pnl"] for x in episode["closed"])
            episode.setdefault("daily", []).append(
                {"date": day.isoformat(), "pnl": round(closed_pnl + unreal, 2)})
        equity_curve.append({"date": day.isoformat(),
                             "equity": round(capital + realized + unreal, 2)})

    options = _options_report(cycles, positions, charges_bd, margin_by_day, premium_by_day,
                              realized)
    # Spot/VIX context for the cycles table + payoff markers. enrich_with_market fills
    # DATE-based spots (cache daily closes) + India VIX for cycles AND positions — then
    # the cycles' spots are overwritten with the replay's own MINUTE-accurate parity
    # values captured at the fills (recorded above; enrich would clobber them).
    minute_spots = {id(c): (c.get("underlying_entry"), c.get("underlying_exit"),
                            c.get("underlying_pct")) for c in cycles}
    try:
        from skas_algo.data.options_provider import enrich_with_market
        from skas_algo.data.provider import get_data_cache

        enrich_with_market(get_data_cache(), options, u)
    except Exception:  # pragma: no cover - display-only enrichment must never kill a run
        logger.exception("spot/VIX enrichment failed (display-only)")
    for c in cycles:
        ue, ux, upct = minute_spots.get(id(c), (None, None, None))
        if ue is not None:
            c["underlying_entry"], c["underlying_exit"], c["underlying_pct"] = ue, ux, upct
    report = _to_report(equity_curve, trades, capital, charges_bd["total"], days_with_bars,
                        cycles=cycles, options=options)
    if sizing_echo is not None:
        # Additive key (ReportView ignores unknowns): records the margin actually used —
        # keyed rupees, derived notional %, reference day, and days skipped for equity.
        sizing_echo["sizing_skipped_days"] = sizing_skipped_days
        report["sizing"] = sizing_echo
    if progress is not None:
        progress(len(days), len(days), days[-1].isoformat())
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
    yearly, monthly_profit, monthly_equity = _periodic_breakdowns(curve, capital)
    if yearly:
        # Same contract keys the EOD engine emits — ReportView's existing Yearly table +
        # Monthly P&L grid light up with no frontend change (owner ask, 2026-07-17).
        report["yearly"] = yearly
        report["monthly_profit"] = monthly_profit
        report["monthly_equity"] = monthly_equity
    if options is not None:
        report["options"] = options
    return report


def _periodic_breakdowns(curve: list[dict], capital: float):
    """yearly / monthly_profit / monthly_equity derived from the daily equity curve.
    Monthly P&L = month-end equity − previous period-end equity (chained from the initial
    capital); per-year Max DD resets its high-water mark at the year boundary (matching
    the EOD report's YearlyTable semantics). Taxes are 0 — the replay charges F&O costs
    per fill instead."""
    if not curve:
        return {}, {}, {}
    monthly_profit: dict[str, dict[str, int | float]] = {}
    monthly_equity: dict[str, dict[str, int | float]] = {}
    yearly: dict[str, dict] = {}
    last_by_month: dict[tuple[str, str], float] = {}
    order: list[tuple[str, str]] = []
    for pt in curve:
        y, mo = pt["date"][:4], str(int(pt["date"][5:7]))
        if (y, mo) not in last_by_month:
            order.append((y, mo))
        last_by_month[(y, mo)] = pt["equity"]
    prev = capital
    for y, mo in order:
        eq = last_by_month[(y, mo)]
        monthly_profit.setdefault(y, {})[mo] = round(eq - prev, 2)
        monthly_equity.setdefault(y, {})[mo] = round(eq, 2)
        prev = eq
    year_start = capital
    for y in sorted({y for y, _ in order}):
        pts = [pt for pt in curve if pt["date"][:4] == y]
        eoy = pts[-1]["equity"]
        peak, dd = -1e18, 0.0   # high-water mark resets each calendar year
        for pt in pts:
            peak = max(peak, pt["equity"])
            if peak > 0:
                dd = max(dd, 100.0 * (peak - pt["equity"]) / peak)
        yearly[y] = {
            "Return (Abs)": round(eoy - year_start, 2),
            "Return (%)": round(100.0 * (eoy - year_start) / year_start, 2) if year_start else 0.0,
            "Portfolio Value": round(eoy, 2),
            "Taxes": 0.0,
            "Max Drawdown (%)": round(dd, 2),
        }
        year_start = eoy
    return yearly, monthly_profit, monthly_equity
