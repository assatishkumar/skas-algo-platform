/** Conditioner bucketing + stats for the Analyze workbench (hero 2 + the conditioning
 *  drill-in's cross-tab). All client-side over the bundle's trade rows. */

import type { AnalyticsTrade } from "../types";

export type Norm = "rs" | "cr" | "mg";
export type Leg = "comb" | "pe" | "ce";

export interface CondBucket { label: string; trades: AnalyticsTrade[] }
export interface CondDef {
  id: string;
  label: string;
  /** null = usable; a string explains why the chip is disabled for this run */
  disabled: (ts: AnalyticsTrade[]) => string | null;
  bucketize: (ts: AnalyticsTrade[]) => CondBucket[];
  footnote?: string;
}

const WD = ["Mon", "Tue", "Wed", "Thu", "Fri"];

function byRanges(ts: AnalyticsTrade[], val: (t: AnalyticsTrade) => number | null,
                  edges: number[], labels: string[]): CondBucket[] {
  const out = labels.map((label) => ({ label, trades: [] as AnalyticsTrade[] }));
  for (const t of ts) {
    const v = val(t);
    if (v == null) continue;
    let i = edges.findIndex((e) => v < e);
    if (i < 0) i = labels.length - 1;
    out[i].trades.push(t);
  }
  return out.filter((b) => b.trades.length > 0);
}

function byQuartiles(ts: AnalyticsTrade[], val: (t: AnalyticsTrade) => number | null,
                     name: string): CondBucket[] {
  const vals = ts.map(val).filter((v): v is number => v != null).sort((a, b) => a - b);
  if (vals.length < 8) return [];
  const q = (p: number) => vals[Math.min(vals.length - 1, Math.floor(p * vals.length))];
  const e = [q(0.25), q(0.5), q(0.75)];
  return byRanges(ts, val, e,
    [`${name} Q1 (low)`, `${name} Q2`, `${name} Q3`, `${name} Q4 (high)`]);
}

export const CONDITIONS: CondDef[] = [
  {
    id: "dte", label: "DTE at entry",
    disabled: (ts) => ts.some((t) => t.dte != null) ? null : "No expiry data",
    bucketize: (ts) => byRanges(ts, (t) => t.dte, [1, 2, 3],
      ["0 DTE", "1 DTE", "2 DTE", "3+ DTE"]),
  },
  {
    id: "cycleday", label: "Day in cycle",
    disabled: () => "Single-day cycles in this run",
    bucketize: () => [],
  },
  {
    id: "carry", label: "Weekend carry",
    disabled: () => "No overnight holds in this run",
    bucketize: () => [],
  },
  {
    id: "dow", label: "Day of week",
    disabled: () => null,
    bucketize: (ts) => WD.map((label, i) => ({
      label, trades: ts.filter((t) => t.weekday === i),
    })).filter((b) => b.trades.length > 0),
    footnote: "Weekday is mostly a proxy for DTE here — see the nested cross-tab in the Conditioning drill-in.",
  },
  {
    id: "ivhv", label: "IV−HV spread",
    disabled: (ts) => ts.some((t) => t.ivhv != null) ? null : "IV−HV not computable for this run",
    bucketize: (ts) => byRanges(ts, (t) => t.ivhv ?? null, [0, 2, 4],
      ["IV−HV < 0", "0 – 2", "2 – 4", "> 4 pts"]),
  },
  {
    id: "vixpct", label: "VIX percentile",
    disabled: (ts) => ts.some((t) => t.vix_entry != null) ? null : "No VIX context",
    bucketize: (ts) => byQuartiles(ts, (t) => t.vix_entry, "VIX"),
  },
  {
    id: "vixchg", label: "VIX change in trade",
    disabled: (ts) => ts.some((t) => t.vix_entry != null && t.vix_exit != null)
      ? null : "No VIX context",
    bucketize: (ts) => byRanges(ts,
      (t) => (t.vix_entry != null && t.vix_exit != null) ? t.vix_exit - t.vix_entry : null,
      [-0.5, 0, 0.5], ["VIX fell > 0.5", "drifted down", "drifted up", "rose > 0.5"]),
  },
  {
    id: "skew", label: "PE/CE skew",
    disabled: (ts) => ts.some((t) => t.skew_entry != null) ? null : "No two-sided structure",
    bucketize: (ts) => byRanges(ts, (t) => t.skew_entry, [0.8, 0.95, 1.1],
      ["Skew < 0.8", "0.8 – 0.95", "0.95 – 1.1", "> 1.1 (fear)"]),
  },
  {
    id: "entry", label: "Entry time",
    disabled: (ts) => new Set(ts.map((t) => t.entry_slot)).size > 1
      ? null : "Fixed entry time in this run",
    bucketize: (ts) => byRanges(ts,
      (t) => { const [h, m] = t.entry_slot.split(":").map(Number); return h * 60 + m; },
      [9 * 60 + 30, 11 * 60, 13 * 60],
      ["Open (≤09:30)", "09:30 – 11:00", "11:00 – 13:00", "After 13:00"]),
  },
  {
    id: "creditq", label: "Credit quartile",
    disabled: () => null,
    bucketize: (ts) => byQuartiles(ts, (t) => t.credit_pts, "Credit"),
  },
  {
    id: "event", label: "Event day",
    disabled: () => "No event calendar wired yet",
    bucketize: () => [],
  },
];

