import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
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
import { api } from "../api/client";
import { formatInr } from "../lib/format";
import type { LiveRunSnapshot } from "../types";

const _fmtIST = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Kolkata", weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false,
});
const _dayIST = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Kolkata", day: "2-digit", month: "short",
});
const _timeIST = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false,
});

/** True only within NSE market hours: Mon–Fri 09:15–15:30 IST. */
function marketHourIST(ms: number): boolean {
  const parts = _fmtIST.formatToParts(new Date(ms));
  const wd = parts.find((p) => p.type === "weekday")?.value;
  if (wd === "Sat" || wd === "Sun") return false;
  const mins = Number(parts.find((p) => p.type === "hour")?.value) * 60 +
    Number(parts.find((p) => p.type === "minute")?.value);
  return mins >= 555 && mins <= 930; // 09:15 → 15:30
}
const dayKey = (ms: number) => _dayIST.format(new Date(ms));

const DANGER = "var(--danger)";
const OPT = "var(--opt-text)";
const WARN = "var(--warn-text)";

/** History for an options deployment as three synced single-axis small-multiples (P&L, Net Δ, IV) —
 *  replacing the old dual-axis overlay (two y-scales on one plot is unreadable + a charting
 *  anti-pattern). Market-hours samples only, re-indexed so overnight gaps collapse; dashed session
 *  breaks mark trading-day boundaries. Scoped to the current cycle (samples since the earliest open
 *  lot) so a prior expired cycle's history doesn't linger. */
