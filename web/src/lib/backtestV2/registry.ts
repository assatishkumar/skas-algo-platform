/** Backtest v2 — the strategy-metadata registry the sectioned form is built from.
 *
 *  ONE source of truth mapping each designed strategy onto its REAL constructor params
 *  (verified against src/skas_algo/strategies/*.py). The form renders only knobs that
 *  actually exist: the design mocks a few that don't (fixed-strike basis, max-entries for
 *  the daily straddle, wing offset, re-hedge Δ, a target/stop BASIS select) — those are
 *  deliberately absent rather than faked, because a control that silently does nothing is
 *  worse than no control (owner call, 2026-07-17). No strategy class changes.
 *
 *  THE UNIT TRAP (CLAUDE.md): `profit_target_pct`/`stop_loss_pct` mean different things by
 *  family — FRACTIONS (0.025 = 2.5%) in the ratio family (batman/hni), WHOLE percents
 *  (2.5) in delta_neutral/iron_fly/cpre/the straddles. The form always shows whole
 *  percents; `unit: "fraction"` is the only thing that divides by 100 at build time (and
 *  multiplies by 100 when a template/clone is loaded back). Get this wrong and a 2.5%
 *  target becomes 250%.
 */

export type Basis = "intraday" | "eod";
export type Unit = "raw" | "fraction";      // "fraction" → ÷100 on build, ×100 on load
export type FieldKind = "number" | "text" | "time" | "select" | "toggle";

export interface FieldSpec {
  param: string;                 // the REAL strategy kwarg
  label: string;
  hint?: string;
  kind: FieldKind;
  unit?: Unit;
  step?: string;
  options?: { value: string; label: string }[];
  default: number | string | boolean;
  /** Conditional reveal, evaluated against the current display-unit params. */
  showIf?: (p: Record<string, unknown>) => boolean;
  /** Omit → the field applies to every basis the strategy supports. */
  bases?: Basis[];
}

export interface EntrySpec {
  /** What the strategy IS — rendered as a locked segmented control, not a knob. */
  frequency: "daily" | "weekly" | "monthly";
  frequencyHint?: string;
  fields: FieldSpec[];           // times, anchors, weekday, strike selection
}

export interface ExitSpec {
  /** Static statement of what the %s apply to — no basis param exists to bind. */
  basisNote: string;
  fields: FieldSpec[];
  /** Profit-protecting trailing stop. `unit: "fraction"` when the strategy's trail params are
   *  fractions (the ratio family); omit for whole-percent (straddles, delta_neutral). */
  trail?: { trigger: string; step: string; mode: string; defaultMode?: string; unit?: Unit };
  emptyNote?: string;            // strategies with no %-exits (ema21, mtg)
}

// eodRatio/hni collapsed into intradayHarness when the positional family moved to
// the 1-min store (2026-07-18) — ONE sizing model for every options strategy.
export type SizingVariant = "intradayHarness" | "mtg";

export interface StrategyFormSpec {
  id: string;
  bases: Basis[];
  /** Capability truth per basis — pills outside these render DISABLED (the store/cache
   *  simply has no data). FINNIFTY/MIDCPNIFTY appear nowhere today; flip here when data
   *  lands, and the form follows. */
  underlyings: Record<Basis, string[]>;
  note?: string;                 // verbatim from the design's NOTES map
  monthlyCycle?: boolean;        // drives the short-window warning
  sizing: SizingVariant;
  entry: EntrySpec;
  exit: ExitSpec;
  extras: FieldSpec[];           // section 06
  fixed?: Record<string, unknown>;  // always-sent params
}

const NONE: string[] = [];
const f = (param: string, label: string, kind: FieldKind, def: number | string | boolean,
           rest: Partial<FieldSpec> = {}): FieldSpec =>
  ({ param, label, kind, default: def, ...rest });

const TIME = (param: string, label: string, def: string, hint?: string): FieldSpec =>
  ({ param, label, kind: "time", default: def, hint });

// The two-cadence decision model (owner, 2026-07-18): every options strategy samples its
// profit/adjust decision on profit_check and its stop on stop_check. Ctor defaults stay
// "tick" (§1 — a recovered deploy is unchanged); the FORM defaults below carry the policy:
// 1min everywhere for profit/adjust, 1min for the intraday family's stops, "eod" 15:20
// for the positional family's. Hard time exits are never cadence-gated.
const CADENCE_OPTS = ["tick", "1min", "5min", "15min", "30min", "60min", "eod"]
  .map((v) => ({ value: v, label: v }));
// BACKTEST-ONLY harness escape hatch: lift the NIFTY 100-multiples rule so a replay can
// mirror pre-2026-07-14 live history (which traded 50s) or probe 50-strike variants.
// Never reaches the strategy or any LIVE path — the harness pops it.
const FIFTY = f("allow_fifty_strikes", "ALLOW 50-STRIKES", "toggle", false,
  { hint: "backtest-only: lift the NIFTY 100s rule (pre-Jul-2026 live behavior)" });

const cadenceFields = (profitDef: string, stopDef: string, eodDef: string): FieldSpec[] => [
  f("profit_check", "PROFIT/ADJUST CHECK", "select", profitDef,
    { options: CADENCE_OPTS, hint: "how often the profit/adjust decision samples" }),
  f("stop_check", "STOP/EXIT CHECK", "select", stopDef,
    { options: CADENCE_OPTS, hint: "how often the SL/exit decision samples" }),
  TIME("eod_time", "EOD CHECK TIME", eodDef, "what \"eod\" cadence means"),
];

