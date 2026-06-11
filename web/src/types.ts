export interface Metrics {
  "Total Return %": number;
  "CAGR %": number;
  "Final Equity": number;
  "Max Drawdown %": number;
  "Max Capital Used": number;
  "Total Trades": number;
  "Win Rate %": number;
  "Cash Balance": number;
  "Total Withdrawals": number;
  "Total Taxes": number;
  "Avg Monthly Profit Booking": number;
  "Avg Monthly Profit (Pre-Tax)": number;
  "Avg Monthly Profit (Post-Tax)": number;
  // Net realized P&L (winners − losers). Optional: runs saved before this field exists
  // render "—".
  "Net Realized P&L"?: number;
  "Avg Monthly Net P&L (Pre-Tax)"?: number;
  "Avg Monthly Net P&L (Post-Tax)"?: number;
}

export interface YearlyRow {
  "Return (Abs)": number;
  "Return (%)": number;
  "Portfolio Value": number;
  Withdrawals: number;
  Taxes: number;
  "Max Drawdown (%)": number;
  "Max Capital Used": number;
}

export interface EquityPoint {
  date: string;
  equity: number;
  gross_equity?: number;
}

export interface BenchmarkPoint {
  date: string;
  value: number;
}

export interface CompareRun {
  run_id: number;
  name: string;
  strategy_id: string;
  params: Record<string, unknown>;
  capital: number | null;
  metrics: Metrics;
  growth: BenchmarkPoint[];
}

export interface Report {
  metrics: Metrics;
  yearly?: Record<string, YearlyRow>;
  monthly_profit?: Record<string, Record<string, number>>;
  monthly_withdrawals?: Record<string, Record<string, number>>;
  monthly_capital?: Record<string, Record<string, number>>;
  monthly_equity?: Record<string, Record<string, number>>;
  equity_curve?: EquityPoint[];
  options?: OptionsReportData; // present only for DERIV (options) runs
}

export interface Trade {
  date: string;
  ticker: string;
  action: string;
  units: number;
  price: number;
  amount: number;
  profit: number;
  pnl_pct: number;
  lots: number;
  tag: string;
  // Options-only enrichment (absent on equity trades)
  exit_reason?: string;
  entry_premium?: number;
  holding_days?: number;
}

// ---- Options report (additive; only populated for DERIV runs) ----
export interface OptionPosition {
  symbol: string;
  underlying: string;
  strike: number;
  right: "CE" | "PE";
  side?: "long" | "short";
  expiry: string;
  entry_date: string;
  entry_premium: number;
  exit_date: string;
  exit_price: number;
  exit_action: "COVER" | "SETTLE" | "SELL";
  exit_reason: string; // target | stop | expiry | manual
  units: number;
  lots: number;
  multiplier: number;
  holding_days: number;
  realized_pnl: number;
  pnl_pct: number;
  premium_collected: number;
  charges?: number;
  net_pnl?: number;
}

// underlying spot + India VIX context, added to cycles/positions post-run
export interface MarketContext {
  exit_date?: string;
  underlying_entry?: number | null;
  underlying_exit?: number | null;
  underlying_pct?: number | null;
  vix_entry?: number | null;
  vix_exit?: number | null;
}

export interface OptionCycle extends MarketContext {
  underlying: string;
  entry_date: string;
  expiry: string;
  legs: string[];
  legs_detail?: OptionPosition[]; // all legs (for multi-leg structures); ce/pe kept for straddles
  premium_collected: number;
  realized_pnl: number;
  charges?: number;
  net_pnl?: number;
  holding_days: number;
  exit_reason: string;
  ce: OptionPosition | null;
  pe: OptionPosition | null;
}

export interface ChargeBreakdown {
  brokerage: number;
  stt: number;
  exchange: number;
  sebi: number;
  stamp: number;
  gst: number;
  total: number;
}

export interface ExitReasonStat {
  count: number;
  pnl: number;
  wins: number;
  losses: number;
}

export interface OptionsReportData {
  summary: {
    total_premium_collected: number;
    total_premium_captured: number;
    premium_capture_pct: number;
    avg_holding_days: number;
    num_positions: number;
    num_cycles: number;
    winning_cycles?: number;
    win_rate_pct: number;
    max_margin_used: number;
    avg_margin_used: number;
    capital_efficiency: number;
    avg_premium_per_cycle: number;
    total_charges: number;
    net_after_charges: number;
  };
  charges?: ChargeBreakdown;
  exit_reasons: Record<string, ExitReasonStat>;
  per_expiry_cycle: {
    expiry: string;
    entries: number;
    premium_collected: number;
    realized_pnl: number;
    win: boolean;
  }[];
  positions: OptionPosition[];
  cycles: OptionCycle[];
  margin_series: { date: string; margin: number }[];
  premium_curve: { date: string; premium: number }[];
}

export interface RunSummary {
  run_id: number;
  algo_id: number;
  name: string;
  notes?: string | null;
  strategy_id: string;
  mode: string;
  archived?: boolean;
  batch_id?: string | null;
  started_at: string | null;
  metrics: Metrics;
}

export interface BacktestResponse {
  run_id: number;
  strategy_id: string;
  report: Report;
  trades: Trade[];
}

export interface OverrideInput {
  scope: string;
  target: string | null;
  rule: Record<string, unknown>;
}

export interface LivePosition {
  symbol: string;
  units: number;
  lots: number;
  avg_price: number;
  ltp: number | null;
  unrealized_pnl: number;
}

