"""Helpers shared by options strategies (premium sanity, strike snap, expiry pick)."""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from math import floor


def bad_close(x) -> bool:
    return x is None or x != x or x <= 0  # None / NaN / non-positive premium


def snap(strikes: list[float], target: float) -> float | None:
    """Nearest listed strike to ``target`` (None on an empty chain)."""
    return min(strikes, key=lambda k: abs(k - target)) if strikes else None


# The first minutes of the session are not a price anyone should act on. Spreads on an index
# option run 3-7% of mid at 09:15 against ~0.3% by 09:30, many LTPs are still yesterday's
# close, and the marks a %-rule is measured against are therefore fiction — paper run 30
# (2026-09-04) crossed those spreads and "booked a target" on a book that was down ₹9,765.
# OWNER RULE, 2026-09-06: no option strategy books a profit, a stop, a calendar exit or a
# roll before this time. The trade-off is accepted deliberately: a genuine gap can widen a
# loss in these five minutes, but the stop that would have fired is measured on prices that
# do not mean anything yet, and the exit itself would pay the same spread. Manual exits are
# NOT gated — the Exit-all button and "Mark closed at broker" are the owner's hand.
EXIT_NOT_BEFORE = time(9, 20)


class OpenSettleGuard:
    """Holds every PRICE-DRIVEN exit decision until the open has settled (``EXIT_NOT_BEFORE``).

    Gates discretionary decisions only — target, stop, trail, calendar exit, adjustment, roll.
    A HARD time exit is never gated (they all sit at 15:00-15:25, so the guard cannot reach
    them), an ENTRY is never gated (the intraday decks deliberately enter 09:16-09:20), and
    neither is a manual flatten, which does not run through a strategy at all.

    ``exit_not_before`` (None = the platform's 09:20) is a class attribute rather than a ctor
    param on fifteen strategies: it is one uniform rail the owner asked for on every option
    book, running deploys included, so there is nothing per-deploy to set. It applies in
    BACKTEST too — the 1-min replay ticks from 09:15, and a rail that fired only in live
    would break the mode-equivalence invariant (§3). The EOD engine decides once a day at
    15:20 and never sees it."""

    exit_not_before: str | None = None

    def _now(self, ctx) -> datetime:
        fn = getattr(ctx, "now", None)
        if fn is not None:
            return fn()
        return datetime.combine(ctx.today(), time(15, 30))  # stub ctx → treat as EOD

    def _open_settled(self, now: datetime) -> bool:
        """False in the opening minutes — hold every price-driven exit until then."""
        floor_t = EXIT_NOT_BEFORE
        raw = getattr(self, "exit_not_before", None)
        if raw:
            try:
                floor_t = time.fromisoformat(str(raw))
            except (ValueError, TypeError):
                floor_t = EXIT_NOT_BEFORE  # malformed → the platform default, never wider
        try:
            return now.time() >= floor_t
        except AttributeError:  # pragma: no cover - a dateless stub is EOD by convention
            return True

    def _settle_phrase(self) -> str:
        raw = getattr(self, "exit_not_before", None) or EXIT_NOT_BEFORE.strftime("%H:%M")
        return f"never before {raw}"


class ExitCadenceMixin(OpenSettleGuard):
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

    def _eod_reached(self, now: datetime) -> bool:
        try:
            return now.time() >= time.fromisoformat(getattr(self, "eod_time", "15:15"))
        except (ValueError, TypeError):
            return True

    def _due(self, kind: str, now: datetime) -> bool:
        """Is the ``kind`` check ("profit"/"stop"/"time", optionally ":<book>"-suffixed)
        due at ``now``? The cadence attr is looked up from the BASE kind (before ":")."""
        # The opening minutes are held (OpenSettleGuard). Return WITHOUT stamping
        # ``_last_check``: the window is not consumed, so the first check at 09:20 fires at
        # once rather than waiting out a cadence interval from a slot it never got to use.
        if not self._open_settled(now):
            return False
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
        settle = f", {self._settle_phrase()}"
        if cadence == "tick":
            return f"checked every tick{settle}"
        return f"checked every {cadence.replace('min', ' min')}{settle}"


