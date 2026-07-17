import { formatInr } from "../../lib/format";
import type { Basis, FieldSpec, StrategyFormSpec } from "../../lib/backtestV2/registry";
import { TRAIL_UI, visibleFields } from "../../lib/backtestV2/registry";
import type { PeriodState, PresetId } from "../../lib/backtestV2/period";
import { PRESETS, shortWindowWarning, windowLabel } from "../../lib/backtestV2/period";
import type { SizingAction, SizingState } from "../../lib/backtestV2/sizing";
import { sizingMath } from "../../lib/backtestV2/sizing";
import {
  Divider, InlineSegmented, NoteLine, PillGroup, SectionCard, V2Field, V2Number,
  V2Select, V2Text, WarnBox, WindowChip, v2InputClass,
} from "./primitives";

type Params = Record<string, number | string | boolean>;
type SetParam = (param: string, v: number | string | boolean) => void;

/** One registry field → its control. */
function RenderField({ fld, params, setParam }: { fld: FieldSpec; params: Params; setParam: SetParam }) {
  const v = params[fld.param];
  const accent = false;
  if (fld.kind === "toggle") {
    return (
      <V2Field label={fld.label} hint={fld.hint}>
        <label className={`${v2InputClass} flex items-center gap-2 cursor-pointer`}>
          <input type="checkbox" checked={Boolean(v)}
            onChange={(e) => setParam(fld.param, e.target.checked)} />
          <span className="text-[12px] font-medium text-[var(--muted)]">{v ? "on" : "off"}</span>
        </label>
      </V2Field>
    );
  }
  return (
    <V2Field label={fld.label} hint={fld.hint} accent={accent}>
      {fld.kind === "number" ? (
        <V2Number value={Number(v ?? 0)} step={fld.step} onChange={(n) => setParam(fld.param, n)} />
      ) : fld.kind === "select" ? (
        <V2Select value={String(v ?? "")} options={fld.options ?? []}
          onChange={(s) => setParam(fld.param, s)} />
      ) : (
        <V2Text type={fld.kind === "time" ? "time" : "text"} value={String(v ?? "")}
          onChange={(s) => setParam(fld.param, s)} />
      )}
    </V2Field>
  );
}

function FieldGrid({ fields, params, setParam, cols = 4 }: {
  fields: FieldSpec[]; params: Params; setParam: SetParam; cols?: 3 | 4;
}) {
  if (!fields.length) return null;
  return (
    <div className={`grid gap-4 sm:grid-cols-2 ${cols === 3 ? "lg:grid-cols-3" : "lg:grid-cols-4"}`}>
      {fields.map((fld) => (
        <RenderField key={fld.param} fld={fld} params={params} setParam={setParam} />
      ))}
    </div>
  );
}

// ------------------------------------------------------------------ 01 Basics
export function BasicsSection({ name, notes, onName, onNotes, strategyId, strategies,
  onStrategy, basis, onBasis, spec, storeDays, appliedTemplate }: {
  name: string; notes: string; onName: (v: string) => void; onNotes: (v: string) => void;
  strategyId: string; strategies: string[]; onStrategy: (v: string) => void;
  basis: Basis; onBasis: (b: Basis) => void; spec: StrategyFormSpec;
  storeDays?: number | null; appliedTemplate?: string | null;
}) {
  return (
    <SectionCard n="01" title="Basics">
      <div className="grid gap-4 sm:grid-cols-2">
        <V2Field label="NAME">
          <V2Text value={name} onChange={onName} placeholder="e.g. NIFTY straddle 2024-26" />
        </V2Field>
        <V2Field label="NOTES">
          <V2Text value={notes} onChange={onNotes} placeholder="what you're testing / why" />
        </V2Field>
        <V2Field label="STRATEGY">
          <V2Select value={strategyId} onChange={onStrategy}
            options={strategies.map((s) => ({ value: s, label: s }))} />
        </V2Field>
        <V2Field label="DATA"
          hint={spec.bases.length === 1
            ? `${spec.id} runs on ${spec.bases[0] === "intraday" ? "the 1-min store" : "the EOD cache"} only`
            : undefined}>
          <V2Select value={basis} disabled={spec.bases.length === 1}
            onChange={(v) => onBasis(v as Basis)}
            options={spec.bases.map((b) => ({
              value: b,
              label: b === "intraday"
                ? `Intraday 1-min store${storeDays ? ` (${storeDays} days captured)` : ""}`
                : "EOD daily cache",
            }))} />
        </V2Field>
      </div>
      {appliedTemplate && (
        <div className="text-[12.5px] font-semibold" style={{ color: "var(--warn-text)" }}>
          ★ prefilled from your saved {appliedTemplate} template — edit anything below
        </div>
      )}
      {spec.note && <NoteLine>{spec.note}</NoteLine>}
    </SectionCard>
  );
}

