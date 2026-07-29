/** Analyze workbench — overview (Phase A of design_handoff_analyze).
 *
 * Renders from the per-run analytics bundle (useBundle: GET → compute job → poll).
 * Phase A ships: identity + coverage chips, the range filter, 5 core + 4 options-native
 * KPI tiles (verbatim tooltips), the Stop/Target Simulator hero, and the six section
 * cards (drill-ins land in Phases B/C — cards navigate to a placeholder view so the
 * information architecture is already real). */

import { useMemo, useState } from "react";
import type { AnalyticsBundle, AnalyticsTrade } from "../../types";
import { formatInr } from "../../lib/format";
import { KPI_TIPS, SECTION_CARDS, type SectionId } from "../../lib/analyzeHelp";
import { useBundle } from "./useBundle";
import Simulator from "./Simulator";
import Explorer from "./Explorer";
import MeltHero from "./MeltHero";
import ConditioningSection from "./sections/ConditioningSection";
import SkewSection from "./sections/SkewSection";
import MeltSection from "./sections/MeltSection";
import LifecycleSection from "./sections/LifecycleSection";
import ExecutionSection from "./sections/ExecutionSection";
import RiskSection from "./sections/RiskSection";
import LedgerSection from "./sections/LedgerSection";
import type { Leg, Norm } from "../../lib/analyzeBuckets";

type Range = "full" | "2025" | "6m";

function filterTrades(b: AnalyticsBundle, range: Range): AnalyticsTrade[] {
  if (range === "full") return b.trades;
  if (range === "2025") return b.trades.filter((t) => t.exit >= "2025-01-01");
  const end = b.end ? new Date(b.end) : new Date();
  end.setMonth(end.getMonth() - 6);
  const cut = end.toISOString().slice(0, 10);
  return b.trades.filter((t) => t.exit.slice(0, 10) >= cut);
}

function Chip({ text, tone, tip }: { text: string; tone: "ok" | "warn" | "id"; tip?: string }) {
  const style = tone === "ok"
    ? { background: "var(--tint)", color: "var(--ft)", border: "1px solid var(--tint-border)" }
    : tone === "warn"
      ? { background: "var(--warn-bg)", color: "var(--warn-text)", border: "1px solid transparent" }
      : { background: "var(--chip)", color: "var(--chip-text)", border: "1px solid transparent" };
  return (
    <span title={tip} style={style}
      className={`px-2.5 py-1 rounded-full text-[11px] font-extrabold whitespace-nowrap${tip ? " cursor-help" : ""}`}>
      {text}
    </span>
  );
}

function coverageChips(b: AnalyticsBundle): { text: string; tone: "ok" | "warn"; tip?: string }[] {
  const c = b.coverage;
  const mk = (label: string, key: string, tipFull?: string, tipPart?: string) => {
    const v = c[key] ?? "none";
    return v === "full"
      ? { text: `${label} ✓`, tone: "ok" as const, tip: tipFull }
      : { text: `${label} ${v === "partial" ? "◐" : "✗"}`, tone: "warn" as const, tip: tipPart ?? tipFull };
  };
  return [
    mk("Conditioning", "conditioning"),
    mk("Skew", "skew"),
    mk("Melt", "melt", "Time-of-day decay from the traded straddles", "Needs the 1-min basis"),
    mk("Lifecycle", "lifecycle", "1-min MAE/MFE paths",
      b.paths_missing ? `${b.paths_missing} trades missing store days` : "Needs the 1-min basis"),
    mk("Execution", "execution", "Real fills", "Slippage & escalation panels need live fills"),
  ];
}

function Kpi({ label, value, sub, tip, color, tinted }: {
  label: string; value: string; sub: string; tip: string; color?: string; tinted?: boolean;
}) {
  return (
    <div title={tip} className="rounded-2xl px-[18px] py-3.5 cursor-help border"
      style={tinted
        ? { background: "var(--tint)", borderColor: "var(--tint-border)" }
        : { background: "var(--card)", borderColor: "var(--border)" }}>
      <div className="text-[11px] text-[var(--faint)] font-extrabold tracking-wider mb-[5px]">{label}</div>
      <div className="flex items-baseline gap-2 flex-wrap">
        <span className="font-['Space_Grotesk'] font-bold text-[19px] whitespace-nowrap tabular-nums"
          style={{ color: color ?? "var(--strong)" }}>{value}</span>
        <span className="text-[11.5px] text-[var(--faint)] font-semibold">{sub}</span>
      </div>
    </div>
  );
}

const fmtHold = (m: number | null) =>
  m == null ? "—" : `${Math.floor(m / 60)}h ${Math.round(m % 60)}m`;

