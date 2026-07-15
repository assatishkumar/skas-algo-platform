import {
  ComposedChart,
  Line,
  ReferenceDot,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { LiveRunSnapshot } from "../types";

/** basket_status payload of weekly_intraday_straddle (kind "weekly_straddle"). */
interface WeeklyStraddleName {
  name: string;
  cycle_expiry?: string | null;
  cycle_strike?: number | null;
  x?: number | null;            // last 5-min combined-premium close
  vwap?: number | null;         // running session VWAP (CE VWAP + PE VWAP)
  vwap_ce?: number | null;
  vwap_pe?: number | null;
  y?: number | null;            // prior day's intraday combined-premium LOW (entry threshold)
  prev_close?: number | null;   // prior day's combined-premium close
  prev_day?: string | null;
  entries_today?: number;
  max_entries?: number;
  series?: { start: string; cc: number; vwap: number | null }[];
}

const OPT = "var(--opt-text)";     // combined premium — the options identity hue
const WARN = "var(--warn-text)";   // VWAP
const DANGER = "var(--danger)";    // prior-day low (the entry threshold)

const _day = new Intl.DateTimeFormat("en-GB", { day: "2-digit", month: "short" });
const fmtDay = (iso?: string | null) => (iso ? _day.format(new Date(`${iso}T00:00:00`)) : "");
const hhmm = (start: string) => start.slice(11, 16);
const rup = (v: number) => `₹${v.toFixed(2)}`;

/** Signal monitor for weekly_intraday_straddle: today's 5-min combined premium vs its running
 *  VWAP, with the prior day's low (the entry threshold) and close as dashed reference levels.
 *  One axis — everything is ₹ combined premium per share. Data rides in basket_status off the
 *  strategy's own per-boundary fetch, so the chart costs no extra broker calls. */
export default function WeeklyStraddlePanel({ run }: { run: LiveRunSnapshot }) {
  const basket = run.basket as unknown as { kind?: string; names?: WeeklyStraddleName[] } | null;
  if (basket?.kind !== "weekly_straddle") return null;
  const n = basket.names?.[0];
  if (!n) return null;

  const rows = (n.series ?? []).map((r, i) => ({ ...r, i }));
  const last = rows.length ? rows[rows.length - 1] : null;
  const armed = n.y != null && n.vwap != null && n.x != null && n.x < n.y && n.x < n.vwap;

  const Chip = ({ color, label, value, title }: { color?: string; label: string; value: string; title?: string }) => (
    <span className="inline-flex items-center gap-1.5 text-[12px] tabular-nums" title={title}>
      {color && <span className="inline-block w-2.5 h-[3px] rounded-full" style={{ background: color }} />}
      <span className="text-[var(--muted)]">{label}</span>
      <span className="font-['Space_Grotesk'] font-semibold" style={{ color: color ?? "var(--strong)" }}>{value}</span>
    </span>
  );

  return (
    <div className="mt-3 rounded-[18px] border border-[var(--border)] bg-[var(--card)] p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
          <span className="font-['Space_Grotesk'] font-bold text-[14px] text-[var(--strong)]">
            Signal · {n.cycle_strike != null ? `${n.cycle_strike.toLocaleString("en-IN")} straddle` : "no cycle"}
            {n.cycle_expiry ? ` · exp ${fmtDay(n.cycle_expiry)}` : ""}
          </span>
          <Chip color={OPT} label="x" value={n.x != null ? rup(n.x) : "—"} title="Combined premium (CE+PE) on the last closed 5-min bar" />
          <Chip color={WARN} label="VWAP" value={n.vwap != null ? rup(n.vwap) : "—"} title="Session VWAP = VWAP(CE) + VWAP(PE)" />
          <Chip label="CE" value={n.vwap_ce != null ? rup(n.vwap_ce) : "—"} title="CE leg session VWAP" />
          <Chip label="PE" value={n.vwap_pe != null ? rup(n.vwap_pe) : "—"} title="PE leg session VWAP" />
          <Chip color={DANGER} label={`low ${fmtDay(n.prev_day)}`} value={n.y != null ? rup(n.y) : "—"} title="Prior day's intraday LOW of the combined premium — the entry threshold (y)" />
          <Chip label={`close ${fmtDay(n.prev_day)}`} value={n.prev_close != null ? rup(n.prev_close) : "—"} title="Prior day's combined-premium close" />
        </div>
        <span className="text-[11px] text-[var(--faint)]">
          {n.entries_today ?? 0}/{n.max_entries ?? 3} entries · SELL when x &lt; low &amp; x &lt; VWAP
          {armed ? " · signal ON" : ""}
        </span>
      </div>
      {rows.length > 1 ? (
        <ResponsiveContainer width="100%" height={190}>
          <ComposedChart data={rows} margin={{ top: 14, right: 76, bottom: 0, left: 0 }}>
            <XAxis dataKey="i" type="number" domain={[0, rows.length - 1]}
              ticks={rows.filter((_, k) => k % 6 === 0).map((r) => r.i)}
              tickFormatter={(i: number) => (rows[Math.round(i)] ? hhmm(rows[Math.round(i)].start) : "")}
              tick={{ fontSize: 9.5, fill: "var(--faint)" }} axisLine={false} tickLine={false} />
            <YAxis tick={{ fontSize: 9.5, fill: "var(--faint)" }} width={44} domain={["auto", "auto"]}
              tickFormatter={(v: number) => v.toFixed(0)} tickCount={4} axisLine={false} tickLine={false} />
            {n.y != null && (
              <ReferenceLine y={n.y} stroke={DANGER} strokeDasharray="4 3" strokeOpacity={0.8}
                label={{ value: `low ${n.y.toFixed(1)}`, position: "right", fontSize: 9.5, fill: DANGER }} />
            )}
            {n.prev_close != null && (
              <ReferenceLine y={n.prev_close} stroke="var(--faint)" strokeDasharray="4 3"
                label={{ value: `close ${n.prev_close.toFixed(1)}`, position: "right", fontSize: 9.5, fill: "var(--faint)" }} />
            )}
            <Tooltip
              contentStyle={{ background: "var(--menu)", border: "1px solid var(--border)", borderRadius: 8, color: "var(--strong)", fontSize: 12 }}
              labelFormatter={(i: number) => (rows[Math.round(i)] ? `${hhmm(rows[Math.round(i)].start)} bar close` : "")}
              formatter={(v: number, key: string) => [rup(v), key === "cc" ? "combined premium" : "VWAP"]} />
            <Line type="monotone" dataKey="vwap" stroke={WARN} strokeWidth={1.6} dot={false} isAnimationActive={false} connectNulls />
            <Line type="monotone" dataKey="cc" stroke={OPT} strokeWidth={1.6} dot={false} isAnimationActive={false} />
            {last && (
              <ReferenceDot x={last.i} y={last.cc} r={3} fill={OPT} stroke="var(--card)" strokeWidth={1.5} isFront />
            )}
          </ComposedChart>
        </ResponsiveContainer>
      ) : (
        <div className="text-[12px] text-[var(--muted)] mt-2">
          Waiting for the first closed 5-min bar… the chart fills as the session trades (updates on each 5-min close).
        </div>
      )}
    </div>
  );
}
