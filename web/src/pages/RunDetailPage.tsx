import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import ReportView from "../components/ReportView";
import { Card, ErrorBox, Spinner } from "../components/ui";
import { formatParamValue, orderedParamKeys, paramLabel } from "../lib/params";
import type { ForwardTestPrefill } from "../types";

function ParametersCard({
  capital,
  params,
  fallbackDates,
}: {
  capital: number | null;
  params: Record<string, unknown>;
  fallbackDates: { start?: string; end?: string };
}) {
  // Merge capital in, and backfill start/end from the equity curve for older runs.
  const merged: Record<string, unknown> = { capital, ...params };
  if (merged.start_date == null && fallbackDates.start) merged.start_date = fallbackDates.start;
  if (merged.end_date == null && fallbackDates.end) merged.end_date = fallbackDates.end;

  const symbols = Array.isArray(merged.symbols) ? (merged.symbols as string[]) : null;
  const keys = orderedParamKeys(Object.keys(merged));

  return (
    <Card>
      <div className="text-sm font-medium text-slate-300 mb-3">Input parameters</div>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
        {keys.map((k) => (
          <div key={k} className="rounded-md bg-slate-800/40 px-3 py-2">
            <div className="text-slate-400 text-xs">{paramLabel(k)}</div>
            <div>{formatParamValue(k, merged[k])}</div>
          </div>
        ))}
      </div>
      {symbols && symbols.length > 0 && (
        <details className="mt-3 text-sm">
          <summary className="cursor-pointer text-slate-400 hover:text-slate-200">
            Show {symbols.length} symbols
          </summary>
          <div className="mt-2 text-slate-300 text-xs leading-relaxed">{symbols.join(", ")}</div>
        </details>
      )}
    </Card>
  );
}

export default function RunDetailPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const runId = Number(id);
  const { data, isLoading, error } = useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.run(runId),
    enabled: Number.isFinite(runId),
  });

  const [editing, setEditing] = useState(false);
  const [name, setName] = useState("");
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);

  // Seed the edit fields once the run loads.
  useEffect(() => {
    if (data) {
      setName(data.name ?? "");
      setNotes(data.notes ?? "");
    }
  }, [data]);

  if (isLoading) return <Spinner />;
  if (error) return <ErrorBox message={(error as Error).message} />;
  if (!data) return null;

  function forwardTest() {
    const prefill: ForwardTestPrefill = {
      strategy_id: data!.strategy_id,
      name: data!.name,
      capital: data!.capital,
      params: data!.params,
    };
    // Carry the backtest's exact config into the Live deploy form.
    navigate("/live/new", { state: { prefill } });
  }

  async function saveEdit() {
    setBusy(true);
    try {
      await api.runUpdate(runId, { name: name.trim(), notes: notes.trim() });
      await queryClient.invalidateQueries({ queryKey: ["run", runId] });
      queryClient.invalidateQueries({ queryKey: ["runs"] });
      setEditing(false);
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!confirm(`Delete "${data!.name}" permanently? This removes its report and trades.`)) return;
    setBusy(true);
    try {
      await api.runDelete(runId);
      queryClient.invalidateQueries({ queryKey: ["runs"] });
      navigate("/");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3 flex-wrap">
        <Link to="/" className="text-slate-400 hover:text-slate-200 text-sm">
          ← Runs
        </Link>
        <h1 className="text-lg font-semibold">
          {data.name || `Run #${runId}`} <span className="text-slate-500 text-sm">· {data.strategy_id}</span>
        </h1>
        <div className="ml-auto flex items-center gap-2 text-sm">
          <button onClick={() => setEditing((v) => !v)} className="rounded-md bg-slate-800 hover:bg-slate-700 px-3 py-1.5">
            {editing ? "Close" : "Edit name/notes"}
          </button>
          <button onClick={remove} disabled={busy} className="rounded-md bg-rose-950 hover:bg-rose-900 text-rose-300 px-3 py-1.5 disabled:opacity-50">
            Delete
          </button>
          <button onClick={forwardTest} className="rounded-md bg-brand hover:bg-brand-light px-3 py-1.5 font-medium">
            Forward-test →
          </button>
        </div>
      </div>

      {editing && (
        <div className="rounded-xl border border-slate-800 bg-slate-900/50 p-4 space-y-2">
          <input
            className="w-full rounded bg-slate-800 border border-slate-700 px-3 py-2 text-sm"
            placeholder="name"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <textarea
            className="w-full rounded bg-slate-800 border border-slate-700 px-3 py-2 text-sm"
            rows={2}
            placeholder="notes"
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
          />
          <button onClick={saveEdit} disabled={busy} className="rounded-md bg-brand hover:bg-brand-light px-4 py-1.5 text-sm font-medium disabled:opacity-50">
            Save
          </button>
        </div>
      )}

      {data.notes && !editing && <div className="text-sm text-slate-400">{data.notes}</div>}

      <ParametersCard
        capital={data.capital}
        params={data.params}
        fallbackDates={{
          start: data.report.equity_curve?.[0]?.date,
          end: data.report.equity_curve?.[(data.report.equity_curve?.length ?? 0) - 1]?.date,
        }}
      />

      <ReportView report={data.report} trades={data.trades} csvUrl={api.tradesCsvUrl(runId)} runId={runId} />
    </div>
  );
}
