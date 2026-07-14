import { parseOptionSymbol } from "../lib/symbol";
import type { LiveRunSnapshot } from "../types";

/** Amber callout spelling out the strategy's live exit / adjustment rules, so the "why it exits"
 *  is a first-class panel rather than buried footnote text. Renders nothing without rules. */
export default function ExitCriteriaCallout({ run }: { run: LiveRunSnapshot }) {
  const expiry = (run.positions ?? [])
    .map((p) => parseOptionSymbol(p.symbol))
    .find((o) => o)?.expiry;
  const rules = [
    ...(run.exit_rules ?? []),
    ...(expiry ? [`Settles at expiry ${expiry}`] : []),
  ];
  if (!rules.length) return null;
  return (
    <div className="mt-3 flex items-start gap-2.5 rounded-[13px] border border-[var(--warn-text)] bg-[var(--warn-bg)] px-3.5 py-3 text-[13px] text-[var(--warn-text)]">
      <svg viewBox="0 0 24 24" width="18" height="18" className="mt-px shrink-0" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
        <line x1="12" y1="9" x2="12" y2="13" />
        <line x1="12" y1="17" x2="12.01" y2="17" />
      </svg>
      <div className="leading-relaxed">
        <span className="font-semibold">Exit &amp; adjustment:</span> {rules.join(" · ")}.
      </div>
    </div>
  );
}