class TrailingStopMixin:
    """A profit-protecting trailing stop, extracted from IntradayStraddleStrategy (2026-07-22)
    so every %-of-margin seller can share ONE implementation. The trail sits ABOVE a fixed
    floor and only ratchets UP as a high-water P&L (`peak_pct`, in % of the strategy's
    `margin_base`) grows — the fixed stop remains the hard backstop.

    Two modes (mirroring the deploy forms):
      * ``ratchet``    — each +``trail_trigger_pct`` of PEAK profit lifts the stop by
                         +``trail_step_pct`` (peak +4% w/ 2/1 → stop −stop+2%; +6% → −stop+3%).
      * ``below_peak`` — once ``peak_pct`` ≥ ``trail_trigger_pct``, stop = ``peak_pct`` −
                         ``trail_step_pct``.
    Trailing is OFF (returns just the fixed floor) when either trail pct is 0 — the §1 default,
    so a strategy that doesn't set them behaves exactly as before.

    Consumers add the three ctor params (`trail_trigger_pct`/`trail_step_pct`/`trail_mode`,
    default 0/0/"ratchet"), init `self.peak_pct = 0.0` (persist it in export_state), lift it on
    the PROFIT cadence via ``_update_peak`` and compare on the STOP cadence via
    ``_trail_stop_level`` — passing their own fixed stop level (in signed % of margin)."""

    def _update_peak(self, pnl_pct: float) -> None:
        """Raise the high-water P&L% (call on the profit cadence, after the pnl is computed)."""
        if pnl_pct > getattr(self, "peak_pct", 0.0):
            self.peak_pct = float(pnl_pct)

    def _trail_stop_level(self, fixed_pct: float) -> float:
        """Current stop as a signed % of margin_base (negative = a loss floor). ``fixed_pct`` is
        the strategy's fixed stop level (e.g. ``-stop_pct``, or ``-inf``/very-negative when no
        fixed stop is set). Ratchets up from there per ``trail_mode``; trailing off (either trail
        pct ≤ 0) → just ``fixed_pct``."""
        trig = float(getattr(self, "trail_trigger_pct", 0.0) or 0.0)
        step = float(getattr(self, "trail_step_pct", 0.0) or 0.0)
        peak = float(getattr(self, "peak_pct", 0.0) or 0.0)
        if trig <= 0 or step <= 0:
            return fixed_pct
        if str(getattr(self, "trail_mode", "ratchet")) == "below_peak":
            if peak < trig:
                return fixed_pct
            return max(fixed_pct, peak - step)
        # ratchet (default): each trail_trigger_pct of peak profit lifts the stop by trail_step_pct
        steps = floor(peak / trig) if peak > 0 else 0
        return max(fixed_pct, fixed_pct + step * steps)


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


class SkipReasonMixin:
    """Remember WHY the last entry attempt was refused, so the Live tile can say so.

    Every option strategy's entry path is a chain of silent ``return []``s — the entry
    day, the entry window, a chain that did not price, a credit outside its window, a
    premium hunt that missed. All correct, all invisible: two paper ratio runs sat flat
    for two weeks (2026-08-20 → 09-04) with nothing in the log or on the tile to say
    which gate had refused them, and the owner reasonably read it as "didn't take off".

    ``_skip(reason, day)`` records and returns ``[]`` so it drops into any ``return []``
    unchanged; ``_entered()`` clears it. The snapshot carries it as ``entry_skip`` and the
    tile renders it while the run is flat. In-memory only — the next slice re-records."""

    last_skip: dict | None = None

    def _skip(self, reason: str, day: date | None = None) -> list:
        self.last_skip = {"reason": str(reason), "day": day.isoformat() if day else None}
        return []

    def _entered(self) -> None:
        self.last_skip = None


