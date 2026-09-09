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
import type { ConsoleLeg } from "../../types";

const MINUS = "−";
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

export default function PayoffSvg({ legs, staged, spot, expiry, today, minHeight = 220 }: {
  legs: ConsoleLeg[];
  staged: ConsoleLeg[] | null;
  spot: number | null;
  expiry: string | null;
  today: string;
  minHeight?: number;
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
  useEffect(() => {
    if (!box.current) return;
    const ro = new ResizeObserver(([e]) => {
      setW(Math.max(320, e.contentRect.width));
      setH(Math.max(minHeight, e.contentRect.height));
    });
    ro.observe(box.current);
    return () => ro.disconnect();
  }, [minHeight]);
  const height = h;

  const live = useMemo(() => toPayoffLegs(legs), [legs]);
  const ghost = useMemo(() => (staged ? toPayoffLegs(staged) : null), [staged]);

  const data = useMemo(
    () => (spot && expiry && live.length ? buildLivePayoff(live, spot, expiry, today) : null),
    [live, spot, expiry, today]);
  const ghostData = useMemo(
    () => (spot && expiry && ghost?.length ? buildLivePayoff(ghost, spot, expiry, today) : null),
    [ghost, spot, expiry, today]);
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
  const ys = [...pts.map((p) => p.expiry), ...pts.map((p) => p.now),
              ...gpts.map((p) => p.expiry)];
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const yLo = Math.min(0, ...ys) * 1.08, yHi = Math.max(0, ...ys) * 1.08;
  const L = 8, R = 62, T = 12, B = 20;
  const px = (v: number) => L + ((v - x0) / (x1 - x0 || 1)) * (w - L - R);
  const py = (v: number) => T + (1 - (v - yLo) / ((yHi - yLo) || 1)) * (height - T - B);

  const line = (get: (p: { spot: number; expiry: number; now: number }) => number,
                src = pts) => src.map((p, i) => `${i ? "L" : "M"}${px(p.spot)},${py(get(p))}`).join(" ");
  const be = metrics?.breakevens ?? [];
  // The expiry line is drawn as two paths so the colour can change at the breakeven — the
  // handoff's "green up to breakeven, red after" is the fastest read on the whole chart.
  const split = be.length ? be[0] : null;
  const upTo = split == null ? pts : pts.filter((p) => p.spot <= split);
  const after = split == null ? [] : pts.filter((p) => p.spot >= split);
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

        {/* expiry payoff — green to the breakeven, red past it */}
        <path d={line((p) => p.expiry, upTo)} fill="none" stroke="var(--oc-pos)" strokeWidth={1.6} />
        {after.length > 1 && (
          <path d={line((p) => p.expiry, after)} fill="none" stroke="var(--oc-neg)" strokeWidth={1.6} />
        )}
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
          return <text key={f} x={px(v)} y={height - 6} fontSize={9} textAnchor="middle"
            fill="var(--oc-faint)">{Math.round(v).toLocaleString("en-IN")}</text>;
        })}
      </svg>
      {at && (
        <div className="absolute pointer-events-none rounded-[8px] px-2.5 py-1.5 text-[11px]"
          style={{
            left: Math.min(Math.max(px(at.spot) + 10, 4), Math.max(4, w - 190)),
            top: 8, background: "var(--oc-ink)", color: "var(--oc-surface)", minWidth: 168,
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
