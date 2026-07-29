/** Drill-in §6 — Risk & robustness (7.1–7.11): equity+underwater, drawdown episodes,
 *  streaks, concentration, rolling Sharpe, autocorrelation, correlations, margin
 *  utilization, bootstrap CIs, regime boundaries. 7.11 (greek attribution) is a dimmed
 *  card until model-based attribution is computed. */

import { useMemo } from "react";
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Line, LineChart, ReferenceLine,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import type { AnalyticsBundle } from "../../../types";
import { formatInr } from "../../../lib/format";
import Panel from "../Panel";

const TT = { background: "var(--card)", border: "1px solid var(--border)", borderRadius: 10, fontSize: 12 };
const AXIS = (extra = {}) => ({
  tick: { fontSize: 10, fill: "var(--faint)" }, tickLine: false,
  axisLine: { stroke: "var(--border)" }, ...extra,
});

function ddEpisodes(daily: AnalyticsBundle["daily"]) {
  const eps: { peak: string; depth: number; length: number; recovery: string }[] = [];
  let start: number | null = null;
  let depth = 0;
  for (let i = 0; i < daily.length; i++) {
    if (daily[i].dd < 0) {
      if (start == null) { start = i; depth = 0; }
      depth = Math.min(depth, daily[i].dd);
    } else if (start != null) {
      eps.push({ peak: daily[start].date, depth, length: i - start, recovery: daily[i].date });
      start = null;
    }
  }
  if (start != null) {
    eps.push({ peak: daily[start].date, depth, length: daily.length - start, recovery: "ongoing" });
  }
  return eps.sort((a, b) => a.depth - b.depth).slice(0, 5);
}

function bootstrap(daily: AnalyticsBundle["daily"], capital: number) {
  const pnl = daily.map((d) => d.pnl);
  if (pnl.length < 40) return null;
  const years = pnl.length / 252;
  const stat = (xs: number[]) => {
    const net = xs.reduce((s, v) => s + v, 0);
    const mean = net / xs.length;
    const sd = Math.sqrt(xs.reduce((s, v) => s + (v - mean) ** 2, 0) / xs.length) || 1;
    let peak = 0, cum = 0, dd = 0;
    for (const v of xs) { cum += v; peak = Math.max(peak, cum); dd = Math.min(dd, cum - peak); }
    return {
      cagr: capital > 0 ? (((capital + net) / capital) ** (1 / years) - 1) * 100 : 0,
      sharpe: (mean / sd) * Math.sqrt(252),
      maxdd: dd,
    };
  };
  const draws = Array.from({ length: 400 }, () => {
    const xs = Array.from({ length: pnl.length },
      () => pnl[Math.floor(Math.random() * pnl.length)]);
    return stat(xs);
  });
  const q = (arr: number[], p: number) => {
    const s = [...arr].sort((a, b) => a - b);
    return s[Math.min(s.length - 1, Math.floor(p * s.length))];
  };
  const point = stat(pnl);
  const row = (label: string, key: "cagr" | "sharpe" | "maxdd", fmt: (v: number) => string) => ({
    label, point: fmt(point[key]),
    lo: fmt(q(draws.map((d) => d[key]), 0.05)),
    hi: fmt(q(draws.map((d) => d[key]), 0.95)),
  });
  return [
    row("CAGR", "cagr", (v) => `${v.toFixed(1)}%`),
    row("Sharpe", "sharpe", (v) => v.toFixed(2)),
    row("Max drawdown", "maxdd", (v) => formatInr(Math.round(v))),
  ];
}

