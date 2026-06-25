"""Donchian Strangle Monthly — a basket short-strangle deployed as ONE multi-underlying position.

The screener (services/donchian_strangle.py + the Trade UI) resolves a basket: for each selected
Nifty 50 name, SELL CE at last month's Donchian high and SELL PE at the low (some names single-leg),
plus a long OTM NIFTY CE+PE hedge sized to the aggregate notional. This strategy is the executor:
it enters all those legs at the first decision and then governs the book at the **portfolio** level.

Like ``custom_options`` it is one-shot (survivors settle to intrinsic at expiry) and reads marks via
``ctx.close()`` — but its legs span MANY underlyings, so each carries its own ``underlying`` and the
combined stop/target spans the whole book (stock legs + hedge). It has **no backtest path**; it is only
ever deployed live/paper from the screener.

Governance (every tick — DERIV runs are tick-driven):
  • Portfolio stop: combined MTM (realized flips + open legs, stock + hedge) ≤ −portfolio_sl_pct·agg_notional
    → flatten all. Optional target (default off) ≥ portfolio_target_pct·premium_collected → flatten.
  • Per-name breach → **ROLL flip** (spec §9, option B): a name's live spot crossing a short strike closes
    that name's open legs and sells ONE fresh ATM short on the OPPOSITE side; ``flip_count`` increments and,
    after ``max_flips``, the name is closed for the cycle (no re-entry). ``breach_basis`` = touch (react
    intraday) | close (only at/after EOD). The long NIFTY hedge is never flipped — it tail-protects the book.
"""

from __future__ import annotations

from datetime import date, time

from skas_algo.engine.options import black_scholes as bs
from skas_algo.engine.options.contract_specs import lot_size_for
from skas_algo.engine.options.instrument import make
from skas_algo.engine.types import Signal, SignalAction

from ._options_common import bad_close

_EOD_CUTOFF = time(15, 15)  # breach_basis="close" → only flip a breach at/after this IST time
_R_FREE = 0.065             # risk-free for the 30Δ flip's implied-vol / delta calc


