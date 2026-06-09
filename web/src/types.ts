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
  strategy_id: string;
  mode: string;
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
  realized_taxes: number;
  positions: LivePosition[];
}

export interface StartLiveRequest {
  strategy_id: string;
  name?: string;
  symbols?: string[];
  universe?: string | null;
  capital: number;
  params: Record<string, unknown>;
  tax_rate: number;
  withdrawal_rate: number;
  lookback: number;
  quote_source: string;
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

export interface LiveTradeEvent {
  ticker: string;
  action: string;
  units: number;
  price: number;
  tag: string;
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
  overrides: OverrideInput[];
}