export function legPnl(t: AnalyticsTrade, leg: Leg): number {
  return leg === "pe" ? t.pnl_pe_rs : leg === "ce" ? t.pnl_ce_rs : t.pnl_rs;
}

export function normValue(t: AnalyticsTrade, leg: Leg, norm: Norm): number | null {
  const p = legPnl(t, leg);
  if (norm === "rs") return p;
  if (norm === "cr") return t.credit_rs > 0 ? (100 * p) / t.credit_rs : null;
  return t.margin_rs ? (100 * p) / t.margin_rs : null;
}

export interface BucketStats {
  label: string; n: number;
  mean: number; median: number; winPct: number; worst: number;
  romCycle: number | null;         // mean P&L as % of that trade's margin — always margin
  ci: [number, number];            // ±1.96 SE band on the mean (in the chosen norm)
  meanPctCredit: number | null;    // drives the diverging bar column
}

export function bucketStats(b: CondBucket, leg: Leg, norm: Norm): BucketStats | null {
  const vals = b.trades.map((t) => normValue(t, leg, norm))
    .filter((v): v is number => v != null);
  if (!vals.length) return null;
  const sorted = [...vals].sort((a, c) => a - c);
  const mean = vals.reduce((s, v) => s + v, 0) / vals.length;
  const sd = vals.length > 1
    ? Math.sqrt(vals.reduce((s, v) => s + (v - mean) ** 2, 0) / (vals.length - 1)) : 0;
  const se = sd / Math.sqrt(vals.length);
  const roms = b.trades.map((t) => t.margin_rs ? (100 * legPnl(t, leg)) / t.margin_rs : null)
    .filter((v): v is number => v != null);
  const crs = b.trades.map((t) => t.credit_rs > 0 ? (100 * legPnl(t, leg)) / t.credit_rs : null)
    .filter((v): v is number => v != null);
  return {
    label: b.label, n: b.trades.length,
    mean, median: sorted[Math.floor(sorted.length / 2)],
    winPct: (100 * b.trades.filter((t) => legPnl(t, leg) > 0).length) / b.trades.length,
    worst: sorted[0],
    romCycle: roms.length ? roms.reduce((s, v) => s + v, 0) / roms.length : null,
    ci: [mean - 1.96 * se, mean + 1.96 * se],
    meanPctCredit: crs.length ? crs.reduce((s, v) => s + v, 0) / crs.length : null,
  };
}
