import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
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
import { formatInr, pct } from "../lib/format";
import type { Report, Trade } from "../types";
import { Badge, Card, MetricCard } from "./ui";
import OptionsReport from "./OptionsReport";

function downsample<T>(arr: T[], maxPoints = 400): T[] {
  if (arr.length <= maxPoints) return arr;
  const step = Math.ceil(arr.length / maxPoints);
  return arr.filter((_, i) => i % step === 0 || i === arr.length - 1);
}

function EquityChart({ report, runId }: { report: Report; runId?: number }) {
  // Default the benchmark to NIFTY 50; "none" hides it.
  const [index, setIndex] = useState("NIFTY 50");
  const hasGross = (report.equity_curve ?? []).some((p) => p.gross_equity != null);

  const { data: benchNames } = useQuery({ queryKey: ["benchmarks"], queryFn: api.benchmarks });
  const { data: bench } = useQuery({
    queryKey: ["benchmark", runId, index],
    queryFn: () => api.runBenchmark(runId!, index),
    enabled: runId != null && index !== "none",
  });

  const data = useMemo(() => {
    const curve = downsample(report.equity_curve ?? []);
    const byDate = new Map((bench?.points ?? []).map((p) => [p.date, p.value]));
    return curve.map((p) => ({ ...p, benchmark: byDate.get(p.date) ?? null }));
  }, [report.equity_curve, bench]);

  if (data.length === 0) return null;
  const options = ["none", ...(benchNames?.benchmarks ?? ["NIFTY 50", "NIFTY 100", "NIFTY 200"])];

  return (
    <Card>
      <div className="flex items-center justify-between mb-3">
        <div className="text-sm font-medium text-slate-300">Equity curve</div>
        {runId != null && (
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
        )}
      </div>
      <ResponsiveContainer width="100%" height={280}>
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
          <Legend wrapperStyle={{ fontSize: 11 }} />
          <Line type="monotone" dataKey="equity" name="Strategy (net)" stroke="#14b8a6" dot={false} strokeWidth={2} />
          {hasGross && (
            <Line
              type="monotone"
              dataKey="gross_equity"
              name="Strategy (gross)"
              stroke="#14b8a6"
              strokeDasharray="4 3"
              dot={false}
              strokeWidth={1.5}
            />
          )}
          {runId != null && index !== "none" && (
            <Line
              type="monotone"
              dataKey="benchmark"
              name={`${index} (gross B&H)`}
              stroke="#f59e0b"
              dot={false}
              strokeWidth={1.5}
              connectNulls
            />
          )}
        </LineChart>
      </ResponsiveContainer>
      <div className="mt-2 text-[11px] text-slate-500">
        <span className="text-slate-400">Strategy (net)</span> is after taxes &amp; withdrawals —
        what you actually keep.{" "}
        {index !== "none" && <>{index} is a gross buy-and-hold (no tax along the way). </>}
        {hasGross ? (
          <>
            <span className="text-slate-400">Strategy (gross)</span> adds taxes &amp; withdrawals
            back, for a before-tax, like-for-like comparison with the index.
          </>
        ) : (
          <>Re-run this backtest to also plot a before-tax Strategy (gross) line.</>
        )}
      </div>
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
                  {["SELL", "COVER", "SETTLE"].includes(t.action) ? formatInr(t.profit) : "—"}
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

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

type MonthlyData = Record<string, Record<string, number>>;

function hasAnyValue(data?: MonthlyData): boolean {
  if (!data) return false;
  return Object.values(data).some((row) => Object.values(row).some((v) => v !== 0));
}

function MonthlyGrid({
  title,
  data,
  total,
  totalLabel = "Total",
}: {
  title: string;
  data?: MonthlyData;
  total: "sum" | "max" | "eoy";
  totalLabel?: string;
}) {
  const years = Object.keys(data ?? {}).sort();
  if (!data || years.length === 0) return null;

  const totalOf = (row: Record<string, number>) => {
    const vals = MONTHS.map((_, i) => row[String(i + 1)] ?? 0);
    if (total === "max") return Math.max(...vals);
    if (total === "eoy") return row["12"] ?? 0;
    return vals.reduce((a, b) => a + b, 0);
  };

  return (
    <Card>
      <div className="text-sm font-medium text-slate-300 mb-3">{title}</div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="text-slate-400 text-left">
            <tr>
              <th className="py-1 pr-3">Year</th>
              {MONTHS.map((mo) => (
                <th key={mo} className="py-1 px-2 text-right">{mo}</th>
              ))}
              <th className="py-1 pl-3 text-right font-semibold">{totalLabel}</th>
            </tr>
          </thead>
          <tbody>
            {years.map((y) => {
              const row = data[y];
              return (
                <tr key={y} className="border-t border-slate-800">
                  <td className="py-1 pr-3">{y}</td>
                  {MONTHS.map((mo, i) => {
                    const v = row[String(i + 1)] ?? 0;
                    return (
                      <td key={mo} className="py-1 px-2 text-right tabular-nums text-slate-300">
                        {v ? formatInr(v) : "·"}
                      </td>
                    );
                  })}
                  <td className="py-1 pl-3 text-right font-semibold tabular-nums">
                    {formatInr(totalOf(row))}
                  </td>
                </tr>
              );
            })}
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
  runId,
}: {
  report: Report;
  trades: Trade[];
  csvUrl?: string;
  runId?: number;
}) {
  const m = report.metrics;
  const netMonthly = m["Avg Monthly Net P&L (Post-Tax)"] ?? 0;
  return (
    <div className="space-y-4">
      {report.options ? (
        // Options runs: a curated headline row (the equity-style grid below is replaced by
        // the options-specific tiles in <OptionsReport/>).
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <MetricCard label="Total Return" value={pct(m["Total Return %"])} tone={m["Total Return %"] >= 0 ? "good" : "bad"} />
          <MetricCard label="CAGR" value={pct(m["CAGR %"])} />
          <MetricCard label="Final Equity" value={formatInr(m["Final Equity"])} />
          <MetricCard label="Max Drawdown" value={pct(m["Max Drawdown %"])} tone="bad" />
          <MetricCard label="Avg Monthly Net P&L" value={formatInr(netMonthly)} tone={netMonthly >= 0 ? "good" : "bad"} />
          <MetricCard label="F&O Charges" value={formatInr(report.options.summary.total_charges)} tone="bad" />
          <MetricCard label="Avg Holding (days)" value={report.options.summary.avg_holding_days.toFixed(1)} />
        </div>
      ) : (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <MetricCard label="Total Return" value={pct(m["Total Return %"])} tone={m["Total Return %"] >= 0 ? "good" : "bad"} />
          <MetricCard label="CAGR" value={pct(m["CAGR %"])} />
          <MetricCard label="Final Equity" value={formatInr(m["Final Equity"])} />
          <MetricCard label="Max Drawdown" value={pct(m["Max Drawdown %"])} tone="bad" />
          <MetricCard label="Total Trades" value={m["Total Trades"]} />
          <MetricCard label="Win Rate" value={pct(m["Win Rate %"])} />
          <MetricCard label="Total Taxes" value={formatInr(m["Total Taxes"])} />
          <MetricCard label="Total Withdrawals" value={formatInr(m["Total Withdrawals"])} />
          <MetricCard label="Cash Balance" value={formatInr(m["Cash Balance"])} />
          <MetricCard label="Avg Monthly Bookings" value={m["Avg Monthly Profit Booking"]?.toFixed(2)} />
          <MetricCard label="Avg Monthly Net P&L" value={formatInr(netMonthly)} tone={netMonthly >= 0 ? "good" : "bad"} />
          <MetricCard label="Avg Winners' Profit (Pre-Tax)" value={formatInr(m["Avg Monthly Profit (Pre-Tax)"])} />
          <MetricCard label="Avg Winners' Profit (Post-Tax)" value={formatInr(m["Avg Monthly Profit (Post-Tax)"])} />
        </div>
      )}
      {report.options && <OptionsReport options={report.options} />}
      <EquityChart report={report} runId={runId} />
      <YearlyTable report={report} />
      <MonthlyGrid
        title={report.options ? "Monthly profit (booked on exit date)" : "Monthly profit (booked)"}
        data={report.monthly_profit}
        total="sum"
      />
      {report.options && (
        <div className="text-[11px] text-slate-500 -mt-2">
          Booked in the month a position <span className="text-slate-400">exits</span> (the Positions table is
          sorted by <span className="text-slate-400">entry</span> date) — so a cycle entered late one month
          books the next, and multiple cycles closing in the same month net together here.
        </div>
      )}
      {hasAnyValue(report.monthly_withdrawals) && (
        <MonthlyGrid title="Monthly withdrawals" data={report.monthly_withdrawals} total="sum" />
      )}
      {!report.options && (
        <MonthlyGrid
          title="Monthly capital utilization (max invested)"
          data={report.monthly_capital}
          total="max"
          totalLabel="Peak"
        />
      )}
      <MonthlyGrid
        title="Monthly equity (end of month)"
        data={report.monthly_equity}
        total="eoy"
        totalLabel="EoY"
      />
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
