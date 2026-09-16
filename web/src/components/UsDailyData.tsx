/** The US daily-bar store (2026-09-16): coverage, staleness, and the refresh job — the
 *  second market's data card. Yahoo-fed, one call per symbol; no broker. */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { Card } from "./ui";

export function UsDailySection() {
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const { data, error } = useQuery({
    queryKey: ["us-daily"], queryFn: api.usDailyStore,
    refetchInterval: (q) => (q.state.data?.job.running ? 3000 : false),
  });
  const refresh = async (targets?: string[]) => {
    setBusy(true); setMsg(null);
    try {
      const out = await api.usDailyRefresh(targets);
      setMsg(`Refreshing ${out.symbols} symbols in the background — one Yahoo call each, about ${Math.ceil(out.symbols / 100)} min.`);
      qc.invalidateQueries({ queryKey: ["us-daily"] });
    } catch (e) { setMsg((e as Error).message); }
    finally { setBusy(false); }
  };
  if (error) return <Card><div className="text-sm text-rose-400">{(error as Error).message}</div></Card>;
  if (!data) return <Card><div className="text-sm text-slate-400">Loading…</div></Card>;
  const job = data.job;
  return (
    <Card>
      <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
        <div className="text-sm font-medium text-slate-300">
          US daily bars · S&amp;P 500 + Nasdaq-100
          <span className="ml-2 text-xs text-slate-500">Yahoo, split-adjusted OHLC, ten years on first fetch</span>
        </div>
        <div className="flex items-center gap-2 text-xs">
          <button onClick={() => refresh()} disabled={busy || job.running}
            title="Fetch the current S&P 500 and Nasdaq-100 lists, then fetch or top up every symbol's daily bars"
            className="rounded-md bg-brand hover:bg-brand-light px-2.5 py-1 text-xs font-medium disabled:opacity-50">
            {job.running ? `Refreshing ${job.done}/${job.total}…` : busy ? "Starting…" : "Refresh both universes"}
          </button>
          {data.stale_count > 0 && !job.running && (
            <span className="rounded px-2 py-0.5 bg-amber-950/50 text-amber-400 border border-amber-800"
              title={data.stale.join(", ")}>{data.stale_count} stale ⚠</span>
          )}
        </div>
      </div>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
        <div><div className="text-xs text-slate-500">Symbols cached</div><div className="text-lg tabular-nums">{data.symbols}</div>
          <div className="text-[11px] text-slate-500">{Object.entries(data.universes).map(([n, c]) => `${n} ${c}`).join(" · ")} listed</div></div>
        <div><div className="text-xs text-slate-500">Earliest bar</div><div className="text-lg tabular-nums">{data.first ?? "—"}</div></div>
        <div><div className="text-xs text-slate-500">Newest bar</div><div className="text-lg tabular-nums">{data.last ?? "—"}</div>
          {data.oldest_last && data.oldest_last !== data.last && <div className="text-[11px] text-slate-500">oldest symbol ends {data.oldest_last}</div>}</div>
        <div><div className="text-xs text-slate-500">Last refresh</div>
          <div className="text-sm tabular-nums">{job.finished_at ?? (job.running ? `running since ${job.started_at}` : "—")}</div>
          {job.result && <div className="text-[11px] text-slate-500">{job.result.error ? job.result.error : `${job.result.ok} ok · ${job.result.failed?.length ?? 0} failed${job.result.failed?.length ? `: ${job.result.failed.slice(0, 6).join(", ")}` : ""}`}</div>}
        </div>
      </div>
      {msg && <div className="mt-3 text-xs text-slate-400">{msg}</div>}
      <div className="mt-3 text-[11px] text-slate-500">
        Store: <code>{data.dir}</code>. Today's index constituents only — a backtest over them carries survivorship bias
        until the point-in-time table exists. Prices are the traded levels (split-adjusted, not dividend-adjusted).
      </div>
    </Card>
  );
}
