import type {
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
  IntradayStraddleDeploy,
  DeltaNeutralDeploy,
  DoubleDiagonalDeploy,
  IronFlyDeploy,
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
    request<{ strategies: string[] }>(`/strategies?basis=${basis}`),
  // INTRADAY replays run as a background job: POST returns {job_id}, poll progress for
  // {done,total,day} and the full BacktestResponse-shaped result once status=="done".
  backtestIntraday: (body: BacktestRequest) =>
    request<{ job_id: string }>("/backtest/intraday", { method: "POST", body: JSON.stringify(body) }),
  backtestIntradayProgress: () => request<ReplayJobSnapshot>("/backtest/intraday/progress"),
  universes: () => request<Universe[]>("/universes"),
  universeSymbols: (name: string) =>
    request<{ name: string; symbols: string[] }>(`/universes/${encodeURIComponent(name)}/symbols`),
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
  runBenchmark: (id: number, index: string) =>
    request<{ index: string; points: BenchmarkPoint[] }>(
      `/runs/${id}/benchmark?index=${encodeURIComponent(index)}`,
    ),
  // --- trade analysis ---
  analysisRuns: () => request<AnalysisRunItem[]>("/analysis/runs"),
  runAnalysis: (id: number) => request<RunAnalysis>(`/runs/${id}/analysis`),
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
  smokeTestDeploy: (body: SmokeTestDeploy) =>
    request<LiveRunSnapshot>("/trade/smoke-test/deploy", { method: "POST", body: JSON.stringify(body) }),
  ironflyAdjust: (runId: number, on: boolean) =>
    request<{ ironfly_adjust: boolean; note: string }>(`/live/${runId}/ironfly-adjust`, { method: "POST", body: JSON.stringify({ on }) }),
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
