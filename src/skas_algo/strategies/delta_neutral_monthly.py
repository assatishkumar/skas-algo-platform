"""delta_neutral_monthly — 18Δ short strangle with premium-rebalance rolls (BANKNIFTY).

Recurring monthly cycle (owner's spec):
  * ENTER on the 2nd trading day after the previous monthly expiry, in the 10:30–15:00
    window at/after ``entry_time`` (~11:00): SELL the ~18-delta PE and CE of the current
    monthly (delta from per-row implied vol off the LIVE chain).
  * ADJUST whenever |CE_ltp − PE_ltp| > 40% of (CE_ltp + PE_ltp): close the CHEAP side
    and re-sell that same side at the strike whose LTP ≈ the expensive side's LTP.
    NOTE the spec's prose says "the tested side is closed" but its own worked example
    (10/10 → CE 15 / PE 6: close the PE, sell a new PE at ~15) closes the CHEAP side —
    the example is authoritative. Strikes never cross: the roll caps at the other
    side's strike (a straddle at most, never ITM past it).
  * Once a straddle forms, BUY hedges at the straddle's breakevens (K ± combined
    premium, snapped to the strike grid) → IRON FLY; adjustments stop.
  * EXIT at profit ≥ 2.5% of margin deployed (``stop_loss_pct`` exists, default 0=off).
    Expiry settlement is the backstop. After any exit → idle until next cycle's entry
    day (deploy once, runs monthly).

Design notes:
- Live-chain-driven throughout (delta solve + premium-matched rolls) → deploy-only with
  a broker quote source; BANKNIFTY has ~no cached chain history, so there is no backtest
  (CLAUDE.md; paper-first like CPRE/momentum_theta).
- ``margin_base`` (the rupee base for target/stop) is FROZEN at entry and RE-FROZEN
  after every structural change (roll / hedge): broker basket margin when available,
  else the deterministic model Σ over short legs (reads ~2× broker — source recorded).
- Adjustments are cooldown-gated (default 15 min) so a whippy day can't churn rolls
  every tick; the straddle is the hard floor regardless of count.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from skas_algo.engine.options import black_scholes as bs
from skas_algo.engine.options.contract_specs import lot_size_for
from skas_algo.engine.options.instrument import make
from skas_algo.engine.options.margin import MarginParams, short_option_margin
from skas_algo.engine.types import Signal, SignalAction

from ._options_common import bad_close

_STRIKE_STEP = {"BANKNIFTY": 100, "NIFTY": 50, "SENSEX": 100}
_EXPIRY_CUTOFF = time(15, 30)


def _hhmm(s: str, fallback: time) -> time:
    try:
        hh, mm = str(s).split(":")
        return time(int(hh), int(mm))
    except Exception:
        return fallback


class DeltaNeutralMonthlyStrategy:
    strategy_id = "delta_neutral_monthly"
    intraday = True  # ticks every refresh; entry window / adjustments / exits self-gate

    def __init__(
        self,
        universe: list[str] | None = None,
        initial_capital: float = 1_000_000,
        underlying: str | None = None,
        lots: int = 1,
        target_delta: float = 0.18,
        entry_time: str = "11:00",
        entry_window_end: str = "15:00",
        entry_days_after_expiry: int = 2,
        force_entry: bool = False,           # deploy-time: enter next window tick, any day
        adjust_threshold_pct: float = 40.0,  # |CE−PE| vs (CE+PE)
        adjust_cooldown_min: int = 15,
        profit_target_pct: float = 2.5,      # % of margin_base
        stop_loss_pct: float = 0.0,          # 0 = off (spec-faithful)
        risk_free_rate: float = 0.065,
        min_leg_oi: int = 1,
        lot_overrides: dict | None = None,
        **_ignored,
    ):
        self.underlying = (underlying or (universe[0] if universe else "BANKNIFTY")).upper()
        self.lots = max(1, int(lots))
        self.target_delta = float(target_delta)
        self.entry_time = _hhmm(entry_time, time(11, 0))
        self.entry_window_end = _hhmm(entry_window_end, time(15, 0))
        self.entry_days_after_expiry = int(entry_days_after_expiry)
        self.force_entry = bool(force_entry)
        self.adjust_threshold_pct = float(adjust_threshold_pct)
        self.adjust_cooldown_min = int(adjust_cooldown_min)
        self.profit_target_pct = float(profit_target_pct)
        self.stop_loss_pct = float(stop_loss_pct)
        self.r = float(risk_free_rate)
        self.min_leg_oi = int(min_leg_oi)
        self.initial_capital = initial_capital
        self.lot_overrides = lot_overrides

        # ---- state (all persisted) ----
        self.legs: list[dict] = []            # [{symbol, right, dir, units, entry}]
        self.phase: str = "idle"              # idle | strangle | straddle | ironfly
        self.cycle_expiry: str | None = None  # ISO expiry of the open cycle
        self.done_expiry: str | None = None   # last completed cycle (no same-month re-entry)
        self.margin_base: float = 0.0
        self.margin_source: str = ""
        self.last_adjust_at: str | None = None
        self.adjust_count: int = 0
        self.entered_day: str | None = None   # entry attempted/made this day (once/day gate)

    # ------------------------------------------------------------ live hooks
    def spot_symbols(self) -> list[str]:
        return [self.underlying]

    # ------------------------------------------------------------ cycle math
    def _listed_expiries(self, ctx, today: date) -> list[date]:
        chain = ctx.option_chain()
        if chain is None:
            return []
        try:
            return sorted(date.fromisoformat(str(e)[:10])
                          for e in chain.expiries(self.underlying, today))
        except Exception:  # pragma: no cover - chain hiccup → no entry this tick
            return []

    def _monthly_of(self, expiries: list[date], y: int, m: int) -> date | None:
        month = [e for e in expiries if (e.year, e.month) == (y, m)]
        return max(month) if month else None

    def _current_monthly(self, expiries: list[date], today: date) -> date | None:
        """The nearest month whose LAST listed expiry (the monthly) is still ahead."""
        y, m = today.year, today.month
        for _ in range(3):
            exp = self._monthly_of(expiries, y, m)
            if exp is not None and exp >= today:
                return exp
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return None

    def _entry_day(self, prev_expiry: date) -> date:
        """N trading days (weekday walk — holidays degrade by a day) after the expiry."""
        d, added = prev_expiry, 0
        while added < self.entry_days_after_expiry:
            d += timedelta(days=1)
            if d.weekday() < 5:
                added += 1
        return d

    def _is_entry_day(self, ctx, today: date) -> bool:
        if self.force_entry:
            return True
        expiries = self._listed_expiries(ctx, today - timedelta(days=45))
        prev = max((e for e in expiries if e < today), default=None)
        if prev is None:
            return False
        return today == self._entry_day(prev)

    # ----------------------------------------------------------------- slice
    def on_slice(self, ctx) -> list[Signal]:
        now: datetime = ctx.now()
        today: date = ctx.today()

        live = self._live_legs(ctx)
        if live:
            return self._manage(ctx, live, now)

        # Flat. A cycle that just ended (target/settle) parks us until the next entry day.
        if self.phase != "idle":
            self.done_expiry = self.cycle_expiry
            self.phase = "idle"
            self.cycle_expiry = None
            self.adjust_count = 0
            self.last_adjust_at = None

        if self.entered_day == today.isoformat():
            return []
        if not (self.entry_time <= now.time() <= self.entry_window_end):
            return []
        if not self._is_entry_day(ctx, today):
            return []
        return self._try_enter(ctx, now, today)

    def _live_legs(self, ctx) -> list[dict]:
        if not self.legs:
            return []
        if not any(ctx.lots(leg["symbol"]) for leg in self.legs):
            self.legs = []  # engine settled/closed everything
            return []
        return self.legs

    # ----------------------------------------------------------------- entry
    def _chain_rows(self, ctx, expiry_iso: str) -> dict[float, dict] | None:
        chain_fn = getattr(ctx.market, "live_chain", None)
        chain = chain_fn(self.underlying, expiry_iso) if chain_fn else None
        if not chain or not chain.get("rows"):
            return None
        return {float(r["strike"]): r for r in chain["rows"]}

    def _ltp(self, row: dict | None) -> float | None:
        v = (row or {}).get("ltp")
        return None if v is None or bad_close(v) else float(v)

    def _oi_ok(self, row: dict | None) -> bool:
        return int((row or {}).get("oi") or 0) >= self.min_leg_oi

    def _t_years(self, expiry: date, now: datetime) -> float:
        exp_dt = datetime(expiry.year, expiry.month, expiry.day,
                          _EXPIRY_CUTOFF.hour, _EXPIRY_CUTOFF.minute)
        return max((exp_dt - now).total_seconds(), 0.0) / (365.0 * 86400.0)

    def _pick_delta_strike(self, rows: dict[float, dict], side: str, spot: float,
                           t: float) -> tuple[float, float] | None:
        """(strike, ltp) whose BS |delta| (IV solved from its own LTP) is nearest
        target_delta. OTM rows only — the 18Δ strike is OTM by definition."""
        best = None
        for k, r in rows.items():
            if (side == "ce" and k <= spot) or (side == "pe" and k >= spot):
                continue
            leg = r.get(side)
            prem = self._ltp(leg)
            if prem is None or not self._oi_ok(leg) or t <= 0:
                continue
            right = "CE" if side == "ce" else "PE"
            iv = bs.implied_vol(prem, spot, k, t, self.r, right)
            if iv is None or iv <= 0:
                continue
            d = abs(bs.delta(spot, k, t, self.r, iv, right))
            err = abs(d - self.target_delta)
            if best is None or err < best[0]:
                best = (err, k, prem)
        return (best[1], best[2]) if best else None

    def _freeze_margin(self, ctx, spot: float) -> None:
        """Freeze/refreeze the rupee base for target/stop: broker basket margin when the
        live path can serve it, else the model sum over SHORT legs (~2× broker)."""
        broker = None
        margin_fn = getattr(ctx, "position_margin", None)
        if margin_fn is not None:
            try:
                broker = margin_fn()
            except Exception:  # pragma: no cover
                broker = None
        if broker:
            self.margin_base = float(broker)
            self.margin_source = "broker"
        else:
            p = MarginParams()
            short_units = sum(leg["units"] for leg in self.legs if leg["dir"] < 0)
            self.margin_base = float(short_option_margin(float(spot), short_units, 1, p))
            self.margin_source = "model"

    def _try_enter(self, ctx, now: datetime, today: date) -> list[Signal]:
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
        t = self._t_years(expiry, now)
        ce = self._pick_delta_strike(rows, "ce", float(spot), t)
        pe = self._pick_delta_strike(rows, "pe", float(spot), t)
        if ce is None or pe is None:
            return []
        try:
            per_lot = lot_size_for(self.underlying, expiry, overrides=self.lot_overrides)
        except KeyError:
            return []
        units = float(self.lots * per_lot)

        def sym(k: float, right: str) -> str:
            return make(self.underlying, expiry, float(k), right, lot_size=per_lot,
                        lot_overrides=self.lot_overrides).symbol

        self.legs = [
            {"symbol": sym(ce[0], "CE"), "right": "CE", "dir": -1, "units": units,
             "entry": ce[1]},
            {"symbol": sym(pe[0], "PE"), "right": "PE", "dir": -1, "units": units,
             "entry": pe[1]},
        ]
        self.phase = "strangle"
        self.cycle_expiry = expiry.isoformat()
        self.entered_day = today.isoformat()
        self.adjust_count = 0
        self.last_adjust_at = None
        self._freeze_margin(ctx, float(spot))
        return [
            Signal(leg["symbol"], SignalAction.ENTER_SHORT, quantity=int(leg["units"]),
                   reason="dnm_entry", meta={"multiplier": 1})
            for leg in self.legs
        ]

    # ---------------------------------------------------------------- manage
    def _manage(self, ctx, live: list[dict], now: datetime) -> list[Signal]:
        # P&L exits first (any phase).
        has_print = getattr(ctx.market, "has_print", None)
        pnl = 0.0
        marks: dict[str, float] = {}
        for leg in live:
            if has_print is not None and not has_print(leg["symbol"]):
                return []  # a stale mark would make the P&L judgement dishonest
            try:
                cur = ctx.close(leg["symbol"])
            except KeyError:
                return []
            marks[leg["symbol"]] = cur
            pnl += (cur - leg["entry"]) * leg["units"] * leg["dir"]
        if self.margin_base > 0:
            if pnl >= self.margin_base * self.profit_target_pct / 100.0:
                return self._exit_all(live, "target")
            if self.stop_loss_pct > 0 and pnl <= -self.margin_base * self.stop_loss_pct / 100.0:
                return self._exit_all(live, "stop")

        if self.phase != "strangle":
            return []  # straddle already hedged (ironfly) → ride to target/settle
        if self.last_adjust_at is not None:
            last = datetime.fromisoformat(self.last_adjust_at)
            if (now - last).total_seconds() < self.adjust_cooldown_min * 60:
                return []
        return self._maybe_adjust(ctx, live, marks, now)

    def _maybe_adjust(self, ctx, live: list[dict], marks: dict[str, float],
                      now: datetime) -> list[Signal]:
        ce_leg = next((leg for leg in live if leg["right"] == "CE" and leg["dir"] < 0), None)
        pe_leg = next((leg for leg in live if leg["right"] == "PE" and leg["dir"] < 0), None)
        if ce_leg is None or pe_leg is None:
            return []
        ce_ltp, pe_ltp = marks[ce_leg["symbol"]], marks[pe_leg["symbol"]]
        total = ce_ltp + pe_ltp
        if total <= 0 or abs(ce_ltp - pe_ltp) <= total * self.adjust_threshold_pct / 100.0:
            return []

        cheap, rich = (pe_leg, ce_leg) if pe_ltp < ce_ltp else (ce_leg, pe_leg)
        rich_ltp = marks[rich["symbol"]]
        side = "pe" if cheap["right"] == "PE" else "ce"
        expiry = date.fromisoformat(self.cycle_expiry)  # cycle always set while holding
        rows = self._chain_rows(ctx, self.cycle_expiry)
        if rows is None:
            return []
        rich_strike = float(rich["symbol"].split("|")[2])
        pick = self._match_premium_strike(rows, side, rich_ltp, rich_strike)
        if pick is None:
            return []
        new_k, new_ltp = pick
        cheap_strike = float(cheap["symbol"].split("|")[2])
        if new_k == cheap_strike:
            return []  # nearest match IS the current strike — nothing to roll

        per_lot = int(cheap["units"] // self.lots) or 1
        new_sym = make(self.underlying, expiry, float(new_k), cheap["right"],
                       lot_size=per_lot, lot_overrides=self.lot_overrides).symbol
        signals = [
            Signal(cheap["symbol"], SignalAction.EXIT_ALL, reason="dnm_roll"),
            Signal(new_sym, SignalAction.ENTER_SHORT, quantity=int(cheap["units"]),
                   reason="dnm_roll", meta={"multiplier": 1}),
        ]
        self.legs = [leg for leg in self.legs if leg["symbol"] != cheap["symbol"]]
        self.legs.append({"symbol": new_sym, "right": cheap["right"], "dir": -1,
                          "units": cheap["units"], "entry": new_ltp})
        self.adjust_count += 1
        self.last_adjust_at = now.isoformat()

        if new_k == rich_strike:
            # Straddle formed → hedge both breakevens NOW (same decision) → iron fly.
            combined = rich_ltp + new_ltp
            step = _STRIKE_STEP.get(self.underlying, 100)
            up_k = round((rich_strike + combined) / step) * step
            dn_k = round((rich_strike - combined) / step) * step
            for k, right in ((up_k, "CE"), (dn_k, "PE")):
                row = rows.get(float(k), {}).get(right.lower())
                prem = self._ltp(row) or 0.0
                hedge_sym = make(self.underlying, expiry, float(k), right,
                                 lot_size=per_lot, lot_overrides=self.lot_overrides).symbol
                signals.append(Signal(hedge_sym, SignalAction.ENTER_LONG,
                                      quantity=int(cheap["units"]), reason="dnm_ironfly",
                                      meta={"multiplier": 1}))
                self.legs.append({"symbol": hedge_sym, "right": right, "dir": 1,
                                  "units": cheap["units"], "entry": prem})
            self.phase = "ironfly"
        else:
            self.phase = "strangle"

        spot_fn = getattr(ctx.market, "index_spot", None)
        spot = (spot_fn(self.underlying) if spot_fn else None) or rich_strike
        self._freeze_margin(ctx, float(spot))
        return signals

    def _match_premium_strike(self, rows: dict[float, dict], side: str,
                              target_ltp: float, cap_strike: float):
        """(strike, ltp) on ``side`` whose LTP is nearest ``target_ltp``, hard-capped at
        the other side's strike (puts stay ≤ cap, calls stay ≥ cap — never crossing)."""
        best = None
        for k, r in rows.items():
            if side == "pe" and k > cap_strike:
                continue
            if side == "ce" and k < cap_strike:
                continue
            leg = r.get(side)
            prem = self._ltp(leg)
            if prem is None or not self._oi_ok(leg):
                continue
            err = abs(prem - target_ltp)
            if best is None or err < best[0]:
                best = (err, k, prem)
        return (best[1], best[2]) if best else None

    def _exit_all(self, live: list[dict], reason: str) -> list[Signal]:
        sigs = [Signal(leg["symbol"], SignalAction.EXIT_ALL, reason=reason) for leg in live]
        self.done_expiry = self.cycle_expiry
        self.legs = []
        self.phase = "idle"
        self.cycle_expiry = None
        return sigs

    # --------------------------------------------------------------- monitor
    def basket_status(self, market, portfolio, margin: float | None = None) -> dict:
        out: dict = {
            "kind": "delta_neutral",
            "phase": self.phase,
            "legs": [dict(leg) for leg in self.legs],
            "margin_base": self.margin_base,
            "margin_source": self.margin_source,
            "target_amt": self.margin_base * self.profit_target_pct / 100.0,
            "stop_amt": (self.margin_base * self.stop_loss_pct / 100.0)
            if self.stop_loss_pct > 0 else None,
            "adjust_count": self.adjust_count,
            "cycle_expiry": self.cycle_expiry,
        }
        try:
            shorts = [leg for leg in self.legs if leg["dir"] < 0]
            if len(shorts) == 2:
                a, b = (market.close(shorts[0]["symbol"]), market.close(shorts[1]["symbol"]))
                if a + b > 0:
                    out["imbalance_pct"] = round(abs(a - b) / (a + b) * 100.0, 1)
                    out["threshold_pct"] = self.adjust_threshold_pct
        except Exception:  # pragma: no cover - monitoring never breaks a snapshot
            pass
        return out

    # ------------------------------------------------------- (de)serialize
    def export_state(self) -> dict:
        return {
            "legs": [dict(leg) for leg in self.legs],
            "phase": self.phase,
            "cycle_expiry": self.cycle_expiry,
            "done_expiry": self.done_expiry,
            "margin_base": self.margin_base,
            "margin_source": self.margin_source,
            "last_adjust_at": self.last_adjust_at,
            "adjust_count": self.adjust_count,
            "entered_day": self.entered_day,
        }

    def load_state(self, state: dict) -> None:
        self.legs = [dict(leg) for leg in state.get("legs", [])]
        self.phase = state.get("phase", "idle")
        self.cycle_expiry = state.get("cycle_expiry")
        self.done_expiry = state.get("done_expiry")
        self.margin_base = float(state.get("margin_base", 0.0))
        self.margin_source = state.get("margin_source", "")
        self.last_adjust_at = state.get("last_adjust_at")
        self.adjust_count = int(state.get("adjust_count", 0))
        self.entered_day = state.get("entered_day")
