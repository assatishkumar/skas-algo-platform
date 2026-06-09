import type {
  BacktestRequest,
  BacktestResponse,
  BenchmarkPoint,
  BrokerAccount,
  BrokerConnectRequest,
  CompareRun,
  Deployment,
  LiveRunSnapshot,
  OverrideInput,
  Report,
  RunSummary,
  StartLiveRequest,
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
  runBenchmark: (id: number, index: string) =>
    request<{ index: string; points: BenchmarkPoint[] }>(
      `/runs/${id}/benchmark?index=${encodeURIComponent(index)}`,
    ),

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
  liveAddOverride: (id: number, ov: OverrideInput) =>
    request<{ run_id: number; overrides: number }>(`/live/${id}/overrides`, {
      method: "POST",
      body: JSON.stringify(ov),
    }),
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
  arm: (id: number) => request<BrokerAccount>(`/brokers/${id}/arm`, { method: "POST" }),
  disarm: (id: number) => request<BrokerAccount>(`/brokers/${id}/disarm`, { method: "POST" }),
  remove: (id: number) => request<{ deleted: number }>(`/brokers/${id}`, { method: "DELETE" }),
};

/** WebSocket URL for the live feed, proxied through the dev server / same origin. */
export function liveWsUrl(): string {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}${BASE}/live/ws`;
}
