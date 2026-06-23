import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "../api/client";
import { Badge, Card, ErrorBox, Spinner } from "../components/ui";
import { formatInr, pct } from "../lib/format";
import { formatParamValue, orderedParamKeys, paramLabel } from "../lib/params";
import type { CompareCycle, CompareRun, Metrics } from "../types";

// Distinct colors for up to 5 runs; benchmark is amber.
const COLORS = ["#14b8a6", "#6366f1", "#ec4899", "#f59e0b", "#22c55e"];
const num = (v: number) => (v == null || Number.isNaN(v) ? "—" : v.toLocaleString("en-IN"));
const num2 = (v: number) => (v == null || Number.isNaN(v) ? "—" : v.toFixed(2));

type Better = "max" | "min" | undefined;
const ROWS: { key: keyof Metrics; label: string; fmt: (v: number) => string; better: Better }[] = [
  { key: "Total Return %", label: "Total Return", fmt: (v) => pct(v), better: "max" },
  { key: "CAGR %", label: "CAGR", fmt: (v) => pct(v), better: "max" },
  { key: "Final Equity", label: "Final Equity", fmt: (v) => formatInr(v), better: "max" },
  { key: "Max Drawdown %", label: "Max Drawdown", fmt: (v) => pct(v), better: "min" },
  { key: "Max Capital Used", label: "Max Capital Used", fmt: (v) => formatInr(v), better: "min" },
  { key: "Total Trades", label: "Total Trades", fmt: num, better: undefined },
  { key: "Win Rate %", label: "Win Rate", fmt: (v) => pct(v), better: "max" },
  { key: "Cash Balance", label: "Cash Balance", fmt: (v) => formatInr(v), better: undefined },
  { key: "Total Withdrawals", label: "Total Withdrawals", fmt: (v) => formatInr(v), better: undefined },
  { key: "Total Taxes", label: "Total Taxes", fmt: (v) => formatInr(v), better: "min" },
  { key: "Avg Monthly Profit Booking", label: "Avg Monthly Bookings", fmt: num2, better: "max" },
  { key: "Avg Monthly Profit (Pre-Tax)", label: "Avg Monthly Profit (Pre-Tax)", fmt: (v) => formatInr(v), better: "max" },
  { key: "Avg Monthly Profit (Post-Tax)", label: "Avg Monthly Profit (Post-Tax)", fmt: (v) => formatInr(v), better: "max" },
];

function bestIndex(runs: CompareRun[], key: keyof Metrics, better: Better): number | null {
  if (!better) return null;
  let bi = -1;
  let bv = better === "max" ? -Infinity : Infinity;
  runs.forEach((r, i) => {
    const v = r.metrics[key];
    if (v == null || Number.isNaN(v)) return;
    if ((better === "max" && v > bv) || (better === "min" && v < bv)) {
      bv = v;
      bi = i;
    }
  });
  return bi >= 0 ? bi : null;
}

const REASON_STYLE: Record<string, string> = {
  target: "text-emerald-600 dark:text-emerald-400",
  stop: "text-rose-600 dark:text-rose-400",
  time: "text-sky-600 dark:text-sky-400",
  expiry: "text-amber-600 dark:text-amber-400",
  manual: "text-slate-400",
  mixed: "text-violet-400",
};

function pnlCls(v: number | null | undefined): string {
  if (v == null) return "text-slate-600";
  return v > 0 ? "text-emerald-600 dark:text-emerald-400" : v < 0 ? "text-rose-600 dark:text-rose-400" : "text-slate-400";
}

