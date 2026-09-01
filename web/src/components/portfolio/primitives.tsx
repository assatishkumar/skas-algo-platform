import { useEffect, useRef, useState, type ReactNode } from "react";

/** Portfolio-screen primitives, token-driven to match the design handoff exactly.
 *
 * These are local rather than shared because the handoff's segmented control differs from
 * the Calm-Hero one already in `redesign.tsx` (card-bg active with a soft shadow, not an
 * accent fill), and quietly changing that component would restyle the Backtest, Live and
 * Trade pages too. */

export function Card({
  children, className = "", pad = "p-[18px_20px]", style,
}: {
  children: ReactNode; className?: string; pad?: string; style?: React.CSSProperties;
}) {
  return (
    <div
      className={`rounded-[16px] border border-[var(--border)] bg-[var(--card)] ${pad} ${className}`}
      style={style}
    >
      {children}
    </div>
  );
}

/** A metric tile. `help` is mandatory in spirit: every KPI on this screen explains not just
 * what it is but how to judge it — a number nobody can act on is decoration. */
export function Kpi({
  label, value, sub, valueColor = "var(--strong)", subColor = "var(--faint)", help, size = 21,
}: {
  label: string; value: ReactNode; sub?: ReactNode;
  valueColor?: string; subColor?: string; help: string; size?: number;
}) {
  return (
    <div
      title={help}
      className="cursor-help rounded-[16px] border border-[var(--border)] bg-[var(--card)] px-[17px] py-[15px]"
    >
      <div className="mb-1.5 text-[11px] font-extrabold tracking-[.05em] text-[var(--faint)]">
        {label}
      </div>
      <div
        className="font-[700] [font-family:'Space_Grotesk',system-ui,sans-serif]"
        style={{ color: valueColor, fontSize: size }}
      >
        {value}
      </div>
      {sub !== undefined && (
        <div className="mt-[3px] text-[11.5px] font-semibold" style={{ color: subColor }}>
          {sub}
        </div>
      )}
    </div>
  );
}

