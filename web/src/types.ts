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
  underlying_spot?: number | null; // index spot at execution (captured live; for accurate entry/exit markers)
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
  basket_cycles?: BasketCycle[]; // donchian_strangle_bt: cycle → names → legs view
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
  lots: number; // count of lot-records (fills) — the manual-close cap, NOT tradable lots
  lot_size?: number | null; // contract lot size (options); tradable lots = units / lot_size
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
  order_error?: string | null;
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
  exit_rules?: string[] | null; // human-readable exit triggers (spot levels, %-targets, …)
  // live controls + exclusion editing
  auto: boolean;
  ignore_market_hours: boolean;
  refresh_seconds: number;
  decision_time: string;
  universe: string[];
  excluded_symbols: string[];
  basket?: DonchianBasket | null; // donchian_strangle_monthly: per-name breakdown + aggregate payoff
}

export interface DonchianBasketLeg {
  side: string; // "SELL CE" | "SELL PE"
  right: "CE" | "PE";
  strike: number;
  units: number;
  entry?: number | null;
  mark?: number | null;
  open: boolean;
  breached: boolean;
  state: string; // open | flip-open | covered | flip-covered
}
export interface DonchianBasketName {
  symbol: string;
  spot?: number | null;
  flip_count: number;
  status: string; // open | flipped | closed | settled
  struct: string; // strangle | CE-only | PE-only | closed
  legs: DonchianBasketLeg[];
  units?: number;   // per-leg contract units
  lot_size?: number | null;
  lots?: number | null;
  credit?: number;  // entry credit collected for the name (Σ entry·units, open legs)
  value?: number;   // current value of the name's open legs (Σ ltp·units)
  realized?: number; // realized P&L booked on this name's flips
  mtm: number;
}
export interface DonchianHedgeLeg {
  underlying: string;
  right: "CE" | "PE";
  strike: number;
  units: number;
  entry?: number | null;
  mark?: number | null;
  otm_pct?: number | null;
}
export interface DonchianBasket {
  names: DonchianBasketName[];
  hedge: {
    legs: DonchianHedgeLeg[]; mtm: number; spot?: number | null; lots?: number | null;
    cost?: number; cost_pct?: number | null; entry_notional: number; current_notional: number;
  };
  net_credit?: number;
  basket_mtm?: number;
  hedge_mtm?: number;
  combined_mtm?: number;
  realized_pnl: number;
  total_flips?: number;
  closed_count?: number;
  portfolio_stop_amount?: number | null;
  portfolio_target_amount?: number | null;
  buffer_to_stop?: number | null;
  portfolio_sl_pct?: number;
  portfolio_target_pct?: number;
  portfolio_target_enabled?: boolean;
  portfolio_basis?: string; // "notional" | "margin"
  entry_progress?: { entered: number; expected: number; done: boolean };
  expiry?: string | null;
  dte?: number | null;
  payoff: { move_pct: number; expiry_pnl: number }[];
}

// ---- FibRet screener (Fibonacci-retracement option selling) ----
export interface FibRetRow {
  symbol: string;
  error?: string | null;
  spot?: number;
  side?: "CE" | "PE";
  swing_high?: number;
  swing_high_date?: string;
  swing_low?: number;
  swing_low_date?: string;
  entry_level?: number;
  strike?: number;
  expiry?: string;
  dte?: number;
  premium?: number | null;
  oi?: number;
  bid?: number | null;
  ask?: number | null;
  spread_pct?: number | null; // (ask−bid)/mid·100 — liquidity gauge
  liquid?: boolean; // spread ≤ 10%
  lot_size?: number;
  lots?: number;
  qty?: number;
  iv?: number | null;
  stop_level?: number;
  est_stop_loss?: number | null;
  max_profit?: number | null;
  reward_risk?: number | null;
  breakeven?: number | null;
  realized_vol?: number | null;
  iv_richness?: number | null;
  margin?: number | null;
  cushion_to_strike_pct?: number;
  cushion_to_stop_pct?: number;
  out_of_range?: boolean; // 1.618 level beyond the listed strikes (too far OTM)
  note?: string | null;
}

export interface FibRetResult {
  as_of: string;
  target_pct: number; // whole percent
  entry_fib: number;
  stop_fib: number;
  rows: FibRetRow[];
}