class DonchianStrangleMonthlyStrategy:
    strategy_id = "donchian_strangle_monthly"
    intraday = True  # tick-driven DERIV run — the portfolio stop + breach/flip checks run every refresh

    def __init__(
        self,
        universe: list[str] | None = None,
        initial_capital: float = 2_500_000,
        expiry: str | date | None = None,            # the monthly sell expiry (all legs)
        legs: list[dict] | None = None,              # [{underlying, right, strike, side, lots, spot, lot_size, strike_step}]
        portfolio_sl_pct: float = 2.0,               # of aggregate notional (combined incl. hedge)
        portfolio_target_enabled: bool = False,
        portfolio_target_pct: float = 50.0,          # unit = % of premium collected
        breach_basis: str = "close",                 # "close" (EOD) | "touch" (intraday)
        flip_delta: str = "atm",                     # flip strike: "atm" | "30delta" (LIVE chain)
        max_flips: int = 2,                          # per name, then close it
        lot_overrides: dict | None = None,
        **_ignored,
    ):
        self._expiry_param = expiry
        self.leg_defs = list(legs or [])
        self.portfolio_sl_pct = portfolio_sl_pct
        self.portfolio_target_enabled = portfolio_target_enabled
        self.portfolio_target_pct = portfolio_target_pct
        self.breach_basis = breach_basis
        self.flip_delta = flip_delta
        self.max_flips = max_flips
        self.initial_capital = initial_capital
        self.lot_overrides = lot_overrides

        # State (persisted for live recovery).
        self.entered = False
        self.done = False
        self.legs: list[str] = []                    # entered leg symbols
        self.entry_close: dict[str, float] = {}      # symbol -> entry premium
        self.units: dict[str, float] = {}            # symbol -> contract units
        self.leg_side: dict[str, str] = {}           # symbol -> "buy" | "sell"
        self.leg_underlying: dict[str, str] = {}     # symbol -> underlying
        self.leg_right: dict[str, str] = {}          # symbol -> "CE" | "PE"
        self.leg_strike: dict[str, float] = {}       # symbol -> strike
        self.agg_notional = 0.0                      # Σ spot·lot_size·lots over short stock NAMES (entry)
        self.premium_collected = 0.0                 # Σ entry_premium·units over short legs (entry)
        self.realized_pnl = 0.0                      # booked P&L from flip-closed legs (keeps the stop honest)
        # Per-name sizing/flip state (the hedge underlying is never flipped).
        self.name_lot: dict[str, int] = {}           # underlying -> contract lot size
        self.name_lots: dict[str, int] = {}          # underlying -> lot-sets
        self.name_step: dict[str, float] = {}        # underlying -> strike step (for ATM flip)
        self.flip_count: dict[str, int] = {}         # underlying -> flips so far
        self.closed_names: list[str] = []            # names closed for the cycle (no re-entry)

    # ------------------------------------------------------------------ slice
    def on_slice(self, ctx) -> list[Signal]:
        if self.done:
            return []
        if not self.entered:
            return self._enter(ctx)
        return self._manage(ctx)

    def spot_symbols(self) -> list[str]:
        """Underlyings whose live spot the run loop must feed (breach detection + sizing)."""
        out = {str(leg["underlying"]).upper() for leg in self.leg_defs}
        out.add("NIFTY")  # the hedge underlying
        return sorted(out)

    # ------------------------------------------------------------ live monitoring
    def basket_status(self, market, portfolio) -> dict:
        """Per-name breakdown for the Live page: each name's status / spot-vs-strikes / open legs /
        flip count / unrealized MTM, the hedge legs + entry-vs-current notional drift, and an
        aggregate at-expiry payoff vs a common % move across all underlyings (incl. the NIFTY hedge —
        the correlated-move scenario the hedge protects)."""
        spot_fn = getattr(market, "index_spot", None)

        def mark(s: str):
            try:
                return float(market.close(s))
            except Exception:  # pragma: no cover - no mark yet
                return None

        def is_open(s: str) -> bool:
            return bool(portfolio.lots(s))

        names: dict[str, dict] = {}
        for s in self.legs:
            if self.leg_side.get(s) != "sell":
                continue
            u = self.leg_underlying[s]
            d = names.setdefault(u, {"symbol": u, "spot": (spot_fn(u) if spot_fn else None),
                                     "flip_count": self.flip_count.get(u, 0), "legs": [], "mtm": 0.0})
            m, entry, sp = mark(s), self.entry_close.get(s), d["spot"]
            breached = sp is not None and (
                (self.leg_right[s] == "CE" and sp >= self.leg_strike[s])
                or (self.leg_right[s] == "PE" and sp <= self.leg_strike[s]))
            opened = is_open(s)
            d["legs"].append({"right": self.leg_right[s], "strike": self.leg_strike[s], "units": self.units[s],
                              "entry": entry, "mark": m, "open": opened, "breached": breached and opened})
            if opened and m is not None and entry is not None:
                d["mtm"] += (entry - m) * self.units[s]  # short: profit as the premium decays
        for u, d in names.items():
            open_legs = [leg for leg in d["legs"] if leg["open"]]
            has_open = bool(open_legs)
            d["status"] = ("closed" if u in self.closed_names else
                           "flipped" if (d["flip_count"] > 0 and has_open) else
                           "open" if has_open else "settled")
            # Name-level economics (clubbing CE+PE): credit collected, current value, contract units.
            d["units"] = max((leg["units"] for leg in open_legs), default=0)
            d["credit"] = sum((leg["entry"] or 0) * leg["units"] for leg in open_legs)
            d["value"] = sum((leg["mark"] or 0) * leg["units"] for leg in open_legs)

        hedge_legs, hedge_mtm = [], 0.0
        for s in self.legs:
            if self.leg_side.get(s) != "buy" or not is_open(s):
                continue
            m, entry = mark(s), self.entry_close.get(s)
            if m is not None and entry is not None:
                hedge_mtm += (m - entry) * self.units[s]  # long: profit as the option richens
            hedge_legs.append({"underlying": self.leg_underlying[s], "right": self.leg_right[s],
                               "strike": self.leg_strike[s], "units": self.units[s], "entry": entry, "mark": m})
        current_notional = 0.0
        for u, d in names.items():
            units = next((leg["units"] for leg in d["legs"] if leg["open"]), 0)
            if d["spot"] and units:
                current_notional += d["spot"] * units

        return {
            "names": sorted(names.values(), key=lambda x: -(x["mtm"] or 0)),
            "hedge": {"legs": hedge_legs, "mtm": hedge_mtm, "spot": (spot_fn("NIFTY") if spot_fn else None),
                      "entry_notional": self.agg_notional, "current_notional": current_notional},
            "net_credit": sum(d.get("credit", 0.0) for d in names.values()),
            "realized_pnl": self.realized_pnl,
            "payoff": self._aggregate_payoff(market, portfolio),
        }

    def _aggregate_payoff(self, market, portfolio) -> list[dict]:
        """At-expiry P&L of the whole open book as every underlying (incl. the NIFTY hedge) moves by
        a common percentage — the correlated-move view the index hedge is bought for."""
        spot_fn = getattr(market, "index_spot", None)
        open_legs = [(s, spot_fn(self.leg_underlying[s]) if spot_fn else None) for s in self.legs
                     if portfolio.lots(s)]
        out: list[dict] = []
        for step in range(-15, 16):
            x = step / 100.0
            total = 0.0
            for s, sp in open_legs:
                entry = self.entry_close.get(s)
                if sp is None or entry is None:
                    continue
                shocked = sp * (1 + x)
                k = self.leg_strike[s]
                intrinsic = max(0.0, shocked - k) if self.leg_right[s] == "CE" else max(0.0, k - shocked)
                total += self._sign(s) * (entry - intrinsic) * self.units[s]
            out.append({"move_pct": step, "expiry_pnl": total})
        return out

    # ------------------------------------------------------------------ enter
    def _expiry_date(self) -> date | None:
        e = self._expiry_param
        if isinstance(e, date):
            return e
        return date.fromisoformat(str(e)[:10]) if e else None

    def _per_lot(self, underlying: str, expiry: date, leg: dict) -> int:
        if leg.get("lot_size"):
            return int(leg["lot_size"])
        try:
            return lot_size_for(underlying, expiry, overrides=self.lot_overrides)
        except KeyError:
            return 0

    def _enter(self, ctx) -> list[Signal]:
        """Enter every basket leg in one shot. Symbols are built from (underlying, expiry, strike,
        right) and the entry premium is read from ``ctx.close`` (live LTP on a live run). If ANY leg
        has no price yet, don't half-enter — retry next tick (like custom_options)."""
        expiry = self._expiry_date()
        if expiry is None:
            return []
        resolved: list[tuple] = []  # (symbol, underlying, right, strike, side, units, close, spot, per_lot, lots, step)
        for leg in self.leg_defs:
            underlying = str(leg["underlying"]).upper()
            right = str(leg["right"]).upper()
            side = str(leg["side"]).lower()
            lots = int(leg.get("lots", 1) or 1)
            per_lot = self._per_lot(underlying, expiry, leg)
            if per_lot <= 0:
                return []
            symbol = make(underlying, expiry, float(leg["strike"]), right,
                          lot_size=per_lot, lot_overrides=self.lot_overrides).symbol
            try:
                close = ctx.close(symbol)
            except KeyError:
                return []  # no live/cached price for a leg yet — retry whole basket next tick
            if bad_close(close):
                return []
            units = lots * per_lot
            if units <= 0:
                return []
            resolved.append((symbol, underlying, right, float(leg["strike"]), side,
                             float(units), float(close), leg.get("spot"), per_lot, lots, leg.get("strike_step")))

        signals: list[Signal] = []
        agg_notional = 0.0
        premium_collected = 0.0
        counted: set[str] = set()  # notional is per NAME (a strangle's two legs share one exposure)
        spot_fn = getattr(ctx.market, "index_spot", None)
        for symbol, underlying, right, strike, side, units, close, spot, per_lot, lots, step in resolved:
            action = SignalAction.ENTER_SHORT if side == "sell" else SignalAction.ENTER_LONG
            signals.append(Signal(symbol, action, quantity=int(units), reason="donchian_basket",
                                  meta={"multiplier": 1}))
            self._record_leg(symbol, underlying, right, strike, side, units, close)
            if side == "sell":  # short stock legs drive notional (once/name) + premium + flip sizing
                premium_collected += close * units
                self.name_lot[underlying] = per_lot
                self.name_lots[underlying] = lots
                if step:
                    self.name_step[underlying] = float(step)
                if underlying not in counted:
                    px = spot if spot else (spot_fn(underlying) if spot_fn else None)
                    if px:
                        agg_notional += float(px) * units
                    counted.add(underlying)
        self.agg_notional = agg_notional
        self.premium_collected = premium_collected
        self.entered = True
        return signals

    def _record_leg(self, symbol, underlying, right, strike, side, units, close) -> None:
        if symbol not in self.legs:
            self.legs.append(symbol)
        self.entry_close[symbol] = close
        self.units[symbol] = units
        self.leg_side[symbol] = side
        self.leg_underlying[symbol] = underlying
        self.leg_right[symbol] = right
        self.leg_strike[symbol] = strike

    # ----------------------------------------------------------------- manage
    def _manage(self, ctx) -> list[Signal]:
        open_legs = self._open_legs(ctx)
        if not open_legs:
            self.done = True  # engine settled/closed everything — one-shot, no re-entry
            return []
        stop = self._portfolio_exit(ctx, open_legs)
        if stop:
            return stop
        return self._flips(ctx, open_legs)

    def _portfolio_exit(self, ctx, open_legs) -> list[Signal]:
        """Flatten the whole book if the combined MTM (realized flips + open legs) breaches the
        portfolio stop, or the optional target."""
        if self.agg_notional <= 0:
            return []
        try:
            net_now = self._net_value(open_legs, lambda s: ctx.close(s))
        except KeyError:
            return []
        net_entry = self._net_value(open_legs, lambda s: self.entry_close[s])
        pnl = self.realized_pnl + (net_entry - net_now)  # realized flips + unrealized on open legs
        if pnl <= -(self.portfolio_sl_pct / 100.0 * self.agg_notional):
            return self._exit_all(open_legs, "portfolio_stop")
        if self.portfolio_target_enabled and self.premium_collected > 0:
            if pnl >= self.portfolio_target_pct / 100.0 * self.premium_collected:
                return self._exit_all(open_legs, "portfolio_target")
        return []

    def _flips(self, ctx, open_legs) -> list[Signal]:
        """Per-name breach → roll: close the name's open legs and sell one fresh ATM short on the
        opposite side; close the name once max_flips is reached."""
        spot_fn = getattr(ctx.market, "index_spot", None)
        if spot_fn is None:
            return []
        if self.breach_basis == "close" and not self._is_eod(ctx):
            return []  # close-basis: only act on a breach at/after EOD
        signals: list[Signal] = []
        names = sorted({self.leg_underlying[s] for s in open_legs if self.leg_side[s] == "sell"})
        for name in names:
            if name in self.closed_names:
                continue
            spot = spot_fn(name)
            if spot is None:
                continue
            breach_side = self._breach_side(name, open_legs, spot)
            if breach_side is None:
                continue
            will_close = self.flip_count.get(name, 0) + 1 >= self.max_flips
            new_leg = None
            if not will_close:
                new_leg = self._build_flip_leg(ctx, name, "PE" if breach_side == "CE" else "CE", spot)
                if new_leg is None:
                    continue  # can't price the replacement → leave the name as-is this tick
            # Commit: close the name's open legs (realize their P&L), then add the rolled short.
            for s in [x for x in open_legs if self.leg_underlying[x] == name]:
                signals.append(Signal(s, SignalAction.EXIT_ALL, reason="flip"))
                self.realized_pnl += self._sign(s) * (self.entry_close[s] - ctx.close(s)) * self.units[s]
            self.flip_count[name] = self.flip_count.get(name, 0) + 1
            if will_close:
                self.closed_names.append(name)
                continue
            sym, atm, units, close = new_leg
            signals.append(Signal(sym, SignalAction.ENTER_SHORT, quantity=int(units), reason="flip",
                                  meta={"multiplier": 1}))
            self._record_leg(sym, name, "PE" if breach_side == "CE" else "CE", atm, "sell", units, close)
        return signals

    def _breach_side(self, name: str, open_legs, spot: float) -> str | None:
        """Which of the name's open SHORT legs is breached: 'CE' (spot ≥ a short call) / 'PE' / None."""
        for s in open_legs:
            if self.leg_side[s] != "sell" or self.leg_underlying[s] != name:
                continue
            if self.leg_right[s] == "CE" and spot >= self.leg_strike[s]:
                return "CE"
            if self.leg_right[s] == "PE" and spot <= self.leg_strike[s]:
                return "PE"
        return None

    def _build_flip_leg(self, ctx, name: str, side: str, spot: float):
        """A fresh short for ``name`` on ``side``: (symbol, strike, units, entry_close) or None.
        Strike = ~0.30-delta off the LIVE chain when flip_delta=='30delta' (with live premium), else
        the ATM strike from the listed step."""
        per_lot = self.name_lot.get(name)
        expiry = self._expiry_date()
        if not per_lot or expiry is None:
            return None
        strike = None
        if self.flip_delta == "30delta":
            strike = self._thirty_delta_strike(ctx, name, side, expiry)
        if strike is None:  # ATM fallback (also the flip_delta=='atm' path)
            step = self.name_step.get(name)
            if not step:
                return None
            strike = round(spot / step) * step
        sym = make(name, expiry, float(strike), side, lot_size=per_lot, lot_overrides=self.lot_overrides).symbol
        try:
            close = ctx.close(sym)  # live LTP (via the quote source) for the entry premium
        except KeyError:
            return None
        if bad_close(close):
            return None
        units = per_lot * self.name_lots.get(name, 1)
        if units <= 0:
            return None
        return sym, float(strike), float(units), float(close)

    def _thirty_delta_strike(self, ctx, name: str, side: str, expiry: date) -> float | None:
        """The listed strike whose LIVE Black-Scholes delta is closest to 0.30 on ``side``, using
        the name's live chain (premium → implied vol → delta). None if no live chain is available."""
        chain_fn = getattr(ctx.market, "live_chain", None)
        chain = chain_fn(name, expiry.isoformat()) if chain_fn else None
        if not chain:
            return None
        spot = chain.get("spot")
        rows = chain.get("rows") or []
        today = ctx.today()
        dte = max((expiry - today).days, 0) if today else 0
        t = dte / 365.0
        if not spot or not rows or t <= 0:
            return None
        best, best_gap = None, 1e9
        for r in rows:
            leg = (r.get("ce") if side == "CE" else r.get("pe")) or {}
            prem = leg.get("ltp") or leg.get("close")
            if not prem or prem <= 0:
                continue
            k = float(r["strike"])
            iv = bs.implied_vol(prem, spot, k, t, _R_FREE, side)
            if not iv:
                continue
            gap = abs(abs(bs.delta(spot, k, t, _R_FREE, iv, side)) - 0.30)
            if gap < best_gap:
                best, best_gap = k, gap
        return best

    def _is_eod(self, ctx) -> bool:
        now = ctx.now()
        t = now.time() if hasattr(now, "time") else None
        return t is None or t >= _EOD_CUTOFF

    # ------------------------------------------------------------- helpers
    def _open_legs(self, ctx) -> list[str]:
        return [s for s in self.legs if ctx.lots(s)]

    def _sign(self, symbol: str) -> float:
        return 1.0 if self.leg_side.get(symbol) == "sell" else -1.0  # +credit / −debit

    def _net_value(self, legs, price_of) -> float:
        return sum(self._sign(s) * price_of(s) * self.units[s] for s in legs)

    def _exit_all(self, legs, reason: str) -> list[Signal]:
        return [Signal(s, SignalAction.EXIT_ALL, reason=reason) for s in legs]

    # ------------------------------------------------------- (de)serialize
    def export_state(self) -> dict:
        return {
            "entered": self.entered,
            "done": self.done,
            "legs": list(self.legs),
            "entry_close": dict(self.entry_close),
            "units": dict(self.units),
            "leg_side": dict(self.leg_side),
            "leg_underlying": dict(self.leg_underlying),
            "leg_right": dict(self.leg_right),
            "leg_strike": dict(self.leg_strike),
            "agg_notional": self.agg_notional,
            "premium_collected": self.premium_collected,
            "realized_pnl": self.realized_pnl,
            "name_lot": dict(self.name_lot),
            "name_lots": dict(self.name_lots),
            "name_step": dict(self.name_step),
            "flip_count": dict(self.flip_count),
            "closed_names": list(self.closed_names),
        }

    def load_state(self, state: dict) -> None:
        self.entered = bool(state.get("entered", False))
        self.done = bool(state.get("done", False))
        self.legs = list(state.get("legs", []))
        self.entry_close = {k: float(v) for k, v in state.get("entry_close", {}).items()}
        self.units = {k: float(v) for k, v in state.get("units", {}).items()}
        self.leg_side = dict(state.get("leg_side", {}))
        self.leg_underlying = dict(state.get("leg_underlying", {}))
        self.leg_right = dict(state.get("leg_right", {}))
        self.leg_strike = {k: float(v) for k, v in state.get("leg_strike", {}).items()}
        self.agg_notional = float(state.get("agg_notional", 0.0))
        self.premium_collected = float(state.get("premium_collected", 0.0))
        self.realized_pnl = float(state.get("realized_pnl", 0.0))
        self.name_lot = {k: int(v) for k, v in state.get("name_lot", {}).items()}
        self.name_lots = {k: int(v) for k, v in state.get("name_lots", {}).items()}
        self.name_step = {k: float(v) for k, v in state.get("name_step", {}).items()}
        self.flip_count = {k: int(v) for k, v in state.get("flip_count", {}).items()}
        self.closed_names = list(state.get("closed_names", []))
