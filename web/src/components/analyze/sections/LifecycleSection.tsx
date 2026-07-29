/** Drill-in §4 — Trade lifecycle (panels 5.1–5.10): MAE/MFE distributions, giveback,
 *  efficiency, hold-time behaviour and the path spaghetti. All from the bundle's
 *  per-trade MAE/MFE scalars + 5-min paths. */

import { useMemo } from "react";
import {
  Bar, BarChart, CartesianGrid, Legend, Line, ComposedChart, ReferenceLine,
  ResponsiveContainer, Scatter, Tooltip, XAxis, YAxis, LineChart,
} from "recharts";
import type { AnalyticsTrade } from "../../../types";
import Panel from "../Panel";

const TT = { background: "var(--card)", border: "1px solid var(--border)", borderRadius: 10, fontSize: 12 };

function histBins(vals: number[], step: number, lo: number, hi: number) {
  const bins = new Map<number, number>();
  for (let b = lo; b < hi; b += step) bins.set(b, 0);
  for (const v of vals) {
    const b = Math.min(hi - step, Math.max(lo, Math.floor(v / step) * step));
    bins.set(b, (bins.get(b) ?? 0) + 1);
  }
  return [...bins.entries()].map(([b, n]) => ({ bin: `${b}`, n }));
}

export default function LifecycleSection({ trades }: { trades: AnalyticsTrade[] }) {
  const withPath = useMemo(() => trades.filter((t) => t.mae_pct != null), [trades]);
  const winners = withPath.filter((t) => t.pnl_rs > 0);
  const losers = withPath.filter((t) => t.pnl_rs <= 0);

  const maePaired = useMemo(() => {
    const mk = (ts: AnalyticsTrade[]) => {
      const m = new Map<number, number>();
      for (const t of ts) {
        const b = Math.min(90, Math.floor(-t.mae_pct! / 10) * 10);
        m.set(b, (m.get(b) ?? 0) + 1);
      }
      return m;
    };
    const w = mk(winners), l = mk(losers);
    return Array.from({ length: 10 }, (_, i) => i * 10).map((b) => ({
      bin: `−${b}–${b + 10}%`,
      winners: w.get(b) ?? 0, losers: l.get(b) ?? 0,
    }));
  }, [winners, losers]);

  const survival = useMemo(() => {
    if (!winners.length) return [];
    return Array.from({ length: 15 }, (_, i) => (i + 1) * 10).map((x) => ({
      stop: `${x}%`,
      killed: +((100 * winners.filter((t) => t.mae_pct! <= -x).length) / winners.length).toFixed(1),
    }));
  }, [winners]);

  const mfeHist = useMemo(
    () => histBins(withPath.map((t) => t.mfe_pct!), 10, 0, 120), [withPath]);

  const giveback = useMemo(() => withPath
    .filter((t) => t.credit_rs > 0)
    .map((t) => ({ x: t.mfe_pct!, y: +((100 * t.pnl_rs) / t.credit_rs).toFixed(2) })), [withPath]);
  const gLo = Math.min(0, ...giveback.map((g) => g.y));
  const gHi = Math.max(10, ...giveback.map((g) => g.x));

  const eff = useMemo(() => winners
    .filter((t) => t.mfe_pct! > 0 && t.credit_rs > 0)
    .map((t) => (100 * t.pnl_rs) / t.credit_rs / t.mfe_pct!), [winners]);
  const effMed = eff.length ? [...eff].sort((a, b) => a - b)[Math.floor(eff.length / 2)] : null;
  const effMean = eff.length ? eff.reduce((s, v) => s + v, 0) / eff.length : null;
  const effHist = useMemo(() => histBins(eff, 0.1, 0, 1.2), [eff]);

  const holdHist = useMemo(
    () => histBins(trades.map((t) => t.hold_min / 60), 0.5, 0, 7), [trades]);

  const holdPnl = useMemo(() => trades
    .filter((t) => t.credit_rs > 0)
    .map((t) => ({ x: +(t.hold_min / 60).toFixed(2), y: +((100 * t.pnl_rs) / t.credit_rs).toFixed(2), win: t.pnl_rs > 0 })), [trades]);

  const tMfe = useMemo(() => histBins(
    withPath.filter((t) => t.t_mfe_min != null && t.hold_min > 0)
      .map((t) => (100 * t.t_mfe_min!) / t.hold_min), 10, 0, 100), [withPath]);

  const spaghetti = useMemo(() => withPath
    .filter((_, i) => i % 8 === 0)
    .map((t) => ({
      win: t.pnl_rs > 0,
      pts: t.path5.map(([m, p]) => ({ m, p: Math.max(-120, Math.min(120, p)) })),
    })), [withPath]);
  const maxMin = Math.max(60, ...spaghetti.flatMap((s) => s.pts.map((p) => p.m)));

  if (!withPath.length) {
    return <div className="rounded-[14px] bg-[var(--warn-bg)] text-[var(--warn-text)] px-4 py-3 text-sm font-bold">
      Lifecycle analytics need per-trade minute paths — available on the 1-minute basis only.</div>;
  }

  const grid = <CartesianGrid strokeDasharray="2 4" stroke="var(--divider)" vertical={false} />;
  return (
    <div className="grid gap-4" style={{ gridTemplateColumns: "repeat(auto-fill,minmax(390px,1fr))" }}>
      <Panel num="5.1" title="MAE — winners vs losers" subtitle="worst moment, % of credit · paired by bucket">
        <div className="h-[220px]"><ResponsiveContainer>
          <BarChart data={maePaired}>{grid}
            <XAxis dataKey="bin" tick={{ fontSize: 9.5, fill: "var(--faint)" }} tickLine={false} axisLine={{ stroke: "var(--border)" }} />
            <YAxis tick={{ fontSize: 10, fill: "var(--faint)" }} width={34} tickLine={false} axisLine={{ stroke: "var(--border)" }} />
            <Tooltip contentStyle={TT} /><Legend wrapperStyle={{ fontSize: 11, fontWeight: 700 }} />
            <Bar dataKey="winners" fill="var(--pos)" radius={[3, 3, 0, 0]} isAnimationActive={false} />
            <Bar dataKey="losers" fill="var(--danger)" radius={[3, 3, 0, 0]} isAnimationActive={false} />
          </BarChart></ResponsiveContainer></div>
      </Panel>

      <Panel num="5.2" title="MAE of eventual winners" subtitle="share of winners a stop at x would have killed">
        <div className="h-[220px]"><ResponsiveContainer>
          <LineChart data={survival}>{grid}
            <XAxis dataKey="stop" tick={{ fontSize: 10, fill: "var(--faint)" }} tickLine={false} axisLine={{ stroke: "var(--border)" }} />
            <YAxis tick={{ fontSize: 10, fill: "var(--faint)" }} width={40} tickLine={false} axisLine={{ stroke: "var(--border)" }} tickFormatter={(v: number) => `${v}%`} />
            <ReferenceLine x="60%" stroke="var(--note, #c2661d)" strokeDasharray="4 4" />
            <Tooltip contentStyle={TT} formatter={(v: number) => [`${v}%`, "winners killed"]} />
            <Line dataKey="killed" stroke="var(--accent)" dot={false} strokeWidth={2.2} isAnimationActive={false} />
          </LineChart></ResponsiveContainer></div>
      </Panel>

      <Panel num="5.3" title="MFE distribution" subtitle="best moment, % of credit">
        <div className="h-[220px]"><ResponsiveContainer>
          <BarChart data={mfeHist}>{grid}
            <XAxis dataKey="bin" tick={{ fontSize: 10, fill: "var(--faint)" }} tickLine={false} axisLine={{ stroke: "var(--border)" }} />
            <YAxis tick={{ fontSize: 10, fill: "var(--faint)" }} width={34} tickLine={false} axisLine={{ stroke: "var(--border)" }} />
            <Tooltip contentStyle={TT} />
            <Bar dataKey="n" fill="var(--accent)" radius={[3, 3, 0, 0]} isAnimationActive={false} />
          </BarChart></ResponsiveContainer></div>
      </Panel>

      <Panel num="5.4" title="MFE vs realized" subtitle="distance below y=x = profit given back">
        <div className="h-[220px]"><ResponsiveContainer>
          <ComposedChart>{grid}
            <XAxis dataKey="x" type="number" domain={[0, gHi]} tick={{ fontSize: 10, fill: "var(--faint)" }} tickLine={false} axisLine={{ stroke: "var(--border)" }} />
            <YAxis dataKey="y" type="number" domain={[gLo, gHi]} tick={{ fontSize: 10, fill: "var(--faint)" }} width={40} tickLine={false} axisLine={{ stroke: "var(--border)" }} />
            <Tooltip contentStyle={TT} />
            <Line data={[{ x: 0, y: 0 }, { x: gHi, y: gHi }]} dataKey="y" stroke="var(--faint)" strokeDasharray="5 4" dot={false} strokeWidth={1.4} isAnimationActive={false} />
            <ReferenceLine y={0} stroke="var(--faint)" strokeWidth={1.2} opacity={0.85} />
            <Scatter data={giveback} fill="var(--accent)" opacity={0.4} isAnimationActive={false} />
          </ComposedChart></ResponsiveContainer></div>
      </Panel>

      <Panel num="5.5" title="Efficiency ratio" subtitle="realized ÷ best-moment, winners only"
        insight={effMed != null ? `Median ${effMed.toFixed(2)} · mean ${effMean!.toFixed(2)} — you keep ~${Math.round((effMed) * 100)}% of the best moment.` : null}>
        <div className="h-[200px]"><ResponsiveContainer>
          <BarChart data={effHist}>{grid}
            <XAxis dataKey="bin" tick={{ fontSize: 10, fill: "var(--faint)" }} tickLine={false} axisLine={{ stroke: "var(--border)" }} />
            <YAxis tick={{ fontSize: 10, fill: "var(--faint)" }} width={34} tickLine={false} axisLine={{ stroke: "var(--border)" }} />
            <Tooltip contentStyle={TT} />
            <Bar dataKey="n" fill="var(--accent)" radius={[3, 3, 0, 0]} isAnimationActive={false} />
          </BarChart></ResponsiveContainer></div>
      </Panel>

      <Panel num="5.7" title="Hold-time distribution" subtitle="hours in the trade">
        <div className="h-[200px]"><ResponsiveContainer>
          <BarChart data={holdHist}>{grid}
            <XAxis dataKey="bin" tick={{ fontSize: 10, fill: "var(--faint)" }} tickLine={false} axisLine={{ stroke: "var(--border)" }} tickFormatter={(v: string) => `${v}h`} />
            <YAxis tick={{ fontSize: 10, fill: "var(--faint)" }} width={34} tickLine={false} axisLine={{ stroke: "var(--border)" }} />
            <Tooltip contentStyle={TT} />
            <Bar dataKey="n" fill="var(--accent)" radius={[3, 3, 0, 0]} isAnimationActive={false} />
          </BarChart></ResponsiveContainer></div>
      </Panel>

      <Panel num="5.8" title="Hold time vs P&L" subtitle="do good trades work immediately?">
        <div className="h-[220px]"><ResponsiveContainer>
          <ComposedChart>{grid}
            <XAxis dataKey="x" type="number" tick={{ fontSize: 10, fill: "var(--faint)" }} tickLine={false} axisLine={{ stroke: "var(--border)" }} tickFormatter={(v: number) => `${v}h`} />
            <YAxis dataKey="y" type="number" tick={{ fontSize: 10, fill: "var(--faint)" }} width={40} tickLine={false} axisLine={{ stroke: "var(--border)" }} />
            <ReferenceLine y={0} stroke="var(--faint)" strokeWidth={1.2} opacity={0.85} />
            <Tooltip contentStyle={TT} />
            <Scatter data={holdPnl.filter((p) => p.win)} fill="var(--pos)" opacity={0.4} isAnimationActive={false} />
            <Scatter data={holdPnl.filter((p) => !p.win)} fill="var(--danger)" opacity={0.4} isAnimationActive={false} />
          </ComposedChart></ResponsiveContainer></div>
      </Panel>

      <Panel num="5.9" title="Time to best moment" subtitle="when MFE arrives, % of the hold">
        <div className="h-[200px]"><ResponsiveContainer>
          <BarChart data={tMfe}>{grid}
            <XAxis dataKey="bin" tick={{ fontSize: 10, fill: "var(--faint)" }} tickLine={false} axisLine={{ stroke: "var(--border)" }} tickFormatter={(v: string) => `${v}%`} />
            <YAxis tick={{ fontSize: 10, fill: "var(--faint)" }} width={34} tickLine={false} axisLine={{ stroke: "var(--border)" }} />
            <Tooltip contentStyle={TT} />
            <Bar dataKey="n" fill="var(--accent)" radius={[3, 3, 0, 0]} isAnimationActive={false} />
          </BarChart></ResponsiveContainer></div>
      </Panel>

      <Panel wide num="5.10" title="Path spaghetti" subtitle={`every 8th trade's MTM path, % of credit · ${spaghetti.length} paths`}>
        <svg viewBox="0 0 1120 300" className="w-full">
          <line x1={40} x2={1110} y1={150} y2={150} stroke="var(--faint)" strokeWidth={1.2} opacity={0.85} />
          {[-100, -50, 50, 100].map((y) => (
            <g key={y}>
              <line x1={40} x2={1110} y1={150 - y * 1.2} y2={150 - y * 1.2}
                stroke="var(--divider)" strokeWidth={1} strokeDasharray="2 4" />
              <text x={4} y={154 - y * 1.2} fontSize={10} fontWeight={700} fill="var(--faint)">{y}%</text>
            </g>
          ))}
          {spaghetti.map((s, i) => (
            <polyline key={i} fill="none"
              stroke={s.win ? "var(--pos)" : "var(--danger)"} strokeWidth={1.1} opacity={0.22}
              points={s.pts.map((p) => `${40 + (p.m / maxMin) * 1070},${150 - p.p * 1.2}`).join(" ")} />
          ))}
        </svg>
      </Panel>
    </div>
  );
}