/** KPIs for the SELECTED range. Full run = the report's own numbers verbatim (the
 *  Analyze==RunDetail acceptance bar); a filtered range recomputes every tile from the
 *  filtered trades + daily series (net of per-trade charges), labeled as range-scoped. */
function rangeKpis(b: AnalyticsBundle, trades: AnalyticsTrade[], range: Range) {
  if (range === "full") return { ...b.kpis, scoped: false };
  const lo = trades.length ? trades[0].entry.slice(0, 10) : "9999";
  const daily = b.daily.filter((d) => d.date >= lo);
  const net = trades.reduce((s, t) => s + t.pnl_rs - (t.charge_rs ?? 0), 0);
  const wins = trades.filter((t) => t.pnl_rs > 0).length;
  const pnl = daily.map((d) => d.pnl);
  const mean = pnl.length ? pnl.reduce((a, x) => a + x, 0) / pnl.length : 0;
  const sd = pnl.length > 1
    ? Math.sqrt(pnl.reduce((a, x) => a + (x - mean) ** 2, 0) / pnl.length) : 0;
  let peak = 0, cum = 0, maxDd = 0;
  for (const d of daily) { cum += d.pnl; peak = Math.max(peak, cum); maxDd = Math.min(maxDd, cum - peak); }
  const eqStart = daily.length ? daily[0].equity - daily[0].pnl : b.capital;
  const years = Math.max(daily.length / 252, 1e-9);
  const credits = trades.filter((t) => t.credit_rs > 0);
  const margins = trades.map((t) => t.margin_rs ?? 0);
  const marginRef = margins.length ? Math.max(...margins) : b.kpis.margin_ref;
  const sumCr = credits.reduce((s, t) => s + t.credit_rs, 0);
  return {
    scoped: true,
    total_pnl: Math.round(net),
    cagr: eqStart > 0 && daily.length >= 63
      ? +((((eqStart + net) / eqStart) ** (1 / years) - 1) * 100).toFixed(1) : null,
    sharpe: sd > 0 ? +((mean / sd) * Math.sqrt(252)).toFixed(2) : null,
    win_rate: trades.length ? +((100 * wins) / trades.length).toFixed(1) : 0,
    wins, trades: trades.length,
    max_dd: Math.round(maxDd),
    margin_ref: marginRef,
    rom_annual: marginRef ? +((100 * net) / marginRef / years).toFixed(1) : null,
    premium_capture: sumCr ? +((100 * net) / sumCr).toFixed(1) : null,
    avg_credit_rs: credits.length ? Math.round(sumCr / credits.length) : null,
    avg_credit_pts: credits.length
      ? +(credits.reduce((s, t) => s + t.credit_pts, 0) / credits.length).toFixed(1) : null,
    avg_hold_min: trades.length
      ? Math.round(trades.reduce((s, t) => s + t.hold_min, 0) / trades.length) : null,
  };
}

