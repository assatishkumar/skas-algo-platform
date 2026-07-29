/** Drill-in §5 — Execution quality: breakeven slippage (6.2), cost decomposition (6.4),
 *  liquidity feasibility (6.5); 6.1/6.3/6.6 render as dimmed cards (need live fills). */

import { useMemo } from "react";
import {
  CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip,
  XAxis, YAxis,
} from "recharts";
import type { AnalyticsBundle } from "../../../types";
import { formatInr } from "../../../lib/format";
import Panel from "../Panel";

const TT = { background: "var(--card)", border: "1px solid var(--border)", borderRadius: 10, fontSize: 12 };

function DimCard({ num, title, reason }: { num: string; title: string; reason: string }) {
  return (
    <div className="rounded-[18px] border border-dashed border-[var(--border)] bg-[var(--card)] p-5"
      style={{ opacity: 0.55 }}>
      <div className="flex items-center justify-between">
        <span className="font-['Space_Grotesk'] font-bold text-[14.5px] text-[var(--strong)]">{num} — {title}</span>
        <span className="px-2 py-1 rounded-md bg-[var(--warn-bg)] text-[var(--warn-text)] text-[10.5px] font-extrabold">{reason}</span>
      </div>
      <svg viewBox="0 0 300 70" className="w-full mt-3">
        <polyline points="0,40 60,38 120,42 180,36 240,40 300,38" fill="none"
          stroke="var(--faint)" strokeWidth={1.4} strokeDasharray="4 5" />
      </svg>
    </div>
  );
}

