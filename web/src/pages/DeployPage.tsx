import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { api, brokers } from "../api/client";
import { Card, ErrorBox, NumberInput } from "../components/ui";
import type { ForwardTestPrefill, StartLiveRequest } from "../types";

const inputClass =
  "w-full rounded-md bg-slate-800 border border-slate-700 px-3 py-2 text-sm focus:outline-none focus:border-brand";
const lbl = "block text-xs text-slate-400 mb-1";

export default function DeployPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const pf = (location.state as { prefill?: ForwardTestPrefill } | null)?.prefill;

  const { data: strategyData } = useQuery({ queryKey: ["strategies"], queryFn: api.strategies });
  const { data: universeData } = useQuery({ queryKey: ["universes"], queryFn: api.universes });
  const { data: accounts } = useQuery({ queryKey: ["brokers"], queryFn: brokers.list });

  const pfParams = (pf?.params ?? {}) as Record<string, unknown>;
  const { symbols: pfSymbols, lookback: pfLookback, tax_rate: pfTax, withdrawal_rate: pfWd, ...pfStrategyParams } =
    pfParams as { symbols?: string[]; lookback?: number; tax_rate?: number; withdrawal_rate?: number };

  const [name, setName] = useState(pf?.name ?? "");
  const [notes, setNotes] = useState("");
  const [strategyId, setStrategyId] = useState(pf?.strategy_id ?? "sst_lifo");
  const [universe, setUniverse] = useState(pf ? "" : "nifty50");
  const [symbols, setSymbols] = useState((pfSymbols ?? ["RELIANCE", "TCS", "INFY"]).join(", "));
  const [capital, setCapital] = useState(pf?.capital ?? 1000000);
  const [parts, setParts] = useState(10);
  const [target, setTarget] = useState(6);
  const [target1, setTarget1] = useState(10);
  const [target2, setTarget2] = useState(8);
  const [target3, setTarget3] = useState(6);
  const [maxLots, setMaxLots] = useState(0);
  const [taxRate, setTaxRate] = useState(20);
  const [withdrawalRate, setWithdrawalRate] = useState(0);
  const [lookback, setLookback] = useState(20);
  const [allocationMode, setAllocationMode] = useState("fixed");
  const [quoteSource, setQuoteSource] = useState("cache");
  const [accountId, setAccountId] = useState<number | null>(null);
  const [ignoreHours, setIgnoreHours] = useState(true);
  const [auto, setAuto] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const isFifo = strategyId === "sst_fifo";
  const sessioned = (accounts ?? []).filter((a) => a.has_session);

  async function deploy() {
    setBusy(true);
    setError(null);
    const isCustom = universe === "";
    const manualParams = {
      capital_parts: parts,
      max_lots: maxLots,
      allocation_mode: allocationMode,
      ...(isFifo
        ? { profit_target_1: target1 / 100, profit_target_2: target2 / 100, profit_target_3: target3 / 100 }
        : { profit_target: target / 100 }),
    };
    const body: StartLiveRequest = {
      strategy_id: strategyId,
      name: name.trim() || undefined,
      notes: notes.trim() || undefined,
      universe: isCustom ? null : universe,
      symbols: isCustom ? symbols.split(",").map((s) => s.trim()).filter(Boolean) : [],
      capital,
      params: pf ? pfStrategyParams : manualParams,
      tax_rate: pf ? (pfTax ?? 0.2) : taxRate / 100,
      withdrawal_rate: pf ? (pfWd ?? 0) : withdrawalRate / 100,
      lookback: pf ? (pfLookback ?? 20) : lookback,
      quote_source: quoteSource,
      broker_account_id: quoteSource === "zerodha" ? accountId : null,
      ignore_market_hours: ignoreHours,
      auto,
    };
    try {
      await api.liveStart(body);
      navigate("/live");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <Link to="/live" className="text-slate-400 hover:text-slate-200 text-sm">← Live</Link>
        <h1 className="text-lg font-semibold">{pf ? "Forward-test a backtest" : "Deploy a strategy"}</h1>
      </div>

      <Card>
        <div className="grid md:grid-cols-2 gap-3 mb-3">
          <label className="block">
            <span className={lbl}>Deployment name</span>
            <input className={inputClass} placeholder="e.g. SST Nifty50 paper" value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <label className="block">
            <span className={lbl}>Notes</span>
            <input className={inputClass} placeholder="why / what you're testing" value={notes} onChange={(e) => setNotes(e.target.value)} />
          </label>
        </div>

        {pf ? (
          <div className="text-sm text-slate-400 mb-3">
            {pf.strategy_id} · {(pfSymbols ?? []).length} symbols · params from backtest (capital editable below)
          </div>
        ) : (
          <div className="space-y-3 mb-3">
            <div className="grid md:grid-cols-3 gap-3">
              <label className="block">
                <span className={lbl}>Strategy</span>
                <select className={inputClass} value={strategyId} onChange={(e) => setStrategyId(e.target.value)}>
                  {(strategyData?.strategies ?? ["sst_lifo"]).map((s) => (
                    <option key={s} value={s}>{s}</option>
                  ))}
                </select>
              </label>
              <label className="block">
                <span className={lbl}>Universe</span>
                <select className={inputClass} value={universe} onChange={(e) => setUniverse(e.target.value)}>
                  {(universeData ?? []).map((u) => (
                    <option key={u.name} value={u.name}>{u.label} ({u.count})</option>
                  ))}
                  <option value="">Custom</option>
                </select>
              </label>
              <label className="block">
                <span className={lbl}>Symbols</span>
                {universe === "" ? (
                  <input className={inputClass} value={symbols} onChange={(e) => setSymbols(e.target.value)} />
                ) : (
                  <input className={`${inputClass} text-slate-500`} disabled value="(from universe)" />
                )}
              </label>
            </div>
            <div className="grid md:grid-cols-3 lg:grid-cols-6 gap-3">
              <label className="block">
                <span className={lbl}>Capital parts</span>
                <NumberInput className={inputClass} value={parts} onChange={setParts} />
              </label>
              {isFifo ? (
                <>
                  <label className="block"><span className={lbl}>Target % (1 lot)</span><NumberInput step="0.1" className={inputClass} value={target1} onChange={setTarget1} /></label>
                  <label className="block"><span className={lbl}>Target % (2)</span><NumberInput step="0.1" className={inputClass} value={target2} onChange={setTarget2} /></label>
                  <label className="block"><span className={lbl}>Target % (3+)</span><NumberInput step="0.1" className={inputClass} value={target3} onChange={setTarget3} /></label>
                </>
              ) : (
                <label className="block"><span className={lbl}>Profit target %</span><NumberInput step="0.1" className={inputClass} value={target} onChange={setTarget} /></label>
              )}
              <label className="block"><span className={lbl}>Max lots (0=∞)</span><NumberInput className={inputClass} value={maxLots} onChange={setMaxLots} /></label>
              <label className="block"><span className={lbl}>Lookback</span><NumberInput className={inputClass} value={lookback} onChange={setLookback} /></label>
              <label className="block"><span className={lbl}>Tax rate %</span><NumberInput className={inputClass} value={taxRate} onChange={setTaxRate} /></label>
              <label className="block"><span className={lbl}>Withdrawal %</span><NumberInput className={inputClass} value={withdrawalRate} onChange={setWithdrawalRate} /></label>
              <label className="block">
                <span className={lbl}>Position sizing</span>
                <select className={inputClass} value={allocationMode} onChange={(e) => setAllocationMode(e.target.value)}>
                  <option value="fixed">Fixed</option>
                  <option value="equity_scaled">Equity-scaled</option>
                </select>
              </label>
            </div>
          </div>
        )}

        <div className="grid md:grid-cols-4 gap-3 items-end">
          <label className="block">
            <span className={lbl}>Capital (₹)</span>
            <NumberInput className={inputClass} value={capital} onChange={setCapital} />
          </label>
          <label className="block">
            <span className={lbl}>Quotes</span>
            <select className={inputClass} value={quoteSource} onChange={(e) => setQuoteSource(e.target.value)}>
              <option value="cache">Cache (last close, offline)</option>
              <option value="zerodha">Zerodha (live)</option>
            </select>
          </label>
          {quoteSource === "zerodha" && (
            <label className="block">
              <span className={lbl}>Account</span>
              <select className={inputClass} value={accountId ?? ""} onChange={(e) => setAccountId(e.target.value ? +e.target.value : null)}>
                <option value="">select…</option>
                {sessioned.map((a) => (<option key={a.id} value={a.id}>{a.label}</option>))}
              </select>
            </label>
          )}
          <div className="flex flex-col gap-1">
            <label className="flex items-center gap-2 text-sm text-slate-300">
              <input type="checkbox" checked={ignoreHours} onChange={(e) => setIgnoreHours(e.target.checked)} />
              ignore market hours
            </label>
            <label className="flex items-center gap-2 text-sm text-slate-300">
              <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
              auto loop (refresh + daily decision)
            </label>
          </div>
        </div>

        <div className="mt-4 flex items-center gap-3">
          <button
            onClick={deploy}
            disabled={busy || (quoteSource === "zerodha" && !accountId)}
            className="rounded-md bg-brand hover:bg-brand-light px-4 py-2 text-sm font-medium disabled:opacity-50"
          >
            {busy ? "Deploying…" : "Deploy"}
          </button>
          <span className="text-xs text-slate-500">
            {quoteSource === "zerodha" ? "Live quotes" : "Cache quotes (offline)"} · simulated fills · no real orders
          </span>
        </div>
        {error && <div className="mt-2"><ErrorBox message={error} /></div>}
      </Card>
    </div>
  );
}
