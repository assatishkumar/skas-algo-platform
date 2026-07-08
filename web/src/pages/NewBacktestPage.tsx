import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import ReportView from "../components/ReportView";
import { ErrorBox, NumberInput } from "../components/ui";
import { Panel } from "../components/redesign";
import type { BacktestRequest, OverrideInput, StrategyTemplate } from "../types";

// Fields a sweep can vary, with how each value maps into the request.
// unit "pct" values are divided by 100 (like the form); "num" pass through.
type SweepField = {
  key: string; label: string; unit: "pct" | "num";
  fifoOnly?: boolean; lifoOnly?: boolean; optionsOnly?: boolean; stockOnly?: boolean;
  donchianOnly?: boolean;
};
const SWEEP_FIELDS: SweepField[] = [
  { key: "profit_target", label: "Profit target %", unit: "pct", lifoOnly: true },
  { key: "profit_target_1", label: "Target % (1 lot)", unit: "pct", fifoOnly: true },
  { key: "profit_target_2", label: "Target % (2 lots)", unit: "pct", fifoOnly: true },
  { key: "profit_target_3", label: "Target % (3+ lots)", unit: "pct", fifoOnly: true },
  { key: "capital_parts", label: "Capital parts", unit: "num", stockOnly: true },
  { key: "max_lots", label: "Max lots", unit: "num", stockOnly: true },
  { key: "lookback", label: "Lookback", unit: "num", stockOnly: true },
  // Options (short_premium) sweepable params
  { key: "dte_target", label: "Enter at DTE", unit: "num", optionsOnly: true },
  { key: "lots", label: "Lots", unit: "num", optionsOnly: true },
  { key: "stop_loss_pct", label: "Stop loss %", unit: "pct", optionsOnly: true },
  { key: "capital_utilization_pct", label: "Capital utilization %", unit: "num", optionsOnly: true },
  { key: "profit_target_pct", label: "Profit target %", unit: "pct", optionsOnly: true },
  { key: "strike_step", label: "Strike step (pts)", unit: "num", optionsOnly: true },
  // Donchian strangle backtest (percent params are raw percents — the strategy expects them)
  { key: "vol_multiplier", label: "Vol multiplier (× HV)", unit: "num", donchianOnly: true },
  { key: "min_hv_ratio", label: "Min HV20/HV60", unit: "num", donchianOnly: true },
  { key: "min_channel_width_pct", label: "Min channel width %", unit: "num", donchianOnly: true },
  { key: "vix_half_threshold", label: "VIX half-size above", unit: "num", donchianOnly: true },
  { key: "portfolio_sl_pct", label: "Portfolio SL %", unit: "num", donchianOnly: true },
  { key: "breach_buffer_pct", label: "Breach buffer %", unit: "num", donchianOnly: true },
  { key: "max_flips", label: "Max flips", unit: "num", donchianOnly: true },
  { key: "tax_rate", label: "Tax rate %", unit: "pct" },
  { key: "withdrawal_rate", label: "Withdrawal %", unit: "pct" },
  { key: "capital", label: "Capital", unit: "num" },
];
const TOP_LEVEL = new Set(["lookback", "tax_rate", "withdrawal_rate", "capital"]);

function applySweep(body: BacktestRequest, field: SweepField, raw: number): BacktestRequest {
  const v = field.unit === "pct" ? raw / 100 : raw;
  const next = { ...body, params: { ...body.params } };
  if (TOP_LEVEL.has(field.key)) (next as Record<string, unknown>)[field.key] = v;
  else next.params[field.key] = v;
  return next;
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="block text-xs uppercase tracking-wide text-[var(--muted)] mb-1">{label}</span>
      {children}
    </label>
  );
}

const inputClass =
  "w-full rounded-[10px] bg-[var(--field)] border border-[var(--field-border)] px-3 py-2 text-sm text-[var(--strong)] focus:outline-none focus:border-[var(--accent)]";

// Carried via router state from a run's "Clone" button into the prefilled backtest form.
type ClonePrefill = {
  strategy_id: string;
  name: string | null;
  capital: number | null;
  params: Record<string, unknown>;
};

