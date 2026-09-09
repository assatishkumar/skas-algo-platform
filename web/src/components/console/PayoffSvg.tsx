/** The console's payoff chart (design handoff, D1).
 *
 *  Written as plain SVG rather than extending `LivePayoffChart`. That component is recharts
 *  with a fixed height, a bounding-box gradient and a zoom ladder, and it is live on the
 *  Live page and Cycle Detail — none of which the handoff asks for, and all of which would
 *  have to be fought. What it asks for is: fills split at the BREAKEVEN (not a gradient), a
 *  green-then-red expiry line, a dashed T+0 curve, a dotted staged curve, a spot marker
 *  carrying its own label, and a chart that re-renders at whatever width the column has.
 *  That is a few dozen path elements over `buildLivePayoff`'s points.
 *
 *  The maths is NOT here. Curves and metrics come from lib/payoff.ts — the same functions
 *  the Live page and the backtest reports use — so the console cannot quietly disagree with
 *  the rest of the platform about what a position is worth.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { buildLivePayoff, computeMetrics, type LiveLeg } from "../../lib/payoff";
import type { ConsoleAlert, ConsoleLeg } from "../../types";

const MINUS = "−";
const ZOOM_ROW = 20;   // the zoom-chip row above the plot, taken out of the measured height
const ZOOMS: [number | null, string][] = [[null, "fit"], [0.02, "±2%"], [0.05, "±5%"], [0.1, "±10%"]];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
/** "2026-04-28" → "28 Apr". A bare "28" in a tooltip reads as a quantity. */
const prettyExpiry = (iso: string | null) =>
  !iso ? "" : `${iso.slice(8, 10)} ${MONTHS[Number(iso.slice(5, 7)) - 1] ?? ""}`;
const inr = (v: number, dp = 0) =>
  (v < 0 ? MINUS : "+") + "₹" + Math.abs(v).toLocaleString("en-IN",
    { minimumFractionDigits: dp, maximumFractionDigits: dp });

/** ConsoleLeg → the payoff library's leg. One adapter, so the shapes stay independent. */
export function toPayoffLegs(legs: ConsoleLeg[]): LiveLeg[] {
  return legs.filter((l) => l.enabled).map((l) => ({
    strike: l.strike, right: l.right, direction: l.direction, units: l.units,
    entry: l.entry, ltp: l.ltp, expiry: l.expiry,
  }));
}