export interface FibRetRequest {
  broker_account_id: number;
  symbols: string[];
  expiry?: string | null;
  swing_lookback?: number;
  entry_fib?: number;
  stop_fib?: number;
  target_pct?: number;
  min_oi?: number;
  lots?: number;
  min_dte?: number;
}

export interface LiveControlsInput {
  auto?: boolean;
  ignore_market_hours?: boolean;
  refresh_seconds?: number;
  excluded_symbols?: string[];
  lots?: number;
}

// ---- Donchian Strangle Monthly (basket short-strangle screener) ----
export type DonchianStatus =
  | "strangle" | "CE-only" | "PE-only" | "excluded:event" | "excluded:filter" | "error";

export interface DonchianLeg {
  strike: number;
  premium?: number | null;
  bid?: number | null;
  ask?: number | null;
  oi?: number;
  spread_pct?: number | null;
  liquid?: boolean;
  skip?: boolean;
}

export interface DonchianRow {
  symbol: string;
  status: DonchianStatus;
  error?: string | null;
  reason?: string | null;
  spot?: number | null;
  ivp?: number | null;
  atm_iv?: number | null;
  hv?: number | null;
  event?: string | null;
  range_high?: number;
  range_low?: number;
  ce?: DonchianLeg | null;
  pe?: DonchianLeg | null;
  lot_size?: number;
  lots?: number;
  margin?: number | null;
  strike_step?: number | null; // listed strike step (ATM flip sizing)
  beta?: number | null;        // vs NIFTY (optional beta-weighted hedge)
  breakout?: "up" | "down" | null; // spot beyond range → ATM opposite leg only
  width_pct?: number | null;   // (range high−low)/spot ·100 — the strike cushion
  hv_ratio?: number | null;    // HV(hv_window)/HV60 — <1 = vol compression
  strikes?: number[];          // listed strikes (manual CE/PE strike override)
  expiry?: string;
}

export interface DonchianCycle {
  prev_expiry: string | null;
  last_expiry: string | null;
  sell_expiry: string | null;
  entry_date: string | null;
  range_start: string | null;
  range_end: string | null;
}

export interface DonchianResult {
  as_of: string;
  dates: DonchianCycle;
  rows: DonchianRow[];
  vix?: number | null; // live India VIX at screen time (market-stress advisory)
  error?: string;
}

export interface DonchianNameInput {
  symbol: string;
  atm_iv?: number | null;
  ivp?: number | null;
  event?: string | null;
}

export interface DonchianAnalyzeRequest {
  broker_account_id: number;
  names: DonchianNameInput[];
  range_start?: string | null;
  range_end?: string | null;
  entry_date?: string | null;
  sell_expiry?: string | null;
  ivp_min?: number;
  require_iv_gt_hv?: boolean;
  hv_window?: number;
  skip_leg_min_premium_pct?: number;
  round_out?: boolean;
  breakout_atm?: boolean;
  lots_per_name?: number;
  min_dte?: number;
  min_hv_ratio?: number;          // 0 = off — exclude vol-compressed names
  min_channel_width_pct?: number; // 0 = off — exclude tight channels
}

export interface DonchianHedge {
  nifty_lots: number;
  nifty_lot_size?: number;
  ce_strike?: number | null;
  pe_strike?: number | null;
  ce_premium?: number | null;
  pe_premium?: number | null;
  cost?: number;
  cost_pct_of_premium?: number | null;
  cap_flag?: boolean;
}

export interface DonchianPanel {
  selected_count: number;
  agg_notional: number;
  premium_collected: number;
  premium_pct_of_notional?: number | null;
  hedge: DonchianHedge;
  portfolio_sl_amount: number;
  portfolio_target_amount?: number | null;
  basket_margin?: number | null;
}

export interface DonchianPortfolioRequest {
  broker_account_id: number;
  sell_expiry: string;
  selected: DonchianRow[];
  hedge_otm_pct?: number;
  hedge_beta_weight?: boolean;
  hedge_cost_cap_pct?: number;
  portfolio_sl_pct?: number;
  portfolio_target_enabled?: boolean;
  portfolio_target_pct?: number;
  portfolio_basis?: string; // "notional" (legacy) | "margin"
}

