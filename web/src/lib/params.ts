import { formatInr } from "./format";

// Human labels + a sensible display order for backtest input parameters.
const LABELS: Record<string, string> = {
  ema_period: "EMA period",
  width_min: "Spread width min (pts)",
  width_max: "Spread width max (pts)",
  credit_min: "Net credit min (₹)",
  credit_max: "Net credit max (₹)",
  credit_ideal_lo: "Ideal credit lo (₹)",
  credit_ideal_hi: "Ideal credit hi (₹)",
  roll_days_before: "Roll days before expiry",
  expiry_switch_day: "Expiry switch day",
  capital: "Capital",
  universe: "Universe",
  symbols: "Symbols",
  start_date: "Start date",
  end_date: "End date",
  capital_parts: "Capital parts",
  allocation_mode: "Position sizing",
  profit_target: "Profit target",
  profit_target_1: "Target (1 lot)",
  profit_target_2: "Target (2 lots)",
  profit_target_3: "Target (3+ lots)",
  max_lots: "Max lots",
  lookback: "Lookback (days)",
  tax_rate: "Tax rate",
  withdrawal_rate: "Withdrawal rate",
  // options strategies
  instrument_class: "Instrument",
  underlying: "Underlying",
  strike_mode: "Strike basis",
  buy_offset: "Buy offset",
  sell_offset: "Sell offset",
  hedge_offset: "Hedge offset",
  lots: "Lots",
  credit_debit_limit_pct: "Max credit % / wing",
  combined_credit_limit_pct: "Max combined credit %",
  min_credit_pct: "Min credit %",
  tail_hedge_offset: "Tail hedge offset",
  tail_hedge_lots: "Tail hedge lots ×",
  tail_hedge_side: "Tail hedge wings",
  shift_step: "Shift step (pts)",
  max_shifts: "Max shifts",
  profit_target_pct: "Profit target",
  stop_loss_pct: "Stop loss",
  max_holding_days: "Max holding (days)",
  min_vix: "Min entry IV %",
  min_dte: "Min DTE",
  entry_weekday: "Entry weekday",
  strike_step: "Strike step (pts)",
  risk_free_rate: "Risk-free rate",
  structure: "Structure",
  dte_target: "DTE target",
  // live intraday exit cadence
  entry_time: "Entry time (IST)",
  profit_check: "Profit check",
  stop_check: "Stop check",
  time_check: "Time-exit check",
  eod_time: "EOD time (IST)",
  // hni_weekly
  buy_lots: "Buy ratio ×",
  sell_lots: "Sell ratio ×",
  hedge_lots: "Hedge ratio ×",
  exit_weekday: "Exit weekday",
  dte_tolerance: "DTE tolerance (±days)",
  margin_per_lotset: "Margin / lot-set",
  // staggered_covered_call
  etf_symbol: "ETF symbol",
  ce_otm_pct: "CE OTM %",
  tranches: "Tranches",
  rolldown_trigger_pct: "Roll-down trigger",
  rolldown_min_dte: "Roll-down min DTE",
  min_premium_pct: "Min premium % (of spot)",
  min_ce_otm_pct: "Min CE OTM % floor",
  keep_strike_above_cost: "Strike ≥ ETF cost",
  min_return_pct: "Min return % on assignment",
  covered_call_delta: "Covered-call Δ (fully covered)",
  sell_puts: "Wheel (sell puts)",
  put_otm_pct: "Put OTM %",
  strangle_delta: "Strangle delta",
  vol_premium: "Implied/realized vol ×",
  vol_window: "Realized-vol window (days)",
  // sst_weekly
  donchian_weeks: "Donchian (weeks)",
  // supertrend_momentum
  timeframe: "Timeframe",
  supertrend_period: "SuperTrend ATR period",
  supertrend_multiplier: "SuperTrend multiplier",
  partial_book_pct: "Book % at target",
  entry_mode: "Entry",
  pullback_pct: "Min pullback %",
  idle_return: "Idle cash return %/yr",
  // nifty_shop
  allocation_pct: "Allocation % / trade",
  num_candidates: "Candidates (below DMA)",
  new_buys_per_day: "New buys / day",
  avg_down_pct: "Average-down trigger",
};

