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

export default function PayoffSvg({ legs, staged, spot, expiry, today, height = 240 }: {
  legs: ConsoleLeg[];
  staged: ConsoleLeg[] | null;
  spot: number | null;
  expiry: string | null;
  today: string;
  height?: number;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(462);
  useEffect(() => {
    if (!box.current) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(320, e.contentRect.width)));
    ro.observe(box.current);
    return () => ro.disconnect();
  }, []);

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
      <div ref={box} className="flex items-center justify-center text-[12px]"
        style={{ height, color: "var(--oc-faint)" }}>
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

  return (
    <div ref={box} style={{ width: "100%" }}>
      <svg width={w} height={height} style={{ display: "block" }}>
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
        {[0, 0.5, 1].map((f) => {
          const v = x0 + (x1 - x0) * f;
          return <text key={f} x={px(v)} y={height - 6} fontSize={9} textAnchor="middle"
            fill="var(--oc-faint)">{Math.round(v).toLocaleString("en-IN")}</text>;
        })}
      </svg>
    </div>
  );
}
