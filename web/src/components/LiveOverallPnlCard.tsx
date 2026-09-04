import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Area,
  Bar,
  Cell,
  ComposedChart,
  ReferenceDot,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "../api/client";
import { formatInr } from "../lib/format";
import type { LivePnlDay, LiveRunSnapshot } from "../types";

const POS = "var(--pos)";
const DANGER = "var(--danger)";

const _day = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", day: "numeric", month: "short" });
const _stamp = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Kolkata", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false,
});
const dayLabel = (iso: string) => _day.format(new Date(`${iso}T00:00:00+05:30`));
const todayIST = () => new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });

interface Row extends LivePnlDay {
  label: string;
  live: boolean; // today's row, carried at the LIVE number rather than the day's last sample
}

function OverallTip({ active, payload }: { active?: boolean; payload?: { payload: Row }[] }) {
  const r = payload?.[0]?.payload;
  if (!active || !r) return null;
  const line = (label: string, v: number, tone?: string) => (
    <div className="flex justify-between gap-4">
      <span className="text-[var(--muted)]">{label}</span>
      <span className="font-semibold tabular-nums" style={{ color: tone ?? "var(--strong)" }}>{formatInr(v)}</span>
    </div>
  );
  return (
    <div className="rounded-lg border border-[var(--border)] bg-[var(--menu)] px-3 py-2 text-[12px] text-[var(--strong)] shadow-lg">
      <div className="mb-1 font-bold">{dayLabel(r.date)}{r.live ? " · live" : " · at close"}</div>
      {line(r.live ? "Overall now" : "Overall", r.overall, r.overall >= 0 ? POS : DANGER)}
      {line(r.live ? "Open book" : "Open at close", r.unrealized_eod)}
      {line("Realized to date", r.realized_cum)}
      {r.closes > 0 && line(`Realized that day · ${r.closes} close${r.closes === 1 ? "" : "s"}`, r.realized_day, r.realized_day >= 0 ? POS : DANGER)}
      {r.charges_day > 0 && line("Charges that day", r.charges_day)}
    </div>
  );
}

/** The deployment's overall P&L since deploy, one point per trading day, on dated axes —
 *  the line is realized-to-date plus that day's closing unrealized; the bars are each day's
 *  realized. Today's point is the LIVE overall (realized + open), so the right edge always
 *  equals the KPI beside it. Sits directly under the KPI band; the tile outside shows the
 *  cycle alone. */
