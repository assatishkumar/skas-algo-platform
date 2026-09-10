"""The MANUAL RAIL — what manages a run's book after the owner's hand has changed it.

Owner decision, 2026-09-10 ("always hand over"): a manual order that leaves a run holding
positions ENDS algorithmic management of that run. The strategy is paused (kept intact,
never told about legs it did not build) and this takes the book: a stop as a percent of
the margin anchor, an optional target, the paused deck's hard time exit where it had one,
and the engine's own expiry settlement. Nothing else — no rolls, no re-entries, no guesses.

Why not let the strategy "adopt" the book: four of the six option families cannot
represent a hand-edited book at all (the delta family keys its rolls on a ``right`` the
generic rebuild drops; ``custom_options`` keeps a list of strings), and two of them failed
SILENTLY — a swallowed exception every slice, no halt, no stop — on paper and LIVE alike.
A strategy's rule on a book it did not build is a different rule, so the honest thing is
to stop applying it and say so on every screen.

This is never deployed directly (not in the registry): ``LiveSession._hand_over``
installs it and ``load_state`` reinstalls it after a restart. It re-derives its legs from
the PORTFOLIO on every slice, so a second manual edit needs no sync and can never corrupt
it. P&L is measured from the lots' real fills. ``stop_pct == 0`` means NO STOP — never
invented: the platform must not place an order the owner did not ask for on a book they
just took by hand — and the tile/console make that absence unmissable (``strategy_alert``).
"""

from __future__ import annotations

from datetime import date, datetime, time

from skas_algo.engine.types import Signal, SignalAction

from ._options_common import OpenSettleGuard

# Which paused-strategy attribute carries a %-OF-MARGIN stop this rail may inherit. The
# ratio family's `stop_loss_pct` is a fraction of CAPITAL, custom_options' a fraction of
# |net premium| — neither is a margin percent, so they are deliberately NOT here.
_MARGIN_STOP_ATTRS = ("stop_pct",)            # delta family: whole percents of margin_base
_INTRADAY_EXIT_ATTRS = ("exit_time", "eod_exit", "cycle_exit_time")


def _when(iso: str | None) -> str:
    """'2026-09-10T18:00+05:30' → '10 Sep 18:00' for the banner."""
    if not iso:
        return "?"
    try:
        dt = datetime.fromisoformat(str(iso))
        return dt.strftime("%d %b %H:%M")
    except ValueError:
        return str(iso)[:16].replace("T", " ")


def _hhmm(v) -> time | None:
    if v is None or v == "":
        return None
    if isinstance(v, time):
        return v
    try:
        return time.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None


