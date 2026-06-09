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
};

const ORDER = [
  "universe",
  "symbols",
  "start_date",
  "end_date",
  "capital",
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
]);

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
  return String(value);
}

/** Order a set of param keys for display: known keys first (in ORDER), then the rest. */
export function orderedParamKeys(keys: string[]): string[] {
  const known = ORDER.filter((k) => keys.includes(k));
  const rest = keys.filter((k) => !ORDER.includes(k)).sort();
  return [...known, ...rest];
}