export default function LiveOverallPnlCard({ run }: { run: LiveRunSnapshot }) {
  const { data } = useQuery({
    queryKey: ["pnl-history", run.run_id],
    queryFn: () => api.livePnlHistory(run.run_id),
    refetchInterval: 60000,
    staleTime: 30000,
  });

  const liveUnrealized = (run.positions ?? []).reduce((s, p) => s + p.unrealized_pnl, 0);
  const liveOverall = (run.realized_pnl ?? 0) + liveUnrealized;

  const { rows, stats } = useMemo(() => {
    const days = data?.days ?? [];
    const today = todayIST();
    const rows: Row[] = days.map((d) => ({ ...d, label: dayLabel(d.date), live: false }));
    // Today's row carries the live number: the day is not over, and the KPI beside the chart
    // is realized + open right now — the two must agree to the rupee.
    const last = rows[rows.length - 1];
    if (last && last.date === today) {
      last.live = true;
      last.unrealized_eod = liveUnrealized;
      last.overall = last.realized_cum + liveUnrealized;
    } else if (rows.length && run.status === "active") {
      rows.push({
        date: today, label: "now", live: true,
        realized_day: 0, charges_day: 0, closes: 0,
        realized_cum: last?.realized_cum ?? 0, unrealized_eod: liveUnrealized,
        overall: (last?.realized_cum ?? 0) + liveUnrealized,
      });
    }
    let peak = 0;
    let dd = 0;
    let best: Row | null = null;
    let worst: Row | null = null;
    let up = 0;
    let down = 0;
    for (const r of rows) {
      peak = Math.max(peak, r.overall);
      dd = Math.min(dd, r.overall - peak);
      if (r.closes > 0) {
        if (r.realized_day > 0) up += 1;
        else if (r.realized_day < 0) down += 1;
        if (!best || r.realized_day > best.realized_day) best = r;
        if (!worst || r.realized_day < worst.realized_day) worst = r;
      }
    }
    const charges = rows.reduce((s, r) => s + r.charges_day, 0);
    return { rows, stats: { peak, dd, best, worst, up, down, charges, traded: up + down + rows.filter((r) => r.closes > 0 && r.realized_day === 0).length } };
  }, [data, liveUnrealized, run.status]);

  const overallNow = rows.length ? rows[rows.length - 1].overall : liveOverall;
  const LINE = overallNow >= 0 ? POS : DANGER;
  const since = data?.started_at ? _stamp.format(new Date(data.started_at)).replace(",", "") : null;
  const yFmt = (v: number) => (Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(Math.abs(v) >= 10000 ? 0 : 1)}k` : `${v.toFixed(0)}`);
  const lastIdx = rows.length - 1;

  return (
    <div className="mt-3 rounded-[18px] border border-[var(--border)] bg-[var(--card)] p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <span className="font-['Space_Grotesk'] font-bold text-[14px] text-[var(--strong)]">Overall P&L · since deploy</span>
          <span className="font-['Space_Grotesk'] font-bold text-[18px] tabular-nums" style={{ color: LINE }}>{formatInr(overallNow)}</span>
          {since && <span className="text-[11px] text-[var(--faint)]">deployed {since}</span>}
        </div>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11.5px] tabular-nums text-[var(--muted)]">
          <span>{rows.length} day{rows.length === 1 ? "" : "s"}</span>
          {stats.traded > 0 && (
            <span>
              <span className="text-[var(--pos)] font-semibold">{stats.up}</span> up ·{" "}
              <span className="text-[var(--danger)] font-semibold">{stats.down}</span> down
            </span>
          )}
          {stats.best && stats.best.realized_day > 0 && <span>best {stats.best.label} <span className="text-[var(--pos)] font-semibold">{formatInr(stats.best.realized_day)}</span></span>}
          {stats.worst && stats.worst.realized_day < 0 && <span>worst {stats.worst.label} <span className="text-[var(--danger)] font-semibold">{formatInr(stats.worst.realized_day)}</span></span>}
          {stats.dd < 0 && <span>max drawdown <span className="text-[var(--danger)] font-semibold">{formatInr(stats.dd)}</span></span>}
          {stats.charges > 0 && <span>charges {formatInr(stats.charges)}</span>}
        </div>
      </div>

      {rows.length > 1 ? (
        <div className="mt-2">
          <ResponsiveContainer width="100%" height={220}>
            <ComposedChart data={rows} margin={{ top: 10, right: 12, bottom: 0, left: 0 }}>
              <defs>
                <linearGradient id={`ov-${run.run_id}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0" stopColor={LINE} stopOpacity={0.18} />
                  <stop offset="1" stopColor={LINE} stopOpacity={0} />
                </linearGradient>
              </defs>
              <XAxis
                dataKey="label"
                tick={{ fontSize: 10, fill: "var(--muted)" }}
                axisLine={{ stroke: "var(--divider)" }}
                tickLine={false}
                interval="preserveStartEnd"
                minTickGap={28}
              />
              <YAxis
                tick={{ fontSize: 10, fill: "var(--muted)" }}
                width={48}
                tickFormatter={yFmt}
                tickCount={5}
                axisLine={false}
                tickLine={false}
              />
              <ReferenceLine y={0} stroke="var(--divider)" />
              <Tooltip cursor={{ stroke: "var(--faint)", strokeDasharray: "3 3" }} content={<OverallTip />} />
              <Bar dataKey="realized_day" barSize={rows.length > 40 ? 3 : 8} radius={[2, 2, 0, 0]} isAnimationActive={false}>
                {rows.map((r) => (
                  <Cell key={r.date} fill={r.realized_day >= 0 ? "var(--pos-fill)" : "var(--neg-fill)"} stroke={r.realized_day >= 0 ? POS : DANGER} strokeWidth={0.75} />
                ))}
              </Bar>
              <Area type="monotone" dataKey="overall" stroke={LINE} strokeWidth={2} fill={`url(#ov-${run.run_id})`} dot={false} isAnimationActive={false} />
              {lastIdx >= 0 && (
                <ReferenceDot x={rows[lastIdx].label} y={rows[lastIdx].overall} r={3.5} fill={LINE} stroke="var(--card)" strokeWidth={1.5} isFront />
              )}
            </ComposedChart>
          </ResponsiveContainer>
          <div className="mt-1 flex flex-wrap gap-x-4 text-[10.5px] text-[var(--faint)]">
            <span><span className="inline-block w-3 h-[3px] rounded-full align-middle mr-1" style={{ background: LINE }} />overall at each close (realized to date + open book)</span>
            <span><span className="inline-block w-2 h-2 rounded-[2px] align-middle mr-1 border" style={{ background: "var(--pos-fill)", borderColor: POS }} />realized that day</span>
            <span>today's point is live</span>
          </div>
        </div>
      ) : (
        <div className="mt-2 text-[12px] text-[var(--faint)]">
          {rows.length === 1
            ? "One trading day so far — the line starts at the second close."
            : "No trades yet — the chart starts with the first fill."}
        </div>
      )}
    </div>
  );
}