export default function AnalyzeWorkbench({ runId }: { runId: number }) {
  const { bundle, computing, progress, error } = useBundle(runId);
  const [range, setRange] = useState<Range>("full");
  const [norm, setNorm] = useState<Norm>("rs");
  const [leg, setLeg] = useState<Leg>("comb");
  const [view, setView] = useState<"overview" | SectionId>("overview");

  const trades = useMemo(
    () => (bundle ? filterTrades(bundle, range) : []), [bundle, range]);

  if (error) {
    return <div className="rounded-[14px] bg-[var(--warn-bg)] text-[var(--warn-text)] px-4 py-3 text-sm font-bold">{error}</div>;
  }
  if (!bundle) {
    const pct = progress && progress.total ? Math.round((100 * progress.done) / progress.total) : null;
    return (
      <div className="rounded-[18px] border border-[var(--border)] bg-[var(--card)] p-8 text-center">
        <div className="font-['Space_Grotesk'] font-bold text-[16px] text-[var(--strong)]">
          {computing ? "Computing analytics…" : "Loading…"}
        </div>
        {computing && (
          <div className="text-sm text-[var(--muted)] font-semibold mt-2">
            Reconstructing per-trade MAE/MFE paths from the 1-minute store
            {pct != null ? ` — ${pct}% (${progress!.done}/${progress!.total} cycles${progress!.day ? ` · ${progress!.day}` : ""})` : ""}.
            Cached after the first build — later opens are instant.
          </div>
        )}
      </div>
    );
  }

  const k = rangeKpis(bundle, trades, range);
  const scopedTag = k.scoped ? ({ "2025": " · 2025+", "6m": " · last 6M" } as Record<string, string>)[range] ?? "" : "";
  const years = bundle.daily.length ? (bundle.daily.length / 252).toFixed(1) : "—";
  const basisLabel = { intraday: "intraday · 1-min store", eod: "EOD daily basis", live: "live fills" }[bundle.basis] ?? bundle.basis;

  if (view !== "overview") {
    const card = SECTION_CARDS.find((s) => s.id === view)!;
    const section = view === "conditioning" ? <ConditioningSection trades={trades} />
      : view === "skew" ? <SkewSection trades={trades} />
      : view === "melt" ? <MeltSection bundle={bundle} />
      : view === "lifecycle" ? <LifecycleSection trades={trades} />
      : view === "execution" ? <ExecutionSection bundle={bundle} />
      : view === "risk" ? <RiskSection bundle={bundle} />
      : <LedgerSection runId={runId} />;
    return (
      <div>
        <div className="flex items-center gap-3.5 mb-4">
          <button onClick={() => { setView("overview"); window.scrollTo(0, 0); }}
            className="inline-flex items-center gap-2 px-3.5 py-2 rounded-[10px] bg-[var(--chip)] text-[var(--chip-text)] font-extrabold text-[13px]">
            ← Overview
          </button>
          <h2 className="font-['Space_Grotesk'] font-bold text-[24px] text-[var(--strong)] m-0">{card.t}</h2>
          <Chip text={basisLabel} tone="id" />
          {range !== "full" && <Chip text={`range: ${range === "2025" ? "2025+" : "last 6M"}`} tone="warn" />}
        </div>
        {section}
      </div>
    );
  }

  return (
    <div>
      {/* identity + coverage chips */}
      <div className="flex items-center gap-2 flex-wrap mb-3">
        <Chip text={`${bundle.strategy_id} · ${bundle.underlying}`} tone="id" />
        <Chip text={`${bundle.start ?? "—"} → ${bundle.end ?? "—"}`} tone="id" />
        <Chip text={basisLabel} tone="ok" />
        <Chip text={`${k.trades} trades`} tone="id" />
        <span className="flex-1" />
        {coverageChips(bundle).map((c) => <Chip key={c.text} {...c} />)}
      </div>

      {/* filter row */}
      <div className="flex items-center gap-2 flex-wrap mb-4">
        <span className="text-[11px] text-[var(--faint)] font-extrabold tracking-wider">UNDERLYING</span>
        <span className="px-2.5 py-1 rounded-full text-[11px] font-extrabold"
          style={{ background: "var(--tint)", color: "var(--ft)", border: "1px solid var(--accent)" }}>
          {bundle.underlying}</span>
        {["NIFTY", "BANKNIFTY", "SENSEX"].filter((u) => u !== bundle.underlying).map((u) => (
          <span key={u} title="Not in this run"
            className="px-2.5 py-1 rounded-full text-[11px] font-extrabold cursor-not-allowed"
            style={{ border: "1px dashed var(--field-border,var(--border))", color: "var(--faint)", opacity: 0.6 }}>
            {u}</span>
        ))}
        <span className="text-[11px] text-[var(--faint)] font-extrabold tracking-wider ml-2">LEG</span>
        <div className="inline-flex rounded-[9px] bg-[var(--seg,var(--chip))] p-[3px]">
          {([["comb", "Combined"], ["pe", "PE only"], ["ce", "CE only"]] as const).map(([v, lab]) => (
            <button key={v} onClick={() => setLeg(v)}
              className="px-2.5 py-1.5 rounded-[7px] text-[12px] font-extrabold"
              style={leg === v
                ? { background: "var(--card)", color: "var(--strong)", boxShadow: "0 1px 2px rgba(0,0,0,.08)" }
                : { color: "var(--muted)" }}>{lab}</button>
          ))}
        </div>
        <span className="text-[11px] text-[var(--faint)] font-extrabold tracking-wider ml-2">SHOW IN</span>
        <div className="inline-flex rounded-[9px] bg-[var(--seg,var(--chip))] p-[3px]">
          {([["rs", "₹"], ["cr", "% credit"], ["mg", "% margin"]] as const).map(([v, lab]) => (
            <button key={v} onClick={() => setNorm(v)}
              className="px-2.5 py-1.5 rounded-[7px] text-[12px] font-extrabold"
              style={norm === v
                ? { background: "var(--card)", color: "var(--strong)", boxShadow: "0 1px 2px rgba(0,0,0,.08)" }
                : { color: "var(--muted)" }}>{lab}</button>
          ))}
        </div>
        <span className="text-[11px] text-[var(--faint)] font-extrabold tracking-wider ml-2">RANGE</span>
        <div className="inline-flex rounded-[9px] bg-[var(--seg,var(--chip))] p-[3px]">
          {([["full", "Full run"], ["2025", "2025+"], ["6m", "Last 6M"]] as const).map(([v, lab]) => (
            <button key={v} onClick={() => setRange(v)}
              className="px-3 py-1.5 rounded-[7px] text-[12px] font-extrabold"
              style={range === v
                ? { background: "var(--card)", color: "var(--strong)", boxShadow: "0 1px 2px rgba(0,0,0,.08)" }
                : { color: "var(--muted)" }}>{lab}</button>
          ))}
        </div>
        {range !== "full" && (
          <span className="text-[12px] text-[var(--muted)] font-semibold">
            {trades.length} of {bundle.trades.length} trades — every tile and the simulator re-slice
          </span>
        )}
      </div>

      {/* core KPI row */}
      <div className="grid gap-3 mb-3" style={{ gridTemplateColumns: "repeat(auto-fit,minmax(180px,1fr))" }}>
        <Kpi label="TOTAL P&L" value={formatInr(k.total_pnl)} sub={`on ${formatInr(bundle.capital)} capital${scopedTag}`}
          tip={KPI_TIPS.total_pnl} color={k.total_pnl >= 0 ? "var(--pos)" : "var(--danger)"} />
        <Kpi label="CAGR" value={k.cagr != null ? `${k.cagr.toFixed(1)}%` : "—"}
          sub={k.scoped ? `annualized${scopedTag}` : `${years} years`} tip={KPI_TIPS.cagr} />
        <Kpi label="SHARPE" value={k.sharpe != null ? k.sharpe.toFixed(2) : "—"}
          sub={`daily, annualized${scopedTag}`} tip={KPI_TIPS.sharpe} />
        <Kpi label="WIN RATE" value={`${k.win_rate.toFixed(1)}%`} sub={`${k.wins} of ${k.trades}`} tip={KPI_TIPS.win_rate} />
        <Kpi label="MAX DRAWDOWN" value={formatInr(k.max_dd)} sub={bundle.capital ? `${((-100 * k.max_dd) / bundle.capital).toFixed(1)}% of capital` : ""}
          tip={KPI_TIPS.max_dd} color="var(--danger)" />
      </div>

      {/* options-native KPI row */}
      <div className="grid gap-3 mb-[18px]" style={{ gridTemplateColumns: "repeat(auto-fit,minmax(200px,1fr))" }}>
        <Kpi tinted label="RETURN ON MARGIN" value={k.rom_annual != null ? `${k.rom_annual.toFixed(1)}%` : "—"}
          sub={k.margin_ref ? `annualized · ${formatInr(k.margin_ref)} margin` : "no margin recorded"} tip={KPI_TIPS.rom} />
        <Kpi tinted label="PREMIUM CAPTURE" value={k.premium_capture != null ? `${k.premium_capture.toFixed(1)}%` : "—"}
          sub="per cycle · net ÷ credit" tip={KPI_TIPS.capture} />
        <Kpi tinted label="AVG CREDIT" value={k.avg_credit_rs != null ? formatInr(k.avg_credit_rs) : "—"}
          sub={k.avg_credit_pts != null ? `${k.avg_credit_pts.toFixed(0)} pts` : ""} tip={KPI_TIPS.avg_credit} />
        <Kpi tinted label="AVG HOLD" value={fmtHold(k.avg_hold_min)} sub="entry → square-off" tip={KPI_TIPS.avg_hold} />
      </div>

      <Simulator trades={trades} disabled={bundle.coverage.simulator === "none"} />

      <Explorer trades={trades} norm={norm} leg={leg} />

      <MeltHero bundle={bundle} />

      {/* section cards */}
      <div className="grid gap-4" style={{ gridTemplateColumns: "repeat(auto-fill,minmax(320px,1fr))" }}>
        {SECTION_CARDS.map((sc) => (
          <button key={sc.id} onClick={() => { setView(sc.id); window.scrollTo(0, 0); }}
            className="text-left rounded-[18px] border border-[var(--border)] bg-[var(--card)] px-[22px] py-5 transition-colors hover:border-[var(--accent)] hover:bg-[var(--row-hover,var(--stat))]">
            <div className="flex items-center justify-between">
              <span className="font-['Space_Grotesk'] font-bold text-[15px] text-[var(--strong)]">{sc.t}</span>
              <span className="text-[var(--accent-deep)] font-extrabold text-[15px]">→</span>
            </div>
            <div className="text-[12.5px] text-[var(--faint)] font-semibold mt-[5px] leading-[1.5]">{sc.sub}</div>
            <div className="flex items-center gap-2 mt-3">
              <span className="px-2.5 py-[5px] rounded-lg bg-[var(--chip)] text-[var(--chip-text)] font-extrabold text-[11px]">{sc.n}</span>
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}
