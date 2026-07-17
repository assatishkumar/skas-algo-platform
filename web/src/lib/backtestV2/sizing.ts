/** Backtest v2 — section 03's two-way lots ⇄ capital coupling.
 *
 *  The user enters the margin one lot-set of THIS strategy's structure costs today (a
 *  NIFTY straddle ≈ ₹2L, a credit spread ≈ ₹50k), then drives EITHER lots or capital —
 *  the other fills in. Which side the user drives is sticky (`capitalDriven`), so a later
 *  margin/buffer edit recomputes the AUTO side and never fights the typed one.
 *
 *  Formulas are the design's verbatim (its `recalc()`), and they match the replay
 *  harness's own capital sizing: floor(equity ÷ (margin × (1 + buffer))).
 */

export interface SizingState {
  margin: number;        // ₹ per lot-set, TODAY's broker margin
  lots: number;
  capital: number;       // ₹
  buffer: number;        // %
  capitalDriven: boolean;
  mode: "fixed" | "capital";
}

export const DEFAULT_SIZING: SizingState = {
  margin: 200_000, lots: 5, capital: 1_100_000, buffer: 10,
  capitalDriven: false, mode: "fixed",
};

export type SizingAction =
  | { type: "margin"; v: number }
  | { type: "lots"; v: number }
  | { type: "capital"; v: number }
  | { type: "buffer"; v: number }
  | { type: "mode"; v: "fixed" | "capital" }
  | { type: "reset"; v: Partial<SizingState> };

/** Recompute the AUTO side. Guards against a 0/NaN margin (division) — the fields are
 *  free-typed, and mid-edit an empty box reads as 0. */
export function recalcSizing(s: SizingState): SizingState {
  const b = 1 + (s.buffer || 0) / 100;
  if (!(s.margin > 0)) return s;
  return s.capitalDriven
    ? { ...s, lots: Math.max(0, Math.floor(s.capital / (s.margin * b))) }
    : { ...s, capital: Math.round(s.margin * s.lots * b) };
}

export function sizingReducer(s: SizingState, a: SizingAction): SizingState {
  switch (a.type) {
    case "margin": return recalcSizing({ ...s, margin: a.v });
    case "lots":   return recalcSizing({ ...s, lots: a.v, capitalDriven: false });
    case "capital": return recalcSizing({ ...s, capital: a.v, capitalDriven: true });
    case "buffer": return recalcSizing({ ...s, buffer: a.v });
    case "mode":   return { ...s, mode: a.v };
    case "reset":  return recalcSizing({ ...s, ...a.v });
  }
}

/** The rail's math box + the capital-short warning. */
export function sizingMath(s: SizingState) {
  const b = 1 + (s.buffer || 0) / 100;
  const marginTotal = s.margin * s.lots;
  const capitalRequired = Math.round(marginTotal * b);
  const oneLotSet = Math.round(s.margin * b);
  return {
    marginTotal,
    capitalRequired,
    bufferAmount: capitalRequired - marginTotal,
    oneLotSet,
    // Only meaningful when the user drives capital: they typed a number too small to fund
    // a single buffered lot-set, so the run would trade nothing.
    capitalShort: s.capitalDriven && s.margin > 0 && s.capital < oneLotSet ? oneLotSet : null,
  };
}
