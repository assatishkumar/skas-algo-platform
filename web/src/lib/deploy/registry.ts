/** DEPLOY REGISTRY — every deployable strategy declared as DATA, one page renders them all.
 *
 *  WHY (owner, 2026-08-27): deploying used to happen on two pages that split on an invisible
 *  line — "does this strategy have a backtest?". Backtestable ones used the GENERIC form at
 *  /live/new (one shared layout for 17 strategies, showing knobs a given strategy doesn't
 *  have); deploy-only ones used hand-built cards on /trade. Options strategies lived on BOTH,
 *  with no way to tell which page owned one except memorising it, and no cross-link when you
 *  guessed wrong. That cost real time twice in two days (value_investing deployed PAPER by
 *  accident; strangle_combo looked "unavailable" when it simply had no card).
 *
 *  The generic form's shape was also a correctness problem, not just a UX one: strategy ctors
 *  end in `**_ignored`, so a form field that isn't a real kwarg is accepted and silently does
 *  nothing. That is the mechanism behind #279 (a run asked for equal_value sizing and traded
 *  price-weighted) and the value_investing deploy bug (form showed capital_parts/profit_target,
 *  strategy used its own defaults). Declaring fields per strategy kills the whole class.
 *
 *  This mirrors `lib/backtestV2/registry.ts`, which already solved exactly this for the
 *  backtest form — same FieldSpec shape, same "one page renders the data" idea.
 *
 *  INVARIANTS
 *  * `param` is the REAL strategy ctor kwarg (or a deploy-model field). Pinned per strategy by
 *    the model→route→ctor tests; a typo would otherwise be swallowed by `**_ignored`.
 *  * `default` is the DEPLOY default, which deliberately differs from the ctor default where
 *    policy says so (CLAUDE.md §1: a recovered deploy must be byte-identical, so new policy
 *    lives in the form, never the ctor). Examples below: intraday exits 15:20 vs ctor 15:25,
 *    cadences 1min vs ctor tick, fair-value side "ce" vs ctor "fair_value".
 *  * `route` = a strategy's own deploy endpoint. Omit → the generic `/live/start` path.
 *  * `needsBroker` → quotes must be a broker source; the backend 422s otherwise, and the form
 *    refuses to submit so the user never sees a server error for something it could tell them.
 */

export type DeployKind = "number" | "text" | "time" | "select" | "toggle";

export interface DeployField {
  param: string;
  label: string;
  kind: DeployKind;
  default: number | string | boolean;
  hint?: string;
  step?: string;
  options?: { value: string; label: string }[];
  /** Conditional reveal, evaluated against the current param values. */
  showIf?: (p: Record<string, unknown>) => boolean;
  /** Full-width in the 4-column grid (long text like a watchlist). */
  wide?: boolean;
}

/** Held-vs-flat is the thing an operator actually needs to know at a glance, so the left rail
 *  groups by it — matching the Live page's own language. */
export type CadenceGroup = "intraday" | "weekly" | "monthly" | "positional";

export interface DeploySpec {
  /** The strategy_id — also the key into lib/strategyDocs for the summary panel. */
  id: string;
  name: string;
  group: CadenceGroup;
  cadence: string;              // the tag shown on the rail card
  blurb: string;
  instrument: "DERIV" | "STOCK";
  /** Its own deploy route; omit for the generic /live/start path. */
  route?: string;
  needsBroker?: boolean;
  /** DERIV: the index picker's options. One index per deploy. */
  underlyings?: string[];
  /** STOCK: default universe key for the generic path. */
  universe?: string;
  fields: DeployField[];
  /** Always-sent params that are not user-editable. */
  fixed?: Record<string, unknown>;
  /** Shown as an amber warning on the form (live-capability caveats). */
  warn?: string;
  /** The ONE non-declarative entry. `custom_equity` is a MANUAL position (pick a symbol,
   *  size, stop, target) rather than a parameterized strategy, so it keeps its bespoke form
   *  and has no /docs card. It arguably belongs under "Build a position" — moving it there
   *  is a follow-up; leaving it here preserves the capability rather than dropping it. */
  custom?: "equity";
}

const f = (
  param: string, label: string, kind: DeployKind, def: number | string | boolean,
  rest: Partial<DeployField> = {},
): DeployField => ({ param, label, kind, default: def, ...rest });

const TIME = (param: string, label: string, def: string, hint?: string): DeployField =>
  ({ param, label, kind: "time", default: def, hint });

