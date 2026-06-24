import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { ErrorBox, Spinner, timeAgo } from "../components/ui";
import { KebabMenu, Segmented, Tag, type MenuItem } from "../components/redesign";
import { formatInr, pct } from "../lib/format";
import type { ForwardTestPrefill, RunSummary } from "../types";

const STRATEGY_LABELS: Record<string, string> = {
  sst_lifo: "SST (LIFO)",
  sst_fifo: "SST (FIFO)",
  short_premium: "Short Premium (options)",
  call_ratio_monthly: "Call Ratio Monthly (options)",
  put_ratio_monthly: "Put Ratio Monthly (options)",
  batman_ratio_monthly: "Batman Ratio Monthly (options)",
  hni_weekly: "HNI Weekly (options)",
  staggered_covered_call: "Staggered Covered Call (options)",
  custom_options: "Custom options",
  supertrend_momentum: "supertrend_momentum",
};
const strategyLabel = (id: string) => STRATEGY_LABELS[id] ?? id;
const ret = (r: RunSummary) => r.metrics["Total Return %"] ?? 0;
const isBatch = (r: RunSummary) => !!r.batch_id;

/** Per-run navigation + lifecycle actions, reused by the winner card and ranked rows. */
function useRunActions(run: RunSummary, onChanged: () => void) {
  const navigate = useNavigate();
  return {
    open: () => navigate(`/runs/${run.run_id}`),
    forwardTest: async () => {
      const full = await api.run(run.run_id);
      const prefill: ForwardTestPrefill = { strategy_id: full.strategy_id, name: full.name, capital: full.capital, params: full.params };
      navigate("/live/new", { state: { prefill } });
    },
    clone: async () => {
      const full = await api.run(run.run_id);
      navigate("/backtest?tab=new", { state: { clonePrefill: { strategy_id: full.strategy_id, name: full.name, capital: full.capital, params: full.params } } });
    },
    archive: () => api.runArchive(run.run_id).then(onChanged),
    unarchive: () => api.runUnarchive(run.run_id).then(onChanged),
    del: () => { if (confirm(`Delete "${run.name}" permanently? This removes its report and trades.`)) api.runDelete(run.run_id).then(onChanged); },
  };
}

function menuFor(run: RunSummary, a: ReturnType<typeof useRunActions>, navigate: ReturnType<typeof useNavigate>): MenuItem[] {
  return [
    { label: "Clone", onClick: a.clone },
    run.archived ? { label: "Unarchive", onClick: a.unarchive } : { label: "Archive", onClick: a.archive },
    { label: "Edit name / notes", onClick: () => navigate(`/runs/${run.run_id}`) },
    { label: "Delete", tone: "danger", onClick: a.del },
  ];
}

function Metric({ label, value, tone, big }: { label: string; value: string; tone?: "pos" | "danger"; big?: boolean; }) {
  return (
    <div className="text-right">
      <div className={`text-[11px] ${big ? "opacity-90" : "text-[var(--muted)]"}`}>{label}</div>
      <div className={`tabular-nums font-['Space_Grotesk'] font-bold ${big ? "text-[25px]" : "text-sm"} ${tone === "pos" ? "text-[var(--pos)]" : tone === "danger" ? "text-[var(--danger)]" : ""}`}>{value}</div>
    </div>
  );
}

