"""iron_fly_monthly — BANKNIFTY monthly IRON FLY (ATM straddle + breakeven wings) with the
shared post-iron-fly adjustment.

Same monthly cadence + machinery as ``delta_neutral_monthly`` (which it subclasses), but it
ENTERS the iron fly directly instead of arriving at it via a strangle → straddle → hedge roll:
  * SELL 1× ATM CE + 1× ATM PE — the straddle at strike K = the listed strike nearest spot.
  * ``combined = CE_ltp + PE_ltp``; BUY 1× CE at K+combined and 1× PE at K−combined (snapped to
    the strike grid) — the straddle's breakevens → a defined-risk iron fly.

The post-iron-fly adjustment is inherited and defaults ON here: on a breakeven breach, sell a
naked ~15-20Δ short on the untested side and roll it (close at ≤10Δ / ≤¼ premium, re-sell), and
exit ALL if the expiry payoff turns entirely negative (see delta_neutral_monthly._adjust_ironfly).

Deploy-only (live-chain ATM + wing premiums; broker quote source required) — no backtest.
"""

from __future__ import annotations

from datetime import date, datetime

from skas_algo.engine.options.contract_specs import lot_size_for
from skas_algo.engine.options.instrument import make
from skas_algo.engine.types import Signal, SignalAction

from ._options_common import bad_close
from .delta_neutral_monthly import _STRIKE_STEP, DeltaNeutralMonthlyStrategy


class IronFlyMonthlyStrategy(DeltaNeutralMonthlyStrategy):
    strategy_id = "iron_fly_monthly"

    def __init__(self, *args, ironfly_adjust: bool = True, **kwargs):
        # The whole point of this strategy is the active iron-fly adjustment → default ON
        # (delta_neutral_monthly defaults it OFF).
        super().__init__(*args, ironfly_adjust=ironfly_adjust, **kwargs)

    def _atm_strike(self, rows: dict[float, dict], spot: float) -> float | None:
        """The listed strike nearest ``spot`` with BOTH a CE and PE that print + have OI."""
        best = None
        for k, r in rows.items():
            ce, pe = r.get("ce"), r.get("pe")
            if not (self._oi_ok(ce) and self._oi_ok(pe)):
                continue
            if self._ltp(ce) is None or self._ltp(pe) is None:
                continue
            err = abs(k - spot)
            if best is None or err < best[0]:
                best = (err, k)
        return best[1] if best else None

    def _try_enter(self, ctx, now: datetime, today: date) -> list[Signal]:
        if self.entry_legs:
            # Build-view manual deploy: enter the owner's exact fly, then run _adjust_ironfly.
            sigs = self._enter_manual_generic(ctx, now, today)
            if sigs:  # a manual fly seeds the adjustment state the ironfly manager expects
                self.adjust_symbol = None
                self.adjust_realized = 0.0
            return sigs
        expiries = self._listed_expiries(ctx, today)
        expiry = self._current_monthly(expiries, today)
        if expiry is None or expiry.isoformat() == self.done_expiry:
            return []
        rows = self._chain_rows(ctx, expiry.isoformat())
        if rows is None:
            return []  # chain hiccup — retry within the window
        spot_fn = getattr(ctx.market, "index_spot", None)
        spot = spot_fn(self.underlying) if spot_fn else None
        if spot is None or bad_close(spot):
            return []
        k = self._atm_strike(rows, float(spot))
        if k is None:
            return []
        ce_ltp, pe_ltp = self._ltp(rows[k].get("ce")), self._ltp(rows[k].get("pe"))
        if ce_ltp is None or pe_ltp is None:
            return []
        try:
            per_lot = lot_size_for(self.underlying, expiry, overrides=self.lot_overrides)
        except KeyError:
            return []
        units = float(self.lots * per_lot)

        step = _STRIKE_STEP.get(self.underlying, 100)
        combined = ce_ltp + pe_ltp
        up_k = round((k + combined) / step) * step  # long CALL wing at the upper breakeven
        dn_k = round((k - combined) / step) * step  # long PUT wing at the lower breakeven
        up_prem = self._ltp(rows.get(float(up_k), {}).get("ce"))
        dn_prem = self._ltp(rows.get(float(dn_k), {}).get("pe"))
        if up_prem is None or dn_prem is None:
            return []  # a wing strike isn't tradeable in the chain — retry/skip

        def sym(strike: float, right: str) -> str:
            return make(
                self.underlying,
                expiry,
                float(strike),
                right,
                lot_size=per_lot,
                lot_overrides=self.lot_overrides,
            ).symbol

        self.legs = [
            {"symbol": sym(k, "CE"), "right": "CE", "dir": -1, "units": units, "entry": ce_ltp},
            {"symbol": sym(k, "PE"), "right": "PE", "dir": -1, "units": units, "entry": pe_ltp},
            {"symbol": sym(up_k, "CE"), "right": "CE", "dir": 1, "units": units, "entry": up_prem},
            {"symbol": sym(dn_k, "PE"), "right": "PE", "dir": 1, "units": units, "entry": dn_prem},
        ]
        self.phase = "ironfly"
        self.cycle_expiry = expiry.isoformat()
        self.entered_day = today.isoformat()
        self.adjust_count = 0
        self.last_adjust_at = None
        self.adjust_symbol = None
        self.adjust_realized = 0.0
        self._freeze_margin(ctx, float(spot))
        return [
            Signal(
                leg["symbol"],
                SignalAction.ENTER_SHORT if leg["dir"] < 0 else SignalAction.ENTER_LONG,
                quantity=int(leg["units"]),
                reason="ifm_entry",
                meta={"multiplier": 1},
            )
            for leg in self.legs
        ]
