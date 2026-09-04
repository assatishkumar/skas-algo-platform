"""fair_value_calendar — premium-matched ratio calendar with fair-value side selection (NIFTY).

The owner's video spec (2026-08-19; ref video
https://www.youtube.com/watch?v=tn-73I63yBw&t=2162s), two parts:

MEAN-REVERSION SIDE PICK. A "fair value" line = a post-crash reference high (the Jan-2020
COVID high, 12,430) compounded at a long-run growth rate (11.7%/yr). Spot significantly ABOVE
the line → a PUT calendar (paid by a reversion down); significantly BELOW → a CALL calendar;
inside the band → CALLs preferred (better rollover carry). ``side_mode`` makes the selector
optional: "fair_value" (auto) / "both" (both sides every month) / "pe" / "ce" (fixed).

THE STRUCTURE (per set, premium-hunted — screenshot #126, PE side, spot ~24,966):
  * SELL 1 lot  near-weekly (≥ min_sold_dte DTE) at ~₹150 premium   (24700 — OTM-ish)
  * SELL 1 lot  same weekly                      at ~₹450 premium   (25400 — ITM)
  * BUY  3 lots the MONTHLY after that weekly    at ~₹200 premium   (24500 — OTM)
Sell ≈ 150+450 ≈ 600 vs buy ≈ 3×200 ≈ 600 — premium-matched by construction, so the net
debit is ~0 and the %-target anchors to MARGIN (owner decision), the platform convention.

THE 900-POINT GAP RULE. Each sold leg earns ~70-80 pts per weekly roll → ~150/week → ~450
over a month's ~3 rolls. Only enter when the dead zone — |buy strike − furthest sell strike|
— is ≤ 2× that expected income (900). Premium hunting makes the gap volatility's choice, so
the rule doubles as a vol filter; a failed check retries the NEXT DAY (never burns the month).

CYCLE. Enter at the start of a calendar month (retry daily until a valid setup lands).
Target +profit_target_pct (5%) of the FROZEN broker margin on the whole cycle's P&L
(pnl_basis="total": banked rolls + open MTM). Not hit by the sold weekly's expiry → ROLL both
sold legs to the next weekly at the SAME strikes (screenshot #127), banking the decay. THE BUY
LEG IS NEVER ROLLED (owner rule 2026-08-19): when the roll would land the sells ON the buy
expiry the CYCLE ENDS — exit all, clear the month latch, and a FRESH cycle (new FV read, new
hunt) opens the next session, so a losing cycle is structurally bounded at ~one buy-expiry
month. Target hit / max_rolls → exit all, done for the month (flat till the next month starts).

ERA SCALING (owner decision): premiums are ABSOLUTE rupees for entries ≥ premium_scale_before
(2024-08-01) and scale by spot/premium_ref_spot before that — ₹150 at 25k was not ₹150 at 16k.
The gap cap scales identically. Live deploys always land in the absolute era.

Subclasses DeltaNeutralMonthlyStrategy (the double_diagonal_calendar precedent) for the
margin freeze/pending wait, %-of-margin target/stop/trail, cadences and the serialize spine;
phase stays "calendar" so the base's delta-adjustment machinery never engages. Deploy-only +
broker source live; REPLAYABLE on the 1-min store (the first two-expiry strategy with a
backtest — the premium hunt reads the same live_chain surface in both modes).
"""

from __future__ import annotations

from datetime import date, datetime, time

from skas_algo.engine.options.contract_specs import lot_size_for
from skas_algo.engine.options.instrument import make
from skas_algo.engine.types import Signal, SignalAction
from skas_algo.live.holidays import previous_trading_day

from ._options_common import EntrySpreadGateMixin, bad_close
from .delta_neutral_monthly import DeltaNeutralMonthlyStrategy, _hhmm


def _iso_date(s: str, fallback: date) -> date:
    try:
        return date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return fallback


