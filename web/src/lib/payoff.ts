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

export function intrinsic(right: string, spot: number, strike: number): number {
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

// ---- live positions payoff (Sensibull-style) -----------------------------------
export interface LiveLeg {
  strike: number;
  right: string;
  direction: number; // +1 long / −1 short
  units: number;
  entry: number; // avg fill premium
  ltp: number | null;
}

export interface LivePayoffData {
  data: { spot: number; expiry: number; now: number }[];
  spot: number;
  expiryDate: string;
}

/** Expiry payoff tent + a current-value (T+0) curve for the OPEN option legs.
 *  Default range spans spot AND every strike ±10%; ``rangePct`` (zoom) overrides it
 *  with a symmetric spot ± pct window — strikes outside simply fall off the chart,
 *  which is the point of zooming in. IV per leg from its live LTP (fallback 15%). */
export function buildLivePayoff(
  legs: LiveLeg[], spot: number, expiryDate: string, today?: string,
  rangePct?: number | null,
): LivePayoffData | null {
  if (!legs.length || !spot) return null;
  const asOf = today ?? new Date().toISOString().slice(0, 10);
  const t = Math.max(daysBetween(asOf, expiryDate) / 365, 1 / 365); // ≥ ~1 day keeps BS sane
  const ivs = legs.map(
    (l) => (l.ltp != null ? impliedVol(l.ltp, spot, l.strike, t, RISK_FREE, l.right) : null) ?? 0.15,
  );
  // Span spot AND every strike (padded) so the payoff kinks + breakevens are visible even when a
  // strike sits well outside ±10% of spot (e.g. a far-OTM short call). A zoom override
  // narrows to spot ± rangePct instead.
  const refs = [spot, ...legs.map((l) => l.strike)];
  const lo = rangePct ? spot * (1 - rangePct) : Math.min(...refs) * 0.9;
  const hi = rangePct ? spot * (1 + rangePct) : Math.max(...refs) * 1.1;
  const n = 81;
  const data = [];
  for (let i = 0; i < n; i++) {
    const S = lo + ((hi - lo) * i) / (n - 1);
    let expiry = 0;
    let now = 0;
    legs.forEach((l, j) => {
      expiry += l.direction * (intrinsic(l.right, S, l.strike) - l.entry) * l.units;
      now += l.direction * (bsPrice(S, l.strike, t, RISK_FREE, ivs[j], l.right) - l.entry) * l.units;
    });
    data.push({ spot: S, expiry, now });
  }
  return { data, spot, expiryDate };
}

// ---- Sensibull-style position metrics --------------------------------------------
export interface PositionMetrics {
  maxProfit: number; // +Infinity if unbounded
  maxLoss: number; // negative; -Infinity if unbounded
  maxProfitUnlimited: boolean;
  maxLossUnlimited: boolean;
  breakevens: number[];
  pop: number | null; // probability of profit, 0..1
  currentPnl: number;
  profitLeft: number; // maxProfit − currentPnl (Infinity if unbounded)
  lossLeft: number; // currentPnl − maxLoss (Infinity if unbounded)
  intrinsicValue: number; // net intrinsic of the position (signed)
  timeValue: number; // net mark − intrinsic (signed)
  rewardRisk: number | null; // maxProfit / |maxLoss| when both finite
}

/** Derive Max P/L, breakevens, POP, time/intrinsic value and reward:risk from the OPEN
 *  legs' expiry payoff curve. "Unlimited" upside/downside comes from the net call slope
 *  (puts are bounded at S=0). POP uses a risk-neutral lognormal at expiry with ``aggIv``. */
export function computeMetrics(
  legs: LiveLeg[], spot: number, expiryDate: string, today?: string, aggIv?: number | null,
): PositionMetrics | null {
  if (!legs.length || !spot) return null;
  const asOf = today ?? new Date().toISOString().slice(0, 10);
  const t = Math.max(daysBetween(asOf, expiryDate) / 365, 1 / 365);

  const expiryPnl = (S: number) =>
    legs.reduce((p, l) => p + l.direction * (intrinsic(l.right, S, l.strike) - l.entry) * l.units, 0);

  // Unbounded tails: only net calls run away (puts are capped at S=0).
  const ceNet = legs
    .filter((l) => l.right === "CE")
    .reduce((s, l) => s + l.direction * l.units, 0);
  const maxProfitUnlimited = ceNet > 1e-9;
  const maxLossUnlimited = ceNet < -1e-9;

  // Dense grid over [0, spot·2.5] including every strike so kinks + the S=0 edge are hit.
  const xs = new Set<number>([0, ...legs.map((l) => l.strike)]);
  const hi = spot * 2.5;
  const n = 500;
  for (let i = 0; i <= n; i++) xs.add((hi * i) / n);
  const grid = [...xs].filter((s) => s >= 0).sort((a, b) => a - b);

  let gMax = -Infinity;
  let gMin = Infinity;
  const breakevens: number[] = [];
  let prevS = grid[0];
  let prevP = expiryPnl(prevS);
  gMax = Math.max(gMax, prevP);
  gMin = Math.min(gMin, prevP);
  for (let i = 1; i < grid.length; i++) {
    const s = grid[i];
    const p = expiryPnl(s);
    gMax = Math.max(gMax, p);
    gMin = Math.min(gMin, p);
    if ((prevP < 0 && p >= 0) || (prevP > 0 && p <= 0)) {
      const be = prevP === p ? s : prevS + ((0 - prevP) * (s - prevS)) / (p - prevP);
      if (!breakevens.some((b) => Math.abs(b - be) < spot * 0.0005)) breakevens.push(be);
    }
    prevS = s;
    prevP = p;
  }

  const maxProfit = maxProfitUnlimited ? Infinity : gMax;
  const maxLoss = maxLossUnlimited ? -Infinity : gMin;

  // POP: risk-neutral lognormal mass over the profit segments between breakevens.
  const sigma = aggIv && aggIv > 0
    ? aggIv
    : (() => {
        const xv = legs
          .map((l) => (l.ltp != null ? impliedVol(l.ltp, spot, l.strike, t, RISK_FREE, l.right) : null))
          .filter((v): v is number => v != null);
        return xv.length ? xv.reduce((a, b) => a + b, 0) / xv.length : 0;
      })();
  let pop: number | null = null;
  if (sigma > 0) {
    const cdf = (K: number) =>
      K <= 0 ? 0 : normCdf((Math.log(K / spot) - (RISK_FREE - 0.5 * sigma * sigma) * t) / (sigma * Math.sqrt(t)));
    const bounds = [0, ...breakevens.slice().sort((a, b) => a - b), Infinity];
    pop = 0;
    for (let i = 0; i < bounds.length - 1; i++) {
      const a = bounds[i];
      const b = bounds[i + 1];
      const mid = b === Infinity ? a * 1.5 + spot * 0.25 : (a + b) / 2;
      if (expiryPnl(mid) > 0) pop += (b === Infinity ? 1 : cdf(b)) - cdf(a);
    }
    pop = Math.max(0, Math.min(1, pop));
  }

  const currentPnl = legs.reduce(
    (p, l) => p + l.direction * ((l.ltp ?? l.entry) - l.entry) * l.units, 0,
  );
  const intrinsicValue = legs.reduce(
    (p, l) => p + l.direction * intrinsic(l.right, spot, l.strike) * l.units, 0,
  );
  const netMark = legs.reduce((p, l) => p + l.direction * (l.ltp ?? l.entry) * l.units, 0);
  const timeValue = netMark - intrinsicValue;
  const rewardRisk =
    maxProfitUnlimited || maxLossUnlimited || maxLoss >= 0 ? null : maxProfit / Math.abs(maxLoss);

  return {
    maxProfit,
    maxLoss,
    maxProfitUnlimited,
    maxLossUnlimited,
    breakevens: breakevens.sort((a, b) => a - b),
    pop,
    currentPnl,
    profitLeft: maxProfitUnlimited ? Infinity : maxProfit - currentPnl,
    lossLeft: maxLossUnlimited ? Infinity : currentPnl - maxLoss,
    intrinsicValue,
    timeValue,
    rewardRisk,
  };
}
