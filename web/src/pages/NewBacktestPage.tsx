import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import ReportView from "../components/ReportView";
import { Card, ErrorBox } from "../components/ui";
import type { BacktestRequest, OverrideInput } from "../types";

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

  const [strategyId, setStrategyId] = useState("sst_lifo");
  const [symbols, setSymbols] = useState("RELIANCE, TCS, INFY, HDFCBANK, ICICIBANK");
  const [startDate, setStartDate] = useState("2018-01-01");
  const [endDate, setEndDate] = useState("2022-12-31");
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

  // Override builder
  const [ovEnabled, setOvEnabled] = useState(false);
  const [ovScope, setOvScope] = useState("ALGO");
  const [ovTarget, setOvTarget] = useState("");
  const [ovAtPct, setOvAtPct] = useState(6);
  const [ovBookPct, setOvBookPct] = useState(50);
  const [ovTrailPct, setOvTrailPct] = useState(2);

  const isFifo = strategyId === "sst_fifo";

  const mutation = useMutation({
    mutationFn: (body: BacktestRequest) => api.backtest(body),
  });

  function submit(e: React.FormEvent) {
    e.preventDefault();
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
    const body: BacktestRequest = {
      strategy_id: strategyId,
      symbols: symbols.split(",").map((s) => s.trim()).filter(Boolean),
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
    mutation.mutate(body);
  }

  const result = mutation.data;

  return (
    <div className="space-y-6">
      <h1 className="text-lg font-semibold">New backtest</h1>

      <Card>
        <form onSubmit={submit} className="space-y-4">
          <div className="grid md:grid-cols-2 gap-4">
            <Field label="Strategy">
              <select className={inputClass} value={strategyId} onChange={(e) => setStrategyId(e.target.value)}>
                {strategies.map((s) => (
                  <option key={s} value={s}>{s}</option>
                ))}
              </select>
            </Field>
            <Field label="Symbols (comma-separated)">
              <input className={inputClass} value={symbols} onChange={(e) => setSymbols(e.target.value)} />
            </Field>
            <Field label="Start date">
              <input type="date" className={inputClass} value={startDate} onChange={(e) => setStartDate(e.target.value)} />
            </Field>
            <Field label="End date">
              <input type="date" className={inputClass} value={endDate} onChange={(e) => setEndDate(e.target.value)} />
            </Field>
          </div>

          <div className="grid md:grid-cols-3 gap-4">
            <Field label="Capital (₹)">
              <input type="number" className={inputClass} value={capital} onChange={(e) => setCapital(+e.target.value)} />
            </Field>
            <Field label="Capital parts">
              <input type="number" className={inputClass} value={parts} onChange={(e) => setParts(+e.target.value)} />
            </Field>
            {isFifo ? (
              <>
                <Field label="Target % (1 lot)">
                  <input type="number" step="0.1" className={inputClass} value={target1} onChange={(e) => setTarget1(+e.target.value)} />
                </Field>
                <Field label="Target % (2 lots)">
                  <input type="number" step="0.1" className={inputClass} value={target2} onChange={(e) => setTarget2(+e.target.value)} />
                </Field>
                <Field label="Target % (3+ lots)">
                  <input type="number" step="0.1" className={inputClass} value={target3} onChange={(e) => setTarget3(+e.target.value)} />
                </Field>
              </>
            ) : (
              <Field label="Profit target %">
                <input type="number" step="0.1" className={inputClass} value={target} onChange={(e) => setTarget(+e.target.value)} />
              </Field>
            )}
            <Field label="Max lots (0 = unlimited)">
              <input type="number" className={inputClass} value={maxLots} onChange={(e) => setMaxLots(+e.target.value)} />
            </Field>
            <Field label="Tax rate %">
              <input type="number" className={inputClass} value={taxRate} onChange={(e) => setTaxRate(+e.target.value)} />
            </Field>
            <Field label="Withdrawal rate %">
              <input type="number" step="1" className={inputClass} value={withdrawalRate} onChange={(e) => setWithdrawalRate(+e.target.value)} />
            </Field>
            <Field label="Lookback (days)">
              <input type="number" className={inputClass} value={lookback} onChange={(e) => setLookback(+e.target.value)} />
            </Field>
            <Field label="Position sizing">
              <select className={inputClass} value={allocationMode} onChange={(e) => setAllocationMode(e.target.value)}>
                <option value="fixed">Fixed (capital / parts)</option>
                <option value="equity_scaled">Equity-scaled (compounds)</option>
              </select>
            </Field>
          </div>

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
                  <input type="number" step="0.1" className={inputClass} value={ovAtPct} onChange={(e) => setOvAtPct(+e.target.value)} />
                </Field>
                <Field label="Book %">
                  <input type="number" className={inputClass} value={ovBookPct} onChange={(e) => setOvBookPct(+e.target.value)} />
                </Field>
                <Field label="Trail SL %">
                  <input type="number" step="0.1" className={inputClass} value={ovTrailPct} onChange={(e) => setOvTrailPct(+e.target.value)} />
                </Field>
              </div>
            )}
          </div>

          <button
            type="submit"
            disabled={mutation.isPending}
            className="rounded-md bg-brand hover:bg-brand-light px-4 py-2 text-sm font-medium disabled:opacity-50"
          >
            {mutation.isPending ? "Running…" : "Run backtest"}
          </button>
        </form>
      </Card>

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
          />
        </div>
      )}
    </div>
  );
}
