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

      <div className="grid gap-6 mt-4 items-start" style={{ gridTemplateColumns: "minmax(0,1fr) 300px" }}>
        {/* bucket table */}
        <div className="overflow-x-auto">
          <div className="grid gap-2 px-3 py-2 rounded-lg bg-[var(--stat)] text-[10px] font-extrabold tracking-wider text-[var(--faint)]"
            style={{ gridTemplateColumns: "1.5fr 0.6fr 1fr 1fr 0.7fr 1fr 0.9fr 1.4fr" }}>
            <span>BUCKET</span><span className="text-right">TRADES</span>
            <span className="text-right">MEAN</span><span className="text-right">MEDIAN</span>
            <span className="text-right">WIN %</span><span className="text-right">WORST</span>
            <span className="text-right">ROM/CYC</span><span className="text-right">95% CI</span>
          </div>
          {rows.map((r) => (
            <div key={r.label}
              className="grid gap-2 px-3 py-2 border-b border-[var(--divider)] text-[12.5px] font-semibold tabular-nums"
              style={{ gridTemplateColumns: "1.5fr 0.6fr 1fr 1fr 0.7fr 1fr 0.9fr 1.4fr", opacity: r.n < 20 ? 0.45 : 1 }}>
              <span className="text-[var(--strong)] font-extrabold">{r.label}</span>
              <span className="text-right">{r.n}{r.n < 20 ? " ⚠" : ""}</span>
              <span className="text-right font-bold" style={{ color: r.mean >= 0 ? "var(--pos)" : "var(--danger)" }}>{fmt(r.mean, norm)}</span>
              <span className="text-right text-[var(--muted)]">{fmt(r.median, norm)}</span>
              <span className="text-right text-[var(--muted)]">{r.winPct.toFixed(0)}%</span>
              <span className="text-right text-[var(--danger)]">{fmt(r.worst, norm)}</span>
              <span className="text-right text-[var(--muted)]">{r.romCycle != null ? `${r.romCycle.toFixed(2)}%` : "—"}</span>
              <span className="text-right text-[var(--faint)]">{fmt(r.ci[0], norm)} … {fmt(r.ci[1], norm)}</span>
            </div>
          ))}
          {!rows.length && (
            <div className="px-3 py-5 text-sm text-[var(--muted)] font-semibold">No trades in range for this condition.</div>
          )}
          <div className="text-[11.5px] text-[var(--faint)] font-semibold mt-2 leading-[1.5]">{footnote}</div>
        </div>

        {/* diverging mean-%-of-credit bars */}
        <div className="pt-7">
          {rows.map((r) => {
            const v = r.meanPctCredit ?? 0;
            const w = Math.min(46, (Math.abs(v) / maxBar) * 46);
            return (
              <div key={r.label} className="flex items-center gap-1.5 h-[26px]"
                style={{ opacity: r.n < 20 ? 0.45 : 1 }}>
                <div className="relative h-[10px]" style={{ width: 120 }}>
                  <div className="absolute top-0 bottom-0 w-px bg-[var(--faint)] opacity-85" style={{ left: 60 }} />
                  <div className="absolute top-0 h-[10px] rounded"
                    style={{
                      left: v >= 0 ? 60 : 60 - w, width: w,
                      background: v >= 0 ? "var(--pos)" : "var(--danger)",
                    }} />
                </div>
                <span className="text-[10.5px] font-extrabold tabular-nums"
                  style={{ color: v >= 0 ? "var(--pos)" : "var(--danger)" }}>
                  {v >= 0 ? "+" : ""}{v.toFixed(1)}%
                </span>
              </div>
            );
          })}
          {rows.length > 0 && (
            <div className="text-[10px] text-[var(--faint)] font-bold mt-1">mean · % of credit</div>
          )}
        </div>
      </div>
    </div>
  );
}