const ORDER = [
  "universe",
  "symbols",
  "instrument_class",
  "underlying",
  "start_date",
  "end_date",
  "capital",
  "lots",
  "buy_lots",
  "sell_lots",
  "hedge_lots",
  "etf_symbol",
  "ce_otm_pct",
  "tranches",
  "rolldown_trigger_pct",
  "rolldown_min_dte",
  "min_premium_pct",
  "min_ce_otm_pct",
  "min_return_pct",
  "covered_call_delta",
  "sell_puts",
  "put_otm_pct",
  "strike_mode",
  "buy_offset",
  "sell_offset",
  "hedge_offset",
  "credit_debit_limit_pct",
  "combined_credit_limit_pct",
  "min_credit_pct",
  "tail_hedge_offset",
  "tail_hedge_lots",
  "tail_hedge_side",
  "profit_target_pct",
  "stop_loss_pct",
  "margin_per_lotset",
  "dte_target",
  "dte_tolerance",
  "entry_weekday",
  "exit_weekday",
  "max_holding_days",
  "min_vix",
  "min_dte",
  "allocation_pct",
  "num_candidates",
  "new_buys_per_day",
  "avg_down_pct",
  "capital_parts",
  "allocation_mode",
  "donchian_weeks",
  "timeframe",
  "supertrend_period",
  "supertrend_multiplier",
  "partial_book_pct",
  "entry_mode",
  "pullback_pct",
  "idle_return",
  "profit_target",
  "profit_target_1",
  "profit_target_2",
  "profit_target_3",
  "max_lots",
  "lookback",
  "tax_rate",
  "withdrawal_rate",
];

const PCT_KEYS = new Set([
  "tax_rate",
  "withdrawal_rate",
  "profit_target",
  "profit_target_1",
  "profit_target_2",
  "profit_target_3",
  "credit_debit_limit_pct",
  "combined_credit_limit_pct",
  "min_credit_pct",
  "profit_target_pct",
  "stop_loss_pct",
  "risk_free_rate",
  "rolldown_trigger_pct",
  "min_premium_pct",
  "allocation_pct",
  "avg_down_pct",
  "partial_book_pct",
  "pullback_pct",
  "idle_return",
]);

const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

/** Strategy ids that trade options (DERIV) — share one source of truth across the UI. */
export const OPTIONS_STRATEGIES = [
  "hni_weekly",
  "batman_ratio_monthly",
  "call_ratio_monthly",
  "put_ratio_monthly",
  "short_premium",
  "21_ema_momentum",
];

export function isOptionsStrategy(strategyId: string): boolean {
  return OPTIONS_STRATEGIES.includes(strategyId);
}

export function paramLabel(key: string): string {
  return LABELS[key] ?? key;
}

/** Format a parameter value for display (percent fractions, money, enums, symbol counts). */
export function formatParamValue(key: string, value: unknown): string {
  if (value == null || value === "") return "—";
  if (Array.isArray(value)) return `${value.length} symbol${value.length === 1 ? "" : "s"}`;
  if ((key === "capital" || key === "margin_per_lotset") && typeof value === "number")
    return formatInr(value);
  if (PCT_KEYS.has(key) && typeof value === "number") {
    const p = value * 100;
    return `${Number.isInteger(p) ? p : p.toFixed(1)}%`;
  }
  if (key === "max_lots" && value === 0) return "∞ (unlimited)";
  if (key === "allocation_mode") return value === "equity_scaled" ? "Equity-scaled" : "Fixed";
  if (key === "entry_mode") return value === "pullback" ? "Pullback breakout" : "On green flip";
  if ((key === "entry_weekday" || key === "exit_weekday") && typeof value === "number")
    return WEEKDAYS[value] ?? String(value);
  if (key === "min_vix" && value === 0) return "off";
  if (key === "tail_hedge_offset" && value === 0) return "off";
  return String(value);
}

/** Order a set of param keys for display: known keys first (in ORDER), then the rest. */
export function orderedParamKeys(keys: string[]): string[] {
  const known = ORDER.filter((k) => keys.includes(k));
  const rest = keys.filter((k) => !ORDER.includes(k)).sort();
  return [...known, ...rest];
}
