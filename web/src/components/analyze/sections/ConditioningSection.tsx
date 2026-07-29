/** Drill-in §1 — Conditioning deep-dive: cross-tab heatmap (any two conditions) +
 *  weekday-nested-inside-DTE grouped bars (the Thursday≡0-DTE confound). */

import { useMemo, useState } from "react";
import {
  Bar, BarChart, CartesianGrid, Legend, ReferenceLine, ResponsiveContainer, Tooltip,
  XAxis, YAxis,
} from "recharts";
import type { AnalyticsTrade } from "../../../types";
import { CONDITIONS, legPnl } from "../../../lib/analyzeBuckets";
import Panel from "../Panel";

const HEAT_DIMS = ["dte", "vixpct", "ivhv", "entry", "creditq", "skew"];
const WD = ["Mon", "Tue", "Wed", "Thu", "Fri"];

function meanRom(ts: AnalyticsTrade[]): number | null {
  const v = ts.map((t) => (t.margin_rs ? (100 * legPnl(t, "comb")) / t.margin_rs : null))
    .filter((x): x is number => x != null);
  return v.length ? v.reduce((s, x) => s + x, 0) / v.length : null;
}

export default function ConditioningSection({ trades }: { trades: AnalyticsTrade[] }) {
  const usable = useMemo(
    () => CONDITIONS.filter((c) => HEAT_DIMS.includes(c.id) && !c.disabled(trades)),
    [trades]);
  const [aId, setAId] = useState<string>(usable[0]?.id ?? "dte");
  const [bId, setBId] = useState<string>(usable[1]?.id ?? "vixpct");

  const heat = useMemo(() => {
    const A = usable.find((c) => c.id === aId) ?? usable[0];
    const B = usable.find((c) => c.id === bId && c.id !== A?.id) ?? usable.find((c) => c.id !== A?.id);
    if (!A || !B) return null;
    const rows = A.bucketize(trades);
    const cols = B.bucketize(trades);
    const cells = rows.map((r) => cols.map((c) => {
      const both = r.trades.filter((t) => c.trades.includes(t));
      return { n: both.length, rom: both.length >= 8 ? meanRom(both) : null };
    }));
    const maxAbs = Math.max(0.01, ...cells.flat().map((c) => Math.abs(c.rom ?? 0)));
    return { A, B, rows, cols, cells, maxAbs };
  }, [usable, aId, bId, trades]);

  const wdData = useMemo(() => WD.map((label, wd) => {
    const day = trades.filter((t) => t.weekday === wd);
    const grp = (lo: number, hi: number) => {
      const g = day.filter((t) => t.dte != null && t.dte >= lo && t.dte <= hi);
      const v = g.map((t) => (t.credit_rs > 0 ? (100 * t.pnl_rs) / t.credit_rs : null))
        .filter((x): x is number => x != null);
      return v.length ? +(v.reduce((s, x) => s + x, 0) / v.length).toFixed(2) : null;
    };
    return { label, "0 DTE": grp(0, 0), "1–2 DTE": grp(1, 2), "3+ DTE": grp(3, 99) };
  }), [trades]);

  const sel = "rounded-md bg-[var(--chip)] text-[var(--chip-text)] text-[12px] font-bold px-2 py-1.5 border border-[var(--border)]";
  return (
    <div className="grid gap-4" style={{ gridTemplateColumns: "repeat(auto-fill,minmax(390px,1fr))" }}>
      <Panel wide title="Cross-tab heatmap" num="2.1"
        help="Cell = mean return-on-margin per cycle for trades in BOTH buckets; the label under it is the trade count. Cells under 8 trades are blanked to '·' — a two-way slice thins samples fast, so treat lone bright cells with suspicion."
        subtitle="mean ROM/cycle · green/red by sign · sparse cells blanked">
        <div className="flex items-center gap-2 mb-3">
          <select className={sel} value={aId} onChange={(e) => setAId(e.target.value)}>
            {usable.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
          </select>
          <span className="text-[11px] text-[var(--faint)] font-extrabold">×</span>
          <select className={sel} value={bId} onChange={(e) => setBId(e.target.value)}>
            {usable.filter((c) => c.id !== aId).map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
          </select>
        </div>
        {heat && (
          <div className="overflow-x-auto">
            <table className="border-separate" style={{ borderSpacing: 3 }}>
              <thead>
                <tr>
                  <th />
                  {heat.cols.map((c) => (
                    <th key={c.label} className="text-[10px] font-extrabold text-[var(--faint)] px-1 pb-1 text-center">{c.label}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {heat.rows.map((r, i) => (
                  <tr key={r.label}>
                    <td className="text-[10.5px] font-extrabold text-[var(--faint)] pr-2 whitespace-nowrap">{r.label}</td>
                    {heat.cells[i].map((cell, j) => {
                      const rom = cell.rom;
                      const a = rom == null ? 0 : Math.min(0.85, 0.15 + (0.7 * Math.abs(rom)) / heat.maxAbs);
                      return (
                        <td key={j} className="rounded-md text-center align-middle"
                          style={{
                            minWidth: 74, height: 46,
                            background: rom == null ? "var(--stat)"
                              : rom >= 0 ? `rgba(15,157,99,${a})` : `rgba(217,84,74,${a})`,
                          }}>
                          {rom == null ? (
                            <span className="text-[var(--faint)]">·</span>
                          ) : (
                            <div>
                              <div className="text-[12px] font-extrabold tabular-nums"
                                style={{ color: "var(--strong)" }}>{rom >= 0 ? "+" : ""}{rom.toFixed(2)}%</div>
                              <div className="text-[9.5px] font-bold text-[var(--muted)]">{cell.n}</div>
                            </div>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Panel wide num="Day" title="Weekday nested inside DTE"
        subtitle="mean P&L % of credit · the honest version of a weekday table"
        insight="If the colours disagree more within a weekday than across weekdays, DTE is doing the work.">
        <div className="h-[260px]">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={wdData} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid strokeDasharray="2 4" stroke="var(--divider)" vertical={false} />
              <XAxis dataKey="label" tick={{ fontSize: 11, fill: "var(--faint)" }} tickLine={false}
                axisLine={{ stroke: "var(--border)" }} />
              <YAxis tick={{ fontSize: 10, fill: "var(--faint)" }} tickLine={false} width={44}
                axisLine={{ stroke: "var(--border)" }} tickFormatter={(v: number) => `${v}%`} />
              <ReferenceLine y={0} stroke="var(--faint)" strokeWidth={1.2} opacity={0.85} />
              <Tooltip contentStyle={{ background: "var(--card)", border: "1px solid var(--border)", borderRadius: 10, fontSize: 12 }}
                formatter={(v: number) => [`${v}%`, ""]} />
              <Legend wrapperStyle={{ fontSize: 11, fontWeight: 700 }} />
              <Bar dataKey="0 DTE" fill="var(--danger)" radius={[3, 3, 0, 0]} isAnimationActive={false} />
              <Bar dataKey="1–2 DTE" fill="var(--pe, #d97706)" radius={[3, 3, 0, 0]} isAnimationActive={false} />
              <Bar dataKey="3+ DTE" fill="var(--accent)" radius={[3, 3, 0, 0]} isAnimationActive={false} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </Panel>
    </div>
  );
}