export interface LiveRunSnapshot {
  run_id: number;
  status: string;
  name: string;
  strategy_id: string;
  cash: number;
  holdings_value: number;
  equity: number;
  invested: number;
  open_positions: number;
  open_lots: number;
  parts_total: number | null;
  quote_source: string;
  on_cache_fallback?: boolean;
  realized_taxes: number;
  positions: LivePosition[];
  // live controls + exclusion editing
  auto: boolean;
  ignore_market_hours: boolean;
  refresh_seconds: number;
  decision_time: string;
  universe: string[];
  excluded_symbols: string[];
}

export interface LiveControlsInput {
  auto?: boolean;
  ignore_market_hours?: boolean;
  refresh_seconds?: number;
  excluded_symbols?: string[];
}

export interface StartLiveRequest {
  strategy_id: string;
  name?: string;
  notes?: string;
  symbols?: string[];
  universe?: string | null;
  capital: number;
  params: Record<string, unknown>;
  tax_rate: number;
  withdrawal_rate: number;
  lookback: number;
  quote_source: string;
  broker_account_id?: number | null;
  ignore_market_hours: boolean;
  auto: boolean;
}

export interface BrokerAccount {
  id: number;
  broker: string;
  label: string;
  user_id: string | null;
  armed: boolean;
  has_session: boolean;
  session_expires_at: string | null;
  live_trading_enabled: boolean;
}

export interface BrokerConnectRequest {
  broker: string;
  label: string;
  api_key: string;
  api_secret: string;
  user_id: string;
}

// Carried via router state from a backtest run into the Live "start" form.
export interface ForwardTestPrefill {
  strategy_id: string;
  name: string | null;
  capital: number | null;
  params: Record<string, unknown>; // includes symbols, lookback, tax_rate, withdrawal_rate + strategy params
}

export interface LiveTradeEvent {
  ticker: string;
  action: string;
  units: number;
  price: number;
  tag: string;
}

export interface DeploymentMetrics {
  equity?: number | null;
  cash?: number | null;
  invested?: number | null;
  open_positions?: number;
  open_lots?: number;
  parts_total?: number | null;
  unrealized_pnl?: number;
  total_return_pct?: number | null;
  total_trades?: number;
}

export interface Deployment {
  run_id: number;
  algo_id: number;
  name: string;
  notes: string | null;
  strategy_id: string;
  mode: string;
  status: "active" | "stopped" | "archived";
  quote_source: string;
  started_at: string | null;
  stopped_at: string | null;
  metrics: DeploymentMetrics;
}

export interface DataSummary {
  symbol_count: number;
  db_path: string | null;
}

export interface DataCoverage {
  instrument_class: string;
  underlying?: string | null;
  start_date: string | null;
  end_date: string | null;
}

export interface DataSymbol {
  symbol: string;
  last_date: string | null;
  stale_days: number | null;
  stale: boolean;
}

export interface DataSymbolDetail {
  symbol: string;
  start_date: string | null;
  end_date: string | null;
  total_records: number;
  yearly: { year: number; count: number }[];
  recent: { date: string; close: number }[];
}

// ---- Data tab: options & futures ----
export interface UnderlyingList {
  supported: string[];
  available: string[];
}

export interface DerivCoverage {
  symbol: string | null;
  start_date: string | null;
  end_date: string | null;
  total_records: number;
  trading_days: number;
}

export interface OptionsExpiries {
  underlying: string;
  date: string | null;
  expiries: string[];
}

export interface OptionChainLeg {
  ltp: number | null;
  close: number | null;
  oi: number | null;
  change_in_oi: number | null;
  iv?: number | null;
  delta?: number | null;
  gamma?: number | null;
  theta?: number | null;
  vega?: number | null;
}

export interface OptionChainRow {
  strike: number;
  ce: OptionChainLeg | null;
  pe: OptionChainLeg | null;
}

export interface OptionChain {
  underlying: string;
  date: string;
  expiry: string;
  spot: number | null;
  atm_strike: number | null;
  rows: OptionChainRow[];
  synthetic?: boolean; // GOLD: Black-Scholes model prices, not market premiums
}

export interface RefreshResult {
  underlyings: string[];
  days_saved: number;
  rows_saved: number;
  errors: string[];
}

export interface FuturesPoint {
  date: string | null;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  settle: number | null;
  oi: number | null;
  expiry: string | null;
}

export interface FuturesSeries {
  underlying: string;
  points: FuturesPoint[];
}

export interface WatchRow {
  symbol: string;
  ltp: number | null;
  high_20d: number | null;
  low_20d: number | null;
  tracking: boolean;
  excluded: boolean;
  held: boolean;
  lots: number;
  units: number;
  avg_price: number | null;
  unrealized_pnl: number;
  pnl_pct: number | null;
  to_breakout_pct: number | null;
  signal: string; // "BUY" | "SELL" | "" — would the next decision act?
  status: string;
}

// WebSocket message envelope from /api/v1/live/ws
export interface LiveWsMessage {
  type: "snapshot" | "trades" | "stopped";
  run_id: number;
  positions?: LivePosition[];
  cash?: number;
  equity?: number;
  events?: LiveTradeEvent[];
}

export interface Universe {
  name: string;
  label: string;
  count: number;
}

export interface BacktestRequest {
  strategy_id: string;
  symbols: string[];
  universe?: string | null;
  instrument_class?: string; // "STOCK" (default) | "DERIV"
  underlying?: string | null; // DERIV: NIFTY / BANKNIFTY
  start_date: string;
  end_date: string;
  capital: number;
  params: Record<string, unknown>;
  tax_rate: number;
  withdrawal_rate: number;
  lookback: number;
  name?: string;
  notes?: string;
  batch_id?: string;
  overrides: OverrideInput[];
}
