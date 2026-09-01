import type {
  AnalyticsBundle,
  BacktestRequest,
  BacktestResponse,
  ReplayJobSnapshot,
  BenchmarkPoint,
  LoginResponse,
  BrokerAccount,
  BrokerConnectRequest,
  BsCalibrationResult,
  CycleDetail,
  DonchianStudyRequest,
  DonchianStudyResult,
  CompareRun,
  DataCoverage,
  DataSummary,
  DataSymbol,
  DataSymbolDetail,
  AnalysisRunItem,
  Deployment,
  LiveSummary,
  DerivCoverage,
  OptionBarsStore,
  EquityTradeDeploy,
  DonchianAnalyzeRequest,
  DonchianDeploy,
  DonchianPanel,
  DonchianPortfolioRequest,
  DonchianResult,
  FibRetRequest,
  FibRetResult,
  FuturesSeries,
  OptionTradeLeg,
  OptionsTradeDeploy,
  GreeksHistory,
  RunAnalysis,
  StockSeries,
  LiveControlsInput,
  ManualOrderInput,
  OptionChain,
  OptionsExpiries,
  RefreshResult,
  UnderlyingList,
  LiveRunSnapshot,
  OverrideInput,
  Report,
  RunSummary,
  StartLiveRequest,
  StrategyTemplate,
  Trade,
  Universe,
  CpRatioExpiryDeploy,
  FairValueCalendarDeploy,
  IntradayStrangleComboDeploy,
  VolcanoCalendarDeploy,
  IntradayStraddleDeploy,
  DeltaNeutralDeploy,
  DoubleDiagonalDeploy,
  IronFlyDeploy,
  RatioManualDeploy,
  MomentumThetaDeploy,
  MtgBtResult,
  LossStudyProgress,
  SmokeTestDeploy,
  WatchRow,
  WeeklyIntradayStraddleDeploy,
} from "../types";

import { clearToken, getToken } from "../lib/auth";

const BASE = "/api/v1";

// Absolute backend origin for NON-same-origin shells (the Capacitor mobile app, whose
// webview origin is capacitor://localhost). "" = same-origin — the web app is unchanged.
let apiOrigin = "";
// 401 handler override: web keeps the /login redirect below; the mobile shell registers
// its own (unlock screen) because window.location.assign is meaningless in the webview.
let onUnauthorized: (() => void) | null = null;

export function setApiOrigin(origin: string): void {
  apiOrigin = origin.replace(/\/+$/, "");
}

export function getApiOrigin(): string {
  return apiOrigin;
}

export function setUnauthorizedHandler(fn: (() => void) | null): void {
  onUnauthorized = fn;
}

/** Build request headers: JSON + the Authorization bearer token (if we have one), without
 *  clobbering any per-call headers a caller passes. */
function authHeaders(extra?: HeadersInit): HeadersInit {
  const token = getToken();
  return {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(extra ?? {}),
  };
}