// Which P&L the %-of-margin target/stop/trail measure (delta_neutral / iron_fly, which roll +
// bank realized). Form defaults to "total" (realized+unrealized — owner 2026-07-22); the strategy
// ctor default stays "open_legs" so a recovered deploy is unchanged (§1).
const PNL_BASIS = f("pnl_basis", "P&L BASIS", "select", "total", {
  options: [
    { value: "total", label: "Realized + unrealized" },
    { value: "open_legs", label: "Open legs only" },
  ],
  hint: "which P&L the %-of-margin target / stop / trail compare against",
});
// Which MARGIN anchors the ₹ thresholds (delta_neutral / iron_fly). "entry" = frozen once at
// cycle entry → absolute ₹ target/stop fixed from day one (owner's two-margin scheme,
// 2026-07-27 — the naked add's SPAN jump doesn't move the thresholds); "current" = re-frozen
// after every roll/hedge/add (the ctor default, so recovered deploys are unchanged — §1).
const EXIT_MARGIN_BASIS = f("exit_margin_basis", "THRESHOLD MARGIN", "select", "entry", {
  options: [
    { value: "entry", label: "Entry margin (frozen once)" },
    { value: "current", label: "Current margin (re-frozen on change)" },
  ],
  hint: "the ₹ base for target/stop/trail — frozen at entry, or re-based after each adjustment",
});
// Adjustment (roll/hedge) timing, decoupled from the profit cadence (delta_neutral / iron_fly).
const adjustTimingFields = (): FieldSpec[] => [
  f("adjust_check", "ADJUST CHECK", "select", "5min",
    { options: CADENCE_OPTS, hint: "how often the roll/hedge decision samples (own cadence)" }),
  f("adjust_after_open_min", "NO ADJUST — FIRST N MIN", "number", 5,
    { hint: "no roll/hedge in the first N min after 09:15 (deep-OTM wing strikes untraded)" }),
];