export default function GreeksHistoryCard({ run }: { run: LiveRunSnapshot }) {
  const { data } = useQuery({
    queryKey: ["greeks-history", run.run_id],
    queryFn: () => api.liveGreeksHistory(run.run_id),
    refetchInterval: 30000,
  });

  const { rows, breaks, nowLabel } = useMemo(() => {
    const entryDates = (run.positions ?? []).map((p) => p.entry_date).filter((d): d is string => !!d);
    const cycleStartMs = entryDates.length
      ? Math.min(...entryDates.map((d) => new Date(`${d}T00:00:00+05:30`).getTime()))
      : 0;
    const all = (data?.points ?? [])
      .map((p) => ({ t: p.ts ? new Date(p.ts).getTime() : 0, delta: p.net_delta, iv: p.net_iv != null ? p.net_iv * 100 : null, pnl: p.pnl }))
      .filter((r) => r.t && r.t >= cycleStartMs);
    const mh = all.filter((r) => marketHourIST(r.t));
    const src = mh.length > 1 ? mh : all;
    const rows = src.map((r, i) => ({ ...r, i }));
    // Session breaks: index where the IST calendar day changes.
    const breaks: { i: number; label: string }[] = [];
    for (let k = 1; k < rows.length; k++) {
      if (dayKey(rows[k].t) !== dayKey(rows[k - 1].t)) breaks.push({ i: rows[k].i, label: dayKey(rows[k].t) });
    }
    if (rows.length) breaks.unshift({ i: 0, label: dayKey(rows[0].t) });
    const nowLabel = rows.length ? `now · ${_timeIST.format(new Date(rows[rows.length - 1].t))}` : "";
    return { rows, breaks, nowLabel };
  }, [data, run.positions]);

  const netDelta = run.net_delta;
  const netIv = run.net_iv;
  const livePnl = (run.positions ?? []).reduce((s, p) => s + p.unrealized_pnl, 0);
  const last = rows.length ? rows[rows.length - 1] : null;

  const xDomain: [number, number] = [0, Math.max(0, rows.length - 1)];
  const xLabel = (i: number) => (rows[Math.round(i)] ? new Date(rows[Math.round(i)].t).toLocaleString("en-IN") : "");

  const Chip = ({ color, label, value }: { color: string; label: string; value: string }) => (
    <span className="inline-flex items-center gap-1.5 text-[12px] tabular-nums">
      <span className="inline-block w-2.5 h-[3px] rounded-full" style={{ background: color }} />
      <span className="text-[var(--muted)]">{label}</span>
      <span className="font-['Space_Grotesk'] font-semibold" style={{ color }}>{value}</span>
    </span>
  );

  // One panel = a single-axis small-multiple. `kind` picks line vs filled area.
  const Panel = ({
    height, corner, dataKey, color, area, fmt, zeroLine, yFmt,
  }: {
    height: number; corner: string; dataKey: "pnl" | "delta" | "iv"; color: string;
    area?: boolean; fmt: (v: number) => string; zeroLine?: boolean; yFmt: (v: number) => string;
  }) => (
    <div className="relative">
      <div className="absolute left-2 top-1 z-10 text-[9.5px] uppercase tracking-wide text-[var(--faint)] pointer-events-none">{corner}</div>
      <ResponsiveContainer width="100%" height={height}>
        <ComposedChart data={rows} margin={{ top: 4, right: 10, bottom: 0, left: 0 }}>
          <XAxis dataKey="i" type="number" domain={xDomain} hide />
          <YAxis tick={{ fontSize: 9.5, fill: "var(--faint)" }} width={44} tickFormatter={yFmt}
            tickCount={3} axisLine={false} tickLine={false} />
          {zeroLine && <ReferenceLine y={0} stroke="var(--divider)" strokeDasharray="3 3" />}
          {breaks.slice(1).map((b) => (
            <ReferenceLine key={b.i} x={b.i} stroke="var(--divider)" strokeDasharray="3 3" />
          ))}
          <Tooltip
            contentStyle={{ background: "var(--menu)", border: "1px solid var(--border)", borderRadius: 8, color: "var(--strong)", fontSize: 12 }}
            labelFormatter={xLabel} formatter={(v: number) => [fmt(v), corner]} />
          {area ? (
            <Area type="monotone" dataKey={dataKey} stroke={color} strokeWidth={1.6} fill="var(--neg-fill)" isAnimationActive={false} dot={false} />
          ) : (
            <Line type="monotone" dataKey={dataKey} stroke={color} strokeWidth={1.6} dot={false} isAnimationActive={false} />
          )}
          {last && last[dataKey] != null && (
            <ReferenceDot x={last.i} y={last[dataKey] as number} r={3} fill={color} stroke="var(--card)" strokeWidth={1.5} isFront />
          )}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );

  return (
    <div className="mt-3 rounded-[18px] border border-[var(--border)] bg-[var(--card)] p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
          <span className="font-['Space_Grotesk'] font-bold text-[14px] text-[var(--strong)]">History · this cycle</span>
          <Chip color={DANGER} label="P&L" value={formatInr(last?.pnl ?? livePnl)} />
          <Chip color={OPT} label="Net Δ" value={netDelta != null ? netDelta.toFixed(1) : "—"} />
          <Chip color={WARN} label="IV" value={netIv != null ? `${(netIv * 100).toFixed(1)}%` : "—"} />
        </div>
        <span className="text-[11px] text-[var(--faint)]">market hours only · sampled ~1/min</span>
      </div>
      {rows.length > 1 ? (
        <div className="mt-2 space-y-1">
          <Panel height={110} corner="P&L" dataKey="pnl" color={DANGER} area
            fmt={(v) => formatInr(v)} yFmt={(v) => `${(v / 1000).toFixed(0)}k`} />
          <Panel height={96} corner="Net Δ · position" dataKey="delta" color={OPT} zeroLine
            fmt={(v) => v.toFixed(1)} yFmt={(v) => v.toFixed(0)} />
          <Panel height={72} corner="IV % · book avg" dataKey="iv" color={WARN}
            fmt={(v) => `${v.toFixed(1)}%`} yFmt={(v) => `${v.toFixed(0)}%`} />
          {/* Day axis: session labels at each break + the current time at the right edge. */}
          <div className="relative h-4 ml-[44px] mr-[10px] text-[10px] text-[var(--muted)] tabular-nums">
            {breaks.map((b) => (
              <span key={b.i} className={`absolute ${b.i === 0 ? "" : "-translate-x-1/2"}`} style={{ left: `${rows.length > 1 ? (b.i / (rows.length - 1)) * 100 : 0}%` }}>
                {b.label}
              </span>
            ))}
            <span className="absolute right-0 text-[var(--faint)]">{nowLabel}</span>
          </div>
        </div>
      ) : (
        <div className="text-[12px] text-[var(--muted)] mt-2">Collecting greeks history…</div>
      )}
    </div>
  );
}
