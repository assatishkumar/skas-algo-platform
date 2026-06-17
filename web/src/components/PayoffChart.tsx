import { useMemo } from "react";
import {
  Area,
  ComposedChart,
  Line,
  ReferenceDot,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { formatInr } from "../lib/format";
import { buildPayoff } from "../lib/payoff";
import type { OptionCycle } from "../types";

function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="inline-block w-3 h-[3px] rounded" style={{ background: color }} />
      {label}
    </span>
  );
}

/** Sensibull-style payoff diagram for one saved cycle: expiry tent (entry premiums),
 * model curve on the exit date (entry IVs), entry/exit spot markers, and a dot at the
 * actual realized P&L. All values are gross of F&O charges. */
export default function PayoffChart({ cycle }: { cycle: OptionCycle }) {
  const pf = useMemo(() => buildPayoff(cycle), [cycle]);
  if (!pf) return null;
  const { data, entrySpot, exitSpot, realized } = pf;

  const ys = data.flatMap((d) => [d.expiry, d.exit]);
  const yMax = Math.max(...ys, realized);
  const yMin = Math.min(...ys, realized);
  // Split the tent fill green/red at the zero line.
  const off = yMax <= 0 ? 0 : yMin >= 0 ? 1 : yMax / (yMax - yMin);
  const gid = `payoff-${cycle.entry_date}-${cycle.expiry}`.replace(/[^a-zA-Z0-9-]/g, "");

  return (
    <div>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-slate-400 mb-1">
        <LegendDot color="#94a3b8" label="At expiry (entry premiums)" />
        <LegendDot color="#3b82f6" label={`On exit date (model, ${cycle.exit_date ?? ""})`} />
        {entrySpot != null && <LegendDot color="#f59e0b" label="entry spot" />}
        {exitSpot != null && <LegendDot color="#38bdf8" label="exit spot" />}
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block w-2 h-2 rounded-full bg-white border border-slate-900" /> actual exit P&L
        </span>
      </div>
      <ResponsiveContainer width="100%" height={240}>
        <ComposedChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
          <defs>
            <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
              <stop offset={off} stopColor="#10b981" stopOpacity={0.22} />
              <stop offset={off} stopColor="#f43f5e" stopOpacity={0.22} />
            </linearGradient>
          </defs>
          <XAxis
            dataKey="spot"
            type="number"
            domain={["dataMin", "dataMax"]}
            tick={{ fill: "#64748b", fontSize: 11 }}
            tickFormatter={(v: number) => Math.round(v).toLocaleString("en-IN")}
            tickCount={7}
          />
          <YAxis
            tick={{ fill: "#64748b", fontSize: 11 }}
            tickFormatter={(v: number) => formatInr(v)}
            width={78}
          />
          <Tooltip
            contentStyle={{ background: "rgb(var(--slate-900))", border: "1px solid rgb(var(--slate-700))", color: "rgb(var(--slate-100))", fontSize: 12 }}
            labelFormatter={(v) => `Spot ${Math.round(Number(v)).toLocaleString("en-IN")}`}
            formatter={(v: number, name: string) => [
              formatInr(v),
              name === "expiry" ? "At expiry" : "On exit date",
            ]}
          />
          <ReferenceLine y={0} stroke="#475569" />
          {entrySpot != null && (
            <ReferenceLine x={entrySpot} stroke="#f59e0b" strokeDasharray="4 3" />
          )}
          {exitSpot != null && (
            <ReferenceLine x={exitSpot} stroke="#38bdf8" strokeDasharray="4 3" />
          )}
          <Area
            dataKey="expiry"
            stroke="#94a3b8"
            strokeWidth={1.5}
            fill={`url(#${gid})`}
            baseValue={0}
            isAnimationActive={false}
            dot={false}
          />
          <Line
            dataKey="exit"
            stroke="#3b82f6"
            strokeWidth={1.5}
            isAnimationActive={false}
            dot={false}
          />
          {exitSpot != null && (
            <ReferenceDot
              x={exitSpot}
              y={realized}
              r={4}
              fill="#ffffff"
              stroke="#0f172a"
              ifOverflow="extendDomain"
            />
          )}
        </ComposedChart>
      </ResponsiveContainer>
      <div className="text-[10px] text-slate-500 mt-0.5">
        Model curve prices each leg with the IV implied by its entry premium — the actual exit
        dot can sit off it when vol moved during the hold. Gross of charges.
      </div>
    </div>
  );
}
