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
  • Portfolio stop/target: combined MTM (realized flips + open legs, stock + hedge) vs a threshold that is
    % of the live basket margin (portfolio_basis="margin", new screener deploys) or, for legacy runs,
    % of notional (stop) / % of premium (target). Breach → flatten the whole book.
  • Leg target (optional): an individual short leg that has captured leg_target_pct of its OWN premium is
    closed (the opposite leg stays open); booked to realized P&L.
  • Per-name breach → **ROLL flip** (spec §9, option B): a name's live spot crossing a short strike closes
    that name's open legs and sells ONE fresh ATM short on the OPPOSITE side; ``flip_count`` increments and,
    after ``max_flips``, the name is closed for the cycle (no re-entry). ``breach_basis`` = touch (react
    intraday) | close (only at/after EOD). The long NIFTY hedge is never flipped — it tail-protects the book.
"""

from __future__ import annotations

from datetime import date, time

from skas_algo.db.enums import OrderSide
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
        portfolio_sl_pct: float = 2.0,               # % of the basis (combined incl. hedge)
        portfolio_target_enabled: bool = False,
        portfolio_target_pct: float = 50.0,          # % of the basis (see portfolio_basis)
        # Base for the portfolio stop/target. "notional" (legacy): stop = % of aggregate notional,
        # target = % of premium collected — the original behavior, kept so already-running deploys are
        # untouched on recovery. "margin": BOTH stop and target are % of the live basket margin
        # (ctx.position_margin()) — the basis new screener deploys use.
        portfolio_basis: str = "notional",
        # Leg-level profit take: close an individual SHORT leg once it has captured this % of its OWN
        # entry premium (premium decay). The opposite leg of a strangle stays open. Independent of the
        # portfolio target; booked into realized P&L so the portfolio stop/target stay honest.
        leg_target_enabled: bool = False,
        leg_target_pct: float = 80.0,
        breach_basis: str = "close",                 # "close" (EOD) | "touch" (intraday)
        breach_buffer_pct: float = 0.5,              # spot must clear the strike by this % to count as a breach
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
        self.portfolio_basis = portfolio_basis
        self.leg_target_enabled = leg_target_enabled
        self.leg_target_pct = leg_target_pct
        self.breach_basis = breach_basis
        self.breach_buffer_pct = breach_buffer_pct
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
        self.realized_by_name: dict[str, float] = {}  # underlying -> realized P&L booked on its flips
        self.leg_origin: dict[str, str] = {}          # symbol -> "entry" | "flip" (for the live state tag)
        self.last_flip_day: dict[str, str] = {}       # underlying -> ISO date of its last flip (one flip/day cap)

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
    def basket_status(self, market, portfolio, margin: float | None = None) -> dict:
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
            origin = self.leg_origin.get(s, "entry")
            state = ("flip-open" if (origin == "flip" and opened) else "flip-covered" if origin == "flip"
                     else "open" if opened else "covered")
            d["legs"].append({"side": f"SELL {self.leg_right[s]}", "right": self.leg_right[s],
                              "strike": self.leg_strike[s], "units": self.units[s], "entry": entry, "mark": m,
                              "open": opened, "breached": breached and opened, "state": state})
            if opened and m is not None and entry is not None:
                d["mtm"] += (entry - m) * self.units[s]  # short: profit as the premium decays
        for u, d in names.items():
            open_legs = [leg for leg in d["legs"] if leg["open"]]
            has_open = bool(open_legs)
            d["status"] = ("closed" if u in self.closed_names else
                           "flipped" if (d["flip_count"] > 0 and has_open) else
                           "open" if has_open else "settled")
            # The option STRUCTURE from the open legs (independent of open/closed status).
            rights = {leg["right"] for leg in open_legs}
            d["struct"] = ("strangle" if {"CE", "PE"} <= rights else "CE-only" if rights == {"CE"}
                           else "PE-only" if rights == {"PE"} else "closed")
            # Name-level economics (clubbing CE+PE): credit collected, current value, units, realized.
            d["units"] = max((leg["units"] for leg in open_legs), default=0)
            d["credit"] = sum((leg["entry"] or 0) * leg["units"] for leg in open_legs)
            d["value"] = sum((leg["mark"] or 0) * leg["units"] for leg in open_legs)
            d["lot_size"] = self.name_lot.get(u)
            d["lots"] = self.name_lots.get(u)
            d["realized"] = self.realized_by_name.get(u, 0.0)

        nifty_spot = spot_fn("NIFTY") if spot_fn else None
        hedge_legs, hedge_mtm, hedge_cost = [], 0.0, 0.0
        for s in self.legs:
            if self.leg_side.get(s) != "buy" or not is_open(s):
                continue
            m, entry = mark(s), self.entry_close.get(s)
            if m is not None and entry is not None:
                hedge_mtm += (m - entry) * self.units[s]  # long: profit as the option richens
            hedge_cost += (entry or 0) * self.units[s]
            otm = (abs(self.leg_strike[s] - nifty_spot) / nifty_spot * 100) if nifty_spot else None
            hedge_legs.append({"underlying": self.leg_underlying[s], "right": self.leg_right[s],
                               "strike": self.leg_strike[s], "units": self.units[s], "entry": entry,
                               "mark": m, "otm_pct": otm})
        current_notional = 0.0
        for u, d in names.items():
            units = next((leg["units"] for leg in d["legs"] if leg["open"]), 0)
            if d["spot"] and units:
                current_notional += d["spot"] * units

        net_credit = sum(d.get("credit", 0.0) for d in names.values())
        basket_mtm = sum(d.get("mtm", 0.0) for d in names.values())
        combined_mtm = basket_mtm + hedge_mtm
        # Stop/target amounts on the same base the decision uses: % of live basket margin
        # (portfolio_basis="margin"; margin passed in by the LiveRun) or legacy % of notional / premium.
        if self.portfolio_basis == "margin":
            base = float(margin) if (margin and margin > 0) else None
            stop_amount = (self.portfolio_sl_pct / 100.0 * base) if base else None
            target_amount = (self.portfolio_target_pct / 100.0 * base
                             if (self.portfolio_target_enabled and base) else None)
        else:
            stop_amount = self.portfolio_sl_pct / 100.0 * self.agg_notional
            target_amount = (self.portfolio_target_pct / 100.0 * self.premium_collected
                             if self.portfolio_target_enabled else None)
        expiry = self._expiry_date()
        today = getattr(market, "current_date", None) or date.today()
        return {
            "names": sorted(names.values(), key=lambda x: -(x["mtm"] or 0)),
            "hedge": {
                "legs": hedge_legs, "mtm": hedge_mtm, "spot": nifty_spot,
                "lots": self.name_lots.get("NIFTY"), "cost": hedge_cost,
                "cost_pct": (hedge_cost / net_credit * 100.0) if net_credit > 0 else None,
                "entry_notional": self.agg_notional, "current_notional": current_notional,
            },
            "net_credit": net_credit,
            "basket_mtm": basket_mtm,
            "hedge_mtm": hedge_mtm,
            "combined_mtm": combined_mtm,
            "realized_pnl": self.realized_pnl,
            "total_flips": sum(self.flip_count.values()),
            "closed_count": len(self.closed_names),
            "portfolio_stop_amount": stop_amount,
            "portfolio_target_amount": target_amount,
            "buffer_to_stop": (combined_mtm + stop_amount) if stop_amount is not None else None,
            # So the UI labels the stop with its real basis (e.g. "4% margin"), not a hardcoded string.
            "portfolio_sl_pct": self.portfolio_sl_pct,
            "portfolio_target_pct": self.portfolio_target_pct,
            "portfolio_target_enabled": self.portfolio_target_enabled,
            "portfolio_basis": self.portfolio_basis,
            # Entry progress so a fresh deploy shows up immediately with an "entering N/M" metric
            # instead of looking empty while the legs price.
            "entry_progress": {
                "entered": len(self.legs),
                "expected": len(self.leg_defs),
                "done": bool(self.entered),
            },
            "expiry": expiry.isoformat() if expiry else None,
            "dte": (max((expiry - today).days, 0) if expiry else None),
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
        # Batch-fetch every leg's live quote in ONE call before pricing them one-by-one — a 78-leg
        # basket otherwise makes 78 serial quote round-trips at entry. No-op off a cache source.
        prefetch = getattr(ctx.market, "prefetch_quotes", None)
        if prefetch is not None:
            syms: list[str] = []
            for leg in self.leg_defs:
                pl = self._per_lot(str(leg["underlying"]).upper(), expiry, leg)
                if pl > 0:
                    syms.append(make(str(leg["underlying"]).upper(), expiry, float(leg["strike"]),
                                     str(leg["right"]).upper(), lot_size=pl,
                                     lot_overrides=self.lot_overrides).symbol)
            prefetch(syms)
        # Price entries at the ACTUAL fill (SELL@bid / BUY@ask) — the same price the broker books —
        # so the basket MTM / stop / premium agree with the portfolio's unrealized instead of an
        # optimistic last-traded that hides the entry spread. Falls back to LTP off a cache source.
        fill_fn = getattr(ctx.market, "fill_price", None)
        resolved: list[tuple] = []  # (symbol, underlying, right, strike, side, units, close, spot, per_lot, lots, step)
        for leg in self.leg_defs:
            underlying = str(leg["underlying"]).upper()
            right = str(leg["right"]).upper()
            side = str(leg["side"]).lower()
            lots = int(leg.get("lots", 1) or 1)
            per_lot = self._per_lot(underlying, expiry, leg)
            if per_lot <= 0:
                continue  # can't size this leg — skip it (don't block the rest of the basket)
            symbol = make(underlying, expiry, float(leg["strike"]), right,
                          lot_size=per_lot, lot_overrides=self.lot_overrides).symbol
            order_side = OrderSide.SELL if side == "sell" else OrderSide.BUY
            try:
                close = fill_fn(symbol, order_side) if fill_fn is not None else ctx.close(symbol)
            except KeyError:
                close = None  # no quote for this contract (e.g. an illiquid / unlisted strike)
            # Skip a dead/unpriceable leg (price 0 or missing) rather than deferring the WHOLE basket —
            # one illiquid strike must not block the other names. The hedge longs always price, so a
            # genuine pre-open / total-outage tick leaves ``resolved`` empty → we retry next tick.
            units = lots * per_lot
            if bad_close(close) or units <= 0:
                continue
            resolved.append((symbol, underlying, right, float(leg["strike"]), side,
                             float(units), float(close), leg.get("spot"), per_lot, lots, leg.get("strike_step")))
        if not resolved:
            return []  # nothing priceable yet (warmup / outage) — retry the whole basket next tick

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
            self.leg_origin[symbol] = "entry"
            self.name_lot[underlying] = per_lot      # lot size per name (incl. the NIFTY hedge)
            self.name_lots[underlying] = lots
            if side == "sell":  # short stock legs drive notional (once/name) + premium + flip sizing
                premium_collected += close * units
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
        # Leg-level profit takes (close individual decayed legs) run before breach flips; the
        # remaining open legs still flip on a breach this tick.
        legs_closed = self._leg_targets(ctx, open_legs)
        if legs_closed:
            closed = {sig.symbol for sig in legs_closed}
            return legs_closed + self._flips(ctx, [s for s in open_legs if s not in closed])
        return self._flips(ctx, open_legs)

    def _margin_base(self, ctx) -> float | None:
        """Live basket margin (real broker margin live, model estimate in paper) for %-of-margin
        stop/target, or None when no reliable margin is available this tick."""
        fn = getattr(ctx, "position_margin", None)
        m = fn() if fn is not None else None
        return float(m) if (m and m > 0) else None

    def _portfolio_exit(self, ctx, open_legs) -> list[Signal]:
        """Flatten the whole book if the combined MTM (realized flips + open legs) breaches the
        portfolio stop, or the optional target. Thresholds are % of the live basket margin
        (portfolio_basis="margin") or, for legacy runs, % of notional (stop) / % of premium (target)."""
        if self.portfolio_basis == "margin":
            base = self._margin_base(ctx)
            if base is None:
                return []  # no reliable margin this tick → don't act on a bad base
            stop_threshold = self.portfolio_sl_pct / 100.0 * base
            target_threshold = self.portfolio_target_pct / 100.0 * base
        else:  # legacy notional/premium behavior — keeps already-running deploys unchanged
            if self.agg_notional <= 0:
                return []
            stop_threshold = self.portfolio_sl_pct / 100.0 * self.agg_notional
            target_threshold = self.portfolio_target_pct / 100.0 * self.premium_collected
        try:
            net_now = self._net_value(open_legs, lambda s: ctx.close(s))
        except KeyError:
            return []
        net_entry = self._net_value(open_legs, lambda s: self.entry_close[s])
        pnl = self.realized_pnl + (net_entry - net_now)  # realized flips + unrealized on open legs
        if pnl <= -stop_threshold:
            return self._exit_all(open_legs, "portfolio_stop")
        if self.portfolio_target_enabled and target_threshold > 0 and pnl >= target_threshold:
            return self._exit_all(open_legs, "portfolio_target")
        return []

    def _leg_targets(self, ctx, open_legs) -> list[Signal]:
        """Close any open SHORT leg that has captured ``leg_target_pct`` of its OWN entry premium
        (premium decay). The opposite leg stays open; the realized P&L is booked so the portfolio
        stop/target stay honest. No-op unless ``leg_target_enabled``."""
        if not self.leg_target_enabled or self.leg_target_pct <= 0:
            return []
        frac = self.leg_target_pct / 100.0
        signals: list[Signal] = []
        for s in open_legs:
            if self.leg_side.get(s) != "sell":
                continue
            entry = self.entry_close.get(s)
            if not entry or entry <= 0:
                continue
            try:
                mark = ctx.close(s)
            except KeyError:
                continue
            if bad_close(mark):
                continue
            if (entry - mark) / entry >= frac:  # short: captured = premium decayed away
                signals.append(Signal(s, SignalAction.EXIT_ALL, reason="leg_target"))
                contrib = self._sign(s) * (entry - mark) * self.units[s]
                self.realized_pnl += contrib
                name = self.leg_underlying[s]
                self.realized_by_name[name] = self.realized_by_name.get(name, 0.0) + contrib
        return signals

    def _flips(self, ctx, open_legs) -> list[Signal]:
        """Per-name breach → roll: close the name's open legs and sell one fresh ATM short on the
        opposite side; close the name once max_flips is reached."""
        spot_fn = getattr(ctx.market, "index_spot", None)
        if spot_fn is None:
            return []
        if self.breach_basis == "close" and not self._is_eod(ctx):
            return []  # close-basis: only act on a breach at/after EOD
        today = ctx.today()
        today_iso = today.isoformat() if today is not None else ""
        signals: list[Signal] = []
        names = sorted({self.leg_underlying[s] for s in open_legs if self.leg_side[s] == "sell"})
        for name in names:
            if name in self.closed_names:
                continue
            if self.last_flip_day.get(name) == today_iso:
                continue  # one flip per name per trading day — no same-session thrash
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
                contrib = self._sign(s) * (self.entry_close[s] - ctx.close(s)) * self.units[s]
                self.realized_pnl += contrib
                self.realized_by_name[name] = self.realized_by_name.get(name, 0.0) + contrib
            self.flip_count[name] = self.flip_count.get(name, 0) + 1
            self.last_flip_day[name] = today_iso  # cap: at most one flip for this name today
            if will_close:
                self.closed_names.append(name)
                continue
            sym, atm, units, close = new_leg
            signals.append(Signal(sym, SignalAction.ENTER_SHORT, quantity=int(units), reason="flip",
                                  meta={"multiplier": 1}))
            self._record_leg(sym, name, "PE" if breach_side == "CE" else "CE", atm, "sell", units, close)
            self.leg_origin[sym] = "flip"
        return signals

    def _breach_side(self, name: str, open_legs, spot: float) -> str | None:
        """Which of the name's open SHORT legs is breached — spot must clear the strike by
        ``breach_buffer_pct`` (not a marginal touch): 'CE' / 'PE' / None."""
        buf = self.breach_buffer_pct / 100.0
        for s in open_legs:
            if self.leg_side[s] != "sell" or self.leg_underlying[s] != name:
                continue
            k = self.leg_strike[s]
            if self.leg_right[s] == "CE" and spot >= k * (1 + buf):
                return "CE"
            if self.leg_right[s] == "PE" and spot <= k * (1 - buf):
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
        fill_fn = getattr(ctx.market, "fill_price", None)
        try:
            # The rolled short fills at the bid (same as the broker books it), not the LTP.
            close = fill_fn(sym, OrderSide.SELL) if fill_fn is not None else ctx.close(sym)
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
    def sync_to_book(self, portfolio, ts=None) -> None:
        """Reconcile tracked legs with the live book after a manual change (e.g. a name exited):
        drop any leg whose lots are gone so the strategy manages exactly what's held. Keeps
        ``self.legs`` as symbol strings (the basket model) — NOT the custom_options dict model."""
        self.legs = [s for s in self.legs if portfolio.lots(s)]

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
            "realized_by_name": dict(self.realized_by_name),
            "leg_origin": dict(self.leg_origin),
            "last_flip_day": dict(self.last_flip_day),
        }

    def load_state(self, state: dict) -> None:
        self.entered = bool(state.get("entered", False))
        self.done = bool(state.get("done", False))
        # self.legs is a list of symbol STRINGS. Guard against legacy state where a generic
        # book-sync wrote {symbol,...} dicts (the custom_options leg model) — coerce to symbols.
        self.legs = [l["symbol"] if isinstance(l, dict) else l for l in state.get("legs", [])]
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
        self.realized_by_name = {k: float(v) for k, v in state.get("realized_by_name", {}).items()}
        self.leg_origin = dict(state.get("leg_origin", {}))
        self.last_flip_day = dict(state.get("last_flip_day", {}))
