"""Short-premium options strategy: short straddle / strangle on an index underlying.

Sells an ATM straddle (or an OTM strangle) near a target days-to-expiry, then manages
the combined position by premium decay (profit target) or adverse move (stop loss);
anything still open at expiry is force-settled to intrinsic by the engine's
ExpirySettler. Reads the option chain via ``ctx.option_chain()`` (real bhavcopy
premiums), so backtest/forward-test/live all run this same code.

Multi-leg entry is just two ``ENTER_SHORT`` signals; exits are ``EXIT_ALL`` per leg
(the resolver buys each short lot to close).
"""

from __future__ import annotations

from datetime import date

from skas_algo.engine.options import black_scholes as bs
from skas_algo.engine.options.contract_specs import lot_size_for
from skas_algo.engine.types import Signal, SignalAction


class ShortPremiumStrategy:
    strategy_id = "short_premium"

    def __init__(
        self,
        universe: list[str] | None = None,
        initial_capital: float = 2_500_000,
        underlying: str | None = None,
        structure: str = "straddle",          # "straddle" | "strangle"
        dte_target: int = 2,                   # enter when an expiry is ~this many days out
        strike_step: float | None = None,      # strangle: fixed point offset from ATM
        strangle_delta: float = 0.20,          # strangle: target |delta| per leg (if no strike_step)
        lots: int = 1,
        stop_loss_pct: float = 0.50,           # stop if combined premium rises this % over entry
        profit_target_pct: float = 0.50,       # book if combined premium decays this %
        reentry: bool = False,
        max_reentries: int = 0,
        risk_free_rate: float = 0.065,
        lot_overrides: dict | None = None,
        **_ignored,
    ):
        self.underlying = (underlying or (universe[0] if universe else "NIFTY")).upper()
        self.structure = structure
        self.dte_target = dte_target
        self.strike_step = strike_step
        self.strangle_delta = strangle_delta
        self.lots = lots
        self.stop_loss_pct = stop_loss_pct
        self.profit_target_pct = profit_target_pct
        self.reentry = reentry
        self.max_reentries = max_reentries
        self.r = risk_free_rate
        self.lot_overrides = lot_overrides

        # State (persisted for live recovery).
        self.legs: list[str] = []
        self.entry_premium: float = 0.0
        self.entry_expiry: date | None = None
        self.handled_expiry: date | None = None  # expiry already traded this cycle
        self.reentries: int = 0

    # ------------------------------------------------------------------ slice
    def on_slice(self, ctx) -> list[Signal]:
        chain = ctx.option_chain()
        if chain is None:
            return []  # not an options run
        today = ctx.today()

        if self.legs:
            return self._manage(ctx)
        return self._maybe_enter(ctx, chain, today)

    def _manage(self, ctx) -> list[Signal]:
        # If the engine already closed our legs (e.g. expiry settlement), reset state
        # so we can trade the next cycle and don't emit exits for non-existent lots.
        if not any(ctx.lots(leg) for leg in self.legs):
            self.handled_expiry = self.entry_expiry
            self._flat()
            return []
        try:
            current = sum(ctx.close(leg) for leg in self.legs)
        except KeyError:
            return []  # a leg didn't print today; manage next slice
        if self.entry_premium <= 0:
            return []
        change = (current - self.entry_premium) / self.entry_premium  # +ve = adverse for a short
        exit_now = reason = None
        if -change >= self.profit_target_pct:
            exit_now, reason = True, "target"
        elif change >= self.stop_loss_pct:
            exit_now, reason = True, "stop"
        if not exit_now:
            return []
        signals = [Signal(leg, SignalAction.EXIT_ALL, reason=reason) for leg in self.legs]
        self.handled_expiry = self.entry_expiry if reason == "target" else self.handled_expiry
        if reason == "stop" and self.reentry and self.reentries < self.max_reentries:
            self.reentries += 1  # allow a re-entry into the same expiry later
        else:
            self.handled_expiry = self.entry_expiry
        self._flat()
        return signals

    def _maybe_enter(self, ctx, chain, today) -> list[Signal]:
        expiry = chain.expiry_for_dte(self.underlying, today, self.dte_target)
        if expiry is None or (expiry - today).days > self.dte_target:
            return []  # entry window not reached yet
        if expiry == self.handled_expiry and not (self.reentry and self.reentries <= self.max_reentries):
            return []  # already traded this expiry
        spot = chain.spot(self.underlying, today)
        atm = chain.atm_strike(self.underlying, today, expiry, spot)
        if atm is None or spot is None:
            return []

        rows = {(r.strike, r.right): r for r in chain.chain(self.underlying, today, expiry)}
        ce_strike, pe_strike = self._pick_strikes(rows, atm, spot, expiry, today)
        ce, pe = rows.get((ce_strike, "CE")), rows.get((pe_strike, "PE"))
        if ce is None or pe is None or _bad(ce.close) or _bad(pe.close):
            return []

        units = self.lots * lot_size_for(self.underlying, expiry, overrides=self.lot_overrides)
        self.legs = [ce.symbol, pe.symbol]
        self.entry_premium = ce.close + pe.close
        self.entry_expiry = expiry
        meta = {"multiplier": 1}
        return [
            Signal(ce.symbol, SignalAction.ENTER_SHORT, quantity=units, reason="short_premium", meta=meta),
            Signal(pe.symbol, SignalAction.ENTER_SHORT, quantity=units, reason="short_premium", meta=meta),
        ]

    def _pick_strikes(self, rows, atm, spot, expiry, today):
        if self.structure == "straddle":
            return atm, atm
        if self.strike_step:  # fixed-offset strangle
            ce_strikes = sorted({k for (k, rgt) in rows if rgt == "CE" and k >= atm + self.strike_step})
            pe_strikes = sorted({k for (k, rgt) in rows if rgt == "PE" and k <= atm - self.strike_step}, reverse=True)
            return (ce_strikes[0] if ce_strikes else atm, pe_strikes[0] if pe_strikes else atm)
        # delta-targeted strangle: choose strikes whose BS delta ~ ±strangle_delta
        t = max((expiry - today).days, 0) / 365.0
        ce = self._delta_strike(rows, "CE", spot, t)
        pe = self._delta_strike(rows, "PE", spot, t)
        return ce or atm, pe or atm

    def _delta_strike(self, rows, right, spot, t):
        best, best_err = None, 1e9
        target = self.strangle_delta
        for (k, rgt), row in rows.items():
            if rgt != right or _bad(row.close) or t <= 0:
                continue
            iv = bs.implied_vol(row.close, spot, k, t, self.r, right)
            if iv is None:
                continue
            d = abs(bs.delta(spot, k, t, self.r, iv, right))
            if abs(d - target) < best_err:
                best, best_err = k, abs(d - target)
        return best

    def _flat(self):
        self.legs = []
        self.entry_premium = 0.0
        self.entry_expiry = None

    # ------------------------------------------------------- (de)serialize
    def export_state(self) -> dict:
        return {
            "legs": list(self.legs),
            "entry_premium": self.entry_premium,
            "entry_expiry": self.entry_expiry.isoformat() if self.entry_expiry else None,
            "handled_expiry": self.handled_expiry.isoformat() if self.handled_expiry else None,
            "reentries": self.reentries,
        }

    def load_state(self, state: dict) -> None:
        self.legs = list(state.get("legs", []))
        self.entry_premium = state.get("entry_premium", 0.0)
        ee = state.get("entry_expiry")
        he = state.get("handled_expiry")
        self.entry_expiry = date.fromisoformat(ee) if ee else None
        self.handled_expiry = date.fromisoformat(he) if he else None
        self.reentries = state.get("reentries", 0)


def _bad(x) -> bool:
    return x is None or x != x or x <= 0  # None / NaN / non-positive premium
