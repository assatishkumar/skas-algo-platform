/** Backtest v2 — section 02's period presets, resolved against REAL data coverage.
 *
 *  The design's preset dates are sample data. Here the window is always anchored to what
 *  the selected basis actually holds — the 1-min store's captured span (intraday) or the
 *  chain cache's span (EOD) — so "Full history" means the real first day, and a preset can
 *  never ask for days the replay would just skip.
 */

export type PresetId = "3M" | "6M" | "1Y" | "2Y" | "3Y" | "ALL" | "CUSTOM";

export const PRESETS: { id: PresetId; label: string; months?: number }[] = [
  { id: "3M", label: "Last 3 months", months: 3 },
  { id: "6M", label: "Last 6 months", months: 6 },
  { id: "1Y", label: "1 year", months: 12 },
  { id: "2Y", label: "2 years", months: 24 },
  { id: "3Y", label: "3 years", months: 36 },
  { id: "ALL", label: "Full history" },
  { id: "CUSTOM", label: "Custom dates" },
];

export interface PeriodState { preset: PresetId; customStart: string; customEnd: string }
export interface Coverage { first: string | null; last: string | null }

const iso = (d: Date) => d.toISOString().slice(0, 10);

export function resolveWindow(p: PeriodState, cov: Coverage):
    { start: string; end: string; tradingDays: number | null } | null {
  if (p.preset === "CUSTOM") {
    if (!p.customStart || !p.customEnd) return null;
    return { start: p.customStart, end: p.customEnd, tradingDays: null };
  }
  if (!cov.first || !cov.last) return null;
  const end = cov.last;
  const preset = PRESETS.find((x) => x.id === p.preset);
  let start = cov.first;
  if (preset?.months) {
    const d = new Date(`${end}T00:00:00`);
    d.setMonth(d.getMonth() - preset.months);
    start = iso(d) < cov.first ? cov.first : iso(d);   // never ask before the data starts
  }
  const spanDays = (Date.parse(end) - Date.parse(start)) / 86_400_000;
  return { start, end, tradingDays: Math.max(1, Math.round((spanDays * 248) / 365)) };
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "17 Jul 2025" — the design's window-chip format. */
export function fmtDay(isoDate: string): string {
  const [y, m, d] = isoDate.split("-");
  return `${Number(d)} ${MONTHS[Number(m) - 1]} ${y}`;
}

export function windowLabel(w: { start: string; end: string; tradingDays: number | null }): string {
  const base = `${fmtDay(w.start)} → ${fmtDay(w.end)}`;
  return w.tradingDays ? `${base} · ~${w.tradingDays} trading days` : base;
}

/** A monthly-cycle strategy on a 3M/6M window truncates its cycles — the design's warning. */
export const MONTHLY_WINDOW_WARNING =
  "Monthly-cycle strategy: a full entry → expiry cycle needs ~2 months of captured days — a "
  + "short window means truncated cycles. force_entry makes it enter on the first replayed day.";

export function shortWindowWarning(monthlyCycle: boolean | undefined, preset: PresetId): string | null {
  return monthlyCycle && (preset === "3M" || preset === "6M") ? MONTHLY_WINDOW_WARNING : null;
}
