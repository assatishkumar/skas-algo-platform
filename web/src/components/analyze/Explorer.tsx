/** Hero 2 — Conditioner Explorer (design_handoff_analyze).
 *
 * 11 condition chips slice every trade by one entry-time condition; the bucket table
 * shows TRADES / MEAN / MEDIAN / WIN % / WORST / ROM per cycle / 95% CI with the money
 * columns following the normalization toggle (₹ / % credit / % margin) and the LEG
 * segment (Combined / PE / CE). Buckets under 20 trades grey to 45% with ⚠. The right
 * column mirrors MEAN as a diverging %-of-credit bar. All client-side. */

import { useMemo, useState } from "react";
import type { AnalyticsTrade } from "../../types";
import { formatInr } from "../../lib/format";
import { EXPLORER_HELP } from "../../lib/analyzeHelp";
import {
  bucketStats, CONDITIONS, type BucketStats, type Leg, type Norm,
} from "../../lib/analyzeBuckets";

function fmt(v: number, norm: Norm): string {
  return norm === "rs" ? formatInr(Math.round(v)) : `${v.toFixed(norm === "mg" ? 2 : 1)}%`;
}

// Column template — the mean bar is now a COLUMN, not a detached chart beside the table:
// reading a bar meant matching row order across a 300px gap, and the ₹ column had nowhere
// to live. Widths are fr-based so the table still fits without the right edge clipping.
const COLS = "1.4fr 0.55fr 0.8fr 0.8fr 0.6fr 0.85fr 0.8fr 1.5fr 1fr 1.1fr";

/** Diverging mean bar, centred on zero — magnitude at a glance beside its own numbers. */
function MeanBar({ value, max }: { value: number | null; max: number }) {
  if (value == null) return <span className="text-right text-[var(--faint)]">—</span>;
  const half = 44;
  const w = Math.min(half, (Math.abs(value) / (max || 1)) * half);
  const color = value >= 0 ? "var(--pos)" : "var(--danger)";
  return (
    <span className="flex items-center justify-center gap-1.5"
      title={`mean ${value >= 0 ? "+" : ""}${value.toFixed(1)}% of credit`}>
      <span className="relative h-[10px] shrink-0" style={{ width: half * 2 }}>
        <span className="absolute top-0 bottom-0 w-px opacity-70"
          style={{ left: half, background: "var(--faint)" }} />
        <span className="absolute top-0 h-[10px] rounded-[2px]"
          style={{ left: value >= 0 ? half : half - w, width: w, background: color }} />
      </span>
      <span className="text-[10.5px] font-extrabold tabular-nums" style={{ color }}>
        {value >= 0 ? "+" : ""}{value.toFixed(1)}%
      </span>
    </span>
  );
}

