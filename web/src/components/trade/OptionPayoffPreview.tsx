import { useMemo } from "react";
import {
  Area, ComposedChart, Line, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { formatInr } from "../../lib/format";
import { buildLivePayoff, type LiveLeg } from "../../lib/payoff";

/** Sensibull-style payoff for the legs being built: a solid green "On expiry" line (with a
 *  light green/red profit/loss tint), a dashed blue "Current value" line, the current spot
 *  marked, and a zero reference — clean lines, not heavy blocks. */
export default function OptionPayoffPreview({ legs, spot, expiry }: { legs: LiveLeg[]; spot: number; expiry: string }) {
  const pf = useMemo(() => buildLivePayoff(legs, spot, expiry), [legs, spot, expiry]);
  if (!pf) return null;
  const ys = pf.data.flatMap((d) => [d.expiry, d.now]);
  const yMax = Math.max(...ys, 0);
  const yMin = Math.min(...ys, 0);
  const off = yMax <= 0 ? 0 : yMin >= 0 ? 1 : yMax / (yMax - yMin); // green/red split at P&L = 0
  const fmtY = (v: number) => (Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(0)}k` : String(Math.round(v)));

  return (
    <div className="mt-3">
      <div className="flex items-center gap-4 text-[11px] text-slate-400 mb-1">
        <span className="inline-flex items-center gap-1"><span className="inline-block w-4 h-0.5 bg-emerald-500" /> On expiry</span>
        <span className="inline-flex items-center gap-1"><span className="inline-block w-4 border-t-2 border-dashed border-sky-400" /> Current value</span>
        <span className="text-sky-500">│ spot {Math.round(spot)}</span>
      </div>
      <ResponsiveContainer width="100%" height={240}>
        <ComposedChart data={pf.data} margin={{ top: 8, right: 12, bottom: 0, left: 8 }}>
          <defs>
            <linearGradient id="ppfill" x1="0" y1="0" x2="0" y2="1">
              <stop offset={off} stopColor="#10b981" stopOpacity={0.16} />
              <stop offset={off} stopColor="#f43f5e" stopOpacity={0.16} />
            </linearGradient>
          </defs>
          <XAxis dataKey="spot" type="number" domain={["dataMin", "dataMax"]}
            tick={{ fontSize: 11, fill: "#94a3b8" }} tickFormatter={(v) => Math.round(v).toString()} />
          <YAxis tick={{ fontSize: 11, fill: "#94a3b8" }} width={52} tickFormatter={fmtY} />
          <Tooltip
            contentStyle={{ background: "rgb(var(--slate-900))", border: "1px solid rgb(var(--slate-700))", color: "rgb(var(--slate-100))" }}
            formatter={(v: number, n: string) => [formatInr(v), n === "expiry" ? "At expiry" : "Current"]}
            labelFormatter={(v: number) => `Spot ${Math.round(v)}`} />
          <ReferenceLine y={0} stroke="#64748b" />
          <ReferenceLine x={spot} stroke="#38bdf8" strokeDasharray="3 3"
            label={{ value: "spot", fill: "#38bdf8", fontSize: 10, position: "top" }} />
          <Area type="monotone" dataKey="expiry" stroke="#10b981" strokeWidth={2} fill="url(#ppfill)" name="expiry" isAnimationActive={false} />
          <Line type="monotone" dataKey="now" stroke="#38bdf8" strokeWidth={1.75} strokeDasharray="5 3" dot={false} name="now" isAnimationActive={false} />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}
