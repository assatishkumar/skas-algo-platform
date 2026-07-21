"""Custom multi-leg options "trade" — a user-defined position, not a coded strategy.

The Trade UI lets a user pick legs off the option chain (buy/sell, strike, lots) for one
expiry, name it, set exits, and deploy it. This strategy is the generic executor for that:
it enters the exact legs at the first decision and then manages exits by any combination of

  * **leg-level**   — each leg's own premium target/stop (``leg_targets``/``leg_stops``),
  * **strategy P&L**— combined MTM as a % of the net entry premium (``target_pct``/``stop_pct``),
  * **underlying spot** — exit the whole position when spot crosses a band (``spot_upper``/``spot_lower``).

It is **one-shot**: whatever is open at the chosen expiry is force-settled to intrinsic by the
engine's ExpirySettler; once flat it does not re-enter. Reads premiums/spot via ``ctx.option_chain()``
so backtest / paper / live all run this same code (like the other options strategies).
"""

from __future__ import annotations

from datetime import date

from skas_algo.engine.options.contract_specs import lot_size_for
from skas_algo.engine.options.instrument import make
from skas_algo.engine.types import Signal, SignalAction

from ._options_common import bad_close


class CustomOptionsStrategy:
    strategy_id = "custom_options"
    intraday = True  # decide every tick (the loop already ticks DERIV; explicit for clarity)

    def __init__(
        self,
        universe: list[str] | None = None,
        initial_capital: float = 2_500_000,
        underlying: str | None = None,
        expiry: str | date | None = None,
        legs: list[dict] | None = None,            # [{right, strike, side: buy|sell, lots}]
        spot_upper: float | None = None,           # exit all if underlying spot >= this
        spot_lower: float | None = None,           # exit all if underlying spot <= this
        target_pct: float | None = None,           # book at +x of |net entry premium| (fraction)
        stop_pct: float | None = None,             # stop at -x of |net entry premium| (fraction)
        leg_targets: dict | None = None,           # {leg_index: fraction} per-leg premium target
        leg_stops: dict | None = None,             # {leg_index: fraction} per-leg premium stop
        lot_size: int = 0,                         # explicit contract lot size (req. for stock F&O)
        lot_overrides: dict | None = None,
        **_ignored,
    ):
        self.underlying = (underlying or (universe[0] if universe else "NIFTY")).upper()
        self._expiry_param = expiry
        self.leg_defs = list(legs or [])
        self.spot_upper = spot_upper
        self.spot_lower = spot_lower
        # Surfaced for LiveSession.snapshot()._exit_amounts() → "Target +₹X / Stop −₹Y".
        self.profit_target_pct = target_pct
        self.stop_loss_pct = stop_pct
        self.leg_targets = {int(k): float(v) for k, v in (leg_targets or {}).items()}
        self.leg_stops = {int(k): float(v) for k, v in (leg_stops or {}).items()}
        self.initial_capital = initial_capital
        self.lot_size = int(lot_size or 0)
        self.lot_overrides = lot_overrides

        # State (persisted for live recovery).
        self.entered = False
        self.done = False
        self.legs: list[str] = []                  # entered leg symbols
        self.entry_close: dict[str, float] = {}    # symbol -> entry premium
        self.units: dict[str, float] = {}          # symbol -> contract units
        self.leg_side: dict[str, str] = {}         # symbol -> "buy" | "sell"
        self.leg_index: dict[str, int] = {}        # symbol -> original leg index

    # ------------------------------------------------------------------ slice
    def on_slice(self, ctx) -> list[Signal]:
        if self.done:
            return []
        today = ctx.today()
        if not self.entered:
            return self._enter(ctx, today)
        return self._manage(ctx, ctx.option_chain(), today)

    def _per_lot(self, expiry) -> int:
        if self.lot_size:
            return self.lot_size
        try:
            return lot_size_for(self.underlying, expiry, overrides=self.lot_overrides)
        except KeyError:
            return 0  # stock F&O with no explicit lot size — can't size

    def _enter(self, ctx, today) -> list[Signal]:
        """Enter the user-picked legs. The leg symbol is built directly from
        (underlying, expiry, strike, right) — NOT looked up in the cached chain — and the
        entry premium is read from ``ctx.close`` (live LTP on a live run, else the cached
        mark). This lets a live deployment of ANY listed contract (incl. stock F&O whose
        EOD chain isn't cached) fill at the real price."""
        default_expiry = self._expiry_date()
        resolved: list[tuple[int, str, str, float, float]] = []
        for i, leg in enumerate(self.leg_defs):
            # Per-leg expiry (calendars/diagonals); legs without one use the trade's default.
            expiry = self._leg_expiry(leg, default_expiry)
            if expiry is None:
                return []
            per_lot = self._per_lot(expiry)
            if per_lot <= 0:
                return []
            right = str(leg["right"]).upper()
            side = str(leg["side"]).lower()
            lots = int(leg.get("lots", 1) or 1)
            symbol = make(self.underlying, expiry, float(leg["strike"]), right,
                          lot_size=per_lot, lot_overrides=self.lot_overrides).symbol
            try:
                close = ctx.close(symbol)
            except KeyError:
                return []  # no live/cached price for a leg yet — don't half-enter; retry
            if bad_close(close):
                return []
            units = lots * per_lot
            if units <= 0:
                return []
            resolved.append((i, symbol, side, float(units), float(close)))

        signals: list[Signal] = []
        for i, symbol, side, units, close in resolved:
            action = SignalAction.ENTER_SHORT if side == "sell" else SignalAction.ENTER_LONG
            signals.append(Signal(symbol, action, quantity=int(units), reason="custom_trade",
                                  meta={"multiplier": 1}))
            self.legs.append(symbol)
            self.entry_close[symbol] = close
            self.units[symbol] = units
            self.leg_side[symbol] = side
            self.leg_index[symbol] = i
        self.entered = True
        return signals

    def _manage(self, ctx, chain, today) -> list[Signal]:
        open_legs = self._open_legs(ctx)
        if not open_legs:
            self.done = True  # engine settled/closed everything — one-shot, no re-entry
            return []

        # 1) Per-leg premium target / stop.
        leg_sigs: list[Signal] = []
        for s in open_legs:
            idx = self.leg_index[s]
            tgt, stp = self.leg_targets.get(idx), self.leg_stops.get(idx)
            if tgt is None and stp is None:
                continue
            try:
                cur = ctx.close(s)
            except KeyError:
                continue
            entry = self.entry_close[s]
            if entry <= 0:
                continue
            if self.leg_side[s] == "sell":  # premium decay is profit for a short
                if tgt is not None and cur <= entry * (1 - tgt):
                    leg_sigs.append(Signal(s, SignalAction.EXIT_ALL, reason="leg_target"))
                elif stp is not None and cur >= entry * (1 + stp):
                    leg_sigs.append(Signal(s, SignalAction.EXIT_ALL, reason="leg_stop"))
            else:
                if tgt is not None and cur >= entry * (1 + tgt):
                    leg_sigs.append(Signal(s, SignalAction.EXIT_ALL, reason="leg_target"))
                elif stp is not None and cur <= entry * (1 - stp):
                    leg_sigs.append(Signal(s, SignalAction.EXIT_ALL, reason="leg_stop"))
        if leg_sigs:
            return leg_sigs

        # 2) Combined P&L as a fraction of |net entry premium| over the still-open legs.
        if self.profit_target_pct is not None or self.stop_loss_pct is not None:
            try:
                net_now = self._net_value(open_legs, lambda s: ctx.close(s))
            except KeyError:
                net_now = None
            net_entry = self._net_value(open_legs, lambda s: self.entry_close[s])
            if net_now is not None and abs(net_entry) > 0:
                pnl = net_entry - net_now  # >0 = position in profit
                base = abs(net_entry)
                if self.profit_target_pct is not None and pnl >= self.profit_target_pct * base:
                    return self._exit_all(open_legs, "target")
                if self.stop_loss_pct is not None and pnl <= -self.stop_loss_pct * base:
                    return self._exit_all(open_legs, "stop")

        # 3) Underlying spot bands (prefer the live index spot; fall back to the cached close).
        if self.spot_upper is not None or self.spot_lower is not None:
            spot = self._spot(ctx, chain, today)
            if spot is not None:
                if self.spot_upper is not None and spot >= self.spot_upper:
                    return self._exit_all(open_legs, "spot_upper")
                if self.spot_lower is not None and spot <= self.spot_lower:
                    return self._exit_all(open_legs, "spot_lower")
        return []

    # ------------------------------------------------------------- helpers
    def _open_legs(self, ctx) -> list[str]:
        return [s for s in self.legs if ctx.lots(s)]

    def _spot(self, ctx, chain, today) -> float | None:
        live = getattr(ctx.market, "index_spot", None)
        s = live(self.underlying) if live else None
        return s if s is not None else chain.spot(self.underlying, today)

    def _sign(self, symbol: str) -> float:
        return 1.0 if self.leg_side.get(symbol) == "sell" else -1.0  # +credit / −debit

    def _net_value(self, legs, price_of) -> float:
        return sum(self._sign(s) * price_of(s) * self.units[s] for s in legs)

    def _exit_all(self, legs, reason: str) -> list[Signal]:
        return [Signal(s, SignalAction.EXIT_ALL, reason=reason) for s in legs]

    def _expiry_date(self) -> date | None:
        e = self._expiry_param
        if isinstance(e, date):
            return e
        return date.fromisoformat(str(e)[:10]) if e else None

    def _leg_expiry(self, leg: dict, default: date | None) -> date | None:
        """A leg's own expiry (calendars) or the trade default. Bad/absent value → default."""
        e = leg.get("expiry")
        if not e:
            return default
        try:
            return date.fromisoformat(str(e)[:10])
        except (ValueError, TypeError):
            return default

    def _risk_base(self, ctx=None) -> float:
        """Rupee base the combined target/stop % apply to: |net entry premium| of open legs."""
        legs = self.legs
        if ctx is not None:
            legs = self._open_legs(ctx) or self.legs
        base = abs(self._net_value(legs, lambda s: self.entry_close.get(s, 0.0)))
        return base if base > 0 else self.initial_capital

    # ------------------------------------------------------- (de)serialize
    def export_state(self) -> dict:
        return {
            "entered": self.entered,
            "done": self.done,
            "legs": list(self.legs),
            "entry_close": dict(self.entry_close),
            "units": dict(self.units),
            "leg_side": dict(self.leg_side),
            "leg_index": dict(self.leg_index),
        }

    def load_state(self, state: dict) -> None:
        self.entered = bool(state.get("entered", False))
        self.done = bool(state.get("done", False))
        self.legs = list(state.get("legs", []))
        self.entry_close = {k: float(v) for k, v in state.get("entry_close", {}).items()}
        self.units = {k: float(v) for k, v in state.get("units", {}).items()}
        self.leg_side = dict(state.get("leg_side", {}))
        self.leg_index = {k: int(v) for k, v in state.get("leg_index", {}).items()}