const CADENCES = ["tick", "1min", "5min", "15min", "30min", "60min", "eod"]
  .map((v) => ({ value: v, label: v }));

/** The two-cadence model (owner 2026-07-18). Ctor defaults stay "tick" (§1); these carry the
 *  policy — 1min for profit/adjust everywhere, 1min stops intraday, eod 15:20 positional. */
const cadence = (profit: string, stop: string): DeployField[] => [
  f("profit_check", "Profit/adjust check", "select", profit,
    { options: CADENCES, hint: "how often the profit decision samples" }),
  f("stop_check", "Stop/exit check", "select", stop,
    { options: CADENCES, hint: "how often the stop decision samples" }),
];

const FORCE = f("force_entry", "Force entry now", "toggle", false,
  { hint: "skip the entry-day/month wait and enter on the next tick" });

/** Deploy-time margin anchor. The broker's basket margin is NOT stable — the same legs priced
 *  ~₹70k/set at entry and ₹4.5L/set on expiry day with a deep-ITM short (run 15, 2026-08-25) —
 *  so a "% of margin" target means different rupees at different times unless anchored. */
const MARGIN_PER_SET = (def: number, extra?: string): DeployField =>
  f("margin_per_set", "Margin per lot-set (₹)", "number", def, {
    step: "any",
    hint: `what the % target/stop is measured against. 0 = use the broker's basket margin, which moves with moneyness${extra ? `. ${extra}` : ""}`,
  });

