import { useMemo, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { formatInr, pct } from "../lib/format";
import type { Report, Trade } from "../types";
import { Badge, Card, MetricCard } from "./ui";

function downsample<T>(arr: T[], maxPoints = 400): T[] {
  if (arr.length <= maxPoints) return arr;
  const step = Math.ceil(arr.length / maxPoints);
  return arr.filter((_, i) => i % step === 0 || i === arr.length - 1);
}

function EquityChart({ report }: { report: Report }) {
  const data = useMemo(() => downsample(report.equity_curve ?? []), [report.equity_curve]);
  if (data.length === 0) return null;
  return (
    <Card>
      <div className="text-sm font-medium text-slate-300 mb-3">Equity curve</div>
      <ResponsiveContainer width="100%" height={260}>
        <LineChart data={data} margin={{ top: 5, right: 10, bottom: 0, left: 10 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
          <XAxis dataKey="date" tick={{ fontSize: 11, fill: "#94a3b8" }} minTickGap={40} />
          <YAxis
            tick={{ fontSize: 11, fill: "#94a3b8" }}
            width={70}
            tickFormatter={(v) => `${(v / 1e5).toFixed(1)}L`}
          />
          <Tooltip
            contentStyle={{ background: "#0f172a", border: "1px solid #334155" }}
            formatter={(v: number) => formatInr(v)}
          />
          <Line type="monotone" dataKey="equity" stroke="#14b8a6" dot={false} strokeWidth={2} />
        </LineChart>
      </ResponsiveContainer>
    </Card>
  );
}

function YearlyTable({ report }: { report: Report }) {
  const years = Object.keys(report.yearly ?? {}).sort();
  if (years.length === 0) return null;
  return (
    <Card>
      <div className="text-sm font-medium text-slate-300 mb-3">Yearly breakdown</div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-slate-400 text-left">
            <tr>
              <th className="py-1 pr-4">Year</th>
              <th className="py-1 pr-4 text-right">Return</th>
              <th className="py-1 pr-4 text-right">Return %</th>
              <th className="py-1 pr-4 text-right">Portfolio</th>
              <th className="py-1 pr-4 text-right">Taxes</th>
              <th className="py-1 pr-4 text-right">Max DD %</th>
            </tr>
          </thead>
          <tbody>
            {years.map((y) => {
              const r = report.yearly![y];
              return (
                <tr key={y} className="border-t border-slate-800">
                  <td className="py-1 pr-4">{y}</td>
                  <td className={`py-1 pr-4 text-right ${r["Return (Abs)"] >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                    {formatInr(r["Return (Abs)"])}
                  </td>
                  <td className="py-1 pr-4 text-right">{pct(r["Return (%)"])}</td>
                  <td className="py-1 pr-4 text-right">{formatInr(r["Portfolio Value"])}</td>
                  <td className="py-1 pr-4 text-right">{formatInr(r.Taxes)}</td>
                  <td className="py-1 pr-4 text-right">{pct(r["Max Drawdown (%)"])}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function TradesTable({ trades }: { trades: Trade[] }) {
  const [tag, setTag] = useState<string>("ALL");
  const tags = useMemo(
    () => ["ALL", ...Array.from(new Set(trades.map((t) => t.tag)))],
    [trades],
  );
  const filtered = useMemo(
    () => (tag === "ALL" ? trades : trades.filter((t) => t.tag === tag)).slice(0, 500),
    [trades, tag],
  );
  if (trades.length === 0) return null;
  return (
    <Card>
      <div className="flex items-center justify-between mb-3">
        <div className="text-sm font-medium text-slate-300">
          Trades <span className="text-slate-500">({trades.length})</span>
        </div>
        <div className="flex gap-1">
          {tags.map((t) => (
            <button
              key={t}
              onClick={() => setTag(t)}
              className={`px-2 py-0.5 rounded text-xs ${
                tag === t ? "bg-brand text-white" : "bg-slate-800 text-slate-300"
              }`}
            >
              {t}
            </button>
          ))}
        </div>
      </div>
      <div className="overflow-x-auto max-h-96 overflow-y-auto">
        <table className="w-full text-sm">
          <thead className="text-slate-400 text-left sticky top-0 bg-slate-900">
            <tr>
              <th className="py-1 pr-4">Date</th>
              <th className="py-1 pr-4">Symbol</th>
              <th className="py-1 pr-4">Action</th>
              <th className="py-1 pr-4 text-right">Units</th>
              <th className="py-1 pr-4 text-right">Price</th>
              <th className="py-1 pr-4 text-right">P&L</th>
              <th className="py-1 pr-4">Tag</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((t, i) => (
              <tr key={i} className="border-t border-slate-800">
                <td className="py-1 pr-4 whitespace-nowrap">{t.date}</td>
                <td className="py-1 pr-4">{t.ticker}</td>
                <td className="py-1 pr-4">{t.action}</td>
                <td className="py-1 pr-4 text-right">{t.units}</td>
                <td className="py-1 pr-4 text-right">{formatInr(t.price, 2)}</td>
                <td className={`py-1 pr-4 text-right ${t.profit > 0 ? "text-emerald-400" : t.profit < 0 ? "text-rose-400" : "text-slate-400"}`}>
                  {t.action === "SELL" ? formatInr(t.profit) : "—"}
                </td>
                <td className="py-1 pr-4">
                  <Badge>{t.tag}</Badge>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

export default function ReportView({
  report,
  trades,
  csvUrl,
}: {
  report: Report;
  trades: Trade[];
  csvUrl?: string;
}) {
  const m = report.metrics;
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <MetricCard label="Total Return" value={pct(m["Total Return %"])} tone={m["Total Return %"] >= 0 ? "good" : "bad"} />
        <MetricCard label="CAGR" value={pct(m["CAGR %"])} />
        <MetricCard label="Final Equity" value={formatInr(m["Final Equity"])} />
        <MetricCard label="Max Drawdown" value={pct(m["Max Drawdown %"])} tone="bad" />
        <MetricCard label="Total Trades" value={m["Total Trades"]} />
        <MetricCard label="Win Rate" value={pct(m["Win Rate %"])} />
        <MetricCard label="Total Taxes" value={formatInr(m["Total Taxes"])} />
        <MetricCard label="Cash Balance" value={formatInr(m["Cash Balance"])} />
      </div>
      <EquityChart report={report} />
      <YearlyTable report={report} />
      <TradesTable trades={trades} />
      {csvUrl && (
        <a
          href={csvUrl}
          className="inline-block rounded-md bg-slate-800 hover:bg-slate-700 px-3 py-2 text-sm"
        >
          ↓ Download trades CSV
        </a>
      )}
    </div>
  );
}