// ------------------------------------------------- 02 Universe & period
export function UniversePeriodSection({ spec, basis, underlyings, onUnderlyings, period,
  onPeriod, window: win }: {
  spec: StrategyFormSpec; basis: Basis; underlyings: string[];
  onUnderlyings: (next: string[]) => void; period: PeriodState;
  onPeriod: (p: PeriodState) => void;
  window: { start: string; end: string; tradingDays: number | null } | null;
}) {
  const supported = spec.underlyings[basis] ?? [];
  const ALL = ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "MIDCPNIFTY"];
  const warn = shortWindowWarning(spec.monthlyCycle, period.preset);
  return (
    <SectionCard n="02" title="Universe & period">
      <div>
        <div className="text-[11.5px] font-extrabold tracking-[.05em] text-[var(--faint)] mb-[7px]">
          UNDERLYING — SELECT ONE OR MORE
        </div>
        <PillGroup multi selected={underlyings} onChange={onUnderlyings}
          options={ALL.map((u) => ({
            value: u, label: u,
            disabled: !supported.includes(u),
            title: supported.includes(u) ? undefined
              : `no ${basis === "intraday" ? "captured 1-min bars" : "cached chain history"} for ${u} on this strategy`,
          }))} />
        {underlyings.length > 1 && (
          <div className="mt-2 text-[11.5px] text-[var(--faint)]">
            {underlyings.length} underlyings → {underlyings.length} runs, executed one after another, compared side by side.
          </div>
        )}
      </div>
      <Divider />
      <div>
        <div className="text-[11.5px] font-extrabold tracking-[.05em] text-[var(--faint)] mb-[7px]">
          PERIOD
        </div>
        <PillGroup selected={[period.preset]}
          onChange={([v]) => onPeriod({ ...period, preset: v as PresetId })}
          options={PRESETS.map((p) => ({ value: p.id, label: p.label }))} />
      </div>
      {period.preset === "CUSTOM" && (
        <div className="grid gap-4 sm:grid-cols-2">
          <V2Field label="START DATE">
            <V2Text type="date" value={period.customStart}
              onChange={(v) => onPeriod({ ...period, customStart: v })} />
          </V2Field>
          <V2Field label="END DATE">
            <V2Text type="date" value={period.customEnd}
              onChange={(v) => onPeriod({ ...period, customEnd: v })} />
          </V2Field>
        </div>
      )}
      {win ? <WindowChip>{windowLabel(win)}</WindowChip> : (
        <div className="text-[12px] text-[var(--faint)]">
          {period.preset === "CUSTOM" ? "Pick both dates." : "Waiting for data coverage…"}
        </div>
      )}
      {warn && <WarnBox>{warn}</WarnBox>}
    </SectionCard>
  );
}

// ---------------------------------------------------------------- 03 Sizing
export function SizingSection({ spec, sizing, dispatch }: {
  spec: StrategyFormSpec; sizing: SizingState; dispatch: (a: SizingAction) => void;
}) {
  const math = sizingMath(sizing);
  const marginLabel = spec.sizing === "hni" ? "MARGIN / LOT-SET (₹)" : "MARGIN / LOT (₹)";
  const marginHint = spec.sizing === "eodRatio"
    ? "reference only on EOD — this family fits lots off its own model margin"
    : "today's broker margin for one lot-set of this structure";
  return (
    <SectionCard n="03" title="Sizing"
      caption="at today's lot size & margin — past eras rescale automatically">
      <div className="text-[12.5px] text-[var(--muted)]">
        Enter margin per lot, then set <b>either</b> lots or capital — the other fills in
        (margin × lots × buffer).
      </div>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <V2Field label={marginLabel} hint={marginHint}>
          <V2Number value={sizing.margin} onChange={(v) => dispatch({ type: "margin", v })} />
        </V2Field>
        <V2Field label="LOTS" tag={sizing.capitalDriven ? "· AUTO" : undefined}
          accent={!sizing.capitalDriven}
          hint={sizing.capitalDriven ? "auto — floor(capital ÷ margin incl. buffer)"
                                     : "you set lots → capital fills in"}>
          <V2Number value={sizing.lots} onChange={(v) => dispatch({ type: "lots", v })} />
        </V2Field>
        <V2Field label="CAPITAL (₹)" tag={sizing.capitalDriven ? undefined : "· AUTO"}
          accent={sizing.capitalDriven}
          hint={sizing.capitalDriven ? "you set capital → lots fill in"
                                     : "auto — margin × lots × buffer"}>
          <V2Number value={sizing.capital} onChange={(v) => dispatch({ type: "capital", v })} />
        </V2Field>
        <V2Field label="BUFFER %" hint="headroom kept free">
          <V2Number value={sizing.buffer} step="any" onChange={(v) => dispatch({ type: "buffer", v })} />
        </V2Field>
      </div>
      {math.capitalShort != null && (
        <WarnBox>
          Capital below one lot-set ({formatInr(math.capitalShort)} incl. buffer) — 0 lots would trade.
        </WarnBox>
      )}
      <Divider />
      <div>
        <div className="text-[11.5px] font-extrabold tracking-[.05em] text-[var(--faint)] mb-[7px]">
          SIZING MODE
        </div>
        <InlineSegmented value={sizing.mode}
          onChange={(v) => dispatch({ type: "mode", v: v as "fixed" | "capital" })}
          options={[{ value: "fixed", label: "Fixed lots" },
                    { value: "capital", label: "Capital — refit lots" }]}
          hint={sizing.mode === "fixed" ? "same lot count every entry"
                                        : "lots refit to equity each flat day (era-true margins)"} />
      </div>
    </SectionCard>
  );
}

