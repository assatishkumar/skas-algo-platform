import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import ReportView from "../components/ReportView";
import { Card, ErrorBox, NumberInput } from "../components/ui";
import type { BacktestRequest, OverrideInput } from "../types";

// Fields a sweep can vary, with how each value maps into the request.
// unit "pct" values are divided by 100 (like the form); "num" pass through.
type SweepField = {
  key: string; label: string; unit: "pct" | "num";
  fifoOnly?: boolean; lifoOnly?: boolean; optionsOnly?: boolean; stockOnly?: boolean;
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
  { key: "profit_target_pct", label: "Profit target %", unit: "pct", optionsOnly: true },
  { key: "strike_step", label: "Strike step (pts)", unit: "num", optionsOnly: true },
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
      <span className="block text-xs uppercase tracking-wide text-slate-400 mb-1">{label}</span>
      {children}
    </label>
  );
}

const inputClass =
  "w-full rounded-md bg-slate-800 border border-slate-700 px-3 py-2 text-sm focus:outline-none focus:border-brand";

export default function NewBacktestPage() {
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
  const [creditLimitPct, setCreditLimitPct] = useState(1); // % of capital
  const [crProfitPct, setCrProfitPct] = useState(2.5);
  const [crStopPct, setCrStopPct] = useState(3);
  const [maxHoldingDays, setMaxHoldingDays] = useState(20);
  const [minVix, setMinVix] = useState(0); // 0 = off; skip entry if ATM IV% (≈VIX) below
  const [combinedCreditPct, setCombinedCreditPct] = useState(2); // batman: cap on both wings' credit

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
  const isFifo = strategyId === "sst_fifo";
  const isCallRatio = ["call_ratio_monthly", "put_ratio_monthly", "batman_ratio_monthly"].includes(strategyId);
  const ratioSide =
    strategyId === "put_ratio_monthly" ? "put"
    : strategyId === "batman_ratio_monthly" ? "batman"
    : "call";
  const isOptions = strategyId === "short_premium" || isCallRatio;
  const strikeUnit =
    strikeMode === "delta" ? "Δ"
    : strikeMode === "sd" ? "× exp.move (σ)"
    : strikeMode === "points" ? "offset (pts)"
    : "offset (% OTM)";

  // Ratio strategies deploy ~₹1L of margin per lot per wing; default the capital so the
  // %-of-capital targets are meaningful (Batman = both wings ≈ 2×). User can still edit.
  useEffect(() => {
    if (isCallRatio) setCapital(ratioSide === "batman" ? 200000 : 100000);
  }, [isCallRatio, ratioSide]);

  // Reset the buy/sell/hedge offsets to sensible defaults for the chosen strike basis.
  useEffect(() => {
    const d =
      strikeMode === "delta" ? [0.36, 0.25, 0.05]
      : strikeMode === "sd" ? [0.35, 0.7, 1.85] // multiples of the 1σ expected move
      : strikeMode === "points" ? [300, 600, 1600]
      : [1.3, 2.6, 7.0]; // percent (% OTM)
    setBuyOffset(d[0]);
    setSellOffset(d[1]);
    setHedgeOffset(d[2]);
  }, [strikeMode]);

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

  const mutation = useMutation({
    mutationFn: (body: BacktestRequest) => api.backtest(body),
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
      const params: Record<string, unknown> = isCallRatio
        ? {
            underlying,
            strike_mode: strikeMode,
            buy_offset: buyOffset,
            sell_offset: sellOffset,
            hedge_offset: hedgeOffset,
            lots: crLots,
            credit_debit_limit_pct: creditLimitPct / 100,
            profit_target_pct: crProfitPct / 100,
            stop_loss_pct: crStopPct / 100,
            max_holding_days: maxHoldingDays,
            min_vix: minVix,
            ...(ratioSide === "batman" ? { combined_credit_limit_pct: combinedCreditPct / 100 } : {}),
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
        universe: null,
        symbols: [],
        start_date: startDate,
        end_date: endDate,
        capital,
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
      params: {
        capital_parts: parts,
        max_lots: maxLots,
        allocation_mode: allocationMode,
        ...(isFifo
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
    mutation.mutate(buildBody());
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
        const res = await api.backtest(variant);
        runIds.push(res.run_id);
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
      (!f.optionsOnly || isOptions) &&
      (!f.stockOnly || !isOptions) &&
      (isOptions || ((!f.fifoOnly || isFifo) && (!f.lifoOnly || !isFifo))),
  );
  // Keep the swept field valid when the strategy (and thus its params) changes.
  useEffect(() => {
    if (!sweepableFields.some((f) => f.key === sweepField)) {
      setSweepField(isOptions ? "profit_target_pct" : isFifo ? "profit_target_1" : "profit_target");
    }
  }, [isFifo, isOptions, sweepField, sweepableFields]);

  return (
    <div className="space-y-6">
      <h1 className="text-lg font-semibold">New backtest</h1>

      <Card>
        <form onSubmit={submit} className="space-y-4">
          <div className="grid md:grid-cols-2 gap-4">
            <Field label="Name">
              <input className={inputClass} placeholder="e.g. SST Nifty50 2015-26" value={name} onChange={(e) => setName(e.target.value)} />
            </Field>
            <Field label="Notes">
              <input className={inputClass} placeholder="what you're testing / why" value={notes} onChange={(e) => setNotes(e.target.value)} />
            </Field>
          </div>
          <div className="grid md:grid-cols-2 gap-4">
            <Field label="Strategy">
              <select className={inputClass} value={strategyId} onChange={(e) => setStrategyId(e.target.value)}>
                {strategies.map((s) => (
                  <option key={s} value={s}>{s}</option>
                ))}
              </select>
            </Field>
            {isOptions ? (
              <Field label="Underlying">
                <select className={inputClass} value={underlying} onChange={(e) => setUnderlying(e.target.value)}>
                  <option value="NIFTY">NIFTY</option>
                  <option value="BANKNIFTY">BANKNIFTY</option>
                  <option value="GOLD">GOLD (synthetic)</option>
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
                    <div className={`${inputClass} text-slate-400`}>
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

          {isCallRatio ? (
            <div className="grid md:grid-cols-3 gap-4">
              <div className="md:col-span-3 text-[11px] text-amber-300/90">
                {ratioSide === "batman"
                  ? "Batman: BOTH 1:2 ratio wings (call above + put below spot, each hedged; 6 legs). Both wings must qualify for credit or the month is skipped; one combined target/stop/time exit. Risk = a fast move EITHER way; margin ≈ ₹2L per lot."
                  : ratioSide === "put"
                    ? "1:2 put ratio + outer hedge on NIFTY monthly (strikes BELOW spot — zero upside risk; watch fast sell-offs)."
                    : "1:2 call ratio + outer hedge on NIFTY monthly (strikes ABOVE spot — zero downside risk; watch fast rallies)."}{" "}
                Entry = last Tuesday of the month for next month's expiry (EOD approximates the 3:16 PM
                rule). All %s are on this capital.
              </div>
              <Field label="Capital (₹) — ≈ ₹1L / lot">
                <NumberInput className={inputClass} value={capital} onChange={setCapital} />
              </Field>
              <Field label="Lots (1 buy : 2 sell : 1 hedge)">
                <NumberInput className={inputClass} value={crLots} onChange={setCrLots} />
              </Field>
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
              <div className="md:col-span-3 text-[11px] text-slate-500 -mt-2">
                Entry always requires a <span className="text-slate-400">net credit</span> ≤ the max above
                (strikes auto-shift further OTM when the credit is too rich). Debit months (low IV / thin
                premiums) are <span className="text-slate-400">skipped</span>. NIFTY lot sizes are historical
                (50 → 25 → 75 → 65 per SEBI revisions).
              </div>
              <Field label="Tax rate %">
                <NumberInput className={inputClass} value={taxRate} onChange={setTaxRate} />
              </Field>
              <Field label="Withdrawal rate %">
                <NumberInput step="1" className={inputClass} value={withdrawalRate} onChange={setWithdrawalRate} />
              </Field>
            </div>
          ) : isOptions ? (
            <div className="grid md:grid-cols-3 gap-4">
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
          <div className="grid md:grid-cols-3 gap-4">
            <Field label="Capital (₹)">
              <NumberInput className={inputClass} value={capital} onChange={setCapital} />
            </Field>
            <Field label="Capital parts">
              <NumberInput className={inputClass} value={parts} onChange={setParts} />
            </Field>
            {isFifo ? (
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
            <Field label="Max lots (0 = unlimited)">
              <NumberInput className={inputClass} value={maxLots} onChange={setMaxLots} />
            </Field>
            <Field label="Tax rate %">
              <NumberInput className={inputClass} value={taxRate} onChange={setTaxRate} />
            </Field>
            <Field label="Withdrawal rate %">
              <NumberInput step="1" className={inputClass} value={withdrawalRate} onChange={setWithdrawalRate} />
            </Field>
            <Field label="Lookback (days)">
              <NumberInput className={inputClass} value={lookback} onChange={setLookback} />
            </Field>
            <Field label="Position sizing">
              <select className={inputClass} value={allocationMode} onChange={(e) => setAllocationMode(e.target.value)}>
                <option value="fixed">Fixed (capital / parts)</option>
                <option value="equity_scaled">Equity-scaled (compounds)</option>
              </select>
            </Field>
          </div>
          )}

          {!isOptions && (
          <div className="rounded-lg border border-slate-800 p-3">
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={ovEnabled} onChange={(e) => setOvEnabled(e.target.checked)} />
              <span className="font-medium">Apply exit override</span>
              <span className="text-slate-500">— book a portion at a target, trail the rest</span>
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

          <div className="rounded-lg border border-slate-800 p-3">
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={sweepMode} onChange={(e) => setSweepMode(e.target.checked)} />
              <span className="font-medium">Sweep a parameter (multi-run)</span>
              <span className="text-slate-500">— run up to 5 variants and compare them</span>
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
            className="rounded-md bg-brand hover:bg-brand-light px-4 py-2 text-sm font-medium disabled:opacity-50"
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
      </Card>

      {sweepError && <ErrorBox message={sweepError} />}
      {mutation.error && <ErrorBox message={(mutation.error as Error).message} />}

      {result && (
        <div className="space-y-3">
          <div className="flex items-center gap-3">
            <h2 className="font-semibold">Result</h2>
            <Link to={`/runs/${result.run_id}`} className="text-brand-light text-sm underline">
              open run #{result.run_id}
            </Link>
          </div>
          <ReportView
            report={result.report}
            trades={result.trades}
            csvUrl={api.tradesCsvUrl(result.run_id)}
            runId={result.run_id}
          />
        </div>
      )}
    </div>
  );
}
