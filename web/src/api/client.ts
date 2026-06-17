import type {
  BacktestRequest,
  BacktestResponse,
  BenchmarkPoint,
  BrokerAccount,
  BrokerConnectRequest,
  CompareRun,
  DataCoverage,
  DataSummary,
  DataSymbol,
  DataSymbolDetail,
  AnalysisRunItem,
  Deployment,
  DerivCoverage,
  FuturesSeries,
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
  WatchRow,
} from "../types";

const BASE = "/api/v1";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!resp.ok) {
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
  strategies: () => request<{ strategies: string[] }>("/strategies"),
  universes: () => request<Universe[]>("/universes"),
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
  backtest: (body: BacktestRequest) =>
    request<BacktestResponse>("/backtest", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  tradesCsvUrl: (id: number) => `${BASE}/runs/${id}/trades.csv`,
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
  optionsRefresh: (body: { underlyings: string[]; start_date: string; end_date: string }) =>
    request<RefreshResult>("/data/options/refresh", { method: "POST", body: JSON.stringify(body) }),
  futuresUnderlyings: () => request<UnderlyingList>("/data/futures/underlyings"),
  futuresCoverage: (u: string) =>
    request<DerivCoverage>(`/data/futures/${encodeURIComponent(u)}/coverage`),
  futuresSeries: (u: string) =>
    request<FuturesSeries>(`/data/futures/${encodeURIComponent(u)}/series`),
  futuresRefresh: (body: { underlyings: string[]; start_date: string; end_date: string }) =>
    request<RefreshResult>("/data/futures/refresh", { method: "POST", body: JSON.stringify(body) }),

  // --- live / paper ---
  liveList: () => request<LiveRunSnapshot[]>("/live"),
  liveStart: (body: StartLiveRequest) =>
    request<LiveRunSnapshot>("/live/start", { method: "POST", body: JSON.stringify(body) }),
  liveRefresh: (id: number) =>
    request<LiveRunSnapshot>(`/live/${id}/refresh`, { method: "POST" }),
  liveRunDecision: (id: number) =>
    request<{ run_id: number; trades: unknown[] }>(`/live/${id}/run-decision`, { method: "POST" }),
  liveStop: (id: number) => request<{ stopped: number }>(`/live/${id}/stop`, { method: "POST" }),
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

/** WebSocket URL for the live feed, proxied through the dev server / same origin. */
export function liveWsUrl(): string {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}${BASE}/live/ws`;
}
