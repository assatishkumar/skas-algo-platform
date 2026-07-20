"""Helpers shared by options strategies (premium sanity, strike snap, expiry pick)."""

from __future__ import annotations

from datetime import date, datetime, time


def bad_close(x) -> bool:
    return x is None or x != x or x <= 0  # None / NaN / non-positive premium


def snap(strikes: list[float], target: float) -> float | None:
    """Nearest listed strike to ``target`` (None on an empty chain)."""
    return min(strikes, key=lambda k: abs(k - target)) if strikes else None


class ExitCadenceMixin:
    """The two-cadence decision model shared by ALL options strategies (owner design,
    2026-07-18): every strategy has a PROFIT/ADJUST cadence (`profit_check`) and a
    STOP/EXIT cadence (`stop_check`), each ∈ tick/1min/5min/15min/30min/60min/eod —
    "eod" means at/after `eod_time`. Hard time exits (15:25 square-offs, exit weekdays)
    are NEVER cadence-gated. Extracted byte-identical from CallRatioMonthlyStrategy,
    where it originated (its backtest was one EOD slice/day, so every cadence collapsed
    to the daily bar; on the 1-min replay and in live the cadences actually bite).

    TWO RULES every consumer must follow — this is the riskiest seam of the model:
      1. ``_due`` CONSUMES its window (stamps ``_last_check`` on True). Sample it exactly
         once per kind per slice, AFTER every readiness guard (margin frozen, all legs
         printed, pnl computed) — consuming before an early return silently eats that
         evaluation window (a stop could skip its slot).
      2. Strategies managing multiple books key their checks per book
         (``_due(f"stop:{u}", …)``) or one underlying consumes the other's window.

    ``_last_check`` is created lazily so the mixin imposes nothing on __init__, and it is
    deliberately TRANSIENT (never exported in state): a restart re-arms every cadence,
    which errs toward evaluating sooner — the safe direction for stops."""

    _INTERVAL_MIN = {"tick": 0, "1min": 1, "5min": 5, "15min": 15, "30min": 30, "60min": 60}

    def _now(self, ctx) -> datetime:
        fn = getattr(ctx, "now", None)
        if fn is not None:
            return fn()
        return datetime.combine(ctx.today(), time(15, 30))  # stub ctx → treat as EOD

    def _eod_reached(self, now: datetime) -> bool:
        try:
            return now.time() >= time.fromisoformat(getattr(self, "eod_time", "15:15"))
        except (ValueError, TypeError):
            return True

    def _due(self, kind: str, now: datetime) -> bool:
        """Is the ``kind`` check ("profit"/"stop"/"time", optionally ":<book>"-suffixed)
        due at ``now``? The cadence attr is looked up from the BASE kind (before ":")."""
        cadence = getattr(self, f"{kind.split(':', 1)[0]}_check", "eod")
        if cadence == "eod":
            return self._eod_reached(now)
        mins = self._INTERVAL_MIN.get(cadence, 0)
        checks = self.__dict__.setdefault("_last_check", {})
        last = checks.get(kind)
        if last is None or (now - last).total_seconds() >= mins * 60:
            checks[kind] = now
            return True
        return False

    def _entry_time_ok(self, now: datetime) -> bool:
        if not getattr(self, "entry_time", None):
            return True
        try:
            return now.time() >= time.fromisoformat(self.entry_time)
        except (ValueError, TypeError):
            return True

    def _cadence_phrase(self, kind: str) -> str:
        """Human wording for how often the ``kind`` exit is SAMPLED — surfaced in the UI
        so the owner can see the check is periodic, not on-touch (run-7 2026-07-17: the
        15-min profit samples landed on P&L dips either side of a 19-min target breach)."""
        cadence = getattr(self, f"{kind}_check", "eod")
        if cadence == "eod":
            return f"checked at EOD {getattr(self, 'eod_time', '15:15')}"
        if cadence == "tick":
            return "checked every tick"
        return f"checked every {cadence.replace('min', ' min')}"


