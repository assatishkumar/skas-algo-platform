import { useMemo, useRef, useState } from "react";
import {
  CHART_PALETTE, dayLabel, money, pct,
  type Bucket, type Holding, type PortfolioPayload,
} from "../../lib/portfolio";
import { Card, Kpi, Notice, Segments } from "./primitives";

type Scope = "total" | "class" | "holding" | "bucket";
type Basis = "pct" | "inr";

const MAX_LINES = 8;
const H = 320; // svg user units — the chart's own coordinate space
const W = 1000;
const MARKER_MAX = 60; // draw a dot on every recorded day up to this many — the history is DAILY
const TICK_MAX = 14; // label every day up to this many, else ~8 spaced ticks

interface Series {
  key: string;
  label: string;
  color: string;
  points: (number | null)[]; // rupees, as recorded
  end: number;
}

/** Path over a series that may start late. A gap is a MOVE, not a line to zero — a holding
 * bought last month must not appear to have risen from nothing since tracking began. */
function pathOf(points: (number | null)[], x: (i: number) => number, y: (v: number) => number) {
  let d = "";
  let pen = false;
  points.forEach((v, i) => {
    if (v === null) { pen = false; return; }
    d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)} `;
    pen = true;
  });
  return d.trim();
}

/** Rebase a series to % change from its FIRST recorded day. Eight lines that span ₹14 L to
 * ₹2 Cr share one rupee axis only by flattening every one of them into a ruler line; on a
 * common % axis a 0.8% day on gold and a 0.3% day on EPF are both visible and comparable. */
function rebase(points: (number | null)[]): (number | null)[] {
  const first = points.find((v): v is number => v !== null && v > 0);
  if (first == null) return points.map(() => null);
  return points.map((v) => (v === null ? null : (v / first - 1) * 100));
}

export default function GrowthView({
  rows, payload, buckets,
}: { rows: Holding[]; payload: PortfolioPayload; buckets: Bucket[] }) {
  const [scope, setScope] = useState<Scope>("total");
  const [basis, setBasis] = useState<Basis>("pct");
  const [hidden, setHidden] = useState<Record<string, boolean>>({});
  const [hover, setHover] = useState<number | null>(null);
  const wrap = useRef<HTMLDivElement>(null);

  const g = payload.growth;
  const n = g.dates.length;
  // The total view is a rupee chart (value vs invested only makes sense in rupees); the
  // comparative scopes default to % change and can switch back.
  const pctMode = scope !== "total" && basis === "pct";

  const series: Series[] = useMemo(() => {
    if (scope === "total") {
      return [
        { key: "value", label: "Value", color: "#12b3a4", points: g.total, end: g.total[n - 1] ?? 0 },
        {
          key: "invested", label: "Invested", color: "var(--faint)",
          points: g.invested, end: g.invested[n - 1] ?? 0,
        },
      ];
    }
    if (scope === "holding") {
      return [...rows]
        .sort((a, b) => b.value - a.value)
        .map((h, i) => ({
          key: `h${h.id}`,
          label: h.name,
          color: CHART_PALETTE[i % CHART_PALETTE.length],
          points: g.by_holding[String(h.id)] ?? new Array(n).fill(null),
          end: h.value,
        }));
    }
    if (scope === "class") {
      const keys = Object.keys(payload.asset_classes) as (keyof typeof payload.asset_classes)[];
      return keys
        .map((k) => {
          const members = rows.filter((h) => h.asset_class === k);
          if (!members.length) return null;
          const points: (number | null)[] = new Array(n).fill(null);
          for (const h of members) {
            const s = g.by_holding[String(h.id)];
            if (!s) continue;
            s.forEach((v, i) => {
              if (v === null) return;
              points[i] = (points[i] ?? 0) + v;
            });
          }
          return {
            key: String(k),
            label: payload.asset_classes[k].label,
            color: payload.asset_classes[k].color,
            points,
            end: members.reduce((a, h) => a + h.value, 0),
          };
        })
        .filter((s): s is Series => s !== null)
        .sort((a, b) => b.end - a.end);
    }
    return buckets.map((b, i) => {
      const points: (number | null)[] = new Array(n).fill(null);
      let end = 0;
      for (const id of b.holding_ids) {
        const h = rows.find((x) => x.id === id);
        if (h) end += h.value;
        const s = g.by_holding[String(id)];
        if (!s) continue;
        s.forEach((v, idx) => {
          if (v === null) return;
          points[idx] = (points[idx] ?? 0) + v;
        });
      }
      return {
        key: `b${b.id}`, label: b.name,
        color: CHART_PALETTE[i % CHART_PALETTE.length], points, end,
      };
    });
  }, [scope, rows, buckets, g, n, payload.asset_classes]);

  const visible = useMemo(
    () => series.filter((s) => !hidden[s.key]).slice(0, scope === "total" ? 2 : MAX_LINES),
    [series, hidden, scope],
  );
  const overflow = scope !== "total" && series.filter((s) => !hidden[s.key]).length > MAX_LINES;

  // What is PLOTTED: rupees, or % change from each series' first day.
  const plotted = useMemo(
    () => visible.map((s) => ({ ...s, plot: pctMode ? rebase(s.points) : s.points })),
    [visible, pctMode],
  );

  const bounds = useMemo(() => {
    let lo = Infinity;
    let hi = -Infinity;
    for (const s of plotted) {
      for (const v of s.plot) {
        if (v === null) continue;
        lo = Math.min(lo, v);
        hi = Math.max(hi, v);
      }
    }
    if (!Number.isFinite(lo)) return { lo: 0, hi: 1 };
    if (pctMode) {
      // Symmetric-ish around zero with headroom, so a flat first day sits mid-chart.
      const span = Math.max(hi - lo, 0.5);
      return { lo: Math.min(lo, 0) - span * 0.15, hi: Math.max(hi, 0) + span * 0.15 };
    }
    if (lo === hi) return { lo: lo * 0.95, hi: hi * 1.05 || 1 };
    return { lo: lo * 0.96, hi: hi * 1.03 };
  }, [plotted, pctMode]);

  const x = (i: number) => (n <= 1 ? W / 2 : (i / (n - 1)) * W);
  const y = (v: number) => H - 10 - ((v - bounds.lo) / (bounds.hi - bounds.lo)) * (H - 20);
  const fmtAxis = (v: number) => (pctMode ? `${v >= 0 ? "+" : ""}${v.toFixed(1)}%` : money(v));

  // Day ticks: EVERY recorded day while there are few (the history is one point per trading
  // day — four points must read as four days, not as a line between two dates), else spaced.
  const ticks = useMemo(() => {
    if (n <= 1) return n === 1 ? [0] : [];
    if (n <= TICK_MAX) return Array.from({ length: n }, (_, i) => i);
    const count = 8;
    const out = new Set<number>();
    for (let k = 0; k < count; k += 1) out.add(Math.round((k / (count - 1)) * (n - 1)));
    return [...out].sort((a, b) => a - b);
  }, [n]);

  // ---- KPIs, from what was actually recorded. No history, no number.
  const kpis = useMemo(() => {
    if (!g.enough_for_trend) return null;
    const first = g.total[0];
    const last = g.total[n - 1];
    const days = (new Date(g.dates[n - 1]).getTime() - new Date(g.dates[0]).getTime()) / 86_400_000;
    const years = days / 365.25;
    let peak = g.total[0];
    let dd = 0;
    let bestStep = 0;
    for (let i = 0; i < n; i += 1) {
      peak = Math.max(peak, g.total[i]);
      dd = Math.min(dd, (g.total[i] - peak) / peak);
      if (i > 0 && g.total[i - 1] > 0) {
        bestStep = Math.max(bestStep, (g.total[i] - g.total[i - 1]) / g.total[i - 1]);
      }
    }
    return {
      spanLabel: `${dayLabel(g.dates[0])} → ${dayLabel(g.dates[n - 1])}`,
      growth: first > 0 ? (last / first - 1) * 100 : 0,
      cagr: first > 0 && years >= 0.5 ? ((last / first) ** (1 / years) - 1) * 100 : null,
      bestStep: bestStep * 100,
      drawdown: dd * 100,
      years,
    };
  }, [g, n]);

  const title =
    scope === "total" ? "Net worth over time"
      : scope === "class" ? "Growth by asset class"
      : scope === "holding" ? "Growth by holding"
      : "Growth by bucket";

  return (
    <div>
      {!g.enough_for_trend && (
        <div className="mb-3.5">
          <Notice tone="warn">
            {g.points === 0 ? (
              <>
                No history recorded yet. This chart is built from a daily snapshot taken after
                the close — nothing is back-filled, because a portfolio's past value can't be
                reconstructed from today's units without inventing it. The first point appears
                after today's snapshot; hit <strong>Sync</strong> to record one now.
              </>
            ) : (
              <>
                {g.points} point{g.points === 1 ? "" : "s"} recorded since {dayLabel(g.since)} —
                too few to read as a trend. One is added each trading day; the growth figures
                below unlock at 8.
              </>
            )}
          </Notice>
        </div>
      )}

      <div className="mb-3.5 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Kpi
          label="GROWTH"
          value={kpis ? pct(kpis.growth) : "—"}
          sub={kpis ? kpis.spanLabel : "needs 8 daily snapshots"}
          valueColor={kpis && kpis.growth < 0 ? "var(--danger)" : "var(--pos)"}
          help="Change in net worth across the recorded window — contributions plus returns, not a return on its own."
        />
        <Kpi
          label="CAGR · VALUE"
          value={kpis?.cagr != null ? pct(kpis.cagr) : "—"}
          sub={kpis?.cagr != null ? `over ${kpis.years.toFixed(1)}y` : "needs 6+ months of history"}
          valueColor={(kpis?.cagr ?? 0) < 0 ? "var(--danger)" : "var(--pos)"}
          help="Compound growth of the whole net-worth line. Compare it against blended XIRR to see how much of the rise is fresh money rather than returns."
        />
        <Kpi
          label="BEST DAY"
          value={kpis ? pct(kpis.bestStep) : "—"}
          sub={kpis ? "largest single-snapshot rise" : "needs 8 daily snapshots"}
          valueColor="var(--pos)"
          help="Biggest jump between two consecutive snapshots. A contribution landing shows up here too — it is not purely a market move."
        />
        <Kpi
          label="MAX DRAWDOWN"
          value={kpis ? pct(kpis.drawdown) : "—"}
          sub={kpis ? "peak to trough" : "needs 8 daily snapshots"}
          valueColor="var(--danger)"
          help="Worst peak-to-trough dip in the recorded window. Under ~10% for a mixed portfolio is comfortable; much deeper means the equity share is driving."
        />
      </div>

      <Card pad="p-5">
        <div className="mb-3 flex flex-wrap items-center gap-3.5">
          <span className="text-[15px] font-extrabold text-[var(--strong)]">{title}</span>
          <Segments
            size="sm"
            value={scope}
            onChange={(v) => { setScope(v); setHidden({}); }}
            options={[
              { value: "total", label: "Total" },
              { value: "class", label: "By class" },
              { value: "holding", label: "By holding" },
              { value: "bucket", label: "By bucket" },
            ]}
          />
          {scope !== "total" && (
            <Segments
              size="sm"
              value={basis}
              onChange={setBasis}
              options={[
                { value: "pct", label: "% since start" },
                { value: "inr", label: "₹ value" },
              ]}
            />
          )}
          {scope === "total" && (
            <span className="ml-auto flex gap-4 text-[12px] font-bold text-[var(--muted)]">
              <span className="flex items-center gap-1.5">
                <span className="inline-block h-[3px] w-3.5 rounded-sm bg-[var(--accent)]" />Value
              </span>
              <span className="flex items-center gap-1.5">
                <span className="inline-block w-3.5 border-t-2 border-dashed border-[var(--faint)]" />
                Invested
              </span>
            </span>
          )}
          {n > 0 && (
            <span className="ml-auto text-[11.5px] font-semibold text-[var(--faint)]">
              {n} trading day{n === 1 ? "" : "s"} · one point per close
            </span>
          )}
        </div>

        {scope !== "total" && (
          <div className="mb-3 flex flex-wrap gap-[7px]">
            {series.map((s) => {
              const on = !hidden[s.key];
              return (
                <button
                  key={s.key}
                  onClick={() => setHidden((h) => ({ ...h, [s.key]: on }))}
                  className="inline-flex items-center gap-1.5 rounded-full border-[1.5px] px-3 py-1.5 text-[12px] font-bold"
                  style={{
                    background: on ? "var(--tint)" : "var(--card)",
                    borderColor: on ? "var(--tint-border)" : "var(--border)",
                    color: on ? "var(--strong)" : "var(--faint)",
                  }}
                >
                  <span
                    className="inline-block h-2 w-2 rounded-full"
                    style={{ background: on ? s.color : "var(--faint)" }}
                  />
                  {s.label}
                </button>
              );
            })}
            {overflow && (
              <span className="inline-flex items-center text-[11.5px] font-bold text-[var(--warn-text)]">
                Showing the first {MAX_LINES} — deselect some to compare fewer
              </span>
            )}
          </div>
        )}

        {n === 0 ? (
          <div className="flex h-[220px] items-center justify-center text-[13px] font-semibold text-[var(--faint)]">
            The chart appears once the first snapshot is recorded.
          </div>
        ) : (
          <div className="relative h-[340px]" ref={wrap}>
            <div className="absolute bottom-[26px] left-16 right-2 top-0">
              <svg
                viewBox={`0 0 ${W} ${H}`}
                preserveAspectRatio="none"
                className="block h-full w-full"
                onMouseMove={(e) => {
                  const box = e.currentTarget.getBoundingClientRect();
                  const frac = (e.clientX - box.left) / box.width;
                  setHover(Math.max(0, Math.min(n - 1, Math.round(frac * (n - 1)))));
                }}
                onMouseLeave={() => setHover(null)}
              >
                <line x1="0" y1={H - 1} x2={W} y2={H - 1} stroke="#9aa8a4" strokeWidth="1.5" />
                {/* Day gridlines: one per recorded close while they are few. */}
                {n <= MARKER_MAX && ticks.map((i) => (
                  <line
                    key={`g${i}`}
                    x1={x(i)} y1="0" x2={x(i)} y2={H}
                    stroke="var(--border)" strokeWidth="1" strokeDasharray="3 4" vectorEffect="non-scaling-stroke"
                  />
                ))}
                {pctMode && bounds.lo < 0 && bounds.hi > 0 && (
                  <line
                    x1="0" y1={y(0)} x2={W} y2={y(0)}
                    stroke="var(--faint)" strokeWidth="1" strokeDasharray="5 4" vectorEffect="non-scaling-stroke"
                  />
                )}
                {scope === "total" && g.total.length > 0 && (
                  <path
                    d={`${pathOf(g.total, x, y)} L${W},${H} L0,${H} Z`}
                    fill="#12b3a4"
                    fillOpacity=".12"
                  />
                )}
                {plotted.map((s) => (
                  <path
                    key={s.key}
                    d={pathOf(s.plot, x, y)}
                    fill="none"
                    stroke={s.key === "invested" ? "#9aa8a4" : s.color}
                    strokeWidth={s.key === "invested" ? 2 : 2.4}
                    strokeDasharray={s.key === "invested" ? "6 5" : undefined}
                    vectorEffect="non-scaling-stroke"
                  />
                ))}
                {hover !== null && (
                  <line
                    x1={x(hover)} y1="0" x2={x(hover)} y2={H}
                    stroke="var(--faint)" strokeWidth="1" vectorEffect="non-scaling-stroke"
                  />
                )}
              </svg>

              {/* A dot on every recorded day (HTML, so the stretched SVG can't squash it). */}
              {n <= MARKER_MAX && plotted.map((s) => s.plot.map((v, i) => (
                v === null ? null : (
                  <span
                    key={`${s.key}-${i}`}
                    className="pointer-events-none absolute h-[7px] w-[7px] -translate-x-1/2 -translate-y-1/2 rounded-full border-[1.5px] border-[var(--card)]"
                    style={{
                      left: `${(x(i) / W) * 100}%`,
                      top: `${(y(v) / H) * 100}%`,
                      background: s.key === "invested" ? "#9aa8a4" : s.color,
                      opacity: hover === null || hover === i ? 1 : 0.55,
                    }}
                  />
                )
              )))}

              {[0, 1, 2, 3].map((k) => {
                const v = bounds.lo + ((bounds.hi - bounds.lo) * k) / 3;
                return (
                  <span
                    key={k}
                    className="absolute -left-16 w-14 -translate-y-1/2 text-right text-[11px] font-bold text-[var(--faint)]"
                    style={{ top: `${(y(v) / H) * 100}%` }}
                  >
                    {fmtAxis(v)}
                  </span>
                );
              })}

              {ticks.map((i) => (
                <span
                  key={i}
                  className={`absolute -bottom-[22px] whitespace-nowrap text-[11px] font-bold ${
                    i === hover ? "text-[var(--strong)]" : "text-[var(--faint)]"
                  } ${i === 0 && n > 1 ? "" : i === n - 1 ? "-translate-x-full" : "-translate-x-1/2"}`}
                  style={{ left: `${(x(i) / W) * 100}%` }}
                >
                  {dayLabel(g.dates[i])}
                </span>
              ))}

              {scope !== "total" && plotted.map((s) => {
                const last = [...s.plot].reverse().find((v) => v !== null);
                if (last == null) return null;
                return (
                  <span
                    key={s.key}
                    className="absolute right-0.5 -translate-y-full rounded px-[3px] text-[11px] font-extrabold"
                    style={{ top: `${(y(last) / H) * 100}%`, color: s.color, background: "var(--card)" }}
                  >
                    {pctMode ? pct(last) : money(s.end)}
                  </span>
                );
              })}
            </div>

            {hover !== null && (
              <div
                className="pointer-events-none absolute top-1 z-10 min-w-[190px] rounded-[10px] border border-[var(--border)] bg-[var(--card)] px-3 py-2 text-[11.5px] shadow-lg"
                style={{
                  left: `calc(64px + ${(x(hover) / W) * 100}% * (100% - 72px) / 100%)`,
                  transform: hover > n / 2 ? "translateX(-108%)" : "translateX(8px)",
                }}
              >
                <div className="mb-1 font-extrabold text-[var(--strong)]">
                  {dayLabel(g.dates[hover])}
                </div>
                {plotted.map((s) => {
                  const raw = s.points[hover];
                  const prev = hover > 0 ? s.points[hover - 1] : null;
                  const day = raw != null && prev != null && prev > 0 ? (raw / prev - 1) * 100 : null;
                  const sinceStart = s.plot[hover];
                  return (
                    <div key={s.key} className="flex items-center justify-between gap-3">
                      <span className="flex items-center gap-1.5 text-[var(--muted)]">
                        <span
                          className="inline-block h-2 w-2 rounded-full"
                          style={{ background: s.key === "invested" ? "#9aa8a4" : s.color }}
                        />
                        {s.label}
                      </span>
                      <span className="font-bold tabular-nums text-[var(--strong)]">
                        {raw == null ? "—" : money(raw)}
                        {day != null && (
                          <span className={`ml-1.5 font-semibold ${day >= 0 ? "text-[var(--pos)]" : "text-[var(--danger)]"}`}>
                            {pct(day, 2)}
                          </span>
                        )}
                        {pctMode && sinceStart != null && (
                          <span className="ml-1.5 text-[var(--faint)]">{pct(sinceStart, 2)} total</span>
                        )}
                      </span>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        )}
      </Card>
    </div>
  );
}
