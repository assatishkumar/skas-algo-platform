import { NumberInput } from "../ui";

/** Backtest v2 form primitives (design_handoff_backtest_v2). The design's own idiom —
 *  numbered section cards, uppercase field labels with hints, toggle pills, an in-form
 *  segmented control — none of which the older redesign primitives cover:
 *  `redesign.tsx`'s Segmented is the accent-active NAV variant, while the form wants a
 *  `--seg` track whose active pill is card-bg + strong text. */

export function SectionCard({ n, title, caption, children }: {
  n: string; title: string; caption?: React.ReactNode; children: React.ReactNode;
}) {
  return (
    <div className="rounded-[18px] border border-[var(--border)] bg-[var(--card)] p-6 mb-[18px]">
      <div className="flex items-baseline gap-2.5 mb-[18px]">
        <span className="font-['Space_Grotesk'] font-bold text-[13px] text-[var(--accent-deep)]">{n}</span>
        <span className="font-['Space_Grotesk'] font-bold text-[16px] text-[var(--strong)]">{title}</span>
        {caption && <span className="ml-auto text-[12.5px] text-[var(--faint)]">{caption}</span>}
      </div>
      <div className="space-y-[18px]">{children}</div>
    </div>
  );
}

export const v2InputClass =
  "w-full rounded-[12px] bg-[var(--field)] border-[1.5px] border-[var(--field-border)] px-3.5 py-3 " +
  "font-['Manrope'] font-semibold text-[15px] text-[var(--strong)] outline-none " +
  "focus:border-[var(--accent)] disabled:opacity-60";

/** Uppercase label · optional "· AUTO" tag · control · optional hint. `accent` draws the
 *  border in accent — the design marks the USER-DRIVEN field of a coupled pair. */
export function V2Field({ label, tag, hint, accent, children }: {
  label: string; tag?: string; hint?: React.ReactNode; accent?: boolean;
  children: React.ReactNode;
}) {
  return (
    <label className="block">
      <span className="block text-[11.5px] font-extrabold tracking-[.05em] text-[var(--faint)] mb-[7px]">
        {label}
        {tag ? <span className="text-[var(--accent-deep)]"> {tag}</span> : null}
      </span>
      <div className={accent ? "[&>input]:border-[var(--accent)] [&>select]:border-[var(--accent)]" : ""}>
        {children}
      </div>
      {hint && <span className="mt-[7px] block text-[11.5px] text-[var(--faint)]">{hint}</span>}
    </label>
  );
}

export function V2Number({ value, onChange, step, disabled }: {
  value: number; onChange: (n: number) => void; step?: string; disabled?: boolean;
}) {
  return <NumberInput className={v2InputClass} value={value} onChange={onChange}
    step={step} disabled={disabled} />;
}

export function V2Select({ value, onChange, options, disabled }: {
  value: string; onChange: (v: string) => void;
  options: { value: string; label: string }[]; disabled?: boolean;
}) {
  return (
    <select className={v2InputClass} value={value} disabled={disabled}
      onChange={(e) => onChange(e.target.value)}>
      {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  );
}

export function V2Text({ value, onChange, placeholder, type = "text", disabled }: {
  value: string; onChange: (v: string) => void; placeholder?: string;
  type?: "text" | "time" | "date"; disabled?: boolean;
}) {
  return (
    <input className={v2InputClass} type={type} value={value} placeholder={placeholder}
      disabled={disabled} onChange={(e) => onChange(e.target.value)} />
  );
}

export interface PillOption { value: string; label: string; disabled?: boolean; title?: string }

/** Toggle pills — multi-select (underlyings; deselecting the LAST is a no-op) or
 *  single-select (period presets). Unsupported options render disabled rather than
 *  hidden, so the absence of data reads as a fact, not a missing feature. */
export function PillGroup({ options, selected, onChange, multi = false }: {
  options: PillOption[]; selected: string[]; onChange: (next: string[]) => void; multi?: boolean;
}) {
  return (
    <div className="flex flex-wrap gap-2">
      {options.map((o) => {
        const on = selected.includes(o.value);
        return (
          <button key={o.value} type="button" disabled={o.disabled} title={o.title}
            onClick={() => {
              if (o.disabled) return;
              if (!multi) return onChange([o.value]);
              const next = on ? selected.filter((v) => v !== o.value) : [...selected, o.value];
              if (next.length) onChange(next);   // never allow an empty selection
            }}
            className="rounded-full border px-3.5 py-2 text-[13px] font-bold transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            style={{
              background: on ? "var(--tint)" : "transparent",
              borderColor: on ? "var(--accent)" : "var(--field-border)",
              color: on ? "var(--accent-deep)" : "var(--chip-text)",
            }}>
            {on && multi ? "✓ " : ""}{o.label}
          </button>
        );
      })}
    </div>
  );
}

/** In-form segmented (`--seg` track; active = card bg + strong text). `disabled` renders
 *  the control as a read-only statement of the strategy's nature (e.g. a monthly-cycle
 *  strategy can't be switched to daily — there is no such knob). */
export function InlineSegmented({ options, value, onChange, disabled, hint }: {
  options: { value: string; label: string }[]; value: string;
  onChange: (v: string) => void; disabled?: boolean; hint?: React.ReactNode;
}) {
  return (
    <div className="flex items-center gap-3 flex-wrap">
      <div className="inline-flex rounded-[11px] bg-[var(--seg)] p-[3px]"
        title={disabled ? "fixed by this strategy" : undefined}>
        {options.map((o) => {
          const on = value === o.value;
          return (
            <button key={o.value} type="button" disabled={disabled}
              onClick={() => onChange(o.value)}
              className={`rounded-[9px] px-4 py-2 text-[13.5px] font-bold transition-colors ${
                disabled ? "cursor-default" : ""} ${on ? "" : "opacity-90"}`}
              style={{
                background: on ? "var(--card)" : "transparent",
                color: on ? "var(--strong)" : "var(--muted)",
              }}>
              {o.label}
            </button>
          );
        })}
      </div>
      {hint && <span className="text-[11.5px] text-[var(--faint)]">{hint}</span>}
    </div>
  );
}

export function Divider() {
  return <div className="h-px bg-[var(--divider)]" />;
}

/** Amber advisory (short window vs a monthly cycle, capital below one lot-set). */
export function WarnBox({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-[12px] px-4 py-3 text-[12.5px] leading-relaxed"
      style={{ background: "var(--warn-bg)", color: "var(--warn-text)" }}>
      {children}
    </div>
  );
}

/** The per-strategy behaviour note (design: orange `--note`). */
export function NoteLine({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[13.5px] font-semibold leading-[1.55]" style={{ color: "var(--note)" }}>
      {children}
    </div>
  );
}

/** Resolved-window chip: tint bg + calendar glyph. */
export function WindowChip({ children }: { children: React.ReactNode }) {
  return (
    <div className="inline-flex items-center gap-2 rounded-[10px] border px-3 py-2 text-[12.5px] font-semibold"
      style={{ background: "var(--tint)", borderColor: "var(--tint-border)", color: "var(--accent-deep)" }}>
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0">
        <rect x="3" y="4" width="18" height="18" rx="2" /><path d="M16 2v4M8 2v4M3 10h18" />
      </svg>
      {children}
    </div>
  );
}
