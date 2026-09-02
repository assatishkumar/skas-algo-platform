import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
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
import { formatInr } from "../lib/format";
import { bookFromPositions, buildLivePayoff, computeMetrics, effectiveSpot } from "../lib/payoff";
import type { LivePosition } from "../types";

const CE_COLOR = "#8b5cf6"; // violet — CE strikes
const PE_COLOR = "#f59e0b"; // amber — PE strikes
const BE_COLOR = "var(--accent-deep)"; // breakeven markers
const GHOST_COLOR = "#94a3b8"; // the "before this event" tent

// Chart geometry the strike rail needs in PIXELS: recharts lays the plot out inside these.
const Y_AXIS_W = 64;
const MARGIN = { top: 18, right: 12, bottom: 0, left: 12 };
const PILL_H = 16;
const ROW_GAP = 20;
const pillWidth = (text: string) => text.length * 6.2 + 12;

/** Breakeven pill dropped into the lower chart region, connected to its ringed zero-crossing dot by
 *  a dashed stem — recharts uses a normal SVG viewBox (no preserveAspectRatio distortion) so SVG
 *  text is safe here. viewBox is the reference LINE's box ({x, y=top, height}). */
function BeLabel({ viewBox, text }: { viewBox?: { x: number; y: number; height: number }; text: string }) {
  if (!viewBox) return null;
  const { x, y, height } = viewBox;
  const py = y + height * 0.58; // upper-middle: clear of the zero-line curve AND the strike rail below
  const w = pillWidth(text);
  return (
    <g style={{ pointerEvents: "none" }}>
      {/* short callout stem up from the pill (a full-height line would read as a gridline) */}
      <line x1={x} y1={py - 24} x2={x} y2={py} stroke={BE_COLOR} strokeWidth={1} strokeDasharray="3 3" opacity={0.8} />
      <rect x={x - w / 2} y={py} width={w} height={17} rx={5} fill="var(--card)" stroke={BE_COLOR} strokeWidth={1} />
      <text x={x} y={py + 12} textAnchor="middle" fontSize={10} fontWeight={700} fill={BE_COLOR}>{text}</text>
    </g>
  );
}

/** Strike marker: a HORIZONTAL pill on a rail just above the x-axis, one row per overlap
 *  (rows are assigned by the parent from the measured plot width, so a tight cluster of
 *  strikes stacks instead of colliding). SELL = filled, BUY = outlined + dashed — the same
 *  code as the line it labels. Replaces the rotated-along-the-line text, which read as a
 *  smear of letters (owner, 2026-09-02). */
function StrikePill({ viewBox, text, color, buy, row, clamp }: {
  viewBox?: { x: number; y: number; height: number }; text: string; color: string; buy: boolean;
  row: number; clamp: [number, number];
}) {
  if (!viewBox) return null;
  const w = pillWidth(text);
  // keep the pill inside the plot: a strike at the window's edge would otherwise half-vanish
  const cx = Math.min(Math.max(viewBox.x, clamp[0] + w / 2), clamp[1] - w / 2);
  const py = viewBox.y + viewBox.height - 6 - PILL_H - row * ROW_GAP;
  return (
    <g style={{ pointerEvents: "none" }}>
      {cx !== viewBox.x && (
        <line x1={viewBox.x} y1={py + PILL_H} x2={cx} y2={py + PILL_H} stroke={color} strokeWidth={1} strokeDasharray="2 2" />
      )}
      <rect x={cx - w / 2} y={py} width={w} height={PILL_H} rx={5}
        fill={buy ? "var(--card)" : color} stroke={color} strokeWidth={1.25} strokeDasharray={buy ? "3 2" : undefined} />
      <text x={cx} y={py + 11.5} textAnchor="middle" fontSize={10} fontWeight={800} fill={buy ? color : "#fff"}>{text}</text>
    </g>
  );
}

/** Sensibull-style payoff for the OPEN option legs of a live deployment: the expiry tent
 *  (green/red split at zero) + a current-value (T+0) curve, with the live spot marked.
 *  Built client-side from the position legs + live LTPs. */
// Zoom ladder for the x-range: null = auto (all strikes ±10%), else spot ± pct.
const ZOOMS: (number | null)[] = [null, 0.08, 0.05, 0.03, 0.015];

