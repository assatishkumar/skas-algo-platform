import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

/** Calm-Hero shared UI primitives (token-driven). Used by the Backtest / Live / Trade redesign. */

/** Consistent "you can't screen/deploy yet" notice for broker-session-gated pages.
 * Distinguishes a dead backend (the brokers query errored) from simply having no
 * logged-in Zerodha session, so a blocked screener/deploy shows a clear REASON
 * instead of a silently-disabled button or a perpetual "Computing…". Renders
 * nothing once a live session exists. */
export function SessionBanner({
  backendDown,
  hasSession,
  needs = "Live chains, pricing and margins",
  className = "",
}: {
  backendDown: boolean;
  hasSession: boolean;
  needs?: string;
  className?: string;
}) {
  if (!backendDown && hasSession) return null;
  return (
    <div
      className={`rounded-[12px] px-3 py-2 text-sm ${className}`}
      style={{ background: "var(--warn-bg)", color: "var(--warn-text)" }}
    >
      {backendDown ? (
        <>
          <span className="font-semibold">Backend unreachable.</span> The API server isn't responding
          — start it (<code>skas-algo</code> on :8080), then retry. Cached tables may still show.
        </>
      ) : (
        <>
          <span className="font-semibold">No logged-in broker session.</span> {needs} need a live
          Zerodha session.{" "}
          <Link to="/brokers" className="underline">Log in on Brokers →</Link>
        </>
      )}
    </div>
  );
}

export function Segmented<T extends string>({
  value,
  onChange,
  options,
}: {
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label: ReactNode }[];
}) {
  return (
    <div className="inline-flex rounded-[11px] bg-[var(--seg)] p-[3px]">
      {options.map((o) => (
        <button
          key={o.value}
          onClick={() => onChange(o.value)}
          className={`px-3 py-1.5 rounded-[9px] text-sm font-medium ${
            value === o.value ? "bg-[var(--accent)] text-white" : "text-[var(--muted)] hover:text-[var(--strong)]"
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Tag({
  children,
  bg = "var(--chip)",
  color = "var(--chip-text)",
  className = "",
  title,
}: {
  children: ReactNode;
  bg?: string;
  color?: string;
  className?: string;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 rounded-[7px] px-1.5 py-0.5 text-[11.5px] font-medium whitespace-nowrap ${className}`}
      style={{ background: bg, color }}
    >
      {children}
    </span>
  );
}

export interface MenuItem {
  label: string;
  onClick: () => void;
  tone?: "danger" | "warn";
}

export function KebabMenu({ items }: { items: MenuItem[] }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const h = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, []);
  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-9 h-9 rounded-[10px] bg-[var(--chip)] text-[var(--chip-text)] hover:opacity-80 flex items-center justify-center"
        aria-label="More actions"
      >
        ⋯
      </button>
      {open && (
        <div className="absolute right-0 mt-1 w-[180px] rounded-xl border border-[var(--border)] bg-[var(--menu)] py-1 shadow-[0_14px_34px_rgba(0,0,0,.18)] z-30 text-sm">
          {items.map((it, i) => (
            <button
              key={i}
              onClick={() => { setOpen(false); it.onClick(); }}
              className={`w-full text-left px-3 py-1.5 hover:bg-[var(--row-hover)] ${
                it.tone === "danger" ? "text-[var(--danger)]" : it.tone === "warn" ? "text-[var(--warn-text)]" : "text-[var(--strong)]"
              }`}
            >
              {it.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** Smoothed line + faint area-fill sparkline (`--pos` up / `--danger` down). */
export function Sparkline({
  values,
  up,
  height = 40,
}: {
  values: number[];
  up?: boolean;
  height?: number;
}) {
  const gid = useId().replace(/:/g, "");
  if (!values || values.length < 2) return null;
  const W = 300;
  const color = up === false ? "var(--danger)" : "var(--pos)";
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const n = values.length;
  const pad = 2;
  const x = (i: number) => (i / (n - 1)) * W;
  const y = (v: number) => height - pad - ((v - min) / span) * (height - 2 * pad);
  let d = `M${x(0).toFixed(1)} ${y(values[0]).toFixed(1)}`;
  for (let i = 1; i < n; i++) {
    const cx = (x(i - 1) + x(i)) / 2;
    d += ` C${cx.toFixed(1)} ${y(values[i - 1]).toFixed(1)} ${cx.toFixed(1)} ${y(values[i]).toFixed(1)} ${x(i).toFixed(1)} ${y(values[i]).toFixed(1)}`;
  }
  return (
    <svg viewBox={`0 0 ${W} ${height}`} className="w-full block" style={{ height }} preserveAspectRatio="none">
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor={color} stopOpacity="0.12" />
          <stop offset="1" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={`${d} L${W} ${height} L0 ${height} Z`} fill={`url(#${gid})`} />
      <path d={d} fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** Token-styled card surface. */
export function Panel({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <div className={`rounded-[18px] border border-[var(--border)] bg-[var(--card)] ${className}`}>
      {children}
    </div>
  );
}
