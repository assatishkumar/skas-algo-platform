import { formatInr } from "./format";

// Human labels + a sensible display order for backtest input parameters.
const LABELS: Record<string, string> = {
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
  strangle_delta: "Strangle delta",
  vol_premium: "Implied/realized vol ×",
  vol_window: "Realized-vol window (days)",
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
  "max_holding_days",
  "min_vix",
  "min_dte",
  "capital_parts",
  "allocation_mode",
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
]);

const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

export function paramLabel(key: string): string {
  return LABELS[key] ?? key;
}

/** Format a parameter value for display (percent fractions, money, enums, symbol counts). */
export function formatParamValue(key: string, value: unknown): string {
  if (value == null || value === "") return "—";
  if (Array.isArray(value)) return `${value.length} symbol${value.length === 1 ? "" : "s"}`;
  if (key === "capital" && typeof value === "number") return formatInr(value);
  if (PCT_KEYS.has(key) && typeof value === "number") {
    const p = value * 100;
    return `${Number.isInteger(p) ? p : p.toFixed(1)}%`;
  }
  if (key === "max_lots" && value === 0) return "∞ (unlimited)";
  if (key === "allocation_mode") return value === "equity_scaled" ? "Equity-scaled" : "Fixed";
  if (key === "entry_weekday" && typeof value === "number") return WEEKDAYS[value] ?? String(value);
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
