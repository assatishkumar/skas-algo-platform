import { formatInr } from "./format";

/** The margin-related fields a snapshot or a tile's metrics carry. */
export interface MarginFields {
  margin_used?: number | null;
  margin_source?: string | null; // "zerodha" | "dhan" | "model" | cache
  margin_via?: string | null; // "reference" = Kite priced a Dhan run's legs
  margin_via_label?: string | null; // which Kite account priced it
  margin_dhan_sum?: number | null; // Dhan's own per-leg sum, kept as a footnote
  threshold_base?: number | null; // the frozen/manual anchor the %-rules read
  threshold_source?: string | null; // "manual" | "broker" | …
}

export interface MarginDisplay {
  value: number | null; // the headline figure
  label: string | null; // one line under it
  note: string | null; // the longer explanation (KPI band only)
  footnote: string | null; // Dhan's per-leg sum when it is not the headline
}

/** ONE rule for what "Margin" means on a tile, so the header and the KPI band agree.
 *
 *  Zerodha runs: Kite's basket. Dhan runs: Dhan has no basket API — its figure is every
 *  short leg's standalone margin added up, so a hedged book reads at more than double what
 *  the broker blocks (run 27: ₹13.44L for a ₹5.7L structure). So on a Dhan run the
 *  headline is Kite's basket when a Kite session priced the same legs; else the manual
 *  anchor when the strategy has one; and only when neither exists, Dhan's own sum — said
 *  plainly. Owner call, 2026-09-04. */
export function marginDisplay(m: MarginFields): MarginDisplay {
  const used = m.margin_used ?? null;
  const src = m.margin_source ?? null;
  if (src === "zerodha" && m.margin_via === "reference") {
    return {
      value: used,
      label: `Kite basket · reference${m.margin_via_label ? ` (${m.margin_via_label})` : ""}`,
      note: "This is a Dhan account: Dhan has no basket API, so Kite priced the same legs with hedge benefit. Dhan's own per-leg sum is below.",
      footnote: m.margin_dhan_sum != null ? `Dhan per-leg sum ${formatInr(m.margin_dhan_sum)} · no hedge benefit` : null,
    };
  }
  if (src === "zerodha") return { value: used, label: "Zerodha basket", note: null, footnote: null };
  if (src === "dhan") {
    if (m.threshold_source === "manual" && m.threshold_base != null) {
      return {
        value: m.threshold_base,
        label: "manual anchor (margin per set × sets)",
        note: "No Kite session to price the basket, and Dhan has no basket API — the anchor stands in for the blocked margin. Log in to Kite for a netted figure.",
        footnote: used != null ? `Dhan per-leg sum ${formatInr(used)} · no hedge benefit` : null,
      };
    }
    return {
      value: used,
      label: "Dhan · each short leg added up, no hedge benefit",
      note: "Dhan has no basket API, so this counts every short leg as if it stood alone — the real blocked margin on a hedged book is lower. Log in to Kite for a netted reference.",
      footnote: null,
    };
  }
  if (src === "model") return { value: used, label: "model estimate", note: null, footnote: null };
  return { value: used, label: null, note: null, footnote: null };
}
