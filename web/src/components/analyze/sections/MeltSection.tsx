/** Drill-in §3 — Premium melt: melt by DTE (4.2), 0-DTE melt by VIX regime (4.3),
 *  theta-by-hour bars (4.4). All from the bundle's melt aggregation. */

import { useMemo } from "react";
import {
  Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip,
  XAxis, YAxis,
} from "recharts";
import type { AnalyticsBundle } from "../../../types";
import Panel from "../Panel";

const DTE_COLORS: Record<string, string> = {
  "0": "var(--danger)", "1": "var(--pe, #d97706)", "2": "var(--indigo, #5b62e8)",
  "3+": "var(--accent)",
};
const VIX_COLORS: Record<string, string> = {
  low: "var(--accent)", med: "var(--pe, #d97706)", high: "var(--danger)",
};

export default function MeltSection({ bundle }: { bundle: AnalyticsBundle }) {
  const melt = bundle.melt;

  const byDte = useMemo(() => melt
    ? melt.slots.map((slot, i) => ({
        slot,
        "0": melt.premium_by_dte["0"]?.[i],
        "1": melt.premium_by_dte["1"]?.[i],
        "2": melt.premium_by_dte["2"]?.[i],
        "3+": melt.premium_by_dte["3+"]?.[i],
      }))
    : [], [melt]);

  const byVix = useMemo(() => melt
    ? melt.slots.map((slot, i) => ({
        slot,
        low: melt.premium_0dte_by_vix.low?.[i],
        med: melt.premium_0dte_by_vix.med?.[i],
        high: melt.premium_0dte_by_vix.high?.[i],
      }))
    : [], [melt]);

  const theta = useMemo(() => {
    if (!melt) return [];
    // all-DTE average curve → per-slot share of the day's total decay
    const avg: (number | null)[] = melt.slots.map((_, i) => {
      let v = 0, n = 0;
      for (const k of ["0", "1", "2", "3+"]) {
        const x = melt.premium_by_dte[k]?.[i];
        if (x != null) { v += x; n += 1; }
      }
      return n ? v / n : null;
    });
    const decays: { slot: string; share: number }[] = [];
    let total = 0;
    for (let i = 1; i < avg.length; i++) {
      const a = avg[i - 1], b = avg[i];
      const d = a != null && b != null ? Math.max(0, a - b) : 0;
      decays.push({ slot: melt.slots[i], share: d });
      total += d;
    }
    return total > 0
      ? decays.map((d) => ({ slot: d.slot, share: +((100 * d.share) / total).toFixed(1) }))
      : [];
  }, [melt]);

  if (!melt) {
    return <div className="rounded-[14px] bg-[var(--warn-bg)] text-[var(--warn-text)] px-4 py-3 text-sm font-bold">
      Melt curves need the 1-minute basis — this run's data basis can't resolve time-of-day decay.</div>;
  }

  const axis = {
    x: <XAxis dataKey="slot" tick={{ fontSize: 10, fill: "var(--faint)" }} minTickGap={48}
      tickLine={false} axisLine={{ stroke: "var(--border)" }} />,
    tt: { background: "var(--card)", border: "1px solid var(--border)", borderRadius: 10, fontSize: 12 },
  };

  return (
    <div className="grid gap-4" style={{ gridTemplateColumns: "repeat(auto-fill,minmax(390px,1fr))" }}>
      <Panel wide num="4.2" title="Melt by DTE" subtitle="normalized traded-straddle premium · 1.00 at entry">
        <div className="h-[260px]">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={byDte} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid strokeDasharray="2 4" stroke="var(--divider)" vertical={false} />
              {axis.x}
              <YAxis domain={[0, 1.1]} tick={{ fontSize: 10, fill: "var(--faint)" }} width={40}
                tickLine={false} axisLine={{ stroke: "var(--border)" }}
                tickFormatter={(v: number) => v.toFixed(2)} />
              <Tooltip contentStyle={axis.tt} />
              <Legend wrapperStyle={{ fontSize: 11, fontWeight: 700 }} />
              {(["0", "1", "2", "3+"] as const).map((k) => (
                <Line key={k} dataKey={k} name={`${k} DTE`} stroke={DTE_COLORS[k]} dot={false}
                  strokeWidth={2} connectNulls isAnimationActive={false} />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>
      </Panel>

      <Panel num="4.3" title="0-DTE melt by VIX regime" subtitle="high VIX melts hardest — rent and risk arrive together">
        <div className="h-[230px]">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={byVix} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid strokeDasharray="2 4" stroke="var(--divider)" vertical={false} />
              {axis.x}
              <YAxis domain={[0, 1.1]} tick={{ fontSize: 10, fill: "var(--faint)" }} width={40}
                tickLine={false} axisLine={{ stroke: "var(--border)" }}
                tickFormatter={(v: number) => v.toFixed(2)} />
              <Tooltip contentStyle={axis.tt} />
              <Legend wrapperStyle={{ fontSize: 11, fontWeight: 700 }} />
              <Line dataKey="low" name="VIX < 13" stroke={VIX_COLORS.low} dot={false}
                strokeWidth={2} connectNulls isAnimationActive={false} />
              <Line dataKey="med" name="VIX 13–19" stroke={VIX_COLORS.med} dot={false}
                strokeWidth={2} connectNulls isAnimationActive={false} />
              <Line dataKey="high" name="VIX ≥ 20" stroke={VIX_COLORS.high} dot={false}
                strokeWidth={2} connectNulls isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </Panel>

      <Panel num="4.4" title="Theta by hour" subtitle="share of the day's decay per 15-min slot">
        <div className="h-[230px]">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={theta} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid strokeDasharray="2 4" stroke="var(--divider)" vertical={false} />
              {axis.x}
              <YAxis tick={{ fontSize: 10, fill: "var(--faint)" }} width={40} tickLine={false}
                axisLine={{ stroke: "var(--border)" }} tickFormatter={(v: number) => `${v}%`} />
              <Tooltip contentStyle={axis.tt} formatter={(v: number) => [`${v}%`, "share of decay"]} />
              <Bar dataKey="share" fill="var(--accent)" radius={[3, 3, 0, 0]} isAnimationActive={false} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </Panel>
    </div>
  );
}