class EntryVolFilterMixin:
    """Optional entry gate for option SELLERS: skip a NEW entry when the vol risk premium
    (implied − realized vol, in vol points) is thin — i.e. you'd be selling cheap vol into a
    market already moving that much. Validated on batman (the /research loss-study, 2026-07):
    of nine candidate signals, an entry vol-premium filter (skip when VIX−HV20 < ~2) was the
    only one that cut losses OUT-OF-SAMPLE without giving back more in winners.

    GENERIC — any option-selling strategy inherits this, adds ``vol_premium_min`` /
    ``hv_window`` ctor params (BOTH defaulting to the OFF value, so §1 recovery stays
    byte-identical), injects a realized-vol provider via ``set_realized_vol_fn`` (the runtime
    wires it in backtest / replay / live by probing for the method), and calls
    ``_vol_premium_ok`` at its entry point passing its OWN ATM-IV.

    Implied vol = the strategy's ATM-IV off the chain it's about to trade (≈ India VIX for a
    NIFTY monthly) — same source in backtest and live, so no parity gap. Realized vol = the
    underlying's annualized HV over ``hv_window`` SETTLED sessions from the injected provider
    (cache-fed in backtest/replay, broker-first in live). FAIL-OPEN: if either number is
    unavailable the entry is NOT blocked — a data hiccup must never silently freeze trading.
    ``_last_vol_premium`` is stashed for surfacing (None when off / unevaluable)."""

    def set_realized_vol_fn(self, fn) -> None:
        """Inject ``fn(underlying, on_date) -> annualized HV percent | None``."""
        self._realized_vol_fn = fn

    def _vol_premium_ok(self, underlying: str, on_date, implied_iv_pct: float | None) -> bool:
        """True = OK to enter. ``implied_iv_pct`` and the provider's HV are both in PERCENT
        (vol points). Off (``vol_premium_min`` ≤ 0) or unevaluable → True (fail-open)."""
        self._last_vol_premium = None
        vpm = float(getattr(self, "vol_premium_min", 0.0) or 0.0)
        if vpm <= 0:
            return True                      # filter off → behaviour unchanged (§1)
        fn = getattr(self, "_realized_vol_fn", None)
        hv = fn(underlying, on_date) if fn is not None else None
        if implied_iv_pct is None or hv is None:
            return True                      # missing data → don't block trading
        self._last_vol_premium = float(implied_iv_pct) - float(hv)
        return self._last_vol_premium >= vpm


def legs_mtm_pnl(legs, closes: dict) -> float | None:
    """The DECISION-basis MTM the %-of-margin exit checks compare: Σ dir × (mark − entry)
    × units over the strategy's OWN legs. Leg entries are the decision-time premiums, not
    the actual fills, so live this can differ from the book P&L by the fill slippage
    (run-7 2026-07-17: ~₹276 on the short leg — the UI said "target achieved" while the
    strategy's own measure was still below it). Surfaced in the snapshot as
    ``strategy_pnl`` so the screen shows the number the strategy ACTS on. None when flat
    or any leg lacks a mark (matching the strategies' own bail-outs on missing prints)."""
    if not legs:
        return None
    total = 0.0
    for leg in legs:
        cur = closes.get(leg["symbol"])
        if cur is None:
            return None
        total += (float(cur) - float(leg["entry"])) * leg["units"] * leg["dir"]
    return total


def next_monthly_expiry(chain, underlying: str, today: date, min_dte: int,
                        right: str = "CE") -> date | None:
    """The nearest monthly expiry at least ``min_dte`` out.

    "Monthly" = the most LIQUID expiry of its calendar month (highest total open
    interest on today's chain), not simply the latest date — exchanges sometimes list
    odd late-month expiries whose contracts never trade but still carry frozen
    bhavcopy closes (e.g. NIFTY 2025-04-30 vs the real 2025-04-24 monthly); picking
    by date would enter phantom, un-executable positions.
    """
    exps = chain.expiries(underlying, today)
    if not exps:
        return None
    by_month: dict[tuple[int, int], list[date]] = {}
    for e in exps:
        if (e - today).days >= min_dte:
            by_month.setdefault((e.year, e.month), []).append(e)
    if not by_month:
        return None
    month = min(by_month)  # nearest qualifying month
    cands = by_month[month]
    if len(cands) == 1:
        return cands[0]

    def total_oi(exp: date) -> int:
        return sum(r.oi for r in chain.chain(underlying, today, exp) if r.right == right)

    return max(cands, key=total_oi)
