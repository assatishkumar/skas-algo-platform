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
const ZOOMS: [number | null, string][] = [[null, "fit"], [0.03, "±3%"], [0.06, "±6%"],
                                           [0.1, "±10%"], [0.15, "±15%"]];
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
    // the backend's per-leg IV (a PERCENT there) as the fallback the library uses when its
    // own solve fails — never the library's flat 15%
    iv: l.iv != null ? l.iv / 100 : undefined,
    t: l.t ?? undefined,
  }));
}

export default function PayoffSvg({ legs, staged, spot, expiry, today, minHeight = 220,
  alerts = [], realised = 0, sigma = null, underlying = "" }: {
  legs: ConsoleLeg[];
  staged: ConsoleLeg[] | null;
  spot: number | null;
  expiry: string | null;
  today: string;
  minHeight?: number;
  // Armed levels, drawn amber at their own level: the chart's y IS the cycle's MTM.
  alerts?: ConsoleAlert[];
  // One standard deviation of the underlying to the drawn expiry (spot × ATM IV × √t), for
  // the ±1σ / ±2σ markers; undefined = no ATM IV at the cursor.
  sigma?: number | null;
  underlying?: string;
  // The cycle's realised P&L so far. Every curve carries it, so after a leg is closed at a
  // profit the whole payoff sits that much higher and its breakevens move — the chart reads
  // as "where this cycle ends up", the way StockMock draws it (owner, 2026-09-10).
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
  // Drag-to-zoom: press, drag across the spots you care about, release. A wheel zoom was
  // too sensitive to be useful (owner, 2026-09-09). Double-click, or "fit", resets.
  const [sel, setSel] = useState<[number, number] | null>(null);    // drag in progress (px)
  const selRef = useRef<[number, number] | null>(null);              // same, readable mid-tick
  const [custom, setCustom] = useState<[number, number] | null>(null);
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

  const metrics = useMemo(
    () => (spot && expiry && live.length ? computeMetrics(live, spot, expiry, today) : null),
    [live, spot, expiry, today]);
  // "fit" = the WHOLE structure: spot, every strike AND every breakeven, with room past
  // each so both zero-crossings and the loss beyond them are on screen, and spot never
  // pinned to an edge. A window of spot+strikes alone cut a short straddle off at its
  // peak and hid the left breakeven (owner, three times, 2026-09-09).
  const range = useMemo<[number, number] | null>(() => {
    if (!spot) return null;
    if (custom) return custom;
    if (zoom) return [spot * (1 - zoom), spot * (1 + zoom)];
    const bes = metrics?.breakevens ?? [];
    const refs = [spot, ...live.map((l) => l.strike), ...(ghost ?? []).map((l) => l.strike),
                  ...bes];
    let lo = Math.min(...refs), hi = Math.max(...refs);
    const pad = Math.max((hi - lo) * 0.15, spot * 0.03);
    lo -= pad; hi += pad;
    // spot at least 12% of the window from either edge
    const w0 = hi - lo;
    lo = Math.min(lo, spot - 0.12 * w0);
    hi = Math.max(hi, spot + 0.12 * w0);
    return [lo, hi];
  }, [spot, zoom, custom, live, ghost, metrics]);
  const data = useMemo(
    () => (spot && expiry && live.length && range
      ? buildLivePayoff(live, spot, expiry, today, null, undefined, { range, offset: realised }) : null),
    [live, spot, expiry, today, range, realised]);
  const ghostData = useMemo(
    () => (spot && expiry && ghost?.length && range
      ? buildLivePayoff(ghost, spot, expiry, today, null, undefined, { range, offset: realised }) : null),
    [ghost, spot, expiry, today, range, realised]);

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
    a.kind === "target" ? [{ a, y: a.value }]
    : a.kind === "stop" ? [{ a, y: -Math.abs(a.value) }] : []);
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
  // breakevens = where the DRAWN expiry line crosses zero (it carries the cycle's realised,
  // so they are the cycle's breakevens, not the open book's)
  const be: number[] = [];
  for (let i = 1; i < pts.length; i++) {
    const a = pts[i - 1], b = pts[i];
    if ((a.expiry < 0 && b.expiry >= 0) || (a.expiry >= 0 && b.expiry < 0)) {
      const f = a.expiry / (a.expiry - b.expiry);
      be.push(a.spot + f * (b.spot - a.spot));
    }
  }
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
        <span className="text-[9.5px] mr-1 self-center" style={{ color: "var(--oc-faint)" }}>
          drag to zoom · double-click resets
        </span>
        {custom && (
          <span className="h-[18px] px-1.5 rounded-[4px] text-[9.5px] font-semibold"
            style={{ background: "var(--oc-accent-dim)", color: "var(--oc-accent)" }}>
            {Math.round(custom[0]).toLocaleString("en-IN")}–{Math.round(custom[1]).toLocaleString("en-IN")}
          </span>
        )}
        {ZOOMS.map(([z, label]) => (
          <button key={label} type="button" onClick={() => { setZoom(z); setCustom(null); }}
            title={z ? `spot ${label}` : "the whole structure"}
            className="h-[18px] px-1.5 rounded-[4px] text-[9.5px] font-semibold"
            style={{ background: zoom === z && !custom ? "var(--oc-accent-dim)" : "var(--oc-chip)",
              color: zoom === z && !custom ? "var(--oc-accent)" : "var(--oc-muted)" }}>
            {label}
          </button>
        ))}
      </div>
      <svg width={w} height={height}
        style={{ display: "block", cursor: sel ? "col-resize" : "crosshair" }}
        onMouseLeave={() => { setHover(null); setSel(null); selRef.current = null; }}
        onMouseDown={(e) => {
          const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
          const x = Math.max(L, Math.min(w - R, e.clientX - r.left));
          selRef.current = [x, x]; setSel([x, x]);
        }}
        onMouseUp={() => {
          const cur = selRef.current;
          if (!cur) return;
          const [a, b] = [Math.min(...cur), Math.max(...cur)];
          selRef.current = null; setSel(null);
          if (b - a < 12) return;                       // a click, not a drag
          const toSpot = (x: number) => x0 + ((x - L) / (w - L - R)) * (x1 - x0);
          setCustom([toSpot(a), toSpot(b)]); setZoom(null);
        }}
        onDoubleClick={() => { setCustom(null); setZoom(null); }}
        onMouseMove={(e) => {
          const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
          const x = e.clientX - r.left;
          if (selRef.current) {
            selRef.current = [selRef.current[0], Math.max(L, Math.min(w - R, x))];
            setSel(selRef.current);
          }
          if (x < L || x > w - R) { setHover(null); return; }
          setHover(x0 + ((x - L) / (w - L - R)) * (x1 - x0));
        }}>
        {sel && Math.abs(sel[1] - sel[0]) > 2 && (
          <rect x={Math.min(...sel)} y={T} width={Math.abs(sel[1] - sel[0])} height={height - T - B}
            fill="var(--oc-accent-dim)" stroke="var(--oc-accent)" strokeWidth={1} />
        )}
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
        {/* ±1σ / ±2σ to the drawn expiry: faint verticals with a rotated label, the way
            the StockMock chart marks them (owner, 2026-09-10) */}
        {sigma ? [-2, -1, 1, 2].map((k) => {
          const v = spot + k * sigma;
          if (v < x0 || v > x1) return null;
          return (
            <g key={k}>
              <line x1={px(v)} x2={px(v)} y1={T} y2={height - B} stroke="var(--oc-line)"
                strokeWidth={1} strokeDasharray="2 3" />
              <text x={px(v) - 3} y={T + 8} fontSize={9} fill="var(--oc-muted)"
                transform={`rotate(-90 ${px(v) - 3} ${T + 8})`} textAnchor="end">
                {k > 0 ? `+${k}σ` : `${MINUS}${-k}σ`}
              </text>
            </g>
          );
        }) : null}
        <line x1={px(spot)} x2={px(spot)} y1={T} y2={height - B} stroke="var(--oc-accent)"
          strokeWidth={1.4} />
        <text x={px(spot) + 5} y={T + 11} fontSize={11} fontWeight={700} fill="var(--oc-accent)">
          {underlying ? `${underlying} spot ` : "spot "}{Math.round(spot).toLocaleString("en-IN")}
        </text>

        {metrics && Number.isFinite(metrics.maxProfit) && (
          <text x={w - R + 4} y={py(metrics.maxProfit + realised) + 3} fontSize={9.5} fill="var(--oc-pos)">
            {inr(metrics.maxProfit + realised)}
          </text>
        )}
        {metrics && Number.isFinite(metrics.maxLoss) && (
          <text x={w - R + 4} y={py(metrics.maxLoss + realised) + 3} fontSize={9.5} fill="var(--oc-neg)">
            {inr(metrics.maxLoss + realised)}
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
