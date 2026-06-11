import { useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { formatInr, pct } from "../lib/format";
import type { OptionCycle, OptionPosition, OptionsReportData } from "../types";
import { Card, MetricCard } from "./ui";

const REASON_COLOR: Record<string, string> = {
  target: "#10b981",
  stop: "#f43f5e",
  expiry: "#f59e0b",
  manual: "#64748b",
  mixed: "#8b5cf6",
};

function ReasonChip({ reason }: { reason: string }) {
  const color = REASON_COLOR[reason] ?? "#64748b";
  return (
    <span
      className="inline-block rounded-full px-2 py-0.5 text-xs font-medium"
      style={{ background: `${color}22`, color }}
    >
      {reason}
    </span>
  );
}

function pnlClass(v: number): string {
  return v > 0 ? "text-emerald-400" : v < 0 ? "text-rose-400" : "text-slate-400";
}

function SummaryTiles({ s }: { s: OptionsReportData["summary"] }) {
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
      <MetricCard label="Premium Collected" value={formatInr(s.total_premium_collected)} />
      <MetricCard
        label="Premium Captured"
        value={formatInr(s.total_premium_captured)}
        tone={s.total_premium_captured >= 0 ? "good" : "bad"}
      />
      <MetricCard label="Capture %" value={pct(s.premium_capture_pct)} />
      <MetricCard label="Win Rate" value={pct(s.win_rate_pct)} />
      <MetricCard label="Cycles" value={s.num_cycles} />
      <MetricCard label="Legs Traded" value={s.num_positions} />
      <MetricCard label="Avg Holding (days)" value={s.avg_holding_days.toFixed(1)} />
      <MetricCard label="Avg Premium / Cycle" value={formatInr(s.avg_premium_per_cycle)} />
      <MetricCard label="Max Margin Used" value={formatInr(s.max_margin_used)} />
      <MetricCard label="Avg Margin Used" value={formatInr(s.avg_margin_used)} />
      <MetricCard label="Capital Efficiency*" value={`${s.capital_efficiency.toFixed(2)}×`} />
    </div>
  );
}

