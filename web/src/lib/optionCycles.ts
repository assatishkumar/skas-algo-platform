// Reconstruct multi-leg option cycles from a run's flat trade feed (the same trades the
// Analysis page already fetches). One cycle = the legs held together on one UNDERLYING until
// the book goes flat — so a 1-3-2 ratio spread shows ALL its legs, a calendar's two expiries
// stay one position, and each re-entry after flat is its own cycle. Used to drive the options
// payoff view. Mirrors services/cycle_detail.reconstruct_cycles; keep the two in step.

import type { Trade } from "../types";

export interface CycleLeg {
  symbol: string;
  underlying: string;
  strike: number;
  right: "CE" | "PE";
  expiry: string; // this leg's own expiry — a calendar cycle spans two
  side: "long" | "short";
  units: number; // total contracts entered
  entry_premium: number; // size-weighted avg entry premium
  entry_date: string;
  open_units: number; // contracts still open
  exit_units: number;
  exit_price?: number; // size-weighted avg exit premium
  exit_date?: string;
  exit_action?: string;
  realized_pnl?: number; // signed, gross (closed portion only)
}

export interface ReconCycle {
  underlying: string;
  expiry: string; // the FURTHEST expiry the cycle holds (when the book finally settles)
  entry_date: string;
  exit_date?: string;
  legs: CycleLeg[];
  open: boolean; // any leg still has open contracts
  realized_pnl: number; // sum of legs' realized (gross)
  exit_reason?: string;
  holding_days?: number;
  entry_spot?: number | null; // underlying spot at entry (captured at trade time)
  exit_spot?: number | null; // underlying spot at exit
}

const ENTRY = new Set(["BUY", "SHORT"]);
const EXIT = new Set(["SELL", "COVER", "SETTLE"]);

function parseSymbol(ticker: string) {
  const p = (ticker || "").split("|"); // UNDERLYING|EXPIRY|STRIKE|RIGHT
  if (p.length !== 4) return null;
  const right = p[3] === "CE" || p[3] === "PE" ? (p[3] as "CE" | "PE") : null;
  if (!right) return null;
  return { underlying: p[0], expiry: p[1], strike: Number(p[2]), right };
}

function finalize(cyc: ReconCycle): void {
  let realized = 0;
  let exitDate: string | undefined;
  let anyOpen = false;
  for (const l of cyc.legs) {
    if (l.exit_price != null && l.exit_units > 0) {
      const sign = l.side === "long" ? 1 : -1;
      l.realized_pnl = sign * (l.exit_price - l.entry_premium) * l.exit_units;
      realized += l.realized_pnl;
      if (l.exit_date && (!exitDate || l.exit_date > exitDate)) exitDate = l.exit_date;
    }
    if (l.open_units > 1e-9) anyOpen = true;
  }
  cyc.realized_pnl = realized;
  cyc.open = anyOpen;
  cyc.exit_date = anyOpen ? undefined : exitDate;
  if (cyc.exit_date) {
    cyc.holding_days = Math.round((Date.parse(cyc.exit_date) - Date.parse(cyc.entry_date)) / 86_400_000);
  }
}

/** Group a run's option trades into cycles (newest first). Equity/unknown tickers are ignored. */
export function reconstructCycles(trades: Trade[]): ReconCycle[] {
  // Keyed by UNDERLYING, not expiry (2026-08-20). A two-expiry position — the calendar
  // family's near sells against a far buy, asymmetric_premium's CE/PE on different weeks —
  // is ONE cycle; keying by expiry split a live fair_value_calendar deploy into a "2 leg"
  // cycle and a "1 leg" cycle that were two halves of the same trade. Multi-underlying runs
  // still get one cycle per index, which is what they want. SIDE EFFECT, intended: a Donchian
  // BASKET (42 names sharing one monthly expiry) used to collapse into a single 100-leg
  // "cycle" and now lists one per name.
  const open = new Map<string, ReconCycle>();
  const done: ReconCycle[] = [];

  for (const [idx, t] of (trades ?? []).entries()) {
    const m = parseSymbol(t.ticker);
    if (!m) continue;
    const isEntry = ENTRY.has(t.action);
    const isExit = EXIT.has(t.action);
    if (!isEntry && !isExit) continue;

    let cyc = open.get(m.underlying);
    if (!cyc) {
      if (!isEntry) continue; // an exit with no open cycle — nothing to close
      cyc = { underlying: m.underlying, expiry: m.expiry, entry_date: t.date, legs: [], open: true,
              realized_pnl: 0, entry_spot: t.underlying_spot ?? null };
      open.set(m.underlying, cyc);
    }
    if (m.expiry > cyc.expiry) cyc.expiry = m.expiry; // the cycle settles at its furthest leg

    // ONE record per OPEN→CLOSE episode, not per symbol: a strategy that re-sells the same
    // strike after booking it (intraday_strangle_combo's per-leg re-entry) would otherwise
    // collapse three separate 3-lot shorts into one "9 lot" leg at their average price — a
    // position that never existed — and hide the intermediate exits. Mirrors the backend's
    // services/cycle_detail.reconstruct_cycles; keep the two in step.
    let leg = cyc.legs.find((l) => l.symbol === t.ticker && l.open_units > 1e-9);
    if (isEntry) {
      if (!leg) {
        leg = {
          symbol: t.ticker, underlying: m.underlying, strike: m.strike, right: m.right,
          expiry: m.expiry,
          side: t.action === "BUY" ? "long" : "short", units: 0, entry_premium: 0,
          entry_date: t.date, open_units: 0, exit_units: 0,
        };
        cyc.legs.push(leg);
      }
      leg.entry_premium = (leg.entry_premium * leg.units + t.price * t.units) / (leg.units + t.units);
      leg.units += t.units;
      leg.open_units += t.units;
    } else {
      if (!leg) continue;
      leg.exit_price = ((leg.exit_price ?? 0) * leg.exit_units + t.price * t.units) / (leg.exit_units + t.units);
      leg.exit_units += t.units;
      leg.open_units -= t.units;
      leg.exit_date = t.date;
      leg.exit_action = t.action;
      if (t.exit_reason) cyc.exit_reason = t.exit_reason;
      if (t.underlying_spot != null) cyc.exit_spot = t.underlying_spot; // last exit's spot
    }

    // Flat test AFTER the whole timestamp group: a leg that exits and re-enters in the SAME
    // decision is never actually flat, and testing between the two rows split one trading
    // day into three bogus "cycles" (run 254, 2026-08-13).
    const nxt = (trades ?? [])[idx + 1];
    if (nxt && nxt.date === t.date) continue;
    if (cyc.legs.length && cyc.legs.every((l) => l.open_units <= 1e-9)) {
      finalize(cyc);
      open.delete(m.underlying);
      done.push(cyc);
    }
  }

  for (const cyc of open.values()) {
    finalize(cyc);
    done.push(cyc);
  }
  return done.sort((a, b) => (a.entry_date < b.entry_date ? 1 : a.entry_date > b.entry_date ? -1 : 0));
}