export default function NewBacktestPage({ embedded = false }: { embedded?: boolean } = {}) {
  const { data: strategyData } = useQuery({ queryKey: ["strategies"], queryFn: api.strategies });
  const strategies = strategyData?.strategies ?? ["sst_lifo"];

  const { data: universeData } = useQuery({ queryKey: ["universes"], queryFn: api.universes });
  const universes = universeData ?? [];

  const [name, setName] = useState("");
  const [notes, setNotes] = useState("");
  const [strategyId, setStrategyId] = useState("sst_lifo");
  const [universe, setUniverse] = useState("nifty50"); // "" = Custom
  const [symbols, setSymbols] = useState("RELIANCE, TCS, INFY, HDFCBANK, ICICIBANK");
  const [startDate, setStartDate] = useState("2015-01-01");
  const [endDate, setEndDate] = useState("2026-06-01");
  // Once the user hand-edits a date, stop auto-prefilling from cached coverage.
  const [datesTouched, setDatesTouched] = useState(false);
  const [capital, setCapital] = useState(2500000);
  const [parts, setParts] = useState(50);
  const [target, setTarget] = useState(6);
  // SST-FIFO tiered targets (tighten as lots accumulate): 1 / 2 / 3+ lots.
  const [target1, setTarget1] = useState(10);
  const [target2, setTarget2] = useState(8);
  const [target3, setTarget3] = useState(6);
  const [maxLots, setMaxLots] = useState(0);
  const [taxRate, setTaxRate] = useState(20);
  const [withdrawalRate, setWithdrawalRate] = useState(0);
  const [lookback, setLookback] = useState(20);
  const [allocationMode, setAllocationMode] = useState("fixed");

  // SST Weekly param (weekly Donchian window)
  const [donchianWeeks, setDonchianWeeks] = useState(20);

  // SuperTrend Momentum params
  const [stTimeframe, setStTimeframe] = useState("daily");
  const [stPeriod, setStPeriod] = useState(10);
  const [stMult, setStMult] = useState(3);
  const [stBookPct, setStBookPct] = useState(50); // % booked at the profit target (100 = full)
  const [stEntryMode, setStEntryMode] = useState("flip"); // "flip" | "pullback"
  const [stPullbackPct, setStPullbackPct] = useState(0); // min dip below the post-flip peak
  const [stIdleReturn, setStIdleReturn] = useState(6); // assumed idle-cash yield %/yr (reporting)

  // Nifty_Shop params (DMA-dip accumulator; the Lookback field is the DMA window)
  const [nsAllocPct, setNsAllocPct] = useState(4); // % of equity per trade (compounds)
  const [nsTarget, setNsTarget] = useState(5); // exit a name at +this% over avg cost
  const [nsCandidates, setNsCandidates] = useState(5); // rank the N most-below-DMA
  const [nsNewBuys, setNsNewBuys] = useState(2); // Case 1: open up to this many new/day
  const [nsAvgDown, setNsAvgDown] = useState(3); // Case 2: average a name down >this%

  // 21 EMA momentum params
  const [emaPeriod, setEmaPeriod] = useState(21);
  const [emaWidthMin, setEmaWidthMin] = useState(300);
  const [emaWidthMax, setEmaWidthMax] = useState(500);
  const [emaCreditMin, setEmaCreditMin] = useState(80);
  const [emaCreditMax, setEmaCreditMax] = useState(140);
  const [emaRollDays, setEmaRollDays] = useState(5);
  const [emaSwitchDay, setEmaSwitchDay] = useState(15);
  // Options (short_premium) params
  const [underlying, setUnderlying] = useState("NIFTY");
  const [structure, setStructure] = useState("straddle");
  const [dteTarget, setDteTarget] = useState(2);
  const [lots, setLots] = useState(1);
  const [stopLossPct, setStopLossPct] = useState(50);
  const [profitTargetPct, setProfitTargetPct] = useState(50);
  const [strikeStep, setStrikeStep] = useState(0);

  // Call Ratio Monthly params
  const [strikeMode, setStrikeMode] = useState("percent"); // percent | delta | points
  const [buyOffset, setBuyOffset] = useState(1.3);
  const [sellOffset, setSellOffset] = useState(2.6);
  const [hedgeOffset, setHedgeOffset] = useState(7.0);
  const [crLots, setCrLots] = useState(1);
  // Sizing: "margin" = auto-fit lots to capital each entry (era-true model margin divisor,
  // ~2x conservative vs broker SPAN); "fixed" = legacy exact lot count.
  const [crSizing, setCrSizing] = useState("margin");
  const [crUtil, setCrUtil] = useState(95);
  const [creditLimitPct, setCreditLimitPct] = useState(1); // % of capital
  const [crProfitPct, setCrProfitPct] = useState(2.5);
  const [crStopPct, setCrStopPct] = useState(3);
  const [maxHoldingDays, setMaxHoldingDays] = useState(20);
  const [minVix, setMinVix] = useState(0); // 0 = off; skip entry if ATM IV% (≈VIX) below
  const [combinedCreditPct, setCombinedCreditPct] = useState(2); // batman: cap on both wings' credit
  const [tailOffset, setTailOffset] = useState(0); // 0 = off; extra far "disaster" hedge per wing
  const [tailLots, setTailLots] = useState(1); // tail size as a fraction of lots
  const [tailSide, setTailSide] = useState("both"); // both | put | call (batman wings)
  const [minCreditPct, setMinCreditPct] = useState(0); // credit floor; negative allows a small debit

  // Donchian strangle backtest (synthetic stock options + real NIFTY hedge)
  const [dbUniverse, setDbUniverse] = useState("nifty50"); // nifty50 | nifty25 (top by weight)
  const [dbExclude, setDbExclude] = useState(""); // comma-separated names dropped from the basket
  const [dbInclude, setDbInclude] = useState(""); // comma-separated names added to the basket
  const [dbVolMult, setDbVolMult] = useState(1.1); // × HV20 — calibrate on the Research page
  const [dbBuffer, setDbBuffer] = useState(0.5);
  const [dbBasis, setDbBasis] = useState("touch");
  const [dbMaxFlips, setDbMaxFlips] = useState(3);
  const [dbSl, setDbSl] = useState(2);
  const [dbTargetEnabled, setDbTargetEnabled] = useState(false);
  const [dbTarget, setDbTarget] = useState(50);
  const [dbPortfolioBasis, setDbPortfolioBasis] = useState("notional");
  const [dbLegTargetEnabled, setDbLegTargetEnabled] = useState(false);
  const [dbLegTarget, setDbLegTarget] = useState(80);
  const [dbSkipFloor, setDbSkipFloor] = useState(0.5);
  const [dbRoundOut, setDbRoundOut] = useState(false);
  const [dbBreakoutAtm, setDbBreakoutAtm] = useState(true);
  const [dbHedge, setDbHedge] = useState(true);
  const [dbHedgeOtm, setDbHedgeOtm] = useState(4.5);
  const [dbLots, setDbLots] = useState(1);
  const [dbNotional, setDbNotional] = useState(750000); // per-name target; 0 = fixed lots
  // Entry filters (0 = off) from the run-186 loss study: compression + tight channel +
  // market stress are the danger signature (NOT rising vol).
  const [dbMinHvRatio, setDbMinHvRatio] = useState(0);
  const [dbMinWidth, setDbMinWidth] = useState(0);
  const [dbVixHalf, setDbVixHalf] = useState(0);
  const [dbVixSkip, setDbVixSkip] = useState(0);

  // HNI Weekly params (1-3-2 net-zero weekly tent)
  const [hniLots, setHniLots] = useState(1);
  const [hniBuyLots, setHniBuyLots] = useState(1);
  const [hniSellLots, setHniSellLots] = useState(3);
  const [hniHedgeLots, setHniHedgeLots] = useState(2);
  const [hniBuyOffset, setHniBuyOffset] = useState(200);
  const [hniSellOffset, setHniSellOffset] = useState(400);
  const [hniHedgeOffset, setHniHedgeOffset] = useState(600);
  const [hniDteTarget, setHniDteTarget] = useState(8);
  const [hniTargetPct, setHniTargetPct] = useState(1); // % of deployed margin
  const [hniStopPct, setHniStopPct] = useState(1);
  const [hniMargin, setHniMargin] = useState(132000); // ₹ per 1-3-2 lot-set

  // Staggered Covered Call params
  const [ccEtfSymbol, setCcEtfSymbol] = useState("GOLDBEES");
  const [ccLots, setCcLots] = useState(1);
  const [ccOtmPct, setCcOtmPct] = useState(6);
  const [ccRolldownPct, setCcRolldownPct] = useState(80);
  const [ccRolldownMinDte, setCcRolldownMinDte] = useState(5);
  const [ccMinDte, setCcMinDte] = useState(18);
  const [ccMinPremiumPct, setCcMinPremiumPct] = useState(0.1); // % of spot; walk strike nearer below it
  const [ccMinOtmPct, setCcMinOtmPct] = useState(2); // never sell a call nearer than this
  const [ccKeepAboveCost, setCcKeepAboveCost] = useState(true); // never roll a CE below the ETF cost
  const [ccMinReturnPct, setCcMinReturnPct] = useState(2); // call strike ≥ cost ×(1+this%)
  const [ccDelta, setCcDelta] = useState(0.3); // when fully covered, target this |Δ| (0=off)
  const [ccSellPuts, setCcSellPuts] = useState(false); // wheel: accumulate via short puts
  const [ccPutOtmPct, setCcPutOtmPct] = useState(5); // put strike ≈ spot ×(1−this%)

  // Override builder
  const [ovEnabled, setOvEnabled] = useState(false);
  const [ovScope, setOvScope] = useState("ALGO");
  const [ovTarget, setOvTarget] = useState("");
  const [ovAtPct, setOvAtPct] = useState(6);
  const [ovBookPct, setOvBookPct] = useState(50);
  const [ovTrailPct, setOvTrailPct] = useState(2);

  // Sweep (multi-run) builder
  const [sweepMode, setSweepMode] = useState(false);
  const [sweepField, setSweepField] = useState("profit_target");
  const [sweepValues, setSweepValues] = useState("4, 6, 8, 10");
  const [sweepProgress, setSweepProgress] = useState<{ done: number; total: number } | null>(null);
  const [sweepError, setSweepError] = useState<string | null>(null);

  const navigate = useNavigate();
  // "Clone" from a run lands here with its config in router state. We set strategy/symbols/dates
  // in a one-shot mount effect, then let the (clone-aware) template effect apply its params LAST
  // — same "lands after the strategy-default resets" trick the template prefill already relies on.
  const location = useLocation();
  const clonePrefill = (location.state as { clonePrefill?: ClonePrefill } | null)?.clonePrefill;
  const cloneInitRef = useRef(false);
  const cloneParamsRef = useRef(false);

  const isFifo = strategyId === "sst_fifo";
  const isNiftyShop = strategyId === "nifty_shop";
  const isSstWeekly = strategyId === "sst_weekly";
  const isSstWeeklyFifo = strategyId === "sst_weekly_fifo";
  const isSupertrend = strategyId === "supertrend_momentum";
  const isWeeklyDonchian = isSstWeekly || isSstWeeklyFifo; // both expose donchian_weeks
  const isTiered = isFifo || isSstWeeklyFifo; // FIFO-style tiered profit targets
  const isCallRatio = ["call_ratio_monthly", "put_ratio_monthly", "batman_ratio_monthly"].includes(strategyId);
  const ratioSide =
    strategyId === "put_ratio_monthly" ? "put"
    : strategyId === "batman_ratio_monthly" ? "batman"
    : "call";
  const isHni = strategyId === "hni_weekly";
  const isCoveredCall = strategyId === "staggered_covered_call";
  const isDonchianBt = strategyId === "donchian_strangle_bt";
  const isEma21 = strategyId === "21_ema_momentum";
  const isOptions = strategyId === "short_premium" || isCallRatio || isHni || isCoveredCall || isDonchianBt || isEma21;
  const strikeUnit =
    strikeMode === "delta" ? "Δ"
    : strikeMode === "sd" ? "× exp.move (σ)"
    : strikeMode === "points" ? "offset (pts)"
    : "offset (% OTM)";

  // Ratio strategies deploy ~₹1L of margin per lot per wing; default the capital so the
  // %-of-capital targets are meaningful (Batman = both wings ≈ 2×). User can still edit.
  useEffect(() => {
    if (isCallRatio) setCapital(ratioSide === "batman" ? 200000 : 100000);
    if (isHni) {
      setCapital(200000); // ≥ the ₹1.32L margin per lot-set
      setUnderlying("NIFTY"); // weeklies are cached for NIFTY only
    }
    if (isCoveredCall) {
      setCapital(2000000); // ETF notional ≈ ₹15L + the short CE's margin
      setUnderlying("GOLD");
    }
    if (isDonchianBt) {
      setUnderlying("NIFTY"); // calendar + real hedge chain; capital is auto (peak margin +10%)
    }
  }, [isCallRatio, ratioSide, isHni, isCoveredCall, isDonchianBt]);

  // Covered call: keep the ETF proxy in sync with the underlying (still editable).
  useEffect(() => {
    if (!isCoveredCall) return;
    const map: Record<string, string> = { GOLD: "GOLDBEES", NIFTY: "NIFTYBEES", BANKNIFTY: "BANKBEES" };
    setCcEtfSymbol(map[underlying] ?? `${underlying}BEES`);
  }, [isCoveredCall, underlying]);

  // Reset the buy/sell/hedge/tail offsets to sensible defaults for the chosen strike
  // basis. Batman defaults to the half-size put-wing tail (the run-92 config — best
  // risk-adjusted in the 2020-26 sweep); single-wing ratios default tail-off.
  // A template apply sets strike_mode AND offsets together — this effect must not
  // clobber those offsets on the re-render the mode change triggers.
  const templateModeRef = useRef<string | null>(null);
  const prevSideRef = useRef(ratioSide);
  useEffect(() => {
    const sideChanged = prevSideRef.current !== ratioSide;
    prevSideRef.current = ratioSide;
    if (templateModeRef.current !== null) {
      const fromTemplate = templateModeRef.current === strikeMode;
      templateModeRef.current = null;
      if (fromTemplate) return; // keep the template's offsets
    }
    const d =
      strikeMode === "delta" ? [0.36, 0.25, 0.05, 0.03]
      : strikeMode === "sd" ? [0.35, 0.7, 1.85, 2.4] // multiples of the 1σ expected move
      : strikeMode === "points" ? [300, 600, 1600, 2100]
      : [1.3, 2.6, 7.0, 8.75]; // percent (% OTM)
    setBuyOffset(d[0]);
    setSellOffset(d[1]);
    setHedgeOffset(d[2]);
    if (ratioSide === "batman") {
      if (sideChanged) {
        // Newly selected Batman → arm the default tail (a template apply in the same
        // commit overrides this, including back to off for pre-tail templates).
        setTailOffset(d[3]);
        setTailLots(0.5);
        setTailSide("put");
      } else {
        // Strike-basis change: convert the tail offset to the new mode's units, but
        // respect a deliberately disabled tail (e.g. a tail-off template).
        setTailOffset((cur) => (cur > 0 ? d[3] : 0));
      }
    } else {
      setTailOffset(0);
    }
  }, [strikeMode, ratioSide]);

  // ---- per-strategy template prefill ("set as template" on a run's detail page).
  // Clone (one-shot): adopt the cloned run's strategy / universe / dates / name. Its PARAMS land
  // via the template effect below, so they survive the per-strategy default reset.
  useEffect(() => {
    if (!clonePrefill || cloneInitRef.current) return;
    cloneInitRef.current = true;
    setStrategyId(clonePrefill.strategy_id);
    const p = clonePrefill.params;
    if (typeof p.universe === "string" && p.universe) {
      setUniverse(p.universe);
    } else if (Array.isArray(p.symbols)) {
      setUniverse("");
      setSymbols((p.symbols as string[]).join(", "));
    }
    if (typeof p.start_date === "string") setStartDate(p.start_date);
    if (typeof p.end_date === "string") setEndDate(p.end_date);
    setDatesTouched(true);
    if (clonePrefill.name) setName(`${clonePrefill.name} (copy)`);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clonePrefill]);

  // Declared AFTER the default-resetting effects so, when the strategy changes, the
  // template's (or a clone's) values land last in the same commit and win.
  const { data: templatesData } = useQuery({ queryKey: ["templates"], queryFn: api.templates });
  const [appliedTemplate, setAppliedTemplate] = useState<StrategyTemplate | null>(null);
  useEffect(() => {
    // A clone prefills from its own params (one-shot); otherwise use the strategy's template.
    const cloneActive = !!clonePrefill && clonePrefill.strategy_id === strategyId;
    // Once the clone's params have landed, never re-apply anything for that strategy — a LATER
    // async templatesData load (or any re-render) must not fall back to the strategy template and
    // clobber the clone's params OR the user's subsequent edits (both were silently lost before).
    if (cloneActive && cloneParamsRef.current) return;
    const useClone = cloneActive && !cloneParamsRef.current;
    const t = useClone
      ? ({ run_id: 0, name: clonePrefill!.name ?? "", capital: clonePrefill!.capital ?? undefined, params: clonePrefill!.params } as StrategyTemplate)
      : templatesData?.templates?.[strategyId];
    if (!t) {
      setAppliedTemplate(null);
      return;
    }
    if (useClone) cloneParamsRef.current = true;
    const p = t.params as Record<string, unknown>;
    // ``absent`` makes the prefill FAITHFUL to the template run: a param the run
    // didn't record (e.g. tail-hedge on a pre-tail-feature run) resets to the value
    // that run actually traded with, instead of inheriting form leftovers/defaults.
    const num = (k: string, set: (v: number) => void, scale = 1, absent?: number) => {
      if (typeof p[k] === "number") set((p[k] as number) * scale);
      else if (absent !== undefined) set(absent);
    };
    const str = (k: string, set: (v: string) => void, absent?: string) => {
      if (typeof p[k] === "string") set(p[k] as string);
      else if (absent !== undefined) set(absent);
    };
    if (t.capital) setCapital(t.capital);
    str("underlying", setUnderlying);
    // ratio family (percent-of-capital params are stored as fractions)
    if (typeof p.strike_mode === "string") templateModeRef.current = p.strike_mode;
    str("strike_mode", setStrikeMode);
    num("buy_offset", setBuyOffset);
    num("sell_offset", setSellOffset);
    num("hedge_offset", setHedgeOffset);
    num("credit_debit_limit_pct", setCreditLimitPct, 100);
    // absent = the template run predates auto-sizing → faithful fixed mode
    str("sizing", setCrSizing, "fixed");
    num("capital_utilization_pct", setCrUtil, 1, 95);
    num("combined_credit_limit_pct", setCombinedCreditPct, 100, 2);
    num("min_credit_pct", setMinCreditPct, 100, 0);
    num("max_holding_days", setMaxHoldingDays);
    num("min_vix", setMinVix, 1, 0);
    num("tail_hedge_offset", setTailOffset, 1, 0); // absent = the run traded UN-tailed
    num("tail_hedge_lots", setTailLots, 1, 1);
    str("tail_hedge_side", setTailSide, "both");
    if (strategyId === "hni_weekly") {
      num("lots", setHniLots);
      num("buy_lots", setHniBuyLots);
      num("sell_lots", setHniSellLots);
      num("hedge_lots", setHniHedgeLots);
      num("buy_offset", setHniBuyOffset);
      num("sell_offset", setHniSellOffset);
      num("hedge_offset", setHniHedgeOffset);
      num("dte_target", setHniDteTarget);
      num("profit_target_pct", setHniTargetPct, 100);
      num("stop_loss_pct", setHniStopPct, 100);
      num("margin_per_lotset", setHniMargin);
    }
    if (strategyId === "donchian_strangle_bt") {
      if (typeof p.universe === "string" && p.universe) setDbUniverse(p.universe);
      // absent → reset, so the prefill is faithful to what the template run traded
      setDbExclude(Array.isArray(p.exclude_symbols) ? (p.exclude_symbols as string[]).join(", ") : "");
      setDbInclude(Array.isArray(p.include_symbols) ? (p.include_symbols as string[]).join(", ") : "");
      num("vol_multiplier", setDbVolMult);
      num("breach_buffer_pct", setDbBuffer);
      str("breach_basis", setDbBasis);
      num("max_flips", setDbMaxFlips);
      num("portfolio_sl_pct", setDbSl);
      if (typeof p.portfolio_target_enabled === "boolean") setDbTargetEnabled(p.portfolio_target_enabled);
      num("portfolio_target_pct", setDbTarget);
      str("portfolio_basis", setDbPortfolioBasis);
      if (typeof p.leg_target_enabled === "boolean") setDbLegTargetEnabled(p.leg_target_enabled);
      num("leg_target_pct", setDbLegTarget);
      num("skip_leg_min_premium_pct", setDbSkipFloor);
      if (typeof p.round_out === "boolean") setDbRoundOut(p.round_out);
      if (typeof p.breakout_atm === "boolean") setDbBreakoutAtm(p.breakout_atm);
      if (typeof p.hedge_enabled === "boolean") setDbHedge(p.hedge_enabled);
      num("hedge_otm_pct", setDbHedgeOtm);
      num("lots_per_name", setDbLots);
      num("notional_per_name", setDbNotional, 1, 750000);
      num("min_hv_ratio", setDbMinHvRatio, 1, 0);
      num("min_channel_width_pct", setDbMinWidth, 1, 0);
      num("vix_half_threshold", setDbVixHalf, 1, 0);
      num("vix_skip_threshold", setDbVixSkip, 1, 0);
    }
    if (strategyId === "staggered_covered_call") {
      str("etf_symbol", setCcEtfSymbol);
      num("lots", setCcLots);
      num("ce_otm_pct", setCcOtmPct);
      num("rolldown_trigger_pct", setCcRolldownPct, 100);
      num("rolldown_min_dte", setCcRolldownMinDte);
      num("min_dte", setCcMinDte);
      num("min_premium_pct", setCcMinPremiumPct, 100);
      num("min_ce_otm_pct", setCcMinOtmPct);
      if (typeof p.keep_strike_above_cost === "boolean") setCcKeepAboveCost(p.keep_strike_above_cost);
      num("min_return_pct", setCcMinReturnPct);
      num("covered_call_delta", setCcDelta);
      if (typeof p.sell_puts === "boolean") setCcSellPuts(p.sell_puts);
      num("put_otm_pct", setCcPutOtmPct);
    }
    const ratio = ["call_ratio_monthly", "put_ratio_monthly", "batman_ratio_monthly"].includes(strategyId);
    // short_premium / shared options (ratio templates carry an informational
    // strike_step=50 that must NOT leak into short_premium's strangle step)
    str("structure", setStructure);
    num("dte_target", setDteTarget);
    if (!ratio) num("strike_step", setStrikeStep);
    num("lots", ratio ? setCrLots : setLots);
    num("profit_target_pct", ratio ? setCrProfitPct : setProfitTargetPct, 100);
    num("stop_loss_pct", ratio ? setCrStopPct : setStopLossPct, 100);
    // equity strategies
    num("capital_parts", setParts);
    num("profit_target", setTarget, 100);
    num("profit_target_1", setTarget1, 100);
    num("profit_target_2", setTarget2, 100);
    num("profit_target_3", setTarget3, 100);
    num("max_lots", setMaxLots);
    num("lookback", setLookback);
    str("allocation_mode", setAllocationMode);
    num("tax_rate", setTaxRate, 100);
    num("withdrawal_rate", setWithdrawalRate, 100);
    setAppliedTemplate(useClone ? null : t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [strategyId, templatesData, clonePrefill]);

  // Available cached range for the selected instrument class / underlying — used to
  // default the date pickers so a backtest spans what's actually in the cache.
  const { data: coverage } = useQuery({
    queryKey: ["coverage", isOptions ? "DERIV" : "STOCK", isOptions ? underlying : null],
    queryFn: () =>
      api.dataCoverage(isOptions ? "DERIV" : "STOCK", isOptions ? underlying : undefined),
  });
  useEffect(() => {
    if (datesTouched) return;
    if (coverage?.start_date) setStartDate(coverage.start_date);
    if (coverage?.end_date) setEndDate(coverage.end_date);
  }, [coverage, datesTouched]);

  // Donchian basket: resolve the preset's cached members so the exclude/include inputs
  // can show a live effective count (and flag excludes that aren't in the basket).
  const parseNames = (s: string) =>
    s.split(",").map((x) => x.trim().toUpperCase()).filter(Boolean);
  const { data: dbBasketSyms } = useQuery({
    queryKey: ["universe-symbols", dbUniverse],
    queryFn: () => api.universeSymbols(dbUniverse),
    enabled: isDonchianBt,
  });
  const dbEffective = useMemo(() => {
    if (!dbBasketSyms) return null;
    const excl = parseNames(dbExclude);
    const inBasket = new Set(dbBasketSyms.symbols);
    const kept = dbBasketSyms.symbols.filter((s) => !excl.includes(s));
    // Mirrors the server: an include lands if it isn't already in the post-exclusion set
    // (so re-including an excluded name puts it back). Dedupe repeated entries.
    const added = [...new Set(parseNames(dbInclude))].filter((s) => !kept.includes(s));
    return {
      count: kept.length + added.length,
      unknownExcludes: excl.filter((s) => !inBasket.has(s)),
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dbBasketSyms, dbExclude, dbInclude]);

  const mutation = useMutation({
    mutationFn: (body: BacktestRequest) => api.backtest(body),
  });
  // Persist a previewed result (no recompute); jump to the saved run on success.
  const saveMutation = useMutation({
    mutationFn: (b: Parameters<typeof api.backtestSave>[0]) => api.backtestSave(b),
    onSuccess: (data) => {
      if (data.run_id != null) navigate(`/runs/${data.run_id}`);
    },
  });

  function buildBody(): BacktestRequest {
    const overrides: OverrideInput[] = [];
    if (ovEnabled) {
      overrides.push({
        scope: ovScope,
        target: ovScope === "ALGO" ? null : ovTarget || null,
        rule: {
          exit: [
            { at_pct: ovAtPct, action: "book", qty_pct: ovBookPct },
            { action: "trail_sl", trail_pct: ovTrailPct },
          ],
        },
      });
    }
    if (isOptions) {
      const params: Record<string, unknown> = isDonchianBt
        ? {
            underlying,
            // Donchian percent params are RAW percents (the strategy divides by 100 itself).
            vol_multiplier: dbVolMult,
            breach_buffer_pct: dbBuffer,
            breach_basis: dbBasis,
            max_flips: dbMaxFlips,
            flip_delta: "atm", // no live chain in a backtest — 30Δ would silently degrade anyway
            portfolio_sl_pct: dbSl,
            portfolio_target_enabled: dbTargetEnabled,
            portfolio_target_pct: dbTarget,
            portfolio_basis: dbPortfolioBasis,
            leg_target_enabled: dbLegTargetEnabled,
            leg_target_pct: dbLegTarget,
            skip_leg_min_premium_pct: dbSkipFloor,
            round_out: dbRoundOut,
            breakout_atm: dbBreakoutAtm,
            hedge_enabled: dbHedge,
            hedge_otm_pct: dbHedgeOtm,
            lots_per_name: dbLots,
            notional_per_name: dbNotional,
            min_hv_ratio: dbMinHvRatio,
            min_channel_width_pct: dbMinWidth,
            vix_half_threshold: dbVixHalf,
            vix_skip_threshold: dbVixSkip,
            // Per-run basket overrides (applied server-side to the universe preset).
            ...(parseNames(dbExclude).length ? { exclude_symbols: parseNames(dbExclude) } : {}),
            ...(parseNames(dbInclude).length ? { include_symbols: parseNames(dbInclude) } : {}),
          }
        : isHni
        ? {
            underlying,
            lots: hniLots,
            buy_lots: hniBuyLots,
            sell_lots: hniSellLots,
            hedge_lots: hniHedgeLots,
            buy_offset: hniBuyOffset,
            sell_offset: hniSellOffset,
            hedge_offset: hniHedgeOffset,
            dte_target: hniDteTarget,
            profit_target_pct: hniTargetPct / 100,
            stop_loss_pct: hniStopPct / 100,
            margin_per_lotset: hniMargin,
          }
        : isCoveredCall
        ? {
            underlying,
            etf_symbol: ccEtfSymbol.trim().toUpperCase(),
            lots: ccLots,
            ce_otm_pct: ccOtmPct,
            rolldown_trigger_pct: ccRolldownPct / 100,
            rolldown_min_dte: ccRolldownMinDte,
            min_dte: ccMinDte,
            min_premium_pct: ccMinPremiumPct / 100,
            min_ce_otm_pct: ccMinOtmPct,
            keep_strike_above_cost: ccKeepAboveCost,
            min_return_pct: ccMinReturnPct,
            covered_call_delta: ccDelta,
            sell_puts: ccSellPuts,
            ...(ccSellPuts ? { put_otm_pct: ccPutOtmPct } : {}),
          }
        : isCallRatio
        ? {
            underlying,
            strike_mode: strikeMode,
            buy_offset: buyOffset,
            sell_offset: sellOffset,
            hedge_offset: hedgeOffset,
            lots: crLots,
            sizing: crSizing,
            ...(crSizing === "margin" ? { capital_utilization_pct: crUtil } : {}),
            credit_debit_limit_pct: creditLimitPct / 100,
            profit_target_pct: crProfitPct / 100,
            stop_loss_pct: crStopPct / 100,
            max_holding_days: maxHoldingDays,
            min_vix: minVix,
            ...(ratioSide === "batman" ? { combined_credit_limit_pct: combinedCreditPct / 100 } : {}),
            ...(tailOffset > 0
              ? { tail_hedge_offset: tailOffset, tail_hedge_lots: tailLots, tail_hedge_side: tailSide }
              : {}),
            ...(minCreditPct !== 0 ? { min_credit_pct: minCreditPct / 100 } : {}),
          }
        : isEma21
        ? {
            underlying,
            lots,
            ema_period: emaPeriod,
            width_min: emaWidthMin,
            width_max: emaWidthMax,
            credit_min: emaCreditMin,
            credit_max: emaCreditMax,
            roll_days_before: emaRollDays,
            expiry_switch_day: emaSwitchDay,
          }
        : {
            underlying,
            structure,
            dte_target: dteTarget,
            lots,
            stop_loss_pct: stopLossPct / 100,
            profit_target_pct: profitTargetPct / 100,
            ...(structure === "strangle" && strikeStep > 0 ? { strike_step: strikeStep } : {}),
          };
      return {
        strategy_id: strategyId,
        name: name.trim() || undefined,
        notes: notes.trim() || undefined,
        instrument_class: "DERIV",
        underlying,
        // The donchian backtest is a BASKET — the server resolves the universe to its
        // cached stock names (other DERIV strategies trade one underlying's chain).
        universe: isDonchianBt ? dbUniverse : null,
        symbols: [],
        start_date: startDate,
        end_date: endDate,
        // Donchian bt: capital 0 → the server auto-derives it (peak modelled margin × 1.10).
        capital: isDonchianBt ? 0 : capital,
        params,
        tax_rate: taxRate / 100,
        withdrawal_rate: withdrawalRate / 100,
        lookback,
        overrides: [],
      };
    }
    const isCustom = universe === "";
    return {
      strategy_id: strategyId,
      name: name.trim() || undefined,
      notes: notes.trim() || undefined,
      universe: isCustom ? null : universe,
      symbols: isCustom ? symbols.split(",").map((s) => s.trim()).filter(Boolean) : [],
      start_date: startDate,
      end_date: endDate,
      capital,
      params: isNiftyShop
        ? {
            allocation_pct: nsAllocPct / 100,
            profit_target: nsTarget / 100,
            num_candidates: nsCandidates,
            new_buys_per_day: nsNewBuys,
            avg_down_pct: nsAvgDown / 100,
          }
        : {
            capital_parts: parts,
            ...(isSupertrend ? {} : { max_lots: maxLots }),
            allocation_mode: allocationMode,
            ...(isWeeklyDonchian ? { donchian_weeks: donchianWeeks } : {}),
            ...(isSupertrend
              ? {
                  timeframe: stTimeframe,
                  supertrend_period: stPeriod,
                  supertrend_multiplier: stMult,
                  partial_book_pct: stBookPct / 100,
                  entry_mode: stEntryMode,
                  pullback_pct: stPullbackPct / 100,
                  idle_return: stIdleReturn / 100,
                }
              : {}),
            ...(isTiered
              ? {
                  profit_target_1: target1 / 100,
                  profit_target_2: target2 / 100,
                  profit_target_3: target3 / 100,
                }
              : { profit_target: target / 100 }),
          },
      tax_rate: taxRate / 100,
      withdrawal_rate: withdrawalRate / 100,
      lookback,
      overrides,
    };
  }

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (sweepMode) {
      runSweep();
      return;
    }
    // Single run is a PREVIEW — computed but not persisted until the user clicks "Save backtest".
    mutation.mutate({ ...buildBody(), persist: false });
  }

  async function runSweep() {
    const field = SWEEP_FIELDS.find((f) => f.key === sweepField);
    const values = sweepValues
      .split(",")
      .map((s) => Number(s.trim()))
      .filter((n) => Number.isFinite(n))
      .slice(0, 5);
    if (!field || values.length < 2) {
      setSweepError("Enter 2–5 numeric values for the swept parameter.");
      return;
    }
    setSweepError(null);
    const batchId = crypto.randomUUID().slice(0, 32);
    const base = buildBody();
    const baseName = base.name || `${strategyId} backtest`;
    const runIds: number[] = [];
    try {
      for (let i = 0; i < values.length; i++) {
        setSweepProgress({ done: i, total: values.length });
        const variant = applySweep(base, field, values[i]);
        variant.name = `${baseName} (${field.label} ${values[i]})`;
        variant.batch_id = batchId;
        variant.persist = true; // a sweep persists its variants (they feed the compare view)
        const res = await api.backtest(variant);
        if (res.run_id != null) runIds.push(res.run_id);
      }
      navigate(`/compare?ids=${runIds.join(",")}`);
    } catch (e) {
      setSweepError((e as Error).message);
    } finally {
      setSweepProgress(null);
    }
  }

  const result = mutation.data;
  const sweepableFields = SWEEP_FIELDS.filter(
    (f) =>
      (!f.optionsOnly || (isOptions && !isDonchianBt)) &&
      (!f.donchianOnly || isDonchianBt) &&
      (!f.stockOnly || !isOptions) &&
      (isOptions || ((!f.fifoOnly || isFifo) && (!f.lifoOnly || !isFifo))),
  );
  // Keep the swept field valid when the strategy (and thus its params) changes.
  useEffect(() => {
    if (!sweepableFields.some((f) => f.key === sweepField)) {
      setSweepField(
        isDonchianBt ? "vol_multiplier"
        : isOptions ? "profit_target_pct"
        : isFifo ? "profit_target_1"
        : "profit_target",
      );
    }
  }, [isFifo, isOptions, isDonchianBt, sweepField, sweepableFields]);

  return (
    <div className="space-y-6">
      {!embedded && <h1 className="text-lg font-semibold">New backtest</h1>}

      <Panel className="p-5 max-w-[760px]">
        <form onSubmit={submit} className="space-y-4">
          <div className="grid md:grid-cols-2 gap-4">
            <Field label="Name">
              <input className={inputClass} placeholder="e.g. SST Nifty50 2015-26" value={name} onChange={(e) => setName(e.target.value)} />
            </Field>
            <Field label="Notes">
              <input className={inputClass} placeholder="what you're testing / why" value={notes} onChange={(e) => setNotes(e.target.value)} />
            </Field>
          </div>
          {appliedTemplate && (
            <div className="flex items-center gap-2 rounded-md bg-amber-100 text-amber-800 border border-amber-300 dark:bg-amber-950/40 dark:border-amber-900/50 dark:text-amber-200 px-3 py-2 text-xs">
              <span>
                ★ Params prefilled from this strategy's template:{" "}
                <Link to={`/runs/${appliedTemplate.run_id}`} className="underline hover:text-amber-100">
                  {appliedTemplate.name || `run #${appliedTemplate.run_id}`}
                </Link>{" "}
                — edit anything below, or manage the template from that run's page.
              </span>
            </div>
          )}
          <div className="grid md:grid-cols-2 gap-4">
            <Field label="Strategy">
              <select className={inputClass} value={strategyId} onChange={(e) => setStrategyId(e.target.value)}>
                {strategies.map((s) => (
                  <option key={s} value={s}>{s}</option>
                ))}
              </select>
            </Field>
            {isDonchianBt ? (
              <Field label="Basket">
                <select className={inputClass} value={dbUniverse} onChange={(e) => setDbUniverse(e.target.value)}>
                  <option value="nifty50">Nifty 50 (full basket)</option>
                  <option value="nifty25">Nifty 25 (top by weight — ~half the margin)</option>
                </select>
              </Field>
            ) : isOptions ? (
              <Field label="Underlying">
                <select className={inputClass} value={underlying} onChange={(e) => setUnderlying(e.target.value)}>
                  {isHni || isEma21 ? (
                    <option value="NIFTY">NIFTY (weeklies cached)</option>
                  ) : (
                    <>
                      <option value="NIFTY">NIFTY</option>
                      <option value="BANKNIFTY">BANKNIFTY</option>
                      <option value="GOLD">GOLD (synthetic)</option>
                    </>
                  )}
                </select>
              </Field>
            ) : (
              <>
                <Field label="Universe">
                  <select className={inputClass} value={universe} onChange={(e) => setUniverse(e.target.value)}>
                    {universes.map((u) => (
                      <option key={u.name} value={u.name}>
                        {u.label} ({u.count} available)
                      </option>
                    ))}
                    <option value="">Custom</option>
                  </select>
                </Field>
                {universe === "" ? (
                  <Field label="Symbols (comma-separated)">
                    <input className={inputClass} value={symbols} onChange={(e) => setSymbols(e.target.value)} />
                  </Field>
                ) : (
                  <Field label="Symbols">
                    <div className={`${inputClass} text-[var(--muted)]`}>
                      {universes.find((u) => u.name === universe)?.count ?? "…"} symbols from{" "}
                      {universes.find((u) => u.name === universe)?.label ?? universe}
                    </div>
                  </Field>
                )}
              </>
            )}
            <Field label="Start date">
              <input type="date" className={inputClass} value={startDate} onChange={(e) => { setDatesTouched(true); setStartDate(e.target.value); }} />
            </Field>
            <Field label="End date">
              <input type="date" className={inputClass} value={endDate} onChange={(e) => { setDatesTouched(true); setEndDate(e.target.value); }} />
            </Field>
          </div>

          {isDonchianBt ? (
            <div key="db-params" className="grid md:grid-cols-3 gap-4">
              <div className="md:col-span-3 text-[11px] text-amber-700 dark:text-amber-300/90">
                Donchian strangle backtest: every monthly cycle SELL each name's CE at the previous
                expiry-cycle's high and PE at its low (breach → roll to the ATM opposite side, once
                per name/day, closed after max flips), plus a notional-matched long OTM NIFTY hedge.
                Stock premiums are <b>synthetic Black-Scholes</b> (σ = HV20 × the multiplier — no
                stock-option history exists); the NIFTY hedge uses the real cached chain (2020+).
                Daily bars: flips fill at that day's close. Calibrate the multiplier on the{" "}
                <Link to="/research" className="underline">Research page</Link>.
              </div>
              <Field label={`Exclude names (${dbEffective?.count ?? "…"} in basket)`}>
                <input
                  className={inputClass}
                  placeholder="e.g. ADANIENT, INDUSINDBK"
                  value={dbExclude}
                  onChange={(e) => setDbExclude(e.target.value)}
                />
              </Field>
              <Field label="Also include names">
                <input
                  className={inputClass}
                  placeholder="e.g. names dropped from Nifty 25"
                  value={dbInclude}
                  onChange={(e) => setDbInclude(e.target.value)}
                />
              </Field>
              <div className="self-end pb-2 text-[11px] text-[var(--faint)]">
                {dbEffective?.unknownExcludes.length ? (
                  <span className="text-amber-600 dark:text-amber-400">
                    not in {dbUniverse}: {dbEffective.unknownExcludes.join(", ")}
                  </span>
                ) : (
                  <>includes need cached data + a known F&O lot size (the Nifty-50 pool) — others are dropped silently</>
                )}
              </div>
              <Field label="Capital">
                <div className={`${inputClass} text-[var(--muted)]`}>
                  auto — peak modelled margin + 10%
                </div>
              </Field>
              <Field label="Vol multiplier (× HV20)">
                <NumberInput step="0.05" className={inputClass} value={dbVolMult} onChange={setDbVolMult} />
              </Field>
              <Field label="Notional per name (₹, 0 = fixed lots)">
                <NumberInput className={inputClass} value={dbNotional} onChange={setDbNotional} />
              </Field>
              {dbNotional <= 0 && (
                <Field label="Lots per name (fixed)">
                  <NumberInput className={inputClass} value={dbLots} onChange={setDbLots} />
                </Field>
              )}
              <Field label="Min HV20/HV60 at entry (0 = off)">
                <NumberInput step="0.05" className={inputClass} value={dbMinHvRatio} onChange={setDbMinHvRatio} />
              </Field>
              <Field label="Min channel width % (0 = off)">
                <NumberInput step="0.5" className={inputClass} value={dbMinWidth} onChange={setDbMinWidth} />
              </Field>
              <Field label="VIX half-size above (0 = off)">
                <NumberInput step="1" className={inputClass} value={dbVixHalf} onChange={setDbVixHalf} />
              </Field>
              <Field label="VIX skip cycle above (0 = off)">
                <NumberInput step="1" className={inputClass} value={dbVixSkip} onChange={setDbVixSkip} />
              </Field>
              <Field label="Breach basis">
                <select className={inputClass} value={dbBasis} onChange={(e) => setDbBasis(e.target.value)}>
                  <option value="touch">Touch (day high/low)</option>
                  <option value="close">Close (EOD)</option>
                </select>
              </Field>
              <Field label="Breach buffer % (clear the strike by)">
                <NumberInput step="0.1" className={inputClass} value={dbBuffer} onChange={setDbBuffer} />
              </Field>
              <Field label="Max flips (then close the name)">
                <NumberInput className={inputClass} value={dbMaxFlips} onChange={setDbMaxFlips} />
              </Field>
              <Field label="Portfolio stop basis">
                <select className={inputClass} value={dbPortfolioBasis} onChange={(e) => setDbPortfolioBasis(e.target.value)}>
                  <option value="notional">% of notional (stop) / premium (target)</option>
                  <option value="margin">% of modelled margin</option>
                </select>
              </Field>
              <Field label="Portfolio SL %">
                <NumberInput step="0.1" className={inputClass} value={dbSl} onChange={setDbSl} />
              </Field>
              <Field label="Skip-leg floor (% of spot)">
                <NumberInput step="0.1" className={inputClass} value={dbSkipFloor} onChange={setDbSkipFloor} />
              </Field>
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={dbTargetEnabled} onChange={(e) => setDbTargetEnabled(e.target.checked)} />
                <span>Portfolio target</span>
              </label>
              {dbTargetEnabled && (
                <Field label="Portfolio target %">
                  <NumberInput step="1" className={inputClass} value={dbTarget} onChange={setDbTarget} />
                </Field>
              )}
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={dbLegTargetEnabled} onChange={(e) => setDbLegTargetEnabled(e.target.checked)} />
                <span>Per-leg target</span>
              </label>
              {dbLegTargetEnabled && (
                <Field label="Leg target % (premium captured)">
                  <NumberInput step="1" className={inputClass} value={dbLegTarget} onChange={setDbLegTarget} />
                </Field>
              )}
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={dbHedge} onChange={(e) => setDbHedge(e.target.checked)} />
                <span>NIFTY tail hedge</span>
              </label>
              {dbHedge && (
                <Field label="Hedge OTM % (each side)">
                  <NumberInput step="0.5" className={inputClass} value={dbHedgeOtm} onChange={setDbHedgeOtm} />
                </Field>
              )}
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={dbBreakoutAtm} onChange={(e) => setDbBreakoutAtm(e.target.checked)} />
                <span>Breakout → ATM opposite leg</span>
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={dbRoundOut} onChange={(e) => setDbRoundOut(e.target.checked)} />
                <span>Round strikes out (more cushion)</span>
              </label>
              <Field label="Withdrawal rate %">
                <NumberInput step="1" className={inputClass} value={withdrawalRate} onChange={setWithdrawalRate} />
              </Field>
            </div>
          ) : isHni ? (
            <div key="hni-params" className="grid md:grid-cols-3 gap-4">
              <div className="md:col-span-3 text-[11px] text-amber-700 dark:text-amber-300/90">
                HNI Weekly: net-zero 1-3-2 call ratio "tent" — BUY 1× ~200 OTM, SELL 3× ~400 OTM,
                BUY 2× ~600 OTM on the ~8-DTE weekly (enter Monday, force-exit Friday; no weekend
                carry). Target/stop are % of DEPLOYED MARGIN (≈ ₹1.32L per lot-set), not capital.
                Max profit ≈ max loss (R:R ~1:1) by construction; entry is not gated on the
                credit/debit sign. EOD engine: the 9:45 AM entry and intraday ±1% exits fill at
                daily closes. Weekly Tuesday expiries are cached from Sep 2025.
              </div>
              <Field label="Capital (₹)">
                <NumberInput className={inputClass} value={capital} onChange={setCapital} />
              </Field>
              <Field label="Lot-sets (× 1-3-2)">
                <NumberInput className={inputClass} value={hniLots} onChange={setHniLots} />
              </Field>
              <Field label="Margin per lot-set (₹)">
                <NumberInput className={inputClass} value={hniMargin} onChange={setHniMargin} />
              </Field>
              <Field label="Buy ratio × (near long)">
                <NumberInput className={inputClass} value={hniBuyLots} onChange={setHniBuyLots} />
              </Field>
              <Field label="Sell ratio × (short body)">
                <NumberInput className={inputClass} value={hniSellLots} onChange={setHniSellLots} />
              </Field>
              <Field label="Hedge ratio × (far long)">
                <NumberInput className={inputClass} value={hniHedgeLots} onChange={setHniHedgeLots} />
              </Field>
              <Field label="Buy offset (pts OTM)">
                <NumberInput className={inputClass} value={hniBuyOffset} onChange={setHniBuyOffset} />
              </Field>
              <Field label="Sell offset (pts OTM)">
                <NumberInput className={inputClass} value={hniSellOffset} onChange={setHniSellOffset} />
              </Field>
              <Field label="Hedge offset (pts OTM)">
                <NumberInput className={inputClass} value={hniHedgeOffset} onChange={setHniHedgeOffset} />
              </Field>
              <Field label="DTE target (8 = next Tuesday)">
                <NumberInput className={inputClass} value={hniDteTarget} onChange={setHniDteTarget} />
              </Field>
              <Field label="Target % (of deployed margin)">
                <NumberInput step="0.1" className={inputClass} value={hniTargetPct} onChange={setHniTargetPct} />
              </Field>
              <Field label="Stop % (of deployed margin)">
                <NumberInput step="0.1" className={inputClass} value={hniStopPct} onChange={setHniStopPct} />
              </Field>
              <Field label="Tax rate %">
                <NumberInput className={inputClass} value={taxRate} onChange={setTaxRate} />
              </Field>
              <Field label="Withdrawal rate %">
                <NumberInput step="1" className={inputClass} value={withdrawalRate} onChange={setWithdrawalRate} />
              </Field>
            </div>
          ) : isCoveredCall ? (
            <div key="cc-params" className="grid md:grid-cols-3 gap-4">
              <div className="md:col-span-3 text-[11px] text-amber-700 dark:text-amber-300/90">
                Staggered covered call: SELL 1 monthly CE ~OTM% against the INTENDED full ETF
                position, but buy the ETF in 3 tranches — T1 at entry (~33% covered / 67% naked),
                T2/T3 fire GTT-style as spot closes over S + ⅓/⅔ of the gap to the strike. When
                ~80% of the premium is captured, the CE is rolled DOWN to a fresh OTM strike
                (same expiry). ITM expiry = called away (ETF liquidated, fresh cycle); OTM expiry
                keeps the tranches. EOD engine: GTT buys fill at the CLOSE of the crossing day.
                GOLD options are synthetic (Black-76, no smile); margin reporting is
                coverage-unaware (overstates the short CE).
              </div>
              <Field label="Capital (₹) — covers the full ETF notional">
                <NumberInput className={inputClass} value={capital} onChange={setCapital} />
              </Field>
              <Field label="ETF symbol (auto from underlying)">
                <input className={inputClass} value={ccEtfSymbol} onChange={(e) => setCcEtfSymbol(e.target.value)} />
              </Field>
              <Field label="CE lots">
                <NumberInput className={inputClass} value={ccLots} onChange={setCcLots} />
              </Field>
              <Field label="CE OTM % (3–12)">
                <NumberInput step="0.5" className={inputClass} value={ccOtmPct} onChange={setCcOtmPct} />
              </Field>
              <Field label="Roll-down trigger % (50–95)">
                <NumberInput step="1" className={inputClass} value={ccRolldownPct} onChange={setCcRolldownPct} />
              </Field>
              <Field label="Roll-down min DTE">
                <NumberInput className={inputClass} value={ccRolldownMinDte} onChange={setCcRolldownMinDte} />
              </Field>
              <Field label="Min DTE (monthly expiry pick)">
                <NumberInput className={inputClass} value={ccMinDte} onChange={setCcMinDte} />
              </Field>
              <Field label="Min premium % of spot (else roll nearer)">
                <NumberInput step="0.05" className={inputClass} value={ccMinPremiumPct} onChange={setCcMinPremiumPct} />
              </Field>
              <Field label="Min CE OTM % floor (never nearer)">
                <NumberInput step="0.5" className={inputClass} value={ccMinOtmPct} onChange={setCcMinOtmPct} />
              </Field>
              <label className="flex items-center gap-2 text-sm md:col-span-3">
                <input type="checkbox" checked={ccKeepAboveCost} onChange={(e) => setCcKeepAboveCost(e.target.checked)} />
                <span>Never sell/roll the call below the ETF's average cost</span>
                <span className="text-[var(--faint)]">— so a called-away always books a profit (don't roll into a loss)</span>
              </label>
              <Field label="Min return % on assignment (0 = breakeven)">
                <NumberInput step="0.5" className={inputClass} value={ccMinReturnPct} onChange={setCcMinReturnPct} />
              </Field>
              <Field label="Covered-call delta when fully covered (0 = off)">
                <NumberInput step="0.05" className={inputClass} value={ccDelta} onChange={setCcDelta} />
              </Field>
              <div />
              <label className="flex items-center gap-2 text-sm md:col-span-3">
                <input type="checkbox" checked={ccSellPuts} onChange={(e) => setCcSellPuts(e.target.checked)} />
                <span>Wheel: accumulate by selling cash-secured puts</span>
                <span className="text-[var(--faint)]">— premium income on the way down; assigned on dips (replaces GTT up-buys)</span>
              </label>
              {ccSellPuts && (
                <Field label="Put OTM % (strike below spot)">
                  <NumberInput step="0.5" className={inputClass} value={ccPutOtmPct} onChange={setCcPutOtmPct} />
                </Field>
              )}
              <Field label="Tax rate %">
                <NumberInput className={inputClass} value={taxRate} onChange={setTaxRate} />
              </Field>
              <Field label="Withdrawal rate %">
                <NumberInput step="1" className={inputClass} value={withdrawalRate} onChange={setWithdrawalRate} />
              </Field>
            </div>
          ) : isCallRatio ? (
            <div key="ratio-params" className="grid md:grid-cols-3 gap-4">
              <div className="md:col-span-3 text-[11px] text-amber-700 dark:text-amber-300/90">
                {ratioSide === "batman"
                  ? "Batman: BOTH 1:2 ratio wings (call above + put below spot, each hedged; 6 legs). Both wings must qualify for credit or the month is skipped; one combined target/stop/time exit. Risk = a fast move EITHER way; margin ≈ ₹2L per lot."
                  : ratioSide === "put"
                    ? "1:2 put ratio + outer hedge on NIFTY monthly (strikes BELOW spot — zero upside risk; watch fast sell-offs)."
                    : "1:2 call ratio + outer hedge on NIFTY monthly (strikes ABOVE spot — zero downside risk; watch fast rallies)."}{" "}
                Entry = last Tuesday of the month for next month's expiry (EOD approximates the 3:16 PM
                rule). Auto sizing refits lot-sets to (compounding) capital at every entry using the
                era-true model margin — ~2× conservative vs broker SPAN, so utilization 95 ≈ half the
                real margin; credit %s then scale with current equity.
              </div>
              <Field label="Capital (₹) — ≈ ₹1L / lot">
                <NumberInput className={inputClass} value={capital} onChange={setCapital} />
              </Field>
              <Field label="Sizing">
                <select className={inputClass} value={crSizing} onChange={(e) => setCrSizing(e.target.value)}>
                  <option value="margin">Auto — fit lots to capital each entry</option>
                  <option value="fixed">Fixed lot-sets</option>
                </select>
              </Field>
              {crSizing === "margin" ? (
                <Field label="Capital utilization % (of model margin)">
                  <NumberInput step="5" className={inputClass} value={crUtil} onChange={setCrUtil} />
                </Field>
              ) : (
                <Field label="Lots (1 buy : 2 sell : 1 hedge)">
                  <NumberInput className={inputClass} value={crLots} onChange={setCrLots} />
                </Field>
              )}
              <Field label="Strike basis">
                <select className={inputClass} value={strikeMode} onChange={(e) => setStrikeMode(e.target.value)}>
                  <option value="percent">% of spot (level-aware)</option>
                  <option value="sd">Expected move σ (vol-aware)</option>
                  <option value="delta">Delta (vol-aware)</option>
                  <option value="points">Fixed points</option>
                </select>
              </Field>
              <Field label={`Buy ${strikeUnit} (near, ×1)`}>
                <NumberInput step="0.05" className={inputClass} value={buyOffset} onChange={setBuyOffset} />
              </Field>
              <Field label={`Sell ${strikeUnit} (body, ×2)`}>
                <NumberInput step="0.05" className={inputClass} value={sellOffset} onChange={setSellOffset} />
              </Field>
              <Field label={`Hedge ${strikeUnit} (caps upside)`}>
                <NumberInput step="0.05" className={inputClass} value={hedgeOffset} onChange={setHedgeOffset} />
              </Field>
              <Field label={ratioSide === "batman" ? "Max credit % per wing" : "Max net credit % (of capital)"}>
                <NumberInput step="0.1" className={inputClass} value={creditLimitPct} onChange={setCreditLimitPct} />
              </Field>
              {ratioSide === "batman" && (
                <Field label="Max COMBINED credit % (both wings)">
                  <NumberInput step="0.1" className={inputClass} value={combinedCreditPct} onChange={setCombinedCreditPct} />
                </Field>
              )}
              <Field label="Profit target % (of capital)">
                <NumberInput step="0.1" className={inputClass} value={crProfitPct} onChange={setCrProfitPct} />
              </Field>
              <Field label="Stop loss % (of capital)">
                <NumberInput step="0.1" className={inputClass} value={crStopPct} onChange={setCrStopPct} />
              </Field>
              <Field label="Max holding days">
                <NumberInput className={inputClass} value={maxHoldingDays} onChange={setMaxHoldingDays} />
              </Field>
              <Field label="Min entry IV % (≈VIX, 0 = off)">
                <NumberInput step="0.5" className={inputClass} value={minVix} onChange={setMinVix} />
              </Field>
              <Field label={`Tail hedge ${strikeUnit} (0 = off)`}>
                <NumberInput step="0.05" className={inputClass} value={tailOffset} onChange={setTailOffset} />
              </Field>
              {tailOffset > 0 && (
                <Field label="Tail hedge lots (× lots)">
                  <NumberInput step="0.5" className={inputClass} value={tailLots} onChange={setTailLots} />
                </Field>
              )}
              {tailOffset > 0 && ratioSide === "batman" && (
                <Field label="Tail hedge wings">
                  <select className={inputClass} value={tailSide} onChange={(e) => setTailSide(e.target.value)}>
                    <option value="both">Both wings</option>
                    <option value="put">Put wing only (crash protection)</option>
                    <option value="call">Call wing only</option>
                  </select>
                </Field>
              )}
              {tailOffset > 0 && (
                <Field label="Min credit % (negative = allow debit)">
                  <NumberInput step="0.1" className={inputClass} value={minCreditPct} onChange={setMinCreditPct} />
                </Field>
              )}
              {tailOffset > 0 && (
                <div className="md:col-span-3 text-[11px] text-[var(--faint)] -mt-2">
                  The tail is an extra far long per wing: its vega/gamma convexity cushions{" "}
                  <span className="text-[var(--muted)]">gap moves the MTM stop can't catch</span>, and beyond it
                  the wing turns net long. Its cost counts against the entry credit — a negative min credit
                  lets the strategy pay a small debit for the insurance instead of skipping the month.
                </div>
              )}
              <div className="md:col-span-3 text-[11px] text-[var(--faint)] -mt-2">
                Entry always requires a <span className="text-[var(--muted)]">net credit</span> ≤ the max above
                (strikes auto-shift further OTM when the credit is too rich). Debit months (low IV / thin
                premiums) are <span className="text-[var(--muted)]">skipped</span>. NIFTY lot sizes are historical
                (50 → 25 → 75 → 65 per SEBI revisions).
              </div>
              <Field label="Tax rate %">
                <NumberInput className={inputClass} value={taxRate} onChange={setTaxRate} />
              </Field>
              <Field label="Withdrawal rate %">
                <NumberInput step="1" className={inputClass} value={withdrawalRate} onChange={setWithdrawalRate} />
              </Field>
            </div>
          ) : isEma21 ? (
            <div key="ema21-params" className="grid md:grid-cols-3 gap-4">
              <Field label="Capital (₹)">
                <NumberInput className={inputClass} value={capital} onChange={setCapital} />
              </Field>
              <Field label="Lots">
                <NumberInput className={inputClass} value={lots} onChange={setLots} />
              </Field>
              <Field label="EMA period (high/low channel)">
                <NumberInput className={inputClass} value={emaPeriod} onChange={setEmaPeriod} />
              </Field>
              <Field label="Spread width min (pts)">
                <NumberInput step="100" className={inputClass} value={emaWidthMin} onChange={setEmaWidthMin} />
              </Field>
              <Field label="Spread width max (pts)">
                <NumberInput step="100" className={inputClass} value={emaWidthMax} onChange={setEmaWidthMax} />
              </Field>
              <Field label="Net credit min (₹/share)">
                <NumberInput className={inputClass} value={emaCreditMin} onChange={setEmaCreditMin} />
              </Field>
              <Field label="Net credit max (₹/share)">
                <NumberInput className={inputClass} value={emaCreditMax} onChange={setEmaCreditMax} />
              </Field>
              <Field label="Roll (days before expiry)">
                <NumberInput className={inputClass} value={emaRollDays} onChange={setEmaRollDays} />
              </Field>
              <Field label="Expiry switch day (of month)">
                <NumberInput className={inputClass} value={emaSwitchDay} onChange={setEmaSwitchDay} />
              </Field>
              <div className="md:col-span-3 text-[11px] text-[var(--faint)]">
                Checked once/day at 15:20: close above the EMA-high band → bull put spread; below the
                EMA-low band → bear call spread. 100-pt OTM strikes; credit window skips the day when
                missed; holds until the opposite signal; exits {emaRollDays} days before expiry
                (re-enters next month if the signal persists). Reported margin reads ~2× the real
                broker requirement (model has no long-leg offset).
              </div>
            </div>
          ) : isOptions ? (
            <div key="options-params" className="grid md:grid-cols-3 gap-4">
              <Field label="Capital (₹)">
                <NumberInput className={inputClass} value={capital} onChange={setCapital} />
              </Field>
              <Field label="Structure">
                <select className={inputClass} value={structure} onChange={(e) => setStructure(e.target.value)}>
                  <option value="straddle">Short straddle (ATM CE+PE)</option>
                  <option value="strangle">Short strangle (OTM CE+PE)</option>
                </select>
              </Field>
              <Field label="Lots">
                <NumberInput className={inputClass} value={lots} onChange={setLots} />
              </Field>
              <Field label="Enter at DTE (days to expiry)">
                <NumberInput className={inputClass} value={dteTarget} onChange={setDteTarget} />
              </Field>
              <Field label="Stop loss % (of entry premium)">
                <NumberInput className={inputClass} value={stopLossPct} onChange={setStopLossPct} />
              </Field>
              <Field label="Profit target % (premium decay)">
                <NumberInput className={inputClass} value={profitTargetPct} onChange={setProfitTargetPct} />
              </Field>
              {structure === "strangle" && (
                <Field label="Strike step (points OTM, 0 = ATM/delta)">
                  <NumberInput className={inputClass} value={strikeStep} onChange={setStrikeStep} />
                </Field>
              )}
              <Field label="Tax rate %">
                <NumberInput className={inputClass} value={taxRate} onChange={setTaxRate} />
              </Field>
              <Field label="Withdrawal rate %">
                <NumberInput step="1" className={inputClass} value={withdrawalRate} onChange={setWithdrawalRate} />
              </Field>
            </div>
          ) : (
          <div key="equity-params" className="grid md:grid-cols-3 gap-4">
            <Field label="Capital (₹)">
              <NumberInput className={inputClass} value={capital} onChange={setCapital} />
            </Field>
            {isNiftyShop ? (
              <>
                <Field label="Allocation % per trade (of equity)">
                  <NumberInput step="0.1" className={inputClass} value={nsAllocPct} onChange={setNsAllocPct} />
                </Field>
                <Field label="Exit target %">
                  <NumberInput step="0.1" className={inputClass} value={nsTarget} onChange={setNsTarget} />
                </Field>
                <Field label="Candidates (most below DMA)">
                  <NumberInput className={inputClass} value={nsCandidates} onChange={setNsCandidates} />
                </Field>
                <Field label="New buys / day">
                  <NumberInput className={inputClass} value={nsNewBuys} onChange={setNsNewBuys} />
                </Field>
                <Field label="Average-down trigger %">
                  <NumberInput step="0.1" className={inputClass} value={nsAvgDown} onChange={setNsAvgDown} />
                </Field>
              </>
            ) : (
              <>
                <Field label="Capital parts">
                  <NumberInput className={inputClass} value={parts} onChange={setParts} />
                </Field>
                {isTiered ? (
                  <>
                    <Field label="Target % (1 lot)">
                      <NumberInput step="0.1" className={inputClass} value={target1} onChange={setTarget1} />
                    </Field>
                    <Field label="Target % (2 lots)">
                      <NumberInput step="0.1" className={inputClass} value={target2} onChange={setTarget2} />
                    </Field>
                    <Field label="Target % (3+ lots)">
                      <NumberInput step="0.1" className={inputClass} value={target3} onChange={setTarget3} />
                    </Field>
                  </>
                ) : (
                  <Field label="Profit target %">
                    <NumberInput step="0.1" className={inputClass} value={target} onChange={setTarget} />
                  </Field>
                )}
                {!isSupertrend && (
                  <Field label="Max lots (0 = unlimited)">
                    <NumberInput className={inputClass} value={maxLots} onChange={setMaxLots} />
                  </Field>
                )}
                {isWeeklyDonchian && (
                  <Field label="Donchian (weeks)">
                    <NumberInput className={inputClass} value={donchianWeeks} onChange={setDonchianWeeks} />
                  </Field>
                )}
                {isSupertrend && (
                  <>
                    <Field label="Timeframe">
                      <select className={inputClass} value={stTimeframe} onChange={(e) => setStTimeframe(e.target.value)}>
                        <option value="daily">Daily</option>
                        <option value="weekly">Weekly</option>
                        <option value="monthly">Monthly</option>
                      </select>
                    </Field>
                    <Field label="SuperTrend ATR period">
                      <NumberInput className={inputClass} value={stPeriod} onChange={setStPeriod} />
                    </Field>
                    <Field label="SuperTrend multiplier">
                      <NumberInput step="0.1" className={inputClass} value={stMult} onChange={setStMult} />
                    </Field>
                    <Field label="Book % at target (100 = full)">
                      <NumberInput step="1" className={inputClass} value={stBookPct} onChange={setStBookPct} />
                    </Field>
                    <Field label="Entry">
                      <select className={inputClass} value={stEntryMode} onChange={(e) => setStEntryMode(e.target.value)}>
                        <option value="flip">On green flip</option>
                        <option value="pullback">Pullback breakout</option>
                      </select>
                    </Field>
                    {stEntryMode === "pullback" && (
                      <Field label="Min pullback %">
                        <NumberInput step="0.1" className={inputClass} value={stPullbackPct} onChange={setStPullbackPct} />
                      </Field>
                    )}
                    <Field label="Idle cash return %/yr">
                      <NumberInput step="0.5" className={inputClass} value={stIdleReturn} onChange={setStIdleReturn} />
                    </Field>
                  </>
                )}
              </>
            )}
            <Field label="Tax rate %">
              <NumberInput className={inputClass} value={taxRate} onChange={setTaxRate} />
            </Field>
            <Field label="Withdrawal rate %">
              <NumberInput step="1" className={inputClass} value={withdrawalRate} onChange={setWithdrawalRate} />
            </Field>
            <Field label={isSupertrend ? "Entry delay (bars)" : "Lookback (days)"}>
              <NumberInput className={inputClass} value={lookback} onChange={setLookback} />
            </Field>
            {!isNiftyShop && (
              <Field label="Position sizing">
                <select className={inputClass} value={allocationMode} onChange={(e) => setAllocationMode(e.target.value)}>
                  <option value="fixed">Fixed (capital / parts)</option>
                  <option value="equity_scaled">Equity-scaled (compounds)</option>
                </select>
              </Field>
            )}
          </div>
          )}

          {!isOptions && (
          <div className="rounded-lg border border-[var(--divider)] p-3">
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={ovEnabled} onChange={(e) => setOvEnabled(e.target.checked)} />
              <span className="font-medium">Apply exit override</span>
              <span className="text-[var(--faint)]">— book a portion at a target, trail the rest</span>
            </label>
            {ovEnabled && (
              <div className="grid md:grid-cols-5 gap-3 mt-3">
                <Field label="Scope">
                  <select className={inputClass} value={ovScope} onChange={(e) => setOvScope(e.target.value)}>
                    <option value="ALGO">All positions</option>
                    <option value="SYMBOL">Symbol</option>
                  </select>
                </Field>
                <Field label="Target symbol">
                  <input
                    className={inputClass}
                    value={ovTarget}
                    disabled={ovScope === "ALGO"}
                    placeholder={ovScope === "ALGO" ? "(all)" : "e.g. RELIANCE"}
                    onChange={(e) => setOvTarget(e.target.value)}
                  />
                </Field>
                <Field label="Book at %">
                  <NumberInput step="0.1" className={inputClass} value={ovAtPct} onChange={setOvAtPct} />
                </Field>
                <Field label="Book %">
                  <NumberInput className={inputClass} value={ovBookPct} onChange={setOvBookPct} />
                </Field>
                <Field label="Trail SL %">
                  <NumberInput step="0.1" className={inputClass} value={ovTrailPct} onChange={setOvTrailPct} />
                </Field>
              </div>
            )}
          </div>
          )}

          <div className="rounded-lg border border-[var(--divider)] p-3">
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={sweepMode} onChange={(e) => setSweepMode(e.target.checked)} />
              <span className="font-medium">Sweep a parameter (multi-run)</span>
              <span className="text-[var(--faint)]">— run up to 5 variants and compare them</span>
            </label>
            {sweepMode && (
              <div className="grid md:grid-cols-2 gap-3 mt-3">
                <Field label="Parameter to vary">
                  <select className={inputClass} value={sweepField} onChange={(e) => setSweepField(e.target.value)}>
                    {sweepableFields.map((f) => (
                      <option key={f.key} value={f.key}>{f.label}</option>
                    ))}
                  </select>
                </Field>
                <Field label="Values (comma-separated, 2–5)">
                  <input
                    className={inputClass}
                    value={sweepValues}
                    onChange={(e) => setSweepValues(e.target.value)}
                    placeholder="e.g. 4, 6, 8, 10"
                  />
                </Field>
              </div>
            )}
          </div>

          <button
            type="submit"
            disabled={mutation.isPending || sweepProgress != null}
            className="rounded-md bg-[var(--ft)] px-4 py-2 text-sm font-medium disabled:opacity-50"
          >
            {sweepProgress
              ? `Running ${sweepProgress.done + 1}/${sweepProgress.total}…`
              : mutation.isPending
                ? "Running…"
                : sweepMode
                  ? "Run sweep"
                  : "Run backtest"}
          </button>
        </form>
      </Panel>

      {sweepError && <ErrorBox message={sweepError} />}
      {mutation.error && <ErrorBox message={(mutation.error as Error).message} />}

      {result && (
        <div className="space-y-3">
          <div className="flex items-center gap-3 flex-wrap">
            <h2 className="font-semibold">Result</h2>
            {result.run_id != null ? (
              <Link to={`/runs/${result.run_id}`} className="text-[var(--accent-deep)] text-sm underline">
                open run #{result.run_id}
              </Link>
            ) : (
              <>
                <span className="text-[var(--faint)] text-sm">preview · not saved</span>
                <button
                  onClick={() =>
                    mutation.variables &&
                    // name/notes are metadata (don't affect the computed report/trades), so take
                    // them from the CURRENT form — an edit after previewing must not be lost to the
                    // stale preview-time request.
                    saveMutation.mutate({
                      request: { ...mutation.variables, name: name.trim() || undefined, notes: notes.trim() || undefined },
                      report: result.report,
                      trades: result.trades,
                    })
                  }
                  disabled={saveMutation.isPending}
                  className="rounded-md bg-[var(--ft)] text-white px-3 py-1.5 text-sm font-medium disabled:opacity-50"
                >
                  {saveMutation.isPending ? "Saving…" : "Save backtest"}
                </button>
              </>
            )}
          </div>
          {saveMutation.error && <ErrorBox message={(saveMutation.error as Error).message} />}
          <ReportView
            report={result.report}
            trades={result.trades}
            onDownloadCsv={
              result.run_id != null ? () => api.downloadTradesCsv(result.run_id!) : undefined
            }
            runId={result.run_id ?? undefined}
          />
        </div>
      )}
    </div>
  );
}