class EntrySpreadGateMixin:
    """Refuse a FRESH entry when any leg's bid-ask spread is wider than ``max_spread_pct``
    of its mid.

    Paper run 30 (2026-09-04): a butterfly forced at 09:15:03 crossed ₹20-60 spreads on
    three BANKNIFTY monthly legs, about ₹25 a unit round trip on a structure whose whole
    edge is ~₹17 a unit. A strategy marks on LTP, so it could not see the cost it had just
    paid — it saw a +3% "target" two minutes later on a book that was down ₹9,765. The
    spread is the one execution cost a strategy CAN see before it trades, off the same
    chain it picks strikes from, so it should look.

    FAIL-OPEN where there is no bid/ask: the backtest chain carries only close/oi, so this
    gate never fires in a replay — it is a LIVE-ONLY refusal rail (a skipped entry, never a
    different trade), and it says so in the reason. ``max_spread_pct`` 0 = off (§1)."""

    max_spread_pct: float = 0.0

    @staticmethod
    def _spread_pct(cell: dict | None) -> float | None:
        bid, ask = (cell or {}).get("bid"), (cell or {}).get("ask")
        if bid is None or ask is None:
            return None
        bid, ask = float(bid), float(ask)
        mid = (bid + ask) / 2.0
        if mid <= 0 or ask < bid:
            return None
        return (ask - bid) / mid * 100.0

    def _spread_refusal(self, cells: dict[str, dict | None]) -> str | None:
        """``cells`` = {leg label: chain cell}. The first leg over the cap, described, or
        None when every leg passes (or has no book to judge)."""
        cap = float(getattr(self, "max_spread_pct", 0.0) or 0.0)
        if cap <= 0:
            return None
        for label, cell in cells.items():
            spr = self._spread_pct(cell)
            if spr is not None and spr > cap:
                bid, ask = float(cell["bid"]), float(cell["ask"])
                return (f"{label}: bid {bid:.2f} / ask {ask:.2f} = {spr:.1f}% spread > "
                        f"{cap:g}% cap — not paying that to open")
        return None


_marks_log = logging.getLogger("skas_algo.strategies.marks")