export interface PayoffGhost {
  positions: LivePosition[]; // the book to draw as a faint "before" tent
  spot?: number | null;
  asOf?: string;
  offset?: number; // that book's own realized-so-far
  label: string; // tooltip / legend name ("before R1")
}

export default function LivePayoffChart({
  positions,
  spot,
  asOf,
  caption,
  spotLabel = "spot",
  nowLabel = "Current",
  pnlOffset = 0,
  ghost,
}: {
  positions: LivePosition[];
  spot: number | null | undefined;
  asOf?: string; // value-curve as-of date (defaults to today — i.e. the live "now" curve)
  caption?: ReactNode; // overrides the default "Payoff at expiry …" caption
  spotLabel?: string; // label on the vertical spot line
  nowLabel?: string; // tooltip name of the dashed value curve ("Current" live; "At this point" for a snapshot)
  // realized P&L to add to every point, so the curves read as CYCLE P&L (a point-in-time
  // snapshot after a roll); 0 = the standing book alone (the live page)
  pnlOffset?: number;
  // a second book drawn as a dotted grey expiry tent on the same axes — "before this event"
  ghost?: PayoffGhost | null;
}) {
  const [zoomIdx, setZoomIdx] = useState(0);
  const { legs, expiry, underlying: chartUnderlying, skipped } = useMemo(() => bookFromPositions(positions), [positions]);
  // The run's underlying_spot is the PRIMARY underlying's — swap in a spot that matches
  // the charted book (parity-derived when the provided one is another index's).
  const chartSpot = useMemo(() => effectiveSpot(legs, spot), [legs, spot]);
  // Breakevens = zero-crossings of the expiry payoff; mark the ones inside the visible x-window.
  // Computed BEFORE the payoff so the auto-range can include them (keeps the tent's zero-crossings
  // on-screen and the x-window tight to the structure).
  const bes = useMemo(
    () => (legs.length && chartSpot && expiry
      ? computeMetrics(legs, chartSpot, expiry, asOf, undefined, pnlOffset)?.breakevens ?? [] : []),
    [legs, chartSpot, expiry, asOf, pnlOffset],
  );
  const pf = useMemo(
    () => (legs.length && chartSpot && expiry
      ? buildLivePayoff(legs, chartSpot, expiry, asOf, ZOOMS[zoomIdx], bes, { offset: pnlOffset }) : null),
    [legs, chartSpot, expiry, asOf, zoomIdx, bes, pnlOffset],
  );
  // The ghost tent on the SAME x-grid (explicit range) so the two merge point-for-point.
  const data = useMemo(() => {
    if (!pf) return null;
    if (!ghost) return pf.data;
    const gb = bookFromPositions(ghost.positions);
    const gs = effectiveSpot(gb.legs, ghost.spot);
    if (!gb.legs.length || !gs) return pf.data;
    const g = buildLivePayoff(gb.legs, gs, gb.expiry, ghost.asOf, null, undefined,
      { offset: ghost.offset ?? 0, range: [pf.data[0].spot, pf.data[pf.data.length - 1].spot] });
    if (!g) return pf.data;
    return pf.data.map((d, i) => ({ ...d, prev: g.data[i]?.expiry }));
  }, [pf, ghost]);

  // Plot width in pixels — the strike rail packs its pills into rows by measured overlap.
  const wrapRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    setWidth(el.clientWidth);
    return () => ro.disconnect();
  }, []);

  // Unique CE/PE strikes to mark on the chart (a strangle → one CE + one PE line; dedup exact
  // dupes), each with its rail row: walk them in strike order and drop each pill into the
  // first row whose last pill ends before this one starts.
  const strikes = useMemo(() => {
    const m = new Map<string, { strike: number; right: string; dir: number }>();
    for (const p of positions) {
      const parts = p.symbol.split("|"); // UNDERLYING|EXPIRY|STRIKE|RIGHT
      if (parts.length !== 4) continue;
      const strike = Number(parts[2]);
      if (!Number.isFinite(strike)) continue;
      const right = parts[3];
      const key = `${strike}|${right}`;
      if (!m.has(key)) m.set(key, { strike, right, dir: p.direction ?? 1 });
    }
    const sorted = [...m.values()].sort((a, b) => a.strike - b.strike);
    if (!pf) return sorted.map((s) => ({ ...s, row: 0 }));
    const lo = pf.data[0].spot, hi = pf.data[pf.data.length - 1].spot;
    const plotW = Math.max(width - MARGIN.left - Y_AXIS_W - MARGIN.right, 1);
    const px = (k: number) => ((k - lo) / Math.max(hi - lo, 1e-9)) * plotW;
    const rowEnd: number[] = [];
    return sorted.map((s) => {
      const w = pillWidth(`${s.dir === 1 ? "BUY" : "SELL"} ${s.strike} ${s.right}`);
      const left = px(s.strike) - w / 2;
      let row = rowEnd.findIndex((end) => end + 4 <= left);
      if (row < 0) row = Math.min(rowEnd.length, 3); // at most four rows; beyond that, overlap
      rowEnd[row] = left + w;
      return { ...s, row };
    });
  }, [positions, pf, width]);

  if (!pf || !data) return null;
  // Split the fill green/red at P&L = 0. The gradient maps to the EXPIRY area's own bounding box,
  // so the offset must come from the expiry series alone (not the dashed "now" line) — otherwise an
  // all-profit position gets painted red.
  const exp = pf.data.map((d) => d.expiry);
  const eMax = Math.max(...exp);
  const eMin = Math.min(...exp);
  const off = eMax <= 0 ? 0 : eMin >= 0 ? 1 : eMax / (eMax - eMin);
  const plotLeft = MARGIN.left + Y_AXIS_W;
  const plotRight = Math.max(width - MARGIN.right, plotLeft + 1);
  const inWindow = (x: number) => !pf.data.length || (x >= pf.data[0].spot && x <= pf.data[pf.data.length - 1].spot);

  const zoom = ZOOMS[zoomIdx];
  return (
    <div className="mt-3" ref={wrapRef}>
      <div className="mb-1 flex items-center justify-end gap-1">
        <span className="mr-1 text-[11px] tabular-nums text-[var(--faint)]">
          {zoom == null ? "auto range" : `±${(zoom * 100).toFixed(1).replace(/\.0$/, "")}%`}
        </span>
        <button onClick={() => setZoomIdx((i) => Math.min(i + 1, ZOOMS.length - 1))}
          disabled={zoomIdx >= ZOOMS.length - 1} title="Zoom in (narrower spot range)"
          className="rounded-[7px] bg-[var(--chip)] px-2 py-0.5 text-xs font-bold text-[var(--chip-text)] disabled:opacity-40">+</button>
        <button onClick={() => setZoomIdx((i) => Math.max(i - 1, 0))}
          disabled={zoomIdx === 0} title="Zoom out"
          className="rounded-[7px] bg-[var(--chip)] px-2 py-0.5 text-xs font-bold text-[var(--chip-text)] disabled:opacity-40">−</button>
        <button onClick={() => setZoomIdx(0)} disabled={zoomIdx === 0} title="Reset to auto range (all strikes)"
          className="rounded-[7px] bg-[var(--chip)] px-2 py-0.5 text-xs font-bold text-[var(--chip-text)] disabled:opacity-40">⟲</button>
      </div>
      <div className="text-xs text-slate-400 mb-1">
        {caption ?? (
          <>
            Payoff at expiry {pf.expiryDate}
            {(skipped > 0 || chartUnderlying) && (
              <span className="font-bold text-[var(--strong)]"> · {chartUnderlying} book
                {skipped > 0 ? ` (${skipped} other-underlying leg${skipped > 1 ? "s" : ""} not shown)` : ""}
              </span>
            )}{" "}
            <span className="text-slate-500">— green/red = P&L if held to expiry; dashed = current value;{" "}</span>
            <span className="font-bold" style={{ color: "var(--strong)" }}>▍spot</span>
            <span className="text-slate-500"> · strikes on the rail: </span>
            <span style={{ color: CE_COLOR }}>CE</span>
            <span className="text-slate-500"> · </span>
            <span style={{ color: PE_COLOR }}>PE</span>
            <span className="text-slate-500"> (filled = sell · outlined = buy)</span>
          </>
        )}
      </div>
      <ResponsiveContainer width="100%" height={240}>
        {/* top margin holds the "spot NNNNN" label above its reference line — at 5px the
            label was drawn into the caption and clipped to a dot (seen 2026-09-02) */}
        <ComposedChart data={data} margin={MARGIN}>
          <defs>
            <linearGradient id="livePayoff" x1="0" y1="0" x2="0" y2="1">
              <stop offset={off} stopColor="#10b981" stopOpacity={0.5} />
              <stop offset={off} stopColor="#f43f5e" stopOpacity={0.5} />
            </linearGradient>
          </defs>
          <XAxis
            dataKey="spot"
            type="number"
            domain={["dataMin", "dataMax"]}
            tick={{ fontSize: 11, fill: "#94a3b8" }}
            tickFormatter={(v) => Math.round(v).toString()}
          />
          <YAxis
            tick={{ fontSize: 11, fill: "#94a3b8" }}
            width={Y_AXIS_W}
            tickFormatter={(v) => `${(v / 1e3).toFixed(0)}k`}
          />
          <Tooltip
            contentStyle={{ background: "rgb(var(--slate-900))", border: "1px solid rgb(var(--slate-700))", color: "rgb(var(--slate-100))" }}
            formatter={(v: number, n: string) => [formatInr(v), n === "expiry" ? "At expiry" : n === "prev" ? (ghost?.label ?? "Before") : nowLabel]}
            labelFormatter={(v: number) => {
              const chg = chartSpot ? ((v - chartSpot) / chartSpot) * 100 : null;
              return chg == null ? `Spot ${Math.round(v)}` : `Spot ${Math.round(v)} (${chg >= 0 ? "+" : ""}${chg.toFixed(1)}% vs spot)`;
            }}
          />
          <ReferenceLine y={0} stroke="#475569" />
          {/* Mark each CE/PE strike: colour = right (violet CE / amber PE); the line style and the
              rail pill encode side — SELL = solid/filled, BUY = dashed/outlined. */}
          {strikes.filter((s) => inWindow(s.strike)).map((s) => {
            const color = s.right === "CE" ? CE_COLOR : PE_COLOR;
            const buy = s.dir === 1;
            return (
              <ReferenceLine
                key={`${s.strike}-${s.right}`}
                x={s.strike}
                stroke={color}
                strokeWidth={buy ? 1.25 : 1.5}
                strokeDasharray={buy ? "5 3" : undefined}
                label={<StrikePill text={`${buy ? "BUY" : "SELL"} ${s.strike} ${s.right}`} color={color} buy={buy}
                  row={s.row} clamp={[plotLeft, plotRight]} />}
              />
            );
          })}
          {chartSpot != null && (
            // Owner request: spot is THE reference — a thick solid high-contrast vertical
            // (var(--strong) = black on light, white on dark) so it never hides among strikes.
            <ReferenceLine x={chartSpot} stroke="var(--strong)" strokeWidth={3}
              label={{ value: `${spotLabel} ${Math.round(chartSpot)}`, fill: "var(--strong)", fontSize: 11, fontWeight: 800, position: "top" }} />
          )}
          <Area type="monotone" dataKey="expiry" stroke="#94a3b8" strokeWidth={1.5}
            fill="url(#livePayoff)" name="expiry" />
          {ghost && (
            <Line type="monotone" dataKey="prev" stroke={GHOST_COLOR} strokeWidth={1.5}
              strokeDasharray="2 4" dot={false} name="prev" />
          )}
          <Line type="monotone" dataKey="now" stroke="#60a5fa" strokeWidth={1.5}
            strokeDasharray="4 3" dot={false} name="now" />
          {/* Breakevens: a dashed stem + pill (BeLabel via the ReferenceLine) and a ringed dot on the
              zero line. Only those within the visible x-window are shown (zoom can crop them out). */}
          {bes.filter(inWindow).map((be) => (
            <ReferenceLine key={`be-${be}`} x={be} stroke="transparent"
              label={<BeLabel text={`BE ${Math.round(be).toLocaleString("en-IN")}`} />} />
          ))}
          {bes.filter(inWindow).map((be) => (
            <ReferenceDot key={`bed-${be}`} x={be} y={0} r={4} fill={BE_COLOR}
              stroke="var(--card)" strokeWidth={2} isFront />
          ))}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}