export default function Explorer({ trades, norm, leg }: {
  trades: AnalyticsTrade[]; norm: Norm; leg: Leg;
}) {
  const [condId, setCondId] = useState("dte");
  const [help, setHelp] = useState(false);

  const chips = useMemo(() => CONDITIONS.map((c) => ({
    id: c.id, label: c.label, reason: c.disabled(trades),
  })), [trades]);

  const active = CONDITIONS.find((c) => c.id === condId && !c.disabled(trades))
    ?? CONDITIONS.find((c) => !c.disabled(trades))!;

  const rows: BucketStats[] = useMemo(
    () => active.bucketize(trades)
      .map((b) => bucketStats(b, leg, norm))
      .filter((r): r is BucketStats => r != null),
    [active, trades, leg, norm]);

  const maxBar = Math.max(1, ...rows.map((r) => Math.abs(r.meanPctCredit ?? 0)));
  const footnote = active.footnote
    ?? "Buckets under 20 trades are greyed — not enough sample to trust. CI is a ±1.96 SE band on the mean.";

  return (
    <div className="rounded-[18px] border border-[var(--border)] bg-[var(--card)] p-6 mb-[18px]">
      <div className="flex items-center gap-2.5">
        <span className="font-['Space_Grotesk'] font-bold text-[16px] text-[var(--strong)]">Conditioner explorer</span>
        <button onClick={() => setHelp((h) => !h)} title="How to read this panel"
          className="w-[21px] h-[21px] rounded-full text-[12px] font-extrabold inline-flex items-center justify-center"
          style={{ background: help ? "var(--accent)" : "var(--chip)", color: help ? "#fff" : "var(--chip-text)" }}>?</button>
        <span className="text-[12px] text-[var(--faint)] font-semibold">
          {active.label} · {leg === "comb" ? "combined" : leg.toUpperCase() + " leg"} ·
          {norm === "rs" ? " ₹" : norm === "cr" ? " % of credit" : " % of margin"}
        </span>
      </div>
      {help && (
        <div className="mt-2.5 rounded-[10px] border border-[var(--tint-border)] bg-[var(--tint)] px-3.5 py-[11px] text-[12.5px] font-semibold leading-[1.6] text-[var(--ft)]">
          {EXPLORER_HELP}
        </div>
      )}
      <div className="flex flex-wrap gap-1.5 mt-3.5">
        {chips.map((c) => (
          <button key={c.id}
            onClick={() => !c.reason && setCondId(c.id)}
            title={c.reason ?? undefined}
            disabled={!!c.reason}
            className="px-3 py-1.5 rounded-full text-[11.5px] font-extrabold"
            style={c.reason
              ? { border: "1px dashed var(--field-border,var(--border))", color: "var(--faint)", opacity: 0.65, cursor: "not-allowed" }
              : active.id === c.id
                ? { background: "var(--accent)", color: "#fff", border: "1px solid var(--accent)" }
                : { background: "var(--chip)", color: "var(--chip-text)", border: "1px solid transparent" }}>
            {c.label}
          </button>
        ))}
      </div>

      <div className="mt-4">
        {/* bucket table */}
        <div className="overflow-x-auto">
          <div className="grid gap-2 px-3 py-2 rounded-lg bg-[var(--stat)] text-[10px] font-extrabold tracking-wider text-[var(--muted)]"
            style={{ gridTemplateColumns: COLS }}>
            <span>BUCKET</span><span className="text-right">TRADES</span>
            <span className="text-right">MEAN</span><span className="text-right">MEDIAN</span>
            <span className="text-right">WIN %</span><span className="text-right">WORST</span>
            <span className="text-right">ROM/CYC</span><span className="text-right">95% CI</span>
            <span className="text-right">P&amp;L ₹</span><span className="text-center">MEAN · % CREDIT</span>
          </div>
          {rows.map((r) => (
            <div key={r.label}
              className="grid gap-2 px-3 py-2 border-b border-[var(--divider)] text-[12.5px] font-semibold tabular-nums items-center"
              style={{ gridTemplateColumns: COLS, opacity: r.n < 20 ? 0.55 : 1 }}>
              <span className="text-[var(--strong)] font-extrabold">{r.label}</span>
              <span className="text-right text-[var(--body,var(--strong))]">{r.n}{r.n < 20 ? " ⚠" : ""}</span>
              <span className="text-right font-bold" style={{ color: r.mean >= 0 ? "var(--pos)" : "var(--danger)" }}>{fmt(r.mean, norm)}</span>
              <span className="text-right text-[var(--strong)]">{fmt(r.median, norm)}</span>
              <span className="text-right text-[var(--strong)]">{r.winPct.toFixed(0)}%</span>
              <span className="text-right text-[var(--danger)]">{fmt(r.worst, norm)}</span>
              <span className="text-right text-[var(--strong)]">{r.romCycle != null ? `${r.romCycle.toFixed(2)}%` : "—"}</span>
              <span className="text-right text-[var(--muted)] whitespace-nowrap">{fmt(r.ci[0], norm)} … {fmt(r.ci[1], norm)}</span>
              <span className="text-right font-bold" style={{ color: r.pnlRs >= 0 ? "var(--pos)" : "var(--danger)" }}>{formatInr(Math.round(r.pnlRs))}</span>
              <MeanBar value={r.meanPctCredit} max={maxBar} />
            </div>
          ))}
          {!rows.length && (
            <div className="px-3 py-5 text-sm text-[var(--muted)] font-semibold">No trades in range for this condition.</div>
          )}
          <div className="text-[11.5px] text-[var(--faint)] font-semibold mt-2 leading-[1.5]">{footnote}</div>
        </div>
      </div>
    </div>
  );
}