class MarkBasisMixin:
    """Read a %-target/stop on the prices an EXIT would get, against the REAL fills.

    Every option family used to measure its rule on the last traded price against the
    price it was THINKING about at the decision. The platform's exits are limit-at-touch,
    so a long sells into the BID and a short buys back at the ASK; and the entry cost is
    what the book paid, not the LTP. Paper run 30 booked a "+3% target" that realised
    −₹9,765 (2026-09-04); run 209 booked a "target" on SENSEX prints an hour old that
    realised −₹27,280 (2026-09-18). The delta family got `mark_basis` first (2026-09-07);
    since 2026-09-21 (owner decision) EVERY family reads "exit" by default, ctor included.

    Fail-open by construction: with no two-sided book (the backtest chain, a cache
    source, the 1-min replay) the exit price IS the LTP and a lot list that is not a list
    of lots adopts nothing — so backtests and replays decide exactly as before. `"ltp"`
    is still accepted and only watches. Leg records are ``{symbol, dir, units, entry}``
    dicts (the shape every family but custom_options/donchian keeps).
    """

    mark_basis: str = "exit"
    entry_shortfall: float = 0.0

    def _init_mark_basis(self, mark_basis) -> None:
        mb = str(mark_basis or "exit")
        if mb not in ("ltp", "exit"):
            raise ValueError(f"mark_basis must be 'ltp' or 'exit', got {mark_basis!r}")
        self.mark_basis = mb
        self.entry_shortfall = 0.0
        self._exit_marks: dict[str, float] = {}

    # ---- prices
    def _exit_price(self, ctx, symbol: str, direction: int, ltp: float) -> float:
        """The price an exit would cross NOW: bid for a long, ask for a short; the LTP
        when there is no two-sided book (same fail-open as the spread gate)."""
        ba_fn = getattr(getattr(ctx, "market", None), "_bid_ask", None)
        if ba_fn is None:
            return ltp
        try:
            ba = ba_fn(symbol)
        except Exception:  # pragma: no cover - a chain read must not stop a decision
            return ltp
        if not ba:
            return ltp
        bid, ask = ba
        px = bid if int(direction) > 0 else ask
        try:
            px = float(px)
        except (TypeError, ValueError):
            return ltp
        return px if px > 0 else ltp

    def _acting_mark(self, ctx, symbol: str, direction: int, ltp: float) -> float:
        """What the threshold reads for this leg: the exit price under "exit", the LTP
        under "ltp". Remembered so the snapshot's P&L (`_marks_for`) shows the same."""
        px = self._exit_price(ctx, symbol, direction, ltp) if self.mark_basis == "exit" else ltp
        if not hasattr(self, "_exit_marks"):
            self._exit_marks = {}
        self._exit_marks[symbol] = px
        return px

    def _marks_for(self, closes: dict) -> dict:
        """LTP closes overlaid with the last acting marks — the snapshot's strategy_pnl
        then reads what the thresholds read."""
        if self.mark_basis != "exit":
            return closes
        return {**closes, **getattr(self, "_exit_marks", {})}

    # ---- fills
    def _adopted_fill(self, ctx, symbol: str):
        """The book's average fill for ``symbol`` (units-weighted over its lots), or None
        when there is no book, no lots, or the lots are not lot objects (a replay's int)."""
        try:
            lots = ctx.lots(symbol) or []
        except Exception:  # pragma: no cover - a ctx without a book
            return None
        if not isinstance(lots, (list, tuple)) or not lots:
            return None
        try:
            units = sum(float(lot.units) for lot in lots)
            cost = sum(float(lot.units) * float(lot.price) for lot in lots)
        except (AttributeError, TypeError, ValueError):
            return None
        if units <= 0 or cost <= 0:
            return None
        return cost / units

    def _adopt_fill(self, ctx, leg: dict) -> None:
        """Once per leg dict: record the real fill (``entry_fill``/``entry_ltp``), count the
        shortfall against the decision price, and under "exit" make the fill the entry."""
        if leg.get("fill_seen"):
            return
        fill = self._adopted_fill(ctx, leg["symbol"])
        if fill is None:
            return
        decision = float(leg["entry"])
        direction = int(leg.get("dir", 1))
        slip = (fill - decision) * float(leg.get("units") or 0) * direction
        leg["fill_seen"] = True
        leg["entry_ltp"] = decision
        leg["entry_fill"] = round(fill, 4)
        self.entry_shortfall = float(getattr(self, "entry_shortfall", 0.0)) + slip
        if self.mark_basis == "exit":
            leg["entry"] = fill
        _marks_log.info(
            "MARKS fill %s sym=%s dir=%+d units=%d decision=%.2f fill=%.2f slip_per_unit=%+.2f "
            "shortfall=%+.0f cycle_shortfall=%+.0f basis=%s%s",
            getattr(self, "strategy_id", "?"), leg["symbol"], direction,
            int(leg.get("units") or 0), decision, fill, (fill - decision) * direction, slip,
            self.entry_shortfall, self.mark_basis, " adopted" if self.mark_basis == "exit" else "",
        )

    def _leg_mark(self, ctx, leg: dict, ltp: float) -> float:
        """Adopt the leg's fill (once) and return its acting mark — the one call a
        threshold loop needs per leg."""
        self._adopt_fill(ctx, leg)
        return self._acting_mark(ctx, leg["symbol"], int(leg.get("dir", 1)), ltp)

    def _marks_phrase(self) -> str:
        return ("at exit prices (longs at bid, shorts at ask, against the real fills)"
                if self.mark_basis == "exit" else "at last traded prices")