export default function ExecutionSection({ bundle }: { bundle: AnalyticsBundle }) {
  const trades = bundle.trades;

  const slip = useMemo(() => {
    const eligible = trades.filter((t) => t.units && t.n_legs);
    if (!eligible.length) return { data: [], cross: null as number | null };
    const data = Array.from({ length: 31 }, (_, i) => i * 0.1).map((pts) => {
      // pts/leg slippage on every fill: n_legs × 2 fills × units × pts
      const net = eligible.reduce(
        (s, t) => s + t.pnl_rs - pts * t.units! * t.n_legs! * 2, 0);
      return { pts: +pts.toFixed(1), net: Math.round(net) };
    });
    const c = data.find((d) => d.net <= 0);
    return { data, cross: c ? c.pts : null };
  }, [trades]);

  const bd = bundle.costs.breakdown;
  const gross = bundle.costs.gross || 1;
  const parts = bd
    ? (["stt", "exchange", "brokerage", "gst", "stamp", "sebi"] as const)
      .map((k) => ({ k: k.toUpperCase(), v: bd[k] ?? 0, pct: (100 * (bd[k] ?? 0)) / gross }))
      .filter((p) => p.v > 0)
    : [];
  const COLORS = ["var(--danger)", "var(--pe, #d97706)", "var(--indigo, #5b62e8)",
    "var(--accent)", "var(--muted)", "var(--faint)"];

  const liq = bundle.liquidity;

  return (
    <div className="grid gap-4" style={{ gridTemplateColumns: "repeat(auto-fill,minmax(390px,1fr))" }}>
      <Panel wide num="6.2" title="Breakeven slippage" subtitle="net P&L vs assumed slippage per leg per fill (points)"
        insight={slip.cross != null
          ? `The edge dies at ~${slip.cross.toFixed(1)} pts/leg of slippage — backtest fills are minute closes, so judge your real fills against this.`
          : "The edge survives 3 pts/leg of slippage across the tested range."}>
        <div className="h-[240px]"><ResponsiveContainer>
          <LineChart data={slip.data}>
            <CartesianGrid strokeDasharray="2 4" stroke="var(--divider)" vertical={false} />
            <XAxis dataKey="pts" tick={{ fontSize: 10, fill: "var(--faint)" }} tickLine={false}
              axisLine={{ stroke: "var(--border)" }} tickFormatter={(v: number) => `${v} pt`} />
            <YAxis tick={{ fontSize: 10, fill: "var(--faint)" }} width={72} tickLine={false}
              axisLine={{ stroke: "var(--border)" }} tickFormatter={(v: number) => formatInr(v)} />
            <ReferenceLine y={0} stroke="var(--faint)" strokeWidth={1.2} opacity={0.85} />
            {slip.cross != null && (
              <ReferenceLine x={slip.cross} stroke="var(--danger)" strokeDasharray="4 4"
                label={{ value: `${slip.cross} pt`, fontSize: 10, fill: "var(--danger)", position: "top" }} />
            )}
            <Tooltip contentStyle={TT} formatter={(v: number) => [formatInr(v), "net P&L"]} />
            <Line dataKey="net" stroke="var(--accent)" dot={false} strokeWidth={2.2} isAnimationActive={false} />
          </LineChart></ResponsiveContainer></div>
      </Panel>

      <Panel num="6.4" title="Cost decomposition" subtitle="where gross − net goes">
        <div className="grid grid-cols-3 gap-2 mb-3">
          {[["GROSS", bundle.costs.gross], ["COSTS", -bundle.costs.charges], ["NET", bundle.costs.net]].map(([l, v]) => (
            <div key={l as string} className="rounded-[10px] bg-[var(--stat)] px-3 py-2">
              <div className="text-[10px] text-[var(--faint)] font-extrabold">{l}</div>
              <div className="font-['Space_Grotesk'] font-bold text-[14px] tabular-nums"
                style={{ color: (v as number) < 0 ? "var(--danger)" : "var(--strong)" }}>{formatInr(v as number)}</div>
            </div>
          ))}
        </div>
        {parts.length ? (
          <>
            <div className="flex h-[16px] rounded overflow-hidden">
              {parts.map((p, i) => (
                <div key={p.k} title={`${p.k} ${formatInr(p.v)}`}
                  style={{ width: `${Math.max(2, (100 * p.v) / bundle.costs.charges)}%`, background: COLORS[i] }} />
              ))}
            </div>
            <div className="flex flex-wrap gap-x-4 gap-y-1 mt-2">
              {parts.map((p, i) => (
                <span key={p.k} className="text-[10.5px] font-bold text-[var(--muted)]">
                  <span className="inline-block w-2.5 h-2 rounded-sm mr-1 align-middle" style={{ background: COLORS[i] }} />
                  {p.k} {p.pct.toFixed(1)}%
                </span>
              ))}
            </div>
          </>
        ) : (
          <div className="text-[12px] text-[var(--muted)] font-semibold">No per-head charge breakdown stored on this run.</div>
        )}
      </Panel>

      <Panel num="6.5" title="Liquidity feasibility" subtitle="fills larger than the minute's traded volume = phantom fills">
        {liq ? (
          <>
            <div className="grid grid-cols-2 gap-2 mb-3">
              <div className="rounded-[10px] bg-[var(--stat)] px-3 py-2">
                <div className="text-[10px] text-[var(--faint)] font-extrabold">FILLS CHECKED</div>
                <div className="font-['Space_Grotesk'] font-bold text-[15px]">{liq.checked}</div>
              </div>
              <div className="rounded-[10px] bg-[var(--stat)] px-3 py-2">
                <div className="text-[10px] text-[var(--faint)] font-extrabold">FLAGGED</div>
                <div className="font-['Space_Grotesk'] font-bold text-[15px]"
                  style={{ color: liq.flagged ? "var(--danger)" : "var(--pos)" }}>{liq.flagged}</div>
              </div>
            </div>
            {liq.rows.length > 0 && (
              <div className="max-h-[220px] overflow-y-auto">
                <div className="grid gap-2 text-[10px] font-extrabold text-[var(--faint)] px-1 pb-1"
                  style={{ gridTemplateColumns: "1.3fr 1fr 0.6fr 0.8fr 0.6fr" }}>
                  <span>DATE</span><span>LEG</span><span className="text-right">SIZE</span>
                  <span className="text-right">MIN VOL</span><span className="text-right">RATIO</span>
                </div>
                {liq.rows.map((r, i) => (
                  <div key={i} className="grid gap-2 text-[11.5px] font-semibold tabular-nums px-1 py-1 border-b border-[var(--divider)]"
                    style={{ gridTemplateColumns: "1.3fr 1fr 0.6fr 0.8fr 0.6fr" }}>
                    <span>{r.date}</span><span>{r.leg} · {r.which}</span>
                    <span className="text-right">{r.size}</span>
                    <span className="text-right">{r.volume}</span>
                    <span className="text-right text-[var(--danger)]">{r.ratio}×</span>
                  </div>
                ))}
              </div>
            )}
          </>
        ) : (
          <div className="text-[12px] text-[var(--muted)] font-semibold">Needs the 1-minute basis.</div>
        )}
      </Panel>

      <DimCard num="6.1" title="Slippage per leg" reason="needs live fills" />
      <DimCard num="6.3" title="Escalation impact" reason="needs live fills" />
      <DimCard num="6.6" title="Live vs backtest slippage" reason="needs live fills" />
    </div>
  );
}