export default function RiskSection({ bundle }: { bundle: AnalyticsBundle }) {
  const daily = bundle.daily;
  const cum = useMemo(() => {
    let c = 0;
    return daily.map((d) => { c += d.pnl; return { date: d.date, pnl: c, dd: d.dd }; });
  }, [daily]);

  const streaks = useMemo(() => {
    const bins = new Map<number, number>();
    let run = 0;
    for (const t of bundle.trades) {
      if (t.pnl_rs <= 0) run += 1;
      else { if (run > 0) bins.set(run, (bins.get(run) ?? 0) + 1); run = 0; }
    }
    if (run > 0) bins.set(run, (bins.get(run) ?? 0) + 1);
    return [...bins.entries()].sort(([a], [b]) => a - b)
      .map(([len, n]) => ({ len: `${len}`, n }));
  }, [bundle.trades]);

  const conc = useMemo(() => {
    const pnls = bundle.trades.map((t) => t.pnl_rs);
    const wins = pnls.filter((p) => p > 0).sort((a, b) => b - a);
    const losses = pnls.filter((p) => p < 0).sort((a, b) => a - b);
    const sumW = wins.reduce((s, v) => s + v, 0) || 1;
    const sumL = losses.reduce((s, v) => s + v, 0) || -1;
    return {
      top5: (100 * wins.slice(0, 5).reduce((s, v) => s + v, 0)) / sumW,
      worst5: (100 * losses.slice(0, 5).reduce((s, v) => s + v, 0)) / sumL,
    };
  }, [bundle.trades]);

  const util = useMemo(() => daily
    .filter((d) => d.margin != null && bundle.capital > 0)
    .map((d) => ({ date: d.date, pct: +((100 * d.margin!) / bundle.capital).toFixed(1) })), [daily, bundle.capital]);

  const eps = useMemo(() => ddEpisodes(daily), [daily]);
  const boot = useMemo(() => bootstrap(daily, bundle.capital), [daily, bundle.capital]);
  const corr = bundle.series.correlations;
  const sig = bundle.series.sig_band;

  const grid = <CartesianGrid strokeDasharray="2 4" stroke="var(--divider)" vertical={false} />;
  return (
    <div className="grid gap-4" style={{ gridTemplateColumns: "repeat(auto-fill,minmax(390px,1fr))" }}>
      <Panel wide num="7.1" title="Equity + underwater" subtitle="cumulative P&L above · distance below peak beneath">
        <div className="h-[200px]"><ResponsiveContainer>
          <AreaChart data={cum}>{grid}
            <XAxis dataKey="date" {...AXIS({ minTickGap: 80 })} />
            <YAxis {...AXIS({ width: 72 })} tickFormatter={(v: number) => formatInr(v)} />
            <Tooltip contentStyle={TT} formatter={(v: number) => [formatInr(v), "cum P&L"]} />
            <Area dataKey="pnl" stroke="var(--accent)" fill="var(--accent)" fillOpacity={0.12}
              strokeWidth={2} isAnimationActive={false} />
          </AreaChart></ResponsiveContainer></div>
        <div className="h-[110px]"><ResponsiveContainer>
          <AreaChart data={cum}>{grid}
            <XAxis dataKey="date" hide />
            <YAxis {...AXIS({ width: 72 })} tickFormatter={(v: number) => formatInr(v)} />
            <Tooltip contentStyle={TT} formatter={(v: number) => [formatInr(v), "drawdown"]} />
            <Area dataKey="dd" stroke="var(--danger)" fill="var(--danger)" fillOpacity={0.18}
              strokeWidth={1.6} isAnimationActive={false} />
          </AreaChart></ResponsiveContainer></div>
      </Panel>

      <Panel wide num="7.2" title="Deepest drawdown episodes" subtitle="depth is pain, width is patience">
        <div className="grid gap-2 text-[10px] font-extrabold text-[var(--faint)] px-1 pb-1"
          style={{ gridTemplateColumns: "1fr 1fr 0.7fr 1fr" }}>
          <span>DEPTH</span><span>FROM PEAK</span><span className="text-right">DAYS</span><span className="text-right">RECOVERED</span>
        </div>
        {eps.map((e, i) => (
          <div key={i} className="grid gap-2 text-[12.5px] font-semibold tabular-nums px-1 py-1.5 border-b border-[var(--divider)]"
            style={{ gridTemplateColumns: "1fr 1fr 0.7fr 1fr" }}>
            <span className="text-[var(--danger)] font-bold">{formatInr(Math.round(e.depth))}</span>
            <span>{e.peak}</span>
            <span className="text-right">{e.length}</span>
            <span className="text-right" style={e.recovery === "ongoing" ? { color: "var(--warn-text)", fontWeight: 800 } : undefined}>
              {e.recovery}</span>
          </div>
        ))}
      </Panel>

      <Panel num="7.3" title="Losing streaks" subtitle="size capital and nerves for the right tail">
        <div className="h-[200px]"><ResponsiveContainer>
          <BarChart data={streaks}>{grid}
            <XAxis dataKey="len" {...AXIS()} />
            <YAxis {...AXIS({ width: 34 })} />
            <Tooltip contentStyle={TT} />
            <Bar dataKey="n" fill="var(--danger)" radius={[3, 3, 0, 0]} isAnimationActive={false} />
          </BarChart></ResponsiveContainer></div>
      </Panel>

      <Panel num="7.4" title="P&L concentration" subtitle="is the backtest really five lucky days?">
        <div className="grid grid-cols-2 gap-2">
          <div className="rounded-[10px] bg-[var(--stat)] px-3 py-3">
            <div className="text-[10px] text-[var(--faint)] font-extrabold">TOP 5 WINNERS</div>
            <div className="font-['Space_Grotesk'] font-bold text-[17px]">{conc.top5.toFixed(0)}%</div>
            <div className="text-[10.5px] text-[var(--muted)] font-semibold">of gross profit</div>
          </div>
          <div className="rounded-[10px] bg-[var(--stat)] px-3 py-3">
            <div className="text-[10px] text-[var(--faint)] font-extrabold">WORST 5 LOSERS</div>
            <div className="font-['Space_Grotesk'] font-bold text-[17px] text-[var(--danger)]">{conc.worst5.toFixed(0)}%</div>
            <div className="text-[10.5px] text-[var(--muted)] font-semibold">of gross loss</div>
          </div>
        </div>
      </Panel>

      <Panel num="7.5" title="Rolling 63-day Sharpe" subtitle="the first chart to check before scaling up">
        <div className="h-[200px]"><ResponsiveContainer>
          <LineChart data={bundle.series.rolling_sharpe}>{grid}
            <XAxis dataKey="date" {...AXIS({ minTickGap: 70 })} />
            <YAxis {...AXIS({ width: 36 })} />
            <ReferenceLine y={0} stroke="var(--faint)" strokeWidth={1.2} opacity={0.85} />
            <Tooltip contentStyle={TT} />
            <Line dataKey="v" stroke="var(--accent)" dot={false} strokeWidth={2} isAnimationActive={false} />
          </LineChart></ResponsiveContainer></div>
      </Panel>

      <Panel num="7.6" title="Daily-P&L autocorrelation" subtitle="bars past the band = today predicts tomorrow">
        <div className="h-[200px]"><ResponsiveContainer>
          <BarChart data={bundle.series.autocorr}>{grid}
            <XAxis dataKey="lag" {...AXIS()} />
            <YAxis {...AXIS({ width: 40 })} domain={[-0.3, 0.3]} />
            <ReferenceLine y={0} stroke="var(--faint)" strokeWidth={1.2} opacity={0.85} />
            {sig != null && <ReferenceLine y={sig} stroke="var(--warn-text)" strokeDasharray="4 4" />}
            {sig != null && <ReferenceLine y={-sig} stroke="var(--warn-text)" strokeDasharray="4 4" />}
            <Tooltip contentStyle={TT} />
            <Bar dataKey="v" fill="var(--indigo, #5b62e8)" radius={[2, 2, 0, 0]} isAnimationActive={false} />
          </BarChart></ResponsiveContainer></div>
      </Panel>

      <Panel num="7.7" title="Correlations" subtitle="a short-vol tilt hides in these bars">
        <div className="flex flex-col gap-3 py-2">
          {[["vs NIFTY daily return", corr.nifty], ["vs VIX daily change", corr.vix]].map(([label, v]) => (
            <div key={label as string}>
              <div className="flex justify-between text-[11px] font-bold text-[var(--muted)]">
                <span>{label}</span><span className="tabular-nums">{v != null ? (v as number).toFixed(2) : "—"}</span>
              </div>
              <div className="relative h-[12px] mt-1 rounded bg-[var(--stat)]">
                <div className="absolute top-0 bottom-0 w-px bg-[var(--faint)] opacity-85" style={{ left: "50%" }} />
                {v != null && (
                  <div className="absolute top-[2px] h-[8px] rounded"
                    style={{
                      left: (v as number) >= 0 ? "50%" : `${50 + (v as number) * 50}%`,
                      width: `${Math.abs(v as number) * 50}%`,
                      background: (v as number) >= 0 ? "var(--pos)" : "var(--danger)",
                    }} />
                )}
              </div>
            </div>
          ))}
        </div>
      </Panel>

      <Panel num="7.8" title="Margin utilization" subtitle="size the account off the peak, not the average">
        {util.length ? (
          <div className="h-[200px]"><ResponsiveContainer>
            <LineChart data={util}>{grid}
              <XAxis dataKey="date" {...AXIS({ minTickGap: 70 })} />
              <YAxis {...AXIS({ width: 40 })} tickFormatter={(v: number) => `${v}%`} />
              <ReferenceLine y={90} stroke="var(--danger)" strokeDasharray="5 4" />
              <Tooltip contentStyle={TT} formatter={(v: number) => [`${v}%`, "margin / capital"]} />
              <Line dataKey="pct" stroke="var(--pe, #d97706)" dot={false} strokeWidth={1.8} isAnimationActive={false} />
            </LineChart></ResponsiveContainer></div>
        ) : (
          <div className="text-[12px] text-[var(--muted)] font-semibold py-6 text-center">No margin series on this run.</div>
        )}
      </Panel>

      <Panel num="7.9" title="Bootstrap confidence" subtitle="400 resamples of the daily P&L — the run you got vs runs you could have gotten">
        {boot ? (
          <>
            <div className="grid gap-2 text-[10px] font-extrabold text-[var(--faint)] px-1 pb-1"
              style={{ gridTemplateColumns: "1.2fr 1fr 1fr 1fr" }}>
              <span>METRIC</span><span className="text-right">POINT</span>
              <span className="text-right">5%</span><span className="text-right">95%</span>
            </div>
            {boot.map((r) => (
              <div key={r.label} className="grid gap-2 text-[12.5px] font-semibold tabular-nums px-1 py-1.5 border-b border-[var(--divider)]"
                style={{ gridTemplateColumns: "1.2fr 1fr 1fr 1fr" }}>
                <span className="font-extrabold text-[var(--strong)]">{r.label}</span>
                <span className="text-right">{r.point}</span>
                <span className="text-right text-[var(--muted)]">{r.lo}</span>
                <span className="text-right text-[var(--muted)]">{r.hi}</span>
              </div>
            ))}
          </>
        ) : (
          <div className="text-[12px] text-[var(--muted)] font-semibold py-6 text-center">Needs ≥ 40 trading days.</div>
        )}
      </Panel>

      <Panel num="7.10" title="Regime boundaries" subtitle="what structurally changed mid-history">
        {bundle.regimes?.length ? bundle.regimes.map((r) => (
          <div key={r.date + r.label} className="flex items-center gap-3 py-1.5 border-b border-[var(--divider)]">
            <span className="text-[11.5px] font-extrabold tabular-nums text-[var(--muted)]">{r.date}</span>
            <span className="text-[12.5px] font-bold text-[var(--strong)]">{r.label}</span>
          </div>
        )) : (
          <div className="text-[12px] text-[var(--muted)] font-semibold py-4">No structural changes inside this window.</div>
        )}
      </Panel>

      <div className="rounded-[18px] border border-dashed border-[var(--border)] bg-[var(--card)] p-5" style={{ opacity: 0.55 }}>
        <div className="flex items-center justify-between">
          <span className="font-['Space_Grotesk'] font-bold text-[14.5px] text-[var(--strong)]">7.11 — Greek attribution</span>
          <span className="px-2 py-1 rounded-md bg-[var(--warn-bg)] text-[var(--warn-text)] text-[10.5px] font-extrabold">model attribution — planned</span>
        </div>
        <div className="text-[12px] text-[var(--faint)] font-semibold mt-2">
          Theta/gamma/vega/delta waterfall needs per-minute BS attribution across every path — a heavier compute pass, deliberately deferred rather than approximated.
        </div>
      </div>
    </div>
  );
}
