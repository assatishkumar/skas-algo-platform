import type { OpenPosition, RoundTrip, Trade } from "../types";

const ENTRY = new Set(["BUY", "AVG_BUY"]);

function daysBetween(a: string, b: string): number {
  return Math.max(0, Math.round((Date.parse(b) - Date.parse(a)) / 86_400_000));
}

type Open = { entryDate: string; cost: number; units: number; open: number; exits: RoundTrip["exits"] };

/** Pair trades into completed round-trips AND the leftover still-open positions: entry(s) → exit
 *  leg(s) per symbol, closing a round-trip when the open quantity returns to zero. Handles the
 *  SuperTrend 50%-book + red-flip exit (two exit legs). P&L is computed from prices (entry vs
 *  exit), which equals the engine's per-trade profit for equities (no per-trade charges/taxes).
 *  Symbols still holding units after replay are returned as open positions (avg entry cost). */
export function pairTrades(trades: Trade[]): { roundTrips: RoundTrip[]; openPositions: OpenPosition[] } {
  const sorted = [...trades].sort((a, b) => a.date.localeCompare(b.date));
  const open: Record<string, Open> = {};
  const out: RoundTrip[] = [];

  for (const t of sorted) {
    const sym = t.ticker;
    if (ENTRY.has(t.action)) {
      const p = open[sym] ?? (open[sym] = { entryDate: t.date, cost: 0, units: 0, open: 0, exits: [] });
      p.cost += t.price * t.units;
      p.units += t.units;
      p.open += t.units;
    } else {
      const p = open[sym];
      if (!p) continue; // exit without a tracked entry (shouldn't happen) → skip
      p.exits.push({ date: t.date, price: t.price, units: t.units, tag: t.tag });
      p.open -= t.units;
      if (p.open <= 1e-6) {
        const entryPrice = p.units > 0 ? p.cost / p.units : 0;
        const pnl = p.exits.reduce((s, e) => s + (e.price - entryPrice) * e.units, 0);
        out.push({
          symbol: sym,
          entryDate: p.entryDate,
          entryPrice,
          qty: p.units,
          exits: p.exits,
          exitDate: t.date,
          pnl,
          pnlPct: entryPrice > 0 ? pnl / (entryPrice * p.units) : 0,
          holdingDays: daysBetween(p.entryDate, t.date),
          won: pnl > 0,
        });
        delete open[sym];
      }
    }
  }
  const openPositions: OpenPosition[] = Object.entries(open)
    .filter(([, p]) => p.open > 1e-6)
    .map(([symbol, p]) => {
      const entryPrice = p.units > 0 ? p.cost / p.units : 0; // avg entry over all lots
      return { symbol, entryDate: p.entryDate, entryPrice, qty: p.open, invested: entryPrice * p.open };
    })
    .sort((a, b) => b.invested - a.invested);
  return { roundTrips: out, openPositions };
}

export function buildRoundTrips(trades: Trade[]): RoundTrip[] {
  return pairTrades(trades).roundTrips;
}

export function buildOpenPositions(trades: Trade[]): OpenPosition[] {
  return pairTrades(trades).openPositions;
}

export interface SymbolStat {
  symbol: string;
  pnl: number;
  trades: number;
  wins: number;
  winRate: number;
  avgHold: number;
  roundTrips: RoundTrip[];
}

/** Aggregate round-trips per symbol (P&L contribution, win rate, avg holding). */
export function bySymbol(roundTrips: RoundTrip[]): SymbolStat[] {
  const m = new Map<string, RoundTrip[]>();
  for (const rt of roundTrips) (m.get(rt.symbol) ?? m.set(rt.symbol, []).get(rt.symbol)!).push(rt);
  const out: SymbolStat[] = [];
  for (const [symbol, rts] of m) {
    const pnl = rts.reduce((s, r) => s + r.pnl, 0);
    const wins = rts.filter((r) => r.won).length;
    const avgHold = rts.reduce((s, r) => s + r.holdingDays, 0) / rts.length;
    out.push({ symbol, pnl, trades: rts.length, wins, winRate: wins / rts.length, avgHold, roundTrips: rts });
  }
  return out.sort((a, b) => b.pnl - a.pnl); // ranked by P&L contribution
}