// ----------------------------------------------------------------- 04 Entry
export function EntrySection({ spec, basis, params, setParam }: {
  spec: StrategyFormSpec; basis: Basis; params: Params; setParam: SetParam;
}) {
  const FREQ = [{ value: "daily", label: "Every day" }, { value: "weekly", label: "Weekly" },
                { value: "monthly", label: "Monthly" }];
  return (
    <SectionCard n="04" title="Entry">
      <div>
        <div className="text-[11.5px] font-extrabold tracking-[.05em] text-[var(--faint)] mb-[7px]">
          FREQUENCY
        </div>
        <InlineSegmented value={spec.entry.frequency} onChange={() => {}} disabled
          options={FREQ} hint={spec.entry.frequencyHint} />
      </div>
      <FieldGrid fields={visibleFields(spec.entry.fields, basis, params)}
        params={params} setParam={setParam} />
    </SectionCard>
  );
}

// ------------------------------------------------------------------ 05 Exit
export function ExitSection({ spec, basis, params, setParam }: {
  spec: StrategyFormSpec; basis: Basis; params: Params; setParam: SetParam;
}) {
  const t = spec.exit.trail;
  const trailUi = String(params[TRAIL_UI] ?? "off");
  const trailHint = trailUi === "off" ? "exit only on target / stop / time"
    : trailUi === "ratchet" ? "locks profit floors as P&L climbs"
    : "stop follows P&L at a fixed distance";
  const trailExplain = trailUi === "ratchet"
    ? "Each time P&L gains a trigger-step, the stop floor ratchets up by the lock step — it never gives back locked profit."
    : "Once P&L exceeds the trigger, the stop trails the peak by the step — gives room but follows the high-water mark.";
  return (
    <SectionCard n="05" title="Exit" caption={spec.exit.basisNote}>
      {spec.exit.emptyNote && (
        <div className="text-[12.5px] text-[var(--muted)]">{spec.exit.emptyNote}</div>
      )}
      <FieldGrid fields={visibleFields(spec.exit.fields, basis, params)}
        params={params} setParam={setParam} />
      {t && (
        <>
          <Divider />
          <div>
            <div className="text-[11.5px] font-extrabold tracking-[.05em] text-[var(--faint)] mb-[7px]">
              TRAIL MODE
            </div>
            <InlineSegmented value={trailUi} onChange={(v) => setParam(TRAIL_UI, v)}
              options={[{ value: "off", label: "Off" }, { value: "ratchet", label: "Ratchet" },
                        { value: "below_peak", label: "Trail" }]}
              hint={trailHint} />
          </div>
          {trailUi !== "off" && (
            <>
              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                <V2Field label={trailUi === "ratchet" ? "RATCHET TRIGGER %" : "TRAIL TRIGGER %"}>
                  <V2Number value={Number(params[t.trigger] ?? 1)} step="any"
                    onChange={(v) => setParam(t.trigger, v)} />
                </V2Field>
                <V2Field label={trailUi === "ratchet" ? "LOCK STEP %" : "TRAIL STEP %"}>
                  <V2Number value={Number(params[t.step] ?? 0.5)} step="any"
                    onChange={(v) => setParam(t.step, v)} />
                </V2Field>
              </div>
              <div className="text-[12px] text-[var(--faint)] leading-relaxed">{trailExplain}</div>
            </>
          )}
        </>
      )}
    </SectionCard>
  );
}

// ------------------------------------------------------- 06 Strategy params
export function StrategyParamsSection({ spec, basis, params, setParam }: {
  spec: StrategyFormSpec; basis: Basis; params: Params; setParam: SetParam;
}) {
  const fields = visibleFields(spec.extras, basis, params);
  return (
    <SectionCard n="06" title="Strategy params"
      caption={<span className="font-['Space_Grotesk'] font-semibold text-[13px] text-[var(--accent-deep)]">{spec.id}</span>}>
      {fields.length ? (
        <FieldGrid fields={fields} params={params} setParam={setParam} cols={3} />
      ) : (
        <div className="text-[12.5px] text-[var(--muted)]">
          This strategy has no extra parameters — entry & exit above cover it.
        </div>
      )}
    </SectionCard>
  );
}
