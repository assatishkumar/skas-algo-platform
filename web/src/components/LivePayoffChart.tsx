import { useMemo, type ReactNode } from "react";
import {
  Area,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { formatInr } from "../lib/format";
import { buildLivePayoff, type LiveLeg } from "../lib/payoff";
import type { LivePosition } from "../types";

const CE_COLOR = "#8b5cf6"; // violet — CE strikes
const PE_COLOR = "#f59e0b"; // amber — PE strikes

/** Vertical strike label rendered along its reference line, reading bottom→top so several close
 *  strikes stay legible without overlapping. A light halo keeps it readable over the P&L fill. */
function StrikeLabel({ viewBox, text, color }: { viewBox?: { x: number; y: number; height: number }; text: string; color: string }) {
  if (!viewBox) return null;
  const px = viewBox.x + 3;
  const py = viewBox.y + viewBox.height - 6;
  return (
    <text
      x={px}
      y={py}
      fill={color}
      fontSize={10}
      fontWeight={700}
      textAnchor="start"
      transform={`rotate(-90 ${px} ${py})`}
      style={{ paintOrder: "stroke", stroke: "rgba(255,255,255,0.7)", strokeWidth: 2.5 }}
    >
      {text}
    </text>
  );
}

/** Sensibull-style payoff for the OPEN option legs of a live deployment: the expiry tent
 *  (green/red split at zero) + a current-value (T+0) curve, with the live spot marked.
 *  Built client-side from the position legs + live LTPs. */
export default function LivePayoffChart({
  positions,
  spot,
  asOf,
  caption,
  spotLabel = "spot",
}: {
  positions: LivePosition[];
  spot: number | null | undefined;
  asOf?: string; // value-curve as-of date (defaults to today — i.e. the live "now" curve)
  caption?: ReactNode; // overrides the default "Payoff at expiry …" caption
  spotLabel?: string; // label on the vertical spot line
}) {
  const pf = useMemo(() => {
    const legs: LiveLeg[] = [];
    let expiry = "";
    for (const p of positions) {
      const parts = p.symbol.split("|"); // UNDERLYING|EXPIRY|STRIKE|RIGHT
      if (parts.length !== 4) continue;
      expiry = parts[1];
      legs.push({
        strike: Number(parts[2]),
        right: parts[3],
        direction: p.direction ?? 1,
        units: p.units,
        entry: p.avg_price,
        ltp: p.ltp,
      });
    }
    if (!legs.length || !spot || !expiry) return null;
    return buildLivePayoff(legs, spot, expiry, asOf);
  }, [positions, spot, asOf]);

  // Unique CE/PE strikes to mark on the chart (a strangle → one CE + one PE line; dedup exact dupes).
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
    return [...m.values()].sort((a, b) => a.strike - b.strike);
  }, [positions]);

  if (!pf) return null;
  // Split the fill green/red at P&L = 0. The gradient maps to the EXPIRY area's own bounding box,
  // so the offset must come from the expiry series alone (not the dashed "now" line) — otherwise an
  // all-profit position gets painted red.
  const exp = pf.data.map((d) => d.expiry);
  const eMax = Math.max(...exp);
  const eMin = Math.min(...exp);
  const off = eMax <= 0 ? 0 : eMin >= 0 ? 1 : eMax / (eMax - eMin);

  return (
    <div className="mt-3">
      <div className="text-xs text-slate-400 mb-1">
        {caption ?? (
          <>
            Payoff at expiry {pf.expiryDate}{" "}
            <span className="text-slate-500">— green/red = P&L if held to expiry; dashed = current value;{" "}</span>
            <span className="font-bold" style={{ color: "var(--strong)" }}>▍spot</span>
            <span className="text-slate-500"> · </span>
            <span style={{ color: CE_COLOR }}>▮ CE strike</span>
            <span className="text-slate-500"> · </span>
            <span style={{ color: PE_COLOR }}>▮ PE strike</span>
            <span className="text-slate-500"> (solid = sell · dashed = buy)</span>
          </>
        )}
      </div>
      <ResponsiveContainer width="100%" height={240}>
        <ComposedChart data={pf.data} margin={{ top: 5, right: 12, bottom: 0, left: 12 }}>
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
            width={64}
            tickFormatter={(v) => `${(v / 1e3).toFixed(0)}k`}
          />
          <Tooltip
            contentStyle={{ background: "rgb(var(--slate-900))", border: "1px solid rgb(var(--slate-700))", color: "rgb(var(--slate-100))" }}
            formatter={(v: number, n: string) => [formatInr(v), n === "expiry" ? "At expiry" : "Current"]}
            labelFormatter={(v: number) => {
              const chg = spot ? ((v - spot) / spot) * 100 : null;
              return chg == null ? `Spot ${Math.round(v)}` : `Spot ${Math.round(v)} (${chg >= 0 ? "+" : ""}${chg.toFixed(1)}% vs spot)`;
            }}
          />
          <ReferenceLine y={0} stroke="#475569" />
          {/* Mark each CE/PE strike: colour = right (violet CE / amber PE), and the line style +
              label word encode side — SELL = solid, BUY = dashed — so the book reads at a glance. */}
          {strikes.map((s) => {
            const color = s.right === "CE" ? CE_COLOR : PE_COLOR;
            const buy = s.dir === 1;
            return (
              <ReferenceLine
                key={`${s.strike}-${s.right}`}
                x={s.strike}
                stroke={color}
                strokeWidth={buy ? 1.25 : 1.5}
                strokeDasharray={buy ? "5 3" : undefined}
                label={<StrikeLabel text={`${buy ? "BUY" : "SELL"} ${s.strike} ${s.right}`} color={color} />}
              />
            );
          })}
          {spot != null && (
            // Owner request: spot is THE reference — a thick solid high-contrast vertical
            // (var(--strong) = black on light, white on dark) so it never hides among strikes.
            <ReferenceLine x={spot} stroke="var(--strong)" strokeWidth={3}
              label={{ value: `${spotLabel} ${Math.round(spot)}`, fill: "var(--strong)", fontSize: 11, fontWeight: 800, position: "top" }} />
          )}
          <Area type="monotone" dataKey="expiry" stroke="#94a3b8" strokeWidth={1.5}
            fill="url(#livePayoff)" name="expiry" />
          <Line type="monotone" dataKey="now" stroke="#60a5fa" strokeWidth={1.5}
            strokeDasharray="4 3" dot={false} name="now" />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}