/** Winner (rank 1) — full-bleed teal gradient card. */
function WinnerCard({ run, isTemplate, onChanged }: { run: RunSummary; isTemplate: boolean; onChanged: () => void }) {
  const a = useRunActions(run, onChanged);
  const m = run.metrics;
  return (
    <div className="rounded-[16px] p-5 text-white bg-[linear-gradient(110deg,#0f9e90,#12b3a4_60%,#2bc7a6)]">
      <div className="flex items-center gap-4 flex-wrap">
        <div className="flex h-[46px] w-[46px] items-center justify-center rounded-[12px] bg-white/20 text-2xl font-bold font-['Space_Grotesk']">1</div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 flex-wrap text-[11.5px]">
            <span className="rounded-[7px] bg-white/20 px-1.5 py-0.5 font-semibold">★ BEST RUN</span>
            {isTemplate && <span className="rounded-[7px] bg-[var(--warn-bg)] text-[var(--warn-text)] px-1.5 py-0.5 font-semibold">★ template</span>}
            <span className="rounded-[7px] bg-white/15 px-1.5 py-0.5">{isBatch(run) ? "Batch" : "Individual"}</span>
            <span className="opacity-90">#{run.run_id} · {timeAgo(run.started_at)} · {(m["Total Trades"] ?? 0).toLocaleString("en-IN")} trades</span>
          </div>
          <div className="font-semibold font-['Space_Grotesk'] text-lg truncate mt-0.5">{run.name}</div>
        </div>
        <div className="flex items-center gap-5">
          <div className="text-right"><div className="text-[11px] opacity-90">Final equity</div><div className="tabular-nums font-['Space_Grotesk'] font-bold text-sm">{formatInr(m["Final Equity"])}</div></div>
          <div className="text-right"><div className="text-[11px] opacity-90">Max DD</div><div className="tabular-nums font-['Space_Grotesk'] font-bold text-sm">{pct(m["Max Drawdown %"])}</div></div>
          <div className="text-right"><div className="text-[11px] opacity-90">Sharpe</div><div className="tabular-nums font-['Space_Grotesk'] font-bold text-sm">—</div></div>
          <div className="text-right"><div className="text-[11px] opacity-90">Win rate</div><div className="tabular-nums font-['Space_Grotesk'] font-bold text-sm">{(m["Win Rate %"] ?? 0).toFixed(0)}%</div></div>
          <div className="text-right"><div className="text-[11px] opacity-90">Return</div><div className="tabular-nums font-['Space_Grotesk'] font-bold text-[25px]">{pct(ret(run))}</div></div>
          <div className="flex flex-col gap-1.5">
            <button onClick={a.open} className="rounded-[10px] bg-white text-[#0d6b4f] px-3 py-1.5 text-xs font-semibold">Open</button>
            <button onClick={a.forwardTest} className="rounded-[10px] bg-white/20 text-white px-3 py-1.5 text-xs font-semibold">Forward-test →</button>
          </div>
        </div>
      </div>
    </div>
  );
}

/** Ranks 2…n — token-styled rows. */
function RankedRow({ run, rank, isTemplate, onChanged }: { run: RunSummary; rank: number; isTemplate: boolean; onChanged: () => void }) {
  const navigate = useNavigate();
  const a = useRunActions(run, onChanged);
  const m = run.metrics;
  return (
    <div className="rounded-[13px] border border-[var(--border)] bg-[var(--card)] hover:bg-[var(--row-hover)] px-4 py-3 flex items-center gap-4">
      <div className="flex h-8 w-8 items-center justify-center rounded-[9px] bg-[var(--chip)] text-[var(--chip-text)] text-sm font-bold font-['Space_Grotesk'] shrink-0">{rank}</div>
      <div className="min-w-0 flex-1">
        <Link to={`/runs/${run.run_id}`} className="font-semibold font-['Space_Grotesk'] truncate hover:text-[var(--accent-deep)] text-[var(--strong)]">{run.name}</Link>
        <div className="mt-0.5 flex items-center gap-1.5 flex-wrap text-[11.5px] text-[var(--muted)]">
          {isBatch(run)
            ? <Tag bg="var(--ok-bg)" color="var(--ok-text)">Batch</Tag>
            : <Tag>Individual</Tag>}
          {isTemplate && <span className="text-[var(--warn-text)]">★ template</span>}
          <span>#{run.run_id} · {timeAgo(run.started_at)} · {(m["Total Trades"] ?? 0).toLocaleString("en-IN")} trades</span>
        </div>
      </div>
      <div className="hidden md:flex items-center gap-5 shrink-0">
        <Metric label="Final equity" value={formatInr(m["Final Equity"])} />
        <Metric label="Max DD" value={pct(m["Max Drawdown %"])} tone="danger" />
        <Metric label="Sharpe" value="—" />
        <Metric label="Win rate" value={`${(m["Win Rate %"] ?? 0).toFixed(0)}%`} />
        <Metric label="Return" value={pct(ret(run))} tone={ret(run) >= 0 ? "pos" : "danger"} />
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <button onClick={a.open} className="rounded-[10px] bg-[var(--chip)] text-[var(--chip-text)] px-3 py-1.5 text-xs">Open</button>
        <button onClick={a.forwardTest} className="rounded-[10px] bg-[var(--ft)] text-white px-3 py-1.5 text-xs">Forward-test →</button>
        <KebabMenu items={menuFor(run, a, navigate)} />
      </div>
    </div>
  );
}

type TypeFilter = "all" | "Batch" | "Individual";

