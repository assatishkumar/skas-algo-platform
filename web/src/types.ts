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

export interface Report {
  metrics: Metrics;
  yearly?: Record<string, YearlyRow>;
  monthly_profit?: Record<string, Record<string, number>>;
  monthly_withdrawals?: Record<string, Record<string, number>>;
  monthly_capital?: Record<string, Record<string, number>>;
  monthly_equity?: Record<string, Record<string, number>>;
  equity_curve?: EquityPoint[];
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
}

export interface RunSummary {
  run_id: number;
  algo_id: number;
  name: string;
  notes?: string | null;
  strategy_id: string;
  mode: string;
  archived?: boolean;
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
  realized_taxes: number;
  positions: LivePosition[];
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

export interface WatchRow {
  symbol: string;
  ltp: number | null;
  high_20d: number | null;
  low_20d: number | null;
  tracking: boolean;
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
  start_date: string;
  end_date: string;
  capital: number;
  params: Record<string, unknown>;
  tax_rate: number;
  withdrawal_rate: number;
  lookback: number;
  name?: string;
  notes?: string;
  overrides: OverrideInput[];
}
