/** Drill-in §2 — Skew analytics: distribution, outcome, direction, leg split, drift
 *  (panels 3.1–3.5 of design_handoff_analyze). Skew = PE/CE premium ratio of the short
 *  body; > 1 means puts pricier (fear priced in). */

import { useMemo } from "react";
import {
  Bar, BarChart, CartesianGrid, Legend, ReferenceLine, ResponsiveContainer, Scatter,
  ScatterChart, Tooltip, XAxis, YAxis, Line, ComposedChart,
} from "recharts";
import type { AnalyticsTrade } from "../../../types";
import Panel from "../Panel";

const chartFrame = {
  grid: <CartesianGrid strokeDasharray="2 4" stroke="var(--divider)" />,
  tt: { background: "var(--card)", border: "1px solid var(--border)", borderRadius: 10, fontSize: 12 },
};

function trendLine(pts: { x: number; y: number }[]): { x: number; y: number }[] | null {
  if (pts.length < 10) return null;
  const n = pts.length;
  const sx = pts.reduce((s, p) => s + p.x, 0), sy = pts.reduce((s, p) => s + p.y, 0);
  const sxx = pts.reduce((s, p) => s + p.x * p.x, 0), sxy = pts.reduce((s, p) => s + p.x * p.y, 0);
  const d = n * sxx - sx * sx;
  if (Math.abs(d) < 1e-9) return null;
  const b = (n * sxy - sx * sy) / d, a = (sy - b * sx) / n;
  const xs = pts.map((p) => p.x);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  return [{ x: x0, y: a + b * x0 }, { x: x1, y: a + b * x1 }];
}

