import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { Card } from "../components/ui";
import { formatParamValue, orderedParamKeys, paramLabel } from "../lib/params";
import EquityTradeAnalysis from "../components/analysis/EquityTradeAnalysis";

function RunParams({ params, capital }: { params: Record<string, unknown>; capital: number | null }) {
  const merged: Record<string, unknown> = { ...(capital != null ? { capital } : {}), ...params };
  const keys = orderedParamKeys(Object.keys(merged));
  if (!keys.length) return null;
  return (
    <Card>
      <div className="text-sm font-medium text-slate-300 mb-2">Run parameters</div>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-x-6 gap-y-1 text-sm">
        {keys.map((k) => (
          <div key={k} className="flex justify-between gap-3 border-b border-slate-800/50 py-0.5">
            <span className="text-slate-400">{paramLabel(k)}</span>
            <span className="tabular-nums">{formatParamValue(k, merged[k])}</span>
          </div>
        ))}
      </div>
    </Card>
  );
}

export default function AnalysisPage() {
  const [params, setParams] = useSearchParams();
  const runId = params.get("run") ? Number(params.get("run")) : null;
  const [search, setSearch] = useState("");

  const { data: runs = [] } = useQuery({ queryKey: ["analysisRuns"], queryFn: api.analysisRuns });
  const { data: analysis, isLoading } = useQuery({
    queryKey: ["runAnalysis", runId],
    queryFn: () => api.runAnalysis(runId!),
    enabled: runId != null,
  });

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return runs.filter(
      (r) => !q || (r.name ?? "").toLowerCase().includes(q) || (r.strategy_id ?? "").toLowerCase().includes(q),
    );
  }, [runs, search]);

  const select = (id: number) => {
    params.set("run", String(id));
    setParams(params, { replace: true });
  };

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold">Analyze trades</h1>
        <p className="text-sm text-slate-400">
          Pick a run (backtest, forward-test or live) to analyze its trades — round-trips grouped by
          stock, per-stock P&L, and per-trade charts.
        </p>
      </div>

      <Card>
        <div className="flex items-center gap-3 flex-wrap">
          <label className="text-sm text-slate-400 flex items-center gap-2">
            Run
            <select
              className="rounded-md bg-slate-800 border border-slate-700 px-2 py-1.5 text-sm min-w-[22rem]"
              value={runId ?? ""}
              onChange={(e) => e.target.value && select(Number(e.target.value))}
            >
              <option value="">Select a run…</option>
              {filtered.map((r) => (
                <option key={r.run_id} value={r.run_id}>
                  #{r.run_id} · {r.name ?? r.strategy_id} · {r.strategy_id} · {r.status}
                  {r.instrument_class === "DERIV" ? " · options" : ""}
                </option>
              ))}
            </select>
          </label>
          <input
            className="rounded-md bg-slate-800 border border-slate-700 px-3 py-1.5 text-sm w-56"
            placeholder="Filter by name / strategy"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
      </Card>

      {runId == null && (
        <Card><div className="text-slate-400 text-sm">Select a run above to see its trade analysis.</div></Card>
      )}
      {runId != null && isLoading && <Card><div className="text-slate-400 text-sm">Loading…</div></Card>}
      {analysis && <RunParams params={analysis.params ?? {}} capital={analysis.capital} />}
      {analysis && (
        analysis.instrument_class === "DERIV" ? (
          <Card>
            <div className="text-slate-300 font-medium mb-1">Options trade analysis</div>
            <div className="text-sm text-slate-400">
              Coming soon — options runs ({analysis.strategy_id}) will get a legs/greeks-aware view.
              For now, see the run's report page for the options analytics.
            </div>
          </Card>
        ) : (
          <EquityTradeAnalysis analysis={analysis} />
        )
      )}
    </div>
  );
}
