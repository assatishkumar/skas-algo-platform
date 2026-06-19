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
  // Deployed-capital + idle-cash overlay (present only for opt-in strategies, e.g. SuperTrend).
  "Avg Deployed Capital"?: number;
  "Return on Deployed Capital %"?: number;
  "Deployed Return %/yr"?: number;
  "Idle Interest (assumed)"?: number;
  "CAGR (idle @ 6%) %"?: number;
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

// Slim per-cycle row for the options compare view (aligned by entry month).
export interface CompareCycle extends Omit<MarketContext, "exit_date"> {
  entry_date: string;
  exit_date: string | null;
  expiry: string;
  exit_reason: string;
  holding_days: number;
  premium_collected: number;
  realized_pnl: number;
  charges?: number;
  net_pnl?: number;
  n_legs: number;
}

export interface CompareOptions {
  summary: OptionsReportData["summary"];
  charges: ChargeBreakdown;
  exit_reasons: Record<string, ExitReasonStat>;
  cycles: CompareCycle[];
}

export interface CompareRun {
  run_id: number;
  name: string;
  strategy_id: string;
  params: Record<string, unknown>;
  capital: number | null;
  metrics: Metrics;
  growth: BenchmarkPoint[];
  options?: CompareOptions;
}

export interface StrategyTemplate {
  strategy_id: string;
  run_id: number | null;
  name: string | null;
  capital: number;
  params: Record<string, unknown>;
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

// ---- Trade analysis ----
export interface AnalysisRunItem {
  run_id: number;
  name: string | null;
  strategy_id: string | null;
  instrument_class: string; // "STOCK" | "DERIV"
  mode: string;
  status: string; // "backtest" | "active" | "stopped" | "archived"
}

export interface RunAnalysis {
  run_id: number;
  name: string | null;
  strategy_id: string | null;
  instrument_class: string;
  params: Record<string, unknown>;
  capital: number | null;
  trades: Trade[];
}

export interface StockSeriesPoint {
  date: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  supertrend?: number; // SuperTrend line (when overlay requested)
  direction?: number; // +1 green / −1 red
}

export interface StockSeries {
  symbol: string;
  points: StockSeriesPoint[];
}

export interface RoundTripExit {
  date: string;
  price: number;
  units: number;
  tag: string;
}

export interface RoundTrip {
  symbol: string;
  entryDate: string;
  entryPrice: number;
  qty: number;
  exits: RoundTripExit[];
  exitDate: string;
  pnl: number;
  pnlPct: number;
  holdingDays: number;
  won: boolean;
}

/** A position that has been entered but not yet fully exited (still held). */
export interface OpenPosition {
  symbol: string;
  entryDate: string;
  entryPrice: number;
  qty: number;
  invested: number;
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

// Covered-leg (equity) tranche buy + round-trip — the ETF bought against a sold call.
export interface EquityTranche {
  date: string;
  units: number;
  price: number;
  tag: string;
}

export interface EquityLeg {
  symbol: string;
  side: "equity" | "equity_open";
  entry_date: string;
  entry_price: number;
  exit_date?: string;
  exit_price?: number;
  exit_reason?: string;
  units: number;
  realized_pnl?: number;
  holding_days?: number;
  mark?: number | null;
  unrealized_pnl?: number | null;
  tranches: EquityTranche[];
}

// A call sold (or rolled) during a covered-call campaign.
export interface CampaignCall {
  entry_date: string;
  strike: number;
  entry_premium: number;
  exit_date: string;
  exit_price: number;
  exit_reason: string;
  premium_collected: number;
  realized_pnl: number;
  net_pnl: number;
}

// One accumulation→called-away campaign (or the still-open holding).
export interface Campaign {
  start: string;
  end: string | null;
  status: "called_away" | "open";
  units: number;
  avg_cost: number;
  exit_price: number | null;
  mark?: number | null;
  exit_reason: string;
  holding_days: number | null;
  equity_realized: number;
  equity_open: number;
  option_net: number;
  premium_collected: number;
  combined_net: number;
  n_calls: number;
  tranches: EquityTranche[];
  calls: CampaignCall[];
}

export interface ReportTimeline {
  underlying: string;
  prices: { date: string; close: number }[];
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
    // covered-call only: the equity (ETF) leg folded in
    equity_realized_pnl?: number;
    equity_open_pnl?: number;
    equity_units_held?: number;
    option_open_pnl?: number; // MTM of any option leg still open at the run end
    strategy_net_pnl?: number;
  };
  charges?: ChargeBreakdown;
  equity_legs?: EquityLeg[]; // closed ETF round-trips (covered call)
  equity_held?: EquityLeg[]; // still-held ETF, marked to last close
  campaigns?: Campaign[]; // covered-call accumulation→called-away campaigns
  timeline?: ReportTimeline; // underlying daily price series for campaign charts
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
  run_id: number | null; // null for a non-persisted preview
  strategy_id: string;
  report: Report;
  trades: Trade[];
}

export interface OverrideInput {
  scope: string;
  target: string | null;
  rule: Record<string, unknown>;
}

export interface ManualLegClose {
  symbol: string;
  lots?: number | null; // null = close every lot-record of this symbol
}

export interface ManualLegOpen {
  right: string; // "CE" | "PE"
  strike: number;
  lots: number; // lot-sets (× contract lot size)
  side: string; // "buy" | "sell"
}

export interface ManualOrderInput {
  closes: ManualLegClose[];
  opens: ManualLegOpen[];
}

export interface LivePosition {
  symbol: string;
  units: number;
  lots: number;
  direction?: number; // +1 long / −1 short (for the payoff diagram)
  avg_price: number;
  ltp: number | null;
  unrealized_pnl: number;
  entry_date?: string | null; // earliest lot's open date (YYYY-MM-DD)
  // Live greeks (options only; derived from live LTP + index spot + DTE).
  iv?: number | null; // implied vol (decimal, e.g. 0.14)
  delta?: number | null; // per-contract delta (signed by right)
  pos_delta?: number | null; // position delta = direction · delta · units
}

export interface GreeksHistoryPoint {
  ts: string;
  spot: number | null;
  net_delta: number | null;
  net_iv: number | null;
  pnl: number | null; // net unrealized P&L (₹) at this sample
  legs: { symbol: string; iv: number | null; delta: number | null; pos_delta: number | null }[];
}

export interface GreeksHistory {
  run_id: number;
  points: GreeksHistoryPoint[];
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
  lots?: number | null; // options: lot-sets (null for equity strategies)
  instrument_class?: string | null;
  underlying?: string | null;
  underlying_spot?: number | null; // live index spot (payoff marker)
  quote_source: string;
  on_cache_fallback?: boolean;
  quote_error?: string | null; // last live-quote fetch failure (e.g. rejected Zerodha token)
  realized_taxes: number;
  positions: LivePosition[];
  net_delta?: number | null; // options: Σ position delta (None for equity)
  net_iv?: number | null; // options: units-weighted IV (decimal)
  margin_used?: number | null; // options: real Zerodha basket margin or model estimate
  margin_source?: string | null; // "zerodha" | "model"
  net_credit?: number | null; // options: net premium (+credit / −debit)
  realized_pnl?: number | null; // booked P&L so far (incl. a backtest seed's trades)
  profit_target_amt?: number | null; // ₹ profit target the strategy will act on
  stop_loss_amt?: number | null; // ₹ stop-loss the strategy will act on
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
  lots?: number;
}

export interface StartLiveRequest {
  strategy_id: string;
  name?: string;
  notes?: string;
  symbols?: string[];
  universe?: string | null;
  instrument_class?: string; // "STOCK" | "DERIV"
  underlying?: string;
  capital: number;
  params: Record<string, unknown>;
  tax_rate: number;
  withdrawal_rate: number;
  lookback: number;
  quote_source: string;
  broker_account_id?: number | null;
  ignore_market_hours: boolean;
  auto: boolean;
  warm_from_date?: string; // options PAPER: seed from this past date (ISO)
}

// ---- Trade feature: deploy a user-built option / equity position ----
export interface OptionTradeLeg {
  right: "CE" | "PE";
  strike: number;
  side: "buy" | "sell";
  lots: number;
}

export interface OptionsTradeDeploy {
  name: string;
  underlying: string;
  expiry: string;
  legs: OptionTradeLeg[];
  lot_size: number;
  capital: number;
  spot_upper?: number | null;
  spot_lower?: number | null;
  target_pct?: number | null;          // combined P&L %, on net entry premium
  stop_pct?: number | null;
  leg_targets?: Record<number, number> | null; // {legIndex: %}
  leg_stops?: Record<number, number> | null;
  mode: string;
  quote_source: string;
  broker_account_id?: number | null;
  ignore_market_hours: boolean;
  auto: boolean;
  notes?: string;
}

export interface EquityTradeDeploy {
  name: string;
  symbol: string;
  qty: number;
  capital: number;
  entry_mode: string;                  // "immediate" | "trigger"
  trigger_price?: number | null;
  target_pct?: number | null;
  stop_pct?: number | null;
  trailing: boolean;
  trail_pct?: number | null;
  mode: string;
  quote_source: string;
  broker_account_id?: number | null;
  ignore_market_hours: boolean;
  auto: boolean;
  notes?: string;
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
  // Options tiles: margin utilised + net credit/debit instead of equity value.
  margin_used?: number | null;
  margin_source?: string | null;
  net_credit?: number | null;
  net_delta?: number | null;
  realized_pnl?: number | null;
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
  instrument_class?: string | null; // "DERIV" for options, "STOCK"/null for equity
  underlying?: string | null; // e.g. NIFTY (options only)
  started_at: string | null;
  stopped_at: string | null;
  metrics: DeploymentMetrics;
  // Broker connection (zerodha quotes only; null for cache deployments)
  broker_account_id?: number | null;
  broker_label?: string | null;
  broker_connected?: boolean | null; // session valid right now
  on_cache_fallback?: boolean; // zerodha run currently falling back to cache quotes
  quote_error?: string | null; // last live-quote fetch failure (rejected token, etc.)
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
  live?: boolean;      // true when premiums are real-time Zerodha quotes
  lot_size?: number;   // contract lot size (live chain only)
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
  // SuperTrend runs only (the watchlist is strategy-aware):
  direction?: number | null; // +1 green / −1 red
  supertrend?: number | null; // the trailing SuperTrend line
  to_flip_pct?: number | null; // % from price to the line (flip cushion)
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
  persist?: boolean; // false = preview only (no DB write); save later via /backtest/save
}
