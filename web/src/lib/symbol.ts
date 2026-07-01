const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Turn "2026-07-07" into "7 Jul '26"; pass anything unexpected through unchanged. */
function prettyExpiry(iso: string): string {
  const p = (iso ?? "").split("-"); // YYYY-MM-DD
  if (p.length !== 3) return iso;
  const mon = MONTHS[Number(p[1]) - 1];
  if (!mon) return iso;
  return `${Number(p[2])} ${mon} '${p[0].slice(2)}`;
}

/** Human-readable label for an internal option ticker "UNDERLYING|YYYY-MM-DD|STRIKE|RIGHT",
 *  e.g. `NIFTY 24500 CE · 7 Jul '26`. The raw pipe form renders as `NIFTYI2026-07-07I…` (the `|`
 *  reads as an `I`), so never show it directly. Non-option symbols — equity tickers, or any string
 *  that isn't the 4-part pipe form — pass through unchanged, so this is safe to apply anywhere a
 *  symbol is displayed. Set `expiry:false` to drop the trailing date (when the table already
 *  shows one expiry for every row). */
export function formatOptionSymbol(symbol: string, opts?: { expiry?: boolean }): string {
  const parts = (symbol ?? "").split("|");
  if (parts.length !== 4) return symbol ?? "";
  const [underlying, expiry, strike, right] = parts;
  const strikeNum = Number(strike);
  const strikeLabel = Number.isFinite(strikeNum) ? String(strikeNum) : strike; // "24500.0" → "24500"
  const head = `${underlying} ${strikeLabel} ${right}`;
  return opts?.expiry === false ? head : `${head} · ${prettyExpiry(expiry)}`;
}

export interface ParsedOption {
  underlying: string;
  expiry: string; // YYYY-MM-DD
  strike: number;
  right: string; // CE | PE
}

/** Parse an internal option ticker; null for non-option (equity) symbols. */
export function parseOptionSymbol(symbol: string): ParsedOption | null {
  const parts = (symbol ?? "").split("|");
  if (parts.length !== 4) return null;
  const strike = Number(parts[2]);
  if (!Number.isFinite(strike)) return null;
  return { underlying: parts[0], expiry: parts[1], strike, right: parts[3] };
}

/** Sort comparator for a "symbol" column: order option legs by underlying → expiry → strike →
 *  right (so strikes read in numeric order, not the lexical order of the raw ticker where
 *  "24500" sorts before "9500"). Falls back to a plain string compare when either side isn't an
 *  option (equity tickers). */
export function compareOptionSymbol(a: string, b: string): number {
  const pa = parseOptionSymbol(a);
  const pb = parseOptionSymbol(b);
  if (!pa || !pb) return String(a ?? "").localeCompare(String(b ?? ""));
  return (
    pa.underlying.localeCompare(pb.underlying) ||
    pa.expiry.localeCompare(pb.expiry) ||
    pa.strike - pb.strike ||
    pa.right.localeCompare(pb.right)
  );
}