export default function RunsPage({ embedded = false }: { embedded?: boolean } = {}) {
  const [tab, setTab] = useState<"active" | "archived">("active");
  const [typeFilter, setTypeFilter] = useState<TypeFilter>("all");
  const [search, setSearch] = useState("");
  const [openMap, setOpenMap] = useState<Record<string, boolean>>({});
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const { data, isLoading, error } = useQuery({ queryKey: ["runs", tab], queryFn: () => api.runs(tab) });
  const { data: templatesData } = useQuery({ queryKey: ["templates"], queryFn: api.templates });
  const templateRunIds = new Set(Object.values(templatesData?.templates ?? {}).map((t) => t.run_id));
  const onChanged = () => queryClient.invalidateQueries({ queryKey: ["runs"] });

  if (isLoading) return <Spinner />;
  if (error) return <ErrorBox message={(error as Error).message} />;
  const runs = data ?? [];

  const q = search.trim().toLowerCase();
  const filtered = runs.filter((r) => {
    if (typeFilter === "Batch" && !isBatch(r)) return false;
    if (typeFilter === "Individual" && isBatch(r)) return false;
    if (!q) return true;
    return r.name.toLowerCase().includes(q) || r.strategy_id.toLowerCase().includes(q) || (r.notes ?? "").toLowerCase().includes(q);
  });

  // Group by strategy (most-recent-first), rank each group's runs by return desc.
  const order: string[] = [];
  const byStrategy = new Map<string, RunSummary[]>();
  for (const r of filtered) {
    if (!byStrategy.has(r.strategy_id)) { byStrategy.set(r.strategy_id, []); order.push(r.strategy_id); }
    byStrategy.get(r.strategy_id)!.push(r);
  }

  return (
    <div className="space-y-4">
      {/* Runs filters */}
      <div className="flex items-center gap-3 flex-wrap">
        <Segmented value={tab} onChange={setTab} options={[{ value: "active", label: "Active" }, { value: "archived", label: "Archived" }]} />
        <span className="text-xs text-[var(--muted)]">Type</span>
        <Segmented value={typeFilter} onChange={setTypeFilter} options={[{ value: "all", label: "All" }, { value: "Batch", label: "Batch" }, { value: "Individual", label: "Individual" }]} />
        <span className="rounded-full bg-[var(--chip)] text-[var(--chip-text)] px-3 py-1 text-xs font-medium">Return ↓</span>
        <input
          className="ml-auto rounded-[10px] bg-[var(--field)] border border-[var(--field-border)] px-3 py-1.5 text-sm w-56 text-[var(--strong)] placeholder:text-[var(--faint)] focus:outline-none focus:border-[var(--accent)]"
          placeholder="Search runs" value={search} onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      {filtered.length === 0 ? (
        <div className="rounded-[18px] border border-[var(--border)] bg-[var(--card)] p-6 text-[var(--muted)]">
          {embedded ? "No runs yet. Open the New backtest tab to launch one." : <>No runs yet. <Link to="/backtest?tab=new" className="text-[var(--accent-deep)] underline">new backtest</Link>.</>}
        </div>
      ) : (
        order.map((sid) => {
          const rs = [...byStrategy.get(sid)!].sort((x, y) => ret(y) - ret(x)); // rank by return desc
          const open = q ? true : (openMap[sid] ?? false);
          const best = rs.length ? ret(rs[0]) : 0;
          const top3 = rs.slice(0, 3).map((r) => r.run_id).join(",");
          return (
            <div key={sid} className="rounded-[18px] border border-[var(--border)] bg-[var(--card)] p-4">
              <button onClick={() => setOpenMap((g) => ({ ...g, [sid]: !(g[sid] ?? false) }))} className="w-full flex items-center justify-between gap-3">
                <div className="flex items-center gap-2.5 min-w-0">
                  <span className="text-[var(--muted)] text-sm w-3 shrink-0">{open ? "▾" : "▸"}</span>
                  <span className="font-bold font-['Space_Grotesk'] text-[19px] truncate text-[var(--strong)]">{strategyLabel(sid)}</span>
                  <Tag>{rs.length} runs</Tag>
                </div>
                <span className="text-sm text-[var(--muted)] shrink-0">best <span className="text-[var(--pos)] font-semibold tabular-nums">{pct(best)}</span></span>
              </button>
              {open && (
                <div className="mt-3 space-y-2.5">
                  <div className="flex items-center justify-between text-[11px] text-[var(--faint)] uppercase tracking-wide">
                    <span>Ranked by return · {rs.length} run{rs.length === 1 ? "" : "s"}</span>
                    {rs.length > 1 && (
                      <button onClick={() => navigate(`/compare?ids=${top3}`)} className="rounded-full bg-[var(--chip)] text-[var(--chip-text)] px-2.5 py-0.5 normal-case">Compare top 3 →</button>
                    )}
                  </div>
                  <WinnerCard run={rs[0]} isTemplate={templateRunIds.has(rs[0].run_id)} onChanged={onChanged} />
                  {rs.slice(1).map((r, i) => (
                    <RankedRow key={r.run_id} run={r} rank={i + 2} isTemplate={templateRunIds.has(r.run_id)} onChanged={onChanged} />
                  ))}
                </div>
              )}
            </div>
          );
        })
      )}
    </div>
  );
}
