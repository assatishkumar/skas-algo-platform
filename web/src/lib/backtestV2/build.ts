/** Backtest v2 — form state → BacktestRequest.
 *
 *  Replaces the old page's per-strategy ternary chain with one registry walk. The two
 *  things it must get right, because nothing downstream will catch them:
 *   1. UNITS — only `unit: "fraction"` fields divide by 100 (the ratio family's
 *      profit_target_pct=0.025 convention). Everything else passes through as typed.
 *   2. SIZING — each variant maps to DIFFERENT real params; `margin_per_lot` exists only
 *      on the intraday replay harness and must never be sent on an EOD run.
 */

import type { BacktestRequest } from "../../types";
import type { Basis, FieldSpec, StrategyFormSpec } from "./registry";
import { allFields, TRAIL_UI, visibleFields } from "./registry";
import type { SizingState } from "./sizing";

export interface V2FormState {
  name: string;
  notes: string;
  basis: Basis;
  params: Record<string, number | string | boolean>;   // DISPLAY units
  sizing: SizingState;
  window: { start: string; end: string };
}

/** Display value → the strategy's expected units. */
function toParamValue(fld: FieldSpec, v: number | string | boolean): number | string | boolean {
  if (fld.unit === "fraction") return Number(v) / 100;
  // Selects that carry a numeric strategy param (entry_weekday "1" → 1).
  if (fld.kind === "select" && typeof v === "string" && v !== "" && !Number.isNaN(Number(v))) {
    return Number(v);
  }
  return v;
}

/** Template/clone value (real units) → the form's display units. */
export function toDisplayValue(fld: FieldSpec, v: unknown): number | string | boolean {
  if (fld.unit === "fraction") return Number(v) * 100;
  if (fld.kind === "select") return String(v);
  if (fld.kind === "toggle") return Boolean(v);
  return v as number | string | boolean;
}

function applySizing(spec: StrategyFormSpec, s: SizingState, p: Record<string, unknown>): void {
  switch (spec.sizing) {
    case "intradayHarness":
      // The 2026-07-17 replay-harness sizing: keyed ₹/lot-set → a %-of-notional applied
      // era-true; sizing="capital" refits lots per flat day.
      p.margin_per_lot = s.margin;
      p.lots = s.lots;
      p.sizing = s.mode;
      p.sizing_buffer_pct = s.buffer;
      break;
    case "eodRatio":
      // The ratio family's own auto-sizing: no margin_per_lot on this path — lots are
      // fitted from capital × utilization ÷ the era-true MODEL margin.
      p.lots = s.lots;
      p.sizing = s.mode === "capital" ? "margin" : "fixed";
      if (s.mode === "capital") p.capital_utilization_pct = 100 - s.buffer;
      break;
    case "hni":
      p.lots = s.lots;
      p.margin_per_lotset = s.margin;
      p.sizing = s.mode === "capital" ? "margin" : "fixed";
      if (s.mode === "capital") p.capital_utilization_pct = 100 - s.buffer;
      break;
    case "mtg":
      p.lots = s.lots;   // BS service: capital rides the request envelope
      break;
  }
}

export function buildV2Body(spec: StrategyFormSpec, st: V2FormState,
                            underlying: string): BacktestRequest {
  const p: Record<string, unknown> = { ...(spec.fixed ?? {}) };
  for (const fld of visibleFields(allFields(spec), st.basis, st.params)) {
    // A hidden field is OMITTED, not zeroed — the strategy's own default then applies
    // (matches the old form's conditional-spread idiom).
    p[fld.param] = toParamValue(fld, st.params[fld.param]);
  }
  // Trail "Off" is the ABSENCE of a trail, expressed the way the strategy reads it: a zero
  // trigger/step disables trailing (its fields are hidden, so the walk above skipped them).
  if (spec.exit.trail && st.params[TRAIL_UI] === "off") {
    p[spec.exit.trail.trigger] = 0;
    p[spec.exit.trail.step] = 0;
  }
  delete p[TRAIL_UI];   // form-only key — never a strategy kwarg
  applySizing(spec, st.sizing, p);

  const isIntraday = st.basis === "intraday";
  return {
    strategy_id: spec.id,
    name: st.name.trim() || undefined,
    notes: st.notes.trim() || undefined,
    universe: null,
    symbols: [underlying],
    instrument_class: "DERIV",
    underlying,
    start_date: st.window.start,
    end_date: st.window.end,
    capital: st.sizing.capital,
    params: isIntraday ? { ...p, data_basis: "intraday" } : p,
    tax_rate: 0,
    withdrawal_rate: 0,
    lookback: 20,
    overrides: [],
  };
}