class FairValueCalendarStrategy(EntrySpreadGateMixin, DeltaNeutralMonthlyStrategy):
    strategy_id = "fair_value_calendar"
    intraday = True

    def __init__(
        self,
        universe: list[str] | None = None,
        initial_capital: float = 1_000_000,
        underlying: str | None = None,
        sets: int = 1,
        margin_per_set: float = 0.0,   # ₹ per lot-SET; 0 = derive from broker
        # --- the premium-matched structure (per set; era-scaled, see module docstring) ---
        sell_premium_1: float = 150.0,
        sell_premium_2: float = 450.0,
        buy_premium: float = 200.0,
        buy_lots_per_set: int = 3,
        premium_tolerance_pct: float = 30.0,  # hunt miss beyond this → no entry today, retry
        max_gap_points: float = 900.0,  # |buy K − furthest sell K| cap (2× rollover income)
        premium_scale_before: str = "2024-08-01",  # entries before this scale by spot/ref
        premium_ref_spot: float = 24500.0,
        min_sold_dte: int = 4,
        # --- fair-value side selection ---
        side_mode: str = "fair_value",  # fair_value | both | pe | ce
        fv_anchor_date: str = "2020-01-20",
        fv_anchor_value: float = 12430.0,
        fv_growth_pct: float = 11.7,
        fv_band_pct: float = 4.0,  # |dev| ≤ band → CE preferred (rollover carry)
        # --- cycle management ---
        entry_time: str = "09:30",
        entry_window_end: str = "15:00",
        roll_time: str = "15:00",  # time of day the roll fires
        # Roll this many TRADING days BEFORE the sold expiry. Expiry-day margin on a short
        # weekly spikes (the exchange raises the risk charge into settlement), so rolling
        # on the last day costs materially more than rolling the day before — owner rule
        # 2026-08-20. Ctor default 0 = the historical expiry-day roll, so a recovered deploy
        # is byte-identical (§1); the FORM and the deploy route default to 1.
        roll_days_before: int = 0,
        max_rolls: int = 0,  # 0 = roll until the target hits (spec behavior)
        profit_target_pct: float = 5.0,  # % of frozen broker margin (owner decision)
        stop_loss_pct: float = 0.0,  # 0 = off (spec gives no stop)
        force_entry: bool = False,
        profit_check: str = "tick",
        stop_check: str = "tick",
        # The rolls ARE the income — the target must see their banked realized, so the
        # whole-cycle basis is the default here (unlike the base's open_legs).
        pnl_basis: str = "total",
        exit_margin_basis: str = "entry",
        min_leg_oi: int = 1,
        lot_overrides: dict | None = None,
        risk_free_rate: float = 0.065,
        max_spread_pct: float = 0.0,  # refuse to open on a leg wider than this % of mid; 0 = off
        **_ignored,
    ):
        super().__init__(
            universe=universe,
            initial_capital=initial_capital,
            underlying=(underlying or (universe[0] if universe else "NIFTY")),
            lots=sets,
            entry_time=entry_time,
            entry_window_end=entry_window_end,
            force_entry=force_entry,
            profit_target_pct=profit_target_pct,
            stop_loss_pct=stop_loss_pct,
            risk_free_rate=risk_free_rate,
            profit_check=profit_check,
            stop_check=stop_check,
            pnl_basis=pnl_basis,
            exit_margin_basis=exit_margin_basis,
            margin_per_set=margin_per_set,
            min_leg_oi=min_leg_oi,
            lot_overrides=lot_overrides,
        )
        self.sets = max(1, int(sets))
        self.max_spread_pct = max(0.0, float(max_spread_pct or 0.0))
        self.sell_premium_1 = float(sell_premium_1)
        self.sell_premium_2 = float(sell_premium_2)
        self.buy_premium = float(buy_premium)
        self.buy_lots_per_set = max(1, int(buy_lots_per_set))
        self.premium_tolerance_pct = float(premium_tolerance_pct)
        self.max_gap_points = float(max_gap_points)
        self.premium_scale_before = _iso_date(premium_scale_before, date(2024, 8, 1))
        self.premium_ref_spot = float(premium_ref_spot)
        self.min_sold_dte = int(min_sold_dte)
        self.side_mode = str(side_mode or "fair_value").lower()
        self.fv_anchor_date = _iso_date(fv_anchor_date, date(2020, 1, 20))
        self.fv_anchor_value = float(fv_anchor_value)
        self.fv_growth_pct = float(fv_growth_pct)
        self.fv_band_pct = float(fv_band_pct)
        self.roll_time = _hhmm(roll_time, time(15, 0))
        self.roll_days_before = max(0, int(roll_days_before))
        self.max_rolls = int(max_rolls)
        # Replay-harness sizing hint: short lots per lot-SET (one SIDE's two sold legs;
        # "both" doubles it — margin_per_lot is then the ₹ for the two-sided set). Per-set,
        # NOT ×sets: the harness spreads margin_per_lot across exactly these shorts.
        self.sell_lots = 2 * (2 if self.side_mode == "both" else 1)

        # ---- calendar state (persisted) ----
        self.sold_expiry: str | None = None  # ISO expiry of the sold weeklies
        self.buy_expiry: str | None = None  # ISO expiry of the long monthlies
        self.entered_month: str | None = None  # "YYYY-MM" — one cycle per calendar month
        self.roll_count: int = 0
        self.pending_resell: bool = False  # sold legs settled before the roll could fill
        # The entry strikes, frozen for the cycle — rolls re-sell exactly these
        # (screenshot #127: same strikes, next weekly), and they survive a settlement
        # that removed the live legs. [{right, strike, units}]
        self.sold_specs: list[dict] = []
        self.cycle_side: str | None = None  # PE | CE | BOTH
        self.entry_fv: float | None = None
        self.entry_dev_pct: float | None = None
        self.entry_scale: float = 1.0

    # ------------------------------------------------------------ fair value
    def _size_multiple(self) -> int:
        """This strategy sizes in lot-SETS (one sold pair + buy_lots_per_set longs), not the
        base class's ``lots`` — so a manual ``margin_per_set`` scales by ``sets``."""
        return max(1, int(self.sets))

    def _fair_value(self, today: date) -> float:
        yrs = (today - self.fv_anchor_date).days / 365.25
        return self.fv_anchor_value * (1.0 + self.fv_growth_pct / 100.0) ** yrs

    def _pick_sides(self, spot: float, today: date) -> tuple[list[str], float, float]:
        """(sides, fv, dev%) — the FV read is recorded even when side_mode fixes the side."""
        fv = self._fair_value(today)
        dev = (spot / fv - 1.0) * 100.0
        if self.side_mode == "both":
            return ["PE", "CE"], fv, dev
        if self.side_mode in ("pe", "ce"):
            return [self.side_mode.upper()], fv, dev
        if dev > self.fv_band_pct:
            return ["PE"], fv, dev  # stretched above fair value → paid by reversion down
        return ["CE"], fv, dev  # below OR inside the band → calls (rollover preference)

    def _era_scale(self, today: date, spot: float) -> float:
        if today >= self.premium_scale_before:
            return 1.0
        return spot / self.premium_ref_spot if self.premium_ref_spot > 0 else 1.0

    # ------------------------------------------------------------ live hooks
    def request_force_entry(self) -> str:
        self.force_pending = True
        return "next tick builds the premium-matched calendar (2 weekly sells + monthly buys)"

    def _index_spot(self, ctx) -> float | None:
        spot_fn = getattr(ctx.market, "index_spot", None)
        spot = spot_fn(self.underlying) if spot_fn else None
        return float(spot) if spot is not None and not bad_close(spot) else None

    # ----------------------------------------------------------------- slice
    def on_slice(self, ctx) -> list[Signal]:
        now: datetime = ctx.now()
        today: date = ctx.today()

        live = self._live_legs(ctx)
        if live:
            return self._manage(ctx, live, now)

        # Flat. A cycle the ENGINE closed (monthly settlement) parks us for its month —
        # keyed off the buy leg's expiry month, NOT today: a monthly that settles on the
        # 31st must not burn the month that starts tomorrow.
        if self.phase != "idle":
            self.entered_month = (self.buy_expiry or today.isoformat())[:7]
            self.done_expiry = self.cycle_expiry
            self.phase = "idle"
            self.cycle_expiry = None
            self.sold_expiry = None
            self.buy_expiry = None
            self.sold_specs = []
            self.roll_count = 0
            self.pending_resell = False
            self.realized_rolls = 0.0
            self.adjust_realized = 0.0
            self.peak_pct = 0.0

        if self.force_pending or (self.force_entry and self.entered_month is None):
            got = self._try_enter(ctx, now, today, force=True)
            if got:
                self.force_pending = False
            return got
        if self.entered_day == today.isoformat():
            return []
        if not (self.entry_time <= now.time() <= self.entry_window_end):
            return []
        if self.entered_month == today.strftime("%Y-%m"):
            return self._skip("done for this month (one cycle per calendar month)", today)
        return self._try_enter(ctx, now, today)

    # ----------------------------------------------------------------- entry
    def _hunt_premium(self, rows: dict[float, dict], side: str, target: float):
        """(strike, ltp) trading nearest ``target`` premium — NO moneyness predicate (the
        ~450 leg is deliberately ITM). None on a tolerance miss → no entry today, retry
        tomorrow (a monthly window, unlike cpre's one-day one)."""
        best = None
        # sorted: ties in |prem − target| resolve to the LOWEST strike, deterministically —
        # the chain dict's insertion order follows the data source's row order, which is
        # not stable across replays (parquet scan order) and drifted a rerun by ~₹600.
        for k, r in sorted(rows.items()):
            leg = r.get(side)
            prem = self._ltp(leg)
            if prem is None or not self._oi_ok(leg):
                continue
            err = abs(prem - target)
            if best is None or err < best[0]:
                best = (err, k, prem)
        if best is None or target <= 0:
            return None
        if best[0] / target * 100.0 > self.premium_tolerance_pct:
            return None
        return best[1], best[2]

    def _pick_expiries(self, ctx, today: date) -> tuple[date | None, date | None]:
        """(sold, buy): sold = nearest listed ≥ min_sold_dte DTE; buy = the monthly of the
        sold expiry's month when it lies strictly BEYOND the sold weekly, else the next
        month's monthly (spec: sold and buy expiries must always differ)."""
        expiries = self._listed_expiries(ctx, today)
        sold = min((e for e in expiries if (e - today).days >= self.min_sold_dte), default=None)
        if sold is None:
            return None, None
        cur_mon = self._monthly_of(expiries, sold.year, sold.month)
        if cur_mon is not None and cur_mon > sold:
            return sold, cur_mon
        ny, nm = (sold.year + 1, 1) if sold.month == 12 else (sold.year, sold.month + 1)
        return sold, self._monthly_of(expiries, ny, nm)

    def _try_enter(self, ctx, now: datetime, today: date, force: bool = False) -> list[Signal]:
        if not force and self.entered_month == today.strftime("%Y-%m"):
            return []
        spot = self._index_spot(ctx)
        if spot is None:
            return self._skip("no index spot", today)
        sold_e, buy_e = self._pick_expiries(ctx, today)
        if sold_e is None or buy_e is None:
            return self._skip(f"no sold weekly ≥{self.min_sold_dte} DTE with a monthly beyond it", today)
        rows_sold = self._chain_rows(ctx, sold_e.isoformat())
        rows_buy = self._chain_rows(ctx, buy_e.isoformat())
        if rows_sold is None or rows_buy is None:
            return self._skip("chain did not price (sold or buy expiry)", today)
        try:
            sold_lot = lot_size_for(self.underlying, sold_e, overrides=self.lot_overrides)
            buy_lot = lot_size_for(self.underlying, buy_e, overrides=self.lot_overrides)
        except KeyError:
            return []

        sides, fv, dev = self._pick_sides(spot, today)
        scale = self._era_scale(today, spot)
        gap_cap = self.max_gap_points * scale

        legs: list[dict] = []
        for right in sides:
            side = right.lower()
            s1 = self._hunt_premium(rows_sold, side, self.sell_premium_1 * scale)
            s2 = self._hunt_premium(rows_sold, side, self.sell_premium_2 * scale)
            b = self._hunt_premium(rows_buy, side, self.buy_premium * scale)
            if s1 is None or s2 is None or b is None:
                miss = [n for n, v in (("sell ~%g" % (self.sell_premium_1 * scale), s1),
                                       ("sell ~%g" % (self.sell_premium_2 * scale), s2),
                                       ("buy ~%g" % (self.buy_premium * scale), b)) if v is None]
                return self._skip(f"{right} premium hunt missed {', '.join(miss)} "
                                  f"(±{self.premium_tolerance_pct:g}%) — retry tomorrow", today)
            if s1[0] == s2[0]:
                return self._skip(f"{right} sells landed on the same strike {s1[0]:.0f}", today)
            # The 900-point rule: dead zone (buy strike ↔ furthest sell) ≤ 2× the
            # expected monthly rollover income, era-scaled with the premiums.
            wide = self._spread_refusal({
                f"sell {right} {s1[0]:.0f}": (rows_sold.get(s1[0]) or {}).get(side),
                f"sell {right} {s2[0]:.0f}": (rows_sold.get(s2[0]) or {}).get(side),
                f"buy {right} {b[0]:.0f}": (rows_buy.get(b[0]) or {}).get(side),
            })
            if wide:
                return self._skip(wide, today)
            if max(abs(s1[0] - b[0]), abs(s2[0] - b[0])) > gap_cap:
                return self._skip(f"gap rule: buy {b[0]:.0f} to furthest sell "
                                  f"{max(abs(s1[0] - b[0]), abs(s2[0] - b[0])):.0f} pts > "
                                  f"{gap_cap:.0f} cap", today)
            s_units = float(self.sets * sold_lot)
            b_units = float(self.sets * self.buy_lots_per_set * buy_lot)
            legs += [
                self._leg(sold_e, s1[0], right, -1, s_units, s1[1], sold_lot),
                self._leg(sold_e, s2[0], right, -1, s_units, s2[1], sold_lot),
                self._leg(buy_e, b[0], right, 1, b_units, b[1], buy_lot),
            ]

        self.legs = legs
        self._entered()
        self.phase = "calendar"  # never "strangle"/"ironfly" → base adjustments stay off
        self.sold_expiry = sold_e.isoformat()
        self.buy_expiry = buy_e.isoformat()
        self.cycle_expiry = buy_e.isoformat()  # base helpers/reporting key off cycle_expiry
        self.sold_specs = [
            {"right": leg["right"], "strike": float(leg["symbol"].split("|")[2]),
             "units": leg["units"]}
            for leg in legs
            if leg["dir"] < 0
        ]
        self.entered_day = today.isoformat()
        self.entered_month = today.strftime("%Y-%m")
        self.roll_count = 0
        self.pending_resell = False
        self.cycle_side = "BOTH" if len(sides) == 2 else sides[0]
        self.entry_fv = round(fv, 2)
        self.entry_dev_pct = round(dev, 2)
        self.entry_scale = round(scale, 4)
        self._freeze_margin(ctx, spot)
        return [
            Signal(
                leg["symbol"],
                SignalAction.ENTER_SHORT if leg["dir"] < 0 else SignalAction.ENTER_LONG,
                quantity=int(leg["units"]),
                reason="fvc_entry",
                meta={"multiplier": 1},
            )
            for leg in legs
        ]

    def _leg(
        self, expiry: date, k: float, right: str, direction: int, units: float,
        entry: float, per_lot: int,
    ) -> dict:
        sym = make(
            self.underlying, expiry, float(k), right,
            lot_size=per_lot, lot_overrides=self.lot_overrides,
        ).symbol
        return {"symbol": sym, "right": right, "dir": direction, "units": units, "entry": entry}

    # ---------------------------------------------------------------- manage
    def _manage(self, ctx, live: list[dict], now: datetime) -> list[Signal]:
        live = self._adopt_settled(ctx)
        if not live:
            return []  # everything settled → the flat transition parks the month next slice
        out = super()._manage(ctx, live, now)
        if out:
            if self.phase == "idle":  # target/stop/trail just closed the cycle
                self.entered_month = ctx.today().strftime("%Y-%m")
            return out
        return self._maybe_roll(ctx, live, now)

    def _adopt_settled(self, ctx) -> list[dict]:
        """Settlement backstop: legs the engine already settled (a sold weekly whose roll
        couldn't fill before expiry — store hole in replay) vanish from the portfolio. Bank
        them at intrinsic-vs-spot, flag the re-sell, and drop them from ``legs`` — without
        this the base _manage's has_print gate would defer every decision forever on a dead
        symbol."""
        gone = [leg for leg in self.legs if not ctx.lots(leg["symbol"])]
        if not gone:
            return self.legs
        spot = self._index_spot(ctx)
        for leg in gone:
            k = float(leg["symbol"].split("|")[2])
            intr = 0.0
            if spot is not None:
                intr = max(0.0, k - spot) if leg["right"] == "PE" else max(0.0, spot - k)
            self.realized_rolls += (intr - leg["entry"]) * leg["units"] * leg["dir"]
            if leg["dir"] < 0:
                self.pending_resell = True
        self.legs = [leg for leg in self.legs if ctx.lots(leg["symbol"])]
        return self.legs

    def _roll_date(self, sold_expiry: date) -> date:
        """The session the sells are rolled on: ``roll_days_before`` TRADING days before the
        sold expiry (0 = expiry day itself). Trading days, not calendar days, so a Tuesday
        expiry rolls on Monday and never onto a weekend or an NSE holiday."""
        d = sold_expiry
        for _ in range(self.roll_days_before):
            d = previous_trading_day(d)
        return d

    def _maybe_roll(self, ctx, live: list[dict], now: datetime) -> list[Signal]:
        if not self.sold_expiry:
            return []
        today = ctx.today()
        se = date.fromisoformat(self.sold_expiry)
        rd = self._roll_date(se)
        # ``today > rd`` (not ``== rd``) keeps the catch-up: a roll that could not fill on
        # its day — no print, no chain — still fires on every later slice, expiry day
        # included. With roll_days_before=0 this is exactly the old expiry-day test.
        due = (
            self.pending_resell
            or today > rd
            or (today == rd and now.time() >= self.roll_time)
        )
        if not due:
            return []
        if self.max_rolls > 0 and self.roll_count >= self.max_rolls:
            sigs = self._exit_all(live, "fvc_max_rolls")
            self.entered_month = today.strftime("%Y-%m")
            return sigs

        expiries = self._listed_expiries(ctx, today)
        nxt = min((e for e in expiries if e > se and e > today), default=None)
        if nxt is None:
            return []

        # The buy leg is NEVER rolled (owner rule 2026-08-19): when the sells would land
        # on its expiry the CYCLE is over — close everything and clear the month latch so
        # a FRESH cycle (new FV read, new premium hunt) opens the next session. This also
        # structurally bounds a losing cycle at ~one buy-expiry month, where the old
        # buy-roll let one drag on for 46+ weekly rolls (run #269's 198-day cycle).
        if self.buy_expiry and nxt.isoformat() == self.buy_expiry:
            sigs = self._exit_all(live, "fvc_cycle_end")
            self.entered_month = None
            return sigs

        # ---- validate EVERYTHING before mutating (all-or-nothing roll) ----
        # The sold specs re-sell at the SAME strikes on the next weekly.
        rows_new = self._chain_rows(ctx, nxt.isoformat())
        if rows_new is None:
            return []
        try:
            nxt_lot = lot_size_for(self.underlying, nxt, overrides=self.lot_overrides)
        except KeyError:
            return []
        live_sold = {
            (leg["right"], float(leg["symbol"].split("|")[2])): leg
            for leg in self.legs
            if leg["dir"] < 0
        }
        sold_moves: list[tuple[dict | None, dict, float]] = []  # (old or None, spec, new ltp)
        for spec in self.sold_specs:
            row = rows_new.get(float(spec["strike"]))
            prem = self._ltp((row or {}).get(spec["right"].lower()))
            if prem is None or not self._oi_ok((row or {}).get(spec["right"].lower())):
                return []  # the strike must price on the next weekly → defer
            old = live_sold.get((spec["right"], float(spec["strike"])))
            if old is not None and not self._has_mark(ctx, old["symbol"]):
                return []  # buyback needs an honest mark to bank the roll
            sold_moves.append((old, spec, prem))

        # ---- mutate + emit (exits first; the new legs land in the same decision) ----
        exits: list[Signal] = []
        enters: list[Signal] = []
        for old, spec, prem in sold_moves:
            if old is not None:
                mark = ctx.close(old["symbol"])
                self.realized_rolls += (mark - old["entry"]) * old["units"] * old["dir"]
                exits.append(Signal(old["symbol"], SignalAction.EXIT_ALL, reason="fvc_roll"))
                self.legs = [x for x in self.legs if x["symbol"] != old["symbol"]]
            new_leg = self._leg(nxt, spec["strike"], spec["right"], -1,
                                spec["units"], prem, nxt_lot)
            enters.append(
                Signal(new_leg["symbol"], SignalAction.ENTER_SHORT,
                       quantity=int(new_leg["units"]), reason="fvc_roll",
                       meta={"multiplier": 1})
            )
            self.legs.append(new_leg)

        self.sold_expiry = nxt.isoformat()
        self.roll_count += 1
        self.pending_resell = False
        spot = self._index_spot(ctx)
        if spot is not None:
            self._freeze_margin(ctx, spot)  # no-op under exit_margin_basis="entry"
        return exits + enters

    def _has_mark(self, ctx, symbol: str) -> bool:
        has_print = getattr(ctx.market, "has_print", None)
        if has_print is not None and not has_print(symbol):
            return False
        try:
            ctx.close(symbol)
            return True
        except KeyError:
            return False

    # ------------------------------------------------------------ snapshot hooks
    def exit_rules(self) -> list[str]:
        mlabel = self._margin_label()  # names the manual anchor + its arithmetic when set
        rules = [
            f"Book profit at +{self.target_pct:g}% of {mlabel} on the whole cycle "
            f"(banked rolls + open MTM, {self._cadence_phrase('profit')})"
        ]
        if self.stop_pct > 0:
            rules.append(
                f"Stop out at −{self.stop_pct:g}% of {mlabel} "
                f"({self._cadence_phrase('stop')})"
            )
        when = (
            f"{self.roll_time.strftime('%H:%M')} on expiry day"
            if self.roll_days_before == 0
            else f"{self.roll_time.strftime('%H:%M')}, {self.roll_days_before} trading day"
                 f"{'s' if self.roll_days_before > 1 else ''} before the sold expiry"
        )
        rules.append(
            f"No target by the sold weekly's expiry → roll both sells to the next weekly "
            f"at the same strikes ({when})"
        )
        rules.append(
            "Sells about to land on the buy expiry → close the WHOLE cycle; a fresh one "
            "opens the next session (the buy leg is never rolled)"
        )
        if self.max_rolls > 0:
            rules.append(f"Exit all after {self.max_rolls} rolls")
        rules.append("After any exit: flat until the next calendar month's entry")
        return rules

    def basket_status(self, market, portfolio, margin: float | None = None) -> dict:
        out = super().basket_status(market, portfolio, margin)
        out.update(
            {
                "kind": "fair_value_calendar",
                "phase": "calendar" if self.legs else self.phase,
                "side": self.cycle_side,
                "side_mode": self.side_mode,
                "sold_expiry": self.sold_expiry,
                "buy_expiry": self.buy_expiry,
                "roll_count": self.roll_count,
                "entry_fv": self.entry_fv,
                "entry_dev_pct": self.entry_dev_pct,
            }
        )
        return out

    # ------------------------------------------------------- (de)serialize
    def export_state(self) -> dict:
        state = super().export_state()
        state.update(
            {
                "sold_expiry": self.sold_expiry,
                "buy_expiry": self.buy_expiry,
                "entered_month": self.entered_month,
                "roll_count": self.roll_count,
                "pending_resell": self.pending_resell,
                "sold_specs": [dict(s) for s in self.sold_specs],
                "cycle_side": self.cycle_side,
                "entry_fv": self.entry_fv,
                "entry_dev_pct": self.entry_dev_pct,
                "entry_scale": self.entry_scale,
            }
        )
        return state

    def load_state(self, state: dict) -> None:
        super().load_state(state)
        self.sold_expiry = state.get("sold_expiry")
        self.buy_expiry = state.get("buy_expiry")
        self.entered_month = state.get("entered_month")
        self.roll_count = int(state.get("roll_count", 0))
        self.pending_resell = bool(state.get("pending_resell", False))
        self.sold_specs = [dict(s) for s in state.get("sold_specs", [])]
        self.cycle_side = state.get("cycle_side")
        self.entry_fv = state.get("entry_fv")
        self.entry_dev_pct = state.get("entry_dev_pct")
        self.entry_scale = float(state.get("entry_scale", 1.0))