export default function PayoffSvg({ legs, staged, spot, expiry, today, minHeight = 220,
  alerts = [], realised = 0 }: {
  legs: ConsoleLeg[];
  staged: ConsoleLeg[] | null;
  spot: number | null;
  expiry: string | null;
  today: string;
  minHeight?: number;
  // Armed levels, drawn amber. A target/stop is a level of TOTAL MTM, and the chart's y is
  // the open book's P&L, so the line sits at (level − realised): the chart shows where the
  // book has to get to, not where the number is.
  alerts?: ConsoleAlert[];
  realised?: number;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(462);
  // The chart fills whatever the column gives it, in BOTH directions. A fixed height left
  // the payoff panel shorter than the risk rail beside it, so the two columns did not line
  // up at the bottom (owner, 2026-09-09).
  const [h, setH] = useState(minHeight);
  // The spot the pointer is over. A payoff chart is read by asking "and if it closes
  // THERE?", which is a question the picture can only answer with a number attached.
  const [hover, setHover] = useState<number | null>(null);
  // The x-window. "fit" spans spot and every strike, padded a little — NOT the breakevens:
  // a deep-ITM short's breakeven sits thousands of points away and the library's auto
  // range chased it out to 2,508 on a 24,580 spot, flattening the whole tent (owner,
  // 2026-09-09). The percent chips are spot-centred.
  const [zoom, setZoom] = useState<number | null>(null);
  useEffect(() => {
    if (!box.current) return;
    const ro = new ResizeObserver(([e]) => {
      setW(Math.max(320, e.contentRect.width));
      setH(Math.max(minHeight, e.contentRect.height - ZOOM_ROW));
    });
    ro.observe(box.current);
    return () => ro.disconnect();
  }, [minHeight]);
  const height = h;

  const live = useMemo(() => toPayoffLegs(legs), [legs]);
  const ghost = useMemo(() => (staged ? toPayoffLegs(staged) : null), [staged]);

  const range = useMemo<[number, number] | null>(() => {
    if (!spot) return null;
    if (zoom) return [spot * (1 - zoom), spot * (1 + zoom)];
    const refs = [spot, ...live.map((l) => l.strike), ...(ghost ?? []).map((l) => l.strike)];
    const lo = Math.min(...refs), hi = Math.max(...refs);
    const pad = Math.max((hi - lo) * 0.25, spot * 0.02);
    return [lo - pad, hi + pad];
  }, [spot, zoom, live, ghost]);
  const data = useMemo(
    () => (spot && expiry && live.length && range
      ? buildLivePayoff(live, spot, expiry, today, null, undefined, { range }) : null),
    [live, spot, expiry, today, range]);
  const ghostData = useMemo(
    () => (spot && expiry && ghost?.length && range
      ? buildLivePayoff(ghost, spot, expiry, today, null, undefined, { range }) : null),
    [ghost, spot, expiry, today, range]);
  const metrics = useMemo(
    () => (spot && expiry && live.length ? computeMetrics(live, spot, expiry, today) : null),
    [live, spot, expiry, today]);

  if (!spot || !expiry || (!data && !ghostData)) {
    return (
      <div ref={box} className="flex items-center justify-center text-[12px] h-full"
        style={{ minHeight, color: "var(--oc-faint)" }}>
        Buy or sell a strike to see its payoff.
      </div>
    );
  }

  const pts = (data ?? ghostData)!.data;
  const gpts = ghostData?.data ?? [];
  const xs = pts.map((p) => p.spot);
  const hLines = alerts.flatMap((a) =>
    a.kind === "target" ? [{ a, y: a.value - realised }]
    : a.kind === "stop" ? [{ a, y: -Math.abs(a.value) - realised }] : []);
  const vLines = alerts.filter((a) => a.kind === "above" || a.kind === "below");
  const ys = [...pts.map((p) => p.expiry), ...pts.map((p) => p.now),
              ...gpts.map((p) => p.expiry), ...hLines.map((h) => h.y)];
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const yLo = Math.min(0, ...ys) * 1.08, yHi = Math.max(0, ...ys) * 1.08;
  const L = 8, R = 62, T = 12, B = 20;
  const px = (v: number) => L + ((v - x0) / (x1 - x0 || 1)) * (w - L - R);
  const py = (v: number) => T + (1 - (v - yLo) / ((yHi - yLo) || 1)) * (height - T - B);

  const line = (get: (p: { spot: number; expiry: number; now: number }) => number,
                src = pts) => src.map((p, i) => `${i ? "L" : "M"}${px(p.spot)},${py(get(p))}`).join(" ");
  const be = (metrics?.breakevens ?? []).filter((b) => b >= x0 && b <= x1);
  // The expiry line is drawn as one path per SIGN RUN — green where the structure makes
  // money at expiry, red where it loses — so a straddle reads green between its two
  // breakevens and a spread green up to its one. (A split at the first breakeven alone
  // coloured a straddle's whole right half red.) Each run includes its neighbour so the
  // colour changes at the crossing, not a sample early.
  const runs: { pos: boolean; pts: typeof pts }[] = [];
  pts.forEach((p, i) => {
    const pos = p.expiry >= 0;
    const last = runs[runs.length - 1];
    if (!last || last.pos !== pos) runs.push({ pos, pts: i ? [pts[i - 1], p] : [p] });
    else last.pts.push(p);
  });
  const zero = py(0);
  const area = (src: typeof pts, sign: 1 | -1) => {
    const seg = src.filter((p) => (sign > 0 ? p.expiry >= 0 : p.expiry <= 0));
    if (seg.length < 2) return "";
    return `M${px(seg[0].spot)},${zero} ` +
      seg.map((p) => `L${px(p.spot)},${py(p.expiry)}`).join(" ") +
      ` L${px(seg[seg.length - 1].spot)},${zero} Z`;
  };

  // nearest computed point to the pointer — the curve is sampled, so snap rather than
  // interpolate, and the tooltip then quotes a value the chart actually drew.
  const at = hover == null ? null
    : pts.reduce((best, p) =>
        Math.abs(p.spot - hover) < Math.abs(best.spot - hover) ? p : best, pts[0]);
  const atGhost = hover == null || !gpts.length ? null
    : gpts.reduce((best, p) =>
        Math.abs(p.spot - hover) < Math.abs(best.spot - hover) ? p : best, gpts[0]);

  return (
    <div ref={box} className="h-full" style={{ width: "100%", position: "relative",
      minHeight }}>
      <div className="flex justify-end gap-1" style={{ height: ZOOM_ROW }}>
        {ZOOMS.map(([z, label]) => (
          <button key={label} type="button" onClick={() => setZoom(z)}
            title={z ? `spot ${label}` : "span spot and every strike"}
            className="h-[18px] px-1.5 rounded-[4px] text-[9.5px] font-semibold"
            style={{ background: zoom === z ? "var(--oc-accent-dim)" : "var(--oc-chip)",
              color: zoom === z ? "var(--oc-accent)" : "var(--oc-muted)" }}>
            {label}
          </button>
        ))}
      </div>
      <svg width={w} height={height} style={{ display: "block" }}
        onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
          const x = e.clientX - r.left;
          if (x < L || x > w - R) { setHover(null); return; }
          setHover(x0 + ((x - L) / (w - L - R)) * (x1 - x0));
        }}>
        {[0.25, 0.5, 0.75].map((f) => {
          const v = x0 + (x1 - x0) * f;
          return <line key={f} x1={px(v)} x2={px(v)} y1={T} y2={height - B}
            stroke="var(--oc-hair)" strokeWidth={1} />;
        })}
        <path d={area(pts, 1)} fill="var(--oc-pos-fill)" />
        <path d={area(pts, -1)} fill="var(--oc-neg-fill)" />
        <line x1={L} x2={w - R} y1={zero} y2={zero} stroke="var(--oc-muted)" strokeWidth={1} />

        {/* expiry payoff — green where it pays, red where it loses */}
        {runs.filter((r) => r.pts.length > 1).map((r, i) => (
          <path key={i} d={line((p) => p.expiry, r.pts)} fill="none"
            stroke={r.pos ? "var(--oc-pos)" : "var(--oc-neg)"} strokeWidth={1.6} />
        ))}
        {/* T+0: what the book is worth NOW across spot */}
        {data && (
          <path d={line((p) => p.now)} fill="none" stroke="var(--oc-accent)" strokeWidth={1.6}
            strokeDasharray="5 3" opacity={0.9} />
        )}
        {/* the staged book, dotted — the change previewed before it is real */}
        {ghostData && (
          <path d={line((p) => p.expiry, gpts)} fill="none" stroke="var(--oc-accent)"
            strokeWidth={1.6} strokeDasharray="2 3" opacity={0.65} />
        )}

        {be.map((b) => (
          <g key={b}>
            <circle cx={px(b)} cy={zero} r={3.5} fill="none" stroke="var(--oc-accent)" strokeWidth={1.4} />
            <text x={px(b)} y={zero - 7} textAnchor="middle" fontSize={9}
              fill="var(--oc-accent)">BE {Math.round(b).toLocaleString("en-IN")}</text>
          </g>
        ))}
        {/* alert levels — amber, dashed while armed, solid once fired */}
        {hLines.map(({ a, y }) => (
          <g key={a.id}>
            <line x1={L} x2={w - R} y1={py(y)} y2={py(y)} stroke="var(--oc-caution)"
              strokeWidth={1.2} strokeDasharray={a.state === "fired" ? undefined : "6 3"} />
            <text x={L + 3} y={py(y) - 3} fontSize={9} fill="var(--oc-caution)">
              {a.kind} {inr(a.kind === "stop" ? -Math.abs(a.value) : a.value)}
              {a.state === "fired" ? ` · fired ${a.fired_at?.slice(11) ?? ""}` : ""}
            </text>
          </g>
        ))}
        {vLines.filter((a) => a.value >= x0 && a.value <= x1).map((a) => (
          <g key={a.id}>
            <line x1={px(a.value)} x2={px(a.value)} y1={T} y2={height - B}
              stroke="var(--oc-caution)" strokeWidth={1.2}
              strokeDasharray={a.state === "fired" ? undefined : "6 3"} />
            <text x={px(a.value) + 3} y={height - B - 4} fontSize={9} fill="var(--oc-caution)">
              {a.kind} {Math.round(a.value).toLocaleString("en-IN")}
            </text>
          </g>
        ))}
        <line x1={px(spot)} x2={px(spot)} y1={T} y2={height - B} stroke="var(--oc-accent)"
          strokeWidth={1} strokeDasharray="3 3" />
        <text x={px(spot) + 4} y={T + 10} fontSize={9.5} fill="var(--oc-accent)">
          spot {Math.round(spot).toLocaleString("en-IN")}
        </text>

        {metrics && Number.isFinite(metrics.maxProfit) && (
          <text x={w - R + 4} y={py(metrics.maxProfit) + 3} fontSize={9.5} fill="var(--oc-pos)">
            {inr(metrics.maxProfit)}
          </text>
        )}
        {metrics && Number.isFinite(metrics.maxLoss) && (
          <text x={w - R + 4} y={py(metrics.maxLoss) + 3} fontSize={9.5} fill="var(--oc-neg)">
            {inr(metrics.maxLoss)}
          </text>
        )}
        {at && (
          <>
            <line x1={px(at.spot)} x2={px(at.spot)} y1={T} y2={height - B}
              stroke="var(--oc-muted)" strokeWidth={1} strokeDasharray="2 2" />
            <circle cx={px(at.spot)} cy={py(at.expiry)} r={3} fill="var(--oc-pos)" />
            <circle cx={px(at.spot)} cy={py(at.now)} r={3} fill="var(--oc-accent)" />
          </>
        )}
        {[0, 0.5, 1].map((f) => {
          const v = x0 + (x1 - x0) * f;
          return <text key={f} x={px(v)} y={height - 6} fontSize={9}
            textAnchor={f === 0 ? "start" : f === 1 ? "end" : "middle"}
            fill="var(--oc-faint)">{Math.round(v).toLocaleString("en-IN")}</text>;
        })}
      </svg>
      {at && (
        <div className="absolute pointer-events-none rounded-[8px] px-2.5 py-1.5 text-[11px]"
          style={{
            left: Math.min(Math.max(px(at.spot) + 10, 4), Math.max(4, w - 190)),
            top: ZOOM_ROW + 6, background: "var(--oc-ink)", color: "var(--oc-surface)", minWidth: 168,
          }}>
          <div className="font-semibold">
            If spot is {Math.round(at.spot).toLocaleString("en-IN")}
            {spot ? (
              <span style={{ opacity: 0.65 }}>
                {" "}({at.spot >= spot ? "+" : MINUS}
                {Math.abs(100 * (at.spot / spot - 1)).toFixed(2)}%)
              </span>
            ) : null}
          </div>
          <div className="mt-1" style={{ opacity: 0.75 }}>P&amp;L</div>
          <Row label={`at expiry ${prettyExpiry(expiry)}`} v={at.expiry} />
          <Row label="now (T+0)" v={at.now} />
          {atGhost && <Row label="staged, at expiry" v={atGhost.expiry} />}
        </div>
      )}
    </div>
  );
}

function Row({ label, v }: { label: string; v: number }) {
  return (
    <div className="flex justify-between gap-4 tabular-nums">
      <span style={{ opacity: 0.7 }}>{label}</span>
      <b style={{ color: v >= 0 ? "var(--oc-pos)" : "var(--oc-neg)" }}>{inr(v)}</b>
    </div>
  );
}
