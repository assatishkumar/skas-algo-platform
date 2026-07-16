/** Tiny dependency-free SVG chart primitives (sparklines, the history panels, payoff).
 * The design's charts are simple stroked paths — recharts would triple the bundle. */

export function linePath(values: (number | null)[], w: number, h: number, pad = 2): string {
  const pts = values.map((v, i) => [i, v] as const).filter((p) => p[1] != null) as
    [number, number][];
  if (pts.length < 2) return "";
  const xs = pts.map((p) => p[0]);
  const ys = pts.map((p) => p[1]);
  const x0 = Math.min(...xs);
  const x1 = Math.max(...xs);
  const y0 = Math.min(...ys);
  const y1 = Math.max(...ys);
  const sx = (x: number) => pad + ((x - x0) / Math.max(1e-9, x1 - x0)) * (w - 2 * pad);
  const sy = (y: number) =>
    h - pad - ((y - y0) / Math.max(1e-9, y1 - y0)) * (h - 2 * pad);
  return pts.map((p, i) => `${i === 0 ? "M" : "L"}${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`)
    .join(" ");
}

/** 84×26 strategy-card sparkline, stroke = sign colour (README §02). */
export function Sparkline({ values, up }: { values: number[]; up: boolean }) {
  const d = linePath(values, 84, 26);
  return (
    <svg width="84" height="26" viewBox="0 0 84 26">
      {d ? (
        <path d={d} fill="none" stroke={up ? "var(--pos)" : "var(--danger)"}
          strokeWidth="1.8" strokeLinecap="round" />
      ) : (
        <line x1="4" y1="13" x2="80" y2="13" stroke="var(--divider)" strokeWidth="1.8" />
      )}
    </svg>
  );
}

/** One panel of the history card: label row with a live value + a single stroked series
 * (optionally soft-filled), sharing the card's width. */
export function HistoryPanel({
  label, value, values, color, height = 74, zeroLine = false, fill,
}: {
  label: string; value: string; values: (number | null)[]; color: string;
  height?: number; zeroLine?: boolean; fill?: string;
}) {
  const w = 320;
  const d = linePath(values, w, height, 4);
  const nums = values.filter((v): v is number => v != null);
  const zeroY = (() => {
    if (!zeroLine || !nums.length) return null;
    const y0 = Math.min(...nums);
    const y1 = Math.max(...nums);
    if (y0 > 0 || y1 < 0) return null;
    return height - 4 - ((0 - y0) / Math.max(1e-9, y1 - y0)) * (height - 8);
  })();
  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <span className="label">{label}</span>
        <span className="sg" style={{ fontWeight: 700, fontSize: 13.5, color }}>{value}</span>
      </div>
      <svg width="100%" height={height} viewBox={`0 0 ${w} ${height}`} preserveAspectRatio="none">
        {fill && d && (
          <path d={`${d} L${w - 4},${height - 4} L4,${height - 4} Z`} fill={fill} opacity="0.35" />
        )}
        {zeroY != null && (
          <line x1="4" y1={zeroY} x2={w - 4} y2={zeroY} stroke="var(--divider)"
            strokeDasharray="3 3" />
        )}
        {d && <path d={d} fill="none" stroke={color} strokeWidth="1.7" strokeLinecap="round" />}
      </svg>
    </div>
  );
}