export default function SkewSection({ trades }: { trades: AnalyticsTrade[] }) {
  const withSkew = useMemo(
    () => trades.filter((t) => t.skew_entry != null && t.credit_rs > 0), [trades]);

  const hist = useMemo(() => {
    const bins: Record<string, number> = {};
    for (const t of withSkew) {
      const b = (Math.round(t.skew_entry! * 20) / 20).toFixed(2);
      bins[b] = (bins[b] ?? 0) + 1;
    }
    return Object.entries(bins).sort(([a], [b]) => +a - +b)
      .map(([skew, n]) => ({ skew, n }));
  }, [withSkew]);

  const outcome = useMemo(() => withSkew.map((t) => ({
    x: t.skew_entry!, y: +((100 * t.pnl_rs) / t.credit_rs).toFixed(2),
    win: t.pnl_rs > 0,
  })), [withSkew]);
  const trend = useMemo(() => trendLine(outcome), [outcome]);

  const move = useMemo(() => withSkew
    .filter((t) => t.u_move_pct != null)
    .map((t) => ({ x: t.skew_entry!, y: t.u_move_pct! })), [withSkew]);

  const legSplit = useMemo(() => {
    const buckets: [string, (t: AnalyticsTrade) => boolean][] = [
      ["< 0.8", (t) => t.skew_entry! < 0.8],
      ["0.8–0.95", (t) => t.skew_entry! >= 0.8 && t.skew_entry! < 0.95],
      ["0.95–1.1", (t) => t.skew_entry! >= 0.95 && t.skew_entry! < 1.1],
      ["> 1.1", (t) => t.skew_entry! >= 1.1],
    ];
    return buckets.map(([label, f]) => {
      const g = withSkew.filter(f);
      const m = (fn: (t: AnalyticsTrade) => number) =>
        g.length ? +(g.reduce((s, t) => s + (100 * fn(t)) / t.credit_rs, 0) / g.length).toFixed(2) : null;
      return { label, "PE leg": m((t) => t.pnl_pe_rs), "CE leg": m((t) => t.pnl_ce_rs), n: g.length };
    }).filter((b) => b.n > 0);
  }, [withSkew]);

  const drift = useMemo(() => withSkew
    .filter((t) => t.skew_exit != null)
    .map((t) => ({ x: t.skew_entry!, y: t.skew_exit! })), [withSkew]);
  const driftLo = Math.min(0.6, ...drift.map((d) => Math.min(d.x, d.y)));
  const driftHi = Math.max(1.4, ...drift.map((d) => Math.max(d.x, d.y)));

  if (!withSkew.length) {
    return <div className="rounded-[14px] bg-[var(--warn-bg)] text-[var(--warn-text)] px-4 py-3 text-sm font-bold">
      No two-sided short structure in this run — skew analytics need a PE and a CE short leg.</div>;
  }

  return (
    <div className="grid gap-4" style={{ gridTemplateColumns: "repeat(auto-fill,minmax(390px,1fr))" }}>
      <Panel num="3.1" title="Entry-skew distribution" subtitle="PE/CE premium ratio at entry">
        <div className="h-[220px]">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={hist} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
              {chartFrame.grid}
              <XAxis dataKey="skew" tick={{ fontSize: 10, fill: "var(--faint)" }} minTickGap={24}
                tickLine={false} axisLine={{ stroke: "var(--border)" }} />
              <YAxis tick={{ fontSize: 10, fill: "var(--faint)" }} width={34} tickLine={false}
                axisLine={{ stroke: "var(--border)" }} />
              <Tooltip contentStyle={chartFrame.tt} />
              <Bar dataKey="n" fill="var(--accent)" radius={[3, 3, 0, 0]} isAnimationActive={false} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </Panel>

      <Panel num="3.2" title="Skew vs outcome" subtitle="entry skew (x) vs P&L % of credit (y)">
        <div className="h-[220px]">
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
              {chartFrame.grid}
              <XAxis dataKey="x" type="number" domain={["auto", "auto"]}
                tick={{ fontSize: 10, fill: "var(--faint)" }} tickLine={false}
                axisLine={{ stroke: "var(--border)" }} />
              <YAxis dataKey="y" type="number" tick={{ fontSize: 10, fill: "var(--faint)" }}
                width={40} tickLine={false} axisLine={{ stroke: "var(--border)" }}
                tickFormatter={(v: number) => `${v}%`} />
              <ReferenceLine y={0} stroke="var(--faint)" strokeWidth={1.2} opacity={0.85} />
              <Tooltip contentStyle={chartFrame.tt} />
              <Scatter data={outcome} fill="var(--accent)" opacity={0.45} isAnimationActive={false} />
              {trend && (
                <Line data={trend} dataKey="y" stroke="var(--note, #c2661d)" strokeDasharray="6 4"
                  dot={false} strokeWidth={2} isAnimationActive={false} />
              )}
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      </Panel>

      <Panel num="3.3" title="Skew vs underlying move" subtitle="does priced-in fear precede drift-up?">
        {move.length ? (
          <div className="h-[220px]">
            <ResponsiveContainer width="100%" height="100%">
              <ScatterChart margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
                {chartFrame.grid}
                <XAxis dataKey="x" type="number" domain={["auto", "auto"]}
                  tick={{ fontSize: 10, fill: "var(--faint)" }} tickLine={false}
                  axisLine={{ stroke: "var(--border)" }} />
                <YAxis dataKey="y" type="number" tick={{ fontSize: 10, fill: "var(--faint)" }}
                  width={40} tickLine={false} axisLine={{ stroke: "var(--border)" }}
                  tickFormatter={(v: number) => `${v}%`} />
                <ReferenceLine y={0} stroke="var(--faint)" strokeWidth={1.2} opacity={0.85} />
                <Tooltip contentStyle={chartFrame.tt} />
                <Scatter data={move} fill="var(--indigo, #5b62e8)" opacity={0.45} isAnimationActive={false} />
              </ScatterChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <div className="h-[220px] flex items-center justify-center text-[12.5px] font-bold text-[var(--warn-text)] bg-[var(--warn-bg)] rounded-[10px]">
            Underlying move per trade isn't recorded on this run — re-save after a newer replay to unlock.
          </div>
        )}
      </Panel>

      <Panel num="3.4" title="Leg P&L by skew regime" subtitle="which side pays in each regime · % of credit">
        <div className="h-[220px]">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={legSplit} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
              {chartFrame.grid}
              <XAxis dataKey="label" tick={{ fontSize: 11, fill: "var(--faint)" }} tickLine={false}
                axisLine={{ stroke: "var(--border)" }} />
              <YAxis tick={{ fontSize: 10, fill: "var(--faint)" }} width={40} tickLine={false}
                axisLine={{ stroke: "var(--border)" }} tickFormatter={(v: number) => `${v}%`} />
              <ReferenceLine y={0} stroke="var(--faint)" strokeWidth={1.2} opacity={0.85} />
              <Tooltip contentStyle={chartFrame.tt} formatter={(v: number) => [`${v}%`, ""]} />
              <Legend wrapperStyle={{ fontSize: 11, fontWeight: 700 }} />
              <Bar dataKey="PE leg" fill="var(--pe, #d97706)" radius={[3, 3, 0, 0]} isAnimationActive={false} />
              <Bar dataKey="CE leg" fill="var(--accent)" radius={[3, 3, 0, 0]} isAnimationActive={false} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </Panel>

      <Panel num="3.5" title="Entry vs exit skew" subtitle="below the dashed y=x line = skew relaxed in-trade">
        <div className="h-[220px]">
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
              {chartFrame.grid}
              <XAxis dataKey="x" type="number" domain={[driftLo, driftHi]}
                tick={{ fontSize: 10, fill: "var(--faint)" }} tickLine={false}
                axisLine={{ stroke: "var(--border)" }} />
              <YAxis dataKey="y" type="number" domain={[driftLo, driftHi]}
                tick={{ fontSize: 10, fill: "var(--faint)" }} width={40} tickLine={false}
                axisLine={{ stroke: "var(--border)" }} />
              <Tooltip contentStyle={chartFrame.tt} />
              <Line data={[{ x: driftLo, y: driftLo }, { x: driftHi, y: driftHi }]} dataKey="y"
                stroke="var(--faint)" strokeDasharray="5 4" dot={false} strokeWidth={1.4}
                isAnimationActive={false} />
              <Scatter data={drift} fill="var(--accent)" opacity={0.4} isAnimationActive={false} />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      </Panel>
    </div>
  );
}
