/** Shared chart-card shell for the Analyze workbench: numbered title, grey subtitle,
 *  "?" help toggle with the verbatim explainer, optional orange insight note. */

import { useState, type ReactNode } from "react";
import { PANEL_HELP } from "../../lib/analyzeHelp";

export default function Panel({ num, title, subtitle, help, insight, wide, children }: {
  num?: string;                 // "5.2" → pulls PANEL_HELP[num] unless `help` given
  title: string;
  subtitle?: string;
  help?: string;
  insight?: string | null;
  wide?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const helpText = help ?? (num ? PANEL_HELP[num] : undefined);
  return (
    <div className="rounded-[18px] border border-[var(--border)] bg-[var(--card)] p-5"
      style={wide ? { gridColumn: "1 / -1" } : undefined}>
      <div className="flex items-center gap-2">
        <span className="font-['Space_Grotesk'] font-bold text-[14.5px] text-[var(--strong)]">
          {num ? `${num} — ` : ""}{title}
        </span>
        {helpText && (
          <button onClick={() => setOpen((o) => !o)} title="How to read this panel"
            className="w-[21px] h-[21px] rounded-full text-[12px] font-extrabold inline-flex items-center justify-center flex-none"
            style={{ background: open ? "var(--accent)" : "var(--chip)", color: open ? "#fff" : "var(--chip-text)" }}>?</button>
        )}
      </div>
      {subtitle && (
        <div className="text-[12px] text-[var(--faint)] font-semibold mt-1 leading-[1.5]">{subtitle}</div>
      )}
      {open && helpText && (
        <div className="mt-2.5 rounded-[10px] border border-[var(--tint-border)] bg-[var(--tint)] px-3.5 py-[11px] text-[12.5px] font-semibold leading-[1.6] text-[var(--ft)]">
          {helpText}
        </div>
      )}
      {insight && (
        <div className="mt-2 text-[12px] font-bold leading-[1.55]" style={{ color: "var(--note, #c2661d)" }}>{insight}</div>
      )}
      <div className="mt-3">{children}</div>
    </div>
  );
}