export const DEPLOY_REGISTRY: DeploySpec[] = [
  // ─────────────────────────────────────────────────────── intraday (flat by close)
  {
    id: "intraday_straddle",
    name: "Intraday straddle",
    group: "intraday", cadence: "DAILY",
    blurb: "Sell ATM CE+PE at 09:18, ratchet trail, flat by 15:20",
    instrument: "DERIV", needsBroker: true,
    underlyings: ["NIFTY", "BANKNIFTY"],
    route: "/trade/options/intraday-straddle/deploy",
    fields: [
      f("lots", "Lots", "number", 1),
      f("strike_delta", "Strike delta (0 = ATM)", "number", 0, {
        step: "any", hint: "0.6 → sell the ~0.6Δ ITM strike instead of ATM",
      }),
      TIME("entry_time", "Entry time", "09:18"),
      TIME("exit_time", "Exit time", "15:20",
        "Zerodha auto-squares intraday F&O at 15:26 — 15:25 leaves no room to retry"),
      f("stop_loss_pct", "Stop % of margin", "number", 2, { step: "any" }),
      f("trail_trigger_pct", "Trail trigger %", "number", 1, { step: "any" }),
      f("trail_step_pct", "Trail step %", "number", 0.5, { step: "any" }),
      f("trail_mode", "Trail mode", "select", "ratchet", {
        options: [
          { value: "ratchet", label: "Ratchet — each trigger lifts the stop" },
          { value: "below_peak", label: "Below peak — peak minus step" },
        ],
      }),
      f("leg_book_pct", "Book a melted leg at %", "number", 0, {
        step: "any", hint: "0 = off; book a leg alone once it has decayed this far",
      }),
    ],
  },
  {
    id: "intraday_strangle_combo",
    name: "Strangle combo",
    group: "intraday", cadence: "INTRADAY",
    blurb: "OTM3 CE+PE, legs managed independently, 40/70 with re-entries",
    instrument: "DERIV", needsBroker: true,
    underlyings: ["NIFTY", "SENSEX"],
    route: "/trade/options/strangle-combo/deploy",
    fields: [
      f("lots", "Lots", "number", 1),
      f("otm_steps", "OTM steps (0 = straddle)", "number", 3,
        { hint: "steps on the exchange LISTING grid — NIFTY 50, SENSEX 100" }),
      f("leg_stop_pct", "Leg stop %", "number", 40, { step: "any" }),
      f("leg_target_pct", "Leg target %", "number", 70, { step: "any" }),
      f("max_sl_reentries", "SL re-entries / side", "number", 2),
      f("max_target_reentries", "Target re-entries / side", "number", 2),
      f("mtm_stop_per_lot", "MTM stop ₹/lot (0 = off)", "number", 1500, {
        step: "any",
        hint: "day-cumulative, per index; outranks a leg stop in the same tick. Deck: NIFTY 1500, SENSEX 0",
      }),
      f("wing_steps", "Wings (grid steps, 0 = off)", "number", 0,
        { hint: ">0 buys a protective long that far beyond each short — the intraday iron fly" }),
      f("same_strike_action", "Same-strike re-entry", "select", "reenter", {
        options: [
          { value: "reenter", label: "Re-enter same strike (deck)" },
          { value: "skip", label: "Skip — flat until the strike moves" },
          { value: "hold", label: "Hold — defers the leg stop (risky)" },
        ],
      }),
      TIME("entry_time", "Entry time", "09:16"),
      TIME("exit_time", "Exit time", "15:20"),
      ...cadence("1min", "1min").slice(1),
    ],
  },
  {
    id: "momentum_theta_gainer_intra",
    name: "Intraday theta",
    group: "intraday", cadence: "INTRADAY",
    blurb: "15-min SuperTrend + daily pivots → sell ATM weeklies, flat by 15:20",
    instrument: "DERIV", needsBroker: true,
    underlyings: ["NIFTY", "SENSEX"],
    route: "/trade/options/momentum-theta/deploy",
    fields: [
      f("lots", "Lots", "number", 1),
      f("st_period", "SuperTrend period", "number", 7),
      f("st_multiplier", "SuperTrend multiplier", "number", 3, { step: "any" }),
      f("candle_minutes", "Candle minutes", "number", 15),
      f("max_trades_per_day", "Max trades / day", "number", 3),
      f("min_dte", "Min DTE", "number", 0),
    ],
    warn: "SENSEX is LIVE-ONLY — no BSE history exists, so it has no backtest at all.",
  },
  {
    id: "call_put_ratio_expiry",
    name: "CP ratio expiry",
    group: "intraday", cadence: "EXPIRY DAY",
    blurb: "Buy 1 ATM set, sell 3 at the ⅓-premium strikes, exit by 15:20",
    instrument: "DERIV", needsBroker: true,
    underlyings: ["NIFTY", "SENSEX"],
    route: "/trade/options/cp-ratio-expiry/deploy",
    fields: [
      f("sets", "Lot-sets", "number", 1),
      f("profit_target_pct", "Target % of margin", "number", 1.1, { step: "any" }),
      f("stop_loss_pct", "Stop % of margin", "number", 1, { step: "any" }),
      f("ratio_tolerance_pct", "Ratio tolerance %", "number", 30, {
        step: "any", hint: "miss beyond this → skip the day rather than trade a wrong strike",
      }),
    ],
  },
  {
    id: "asymmetric_premium_intra",
    name: "Asymmetric premium",
    group: "intraday", cadence: "INTRADAY",
    blurb: "Offset CE/PE with a ratio-triggered adjustment, flat by exit",
    instrument: "DERIV", needsBroker: true,
    underlyings: ["NIFTY"],
    fields: [
      f("lots", "Lots", "number", 1),
      f("strike_offset_steps", "Strike offset (grid steps)", "number", 2),
      f("put_expiry_offset", "Put expiry offset (weeks)", "number", 1),
      f("adjust_trigger_ratio", "Adjust trigger ratio", "number", 1.4, { step: "any" }),
      f("max_adjusts", "Max adjustments", "number", 2),
      f("stop_loss_points", "Stop (combined points)", "number", 100, { step: "any" }),
      TIME("entry_time", "Entry time", "09:20"),
      TIME("exit_time", "Exit time", "15:15"),
    ],
  },
  {
    id: "straddle_btst",
    name: "Straddle BTST",
    group: "intraday", cadence: "OVERNIGHT",
    blurb: "Buy ATM straddle 15:20, sell next session 09:20",
    instrument: "DERIV", needsBroker: true,
    underlyings: ["NIFTY"],
    fields: [
      f("lots", "Lots", "number", 1),
      TIME("entry_time", "Buy time", "15:20"),
      TIME("exit_time", "Sell time (next session)", "09:20"),
    ],
  },

  // ─────────────────────────────────────────────────────────────── weekly cycles
  {
    id: "weekly_intraday_straddle",
    name: "Weekly straddle",
    group: "weekly", cadence: "WEEKLY",
    blurb: "Strike locked per expiry cycle; VWAP + prior-day-low gate",
    instrument: "DERIV", needsBroker: true,
    underlyings: ["NIFTY"],
    route: "/trade/options/weekly-intraday-straddle/deploy",
    fields: [
      f("lots", "Lots", "number", 1),
      TIME("entry_start", "Entry from", "09:20"),
      TIME("entry_cutoff", "Entry cutoff", "15:15"),
      TIME("eod_exit", "EOD exit", "15:20"),
      f("max_entries_per_day", "Max entries / day", "number", 3),
      f("stop_loss_pct", "Stop % of margin (0 = off)", "number", 0, { step: "any" }),
    ],
    warn: "Needs option-contract 5-min bars WITH volume — a broker source only. Unfetchable "
      + "bars surface as an amber alert and disable ALL entries, including the force button.",
  },
  {
    id: "hni_weekly",
    name: "HNI weekly (1-3-2)",
    group: "weekly", cadence: "WEEKLY",
    blurb: "1-3-2 ratio structure on the weekly, margin-anchored exits",
    instrument: "DERIV",
    underlyings: ["NIFTY"],
    fields: [
      f("lots", "Lot-sets", "number", 1),
      f("profit_target_pct", "Target % of margin", "number", 2.5, { step: "any" }),
      f("stop_loss_pct", "Stop % of margin (0 = off)", "number", 0, { step: "any" }),
      ...cadence("1min", "eod"),
    ],
  },

  // ────────────────────────────────────────────────────────────── monthly cycles
  {
    id: "delta_neutral_monthly",
    name: "Delta neutral",
    group: "monthly", cadence: "MONTHLY",
    blurb: "18Δ strangle → rolls the cheap side → caps into an iron fly",
    instrument: "DERIV", needsBroker: true,
    underlyings: ["BANKNIFTY", "NIFTY", "SENSEX"],
    route: "/trade/options/delta-neutral/deploy",
    fields: [
      f("lots", "Lots", "number", 1),
      f("target_delta", "Target delta", "number", 0.18, { step: "any" }),
      f("adjust_threshold_pct", "Adjust threshold %", "number", 40, { step: "any" }),
      f("adjust_cooldown_min", "Cooldown (min)", "number", 15),
      f("profit_target_pct", "Target % of margin", "number", 2.5, { step: "any" }),
      f("stop_loss_pct", "Stop % of margin (0 = off)", "number", 0, { step: "any" }),
      FORCE,
    ],
  },
  {
    id: "iron_fly_monthly",
    name: "Iron fly",
    group: "monthly", cadence: "MONTHLY",
    blurb: "ATM fly from day one, naked untested-side adjustment",
    instrument: "DERIV", needsBroker: true,
    underlyings: ["NIFTY", "BANKNIFTY", "SENSEX"],
    route: "/trade/options/iron-fly/deploy",
    fields: [
      f("lots", "Lots", "number", 1),
      f("ironfly_adjust", "Adjust the fly", "toggle", true,
        { hint: "sell a naked ~15-20Δ short on the untested side at a breakeven breach" }),
      f("adjust_target_delta", "Adjust target delta", "number", 0.18, { step: "any" }),
      f("adjust_cooldown_min", "Cooldown (min)", "number", 15),
      f("profit_target_pct", "Target % of margin", "number", 2.5, { step: "any" }),
      f("stop_loss_pct", "Stop % of margin (0 = off)", "number", 0, { step: "any" }),
      FORCE,
    ],
    warn: "SENSEX (and BANKNIFTY beyond its ~2-month cache) needs Force entry — the "
      + "auto-trigger reads cached expiry history that does not exist for them.",
  },
  {
    id: "fair_value_calendar",
    name: "FV calendar",
    group: "monthly", cadence: "MONTHLY",
    blurb: "Premium-matched ratio calendar, weekly rolls, cycle ends at buy expiry",
    instrument: "DERIV", needsBroker: true,
    underlyings: ["NIFTY"],
    route: "/trade/options/fair-value-calendar/deploy",
    fields: [
      f("sets", "Lot-sets", "number", 1),
      MARGIN_PER_SET(134612, "~₹1.3L/set measured Aug 2026"),
      f("side_mode", "Side", "select", "ce", {
        options: [
          { value: "ce", label: "Calls only (the only positive backtest)" },
          { value: "fair_value", label: "Auto by fair value" },
          { value: "both", label: "Both sides" },
          { value: "pe", label: "Puts only" },
        ],
      }),
      f("sell_premium_1", "Sell premium 1 (₹)", "number", 150, { step: "any" }),
      f("sell_premium_2", "Sell premium 2 (₹)", "number", 450, { step: "any" }),
      f("buy_premium", "Buy premium (₹)", "number", 200, { step: "any" }),
      f("buy_lots_per_set", "Buy lots per set", "number", 3,
        { hint: "long lots per set against the 2 sold. Changing it changes the margin" }),
      f("premium_tolerance_pct", "Premium tolerance %", "number", 30, { step: "any" }),
      f("max_gap_points", "Max gap (pts)", "number", 900, { step: "any" }),
      f("min_sold_dte", "Sold leg min DTE", "number", 4),
      f("profit_target_pct", "Target % of margin", "number", 5, { step: "any" }),
      f("stop_loss_pct", "Stop % (0 = off)", "number", 0, { step: "any" }),
      TIME("entry_time", "Entry time", "09:30"),
      TIME("roll_time", "Roll time", "15:00"),
      f("roll_days_before", "Roll days before expiry", "number", 1,
        { hint: "1 = the day before — a short weekly's margin spikes into settlement" }),
      FORCE,
    ],
  },
  {
    id: "volcano_calendar",
    name: "Volcano calendar",
    group: "monthly", cadence: "MONTHLY",
    blurb: "PE butterfly + CE calendar, last-Friday entry, ±2% of margin",
    instrument: "DERIV", needsBroker: true,
    underlyings: ["NIFTY"],
    route: "/trade/options/volcano-calendar/deploy",
    fields: [
      f("lots", "Lot-sets (1 set = 5 legs)", "number", 1),
      MARGIN_PER_SET(190000, "it also anchors the 4% center-credit rule, which has no other "
        + "denominator before the order exists"),
      f("ce_offset", "CE offset from ATM (pts)", "number", 200),
      f("max_credit_pct", "Max center credit %", "number", 4, { step: "any" }),
      f("profit_target_pct", "Target % of margin", "number", 2, { step: "any" }),
      f("stop_loss_pct", "Stop % of margin", "number", 2, { step: "any" }),
      TIME("entry_time", "Entry time (last Friday)", "15:16"),
      TIME("cycle_exit_time", "Expiry-day exit", "15:15"),
      FORCE,
    ],
  },
  {
    id: "double_diagonal_calendar",
    name: "Double diagonal",
    group: "monthly", cadence: "ONE CYCLE",
    blurb: "Sell a near strangle, own a farther one as the calendar hedge",
    instrument: "DERIV", needsBroker: true,
    underlyings: ["NIFTY"],
    route: "/trade/options/double-diagonal/deploy",
    fields: [
      f("lots", "Lots", "number", 1),
      MARGIN_PER_SET(0),
      f("short_target_delta", "Short delta", "number", 0.22, { step: "any" }),
      f("hedge_target_delta", "Hedge delta", "number", 0.17, { step: "any" }),
      f("near_min_dte", "Near min DTE", "number", 4),
      f("far_min_dte", "Far min DTE", "number", 11),
      f("bias", "Bias", "select", "neutral", {
        options: [
          { value: "neutral", label: "Neutral" },
          { value: "up", label: "Up — widen calls, richen puts" },
          { value: "down", label: "Down — widen puts, richen calls" },
        ],
      }),
      f("profit_target_pct", "Target % of margin", "number", 1.5, { step: "any" }),
      f("stop_loss_pct", "Stop % of margin", "number", 1.5, { step: "any" }),
      FORCE,
    ],
    warn: "ONE cycle per deploy by design — after the exit it sits idle until you deploy again.",
  },
  {
    id: "put_condor",
    name: "Put condor",
    group: "monthly", cadence: "MONTHLY",
    blurb: "Defined-risk put condor, exits as a % of max loss",
    instrument: "DERIV", needsBroker: true,
    underlyings: ["NIFTY"],
    fields: [
      f("lots", "Lots", "number", 1),
      f("spacing", "Wing spacing (pts)", "number", 200),
      f("first_long_offset", "First long offset (pts)", "number", 300),
      f("target_pct_of_max_loss", "Target % of max loss", "number", 25, { step: "any" }),
      f("stop_pct_of_max_loss", "Stop % of max loss", "number", 50, { step: "any" }),
      f("hold_to_expiry", "Hold to expiry", "toggle", false),
      TIME("entry_time", "Entry time", "09:20"),
      FORCE,
    ],
  },
  {
    id: "batman_ratio_monthly",
    name: "Batman ratio",
    group: "monthly", cadence: "MONTHLY",
    blurb: "Two-winged ratio structure, margin-anchored target",
    instrument: "DERIV",
    underlyings: ["NIFTY"],
    fields: [
      f("lots", "Lot-sets", "number", 1),
      f("profit_target_pct", "Target % of margin", "number", 2.5, { step: "any" }),
      f("stop_loss_pct", "Stop % of margin (0 = off)", "number", 0, { step: "any" }),
      f("vol_premium_min", "Min vol premium (ATM IV − HV)", "number", 0, {
        step: "any", hint: "0 = off. ~2 was the loss study's one OOS-robust finding",
      }),
      ...cadence("1min", "eod"),
    ],
  },
  {
    id: "call_ratio_monthly",
    name: "Call ratio",
    group: "monthly", cadence: "MONTHLY",
    blurb: "Monthly call ratio spread, margin-anchored exits",
    instrument: "DERIV",
    underlyings: ["NIFTY"],
    fields: [
      f("lots", "Lot-sets", "number", 1),
      f("profit_target_pct", "Target % of margin", "number", 2.5, { step: "any" }),
      f("stop_loss_pct", "Stop % of margin (0 = off)", "number", 0, { step: "any" }),
      ...cadence("1min", "eod"),
    ],
  },
  {
    id: "put_ratio_monthly",
    name: "Put ratio",
    group: "monthly", cadence: "MONTHLY",
    blurb: "Monthly put ratio spread, margin-anchored exits",
    instrument: "DERIV",
    underlyings: ["NIFTY"],
    fields: [
      f("lots", "Lot-sets", "number", 1),
      f("profit_target_pct", "Target % of margin", "number", 2.5, { step: "any" }),
      f("stop_loss_pct", "Stop % of margin (0 = off)", "number", 0, { step: "any" }),
      ...cadence("1min", "eod"),
    ],
  },
  {
    id: "21_ema_momentum",
    name: "21-EMA momentum",
    group: "monthly", cadence: "DAILY DECISION",
    blurb: "EMA(21) band break at 15:20 → OTM credit spread, rolled pre-expiry",
    instrument: "DERIV",
    underlyings: ["NIFTY"],
    fields: [
      f("lots", "Lots", "number", 1),
      TIME("decision_time", "Decision time", "15:20"),
      f("roll_days_before", "Roll days before expiry", "number", 5),
    ],
  },

  // ──────────────────────────────────────────────────────────── positional equity
  {
    id: "value_investing",
    name: "Value investing (daily drip)",
    group: "positional", cadence: "DAILY · NEVER SELLS",
    blurb: "A fixed rupee budget into the watchlist every day; never sells",
    instrument: "STOCK", needsBroker: true, universe: "",
    fields: [
      f("daily_budget", "Daily budget (₹)", "number", 5000, { step: "any" }),
      f("fund_source", "Fund source (ETF sold to fund each day)", "text", "LIQUIDBEES"),
      f("warn_days_left", "Warn at days of runway left", "number", 2),
      f("sizing", "Sizing", "select", "equal_value", {
        options: [
          { value: "equal_value", label: "Equal value — same rupees into every name" },
          { value: "balanced", label: "Balanced — one share each, skew-capped" },
          { value: "one_share", label: "One share each (weights by SHARE PRICE)" },
        ],
        hint: "one_share put 207× more money into the priciest name over 2020-26",
      }),
      f("max_skew_pct", "Max overweight vs average %", "number", 25,
        { step: "any", showIf: (p) => p.sizing === "balanced" }),
      f("settlement_days", "Settlement days (T+1)", "number", 1, {
        hint: "an equity CNC sale's proceeds are NOT spendable the same day — buy from settled "
          + "cash and pre-sell for tomorrow. 0 = the old same-day model (backtest-only fiction)",
      }),
      f("funding_buffer_pct", "Pre-sale buffer %", "number", 10, {
        step: "any", hint: "sell this much above tomorrow's budget so a price move cannot leave it short",
      }),
      f("fund_yield_pct", "Fund source yield %/yr (reported only)", "number", 0, { step: "any" }),
      f("watchlist", "Watchlist (comma-separated; blank = every symbol)", "text", "", {
        wide: true,
        hint: "hot-editable on the running tile — the run's symbol list is not, so deploy a superset",
      }),
    ],
    fixed: { lookback: 1, tax_rate: 0 },
    warn: "Needs a BROKER quote source: on the cache source the live price IS yesterday's "
      + "close, every change reads 0.00%, and the strategy refuses the day.",
  },
  {
    id: "short_premium",
    name: "Short premium (stock)",
    group: "monthly", cadence: "MONTHLY",
    blurb: "Single-stock strangle/straddle premium selling",
    instrument: "DERIV",
    underlyings: ["NIFTY"],
    fields: [
      f("lots", "Lots", "number", 1),
      f("structure", "Structure", "select", "strangle", {
        options: [
          { value: "strangle", label: "Strangle" },
          { value: "straddle", label: "Straddle" },
        ],
      }),
      f("dte_target", "DTE target", "number", 30),
      f("strangle_delta", "Strangle delta", "number", 0.2, { step: "any" }),
      f("profit_target_pct", "Target % of premium", "number", 50, { step: "any" }),
      f("stop_loss_pct", "Stop % of premium", "number", 100, { step: "any" }),
      f("max_reentries", "Max re-entries", "number", 0),
    ],
  },
  {
    id: "staggered_covered_call",
    name: "Staggered covered call",
    group: "positional", cadence: "MONTHLY",
    blurb: "Own the ETF, sell staggered calls above cost",
    instrument: "DERIV",
    underlyings: ["NIFTY"],
    fields: [
      f("lots", "Lots", "number", 1),
      f("etf_symbol", "ETF symbol", "text", "NIFTYBEES"),
      f("ce_otm_pct", "Call OTM %", "number", 3, { step: "any" }),
      f("min_premium_pct", "Min premium %", "number", 0.5, { step: "any" }),
      f("keep_strike_above_cost", "Keep strike above cost", "toggle", true),
      f("sell_puts", "Also sell puts", "toggle", false),
    ],
  },
  {
    id: "sst_weekly",
    name: "SST weekly",
    group: "positional", cadence: "WEEKLY EOD",
    blurb: "Donchian breakout on weekly bars, LIFO booking",
    instrument: "STOCK", universe: "nifty50",
    fields: [
      f("capital_parts", "Capital parts", "number", 10),
      f("profit_target", "Profit target %", "number", 6, { step: "any" }),
      f("donchian_weeks", "Donchian weeks", "number", 20),
      f("max_lots", "Max lots (0 = ∞)", "number", 0),
    ],
  },
  {
    id: "sst_weekly_fifo",
    name: "SST weekly — FIFO",
    group: "positional", cadence: "WEEKLY EOD",
    blurb: "Weekly Donchian breakout with FIFO tiered booking",
    instrument: "STOCK", universe: "nifty50",
    fields: [
      f("profit_target_1", "Target % (1 lot)", "number", 6, { step: "any" }),
      f("profit_target_2", "Target % (2)", "number", 8, { step: "any" }),
      f("profit_target_3", "Target % (3+)", "number", 10, { step: "any" }),
    ],
  },
  {
    id: "gap_reversal",
    name: "Gap reversal",
    group: "positional", cadence: "DAILY EOD",
    blurb: "Gap-up + close above EMA(21) + RSI(10) < 10 → long, exit below EMA",
    instrument: "STOCK", universe: "nifty500mom50",
    fields: [
      f("capital_parts", "Capital parts", "number", 10),
      f("min_gap_pct", "Min gap %", "number", 1, { step: "any" }),
      f("ema_period", "EMA period", "number", 21),
      f("rsi_period", "RSI period", "number", 10),
      f("rsi_entry_below", "RSI entry below", "number", 10, { step: "any" }),
      f("rsi_source", "RSI source", "select", "ema", {
        options: [
          { value: "ema", label: "RSI of the EMA series (TradingView)" },
          { value: "close", label: "RSI of close (classic Wilder)" },
        ],
      }),
    ],
    warn: "BACKTEST-FIRST — a LIVE deploy FAILS CLOSED (no entries, no blind exits) until the "
      + "live indicator seeding lands. It will run and do nothing.",
  },
  {
    id: "happy_twins",
    name: "Happy twins",
    group: "positional", cadence: "WEEKLY EOD",
    blurb: "Dual SuperTrend — fast in, slow out, on weekly bars",
    instrument: "STOCK", universe: "nifty500mom50",
    fields: [
      f("capital_parts", "Capital parts", "number", 10),
      f("fast_period", "Fast ST period", "number", 2),
      f("fast_multiplier", "Fast ST multiplier", "number", 1, { step: "any" }),
      f("slow_period", "Slow ST period", "number", 3),
      f("slow_multiplier", "Slow ST multiplier", "number", 1, { step: "any" }),
      f("park_symbol", "Park symbol (idle capital)", "text", ""),
      f("regime_symbol", "Regime brake symbol", "text", ""),
    ],
    warn: "BACKTEST-FIRST — a LIVE deploy FAILS CLOSED (no entries) until the live indicator "
      + "seeding lands. It will run and do nothing.",
  },
  {
    id: "custom_equity",
    name: "Equity position",
    group: "positional", cadence: "MANUAL",
    blurb: "Long-only managed stock position you size yourself",
    instrument: "STOCK",
    custom: "equity",
    fields: [],
  },
  {
    id: "supertrend_momentum",
    name: "SuperTrend momentum",
    group: "positional", cadence: "DAILY EOD",
    blurb: "SuperTrend trend-follower over a momentum universe",
    instrument: "STOCK", universe: "nifty500mom50",
    fields: [
      f("capital_parts", "Capital parts", "number", 10),
      f("profit_target", "Profit target %", "number", 6, { step: "any" }),
      // NOT max_lots — this strategy has no such kwarg. The old generic form sent it to
      // every equity strategy and **_ignored ate it here; the registry test caught that.
      f("supertrend_period", "SuperTrend period", "number", 10),
      f("supertrend_multiplier", "SuperTrend multiplier", "number", 3, { step: "any" }),
      f("partial_book_pct", "Partial book %", "number", 0, { step: "any" }),
      f("allocation_mode", "Position sizing", "select", "fixed", {
        options: [
          { value: "fixed", label: "Fixed" },
          { value: "equity_scaled", label: "Equity-scaled" },
        ],
      }),
    ],
  },
  {
    id: "nifty_shop",
    name: "Nifty shop (dip buyer)",
    group: "positional", cadence: "DAILY EOD",
    blurb: "Buys dips across the index, books at a fixed target",
    instrument: "STOCK", universe: "nifty50",
    fields: [
      f("allocation_pct", "Allocation % per trade", "number", 2, { step: "any" }),
      f("profit_target", "Exit target %", "number", 5, { step: "any" }),
      f("num_candidates", "Candidates", "number", 5),
      f("new_buys_per_day", "New buys / day", "number", 1),
      f("avg_down_pct", "Average down %", "number", 5, { step: "any" }),
    ],
  },
  {
    id: "sst_lifo",
    name: "SST — LIFO",
    group: "positional", cadence: "DAILY EOD",
    blurb: "20d Donchian breakout, LIFO profit booking",
    instrument: "STOCK", universe: "nifty50",
    fields: [
      f("capital_parts", "Capital parts", "number", 10),
      f("profit_target", "Profit target %", "number", 6, { step: "any" }),
      f("max_lots", "Max lots (0 = ∞)", "number", 0),
      f("allocation_mode", "Position sizing", "select", "fixed", {
        options: [
          { value: "fixed", label: "Fixed" },
          { value: "equity_scaled", label: "Equity-scaled" },
        ],
      }),
    ],
  },
  {
    id: "sst_fifo",
    name: "SST — FIFO",
    group: "positional", cadence: "DAILY EOD",
    blurb: "20d Donchian breakout, FIFO tiered booking",
    instrument: "STOCK", universe: "nifty50",
    fields: [
      f("capital_parts", "Capital parts", "number", 10),
      f("profit_target_1", "Target % (1 lot)", "number", 6, { step: "any" }),
      f("profit_target_2", "Target % (2)", "number", 8, { step: "any" }),
      f("profit_target_3", "Target % (3+)", "number", 10, { step: "any" }),
      f("max_lots", "Max lots (0 = ∞)", "number", 0),
    ],
  },
];

export const GROUP_LABEL: Record<CadenceGroup, { title: string; note: string }> = {
  intraday: { title: "Intraday", note: "Flat by market close — no overnight exposure" },
  weekly: { title: "Weekly cycles", note: "One expiry cycle at a time" },
  monthly: { title: "Monthly cycles", note: "Held across sessions until target, stop or expiry" },
  positional: { title: "Positional equity", note: "Cash equity, held indefinitely" },
};

export const GROUP_ORDER: CadenceGroup[] = ["intraday", "weekly", "monthly", "positional"];

export function specFor(id: string): DeploySpec | undefined {
  return DEPLOY_REGISTRY.find((s) => s.id === id);
}

/** Display-value map for a spec's fields (its deploy defaults). */
export function defaultsFor(spec: DeploySpec): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const fld of spec.fields) out[fld.param] = fld.default;
  return out;
}
