// Payoff-diagram math for a saved option cycle — computed client-side from legs_detail
// so it works on any persisted run (no API/storage changes). Mirrors
// engine/options/black_scholes.py; q=r turns BS into Black-76 (synthetic GOLD chains).

import type { OptionCycle, OptionPosition } from "../types";

const RISK_FREE = 0.065;

// Abramowitz & Stegun 7.1.26 — max abs error ~1.5e-7, plenty for a chart.
function erf(x: number): number {
  const sign = x < 0 ? -1 : 1;
  const ax = Math.abs(x);
  const t = 1 / (1 + 0.3275911 * ax);
  const y =
    1 -
    (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) *
      t *
      Math.exp(-ax * ax);
  return sign * y;
}

function normCdf(x: number): number {
  return 0.5 * (1 + erf(x / Math.SQRT2));
}

function intrinsic(right: string, spot: number, strike: number): number {
  return right === "CE" ? Math.max(spot - strike, 0) : Math.max(strike - spot, 0);
}

export function bsPrice(
  spot: number, strike: number, t: number, r: number, sigma: number, right: string, q = 0,
): number {
  if (t <= 0 || sigma <= 0) return intrinsic(right, spot, strike);
  const volT = sigma * Math.sqrt(t);
  const d1 = (Math.log(spot / strike) + (r - q + 0.5 * sigma * sigma) * t) / volT;
  const d2 = d1 - volT;
  const dS = spot * Math.exp(-q * t);
  const dK = strike * Math.exp(-r * t);
  return right === "CE" ? dS * normCdf(d1) - dK * normCdf(d2) : dK * normCdf(-d2) - dS * normCdf(-d1);
}

export function impliedVol(
  price: number, spot: number, strike: number, t: number, r: number, right: string, q = 0,
): number | null {
  if (t <= 0 || price <= intrinsic(right, spot, strike) + 1e-9) return null;
  let lo = 1e-4;
  let hi = 5.0;
  if (bsPrice(spot, strike, t, r, hi, right, q) < price) return null;
  for (let i = 0; i < 60; i++) {
    const mid = 0.5 * (lo + hi);
    if (bsPrice(spot, strike, t, r, mid, right, q) < price) lo = mid;
    else hi = mid;
  }
  return 0.5 * (lo + hi);
}

function daysBetween(a: string, b: string): number {
  return Math.round((Date.parse(b) - Date.parse(a)) / 86_400_000);
}

export interface PayoffPoint {
  spot: number;
  expiry: number; // P&L if held to expiry (entry premiums, gross)
  exit: number; // model P&L on the exit date (entry IVs, gross)
}

export interface PayoffData {
  data: PayoffPoint[];
  entrySpot: number | null;
  exitSpot: number | null;
  realized: number; // actual gross realized P&L (for the "where it landed" dot)
}

/** Build the payoff curves for one cycle, or null when legs are missing. */
export function buildPayoff(cycle: OptionCycle): PayoffData | null {
  const legs: OptionPosition[] =
    cycle.legs_detail ?? ([cycle.ce, cycle.pe].filter(Boolean) as OptionPosition[]);
  if (!legs.length) return null;

  const entrySpot = cycle.underlying_entry ?? null;
  const exitSpot = cycle.underlying_exit ?? null;
  const exitDate = cycle.exit_date ?? legs[0].exit_date;
  const q = cycle.underlying === "GOLD" ? RISK_FREE : 0; // futures options → Black-76
  const tExit = Math.max(daysBetween(exitDate, cycle.expiry), 0) / 365;

  const sized = legs.map((leg) => {
    const sign = leg.side === "long" ? 1 : -1; // missing side (old runs) = short premium
    const qty = leg.units * (leg.multiplier || 1);
    const tEntry = Math.max(daysBetween(cycle.entry_date, leg.expiry), 0) / 365;
    const iv = entrySpot
      ? impliedVol(leg.entry_premium, entrySpot, leg.strike, tEntry, RISK_FREE, leg.right, q)
      : null;
    return { ...leg, sign, qty, iv };
  });

  // Grid: span strikes + both spots, padded so the wings' flat region is visible;
  // exact strikes/spots are included so tent kinks and markers line up.
  const refs = [...sized.map((l) => l.strike), entrySpot, exitSpot].filter(
    (v): v is number => v != null,
  );
  const lo0 = Math.min(...refs);
  const hi0 = Math.max(...refs);
  const pad = Math.max((hi0 - lo0) * 0.45, hi0 * 0.02);
  const lo = lo0 - pad;
  const hi = hi0 + pad;
  const n = 120;
  const xs = new Set<number>(refs.map((v) => Math.round(v * 100) / 100));
  for (let i = 0; i <= n; i++) xs.add(Math.round((lo + ((hi - lo) * i) / n) * 100) / 100);

  const data = [...xs]
    .sort((a, b) => a - b)
    .map((s) => {
      let atExpiry = 0;
      let atExit = 0;
      for (const leg of sized) {
        atExpiry += leg.sign * (intrinsic(leg.right, s, leg.strike) - leg.entry_premium) * leg.qty;
        const px =
          leg.iv != null
            ? bsPrice(s, leg.strike, tExit, RISK_FREE, leg.iv, leg.right, q)
            : intrinsic(leg.right, s, leg.strike);
        atExit += leg.sign * (px - leg.entry_premium) * leg.qty;
      }
      return { spot: s, expiry: atExpiry, exit: atExit };
    });

  return { data, entrySpot, exitSpot, realized: cycle.realized_pnl };
}
