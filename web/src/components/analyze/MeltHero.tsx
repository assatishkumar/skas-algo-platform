/** Hero 3 — Premium Melt × Position (design_handoff_analyze).
 *
 * Dual-axis: teal = average normalized premium of the TRADED straddle through the day
 * (1.00 at entry, left axis, derived from the same reconstructed paths as the simulator);
 * indigo = average open MTM as % of credit (right axis, dashed zero). DTE segmented
 * filter; entry/exit density strip below on the same clock. EOD basis → amber notice. */

import { useMemo, useState } from "react";
import {
  CartesianGrid, ComposedChart, Line, ReferenceLine, ResponsiveContainer, Tooltip,
  XAxis, YAxis,
} from "recharts";
import type { AnalyticsBundle } from "../../types";
import { MELT_HELP } from "../../lib/analyzeHelp";

type DteSel = "all" | "0" | "1" | "2" | "3+";
const DTE_KEYS: DteSel[] = ["all", "0", "1", "2", "3+"];

export default function MeltHero({ bundle }: { bundle: AnalyticsBundle }) {
  const [sel, setSel] = useState<DteSel>("all");
  const [help, setHelp] = useState(false);
  const melt = bundle.melt;

  const { data, insight } = useMemo(() => {
    if (!melt) return { data: [], insight: null };
    // premium: average the selected DTE facet ("all" = trade-count-weighted mean of facets)
    const prem: (number | null)[] = sel === "all"
      ? melt.slots.map((_, i) => {
          let v = 0, n = 0;
          for (const k of ["0", "1", "2", "3+"]) {
            const x = melt.premium_by_dte[k]?.[i];
            if (x != null) { v += x; n += 1; }
          }
          return n ? v / n : null;
        })
      : melt.premium_by_dte[sel] ?? [];
    const mtm = melt.mtm_by_dte[sel] ?? melt.mtm_by_dte.all ?? [];
    const maxE = Math.max(1, ...melt.entries, ...melt.exits);
    const data = melt.slots.map((slot, i) => ({
      slot,
      prem: prem[i] != null ? +prem[i]!.toFixed(3) : null,
      mtm: mtm[i] != null ? +mtm[i]!.toFixed(2) : null,
      entries: melt.entries[i] / maxE,
      exits: melt.exits[i] / maxE,
    }));
    // insight: where does the steepest decay sit vs the average exit slot?
    let steep = 0, steepI = 0;
    for (let i = 1; i < data.length; i++) {
      const a = data[i - 1].prem, b = data[i].prem;
      if (a != null && b != null && a - b > steep) { steep = a - b; steepI = i; }
    }
    const insight = steep > 0
      ? `Steepest decay lands in the ${melt.slots[steepI]} slot — the rent is earned there; the flat middle of the day is gamma risk for little theta.`
      : null;
    return { data, insight };
  }, [melt, sel]);

  return (
    <div className="rounded-[18px] border border-[var(--border)] bg-[var(--card)] p-6 mb-[18px]">
      <div className="flex items-center gap-2.5 flex-wrap">
        <span className="font-['Space_Grotesk'] font-bold text-[16px] text-[var(--strong)]">Premium melt × position</span>
        <button onClick={() => setHelp((h) => !h)} title="How to read this panel"
          className="w-[21px] h-[21px] rounded-full text-[12px] font-extrabold inline-flex items-center justify-center"
          style={{ background: help ? "var(--accent)" : "var(--chip)", color: help ? "#fff" : "var(--chip-text)" }}>?</button>
        <div className="ml-auto inline-flex rounded-[10px] bg-[var(--seg,var(--chip))] p-[3px]">
          {DTE_KEYS.map((k) => (
            <button key={k} onClick={() => setSel(k)}
              className="px-3 py-1.5 rounded-lg text-[12.5px] font-bold"
              style={sel === k
                ? { background: "var(--card)", color: "var(--strong)", boxShadow: "0 1px 2px rgba(0,0,0,.08)" }
                : { color: "var(--muted)" }}>
              {k === "all" ? "All DTE" : k === "3+" ? "3+ DTE" : `${k} DTE`}
            </button>
          ))}
        </div>
      </div>
      {help && (
        <div className="mt-2.5 rounded-[10px] border border-[var(--tint-border)] bg-[var(--tint)] px-3.5 py-[11px] text-[12.5px] font-semibold leading-[1.6] text-[var(--ft)]">
          {MELT_HELP}
        </div>
      )}
      {!melt ? (
        <div className="mt-4 rounded-[10px] bg-[var(--warn-bg)] px-4 py-4 text-[13px] font-bold text-[var(--warn-text)]">
          Strategy overlay needs the 1-minute basis — this run's data basis can't resolve
          time-of-day premium decay.
        </div>
      ) : (
        <>
          <div className="h-[280px] mt-3">
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
                <CartesianGrid strokeDasharray="2 4" stroke="var(--divider)" vertical={false} />
                <XAxis dataKey="slot" tick={{ fontSize: 10, fill: "var(--faint)" }}
                  minTickGap={48} tickLine={false} axisLine={{ stroke: "var(--border)" }} />
                <YAxis yAxisId="p" domain={[0, 1.1]} width={44}
                  tick={{ fontSize: 10, fill: "var(--faint)" }} tickLine={false}
                  axisLine={{ stroke: "var(--border)" }}
                  tickFormatter={(v: number) => v.toFixed(2)} />
                <YAxis yAxisId="m" orientation="right" width={44}
                  tick={{ fontSize: 10, fill: "var(--indigo, #5b62e8)" }} tickLine={false}
                  axisLine={{ stroke: "var(--border)" }}
                  tickFormatter={(v: number) => `${v}%`} />
                <ReferenceLine yAxisId="m" y={0} stroke="var(--indigo, #5b62e8)"
                  strokeDasharray="4 4" opacity={0.6} />
                <Tooltip contentStyle={{ background: "var(--card)", border: "1px solid var(--border)", borderRadius: 10, fontSize: 12 }}
                  formatter={(v: number, k: string) =>
                    k === "prem" ? [v.toFixed(3), "Premium ×open"] : [`${v}%`, "Avg MTM % credit"]} />
                <Line yAxisId="p" dataKey="prem" stroke="var(--accent)" dot={false}
                  strokeWidth={2.2} connectNulls isAnimationActive={false} />
                <Line yAxisId="m" dataKey="mtm" stroke="var(--indigo, #5b62e8)" dot={false}
                  strokeWidth={1.8} connectNulls isAnimationActive={false} />
              </ComposedChart>
            </ResponsiveContainer>
          </div>
          {/* entry/exit density strip on the same clock */}
          <div className="flex items-end gap-[2px] h-[44px] mt-1 px-[44px]">
            {data.map((d) => (
              <div key={d.slot} className="flex-1 flex flex-col justify-end items-stretch h-full" title={d.slot}>
                <div style={{ height: `${d.entries * 45}%`, background: "var(--accent)", borderRadius: 1 }} />
                <div className="h-px bg-[var(--divider)]" />
                <div style={{ height: `${d.exits * 45}%`, background: "var(--danger)", borderRadius: 1 }} />
              </div>
            ))}
          </div>
          <div className="flex gap-4 text-[10.5px] font-bold text-[var(--faint)] px-[44px] mt-1">
            <span><span className="inline-block w-2.5 h-2 rounded-sm align-middle mr-1" style={{ background: "var(--accent)" }} />entries ↑</span>
            <span><span className="inline-block w-2.5 h-2 rounded-sm align-middle mr-1" style={{ background: "var(--danger)" }} />exits ↓</span>
            <span className="ml-auto">{melt.trades} trades in the overlay</span>
          </div>
          {insight && (
            <div className="mt-2.5 text-[12px] font-bold leading-[1.55]" style={{ color: "var(--note, #c2661d)" }}>{insight}</div>
          )}
        </>
      )}
    </div>
  );
}