import type {
  Bucket,
  BucketInput,
  DividendRow,
  Goal,
  GoalInput,
  HoldingInput,
  LedgerPreview,
  PortfolioPayload,
  SeedResult,
  SyncReport,
  TransactionInput,
  TransactionRow,
} from "../lib/portfolio";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${apiOrigin}${BASE}${path}`, {
    ...init,
    headers: authHeaders(init?.headers),
  });
  if (!resp.ok) {
    // Session expired / no token but the server now enforces auth → back to the login gate.
    if (resp.status === 401 && onUnauthorized) {
      clearToken();
      onUnauthorized();
    } else if (resp.status === 401 && window.location.pathname !== "/login") {
      clearToken();
      window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
    }
    let detail = resp.statusText;
    try {
      detail = (await resp.json()).detail ?? detail;
    } catch {
      /* ignore */
    }
    throw new Error(`${resp.status}: ${detail}`);
  }
  return resp.json() as Promise<T>;
}

export const api = {
  login: (password: string) =>
    request<LoginResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ password }),
    }),
  strategies: (basis: "eod" | "intraday" = "eod") =>
    // decision_times: each strategy's OWN default daily decision time, so the deploy form
    // shows what the deploy route would resolve rather than keeping its own copy.
    request<{ strategies: string[]; decision_times?: Record<string, string> }>(
      `/strategies?basis=${basis}`),
  // INTRADAY replays run as a background job: POST returns {job_id}, poll progress for
  // {done,total,day} and the full BacktestResponse-shaped result once status=="done".
  backtestIntraday: (body: BacktestRequest) =>
    request<{ job_id: string }>("/backtest/intraday", { method: "POST", body: JSON.stringify(body) }),
  backtestIntradayProgress: () => request<ReplayJobSnapshot>("/backtest/intraday/progress"),
  universes: () => request<Universe[]>("/universes"),
  // cachedOnly=false → the FULL static list (the cache-refresh flow: an empty cache must
  // not 404 the button that populates it).
  universeSymbols: (name: string, cachedOnly = true) =>
    request<{ name: string; symbols: string[] }>(
      `/universes/${encodeURIComponent(name)}/symbols${cachedOnly ? "" : "?cached_only=false"}`,
    ),
  runs: (status?: string) =>
    request<RunSummary[]>(`/runs${status ? `?status=${status}` : ""}`),
  runUpdate: (id: number, body: { name?: string; notes?: string }) =>
    request(`/runs/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  runsCompare: (ids: number[]) =>
    request<{ runs: CompareRun[] }>(`/runs/compare?ids=${ids.join(",")}`),
  templates: () =>
    request<{ templates: Record<string, StrategyTemplate> }>("/strategies/templates"),
  setTemplate: (runId: number) =>
    request<StrategyTemplate>(`/runs/${runId}/set-template`, { method: "POST" }),
  clearTemplate: (strategyId: string) =>
    request(`/strategies/${strategyId}/template`, { method: "DELETE" }),
  runArchive: (id: number) => request(`/runs/${id}/archive`, { method: "POST" }),
  runUnarchive: (id: number) => request(`/runs/${id}/unarchive`, { method: "POST" }),
  runDelete: (id: number) => request(`/runs/${id}`, { method: "DELETE" }),
  run: (id: number) =>
    request<{
      report: Report;
      strategy_id: string;
      name: string | null;
      notes: string | null;
      archived: boolean;
      capital: number | null;
      params: Record<string, unknown>;
      trades: Trade[];
    }>(`/runs/${id}`),
  cycleDetail: (runId: number, index: number) =>
    request<CycleDetail>(`/runs/${runId}/cycles/${index}/detail`),
  // Cycle detail for an UNSAVED backtest preview — the report+trades are already in the
  // browser, so no run_id is needed (the saved-run version above uses the persisted run).
  cycleDetailPreview: (report: unknown, trades: unknown[], index: number,
                       params?: unknown, strategyId?: string) =>
    request<CycleDetail>(`/backtest/cycle-detail`, {
      method: "POST",
      // params + strategy_id feed the absolute ₹ target/SL tile (entry-margin thresholds)
      body: JSON.stringify({ report, trades, index, params, strategy_id: strategyId }),
    }),
  backtest: (body: BacktestRequest) =>
    request<BacktestResponse>("/backtest", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  // Persist a previewed backtest (its computed report + trades) without recomputing.
  backtestSave: (body: { request: BacktestRequest; report: Report; trades: Trade[] }) =>
    request<BacktestResponse>("/backtest/save", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  // Authed download: a plain <a href download> can't carry the bearer header, so fetch the
  // CSV with auth, then save the blob (also keeps the token out of URLs).
  downloadTradesCsv: (id: number) => downloadCsv(`/runs/${id}/trades.csv`, `run-${id}-trades.csv`),
  benchmarks: () => request<{ benchmarks: string[] }>("/benchmarks"),
  dataSummary: () => request<DataSummary>("/data/summary"),
  dataCoverage: (instrumentClass: string, underlying?: string) =>
    request<DataCoverage>(
      `/data/coverage?instrument_class=${instrumentClass}` +
        (underlying ? `&underlying=${encodeURIComponent(underlying)}` : ""),
    ),
  dataSymbols: () => request<DataSymbol[]>("/data/symbols"),
  dataSymbol: (sym: string) =>
    request<DataSymbolDetail>(`/data/symbols/${encodeURIComponent(sym)}`),
  // Benchmark overlay for an UNSAVED preview — dates are the chart's own x-axis.
  benchmarkSeries: (index: string, dates: string[], capital: number) =>
    request<{ index: string; points: BenchmarkPoint[] }>(`/benchmark-series`, {
      method: "POST", body: JSON.stringify({ index, dates, capital }),
    }),
  runBenchmark: (id: number, index: string) =>
    request<{ index: string; points: BenchmarkPoint[] }>(
      `/runs/${id}/benchmark?index=${encodeURIComponent(index)}`,
    ),
  // --- trade analysis ---
  analysisRuns: () => request<AnalysisRunItem[]>("/analysis/runs"),
  runAnalysis: (id: number) => request<RunAnalysis>(`/runs/${id}/analysis`),
  // Analyze workbench: cached analytics bundle (404 until computed), the compute job,
  // and its progress (analytics slot — independent of the intraday-replay job).
  runAnalytics: (id: number) => request<AnalyticsBundle>(`/runs/${id}/analytics`),
  computeAnalytics: (id: number) =>
    request<{ job_id?: string; status?: string }>(`/runs/${id}/analytics/compute`, { method: "POST" }),
  analyticsProgress: () => request<ReplayJobSnapshot>(`/analytics/progress`),
  stockSeries: (
    symbol: string,
    opts: { start?: string; end?: string; st_period?: number; st_multiplier?: number; st_timeframe?: string } = {},
  ) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(opts)) if (v != null && v !== "") q.set(k, String(v));
    const qs = q.toString();
    return request<StockSeries>(`/data/stocks/${encodeURIComponent(symbol)}/series${qs ? `?${qs}` : ""}`);
  },

  // --- options & futures data (no broker session needed) ---
  optionsUnderlyings: () => request<UnderlyingList>("/data/options/underlyings"),
  optionBarsStore: (days = 30) =>
    request<OptionBarsStore>(`/data/options/intraday-store?days=${days}`),
  optionBarsCaptureNow: () =>
    request<{ started: boolean; target_day?: string; reason?: string }>(
      "/data/options/intraday-store/capture", { method: "POST" }),
  optionsCoverage: (u: string) =>
    request<DerivCoverage>(`/data/options/${encodeURIComponent(u)}/coverage`),
  optionsExpiries: (u: string, date?: string) =>
    request<OptionsExpiries>(
      `/data/options/${encodeURIComponent(u)}/expiries${date ? `?date=${date}` : ""}`,
    ),
  optionsChain: (u: string, date: string, expiry: string, greeks = false) =>
    request<OptionChain>(
      `/data/options/${encodeURIComponent(u)}/chain?date=${date}&expiry=${expiry}&greeks=${greeks}`,
    ),
  // Real-time chain via a logged-in Zerodha session (live premiums + OI + lot size + spot).
  optionsLiveUnderlyings: (accountId: number) =>
    request<{ underlyings: string[] }>(`/data/options/live/underlyings?broker_account_id=${accountId}`),
  optionsLiveExpiries: (u: string, accountId: number) =>
    request<OptionsExpiries>(`/data/options/live/${encodeURIComponent(u)}/expiries?broker_account_id=${accountId}`),
  optionsLiveChain: (u: string, expiry: string, accountId: number) =>
    request<OptionChain>(`/data/options/live/${encodeURIComponent(u)}/chain?expiry=${expiry}&broker_account_id=${accountId}`),
  optionsRefresh: (body: { underlyings: string[]; start_date: string; end_date: string }) =>
    request<RefreshResult>("/data/options/refresh", { method: "POST", body: JSON.stringify(body) }),
  futuresUnderlyings: () => request<UnderlyingList>("/data/futures/underlyings"),
  futuresCoverage: (u: string) =>
    request<DerivCoverage>(`/data/futures/${encodeURIComponent(u)}/coverage`),
  futuresSeries: (u: string) =>
    request<FuturesSeries>(`/data/futures/${encodeURIComponent(u)}/series`),
  futuresRefresh: (body: { underlyings: string[]; start_date: string; end_date: string }) =>
    request<RefreshResult>("/data/futures/refresh", { method: "POST", body: JSON.stringify(body) }),

  // --- trade (deploy a user-built option / equity position) ---
  deployOptionTrade: (body: OptionsTradeDeploy) =>
    request<LiveRunSnapshot>("/trade/options/deploy", { method: "POST", body: JSON.stringify(body) }),
  fibretAnalyze: (body: FibRetRequest) =>
    request<FibRetResult>("/trade/options/fibret/analyze", { method: "POST", body: JSON.stringify(body) }),
  donchianAnalyze: (body: DonchianAnalyzeRequest) =>
    request<DonchianResult>("/trade/options/donchian/analyze", { method: "POST", body: JSON.stringify(body) }),
  donchianPortfolio: (body: DonchianPortfolioRequest) =>
    request<DonchianPanel>("/trade/options/donchian/portfolio", { method: "POST", body: JSON.stringify(body) }),
  donchianDeploy: (body: DonchianDeploy) =>
    request<LiveRunSnapshot>("/trade/options/donchian/deploy", { method: "POST", body: JSON.stringify(body) }),
  deltaNeutralDeploy: (body: DeltaNeutralDeploy) =>
    request<LiveRunSnapshot>("/trade/options/delta-neutral/deploy", { method: "POST", body: JSON.stringify(body) }),
  ironFlyDeploy: (body: IronFlyDeploy) =>
    request<LiveRunSnapshot>("/trade/options/iron-fly/deploy", { method: "POST", body: JSON.stringify(body) }),
  doubleDiagonalDeploy: (body: DoubleDiagonalDeploy) =>
    request<LiveRunSnapshot>("/trade/options/double-diagonal/deploy", { method: "POST", body: JSON.stringify(body) }),
  ratioDeploy: (body: RatioManualDeploy) =>
    request<LiveRunSnapshot>("/trade/options/ratio/deploy", { method: "POST", body: JSON.stringify(body) }),
  smokeTestDeploy: (body: SmokeTestDeploy) =>
    request<LiveRunSnapshot>("/trade/smoke-test/deploy", { method: "POST", body: JSON.stringify(body) }),
  ironflyAdjust: (runId: number, on: boolean) =>
    request<{ ironfly_adjust: boolean; note: string }>(`/live/${runId}/ironfly-adjust`, { method: "POST", body: JSON.stringify({ on }) }),
  fairValueCalendarDeploy: (body: FairValueCalendarDeploy) =>
    request<LiveRunSnapshot>("/trade/options/fair-value-calendar/deploy", { method: "POST", body: JSON.stringify(body) }),
  volcanoCalendarDeploy: (body: VolcanoCalendarDeploy) =>
    request<LiveRunSnapshot>("/trade/options/volcano-calendar/deploy", { method: "POST", body: JSON.stringify(body) }),
  /** Deploy through a strategy's OWN route, chosen from lib/deploy/registry. The registry
   *  page needs one call that works for every named route; the typed per-strategy methods
   *  above stay for anything that still calls them directly. */
  deployByRoute: (route: string, body: Record<string, unknown>) =>
    request<LiveRunSnapshot>(route, { method: "POST", body: JSON.stringify(body) }),
  strangleComboDeploy: (body: IntradayStrangleComboDeploy) =>
    request<LiveRunSnapshot>("/trade/options/strangle-combo/deploy", { method: "POST", body: JSON.stringify(body) }),
  cpRatioExpiryDeploy: (body: CpRatioExpiryDeploy) =>
    request<LiveRunSnapshot>("/trade/options/cp-ratio-expiry/deploy", { method: "POST", body: JSON.stringify(body) }),
  intradayStraddleDeploy: (body: IntradayStraddleDeploy) =>
    request<LiveRunSnapshot>("/trade/options/intraday-straddle/deploy", { method: "POST", body: JSON.stringify(body) }),
  weeklyIntradayStraddleDeploy: (body: WeeklyIntradayStraddleDeploy) =>
    request<LiveRunSnapshot>("/trade/options/weekly-intraday-straddle/deploy", { method: "POST", body: JSON.stringify(body) }),
  momentumThetaDeploy: (body: MomentumThetaDeploy) =>
    request<LiveRunSnapshot>("/trade/options/momentum-theta/deploy", { method: "POST", body: JSON.stringify(body) }),
  deployEquityTrade: (body: EquityTradeDeploy) =>
    request<LiveRunSnapshot>("/trade/equity/deploy", { method: "POST", body: JSON.stringify(body) }),
  optionTradeMargin: (body: {
    underlying: string; expiry: string; lot_size: number; legs: OptionTradeLeg[]; broker_account_id?: number | null;
  }) =>
    request<{ margin: number | null; source: string | null }>("/trade/options/margin", {
      method: "POST", body: JSON.stringify(body),
    }),

  // --- research (Donchian breakout study + BS-vs-live calibration) ---
  researchDonchianStudy: (body: DonchianStudyRequest) =>
    request<DonchianStudyResult>("/research/donchian-study", {
      method: "POST", body: JSON.stringify(body),
    }),
  researchMomentumThetaBt: (body: {
    start_date: string; end_date?: string | null; lots?: number; st_period?: number;
    st_multiplier?: number; max_trades_per_day?: number; min_dte?: number;
    vol_multiplier?: number; slippage_bps?: number; capital?: number;
    broker_account_id?: number | null;
  }) =>
    request<MtgBtResult>("/research/momentum-theta-bt", {
      method: "POST", body: JSON.stringify(body),
    }),
  researchBsCalibration: (body: { broker_account_id: number; names?: string[]; hv_window?: number; r?: number; sell_expiry?: string | null; round_out?: boolean }) =>
    request<BsCalibrationResult>("/research/bs-calibration", {
      method: "POST", body: JSON.stringify(body),
    }),
  researchLossStudy: (body: { start_date: string; end_date?: string | null; oos_start: string;
    capital?: number; margin_per_lot?: number; lots?: number }) =>
    request<{ job_id: string }>("/research/loss-study", {
      method: "POST", body: JSON.stringify(body),
    }),
  researchLossStudyProgress: () =>
    request<LossStudyProgress>("/research/loss-study/progress"),

  // --- live / paper ---
  liveList: () => request<LiveRunSnapshot[]>("/live"),
  liveGet: (id: number) => request<LiveRunSnapshot>(`/live/${id}`),
  liveUpdateParams: (id: number, params: Record<string, unknown>) =>
    request<{ run_id: number; applied: string[]; params: Record<string, unknown> }>(
      `/live/${id}/params`,
      { method: "POST", body: JSON.stringify({ params }) },
    ),
  liveStart: (body: StartLiveRequest) =>
    request<LiveRunSnapshot>("/live/start", { method: "POST", body: JSON.stringify(body) }),
  liveRefresh: (id: number, decide = false) =>
    request<LiveRunSnapshot>(`/live/${id}/refresh${decide ? "?decide=true" : ""}`, { method: "POST" }),
  liveActivate: (id: number) =>
    request<LiveRunSnapshot>(`/live/${id}/activate`, { method: "POST" }),
  liveGoLive: (id: number, body: { broker_account_id: number; keep_paper_running?: boolean }) =>
    request<LiveRunSnapshot>(`/live/${id}/go-live`, { method: "POST", body: JSON.stringify(body) }),
  liveRunDecision: (id: number) =>
    request<{ run_id: number; trades: unknown[] }>(`/live/${id}/run-decision`, { method: "POST" }),
  liveStop: (id: number) => request<{ stopped: number }>(`/live/${id}/stop`, { method: "POST" }),
  liveSnapshot: (id: number) => request<LiveRunSnapshot>(`/live/${id}`),
  liveWatchlist: (id: number) =>
    request<{ run_id: number; rows: WatchRow[] }>(`/live/${id}/watchlist`),
  liveSetQuoteSource: (id: number, quote_source: string, broker_account_id?: number | null) =>
    request<LiveRunSnapshot>(`/live/${id}/quote-source`, {
      method: "POST",
      body: JSON.stringify({ quote_source, broker_account_id: broker_account_id ?? null }),
    }),
  liveReconnectQuotes: (id: number) =>
    request<LiveRunSnapshot>(`/live/${id}/reconnect-quotes`, { method: "POST" }),
  liveSetControls: (id: number, body: LiveControlsInput) =>
    request<LiveRunSnapshot>(`/live/${id}/controls`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  liveAddOverride: (id: number, ov: OverrideInput) =>
    request<{ run_id: number; overrides: number }>(`/live/${id}/overrides`, {
      method: "POST",
      body: JSON.stringify(ov),
    }),
  liveFlatten: (id: number) =>
    request<{ run_id: number; closed: number; snapshot: LiveRunSnapshot }>(
      `/live/${id}/flatten`,
      { method: "POST" },
    ),
  // Book legs the BROKER already closed (manual square-off in Kite, MIS auto-square-off) at
  // the prices they settled at. Places NO orders — flatten is the wrong tool when there is
  // nothing left to trade, and without this the run carries a phantom leg that halts every
  // reconciliation (run 10, 2026-08-11).
  liveAdoptBrokerClose: (id: number, legs: { symbol: string; price: number }[]) =>
    request<{ run_id: number; closed: number; snapshot: LiveRunSnapshot }>(
      `/live/${id}/adopt-broker-close`,
      { method: "POST", body: JSON.stringify({ legs }) },
    ),
  // Force the platform's unit count for one symbol to match the broker. Places NO order.
  // Adoption only ever ADDS, so there was no way back from an OVER-count: run 28 read 778
  // LIQUIDCASE against a true 754 and halted every reconciliation (2026-08-31).
  liveSetHolding: (id: number, symbol: string, units: number) =>
    request<{ run_id: number; symbol: string; before: number; after: number;
              changed: number; snapshot: LiveRunSnapshot }>(
      `/live/${id}/set-holding`,
      { method: "POST", body: JSON.stringify({ symbol, units }) },
    ),
  liveManualOrder: (id: number, body: ManualOrderInput) =>
    request<{ run_id: number; executed: number; snapshot: LiveRunSnapshot }>(
      `/live/${id}/manual-order`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  liveGreeksHistory: (id: number) => request<GreeksHistory>(`/live/${id}/greeks-history`),
  liveTrades: (id: number) => request<{ run_id: number; trades: Trade[] }>(`/live/${id}/trades`),
  liveDeployments: (status?: string) =>
    request<Deployment[]>(`/live/deployments${status ? `?status=${status}` : ""}`),
  liveSummary: () => request<LiveSummary>("/live/summary"),
  // In-app alerts feed (mobile Alerts screen + bell badge; rows from notify/in_app).
  alertsList: (limit = 100) =>
    request<{ unread: number; alerts: { id: number; ts: string | null; title: string;
      message: string; level: string; read: boolean }[] }>(`/alerts?limit=${limit}`),
  alertsMarkRead: () => request<{ marked: number }>("/alerts/mark-read", { method: "POST" }),
  liveForceEntry: (id: number) =>
    request<{ armed: boolean; note: string }>(`/live/${id}/force-entry`, { method: "POST" }),
  liveAckOrderError: (id: number) =>
    request<{ cleared: string | null }>(`/live/${id}/ack-order-error`, { method: "POST" }),
  liveArchive: (id: number) => request(`/live/${id}/archive`, { method: "POST" }),
  liveUnarchive: (id: number) => request(`/live/${id}/unarchive`, { method: "POST" }),
  liveDelete: (id: number) => request(`/live/${id}`, { method: "DELETE" }),
  liveUpdate: (id: number, body: { name?: string; notes?: string }) =>
    request(`/live/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
};

/** The personal net-worth tracker (/portfolio). One GET feeds every tab — splitting it
 * would let the KPI strip and the Allocation tab disagree for a frame. */
export const portfolio = {
  get: () => request<PortfolioPayload>("/portfolio"),
  createHolding: (body: HoldingInput) =>
    request<{ id: number }>("/portfolio/holdings", {
      method: "POST", body: JSON.stringify(body),
    }),
  updateHolding: (id: number, body: HoldingInput) =>
    request<{ id: number }>(`/portfolio/holdings/${id}`, {
      method: "PUT", body: JSON.stringify(body),
    }),
  deleteHolding: (id: number) =>
    request<{ deleted: number }>(`/portfolio/holdings/${id}`, { method: "DELETE" }),
  transactions: (holdingId: number) =>
    request<{ holding_id: number; transactions: TransactionRow[] }>(
      `/portfolio/transactions/${holdingId}`),
  addTransaction: (holdingId: number, body: TransactionInput) =>
    request<{ id: number }>(`/portfolio/holdings/${holdingId}/transactions`, {
      method: "POST", body: JSON.stringify(body),
    }),
  deleteTransaction: (txnId: number) =>
    request<{ deleted: number }>(`/portfolio/transactions/${txnId}`, { method: "DELETE" }),
  // replace:true is the safe re-import — appending would double every buy and the cost
  // basis would look merely "a bit high" rather than obviously wrong.
  importTransactions: (holdingId: number, rows: TransactionInput[], replace: boolean) =>
    request<{ imported: number; replaced: boolean; units: number | null;
              invested: number | null; oversold_units: number }>(
      "/portfolio/transactions/import",
      { method: "POST", body: JSON.stringify({ holding_id: holdingId, replace, rows }) }),
  // Parsed server-side so the preview the owner approves is produced by the same code
  // that performs the import — a browser parser would drift and land a partial history.
  parsePaste: (text: string) =>
    request<{
      rows: TransactionInput[];
      errors: string[];
      summary: { rows: number; buys: number; sells: number;
                 earliest: string | null; latest: string | null };
    }>("/portfolio/transactions/parse", { method: "POST", body: JSON.stringify({ text }) }),
  // The WIDE multi-symbol tracking sheet: buys and sells in different columns of one row.
  // Preview first — it reports the net FIFO position per symbol BEFORE anything is written.
  parseLedger: (text: string) =>
    request<LedgerPreview>("/portfolio/parse-ledger", {
      method: "POST", body: JSON.stringify({ text }),
    }),
  seed: (body: {
    text: string;
    symbols: { symbol: string; name: string; asset_class: string }[];
    broker_account_id: number | null;
    sync: "auto" | "manual";
    replace: boolean;
  }) => request<SeedResult>("/portfolio/seed", { method: "POST", body: JSON.stringify(body) }),
  dividends: () => request<{ dividends: DividendRow[] }>("/portfolio/dividends"),
  addDividend: (body: { holding_id: number; on_date: string; amount: number;
                        per_unit: number | null; note: string | null }) =>
    request<{ id: number }>("/portfolio/dividends", {
      method: "POST", body: JSON.stringify(body),
    }),
  deleteDividend: (id: number) =>
    request<{ deleted: number }>(`/portfolio/dividends/${id}`, { method: "DELETE" }),
  createBucket: (body: BucketInput) =>
    request<Bucket>("/portfolio/buckets", { method: "POST", body: JSON.stringify(body) }),
  updateBucket: (id: number, body: BucketInput) =>
    request<Bucket>(`/portfolio/buckets/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteBucket: (id: number) =>
    request<{ deleted: number }>(`/portfolio/buckets/${id}`, { method: "DELETE" }),
  createGoal: (body: GoalInput) =>
    request<Goal>("/portfolio/goals", { method: "POST", body: JSON.stringify(body) }),
  updateGoal: (id: number, body: GoalInput) =>
    request<Goal>(`/portfolio/goals/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteGoal: (id: number) =>
    request<{ deleted: number }>(`/portfolio/goals/${id}`, { method: "DELETE" }),
  saveTargets: (body: { class_targets?: Record<string, number>;
                        kind_targets?: Record<string, number> }) =>
    request<{ class_targets: Record<string, number>; kind_targets: Record<string, number> }>(
      "/portfolio/settings", { method: "PUT", body: JSON.stringify(body) }),
  sync: (holdingIds?: number[]) =>
    request<SyncReport>("/portfolio/sync", {
      method: "POST", body: JSON.stringify({ holding_ids: holdingIds ?? null }),
    }),
  // Cached-file lookup, no network — this runs on every keystroke in the fund picker.
  searchFunds: (q: string) =>
    request<{ results: { isin: string; scheme_code: string; name: string;
                         nav: number; as_of: string }[] }>(
      `/portfolio/funds/search?q=${encodeURIComponent(q)}`),
  refreshFunds: () =>
    request<{ schemes: number; as_of: string | null; stale_days: number | null;
              has_previous: boolean }>("/portfolio/funds/refresh", { method: "POST" }),
};

export const brokers = {
  list: () => request<BrokerAccount[]>("/brokers"),
  connect: (body: BrokerConnectRequest) =>
    request<BrokerAccount>("/brokers", { method: "POST", body: JSON.stringify(body) }),
  loginUrl: (id: number) => request<{ login_url: string }>(`/brokers/${id}/login-url`),
  login: (id: number, requestToken: string) =>
    request<BrokerAccount>(`/brokers/${id}/login`, {
      method: "POST",
      body: JSON.stringify({ request_token: requestToken }),
    }),
  refreshCache: (
    id: number,
    body: { symbols?: string[]; universe?: string; start_date?: string },
  ) =>
    request<{ account_id: number; refreshed: Record<string, { rows?: number; last_date?: string | null; error?: string }> }>(
      `/brokers/${id}/refresh-cache`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  // Fetch & cache the MCX GOLD futures series (underlying for the synthetic GOLD chain).
  refreshGold: (id: number, body: { start_date?: string }) =>
    request<{ account_id: number; refreshed: Record<string, { rows?: number; last_date?: string | null; error?: string }> }>(
      `/brokers/${id}/refresh-gold`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  arm: (id: number) => request<BrokerAccount>(`/brokers/${id}/arm`, { method: "POST" }),
  disarm: (id: number) => request<BrokerAccount>(`/brokers/${id}/disarm`, { method: "POST" }),
  remove: (id: number) => request<{ deleted: number }>(`/brokers/${id}`, { method: "DELETE" }),
};

/** Fetch a file with auth and trigger a browser download of the resulting blob. */
async function downloadCsv(path: string, filename: string): Promise<void> {
  const resp = await fetch(`${BASE}${path}`, { headers: authHeaders() });
  if (!resp.ok) {
    if (resp.status === 401 && window.location.pathname !== "/login") {
      clearToken();
      window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
    }
    throw new Error(`${resp.status}: ${resp.statusText}`);
  }
  const url = URL.createObjectURL(await resp.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/** WebSocket URL for the live feed, proxied through the dev server / same origin.
 *  A WS can't send an Authorization header, so the token rides as a query param. */
export function liveWsUrl(): string {
  const token = getToken();
  const q = token ? `?token=${encodeURIComponent(token)}` : "";
  if (apiOrigin) {
    // Mobile shell: derive ws(s):// from the configured backend origin — the webview's
    // window.location is capacitor://localhost, not the backend.
    return `${apiOrigin.replace(/^http/, "ws")}${BASE}/live/ws${q}`;
  }
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}${BASE}/live/ws${q}`;
}
