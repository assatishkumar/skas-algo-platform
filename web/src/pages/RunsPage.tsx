import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { Badge, Card, ErrorBox, Spinner, StatusPill, timeAgo } from "../components/ui";
import { formatInr, pct } from "../lib/format";
import type { ForwardTestPrefill, RunSummary } from "../types";

/** A backtest run tile: name, notes, key metrics, and rename/archive/delete actions. */
function RunTile({
  run,
  onChanged,
  selectMode = false,
  selected = false,
  onSelect,
  isTemplate = false,
}: {
  run: RunSummary;
  onChanged: () => void;
  selectMode?: boolean;
  selected?: boolean;
  onSelect?: () => void;
  isTemplate?: boolean;
}) {
  const navigate = useNavigate();
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(run.name);
  const [notes, setNotes] = useState(run.notes ?? "");
  const [busy, setBusy] = useState(false);

  const ret = run.metrics["Total Return %"];
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
    } finally {
      setBusy(false);
      onChanged();
    }
  };

  async function saveEdit() {
    await act(() => api.runUpdate(run.run_id, { name: name.trim(), notes: notes.trim() }));
    setEditing(false);
  }

  async function forwardTest() {
    // The list summary lacks capital/params, so pull the full run first.
    const full = await api.run(run.run_id);
    const prefill: ForwardTestPrefill = {
      strategy_id: full.strategy_id,
      name: full.name,
      capital: full.capital,
      params: full.params,
    };
    navigate("/live/new", { state: { prefill } });
  }

  return (
    <Card className={selected ? "border-brand" : ""}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex items-start gap-2">
          {selectMode && (
            <input
              type="checkbox"
              className="mt-1"
              checked={selected}
              onChange={onSelect}
            />
          )}
          <div className="min-w-0">
          {editing ? (
            <input
              className="w-full rounded bg-slate-800 border border-slate-700 px-2 py-1 text-sm"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          ) : (
            <Link to={`/runs/${run.run_id}`} className="font-medium truncate hover:text-brand-light">
              {run.name}
            </Link>
          )}
          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-slate-400">
            {run.archived && <StatusPill status="archived" />}
            {isTemplate && (
              <span className="text-amber-700 dark:text-amber-300" title="Strategy template — new backtests prefill from this run">
                ★ template
              </span>
            )}
            <Badge>{run.strategy_id}</Badge>
            <Badge>{run.mode}</Badge>
            <span>#{run.run_id}</span>
            <span>· {timeAgo(run.started_at)}</span>
          </div>
          </div>
        </div>
        <div className="text-right text-sm shrink-0">
          <div className="text-slate-400 text-xs">Return</div>
          <div className={ret >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400"}>{pct(ret)}</div>
        </div>
      </div>

      {/* Notes (preview + inline edit) */}
      {editing ? (
        <textarea
          className="mt-2 w-full rounded bg-slate-800 border border-slate-700 px-2 py-1 text-sm"
          rows={2}
          placeholder="notes"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
        />
      ) : run.notes ? (
        <div className="mt-2 text-xs text-slate-400 line-clamp-2">{run.notes}</div>
      ) : null}

      {/* Key metrics */}
      <div className="mt-3 grid grid-cols-3 gap-2 text-sm">
        <div className="rounded-md bg-slate-800/40 px-2.5 py-1.5">
          <div className="text-slate-400 text-[11px]">Final equity</div>
          {formatInr(run.metrics["Final Equity"])}
        </div>
        <div className="rounded-md bg-slate-800/40 px-2.5 py-1.5">
          <div className="text-slate-400 text-[11px]">Max DD</div>
          <span className="text-rose-600 dark:text-rose-400">{pct(run.metrics["Max Drawdown %"])}</span>
        </div>
        <div className="rounded-md bg-slate-800/40 px-2.5 py-1.5">
          <div className="text-slate-400 text-[11px]">Trades</div>
          {run.metrics["Total Trades"]}
        </div>
      </div>

      {/* Actions */}
      <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
        <Link to={`/runs/${run.run_id}`} className="rounded bg-slate-800 hover:bg-slate-700 px-3 py-1.5">
          Open
        </Link>
        <button onClick={forwardTest} className="rounded bg-emerald-900 hover:bg-emerald-800 text-white px-3 py-1.5">
          Forward-test →
        </button>
        {run.archived ? (
          <button
            onClick={() => act(() => api.runUnarchive(run.run_id))}
            disabled={busy}
            className="rounded bg-slate-800 hover:bg-slate-700 px-3 py-1.5 disabled:opacity-50"
          >
            Unarchive
          </button>
        ) : (
          <button
            onClick={() => act(() => api.runArchive(run.run_id))}
            disabled={busy}
            className="rounded bg-slate-800 hover:bg-slate-700 px-3 py-1.5 disabled:opacity-50"
          >
            Archive
          </button>
        )}
        <button
          onClick={() => {
            if (confirm(`Delete "${run.name}" permanently? This removes its report and trades.`))
              act(() => api.runDelete(run.run_id));
          }}
          disabled={busy}
          className="rounded bg-rose-950 hover:bg-rose-900 text-rose-300 px-3 py-1.5 disabled:opacity-50"
        >
          Delete
        </button>
        {editing ? (
          <>
            <button onClick={saveEdit} disabled={busy} className="rounded bg-brand hover:bg-brand-light px-3 py-1.5 disabled:opacity-50">
              Save
            </button>
            <button onClick={() => { setEditing(false); setName(run.name); setNotes(run.notes ?? ""); }} className="text-slate-500 px-2">
              Cancel
            </button>
          </>
        ) : (
          <button onClick={() => setEditing(true)} className="ml-auto text-slate-500 hover:text-slate-300">
            Edit name/notes
          </button>
        )}
      </div>
    </Card>
  );
}