function PremiumDecayChart({ options }: { options: OptionsReportData }) {
  const data = useMemo(() => {
    const marginBy = new Map(options.margin_series.map((m) => [m.date, m.margin]));
    return options.premium_curve.map((p) => ({
      date: p.date,
      premium: p.premium,
      margin: marginBy.get(p.date) ?? null,
    }));
  }, [options]);
  if (data.length === 0) return null;
  return (
    <Card>
      <div className="text-sm font-medium text-slate-300 mb-3">
        Open premium (mark-to-market) &amp; margin used
      </div>
      <ResponsiveContainer width="100%" height={260}>
        <LineChart data={data} margin={{ top: 5, right: 10, bottom: 0, left: 10 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
          <XAxis dataKey="date" tick={{ fontSize: 11, fill: "#94a3b8" }} minTickGap={40} />
          <YAxis
            yAxisId="left"
            tick={{ fontSize: 11, fill: "#94a3b8" }}
            width={64}
            tickFormatter={(v) => `${(v / 1e3).toFixed(0)}k`}
          />
          <YAxis
            yAxisId="right"
            orientation="right"
            tick={{ fontSize: 11, fill: "#94a3b8" }}
            width={64}
            tickFormatter={(v) => `${(v / 1e5).toFixed(1)}L`}
          />
          <Tooltip
            contentStyle={{ background: "#0f172a", border: "1px solid #334155" }}
            formatter={(v: number) => formatInr(v)}
          />
          <Legend wrapperStyle={{ fontSize: 11 }} />
          <Line
            yAxisId="left"
            type="monotone"
            dataKey="premium"
            name="Open premium (buy-back cost)"
            stroke="#14b8a6"
            dot={false}
            strokeWidth={2}
          />
          <Line
            yAxisId="right"
            type="monotone"
            dataKey="margin"
            name="Margin used"
            stroke="#f59e0b"
            dot={false}
            strokeWidth={1.5}
            connectNulls
          />
        </LineChart>
      </ResponsiveContainer>
      <div className="mt-2 text-[11px] text-slate-500">
        Open premium is the live cost to buy back the written options — it decays toward zero as
        time passes (the seller's edge). Spikes mean the position moved against you.
      </div>
    </Card>
  );
}

function ExitReasonDonut({ options }: { options: OptionsReportData }) {
  const data = useMemo(
    () =>
      Object.entries(options.exit_reasons).map(([reason, s]) => ({
        reason,
        count: s.count,
        pnl: s.pnl,
      })),
    [options],
  );
  if (data.length === 0) return null;
  return (
    <Card>
      <div className="text-sm font-medium text-slate-300 mb-3">Exits by reason</div>
      <ResponsiveContainer width="100%" height={220}>
        <PieChart>
          <Pie
            data={data}
            dataKey="count"
            nameKey="reason"
            cx="50%"
            cy="50%"
            innerRadius={50}
            outerRadius={80}
            paddingAngle={2}
          >
            {data.map((d) => (
              <Cell key={d.reason} fill={REASON_COLOR[d.reason] ?? "#64748b"} />
            ))}
          </Pie>
          <Tooltip
            contentStyle={{ background: "#0f172a", border: "1px solid #334155" }}
            formatter={(v: number, _n, p) => [`${v} legs · ${formatInr(p.payload.pnl)}`, p.payload.reason]}
          />
          <Legend wrapperStyle={{ fontSize: 11 }} />
        </PieChart>
      </ResponsiveContainer>
    </Card>
  );
}

function PerExpiryBars({ options }: { options: OptionsReportData }) {
  const data = options.per_expiry_cycle;
  if (data.length === 0) return null;
  return (
    <Card>
      <div className="text-sm font-medium text-slate-300 mb-3">P&amp;L per expiry cycle</div>
      <ResponsiveContainer width="100%" height={220}>
        <BarChart data={data} margin={{ top: 5, right: 10, bottom: 0, left: 10 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
          <XAxis dataKey="expiry" tick={{ fontSize: 10, fill: "#94a3b8" }} minTickGap={10} />
          <YAxis
            tick={{ fontSize: 11, fill: "#94a3b8" }}
            width={64}
            tickFormatter={(v) => `${(v / 1e3).toFixed(0)}k`}
          />
          <Tooltip
            contentStyle={{ background: "#0f172a", border: "1px solid #334155" }}
            formatter={(v: number) => formatInr(v)}
          />
          <Bar dataKey="realized_pnl" name="Realized P&L">
            {data.map((d, i) => (
              <Cell key={i} fill={d.realized_pnl >= 0 ? "#10b981" : "#f43f5e"} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </Card>
  );
}

function CycleRow({ c }: { c: OptionCycle }) {
  const [open, setOpen] = useState(false);
  const legs = c.legs_detail ?? [c.ce, c.pe].filter(Boolean) as OptionPosition[];
  const strikes = [c.ce?.strike, c.pe?.strike].filter((s) => s != null);
  const strikeLabel =
    legs.length === 2 && strikes.length === 2 && strikes[0] === strikes[1]
      ? `${strikes[0]} CE+PE`
      : `${legs.length} legs`;
  return (
    <>
      <tr className="border-t border-slate-800 cursor-pointer hover:bg-slate-800/40" onClick={() => setOpen((v) => !v)}>
        <td className="py-1 pr-3 whitespace-nowrap text-slate-500">{open ? "▾" : "▸"}</td>
        <td className="py-1 pr-4 whitespace-nowrap">{c.entry_date}</td>
        <td className="py-1 pr-4 whitespace-nowrap">{c.expiry}</td>
        <td className="py-1 pr-4">{c.underlying}</td>
        <td className="py-1 pr-4 whitespace-nowrap">{strikeLabel}</td>
        <td className="py-1 pr-4 text-right">{formatInr(c.premium_collected)}</td>
        <td className="py-1 pr-4 text-right">{c.holding_days}</td>
        <td className="py-1 pr-4"><ReasonChip reason={c.exit_reason} /></td>
        <td className={`py-1 pr-4 text-right ${pnlClass(c.realized_pnl)}`}>{formatInr(c.realized_pnl)}</td>
      </tr>
      {open &&
        legs.map((leg) => (
          <tr key={leg.symbol} className="bg-slate-900/60 text-xs text-slate-400">
            <td />
            <td className="py-1 pr-4" colSpan={3}>
              ↳ {leg.side === "long" ? "BUY" : leg.side === "short" ? "SELL" : ""} {leg.right} {leg.strike}
              {leg.lots > 1 ? ` ×${leg.lots}` : ""}
            </td>
            <td className="py-1 pr-4">entry {formatInr(leg.entry_premium, 2)} → exit {formatInr(leg.exit_price, 2)}</td>
            <td className="py-1 pr-4 text-right">{formatInr(leg.premium_collected)}</td>
            <td className="py-1 pr-4 text-right">{leg.holding_days}</td>
            <td className="py-1 pr-4"><ReasonChip reason={leg.exit_reason} /></td>
            <td className={`py-1 pr-4 text-right ${pnlClass(leg.realized_pnl)}`}>
              {formatInr(leg.realized_pnl)} ({pct(leg.pnl_pct)})
            </td>
          </tr>
        ))}
    </>
  );
}

function PositionsTable({ options }: { options: OptionsReportData }) {
  const [reason, setReason] = useState<string>("ALL");
  const reasons = useMemo(
    () => ["ALL", ...Object.keys(options.exit_reasons)],
    [options],
  );
  const rows = useMemo(
    () => (reason === "ALL" ? options.cycles : options.cycles.filter((c) => c.exit_reason === reason)),
    [options, reason],
  );
  if (options.cycles.length === 0) return null;
  return (
    <Card>
      <div className="flex items-center justify-between mb-3">
        <div className="text-sm font-medium text-slate-300">
          Positions <span className="text-slate-500">({options.cycles.length} cycles · click a row for legs)</span>
        </div>
        <div className="flex gap-1">
          {reasons.map((r) => (
            <button
              key={r}
              onClick={() => setReason(r)}
              className={`px-2 py-0.5 rounded text-xs ${reason === r ? "bg-brand text-white" : "bg-slate-800 text-slate-300"}`}
            >
              {r}
            </button>
          ))}
        </div>
      </div>
      <div className="overflow-x-auto max-h-96 overflow-y-auto">
        <table className="w-full text-sm">
          <thead className="text-slate-400 text-left sticky top-0 bg-slate-900">
            <tr>
              <th className="py-1 pr-3" />
              <th className="py-1 pr-4">Entry</th>
              <th className="py-1 pr-4">Expiry</th>
              <th className="py-1 pr-4">Underlying</th>
              <th className="py-1 pr-4">Strikes</th>
              <th className="py-1 pr-4 text-right">Premium</th>
              <th className="py-1 pr-4 text-right">Days</th>
              <th className="py-1 pr-4">Exit</th>
              <th className="py-1 pr-4 text-right">Realized P&L</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((c, i) => (
              <CycleRow key={`${c.entry_date}-${c.expiry}-${i}`} c={c} />
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

export default function OptionsReport({ options }: { options: OptionsReportData }) {
  return (
    <div className="space-y-4">
      <div className="text-sm font-semibold text-slate-200">Options analytics</div>
      <SummaryTiles s={options.summary} />
      <PositionsTable options={options} />
      <PremiumDecayChart options={options} />
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <ExitReasonDonut options={options} />
        <PerExpiryBars options={options} />
      </div>
      <div className="text-[11px] text-slate-500">
        * Capital efficiency = premium collected ÷ peak margin used. Margin is a flat SPAN+exposure
        approximation — treat it as indicative.
      </div>
    </div>
  );
}
