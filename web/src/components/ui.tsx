import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

/** A number input you can actually clear and retype — keeps local text so an empty
 * field doesn't snap back to 0, and only pushes a parsed number to the parent.
 * PROGRAMMATIC ``value`` changes (template prefill, per-mode defaults) must reach the
 * DOM, so the text re-syncs whenever the prop changes while the field isn't focused —
 * only the user's own in-progress typing may hold the displayed text. */
export function NumberInput({
  value,
  onChange,
  className = "",
  step,
  placeholder,
  disabled,
}: {
  value: number;
  onChange: (n: number) => void;
  className?: string;
  step?: string;
  placeholder?: string;
  disabled?: boolean;
}) {
  const [text, setText] = useState(String(value));
  const focused = useRef(false);
  useEffect(() => {
    if (!focused.current && Number(text) !== value) setText(String(value));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);
  return (
    <input
      type="number"
      inputMode="decimal"
      className={className}
      step={step}
      placeholder={placeholder}
      disabled={disabled}
      value={text}
      onChange={(e) => {
        const raw = e.target.value;
        setText(raw);
        if (raw !== "" && !Number.isNaN(Number(raw))) onChange(Number(raw));
      }}
      onFocus={() => {
        focused.current = true;
      }}
      onBlur={() => {
        focused.current = false;
        if (text === "" || Number.isNaN(Number(text))) setText(String(value));
        else if (Number(text) !== value) setText(String(value));
      }}
    />
  );
}

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <div className={`rounded-xl border border-slate-800 bg-slate-900/50 p-4 ${className}`}>
      {children}
    </div>
  );
}

export function MetricCard({
  label,
  value,
  tone = "default",
}: {
  label: string;
  value: ReactNode;
  tone?: "default" | "good" | "bad";
}) {
  const color =
    tone === "good" ? "text-emerald-400" : tone === "bad" ? "text-rose-400" : "text-slate-100";
  return (
    <Card>
      <div className="text-xs uppercase tracking-wide text-slate-400">{label}</div>
      <div className={`mt-1 text-xl font-semibold ${color}`}>{value}</div>
    </Card>
  );
}

export function Spinner({ label = "Loading…" }: { label?: string }) {
  return <div className="text-slate-400 py-8 text-center">{label}</div>;
}

export function ErrorBox({ message }: { message: string }) {
  return (
    <div className="rounded-lg border border-rose-800 bg-rose-950/40 text-rose-300 p-3 text-sm">
      {message}
    </div>
  );
}

export function Badge({ children }: { children: ReactNode }) {
  return (
    <span className="inline-block rounded-full bg-slate-800 px-2 py-0.5 text-xs text-slate-300">
      {children}
    </span>
  );
}

const STATUS_STYLE: Record<string, string> = {
  active: "bg-emerald-900/40 text-emerald-300 border border-emerald-700/50",
  stopped: "bg-slate-700/50 text-slate-300 border border-slate-600/50",
  archived: "bg-amber-900/30 text-amber-300 border border-amber-700/40",
};

/** Small colored status chip shared by the Live and Runs dashboards. */
export function StatusPill({ status }: { status: string }) {
  return (
    <span className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${STATUS_STYLE[status] ?? ""}`}>
      {status}
    </span>
  );
}

/** Relative "x ago" for an ISO timestamp. */
export function timeAgo(iso: string | null): string {
  if (!iso) return "—";
  const secs = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (secs < 60) return "just now";
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}