const TABS: { key: string; label: string }[] = [
  { key: "active", label: "Active" },
  { key: "archived", label: "Archived" },
];

const STRATEGY_LABELS: Record<string, string> = {
  sst_lifo: "SST (LIFO)",
  sst_fifo: "SST (FIFO)",
  short_premium: "Short Premium (options)",
  call_ratio_monthly: "Call Ratio Monthly (options)",
  put_ratio_monthly: "Put Ratio Monthly (options)",
  batman_ratio_monthly: "Batman Ratio Monthly (options)",
  hni_weekly: "HNI Weekly (options)",
  staggered_covered_call: "Staggered Covered Call (options)",
};
const strategyLabel = (id: string) => STRATEGY_LABELS[id] ?? id;

export default function RunsPage() {
  const [tab, setTab] = useState("active");
  const [search, setSearch] = useState("");
  const [compareMode, setCompareMode] = useState(false);
  const [selected, setSelected] = useState<number[]>([]);
  // Per-strategy collapse: a group is open if explicitly toggled, else the first group
  // (most recent strategy) defaults open and the rest collapsed.
  const [openMap, setOpenMap] = useState<Record<string, boolean>>({});
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const { data, isLoading, error } = useQuery({
    queryKey: ["runs", tab],
    queryFn: () => api.runs(tab),
  });
  const { data: templatesData } = useQuery({ queryKey: ["templates"], queryFn: api.templates });
  const templateRunIds = new Set(
    Object.values(templatesData?.templates ?? {}).map((t) => t.run_id),
  );

  const onChanged = () => queryClient.invalidateQueries({ queryKey: ["runs"] });

  const toggleSelect = (id: number) =>
    setSelected((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : prev.length >= 5 ? prev : [...prev, id],
    );

  if (isLoading) return <Spinner />;
  if (error) return <ErrorBox message={(error as Error).message} />;
  const runs = data ?? [];

  const q = search.trim().toLowerCase();
  const filtered = q
    ? runs.filter(
        (r) =>
          r.name.toLowerCase().includes(q) ||
          r.strategy_id.toLowerCase().includes(q) ||
          (r.notes ?? "").toLowerCase().includes(q),
      )
    : runs;

  // Group by strategy first (collapsible sections), preserving most-recent-first order.
  const strategyOrder: string[] = [];
  const byStrategy = new Map<string, RunSummary[]>();
  for (const r of filtered) {
    if (!byStrategy.has(r.strategy_id)) {
      byStrategy.set(r.strategy_id, []);
      strategyOrder.push(r.strategy_id);
    }
    byStrategy.get(r.strategy_id)!.push(r);
  }

  const tile = (r: RunSummary) => (
    <RunTile
      key={r.run_id}
      run={r}
      onChanged={onChanged}
      selectMode={compareMode}
      selected={selected.includes(r.run_id)}
      onSelect={() => toggleSelect(r.run_id)}
      isTemplate={templateRunIds.has(r.run_id)}
    />
  );

  // Within a strategy: nest the existing batch grouping, then loose runs.
  const renderGroupedRuns = (rs: RunSummary[]) => {
    const batches = new Map<string, RunSummary[]>();
    const loose: RunSummary[] = [];
    for (const r of rs) {
      if (r.batch_id) batches.set(r.batch_id, [...(batches.get(r.batch_id) ?? []), r]);
      else loose.push(r);
    }
    return (
      <div className="space-y-3">
        {Array.from(batches.entries()).map(([bid, brs]) => (
          <div key={bid} className="rounded-lg border border-slate-800 p-3 space-y-3">
            <div className="flex items-center justify-between">
              <div className="text-sm font-medium text-slate-300">Batch · {brs.length} runs</div>
              <button
                onClick={() => navigate(`/compare?ids=${brs.slice(0, 5).map((r) => r.run_id).join(",")}`)}
                className="rounded bg-slate-800 hover:bg-slate-700 px-3 py-1.5 text-xs"
              >
                Compare batch →
              </button>
            </div>
            <div className="grid gap-3 md:grid-cols-2">{brs.map(tile)}</div>
          </div>
        ))}
        {loose.length > 0 && <div className="grid gap-3 md:grid-cols-2">{loose.map(tile)}</div>}
      </div>
    );
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <h1 className="text-lg font-semibold">Runs</h1>
        <div className="flex items-center gap-2">
          <button
            onClick={() => {
              setCompareMode((v) => !v);
              setSelected([]);
            }}
            className={`rounded-md px-3 py-2 text-sm font-medium ${compareMode ? "bg-brand text-white" : "bg-slate-800 hover:bg-slate-700 text-slate-300"}`}
          >
            {compareMode ? "Cancel compare" : "Compare"}
          </button>
          <Link to="/new" className="rounded-md bg-brand hover:bg-brand-light px-3 py-2 text-sm font-medium">
            + New backtest
          </Link>
        </div>
      </div>

      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex gap-1 rounded-lg bg-slate-800/50 p-1 text-sm">
          {TABS.map((t) => (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              className={`rounded-md px-3 py-1 ${tab === t.key ? "bg-brand text-white" : "text-slate-400 hover:text-slate-200"}`}
            >
              {t.label}
            </button>
          ))}
        </div>
        <input
          className="rounded-md bg-slate-800 border border-slate-700 px-3 py-1.5 text-sm w-56 focus:outline-none focus:border-brand"
          placeholder="Search name / strategy / notes"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      {compareMode && (
        <div className="flex items-center justify-between gap-3 rounded-lg border border-slate-700 bg-slate-800/50 px-3 py-2 text-sm">
          <span className="text-slate-300">{selected.length} selected (pick 2–5)</span>
          <button
            disabled={selected.length < 2}
            onClick={() => navigate(`/compare?ids=${selected.join(",")}`)}
            className="rounded-md bg-brand hover:bg-brand-light px-3 py-1.5 font-medium disabled:opacity-40"
          >
            Compare selected →
          </button>
        </div>
      )}

      {filtered.length === 0 ? (
        <Card>
          <div className="text-slate-400">
            {tab === "active" ? (
              <>No runs yet. Start with a <Link to="/new" className="text-brand-light underline">new backtest</Link>.</>
            ) : (
              "No archived runs."
            )}
          </div>
        </Card>
      ) : (
        <div className="space-y-3">
          {strategyOrder.map((sid, i) => {
            const rs = byStrategy.get(sid)!;
            const open = openMap[sid] ?? i === 0;
            const best = Math.max(...rs.map((r) => r.metrics["Total Return %"] ?? -Infinity));
            return (
              <div key={sid} className="rounded-lg border border-slate-800">
                <button
                  onClick={() => setOpenMap((m) => ({ ...m, [sid]: !(m[sid] ?? i === 0) }))}
                  className="w-full flex items-center justify-between gap-3 px-3 py-2.5 hover:bg-slate-800/40"
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="text-slate-500">{open ? "▾" : "▸"}</span>
                    <span className="font-medium truncate">{strategyLabel(sid)}</span>
                    <Badge>{rs.length} run{rs.length === 1 ? "" : "s"}</Badge>
                  </div>
                  <div className="text-xs text-slate-400 shrink-0">
                    best <span className={best >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400"}>{pct(best)}</span>
                  </div>
                </button>
                {open && <div className="px-3 pb-3">{renderGroupedRuns(rs)}</div>}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