export function Segments<T extends string>({
  value, onChange, options, size = "md",
}: {
  value: T; onChange: (v: T) => void;
  options: { value: T; label: string }[]; size?: "sm" | "md";
}) {
  const pad = size === "sm" ? "px-[14px] py-1.5 text-[12.5px]" : "px-5 py-[9px] text-[13.5px]";
  return (
    <div className={`inline-flex rounded-[12px] bg-[var(--seg)] ${size === "sm" ? "p-[3px]" : "p-1"}`}>
      {options.map((o) => {
        const on = o.value === value;
        return (
          <button
            key={o.value}
            onClick={() => onChange(o.value)}
            className={`rounded-[9px] font-bold ${pad} ${
              on ? "bg-[var(--card)] text-[var(--strong)] shadow-[0_1px_4px_rgba(0,0,0,.08)]"
                 : "text-[var(--muted)] hover:text-[var(--strong)]"
            }`}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

export function Pill({
  children, bg = "var(--chip)", color = "var(--chip-text)", title, className = "",
}: {
  children: ReactNode; bg?: string; color?: string; title?: string; className?: string;
}) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1.5 rounded-full px-[11px] py-1 text-[11px] font-extrabold ${className}`}
      style={{ background: bg, color }}
    >
      {children}
    </span>
  );
}

export function Dot({ color, size = 9, radius = 3 }: { color: string; size?: number; radius?: number }) {
  return (
    <span
      className="inline-block shrink-0"
      style={{ width: size, height: size, borderRadius: radius, background: color }}
    />
  );
}

/** Two-step delete, inline. Never a native confirm() and never a one-click destroy — the
 * whole screen follows this: the row swaps to "Delete? Yes · No" in place, so the thing
 * being destroyed stays visible while you decide. */
export function InlineConfirm({
  prompt = "Delete?", onYes, onNo, className = "",
}: {
  prompt?: string; onYes: () => void; onNo: () => void; className?: string;
}) {
  return (
    <span className={`inline-flex items-center gap-2 text-[12px] font-extrabold text-[var(--danger)] ${className}`}>
      {prompt}
      <button onClick={onYes} className="underline hover:opacity-80">Yes</button>
      <button onClick={onNo} className="text-[var(--muted)] underline hover:opacity-80">No</button>
    </span>
  );
}

/** A destructive action that arms itself, then disarms if you look away. The armed state is
 * held here so a table of twenty rows doesn't need twenty pieces of parent state. */
export function ConfirmAction({
  label, prompt, onConfirm, className = "", armedClassName = "",
}: {
  label: ReactNode; prompt?: string; onConfirm: () => void;
  className?: string; armedClassName?: string;
}) {
  const [armed, setArmed] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  useEffect(() => {
    if (!armed) return;
    // Auto-disarm: an armed delete left on screen becomes a mis-click waiting to happen.
    timer.current = window.setTimeout(() => setArmed(false), 6000);
    return () => window.clearTimeout(timer.current);
  }, [armed]);

  if (armed) {
    return (
      <InlineConfirm
        prompt={prompt}
        className={armedClassName}
        onYes={() => { setArmed(false); onConfirm(); }}
        onNo={() => setArmed(false)}
      />
    );
  }
  return (
    <button onClick={() => setArmed(true)} className={className}>{label}</button>
  );
}

export function Field({
  label, children, span = 1,
}: { label: string; children: ReactNode; span?: number }) {
  return (
    <label className="block" style={{ gridColumn: `span ${span}` }}>
      <span className="text-[11px] font-extrabold tracking-[.04em] text-[var(--faint)]">
        {label}
      </span>
      <div className="mt-[5px]">{children}</div>
    </label>
  );
}

export const inputClass =
  "w-full rounded-[10px] border-[1.5px] border-[var(--field-border)] bg-[var(--field)] " +
  "px-[13px] py-2.5 text-[13.5px] font-semibold text-[var(--strong)] outline-none " +
  "focus:border-[var(--accent)]";

export const selectClass =
  "w-full rounded-[10px] border-[1.5px] border-[var(--field-border)] bg-[var(--field)] " +
  "px-[11px] py-2.5 text-[13px] font-bold text-[var(--strong)] outline-none " +
  "focus:border-[var(--accent)]";

/** A modal that closes on backdrop click and on Escape. Escape matters more than it looks:
 * this screen's modal is the only way to lose typed input, and reaching for the mouse to
 * dismiss it is exactly when a stray click lands on something else. */
export function Modal({
  title, onClose, children, width = 480,
}: { title: string; onClose: () => void; children: ReactNode; width?: number }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div
      onClick={onClose}
      className="fixed inset-0 z-[60] flex items-center justify-center p-6"
      style={{ background: "rgba(8,20,18,.45)" }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="max-h-[88vh] w-full overflow-y-auto rounded-[18px] border border-[var(--border)] bg-[var(--card)] p-6 shadow-[0_24px_64px_rgba(0,0,0,.25)]"
        style={{ maxWidth: width }}
      >
        <div className="mb-4 text-[19px] font-bold [font-family:'Space_Grotesk',system-ui,sans-serif] text-[var(--strong)]">
          {title}
        </div>
        {children}
      </div>
    </div>
  );
}

/** The screen's empty/degraded state. Says WHY there is nothing, and what to do about it —
 * this page fails closed rather than drawing a chart of invented numbers. */
export function Notice({
  tone = "info", children,
}: { tone?: "info" | "warn"; children: ReactNode }) {
  const bg = tone === "warn" ? "var(--warn-bg)" : "var(--tint)";
  const color = tone === "warn" ? "var(--warn-text)" : "var(--ft)";
  const border = tone === "warn" ? "transparent" : "var(--tint-border)";
  return (
    <div
      className="rounded-[12px] border px-4 py-2.5 text-[12.5px] font-bold"
      style={{ background: bg, color, borderColor: border }}
    >
      {children}
    </div>
  );
}