export const V2_REGISTRY: Record<string, StrategyFormSpec> = {
  // ------------------------------------------------------------------ intraday replays
  asymmetric_premium_intra: {
    id: "asymmetric_premium_intra",
    bases: ["intraday"],
    underlyings: { intraday: ["NIFTY", "SENSEX"], eod: NONE },
    sizing: "intradayHarness",
    note: "Two shorts on DIFFERENT expiries: a near-ATM CALL on the current week and a "
      + "near-ATM PUT on the next week, sold 09:30 and flat by 15:15. The adjustment is "
      + "driven by RELATIVE premium — when one leg decays to the trigger ratio of the other, "
      + "the CHEAP leg rolls to the strike on ITS OWN expiry whose premium matches the rich "
      + "one. Note that re-arms the decayed side by moving it back toward the money, so it "
      + "adds risk rather than hedging it. The stop is a COMBINED points loss for the day, "
      + "which exits everything.",
    entry: {
      frequency: "daily",
      frequencyHint: "one two-expiry pair per day",
      fields: [
        TIME("entry_time", "ENTRY TIME", "09:30"),
        f("strike_offset_steps", "STRIKE OFFSET (steps)", "number", 0,
          { hint: "0 = ATM (near-ATM in the sheet); 1 moves each leg one step OTM" }),
      ],
    },
    exit: {
      basisNote: "stop is COMBINED POINTS of loss for the day (realized + open)",
      fields: [
        f("stop_loss_points", "STOP — COMBINED POINTS", "number", 100, { step: "any",
          hint: "times the lot size in rupees, so it stays right as the lot changes; 0 = off" }),
        f("adjust_trigger_ratio", "ADJUST TRIGGER RATIO", "number", 0.5, { step: "any",
          hint: "roll the cheap leg once it decays to this fraction of the rich leg" }),
        f("max_adjusts", "MAX ADJUSTMENTS / DAY", "number", 3,
          { hint: "the sheet sets no cap; 0 disables the adjustment entirely" }),
        f("max_reentries", "RE-ENTRIES AFTER A STOP", "number", 0,
          { hint: "the sheet leaves this undefined, so it is off by default" }),
        TIME("exit_time", "EOD / FORCE EXIT", "15:15", "hard — never carried"),
        f("stop_check", "STOP CHECK", "select", "1min",
          { options: CADENCE_OPTS, hint: "how often the combined stop samples" }),
        TIME("eod_time", "EOD CHECK TIME", "15:10", "what the eod cadence means"),
      ],
    },
    extras: [
      f("adjust_tolerance_pct", "ADJUST TOLERANCE %", "number", 40, { step: "any",
        hint: "no strike within this of the target premium then skip the roll" }),
      TIME("reentry_cutoff", "RE-ENTRY CUTOFF", "14:30"),
      f("min_leg_oi", "MIN LEG OI", "number", 1),
    ],
  },

  intraday_strangle_combo: {
    id: "intraday_strangle_combo",
    bases: ["intraday"],
    underlyings: { intraday: ["NIFTY", "SENSEX"], eod: NONE },
    sizing: "intradayHarness",
    note: "Sell 1 lot CE + PE on the current weekly at 09:16, flat by 15:25 — OTM STEPS sets "
      + "the structure (3 = the deck's OTM3 strangle, 0 = an ATM straddle; both need "
      + "practically the same margin, ~Rs2.25L per NIFTY lot). The two legs are "
      + "managed INDEPENDENTLY — 40% stop / 70% target on each leg's own entry premium, and "
      + "a leg that exits re-enters at a freshly computed strike (up to 2 stop and 2 target "
      + "re-entries, separate budgets). The offset counts the exchange's LISTING grid (NIFTY 50s: spot "
      + "25000 → 24850 PE / 25150 CE), so the NIFTY-100s rule is lifted for this strategy "
      + "automatically. Index rotates by weekday: Mon NIFTY · Tue both · Wed/Thu SENSEX · Fri "
      + "NIFTY — a single-index replay therefore trades only its own days. SENSEX has ~1 month "
      + "of stored data; treat any SENSEX result as unvalidated.",
    entry: {
      frequency: "daily",
      frequencyHint: "one strangle per scheduled day, plus per-leg re-entries",
      fields: [
        TIME("entry_time", "ENTRY TIME", "09:16"),
        f("otm_steps", "STRUCTURE — OTM STEPS", "number", 3,
          { hint: "how far out both legs sit, in LISTING-grid steps (NIFTY 50s, SENSEX 100s). "
              + "3 = the deck's OTM3 strangle · 0 = an ATM STRADDLE · 1-2 = a tighter strangle" }),
        TIME("reentry_cutoff", "RE-ENTRY CUTOFF", "15:00",
             "no re-entry after this; set = exit time to disable"),
      ],
    },
    exit: {
      basisNote: "per leg: % of that leg's OWN entry premium (not margin)",
      fields: [
        f("leg_stop_pct", "LEG STOP %", "number", 40, { step: "any" }),
        f("leg_target_pct", "LEG TARGET %", "number", 70, { step: "any" }),
        f("max_sl_reentries", "MAX SL RE-ENTRIES", "number", 2, { hint: "per leg, per day" }),
        f("max_target_reentries", "MAX TARGET RE-ENTRIES", "number", 2,
          { hint: "per leg, per day — a separate budget from the SL one" }),
        f("same_strike_action", "IF THE STRIKE IS UNCHANGED", "select", "reenter",
          { options: [
              { value: "reenter", label: "Re-enter same strike (deck)" },
              { value: "skip", label: "Book it, wait for the strike to move" },
              { value: "hold", label: "Hold the leg until it can roll away" },
            ],
            hint: "spot must move half a grid step (25 pts NIFTY / 50 SENSEX) before the "
              + "strike shifts; until then a re-entry repositions nothing. \"hold\" DEFERS the leg "
              + "stop — only the overall MTM stop backstops it" }),
        // A replay covers ONE index, so a scalar is exactly right here — the ctor spreads a
        // bare number across the underlyings it was built with. Per the deck: NIFTY ₹1,500,
        // SENSEX none, so set this to 0 for a SENSEX run.
        f("mtm_stop_per_lot", "OVERALL MTM STOP ₹/LOT", "number", 1500,
          { step: "any", hint: "day P&L (realized + open) below this closes the book and stops "
            + "the day; 0 = off, as the deck specifies for SENSEX" }),
        TIME("exit_time", "EOD / FORCE EXIT", "15:25", "hard — never carried"),
        f("stop_check", "MTM STOP CHECK", "select", "1min",
          { options: CADENCE_OPTS, hint: "cadence for the OVERALL MTM stop only; the per-leg "
            + "40/70 exits are checked every tick" }),
        TIME("eod_time", "EOD CHECK TIME", "15:20", "what \"eod\" cadence means"),
      ],
    },
    extras: [
      f("min_leg_oi", "MIN LEG OI", "number", 1),
    ],
  },

  intraday_straddle: {
    id: "intraday_straddle",
    bases: ["intraday"],
    underlyings: { intraday: ["NIFTY", "BANKNIFTY", "SENSEX"], eod: NONE },
    sizing: "intradayHarness",
    entry: {
      frequency: "daily",
      frequencyHint: "one straddle per day (a stopped-out day does not re-enter)",
      fields: [
        TIME("entry_time", "ENTRY TIME", "09:18"),
        TIME("entry_window_end", "ENTRY CUTOFF", "15:00", "latest a re-deploy still enters"),
        f("strike_delta", "STRIKE Δ", "number", 0,
          { step: "any", hint: "0 = ATM straddle; ~0.6 = slightly ITM legs" }),
      ],
    },
    exit: {
      basisNote: "% of the broker basket margin, frozen at entry",
      fields: [
        f("stop_loss_pct", "STOP LOSS %", "number", 2, { step: "any" }),
        TIME("exit_time", "EOD / FORCE EXIT", "15:20",
             "hard — never waits on margin; clear of Zerodha's 15:26 intraday auto-close"),
        ...cadenceFields("1min", "1min", "15:20"),
      ],
      trail: { trigger: "trail_trigger_pct", step: "trail_step_pct", mode: "trail_mode" },
    },
    extras: [
      FIFTY,
      f("min_leg_oi", "MIN LEG OI", "number", 1),
    ],
  },

  weekly_intraday_straddle: {
    id: "weekly_intraday_straddle",
    bases: ["intraday"],
    underlyings: { intraday: ["NIFTY"], eod: NONE },
    sizing: "intradayHarness",
    note: "Weekly cycle: the ATM strike locks once at 09:20 on the first day after the prior "
      + "weekly expiry and is traded ALL week. Entries need the combined premium below both "
      + "the prior day's low and VWAP; the exit is a VWAP cross-up (no profit target).",
    entry: {
      frequency: "daily",
      frequencyHint: "traded every day of the weekly cycle, on the locked strike",
      fields: [
        TIME("entry_start", "ENTRY START", "09:20", "also the cycle strike-lock time"),
        TIME("entry_cutoff", "ENTRY CUTOFF", "15:20"),
      ],
    },
    exit: {
      basisNote: "% of the broker basket margin (the VWAP cross-up is the primary exit)",
      fields: [
        f("stop_loss_pct", "STOP LOSS %", "number", 0, { step: "any", hint: "0 = off" }),
        TIME("eod_exit", "EOD / FORCE EXIT", "15:20",
             "hard square-off — never carried; clear of the 15:26 intraday auto-close"),
        // stop cadence only: this strategy has no profit-booking decision (VWAP exits).
        f("stop_check", "STOP CHECK", "select", "1min",
          { options: CADENCE_OPTS, hint: "how often the SL samples" }),
        TIME("eod_time", "EOD CHECK TIME", "15:20", "what \"eod\" cadence means"),
      ],
    },
    extras: [
      FIFTY,
      f("max_entries_per_day", "MAX ENTRIES / DAY", "number", 3),
      f("candle_minutes", "CANDLE MINUTES", "number", 5, { hint: "the bar the signal reads" }),
      f("min_leg_oi", "MIN LEG OI", "number", 1),
    ],
  },

  call_put_ratio_expiry: {
    id: "call_put_ratio_expiry",
    bases: ["intraday"],
    underlyings: { intraday: ["NIFTY", "SENSEX"], eod: NONE },
    sizing: "intradayHarness",
    note: "Expiry-day only (NIFTY Tuesday · SENSEX Thursday): buy the ATM straddle, then sell "
      + "3 lots a side at the strikes trading nearest ⅓ of each ATM premium. Net short 2 lots "
      + "a side beyond those strikes — open-ended risk; the stop is the only guard.",
    entry: {
      frequency: "daily",
      frequencyHint: "its own expiry day only, in the entry window",
      fields: [
        TIME("entry_start", "ENTRY START", "09:20"),
        TIME("entry_end", "ENTRY CUTOFF", "09:27", "missed window → the day is skipped"),
      ],
    },
    exit: {
      basisNote: "% of the broker basket margin, frozen at entry",
      fields: [
        f("profit_target_pct", "PROFIT TARGET %", "number", 1.1, { step: "any" }),
        f("stop_loss_pct", "STOP LOSS %", "number", 1, { step: "any" }),
        TIME("eod_exit", "EOD / FORCE EXIT", "15:20"),
        ...cadenceFields("1min", "1min", "15:15"),
      ],
    },
    extras: [
      FIFTY,
      f("ratio_divisor", "RATIO DIVISOR", "number", 3, { step: "any", hint: "sell strike ≈ ATM premium ÷ this" }),
      f("ratio_tolerance_pct", "RATIO TOLERANCE %", "number", 30, { step: "any", hint: "worse → skip the day" }),
      f("sets", "SETS", "number", 1, { hint: "1 set = buy 1 + sell 3 per side" }),
      f("sell_lots_per_set", "SELL LOTS / SET", "number", 3),
      f("min_leg_oi", "MIN LEG OI", "number", 1),
    ],
  },

  delta_neutral_monthly: {
    id: "delta_neutral_monthly",
    // Intraday-store ONLY: the 18Δ entry solves delta off a LIVE chain (ctx.market
    // .live_chain), which the EOD engine's market doesn't provide — an EOD run silently
    // makes 0 trades (verified 2026-07-18). It's in _DEPLOY_ONLY for the same reason.
    bases: ["intraday"],
    underlyings: { intraday: ["NIFTY", "BANKNIFTY"], eod: NONE },
    monthlyCycle: true,
    note: "18Δ monthly strangle: when |CE−PE| exceeds the adjust threshold the CHEAP side rolls "
      + "to the rich side's price (capped at the other strike → straddle), then breakeven hedges "
      + "turn it into an iron fly. Target/stop re-freeze off the margin after every adjustment.",
    sizing: "intradayHarness",
    entry: {
      frequency: "monthly",
      frequencyHint: "entry expiry+N trading days, once per monthly cycle",
      fields: [
        f("entry_days_after_expiry", "ENTRY: EXPIRY + N DAYS", "number", 2),
        f("force_entry", "FORCE ENTRY", "toggle", true,
          { hint: "enter on the first replayed day (a short window has no expiry to anchor on)" }),
        TIME("entry_time", "ENTRY TIME", "11:00"),
        TIME("entry_window_end", "ENTRY CUTOFF", "15:00"),
        f("target_delta", "STRIKE Δ TARGET", "number", 0.18, { step: "any", hint: "each short leg" }),
      ],
    },
    exit: {
      basisNote: "% of the entry margin by default (frozen once — absolute ₹ thresholds)",
      fields: [
        f("profit_target_pct", "PROFIT TARGET %", "number", 2.5, { step: "any" }),
        f("stop_loss_pct", "STOP LOSS %", "number", 0, { step: "any", hint: "0 = off" }),
        PNL_BASIS,
        EXIT_MARGIN_BASIS,
        ...cadenceFields("1min", "1min", "15:20"),
      ],
      trail: { trigger: "trail_trigger_pct", step: "trail_step_pct", mode: "trail_mode",
               defaultMode: "off" },
    },
    extras: [
      FIFTY,
      f("adjust_threshold_pct", "ADJUST THRESHOLD %", "number", 40, { step: "any", hint: "|CE−PE| vs (CE+PE)" }),
      f("adjust_cooldown_min", "ADJUST COOLDOWN (MIN)", "number", 15),
      ...adjustTimingFields(),
      f("ironfly_adjust", "IRON-FLY ADJUSTMENT", "toggle", false,
        { hint: "on a breakeven breach, sell ~15-20Δ on the untested side" }),
      f("adjust_target_delta", "ADJUST Δ TARGET", "number", 0.175,
        { step: "any", showIf: (p) => !!p.ironfly_adjust }),
      f("adjust_close_delta", "ADJUST CLOSE Δ", "number", 0.1,
        { step: "any", showIf: (p) => !!p.ironfly_adjust }),
      f("min_leg_oi", "MIN LEG OI", "number", 1),
    ],
  },

  iron_fly_monthly: {
    id: "iron_fly_monthly",
    bases: ["intraday"],
    underlyings: { intraday: ["NIFTY", "BANKNIFTY"], eod: NONE },
    monthlyCycle: true,
    note: "Monthly-cycle strategy — entries on the monthly anchor day; one combined "
      + "target/stop/time exit for the fly.",
    sizing: "intradayHarness",
    entry: {
      frequency: "monthly",
      frequencyHint: "entry expiry+N trading days, once per monthly cycle",
      fields: [
        f("entry_days_after_expiry", "ENTRY: EXPIRY + N DAYS", "number", 2),
        f("force_entry", "FORCE ENTRY", "toggle", true,
          { hint: "enter on the first replayed day (a short window has no expiry to anchor on)" }),
        TIME("entry_time", "ENTRY TIME", "11:00"),
        TIME("entry_window_end", "ENTRY CUTOFF", "15:00"),
      ],
    },
    exit: {
      basisNote: "% of the ENTRY fly margin by default (frozen once — the naked add's margin jump never moves the ₹ thresholds)",
      fields: [
        f("profit_target_pct", "PROFIT TARGET %", "number", 2.5, { step: "any" }),
        f("stop_loss_pct", "STOP LOSS %", "number", 0, { step: "any", hint: "0 = off" }),
        PNL_BASIS,
        EXIT_MARGIN_BASIS,
        ...cadenceFields("1min", "1min", "15:20"),
      ],
      trail: { trigger: "trail_trigger_pct", step: "trail_step_pct", mode: "trail_mode",
               defaultMode: "off" },
    },
    extras: [
      FIFTY,
      f("adjust_target_delta", "ADJUST Δ TARGET", "number", 0.175, { step: "any" }),
      f("adjust_close_delta", "ADJUST CLOSE Δ", "number", 0.1, { step: "any" }),
      f("adjust_cooldown_min", "ADJUST COOLDOWN (MIN)", "number", 15),
      ...adjustTimingFields(),
      f("min_leg_oi", "MIN LEG OI", "number", 1),
    ],
    fixed: { ironfly_adjust: true },   // the whole point of this subclass
  },

  // ------------------------------------------------------------------- BS service
  momentum_theta_gainer_intra: {
    id: "momentum_theta_gainer_intra",
    bases: ["intraday"],
    underlyings: { intraday: ["NIFTY"], eod: NONE },
    note: "Premiums are synthetic Black-Scholes off real 15-min spot bars (long history OK — "
      + "does not use the 1-min option store). Calibrate vol_multiplier on /research.",
    sizing: "mtg",
    entry: {
      frequency: "daily",
      frequencyHint: "15-min SuperTrend flips + daily pivots",
      fields: [
        TIME("entry_cutoff", "ENTRY CUTOFF", "15:00", "no fresh shorts after"),
      ],
    },
    exit: {
      basisNote: "signal-driven (SuperTrend flip / pivot) — no %-target or stop",
      fields: [TIME("eod_exit", "EOD / FORCE EXIT", "15:20")],
      emptyNote: "This strategy exits on its own signal (SuperTrend flip or pivot) and at the "
        + "EOD time — it has no percentage target or stop.",
    },
    // no FIFTY: mtg's backtest is the BS service (computed strikes) — the harness flag can't reach it
    extras: [
      f("st_period", "ST PERIOD", "number", 7),
      f("st_multiplier", "ST MULTIPLIER", "number", 3, { step: "any" }),
      f("candle_minutes", "CANDLE MINUTES", "number", 15),
      f("max_trades_per_day", "MAX TRADES / DAY", "number", 3),
      f("vol_multiplier", "VOL MULTIPLIER", "number", 1.1, { step: "any", hint: "calibrate on /research" }),
      f("slippage_bps", "SLIPPAGE (BPS)", "number", 5, { step: "any", hint: "against us, both sides" }),
      f("min_dte", "MIN DTE", "number", 0, { hint: "0 = 0DTE allowed" }),
    ],
  },

  // ------------------------------------------------------------------- EOD engine
  "21_ema_momentum": {
    id: "21_ema_momentum",
    // 1-min store (2026-07-18): real minute premiums at the 15:20 decision; the EMA
    // bands read cache daily bars for PRIOR days + a FORMING bar for today (no settled-
    // bar lookahead — owner veto). The EOD options basis left the UI.
    bases: ["intraday"],
    underlyings: { intraday: ["NIFTY"], eod: NONE },
    note: "Checked once/day at 15:20: close above the EMA-high band → bull put spread; below the "
      + "EMA-low band → bear call spread; holds until the opposite signal.",
    sizing: "intradayHarness",
    entry: {
      frequency: "daily",
      frequencyHint: "one decision a day, at the decision time",
      fields: [
        TIME("decision_time", "DECISION TIME", "15:20"),
        f("strike_step", "STRIKE STEP", "number", 100, { hint: "NIFTY: 100s only" }),
      ],
    },
    exit: {
      basisNote: "signal-driven — holds until the opposite signal, rolling before expiry",
      fields: [
        f("roll_days_before", "ROLL (DAYS BEFORE EXPIRY)", "number", 5),
      ],
      emptyNote: "No percentage target or stop: the position is held until the opposite EMA "
        + "signal fires, and rolled before expiry.",
    },
    extras: [
      FIFTY,
      f("ema_period", "EMA PERIOD", "number", 21, { hint: "high/low channel" }),
      f("width_min", "SPREAD WIDTH MIN (PTS)", "number", 300),
      f("width_max", "SPREAD WIDTH MAX (PTS)", "number", 500),
      f("credit_min", "NET CREDIT MIN (₹/SH)", "number", 80, { step: "any" }),
      f("credit_max", "NET CREDIT MAX (₹/SH)", "number", 140, { step: "any" }),
      f("credit_ideal_lo", "CREDIT IDEAL LO", "number", 90, { step: "any" }),
      f("credit_ideal_hi", "CREDIT IDEAL HI", "number", 130, { step: "any" }),
      f("expiry_switch_day", "EXPIRY SWITCH DAY", "number", 15, { hint: "before the 15th → current month" }),
    ],
  },

  call_ratio_monthly: {
    id: "call_ratio_monthly",
    bases: ["intraday"],
    underlyings: { intraday: ["NIFTY", "BANKNIFTY"], eod: NONE },
    monthlyCycle: true,
    note: "1:2 call ratio spread + far hedge on the next monthly — zero DOWNSIDE risk (all "
      + "calls), upside capped by the hedge; enters only when the credit gate passes.",
    sizing: "intradayHarness",
    entry: {
      frequency: "monthly",
      frequencyHint: "one entry per monthly cycle, zero adjustments",
      fields: [
        TIME("entry_time", "ENTRY TIME", "14:30",
          "owner default for the monthly family (ctor: any time — None)"),
        f("entry_rule", "ENTRY ANCHOR", "select", "last_weekday", {
          options: [
            { value: "last_weekday", label: "Last weekday of month" },
            { value: "post_expiry", label: "Expiry + n days" },
          ],
        }),
        f("entry_weekday", "ANCHOR WEEKDAY", "select", "1", {
          options: [
            { value: "0", label: "Monday" }, { value: "1", label: "Tuesday" },
            { value: "2", label: "Wednesday" }, { value: "3", label: "Thursday" },
            { value: "4", label: "Friday" },
          ],
          showIf: (p) => p.entry_rule === "last_weekday",
        }),
        f("entry_window_days", "N — DAYS AFTER EXPIRY", "number", 7, {
          hint: "retry window after the expiry anchor",
          showIf: (p) => p.entry_rule === "post_expiry",
        }),
        f("strike_mode", "STRIKE BY", "select", "points", {
          options: [
            { value: "points", label: "Points from spot" },
            { value: "percent", label: "% OTM" },
            { value: "delta", label: "Delta" },
            { value: "sd", label: "× expected move (SD)" },
          ],
        }),
        f("buy_offset", "BUY OFFSET (NEAR, ×1)", "number", 300, { step: "any" }),
        f("sell_offset", "SELL OFFSET (BODY, ×2)", "number", 600, { step: "any" }),
        f("hedge_offset", "HEDGE OFFSET (FAR)", "number", 1600, { step: "any", hint: "caps the wing" }),
        f("min_dte", "MIN DTE", "number", 18, { hint: "selects next month's monthly" }),
      ],
    },
    exit: {
      basisNote: "% of account CAPITAL (this family sizes its thresholds off capital, not margin)",
      fields: [
        f("profit_target_pct", "PROFIT TARGET %", "number", 2.5, { step: "any", unit: "fraction" }),
        f("stop_loss_pct", "STOP LOSS %", "number", 3, { step: "any", unit: "fraction" }),
        f("max_holding_days", "MAX HOLDING DAYS", "number", 20),
        ...cadenceFields("1min", "eod", "15:20"),
      ],
      trail: { trigger: "trail_trigger_pct", step: "trail_step_pct", mode: "trail_mode",
               defaultMode: "off", unit: "fraction" },
    },
    extras: [
      FIFTY,
      f("credit_debit_limit_pct", "MAX CREDIT %", "number", 1, { step: "any", unit: "fraction" }),
      f("min_credit_pct", "MIN CREDIT %", "number", 0, { step: "any", unit: "fraction", hint: "negative allows a debit" }),
      f("min_vix", "MIN ENTRY IV %", "number", 0, { step: "any", hint: "≈VIX; 0 = off" }),
      f("vol_premium_min", "VOL-PREMIUM MIN (IV−HV)", "number", 0,
        { step: "any", hint: "skip entry if ATM-IV − HV20 < this (vol pts); loss-study ≈2; 0 = off" }),
      f("tail_hedge_offset", "TAIL HEDGE OFFSET", "number", 0, { step: "any", hint: "0 = off" }),
      f("shift_step", "SHIFT STEP", "number", 100, { step: "any" }),
      f("max_shifts", "MAX SHIFTS", "number", 10),
    ],
  },

  put_ratio_monthly: {
    id: "put_ratio_monthly",
    bases: ["intraday"],
    underlyings: { intraday: ["NIFTY", "BANKNIFTY"], eod: NONE },
    monthlyCycle: true,
    note: "1:2 PUT ratio spread + far hedge — the downside mirror: zero UPSIDE risk; risk is "
      + "a fast sell-off toward the short strikes, capped beyond the hedge.",
    sizing: "intradayHarness",
    entry: {
      frequency: "monthly",
      frequencyHint: "one entry per monthly cycle, zero adjustments",
      fields: [
        TIME("entry_time", "ENTRY TIME", "14:30",
          "owner default for the monthly family (ctor: any time — None)"),
        f("entry_rule", "ENTRY ANCHOR", "select", "last_weekday", {
          options: [
            { value: "last_weekday", label: "Last weekday of month" },
            { value: "post_expiry", label: "Expiry + n days" },
          ],
        }),
        f("entry_weekday", "ANCHOR WEEKDAY", "select", "1", {
          options: [
            { value: "0", label: "Monday" }, { value: "1", label: "Tuesday" },
            { value: "2", label: "Wednesday" }, { value: "3", label: "Thursday" },
            { value: "4", label: "Friday" },
          ],
          showIf: (p) => p.entry_rule === "last_weekday",
        }),
        f("entry_window_days", "N — DAYS AFTER EXPIRY", "number", 7, {
          hint: "retry window after the expiry anchor",
          showIf: (p) => p.entry_rule === "post_expiry",
        }),
        f("strike_mode", "STRIKE BY", "select", "points", {
          options: [
            { value: "points", label: "Points from spot" },
            { value: "percent", label: "% OTM" },
            { value: "delta", label: "Delta" },
            { value: "sd", label: "× expected move (SD)" },
          ],
        }),
        f("buy_offset", "BUY OFFSET (NEAR, ×1)", "number", 300, { step: "any" }),
        f("sell_offset", "SELL OFFSET (BODY, ×2)", "number", 600, { step: "any" }),
        f("hedge_offset", "HEDGE OFFSET (FAR)", "number", 1600, { step: "any", hint: "caps the wing" }),
        f("min_dte", "MIN DTE", "number", 18, { hint: "selects next month's monthly" }),
      ],
    },
    exit: {
      basisNote: "% of account CAPITAL (this family sizes its thresholds off capital, not margin)",
      fields: [
        f("profit_target_pct", "PROFIT TARGET %", "number", 2.5, { step: "any", unit: "fraction" }),
        f("stop_loss_pct", "STOP LOSS %", "number", 3, { step: "any", unit: "fraction" }),
        f("max_holding_days", "MAX HOLDING DAYS", "number", 20),
        ...cadenceFields("1min", "eod", "15:20"),
      ],
      trail: { trigger: "trail_trigger_pct", step: "trail_step_pct", mode: "trail_mode",
               defaultMode: "off", unit: "fraction" },
    },
    extras: [
      FIFTY,
      f("credit_debit_limit_pct", "MAX CREDIT %", "number", 1, { step: "any", unit: "fraction" }),
      f("min_credit_pct", "MIN CREDIT %", "number", 0, { step: "any", unit: "fraction", hint: "negative allows a debit" }),
      f("min_vix", "MIN ENTRY IV %", "number", 0, { step: "any", hint: "≈VIX; 0 = off" }),
      f("vol_premium_min", "VOL-PREMIUM MIN (IV−HV)", "number", 0,
        { step: "any", hint: "skip entry if ATM-IV − HV20 < this (vol pts); loss-study ≈2; 0 = off" }),
      f("tail_hedge_offset", "TAIL HEDGE OFFSET", "number", 0, { step: "any", hint: "0 = off" }),
      f("shift_step", "SHIFT STEP", "number", 100, { step: "any" }),
      f("max_shifts", "MAX SHIFTS", "number", 10),
    ],
  },

  batman_ratio_monthly: {
    id: "batman_ratio_monthly",
    bases: ["intraday"],
    underlyings: { intraday: ["NIFTY", "BANKNIFTY"], eod: NONE },
    monthlyCycle: true,
    note: "Batman: BOTH 1:2 ratio wings (call above + put below spot, each hedged; 6 legs). Both "
      + "wings must qualify for credit or the month is skipped; risk = a fast move either way.",
    sizing: "intradayHarness",
    entry: {
      frequency: "monthly",
      frequencyHint: "one entry per monthly cycle, zero adjustments",
      fields: [
        TIME("entry_time", "ENTRY TIME", "14:30",
          "owner default for the monthly family (ctor: any time — None)"),
        f("entry_rule", "ENTRY ANCHOR", "select", "last_weekday", {
          options: [
            { value: "last_weekday", label: "Last weekday of month" },
            { value: "post_expiry", label: "Expiry + n days" },
          ],
        }),
        f("entry_weekday", "ANCHOR WEEKDAY", "select", "1", {
          options: [
            { value: "0", label: "Monday" }, { value: "1", label: "Tuesday" },
            { value: "2", label: "Wednesday" }, { value: "3", label: "Thursday" },
            { value: "4", label: "Friday" },
          ],
          showIf: (p) => p.entry_rule === "last_weekday",
        }),
        f("entry_window_days", "N — DAYS AFTER EXPIRY", "number", 7, {
          hint: "retry window after the expiry anchor",
          showIf: (p) => p.entry_rule === "post_expiry",
        }),
        f("strike_mode", "STRIKE BY", "select", "points", {
          options: [
            { value: "points", label: "Points from spot" },
            { value: "percent", label: "% OTM" },
            { value: "delta", label: "Delta" },
            { value: "sd", label: "× expected move (SD)" },
          ],
        }),
        f("buy_offset", "BUY OFFSET (NEAR, ×1)", "number", 300, { step: "any" }),
        f("sell_offset", "SELL OFFSET (BODY, ×2)", "number", 600, { step: "any" }),
        f("hedge_offset", "HEDGE OFFSET (FAR)", "number", 1600, { step: "any", hint: "caps the wing" }),
        f("min_dte", "MIN DTE", "number", 18, { hint: "selects next month's monthly" }),
      ],
    },
    exit: {
      basisNote: "% of account CAPITAL (this family sizes its thresholds off capital, not margin)",
      fields: [
        f("profit_target_pct", "PROFIT TARGET %", "number", 2.5, { step: "any", unit: "fraction" }),
        f("stop_loss_pct", "STOP LOSS %", "number", 3, { step: "any", unit: "fraction" }),
        f("max_holding_days", "MAX HOLDING DAYS", "number", 20),
        ...cadenceFields("1min", "eod", "15:20"),
      ],
      trail: { trigger: "trail_trigger_pct", step: "trail_step_pct", mode: "trail_mode",
               defaultMode: "off", unit: "fraction" },
    },
    extras: [
      FIFTY,
      f("credit_debit_limit_pct", "MAX CREDIT % / WING", "number", 1, { step: "any", unit: "fraction" }),
      f("combined_credit_limit_pct", "MAX COMBINED CREDIT %", "number", 2, { step: "any", unit: "fraction" }),
      f("min_credit_pct", "MIN CREDIT %", "number", 0, { step: "any", unit: "fraction", hint: "negative allows a debit" }),
      f("min_vix", "MIN ENTRY IV %", "number", 0, { step: "any", hint: "≈VIX; 0 = off" }),
      f("vol_premium_min", "VOL-PREMIUM MIN (IV−HV)", "number", 0,
        { step: "any", hint: "skip entry if ATM-IV − HV20 < this (vol pts); loss-study ≈2; 0 = off" }),
      f("tail_hedge_offset", "TAIL HEDGE OFFSET", "number", 2100, { step: "any", hint: "0 = off" }),
      f("tail_hedge_lots", "TAIL HEDGE LOTS ×", "number", 0.5, { step: "any" }),
      f("tail_hedge_side", "TAIL HEDGE SIDE", "select", "put", {
        options: [{ value: "both", label: "Both wings" }, { value: "call", label: "Call" },
                  { value: "put", label: "Put" }],
      }),
      f("shift_step", "SHIFT STEP", "number", 100, { step: "any" }),
      f("max_shifts", "MAX SHIFTS", "number", 10),
      // margin_per_lotset superseded by the harness margin_per_lot (Sizing section).
    ],
  },

  hni_weekly: {
    id: "hni_weekly",
    bases: ["intraday"],
    underlyings: { intraday: ["NIFTY"], eod: NONE },
    note: "HNI Weekly: net-zero 1-3-2 call ratio \"tent\" on the ~8-DTE weekly (enter Monday, "
      + "force-exit Friday; no weekend carry). Target/stop are % of deployed margin.",
    sizing: "intradayHarness",
    entry: {
      frequency: "weekly",
      frequencyHint: "one trade per ISO week",
      fields: [
        f("entry_weekday", "ENTRY WEEKDAY", "select", "0", {
          options: [
            { value: "0", label: "Monday" }, { value: "1", label: "Tuesday" },
            { value: "2", label: "Wednesday" }, { value: "3", label: "Thursday" },
            { value: "4", label: "Friday" },
          ],
        }),
        TIME("entry_time", "ENTRY TIME", "09:45"),
        f("dte_target", "DTE TARGET", "number", 8, { hint: "8 = next Tuesday's weekly" }),
        f("buy_offset", "BUY OFFSET (PTS OTM)", "number", 200, { step: "any" }),
        f("sell_offset", "SELL OFFSET (PTS OTM)", "number", 400, { step: "any" }),
        f("hedge_offset", "HEDGE OFFSET (PTS OTM)", "number", 600, { step: "any" }),
      ],
    },
    exit: {
      basisNote: "% of DEPLOYED MARGIN (this strategy overrides the family's capital basis)",
      fields: [
        f("profit_target_pct", "PROFIT TARGET %", "number", 1, { step: "any", unit: "fraction" }),
        f("stop_loss_pct", "STOP LOSS %", "number", 1, { step: "any", unit: "fraction" }),
        f("exit_weekday", "FORCE-EXIT WEEKDAY", "select", "4", {
          options: [
            { value: "1", label: "Tuesday" }, { value: "2", label: "Wednesday" },
            { value: "3", label: "Thursday" }, { value: "4", label: "Friday" },
          ],
          hint: "no weekend carry",
        }),
        ...cadenceFields("1min", "eod", "15:20"),
      ],
      trail: { trigger: "trail_trigger_pct", step: "trail_step_pct", mode: "trail_mode",
               defaultMode: "off", unit: "fraction" },
    },
    extras: [
      FIFTY,
      f("buy_lots", "BUY RATIO × (NEAR)", "number", 1),
      f("sell_lots", "SELL RATIO × (BODY)", "number", 3),
      f("hedge_lots", "HEDGE RATIO × (FAR)", "number", 2),
      f("dte_tolerance", "DTE TOLERANCE", "number", 3, { hint: "no ~8-DTE weekly → skip the week" }),
      f("vol_premium_min", "VOL-PREMIUM MIN (IV−HV)", "number", 0,
        { step: "any", hint: "skip entry if ATM-IV − HV20 < this (vol pts); 0 = off" }),
    ],
  },
};