class ManualBookStrategy(OpenSettleGuard):
    strategy_id = "manual_book"
    intraday = True                # tick-driven like every option deck

    def __init__(self, universe: list[str] | None = None, initial_capital: float = 0.0,
                 underlying: str | None = None, stop_pct: float = 0.0,
                 target_pct: float = 0.0, time_exit: str | None = None,
                 margin_anchor: float = 0.0, stop_amt: float = 0.0, target_amt: float = 0.0,
                 paused_strategy_id: str | None = None,
                 handover_at: str | None = None, handover_reason: str | None = None,
                 **_ignored) -> None:
        self.underlying = (underlying or (universe[0] if universe else "NIFTY")).upper()
        self.initial_capital = float(initial_capital or 0.0)
        self.stop_pct = float(stop_pct or 0.0)          # % of the margin anchor, 0 = OFF
        self.target_pct = float(target_pct or 0.0)      # % of the margin anchor, 0 = OFF
        # RUPEE thresholds on the book's MTM (what the console's Alerts card arms): they
        # need no margin anchor and outrank the % rules when both are set.
        self.stop_amt = float(stop_amt or 0.0)          # loss as a positive number, 0 = OFF
        self.target_amt = float(target_amt or 0.0)      # profit, 0 = OFF
        self.time_exit: time | None = _hhmm(time_exit)  # hard square-off, None = hold to expiry
        self.margin_anchor = float(margin_anchor or 0.0)  # manual ₹ anchor, outranks the broker
        self.paused_strategy_id = paused_strategy_id
        self.handover_at = handover_at
        self.handover_reason = handover_reason
        # The anchor the % rules read: the first broker push after handover, frozen (the
        # ratio-family precedent), or the manual figure. "pending" = no % stop can fire yet.
        self.margin_base: float | None = self.margin_anchor if self.margin_anchor > 0 else None
        self.margin_source: str = "manual" if self.margin_anchor > 0 else "pending"
        self.legs: list[dict] = []     # re-derived from the book each slice (display only)
        self.exited_at: str | None = None
        self.exit_reason: str | None = None

    # ------------------------------------------------------------ install
    @classmethod
    def from_paused(cls, paused, *, underlying: str | None, ts: datetime | date,
                    reason: str, initial_capital: float = 0.0) -> ManualBookStrategy:
        """Build the rail beside the strategy it replaces, inheriting only what is
        unambiguous: a %-of-margin stop, an intraday deck's hard exit, a frozen anchor."""
        stop = 0.0
        for attr in _MARGIN_STOP_ATTRS:
            v = getattr(paused, attr, None)
            if isinstance(v, (int, float)) and v > 0:
                stop = float(v)
                break
        exit_t = None
        if getattr(paused, "intraday", False):
            for attr in _INTRADAY_EXIT_ATTRS:
                t = _hhmm(getattr(paused, attr, None))
                if t is not None:
                    exit_t = t.strftime("%H:%M")
                    break
        anchor = 0.0
        base = getattr(paused, "margin_base", None)
        src = getattr(paused, "margin_source", None)
        if isinstance(base, (int, float)) and base > 0 and src in ("broker", "manual"):
            anchor = float(base)
        at = ts.isoformat(timespec="minutes") if isinstance(ts, datetime) else ts.isoformat()
        rail = cls(underlying=underlying or getattr(paused, "underlying", None),
                   initial_capital=initial_capital, stop_pct=stop, time_exit=exit_t,
                   paused_strategy_id=getattr(paused, "strategy_id", None), handover_at=at,
                   handover_reason=reason)
        if anchor > 0:
            # inherited from the strategy's own freeze — a broker figure, not a manual one
            rail.margin_base, rail.margin_source = anchor, "broker"
        return rail

    # ------------------------------------------------------------ hooks
    def set_broker_margin(self, value: float) -> None:
        """First broker push after handover freezes the anchor; a manual anchor outranks."""
        if self.margin_anchor > 0 or self.margin_base is not None:
            return
        if value and value > 0:
            self.margin_base, self.margin_source = float(value), "broker"

    def update(self, **changes) -> list[str]:
        """Hot-edit the rail's own knobs (the tile's Edit params on a handed-over run)."""
        applied = []
        for k, v in changes.items():
            if k in ("stop_pct", "target_pct", "stop_amt", "target_amt"):
                setattr(self, k, max(0.0, float(v or 0.0)))
            elif k == "time_exit":
                self.time_exit = _hhmm(v)
            elif k == "margin_anchor":
                self.margin_anchor = max(0.0, float(v or 0.0))
                if self.margin_anchor > 0:
                    self.margin_base, self.margin_source = self.margin_anchor, "manual"
            else:
                raise ValueError(f"unknown manual-rail param: {k}")
            applied.append(k)
        return applied

    def _thresholds(self) -> tuple[float | None, float | None]:
        """(target ₹, stop ₹ as a positive loss): rupees first, else % of the anchor."""
        tgt = self.target_amt if self.target_amt > 0 else (
            self.margin_base * self.target_pct / 100.0
            if self.margin_base and self.target_pct > 0 else None)
        stp = self.stop_amt if self.stop_amt > 0 else (
            self.margin_base * self.stop_pct / 100.0
            if self.margin_base and self.stop_pct > 0 else None)
        return tgt, stp

    # ------------------------------------------------------------ the slice
    def _book(self, ctx) -> list[dict]:
        out = []
        for symbol in ctx.portfolio.lot_symbols():
            lots = ctx.portfolio.lots(symbol)
            if not lots:
                continue
            units = sum(lot.units for lot in lots)
            cost = sum(lot.units * lot.price for lot in lots)
            out.append({"symbol": symbol, "dir": lots[0].direction, "units": units,
                        "entry": cost / units if units else 0.0,
                        "tag": lots[0].tag if all(lot.tag == lots[0].tag for lot in lots)
                        else "MIXED"})
        return out

    def _pnl(self, legs: list[dict], price_of) -> float | None:
        total = 0.0
        for leg in legs:
            try:
                px = price_of(leg["symbol"])
            except KeyError:
                return None
            if px is None:
                return None
            total += leg["dir"] * (float(px) - leg["entry"]) * leg["units"]
        return total

    def on_slice(self, ctx) -> list[Signal]:
        legs = self._book(ctx)
        self.legs = legs
        if not legs:
            return []
        now = self._now(ctx)
        # A hard time exit is never gated by the opening-window guard (it sits at 15:xx).
        if self.time_exit is not None and now.time() >= self.time_exit:
            return self._exit_all(legs, "rail_time_exit", now)
        if not self._open_settled(now):
            return []
        tgt, stp = self._thresholds()
        if tgt is None and stp is None:
            return []
        pnl = self._pnl(legs, ctx.close)
        if pnl is None:
            return []                                   # a leg without a print: hold
        if stp is not None and pnl <= -stp:
            return self._exit_all(legs, "rail_stop", now)
        if tgt is not None and pnl >= tgt:
            return self._exit_all(legs, "rail_target", now)
        return []

    def _exit_all(self, legs: list[dict], reason: str, now: datetime) -> list[Signal]:
        self.exited_at = now.isoformat(timespec="minutes")
        self.exit_reason = reason
        # shorts first — the same ordering the delta family uses, so a hedge is never the
        # last thing standing while its short waits
        ordered = sorted(legs, key=lambda leg: 0 if leg["dir"] < 0 else 1)
        return [Signal(leg["symbol"], SignalAction.EXIT_ALL, reason=reason) for leg in ordered]

    # ------------------------------------------------------------ surfaces
    # No `strategy_alert`: a target or stop on a manual-mode book is OPTIONAL (owner,
    # 2026-09-10) — the banner says "manual mode", never an alarm about what is unset.

    def strategy_pnl(self, closes: dict) -> float | None:
        return self._pnl(self.legs, closes.get) if self.legs else None

    def exit_amounts(self) -> tuple[float | None, float | None]:
        return self._thresholds()

    def exit_rules(self) -> list[str]:
        anchor = (f"₹{self.margin_base:,.0f} ({self.margin_source} anchor)"
                  if self.margin_base else "the margin anchor (pending)")
        rules = [f"Manual mode — {self.paused_strategy_id or 'the strategy'} paused since "
                 f"{_when(self.handover_at)}; you handle adjustments and exits"]
        if self.stop_amt > 0:
            rules.append(f"Stop out at −₹{self.stop_amt:,.0f} MTM")
        elif self.stop_pct > 0:
            rules.append(f"Stop out at −{self.stop_pct:g}% of {anchor}")
        if self.target_amt > 0:
            rules.append(f"Book profit at +₹{self.target_amt:,.0f} MTM")
        elif self.target_pct > 0:
            rules.append(f"Book profit at +{self.target_pct:g}% of {anchor}")
        if self.stop_pct <= 0 and self.target_pct <= 0 and self.stop_amt <= 0 \
                and self.target_amt <= 0:
            rules.append("No stop or target set (optional — Edit params or the console)")
        if self.time_exit is not None:
            rules.append(f"Square off at {self.time_exit.strftime('%H:%M')}")
        rules.append("Expiry settles to intrinsic (engine)")
        return rules

    def rail_status(self) -> dict:
        return {"stop_pct": self.stop_pct, "target_pct": self.target_pct,
                "stop_amt": self.stop_amt, "target_amt": self.target_amt,
                "time_exit": self.time_exit.strftime("%H:%M") if self.time_exit else None,
                "margin_base": self.margin_base, "margin_source": self.margin_source,
                "no_stop": self.stop_pct <= 0 and self.stop_amt <= 0,
                "paused_strategy_id": self.paused_strategy_id,
                "handover_at": self.handover_at, "handover_label": _when(self.handover_at),
                "handover_reason": self.handover_reason,
                "exited_at": self.exited_at, "exit_reason": self.exit_reason}

    def basket_status(self, market=None, portfolio=None, margin=None) -> dict:
        """The manager calls this with (market, portfolio, margin=…) like the basket
        strategies; the rail needs none of them."""
        return {"phase": "manual", "legs": [dict(leg) for leg in self.legs],
                **self.rail_status()}

    # ------------------------------------------------------------ (de)serialize
    def export_state(self) -> dict:
        return {**self.rail_status(), "margin_anchor": self.margin_anchor,
                "underlying": self.underlying}

    def load_state(self, state: dict) -> None:
        self.stop_pct = float(state.get("stop_pct", self.stop_pct) or 0.0)
        self.target_pct = float(state.get("target_pct", self.target_pct) or 0.0)
        self.stop_amt = float(state.get("stop_amt", 0.0) or 0.0)
        self.target_amt = float(state.get("target_amt", 0.0) or 0.0)
        self.time_exit = _hhmm(state.get("time_exit"))
        self.margin_anchor = float(state.get("margin_anchor", 0.0) or 0.0)
        mb = state.get("margin_base")
        self.margin_base = float(mb) if isinstance(mb, (int, float)) and mb > 0 else None
        self.margin_source = str(state.get("margin_source") or
                                 ("manual" if self.margin_anchor > 0 else "pending"))
        self.paused_strategy_id = state.get("paused_strategy_id", self.paused_strategy_id)
        self.handover_at = state.get("handover_at", self.handover_at)
        self.handover_reason = state.get("handover_reason", self.handover_reason)
        self.exited_at = state.get("exited_at")
        self.exit_reason = state.get("exit_reason")
        if state.get("underlying"):
            self.underlying = str(state["underlying"]).upper()