/** Options-strategy comparison: per-run lifecycle stats side by side. */
function OptionsSummaryCompare({ runs }: { runs: CompareRun[] }) {
  const exits = (o: NonNullable<CompareRun["options"]>) => {
    const er = o.exit_reasons ?? {};
    const n = (k: string) => er[k]?.count ?? 0;
    return `${n("target")} / ${n("time")} / ${n("stop")}`;
  };
  const worst = (o: NonNullable<CompareRun["options"]>) =>
    o.cycles.length ? Math.min(...o.cycles.map((c) => c.net_pnl ?? c.realized_pnl)) : 0;
  const best = (o: NonNullable<CompareRun["options"]>) =>
    o.cycles.length ? Math.max(...o.cycles.map((c) => c.net_pnl ?? c.realized_pnl)) : 0;
  const rows: { label: string; fn: (o: NonNullable<CompareRun["options"]>) => number | string; fmt?: (v: number) => string; better?: Better }[] = [
    { label: "Net P&L (after charges)", fn: (o) => o.summary.net_after_charges, fmt: formatInr, better: "max" },
    { label: "F&O charges", fn: (o) => o.summary.total_charges, fmt: formatInr, better: "min" },
    { label: "Premium collected (net)", fn: (o) => o.summary.total_premium_collected, fmt: formatInr },
    { label: "Cycles entered", fn: (o) => o.summary.num_cycles },
    { label: "Win rate (cycles)", fn: (o) => o.summary.win_rate_pct, fmt: (v) => pct(v), better: "max" },
    { label: "Exits target / time / stop", fn: exits },
    { label: "Avg holding (days)", fn: (o) => o.summary.avg_holding_days, fmt: (v) => v.toFixed(1) },
    { label: "Best cycle (net)", fn: best, fmt: formatInr, better: "max" },
    { label: "Worst cycle (net)", fn: worst, fmt: formatInr, better: "max" },
    { label: "Max margin used", fn: (o) => o.summary.max_margin_used, fmt: formatInr, better: "min" },
  ];
  return (
    <Card>
      <div className="text-sm font-medium text-slate-300 mb-2">Options summary</div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm tabular-nums">
          <thead className="text-slate-400 text-left">
            <tr>
              <th className="py-1 pr-4">Metric</th>
              {runs.map((r, i) => (
                <th key={r.run_id} className="py-1 pr-4 text-right" style={{ color: COLORS[i % COLORS.length] }}>
                  {r.name}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const vals = runs.map((r) => (r.options ? row.fn(r.options) : null));
              let bi: number | null = null;
              if (row.better) {
                let bv = row.better === "max" ? -Infinity : Infinity;
                vals.forEach((v, i) => {
                  if (typeof v !== "number") return;
                  if ((row.better === "max" && v > bv) || (row.better === "min" && v < bv)) {
                    bv = v;
                    bi = i;
                  }
                });
              }
              return (
                <tr key={row.label} className="border-t border-slate-800">
                  <td className="py-1.5 pr-4 text-slate-400">{row.label}</td>
                  {vals.map((v, i) => (
                    <td key={runs[i].run_id} className={`py-1.5 pr-4 text-right ${i === bi ? "text-emerald-600 dark:text-emerald-400 font-semibold" : ""}`}>
                      {v == null ? "—" : typeof v === "number" ? (row.fmt ?? String)(v) : v}
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

/** Month-by-month position comparison: cycles aligned by entry month across runs, so
 * each run's entry (or skip), exit reason, and net P&L sit side by side. */
function CycleCompare({ runs }: { runs: CompareRun[] }) {
  const months = useMemo(() => {
    const set = new Set<string>();
    runs.forEach((r) => r.options?.cycles.forEach((c) => set.add(c.entry_date.slice(0, 7))));
    return Array.from(set).sort();
  }, [runs]);
  const byMonth = useMemo(
    () =>
      runs.map((r) => {
        const m = new Map<string, CompareCycle[]>();
        r.options?.cycles.forEach((c) => {
          const k = c.entry_date.slice(0, 7);
          m.set(k, [...(m.get(k) ?? []), c]);
        });
        return m;
      }),
    [runs],
  );
  if (!months.length) return null;
  return (
    <Card>
      <div className="text-sm font-medium text-slate-300 mb-1">
        Positions month by month{" "}
        <span className="text-slate-500 font-normal">(net P&L by ENTRY month · exit reason · — = month skipped)</span>
      </div>
      <div className="overflow-x-auto max-h-[28rem] overflow-y-auto">
        <table className="w-full text-sm tabular-nums">
          <thead className="text-slate-400 text-left sticky top-0 bg-slate-900">
            <tr>
              <th className="py-1 pr-4">Entry month</th>
              {runs.map((r, i) => (
                <th key={r.run_id} className="py-1 pr-4 text-right" style={{ color: COLORS[i % COLORS.length] }}>
                  {r.name}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {months.map((m) => (
              <tr key={m} className="border-t border-slate-800">
                <td className="py-1 pr-4 text-slate-400 whitespace-nowrap">{m}</td>
                {runs.map((r, i) => {
                  const cycles = byMonth[i].get(m);
                  if (!cycles?.length)
                    return (
                      <td key={r.run_id} className="py-1 pr-4 text-right text-slate-600">—</td>
                    );
                  return (
                    <td key={r.run_id} className="py-1 pr-4 text-right whitespace-nowrap">
                      {cycles.map((c, j) => {
                        const net = c.net_pnl ?? c.realized_pnl;
                        const spot =
                          c.underlying_entry != null && c.underlying_exit != null
                            ? ` · spot ${Math.round(c.underlying_entry)}→${Math.round(c.underlying_exit)}`
                            : "";
                        return (
                          <span key={j} title={`${c.entry_date} → ${c.exit_date ?? "?"} (${c.holding_days}d)${spot}`}>
                            {j > 0 && <span className="text-slate-600"> · </span>}
                            <span className={pnlCls(net)}>{formatInr(net)}</span>{" "}
                            <span className={`text-[10px] uppercase ${REASON_STYLE[c.exit_reason] ?? "text-slate-500"}`}>
                              {c.exit_reason}
                            </span>
                          </span>
                        );
                      })}
                    </td>
                  );
                })}
              </tr>
            ))}
            <tr className="border-t-2 border-slate-700 font-medium">
              <td className="py-1.5 pr-4 text-slate-300">Total (net)</td>
              {runs.map((r) => {
                const total = (r.options?.cycles ?? []).reduce(
                  (s, c) => s + (c.net_pnl ?? c.realized_pnl),
                  0,
                );
                return (
                  <td key={r.run_id} className={`py-1.5 pr-4 text-right ${pnlCls(total)}`}>
                    {formatInr(total)}
                  </td>
                );
              })}
            </tr>
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function ParamCompare({ runs }: { runs: CompareRun[] }) {
  // All input params, capital merged in; rows that differ across runs are flagged.
  const merged = runs.map((r) => ({ capital: r.capital, ...(r.params ?? {}) }) as Record<string, unknown>);
  const keys = orderedParamKeys(Array.from(new Set(merged.flatMap((p) => Object.keys(p)))));
  const [diffOnly, setDiffOnly] = useState(false);

  const differs = (k: string) => new Set(merged.map((p) => JSON.stringify(p[k]))).size > 1;
  const rows = diffOnly ? keys.filter(differs) : keys;

  return (
    <Card>
      <div className="flex items-center justify-between mb-2">
        <div className="text-sm font-medium text-slate-300">Input parameters</div>
        <label className="text-xs text-slate-400 flex items-center gap-1.5">
          <input type="checkbox" checked={diffOnly} onChange={(e) => setDiffOnly(e.target.checked)} />
          differences only
        </label>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm tabular-nums">
          <thead className="text-slate-400 text-left">
            <tr>
              <th className="py-1 pr-4">Parameter</th>
              {runs.map((r, i) => (
                <th key={r.run_id} className="py-1 pr-4 text-right" style={{ color: COLORS[i % COLORS.length] }}>
                  {r.name}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((k) => {
              const diff = differs(k);
              return (
                <tr key={k} className={`border-t border-slate-800 ${diff ? "bg-amber-900/10" : ""}`}>
                  <td className={`py-1.5 pr-4 ${diff ? "text-amber-700 dark:text-amber-300" : "text-slate-400"}`}>
                    {paramLabel(k)}
                  </td>
                  {merged.map((p, i) => (
                    <td key={runs[i].run_id} className="py-1.5 pr-4 text-right">
                      {formatParamValue(k, p[k])}
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function downsample<T>(arr: T[], maxPoints = 400): T[] {
  if (arr.length <= maxPoints) return arr;
  const step = Math.ceil(arr.length / maxPoints);
  return arr.filter((_, i) => i % step === 0 || i === arr.length - 1);
}

function GrowthChart({ runs }: { runs: CompareRun[] }) {
  const [index, setIndex] = useState("none");
  const { data: benchNames } = useQuery({ queryKey: ["benchmarks"], queryFn: api.benchmarks });
  // The index series (normalized to capital) is rebased to 100 for the growth view.
  const { data: bench } = useQuery({
    queryKey: ["benchmark", runs[0]?.run_id, index],
    queryFn: () => api.runBenchmark(runs[0].run_id, index),
    enabled: index !== "none" && runs.length > 0,
  });

  const data = useMemo(() => {
    const dates = new Set<string>();
    runs.forEach((r) => r.growth.forEach((p) => dates.add(p.date)));
    const maps = runs.map((r) => new Map(r.growth.map((p) => [p.date, p.value])));
    const benchBase = bench?.points?.[0]?.value ?? null;
    const benchMap = new Map((bench?.points ?? []).map((p) => [p.date, p.value]));
    const rows = Array.from(dates)
      .sort()
      .map((d) => {
        const row: Record<string, number | string | null> = { date: d };
        runs.forEach((r, i) => (row[`r${r.run_id}`] = maps[i].get(d) ?? null));
        if (index !== "none" && benchBase) {
          const v = benchMap.get(d);
          row.benchmark = v != null ? (100 * v) / benchBase : null;
        }
        return row;
      });
    return downsample(rows);
  }, [runs, bench, index]);

  const options = ["none", ...(benchNames?.benchmarks ?? ["NIFTY 50", "NIFTY 100", "NIFTY 200"])];

  return (
    <Card>
      <div className="flex items-center justify-between mb-3">
        <div className="text-sm font-medium text-slate-300">Equity growth (rebased to 100)</div>
        <label className="text-xs text-slate-400 flex items-center gap-1.5">
          benchmark
          <select
            className="rounded bg-slate-800 border border-slate-700 px-1.5 py-0.5"
            value={index}
            onChange={(e) => setIndex(e.target.value)}
          >
            {options.map((o) => (
              <option key={o} value={o}>{o === "none" ? "None" : o}</option>
            ))}
          </select>
        </label>
      </div>
      <ResponsiveContainer width="100%" height={320}>
        <LineChart data={data} margin={{ top: 5, right: 10, bottom: 0, left: 10 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
          <XAxis dataKey="date" tick={{ fontSize: 11, fill: "#94a3b8" }} minTickGap={40} />
          <YAxis tick={{ fontSize: 11, fill: "#94a3b8" }} width={50} tickFormatter={(v) => `${v.toFixed(0)}`} />
          <Tooltip
            contentStyle={{ background: "rgb(var(--slate-900))", border: "1px solid rgb(var(--slate-700))", color: "rgb(var(--slate-100))" }}
            formatter={(v: number) => v?.toFixed(1)}
          />
          <Legend wrapperStyle={{ fontSize: 11 }} />
          {runs.map((r, i) => (
            <Line
              key={r.run_id}
              type="monotone"
              dataKey={`r${r.run_id}`}
              name={r.name}
              stroke={COLORS[i % COLORS.length]}
              dot={false}
              strokeWidth={1.8}
              connectNulls
            />
          ))}
          {index !== "none" && (
            <Line
              type="monotone"
              dataKey="benchmark"
              name={`${index} (gross B&H)`}
              stroke="#94a3b8"
              strokeDasharray="4 3"
              dot={false}
              strokeWidth={1.5}
              connectNulls
            />
          )}
        </LineChart>
      </ResponsiveContainer>
    </Card>
  );
}

export default function ComparePage() {
  const [params] = useSearchParams();
  const ids = (params.get("ids") ?? "")
    .split(",")
    .map((x) => Number(x))
    .filter((n) => Number.isFinite(n));

  const { data, isLoading, error } = useQuery({
    queryKey: ["compare", ids],
    queryFn: () => api.runsCompare(ids),
    enabled: ids.length >= 2,
  });

  if (ids.length < 2)
    return (
      <Card>
        <div className="text-slate-400">
          Select 2–5 runs to compare from the <Link to="/backtest" className="text-brand-light underline">Runs</Link> page.
        </div>
      </Card>
    );
  if (isLoading) return <Spinner />;
  if (error) return <ErrorBox message={(error as Error).message} />;
  const runs = data?.runs ?? [];
  const anyOptions = runs.some((r) => r.options);

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <Link to="/backtest" className="text-slate-400 hover:text-slate-200 text-sm">← Runs</Link>
        <h1 className="text-lg font-semibold">Compare {runs.length} runs</h1>
      </div>

      <GrowthChart runs={runs} />

      {anyOptions && <OptionsSummaryCompare runs={runs} />}
      {anyOptions && <CycleCompare runs={runs} />}

      <Card>
        <div className="text-sm font-medium text-slate-300 mb-2">Metrics</div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm tabular-nums">
            <thead className="text-slate-400 text-left">
              <tr>
                <th className="py-2 pr-4">Metric</th>
                {runs.map((r, i) => (
                  <th key={r.run_id} className="py-2 pr-4 text-right">
                    <Link to={`/runs/${r.run_id}`} className="hover:text-brand-light" style={{ color: COLORS[i % COLORS.length] }}>
                      {r.name}
                    </Link>
                    <div className="font-normal"><Badge>{r.strategy_id}</Badge></div>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {ROWS.map((row) => {
                const bi = bestIndex(runs, row.key, row.better);
                return (
                  <tr key={String(row.key)} className="border-t border-slate-800">
                    <td className="py-1.5 pr-4 text-slate-400">{row.label}</td>
                    {runs.map((r, i) => (
                      <td
                        key={r.run_id}
                        className={`py-1.5 pr-4 text-right ${i === bi ? "text-emerald-600 dark:text-emerald-400 font-semibold" : ""}`}
                      >
                        {row.fmt(r.metrics[row.key] ?? 0)}
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      <ParamCompare runs={runs} />
    </div>
  );
}
