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
import type { CompareRun, Metrics } from "../types";

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

function ParamDiff({ runs }: { runs: CompareRun[] }) {
  // Show only params whose value differs across the selected runs.
  const keys = Array.from(new Set(runs.flatMap((r) => Object.keys(r.params ?? {}))));
  const differing = keys.filter((k) => {
    const vals = new Set(runs.map((r) => JSON.stringify((r.params ?? {})[k])));
    return vals.size > 1;
  });
  if (differing.length === 0) return null;
  return (
    <Card>
      <div className="text-sm font-medium text-slate-300 mb-2">Differing parameters</div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-slate-400 text-left">
            <tr>
              <th className="py-1 pr-4">Param</th>
              {runs.map((r) => (
                <th key={r.run_id} className="py-1 pr-4 text-right">{r.name}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {differing.map((k) => (
              <tr key={k} className="border-t border-slate-800">
                <td className="py-1 pr-4 text-slate-400">{k}</td>
                {runs.map((r) => (
                  <td key={r.run_id} className="py-1 pr-4 text-right">
                    {String((r.params ?? {})[k] ?? "—")}
                  </td>
                ))}
              </tr>
            ))}
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
            contentStyle={{ background: "#0f172a", border: "1px solid #334155" }}
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
          Select 2–5 runs to compare from the <Link to="/" className="text-brand-light underline">Runs</Link> page.
        </div>
      </Card>
    );
  if (isLoading) return <Spinner />;
  if (error) return <ErrorBox message={(error as Error).message} />;
  const runs = data?.runs ?? [];

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <Link to="/" className="text-slate-400 hover:text-slate-200 text-sm">← Runs</Link>
        <h1 className="text-lg font-semibold">Compare {runs.length} runs</h1>
      </div>

      <GrowthChart runs={runs} />

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
                        className={`py-1.5 pr-4 text-right ${i === bi ? "text-emerald-400 font-semibold" : ""}`}
                      >
                        {row.fmt(r.metrics[row.key])}
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      <ParamDiff runs={runs} />
    </div>
  );
}