export interface DonchianDeployLeg {
  underlying: string;
  right: "CE" | "PE";
  strike: number;
  side: "buy" | "sell";
  lots: number;
  spot?: number;
  lot_size?: number;
  strike_step?: number | null; // for the ATM roll flip
}

export interface DonchianDeploy {
  name: string;
  notes?: string;
  sell_expiry: string;
  legs: DonchianDeployLeg[];
  capital: number;
  portfolio_sl_pct?: number;
  portfolio_target_enabled?: boolean;
  portfolio_target_pct?: number;
  portfolio_basis?: string; // "notional" (legacy) | "margin"
  leg_target_enabled?: boolean;
  leg_target_pct?: number; // % of each leg's own premium → close that leg
  breach_basis?: string;
  breach_buffer_pct?: number; // spot must clear a short strike by this % to flip
  flip_delta?: string; // "atm" | "30delta"
  max_flips?: number;
  mode: string;
  quote_source: string;
  broker_account_id?: number | null;
  ignore_market_hours?: boolean;
  auto?: boolean;
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

export interface MtgBtStats {
  trades: number;
  win_rate?: number;
  total_pnl?: number;
  return_pct?: number;
  avg_pnl?: number;
  avg_win?: number;
  avg_loss?: number;
  worst_day?: number;
  best_day?: number;
  max_drawdown_pct?: number;
  peak_margin?: number;
  trading_days?: number;
  days_with_trades_pct?: number;
  cap_saturated_days?: number;
  by_exit_reason?: Record<string, { count: number; pnl: number }>;
  by_side?: Record<string, { count: number; pnl: number }>;
}

export interface MtgBtTrade {
  entry_time: string;
  exit_time: string;
  symbol: string;
  side: string;
  exit_reason: string;
  entry_premium: number;
  exit_premium: number;
  units: number;
  entry_spot: number;
  exit_spot: number;
  margin: number;
  pnl: number;
}

export interface MtgBtResult {
  error?: string;
  note?: string;
  skipped_entries?: number;
  params?: Record<string, unknown>;
  stats?: MtgBtStats;
  equity?: { date: string; equity: number; pnl: number }[];
  trades?: MtgBtTrade[];
}

export interface DeltaNeutralDeploy {
  name: string;
  underlying: string;
  lots: number;
  target_delta: number;
  force_entry: boolean;
  adjust_threshold_pct: number;
  adjust_cooldown_min: number;
  profit_target_pct: number;
  stop_loss_pct: number;
  capital: number;
  mode: string;
  quote_source: string;
  broker_account_id: number | null;
  auto: boolean;
}

export interface CpRatioExpiryDeploy {
  name: string;
  underlyings: string[];
  sets: Record<string, number>;
  profit_target_pct: number;
  stop_loss_pct: number;
  ratio_tolerance_pct: number;
  capital: number;
  mode: string;
  quote_source: string;
  broker_account_id: number | null;
  auto: boolean;
}

export interface MomentumThetaDeploy {
  name: string;
  underlyings: string[];
  lots: Record<string, number>;
  st_period: number;
  st_multiplier: number;
  candle_minutes: number;
  max_trades_per_day: number;
  min_dte: number;
  capital: number;
  mode: string;
  quote_source: string;
  broker_account_id: number | null;
  auto: boolean;
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

export interface LiveSummary {
  win_rate: number | null; // % of closed round-trips that booked a profit (paper)
  total_trades: number;
  equity_series: number[]; // ~30d aggregated daily paper equity (sparkline)
  equity_change_pct_30d: number | null;
  sharpe_30d: number | null; // annualized, from the daily series
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
  order_error?: string | null; // real-order failure/book-mismatch halt (ack to resume)
  underlying_spot?: number | null; // live underlying spot (tile subline)
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
  bid?: number | null; // top-of-book (live chain only)
  ask?: number | null;
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
  no_cash?: boolean; // breakout that can't be funded (no free capital part)
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

// --- research: Donchian breakout study (cache-only) + BS-vs-live calibration ---

export interface DonchianStudyRequest {
  universe?: string;
  symbols?: string[];
  start_date: string;
  end_date?: string | null;
  buffer_pct?: number;
  basis?: "touch" | "close";
  max_flips?: number;
  include_index?: boolean;
  detail?: boolean;
}

export interface StudyCycleRow {
  cycle_id: string;
  range_start: string;
  range_end: string;
  entry_date: string;
  expiry: string;
  n_names: number;
  inside: number;
  breakout: number;
  "re-entered": number;
  whipsaw: number;
  breakout_up: number;
  breakout_down: number;
  both_sides: number;
  closed_by_flips: number;
  gap_entries: number;
  vix_entry: number | null;
  index_status: string | null;
}

export interface StudyNameCycle {
  cycle_id: string;
  symbol: string;
  status: string; // inside | breakout | re-entered | whipsaw
  days: number;
  breakout_at_entry: string | null;
  first_breach_side: string | null;
  first_breach_day: number | null;
  re_entered: boolean;
  re_entry_day: number | null;
  whipsaw: boolean;
  whipsaw_side: string | null;
  both_sides_breached: boolean;
  max_excursion_up_pct: number;
  max_excursion_down_pct: number;
  flips: { day: number; date: string; side: string; action: string }[];
  flip_count: number;
  closed_by_flips: boolean;
  closed_day: number | null;
  range_high: number;
  range_low: number;
}

export interface StudyLeagueRow {
  symbol: string;
  is_index: boolean;
  cycles: number;
  inside: number;
  breach_rate: number;
  up: number;
  down: number;
  re_entries: number;
  whipsaws: number;
  both_sides: number;
  closed_by_flips: number;
  avg_flips: number;
  median_breach_day: number | null;
  avg_excursion_pct: number | null;
}

export interface DonchianStudyResult {
  params: { buffer_pct: number; basis: string; max_flips: number };
  cycles: StudyCycleRow[];
  league: StudyLeagueRow[];
  histograms: { days_to_first_breach: number[]; excursion_pct: number[] };
  aggregates: Record<string, number | null>;
  vix_split: {
    bucket: string; cycles: number; name_cycles: number;
    inside_pct?: number; whipsaw_pct?: number; both_sides_pct?: number; closed_pct?: number;
  }[];
  detail?: StudyNameCycle[];
  caveats: string[];
}

export interface CalibRow {
  symbol: string;
  spot: number;
  hv_pct: number;
  strike: number;
  right: string;
  kind: "screener" | "atm";
  moneyness_pct: number;
  market_bid: number | null;
  market_mid: number | null;
  market: number;
  bs_price: number;
  ratio: number;
  market_iv_pct: number | null;
  iv_over_hv: number | null;
}

export interface CalibStats {
  n: number;
  median: number;
  q1: number;
  q3: number;
}

export interface BsCalibrationResult {
  as_of: string;
  sell_expiry: string;
  range_start: string;
  range_end: string;
  r: number;
  hv_window: number;
  rows: CalibRow[];
  aggregates: {
    rows: number;
    ratio: CalibStats | null;
    iv_over_hv: CalibStats | null;
    by_right: Record<string, CalibStats | null>;
    by_moneyness: { bucket: string; ratio: CalibStats | null; iv_over_hv: CalibStats | null }[];
    suggested_vol_multiplier: number | null;
  };
  errors: { symbol: string; error: string }[];
}

// --- donchian_strangle_bt basket run: cycle → names → legs drill-down ---

export interface BasketLeg {
  symbol: string;
  right: string;
  strike: number;
  side: "sell" | "buy";
  units: number;
  entry_date: string | null;
  entry_price: number | null;
  exit_date: string | null;
  exit_price: number | null;
  exit_reason: string; // flip | leg_target | portfolio_stop | portfolio_target | expiry | open
  pnl: number;
}

export interface BasketNameRow {
  name: string;
  side: "short" | "hedge";
  lot_size: number;
  lots: number | null;
  units: number;
  premium: number;
  pnl: number;
  charges: number;
  pnl_net: number;
  flips: number;
  legs: BasketLeg[];
}

export interface BasketCycle {
  cycle: string; // "2024-03" (labelled by expiry month)
  expiry: string;
  entry_date: string;
  exit_date: string;
  names: number;
  premium_collected: number;
  flips: number;
  exit_reason: string; // expiry | portfolio_stop | portfolio_target
  margin_peak: number | null;
  pnl: number;
  charges: number;
  pnl_net: number;
  return_on_margin_pct: number | null;
  name_rows: BasketNameRow[];
}
