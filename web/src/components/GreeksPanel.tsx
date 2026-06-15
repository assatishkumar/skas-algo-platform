import { useQuery } from "@tanstack/react-query";
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
import { api } from "../api/client";
import { formatInr } from "../lib/format";
import type { LiveRunSnapshot } from "../types";

const timeFmt = (v: number) =>
  new Date(v).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" });

/** Net greeks (delta + IV) for an options deployment — the current values from the live
 *  snapshot plus a sampled history chart. Greeks are derived from live Zerodha quotes
 *  (LTP + index spot + DTE), persisted ~1/min server-side. */
export default function GreeksPanel({ run }: { run: LiveRunSnapshot }) {
  const { data } = useQuery({
    queryKey: ["greeks-history", run.run_id],
    queryFn: () => api.liveGreeksHistory(run.run_id),
    refetchInterval: 30000,
  });
  const points = (data?.points ?? []).map((p) => ({
    t: p.ts ? new Date(p.ts).getTime() : 0,
    delta: p.net_delta,
    iv: p.net_iv != null ? p.net_iv * 100 : null,
    pnl: p.pnl,
  }));
  const netDelta = run.net_delta;
  const netIv = run.net_iv;
  const livePnl = (run.positions ?? []).reduce((s, p) => s + p.unrealized_pnl, 0);
  // Split the P&L area green-above-zero / red-below at y=0.
  const pnls = points.map((p) => p.pnl ?? 0);
  const pMax = pnls.length ? Math.max(...pnls, 0) : 0;
  const pMin = pnls.length ? Math.min(...pnls, 0) : 0;
  const zeroOff = pMax <= 0 ? 0 : pMin >= 0 ? 1 : pMax / (pMax - pMin);

  return (
    <div className="mt-3 rounded-md border border-slate-800 bg-slate-900/40 p-3">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-sm">
        <div>
          <span className="text-slate-400 text-xs mr-2">Net Δ</span>
          <span
            className={`font-medium tabular-nums ${(netDelta ?? 0) >= 0 ? "text-emerald-400" : "text-rose-400"}`}
          >
            {netDelta != null ? netDelta.toFixed(1) : "—"}
          </span>
        </div>
        <div>
          <span className="text-slate-400 text-xs mr-2">IV</span>
          <span className="font-medium tabular-nums">
            {netIv != null ? `${(netIv * 100).toFixed(1)}%` : "—"}
          </span>
        </div>
        <div>
          <span className="text-slate-400 text-xs mr-2">P&L</span>
          <span className={`font-medium tabular-nums ${livePnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
            {formatInr(livePnl)}
          </span>
        </div>
        <span className="text-xs text-slate-500">greeks from live quotes · sampled ~1/min</span>
      </div>
      {points.length > 1 ? (
        <div className="mt-2 space-y-2">
          {/* P&L over time */}
          <ResponsiveContainer width="100%" height={120}>
            <ComposedChart data={points} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
              <defs>
                <linearGradient id={`pnlSplit-${run.run_id}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset={zeroOff} stopColor="#10b981" stopOpacity={0.5} />
                  <stop offset={zeroOff} stopColor="#f43f5e" stopOpacity={0.5} />
                </linearGradient>
              </defs>
              <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} scale="time"
                tick={{ fontSize: 10, fill: "#94a3b8" }} tickFormatter={timeFmt} />
              <YAxis tick={{ fontSize: 10, fill: "#94a3b8" }} width={52}
                tickFormatter={(v) => `${(v / 1000).toFixed(0)}k`} />
              <ReferenceLine y={0} stroke="#475569" />
              <Tooltip contentStyle={{ background: "#0f172a", border: "1px solid #334155" }}
                labelFormatter={(v: number) => new Date(v).toLocaleString("en-IN")}
                formatter={(val: number) => [formatInr(val), "P&L"]} />
              <Area type="monotone" dataKey="pnl" stroke="#94a3b8" strokeWidth={1.5}
                fill={`url(#pnlSplit-${run.run_id})`} name="pnl" />
            </ComposedChart>
          </ResponsiveContainer>
          {/* Net Δ + IV over time */}
          <ResponsiveContainer width="100%" height={160}>
            <ComposedChart data={points} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
              <XAxis
                dataKey="t"
                type="number"
                domain={["dataMin", "dataMax"]}
                scale="time"
                tick={{ fontSize: 10, fill: "#94a3b8" }}
                tickFormatter={timeFmt}
              />
              <YAxis yAxisId="d" tick={{ fontSize: 10, fill: "#94a3b8" }} width={44} />
              <YAxis
                yAxisId="iv"
                orientation="right"
                tick={{ fontSize: 10, fill: "#94a3b8" }}
                width={40}
                tickFormatter={(v) => `${v.toFixed(0)}%`}
              />
              <ReferenceLine yAxisId="d" y={0} stroke="#475569" />
              <Tooltip
                contentStyle={{ background: "#0f172a", border: "1px solid #334155" }}
                labelFormatter={(v: number) => new Date(v).toLocaleString("en-IN")}
                formatter={(val: number, n: string) => [
                  n === "iv" ? `${val.toFixed(1)}%` : val.toFixed(1),
                  n === "iv" ? "IV" : "Net Δ",
                ]}
              />
              <Line yAxisId="d" type="monotone" dataKey="delta" stroke="#60a5fa" dot={false} strokeWidth={1.5} name="delta" />
              <Line yAxisId="iv" type="monotone" dataKey="iv" stroke="#f59e0b" dot={false} strokeWidth={1.5} name="iv" />
            </ComposedChart>
          </ResponsiveContainer>
          <div className="text-xs text-slate-500 mt-1">
            <span className="text-emerald-400">▮ P&L</span> ·{" "}
            <span className="text-sky-400">— Net Δ</span> ·{" "}
            <span className="text-amber-400">— IV %</span> over time
          </div>
        </div>
      ) : (
        <div className="text-xs text-slate-500 mt-2">Collecting greeks history…</div>
      )}
    </div>
  );
}
