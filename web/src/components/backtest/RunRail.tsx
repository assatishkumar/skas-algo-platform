import { formatInr } from "../../lib/format";
import type { StrategyFormSpec } from "../../lib/backtestV2/registry";
import type { SizingState } from "../../lib/backtestV2/sizing";
import { sizingMath } from "../../lib/backtestV2/sizing";
import { v2InputClass } from "./primitives";

/** Progress of the (sequential) run batch — one entry per selected underlying. */
export interface BatchProgress {
  idx: number; total: number; underlying: string;
  day: string | null; done: number; jobTotal: number;   // intraday replay job detail
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex gap-3 py-[5px] text-[13px]">
      <span className="w-[96px] shrink-0 font-bold text-[var(--faint)]">{label}</span>
      <span className="font-semibold text-[var(--strong)] break-words">{children}</span>
    </div>
  );
}

/** The sticky Run-summary rail: what's about to run, the sizing math in rupees, the
 *  buttons, and (mid-run) the batch's live progress. */
export default function RunRail({
  spec, underlyings, windowLabel, sizing, running, batch, onRun, onSaveTemplate,
  saveTemplateDisabled, saveTemplateHint, sweep, error,
}: {
  spec: StrategyFormSpec;
  underlyings: string[];
  windowLabel: string | null;
  sizing: SizingState;
  running: boolean;
  batch: BatchProgress | null;
  onRun: () => void;
  onSaveTemplate: () => void;
  saveTemplateDisabled: boolean;
  saveTemplateHint?: string;
  sweep?: React.ReactNode;
  error?: string | null;
}) {
  const math = sizingMath(sizing);
  const b = (1 + (sizing.buffer || 0) / 100).toFixed(2);
  const sizingSummary = sizing.mode === "fixed"
    ? `Fixed · ${sizing.lots} lot${sizing.lots === 1 ? "" : "s"}`
    : `Capital refit · floor(equity ÷ era margin × ${b})`;
  // Overall batch fraction: completed underlyings + the current job's own progress.
  const frac = batch
    ? (batch.idx + (batch.jobTotal ? batch.done / batch.jobTotal : 0)) / batch.total
    : 0;

  return (
    <div className="rounded-[18px] border border-[var(--border)] bg-[var(--card)] p-[22px] lg:sticky lg:top-[86px]">
      <div className="font-['Space_Grotesk'] font-bold text-[16px] text-[var(--strong)] mb-3">
        Run summary
      </div>
      <Row label="Strategy">{spec.id}</Row>
      <Row label="Underlying">{underlyings.join(" · ")}</Row>
      <Row label="Window">{windowLabel ?? "—"}</Row>
      <Row label="Sizing">{sizingSummary}</Row>

      <div className="mt-3 rounded-[13px] border border-[var(--border)] p-3.5"
        style={{ background: "var(--stat)" }}>
        <div className="flex justify-between gap-3 text-[12.5px]">
          <span className="text-[var(--muted)]">Margin × lots</span>
          <span className="tabular-nums font-semibold text-[var(--strong)]">
            {formatInr(sizing.margin)} × {sizing.lots} = {formatInr(math.marginTotal)}
          </span>
        </div>
        <div className="mt-1.5 flex justify-between gap-3 text-[12.5px]">
          <span className="text-[var(--muted)]">+ {sizing.buffer}% buffer</span>
          <span className="tabular-nums font-semibold text-[var(--strong)]">
            {formatInr(math.bufferAmount)}
          </span>
        </div>
        <div className="my-2.5 h-px bg-[var(--divider)]" />
        <div className="flex justify-between gap-3 items-baseline">
          <span className="text-[12.5px] text-[var(--muted)]">Capital required</span>
          <span className="font-['Space_Grotesk'] font-bold text-[15px] tabular-nums"
            style={{ color: "var(--accent-deep)" }}>
            {formatInr(math.capitalRequired)}
          </span>
        </div>
      </div>

      {batch && (
        <div className="mt-3">
          <div className="text-[11.5px] text-[var(--muted)] tabular-nums">
            {batch.total > 1 && `Underlying ${batch.idx + 1}/${batch.total} — `}
            {batch.underlying}
            {batch.jobTotal ? ` · day ${Math.min(batch.done + 1, batch.jobTotal)}/${batch.jobTotal}` : " · starting…"}
          </div>
          <div className="mt-1 h-[7px] rounded-full bg-[var(--track)] overflow-hidden">
            <div className="h-full rounded-full transition-[width] duration-300"
              style={{ width: `${Math.round(frac * 100)}%`, background: "var(--ft)" }} />
          </div>
        </div>
      )}

      {error && (
        <div className="mt-3 rounded-[10px] px-3 py-2 text-[12px]"
          style={{ background: "var(--rose-bg)", color: "var(--rose-text)" }}>
          {error}
        </div>
      )}

      <button type="button" onClick={onRun} disabled={running}
        className="mt-3.5 w-full rounded-[13px] py-3.5 text-[14px] font-bold text-white disabled:opacity-60"
        style={{ background: "var(--ft)" }}>
        {running ? "Running…" : underlyings.length > 1
          ? `Run ${underlyings.length} backtests` : "Run backtest"}
      </button>
      <button type="button" onClick={onSaveTemplate} disabled={saveTemplateDisabled}
        title={saveTemplateHint}
        className="mt-2 w-full rounded-[13px] border-[1.5px] border-[var(--border)] py-3 text-[13.5px] font-bold text-[var(--muted)] disabled:opacity-50">
        Save as template
      </button>

      {sweep}

      <div className="mt-3.5 text-[12px] leading-relaxed text-[var(--faint)]">
        Replays the actual strategy class over captured premiums — fills at minute closes,
        F&O charges applied. Era-true lot sizes & margins; multi-underlying runs execute
        once per underlying and report side by side.
      </div>
    </div>
  );
}

/** Compact sweep block (EOD, single underlying — same constraint the old form had). */
export function SweepBlock({ enabled, onToggle, field, onField, values, onValues, options,
  disabledReason }: {
  enabled: boolean; onToggle: (v: boolean) => void;
  field: string; onField: (v: string) => void;
  values: string; onValues: (v: string) => void;
  options: { value: string; label: string }[];
  disabledReason?: string | null;
}) {
  return (
    <div className="mt-3.5 border-t border-[var(--divider)] pt-3.5">
      <label className={`flex items-center gap-2 text-[12.5px] font-semibold ${
        disabledReason ? "opacity-50" : "text-[var(--muted)]"}`} title={disabledReason ?? undefined}>
        <input type="checkbox" checked={enabled} disabled={!!disabledReason}
          onChange={(e) => onToggle(e.target.checked)} />
        Sweep a parameter
      </label>
      {disabledReason && (
        <div className="mt-1 text-[11px] text-[var(--faint)]">{disabledReason}</div>
      )}
      {enabled && !disabledReason && (
        <div className="mt-2 space-y-2">
          <select className={v2InputClass} value={field} onChange={(e) => onField(e.target.value)}>
            {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
          <input className={v2InputClass} value={values} placeholder="2, 2.5, 3"
            onChange={(e) => onValues(e.target.value)} />
          <div className="text-[11px] text-[var(--faint)]">
            2–5 values · each runs and saves, then opens Compare.
          </div>
        </div>
      )}
    </div>
  );
}
