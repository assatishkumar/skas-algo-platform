/** Hero 1 — Stop / Target Simulator (design_handoff_analyze).
 *
 * Replays every trade's stored 5-min MTM path against a stop and a profit target: the
 * FIRST breach in path order wins; a stop fill assumes slippage of 3% of credit past the
 * level. Trades without a path (store gap / non-intraday basis) ride as-traded.
 * BASIS (owner 2026-07-28): the platform's strategies express exits as % of MARGIN
 * (intraday_straddle −2% margin, hni ±1%), so the sliders default to %-of-margin —
 * thresholds convert per trade via its own frozen margin base (margin_rs). %-of-credit
 * (the design's original basis) stays one toggle away. All client-side, zero round-trips. */

import { useMemo, useState } from "react";
import {
  CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import type { AnalyticsTrade } from "../../types";
import { formatInr } from "../../lib/format";
import { PANEL_HELP } from "../../lib/analyzeHelp";

export type SimBasis = "margin" | "credit";

// slider ranges per basis; the top of each range = Off. Margin range matches the
// strategies' own vocabulary (intraday_straddle stop 2%, hni ±1%, delta_neutral 2.5%).
const CFG = {
  credit: { stopMin: 20, stopMax: 150, stopStep: 5, tgtMin: 10, tgtMax: 100, tgtStep: 5 },
  margin: { stopMin: 0.5, stopMax: 6, stopStep: 0.25, tgtMin: 0.25, tgtMax: 4, tgtStep: 0.25 },
} as const;

export type TrailMode = "off" | "ratchet" | "below";

// trail slider ranges per basis (the strategies' vocabulary: intraday_straddle's live
// ratchet is trigger 1.0 / step 0.5, % of margin)
const TRAIL_CFG = {
  margin: { trgMin: 0.25, trgMax: 3, trgStep: 0.25, stMin: 0.1, stMax: 2, stStep: 0.1 },
  credit: { trgMin: 5, trgMax: 50, trgStep: 5, stMin: 2, stMax: 30, stStep: 2 },
} as const;

interface SimTrade { pnl: number; base: number; clipped: boolean; exit: string }

/** Thresholds arrive in the CHOSEN basis; each trade works in that basis (its path point
 *  pct-of-credit ÷ conv), so a 2%-of-margin stop is a DIFFERENT credit-% per trade,
 *  exactly like the live strategies' frozen margin_base. The trail mirrors
 *  intraday_straddle._stop_level: it LIFTS the fixed stop and never lowers —
 *  ratchet: −stop + step×⌊peak/trigger⌋ · below-peak: max(−stop, peak−step) once
 *  peak ≥ trigger. Stop/trail breach is checked before the target at each point. */
function simulate(trades: AnalyticsTrade[], basis: SimBasis, stop: number, target: number,
                  trailMode: TrailMode, trailTrig: number, trailStep: number): SimTrade[] {
  const cfg = CFG[basis];
  const stopOff = stop >= cfg.stopMax, targetOff = target >= cfg.tgtMax;
  const trailOn = trailMode !== "off" && !stopOff;   // the trail lifts the FIXED stop
  return trades.map((t) => {
    let pnl = t.pnl_rs;
    let clipped = false;
    const conv = basis === "margin"
      ? (t.margin_rs && t.credit_rs > 0 ? t.margin_rs / t.credit_rs : null)
      : 1;
    if (!(stopOff && targetOff) && t.path5.length && t.credit_rs > 0 && conv != null) {
      const baseRs = basis === "margin" ? t.margin_rs! : t.credit_rs;
      const slipRs = 0.03 * t.credit_rs;               // stop fills slip 3% of credit
      let peak = 0;
      for (const [, pctC] of t.path5) {
        const pct = pctC / conv;                        // path point in the CHOSEN basis
        peak = Math.max(peak, pct);
        let level = stopOff ? -Infinity : -stop;
        if (trailOn) {
          level = trailMode === "ratchet"
            ? -stop + trailStep * Math.floor(Math.max(peak, 0) / trailTrig)
            : peak >= trailTrig ? Math.max(-stop, peak - trailStep) : -stop;
        }
        if (!stopOff && pct <= level) {
          pnl = (level / 100) * baseRs - slipRs;
          clipped = t.pnl_rs > 0 && pnl < t.pnl_rs;
          break;
        }
        if (!targetOff && pct >= target) {
          pnl = (target / 100) * baseRs;
          break;
        }
      }
    }
    return { pnl, base: t.pnl_rs, clipped, exit: t.exit };
  });
}

function equitySeries(sims: SimTrade[]) {
  const sorted = [...sims].sort((a, b) => a.exit.localeCompare(b.exit));
  let cb = 0, cs = 0, pb = 0, ps = 0, ddB = 0, ddS = 0;
  const pts = sorted.map((s, i) => {
    cb += s.base; cs += s.pnl;
    pb = Math.max(pb, cb); ps = Math.max(ps, cs);
    ddB = Math.min(ddB, cb - pb); ddS = Math.min(ddS, cs - ps);
    return { i, date: s.exit.slice(0, 10), base: Math.round(cb), sim: Math.round(cs) };
  });
  return { pts, ddB, ddS };
}

export default function Simulator({ trades, disabled, capital }: {
  trades: AnalyticsTrade[];
  disabled: boolean;         // coverage.simulator === "none" (no paths on this basis)
  capital: number;           // same base the header CAGR uses, so the two are comparable
}) {
  // margin basis needs per-trade margin_rs (bundle v2+); fall back to credit when absent
  const marginable = trades.some((t) => t.margin_rs != null);
  const [basis, setBasis] = useState<SimBasis>(marginable ? "margin" : "credit");
  const cfg = CFG[basis];
  const [stop, setStop] = useState<number>(cfg.stopMax);
  const [target, setTarget] = useState<number>(cfg.tgtMax);
  const [help, setHelp] = useState(false);

  const [trailMode, setTrailMode] = useState<TrailMode>("off");
  const [trailTrig, setTrailTrig] = useState<number>(TRAIL_CFG[basis].trgMin * 4);
  const [trailStep, setTrailStep] = useState<number>(TRAIL_CFG[basis].stMin * 5);
  const tcfg = TRAIL_CFG[basis];

  const switchBasis = (b: SimBasis) => {
    setBasis(b);
    setStop(CFG[b].stopMax);      // re-arm at Off — thresholds don't translate 1:1
    setTarget(CFG[b].tgtMax);
    setTrailMode("off");
    setTrailTrig(TRAIL_CFG[b].trgMin * 4);
    setTrailStep(TRAIL_CFG[b].stMin * 5);
  };

  const { pts, tiles, note } = useMemo(() => {
    const sims = simulate(trades, basis, stop, target, trailMode, trailTrig, trailStep);
    const { pts, ddB, ddS } = equitySeries(sims);
    const be = sims.reduce((s, x) => s + x.base, 0);
    const se = sims.reduce((s, x) => s + x.pnl, 0);
    const bWins = sims.filter((x) => x.base > 0).length;
    const sWins = sims.filter((x) => x.pnl > 0).length;
    const clipped = sims.filter((x) => x.clipped).length;
    const stopOff = stop >= cfg.stopMax, targetOff = target >= cfg.tgtMax;
    const n = sims.length || 1;
    const unit = basis === "margin" ? "% of margin" : "% of credit";
    // CAGR on the same capital base as the header KPI, over the span the trades cover.
    // Needs a real span and a surviving equity base — else it is not a rate, and "—" is
    // the honest answer rather than a number nobody can act on.
    const days = trades.length
      ? (Date.parse(trades[trades.length - 1].exit) - Date.parse(trades[0].entry)) / 864e5 : 0;
    const yrs = days / 365.25;
    const cagrOf = (p: number) =>
      capital > 0 && yrs >= 0.25 && capital + p > 0
        ? ((capital + p) / capital) ** (1 / yrs) * 100 - 100 : null;
    const cS = cagrOf(se), cB = cagrOf(be);
    const tiles = [
      { l: "NET P&L", v: formatInr(Math.round(se)), d: `${se - be >= 0 ? "+" : "−"}${formatInr(Math.abs(Math.round(se - be))).replace("−", "")}`, good: se - be >= 0 },
      { l: "WIN RATE", v: `${((100 * sWins) / n).toFixed(1)}%`, d: `${sWins - bWins >= 0 ? "+" : "−"}${Math.abs(sWins - bWins)} trades`, good: sWins - bWins >= 0 },
      { l: "MAX DRAWDOWN", v: formatInr(Math.round(ddS)), d: `${ddS - ddB >= 0 ? "−" : "+"}${formatInr(Math.abs(Math.round(ddS - ddB))).replace("−", "")}`, good: ddS >= ddB },
      { l: "WINNERS CLIPPED", v: String(clipped), d: bWins ? `${((clipped / bWins) * 100).toFixed(0)}% of winners` : "", good: clipped === 0 },
      { l: "CAGR", v: cS != null ? `${cS.toFixed(1)}%` : "—",
        d: cS != null && cB != null
          ? `${cS - cB >= 0 ? "+" : "−"}${Math.abs(cS - cB).toFixed(1)} pts`
          : "needs 3+ months",
        good: cS != null && cB != null ? cS - cB >= 0 : true },
    ];
    const trailTxt = trailMode !== "off" && !stopOff
      ? ` + ${trailMode === "ratchet" ? "ratchet" : "below-peak"} trail (${trailTrig}/${trailStep})`
      : "";
    const note = stopOff && targetOff
      ? "Both off — showing the run as traded. Drag a slider to test an exit rule against every stored MAE/MFE path."
      : `A stop at ${stopOff ? "—" : `${stop}${unit}`}${trailTxt}${targetOff ? "" : ` with a ${target}${unit} target`} would have ${se - be >= 0 ? "added " : "cost "}${formatInr(Math.abs(Math.round(se - be)))} vs as-traded, clipping ${clipped} eventual winners.`;
    return { pts, tiles, note };
  }, [trades, basis, stop, target, trailMode, trailTrig, trailStep, cfg]);

  return (
    <div className="rounded-[18px] border border-[var(--border)] bg-[var(--card)] p-6 mb-[18px]">
      <div className="grid gap-7 items-start" style={{ gridTemplateColumns: "330px minmax(0,1fr)" }}>
        <div>
          <div className="flex items-center gap-2.5">
            <span className="font-['Space_Grotesk'] font-bold text-[16px] text-[var(--strong)]">Stop / target simulator</span>
            <button onClick={() => { setStop(cfg.stopMax); setTarget(cfg.tgtMax); setTrailMode("off"); }}
              className="text-[11.5px] font-extrabold text-[var(--accent-deep)]">Reset</button>
            <button onClick={() => setHelp((h) => !h)} title="How to read this panel"
              className="w-[21px] h-[21px] rounded-full text-[12px] font-extrabold inline-flex items-center justify-center"
              style={{ background: help ? "var(--accent)" : "var(--chip)", color: help ? "#fff" : "var(--chip-text)" }}>?</button>
          </div>
          <div className="text-[12.5px] text-[var(--faint)] font-semibold mt-1 leading-[1.5]">
            Recomputed from stored MAE / MFE paths of every trade. This panel prescribes; the rest describe.
          </div>
          {help && (
            <div className="mt-2.5 rounded-[10px] border border-[var(--tint-border)] bg-[var(--tint)] px-3.5 py-[11px] text-[12.5px] font-semibold leading-[1.6] text-[var(--ft)]">
              {PANEL_HELP.sim} On the margin basis, thresholds are % of each trade's own
              frozen margin — the exact unit the strategies' stop_loss_pct / profit targets use.
            </div>
          )}
          {disabled ? (
            <div className="mt-4 rounded-[10px] bg-[var(--warn-bg)] px-3.5 py-3 text-[12.5px] font-bold text-[var(--warn-text)]">
              Simulator needs per-trade minute paths — available on the 1-minute (intraday) data basis only.
            </div>
          ) : (
            <>
              <div className="mt-3.5 flex items-center gap-2">
                <span className="text-[11px] text-[var(--faint)] font-extrabold tracking-wider">BASIS</span>
                <div className="inline-flex rounded-[9px] bg-[var(--chip)] p-[3px]">
                  {(["margin", "credit"] as const).map((b) => (
                    <button key={b} onClick={() => switchBasis(b)}
                      disabled={b === "margin" && !marginable}
                      title={b === "margin" && !marginable ? "This run has no margin series" : undefined}
                      className="px-2.5 py-1 rounded-[7px] text-[11.5px] font-extrabold disabled:opacity-50"
                      style={basis === b
                        ? { background: "var(--card)", color: "var(--strong)", boxShadow: "0 1px 2px rgba(0,0,0,.08)" }
                        : { color: "var(--muted)" }}>
                      {b === "margin" ? "% margin" : "% credit"}
                    </button>
                  ))}
                </div>
                {basis === "margin" && (
                  <span className="text-[11px] text-[var(--faint)] font-semibold">the strategies' own unit</span>
                )}
              </div>
              <div className="mt-4">
                <div className="flex justify-between items-baseline mb-2">
                  <span className="text-[11px] text-[var(--faint)] font-extrabold tracking-wider">STOP LOSS</span>
                  <span className="font-['Space_Grotesk'] font-bold text-[13px] text-[var(--danger)]">
                    {stop >= cfg.stopMax ? "Off" : `−${stop}% of ${basis}`}</span>
                </div>
                <input type="range" min={cfg.stopMin} max={cfg.stopMax} step={cfg.stopStep} value={stop}
                  onChange={(e) => setStop(+e.target.value)} className="w-full accent-[var(--accent)]" />
              </div>
              <div className="mt-4">
                <div className="flex justify-between items-baseline mb-2">
                  <span className="text-[11px] text-[var(--faint)] font-extrabold tracking-wider">PROFIT TARGET</span>
                  <span className="font-['Space_Grotesk'] font-bold text-[13px] text-[var(--pos)]">
                    {target >= cfg.tgtMax ? "Off" : `+${target}% of ${basis}`}</span>
                </div>
                <input type="range" min={cfg.tgtMin} max={cfg.tgtMax} step={cfg.tgtStep} value={target}
                  onChange={(e) => setTarget(+e.target.value)} className="w-full accent-[var(--accent)]" />
              </div>
              <div className="mt-4">
                <div className="flex justify-between items-baseline mb-2">
                  <span className="text-[11px] text-[var(--faint)] font-extrabold tracking-wider">TRAILING STOP</span>
                  <div className="inline-flex rounded-[8px] bg-[var(--chip)] p-[2px]">
                    {([["off", "Off"], ["ratchet", "Ratchet"], ["below", "Below peak"]] as const).map(([v, lab]) => (
                      <button key={v} onClick={() => setTrailMode(v)}
                        className="px-2 py-[3px] rounded-[6px] text-[10.5px] font-extrabold"
                        style={trailMode === v
                          ? { background: "var(--card)", color: "var(--strong)" }
                          : { color: "var(--muted)" }}>{lab}</button>
                    ))}
                  </div>
                </div>
                {trailMode !== "off" && stop >= cfg.stopMax && (
                  <div className="text-[11.5px] font-bold text-[var(--warn-text)] bg-[var(--warn-bg)] rounded-[8px] px-2.5 py-2">
                    The trail lifts the fixed stop — set a STOP LOSS first (like the strategies do).
                  </div>
                )}
                {trailMode !== "off" && stop < cfg.stopMax && (
                  <>
                    <div className="flex justify-between items-baseline mb-1 mt-1">
                      <span className="text-[10.5px] text-[var(--faint)] font-bold">
                        {trailMode === "ratchet" ? "every peak trigger of" : "activates at peak"}</span>
                      <span className="font-['Space_Grotesk'] font-bold text-[12px] text-[var(--strong)]">+{trailTrig}% {basis}</span>
                    </div>
                    <input type="range" min={tcfg.trgMin} max={tcfg.trgMax} step={tcfg.trgStep} value={trailTrig}
                      onChange={(e) => setTrailTrig(+e.target.value)} className="w-full accent-[var(--accent)]" />
                    <div className="flex justify-between items-baseline mb-1 mt-1">
                      <span className="text-[10.5px] text-[var(--faint)] font-bold">
                        {trailMode === "ratchet" ? "lifts the stop by" : "trail distance below peak"}</span>
                      <span className="font-['Space_Grotesk'] font-bold text-[12px] text-[var(--strong)]">{trailStep}% {basis}</span>
                    </div>
                    <input type="range" min={tcfg.stMin} max={tcfg.stMax} step={tcfg.stStep} value={trailStep}
                      onChange={(e) => setTrailStep(+e.target.value)} className="w-full accent-[var(--accent)]" />
                  </>
                )}
              </div>
              <div className="grid grid-cols-2 gap-2.5 mt-4">
                {tiles.map((t) => (
                  <div key={t.l} className="rounded-[12px] bg-[var(--stat)] px-3 py-2.5">
                    <div className="text-[10px] text-[var(--faint)] font-extrabold tracking-wider">{t.l}</div>
                    <div className="font-['Space_Grotesk'] font-bold text-[15px] text-[var(--strong)] tabular-nums">{t.v}</div>
                    <div className="text-[11px] font-bold tabular-nums"
                      style={{ color: t.good ? "var(--pos)" : "var(--danger)" }}>{t.d}</div>
                  </div>
                ))}
              </div>
              <div className="mt-3 text-[12px] font-bold leading-[1.55]" style={{ color: "var(--note, #c2661d)" }}>{note}</div>
            </>
          )}
        </div>
        <div className="h-[320px] min-w-0 flex flex-col">
          {!disabled && (
            <>
              {/* Two series, two stroke styles, previously no key — the solid/dashed
                  distinction is the whole point of the panel, so name it explicitly. */}
              <div className="flex items-center gap-4 mb-1 text-[11.5px] font-bold">
                <span className="inline-flex items-center gap-1.5" style={{ color: "var(--strong)" }}>
                  <svg width="20" height="8" aria-hidden="true">
                    <line x1="0" y1="4" x2="20" y2="4" stroke="var(--accent)" strokeWidth="2.2" />
                  </svg>
                  Simulated (with your stop / target)
                </span>
                <span className="inline-flex items-center gap-1.5" style={{ color: "var(--muted)" }}>
                  <svg width="20" height="8" aria-hidden="true">
                    <line x1="0" y1="4" x2="20" y2="4" stroke="var(--faint)" strokeWidth="1.6"
                      strokeDasharray="5 4" />
                  </svg>
                  As traded (the run itself)
                </span>
                <span className="text-[11px] font-semibold" style={{ color: "var(--faint)" }}>
                  cumulative P&amp;L
                </span>
              </div>
            <div className="flex-1 min-h-0">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={pts} margin={{ top: 8, right: 12, bottom: 4, left: 8 }}>
                <CartesianGrid strokeDasharray="2 4" stroke="var(--divider)" vertical={false} />
                <XAxis dataKey="date" tick={{ fontSize: 10, fill: "var(--faint)" }}
                  minTickGap={70} tickLine={false} axisLine={{ stroke: "var(--border)" }} />
                <YAxis tick={{ fontSize: 10, fill: "var(--faint)" }} width={72}
                  tickFormatter={(v: number) => formatInr(v)} tickLine={false}
                  axisLine={{ stroke: "var(--border)" }} />
                <Tooltip formatter={(v: number, k: string) => [formatInr(v), k === "sim" ? "Simulated" : "As traded"]}
                  contentStyle={{ background: "var(--card)", border: "1px solid var(--border)", borderRadius: 10, fontSize: 12 }} />
                <Line dataKey="base" name="As traded" stroke="var(--faint)" strokeDasharray="5 4"
                  dot={false} strokeWidth={1.6} isAnimationActive={false} />
                <Line dataKey="sim" name="Simulated" stroke="var(--accent)" dot={false}
                  strokeWidth={2.2} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
            </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