export const isV2Strategy = (id: string): boolean => id in V2_REGISTRY;

/** The UI's trail selector value ("off" | "ratchet" | "below_peak") — a form-only key that
 *  never reaches the strategy: "off" is expressed as trigger/step = 0 at build time. */
export const TRAIL_UI = "__trail_ui";

/** Every field the form may render for a strategy (entry + exit + trail + extras).
 *  Trail fields hang off the strategy's real param names and hide when the UI says off. */
export function allFields(spec: StrategyFormSpec): FieldSpec[] {
  const t = spec.exit.trail;
  const trail: FieldSpec[] = t
    ? [
        { param: t.trigger, label: "TRIGGER %", kind: "number", default: 1, step: "any",
          unit: t.unit, showIf: (p) => p[TRAIL_UI] !== "off" },
        { param: t.step, label: "STEP %", kind: "number", default: 0.5, step: "any",
          unit: t.unit, showIf: (p) => p[TRAIL_UI] !== "off" },
        { param: t.mode, label: "TRAIL MODE", kind: "select", default: "ratchet",
          options: [{ value: "ratchet", label: "Ratchet" }, { value: "below_peak", label: "Trail" }],
          showIf: (p) => p[TRAIL_UI] !== "off" },
      ]
    : [];
  return [...spec.entry.fields, ...spec.exit.fields, ...trail, ...spec.extras];
}

/** Display-unit defaults for a strategy on a basis (whole percents everywhere). */
export function defaultParams(spec: StrategyFormSpec, basis: Basis): Record<string, number | string | boolean> {
  const out: Record<string, number | string | boolean> = {};
  for (const fld of allFields(spec)) {
    if (fld.bases && !fld.bases.includes(basis)) continue;
    out[fld.param] = fld.default;
  }
  // The trail selector defaults to the strategy's own default mode (its trail is ON by
  // default — trail_trigger_pct 1 / step 0.5), matching a blank v1 run.
  if (spec.exit.trail) out[TRAIL_UI] = String(spec.exit.trail.defaultMode ?? "ratchet");
  return out;
}

/** Fields visible right now: basis-applicable and passing their showIf. */
export function visibleFields(fields: FieldSpec[], basis: Basis,
                              params: Record<string, unknown>): FieldSpec[] {
  return fields.filter((fld) => (!fld.bases || fld.bases.includes(basis))
    && (!fld.showIf || fld.showIf(params)));
}
