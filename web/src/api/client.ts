import type {
  BacktestRequest,
  BacktestResponse,
  Report,
  RunSummary,
  Trade,
  Universe,
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
  runs: () => request<RunSummary[]>("/runs"),
  run: (id: number) =>
    request<{ report: Report; strategy_id: string; trades: Trade[] }>(`/runs/${id}`),
  backtest: (body: BacktestRequest) =>
    request<BacktestResponse>("/backtest", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  tradesCsvUrl: (id: number) => `${BASE}/runs/${id}/trades.csv`,
};
