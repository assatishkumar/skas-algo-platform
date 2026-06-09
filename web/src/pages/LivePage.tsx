import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import { api, brokers, liveWsUrl } from "../api/client";
import { Badge, Card, ErrorBox } from "../components/ui";
import { formatInr } from "../lib/format";
import type {
  ForwardTestPrefill,
  LiveRunSnapshot,
  LiveTradeEvent,
  LiveWsMessage,
  StartLiveRequest,
} from "../types";

const inputClass =
  "w-full rounded-md bg-slate-800 border border-slate-700 px-3 py-2 text-sm focus:outline-none focus:border-brand";

function useLiveFeed() {
  const [snapshots, setSnapshots] = useState<Record<number, LiveRunSnapshot>>({});
  const [trades, setTrades] = useState<(LiveTradeEvent & { run_id: number })[]>([]);
  const [connected, setConnected] = useState(false);

  const seed = useCallback(async () => {
    const list = await api.liveList();
    setSnapshots(Object.fromEntries(list.map((r) => [r.run_id, r])));
  }, []);

  useEffect(() => {
    seed();
    const ws = new WebSocket(liveWsUrl());
    ws.onopen = () => setConnected(true);
    ws.onclose = () => setConnected(false);
    ws.onmessage = (e) => {
      const msg: LiveWsMessage = JSON.parse(e.data);
      if (msg.type === "snapshot") {
        setSnapshots((prev) => ({
          ...prev,
          [msg.run_id]: { ...prev[msg.run_id], ...msg } as LiveRunSnapshot,
        }));
      } else if (msg.type === "trades" && msg.events) {
        setTrades((prev) =>
          [...msg.events!.map((ev) => ({ ...ev, run_id: msg.run_id })), ...prev].slice(0, 50),
        );
      } else if (msg.type === "stopped") {
        setSnapshots((prev) =>
          prev[msg.run_id]
            ? { ...prev, [msg.run_id]: { ...prev[msg.run_id], status: "stopped" } }
            : prev,
        );
      }
    };
    return () => ws.close();
  }, [seed]);

  return { snapshots, trades, connected, seed };
}

function StartForm({ onStarted, prefill }: { onStarted: () => void; prefill?: ForwardTestPrefill }) {
  const { data: strategyData } = useQuery({ queryKey: ["strategies"], queryFn: api.strategies });
  const { data: universeData } = useQuery({ queryKey: ["universes"], queryFn: api.universes });
  const { data: accounts } = useQuery({ queryKey: ["brokers"], queryFn: brokers.list });

  // Split a backtest's stored params into the non-strategy keys and the strategy params.
  const pf = prefill;
  const pfParams = (pf?.params ?? {}) as Record<string, unknown>;
  const { symbols: pfSymbols, lookback: pfLookback, tax_rate: pfTax, withdrawal_rate: pfWd, ...pfStrategyParams } =
    pfParams as {
      symbols?: string[];
      lookback?: number;
      tax_rate?: number;
      withdrawal_rate?: number;
    };

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

  async function start() {
    setBusy(true);
    setError(null);
    const isCustom = universe === "";
    const isFifo = strategyId === "sst_fifo";
    // Prefill carries the backtest's exact strategy params + tax/withdrawal/lookback.
    const manualParams = {
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
    };
    const params = pf ? pfStrategyParams : manualParams;
    const body: StartLiveRequest = {
      strategy_id: strategyId,
      universe: isCustom ? null : universe,
      symbols: isCustom ? symbols.split(",").map((s) => s.trim()).filter(Boolean) : [],
      capital,
      params,
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
      onStarted();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const sessioned = (accounts ?? []).filter((a) => a.has_session);
  const isFifo = strategyId === "sst_fifo";
  const labeled = "block";
  const lbl = "block text-xs text-slate-400 mb-1";

  return (
    <Card>
      <div className="text-sm font-medium text-slate-300 mb-3">
        {pf ? `Forward-test: ${pf.name ?? pf.strategy_id}` : "Start a paper algo"}
      </div>

      {pf ? (
        <div className="text-sm text-slate-400 mb-3">
          {pf.strategy_id} · {(pfSymbols ?? []).length} symbols · params from backtest (editable capital below)
        </div>
      ) : (
        <div className="space-y-3 mb-3">
          <div className="grid md:grid-cols-3 gap-3">
            <label className={labeled}>
              <span className={lbl}>Strategy</span>
              <select className={inputClass} value={strategyId} onChange={(e) => setStrategyId(e.target.value)}>
                {(strategyData?.strategies ?? ["sst_lifo"]).map((s) => (
                  <option key={s} value={s}>{s}</option>
                ))}
              </select>
            </label>
            <label className={labeled}>
              <span className={lbl}>Universe</span>
              <select className={inputClass} value={universe} onChange={(e) => setUniverse(e.target.value)}>
                {(universeData ?? []).map((u) => (
                  <option key={u.name} value={u.name}>{u.label} ({u.count})</option>
                ))}
                <option value="">Custom</option>
              </select>
            </label>
            <label className={labeled}>
              <span className={lbl}>Symbols</span>
              {universe === "" ? (
                <input className={inputClass} value={symbols} onChange={(e) => setSymbols(e.target.value)} />
              ) : (
                <input className={`${inputClass} text-slate-500`} disabled value="(from universe)" />
              )}
            </label>
          </div>
          <div className="grid md:grid-cols-3 lg:grid-cols-6 gap-3">
            <label className={labeled}>
              <span className={lbl}>Capital parts</span>
              <input type="number" className={inputClass} value={parts} onChange={(e) => setParts(+e.target.value)} />
            </label>
            {isFifo ? (
              <>
                <label className={labeled}>
                  <span className={lbl}>Target % (1 lot)</span>
                  <input type="number" step="0.1" className={inputClass} value={target1} onChange={(e) => setTarget1(+e.target.value)} />
                </label>
                <label className={labeled}>
                  <span className={lbl}>Target % (2)</span>
                  <input type="number" step="0.1" className={inputClass} value={target2} onChange={(e) => setTarget2(+e.target.value)} />
                </label>
                <label className={labeled}>
                  <span className={lbl}>Target % (3+)</span>
                  <input type="number" step="0.1" className={inputClass} value={target3} onChange={(e) => setTarget3(+e.target.value)} />
                </label>
              </>
            ) : (
              <label className={labeled}>
                <span className={lbl}>Profit target %</span>
                <input type="number" step="0.1" className={inputClass} value={target} onChange={(e) => setTarget(+e.target.value)} />
              </label>
            )}
            <label className={labeled}>
              <span className={lbl}>Max lots (0=∞)</span>
              <input type="number" className={inputClass} value={maxLots} onChange={(e) => setMaxLots(+e.target.value)} />
            </label>
            <label className={labeled}>
              <span className={lbl}>Lookback</span>
              <input type="number" className={inputClass} value={lookback} onChange={(e) => setLookback(+e.target.value)} />
            </label>
            <label className={labeled}>
              <span className={lbl}>Tax rate %</span>
              <input type="number" className={inputClass} value={taxRate} onChange={(e) => setTaxRate(+e.target.value)} />
            </label>
            <label className={labeled}>
              <span className={lbl}>Withdrawal %</span>
              <input type="number" className={inputClass} value={withdrawalRate} onChange={(e) => setWithdrawalRate(+e.target.value)} />
            </label>
            <label className={labeled}>
              <span className={lbl}>Position sizing</span>
              <select className={inputClass} value={allocationMode} onChange={(e) => setAllocationMode(e.target.value)}>
                <option value="fixed">Fixed</option>
                <option value="equity_scaled">Equity-scaled</option>
              </select>
            </label>
          </div>
        </div>
      )}

      <div className="grid md:grid-cols-4 gap-3 items-center">
        <label className="block">
          <span className="block text-xs text-slate-400 mb-1">Capital (₹)</span>
          <input type="number" className={inputClass} value={capital} onChange={(e) => setCapital(+e.target.value)} />
        </label>
        <label className="block">
          <span className="block text-xs text-slate-400 mb-1">Quotes</span>
          <select className={inputClass} value={quoteSource} onChange={(e) => setQuoteSource(e.target.value)}>
            <option value="cache">Cache (last close, offline)</option>
            <option value="zerodha">Zerodha (live)</option>
          </select>
        </label>
        {quoteSource === "zerodha" && (
          <label className="block">
            <span className="block text-xs text-slate-400 mb-1">Account</span>
            <select className={inputClass} value={accountId ?? ""} onChange={(e) => setAccountId(e.target.value ? +e.target.value : null)}>
              <option value="">select…</option>
              {sessioned.map((a) => (
                <option key={a.id} value={a.id}>{a.label}</option>
              ))}
            </select>
          </label>
        )}
        <div className="flex flex-col gap-1 pt-4">
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

      <div className="mt-3 flex items-center gap-3">
        <button
          onClick={start}
          disabled={busy || (quoteSource === "zerodha" && !accountId)}
          className="rounded-md bg-brand hover:bg-brand-light px-4 py-2 text-sm font-medium disabled:opacity-50"
        >
          {busy ? "Starting…" : pf ? "Start forward test" : "Start paper run"}
        </button>
        <span className="text-xs text-slate-500">
          {quoteSource === "zerodha"
            ? "Live quotes · simulated fills · no real orders"
            : "Cache quotes (works offline) · simulated fills · no real orders"}
        </span>
      </div>
      {error && <div className="mt-2"><ErrorBox message={error} /></div>}
    </Card>
  );
}

function OverridePanel({ runId, onDone }: { runId: number; onDone: () => void }) {
  const [atPct, setAtPct] = useState(6);
  const [bookPct, setBookPct] = useState(50);
  const [trailPct, setTrailPct] = useState(2);
  async function apply() {
    await api.liveAddOverride(runId, {
      scope: "ALGO",
      target: null,
      rule: {
        exit: [
          { at_pct: atPct, action: "book", qty_pct: bookPct },
          { action: "trail_sl", trail_pct: trailPct },
        ],
      },
    });
    onDone();
  }
  return (
    <div className="mt-3 flex flex-wrap items-end gap-2 border-t border-slate-800 pt-3">
      <span className="text-xs text-slate-400">Intervene: at</span>
      <input type="number" step="0.1" className="w-16 rounded bg-slate-800 border border-slate-700 px-2 py-1 text-sm" value={atPct} onChange={(e) => setAtPct(+e.target.value)} />
      <span className="text-xs text-slate-400">% book</span>
      <input type="number" className="w-16 rounded bg-slate-800 border border-slate-700 px-2 py-1 text-sm" value={bookPct} onChange={(e) => setBookPct(+e.target.value)} />
      <span className="text-xs text-slate-400">% trail</span>
      <input type="number" step="0.1" className="w-16 rounded bg-slate-800 border border-slate-700 px-2 py-1 text-sm" value={trailPct} onChange={(e) => setTrailPct(+e.target.value)} />
      <span className="text-xs text-slate-400">%</span>
      <button onClick={apply} className="rounded bg-slate-700 hover:bg-slate-600 px-3 py-1 text-xs">
        Apply to run
      </button>
    </div>
  );
}

function RunCard({ run, onChanged }: { run: LiveRunSnapshot; onChanged: () => void }) {
  const [showOverride, setShowOverride] = useState(false);
  const act = async (fn: () => Promise<unknown>) => {
    await fn();
    onChanged();
  };
  const stopped = run.status === "stopped";
  return (
    <Card>
      <div className="flex items-center justify-between">
        <div>
          <span className="font-medium">{run.name}</span>{" "}
          <span className="text-xs text-slate-400">#{run.run_id} · {run.strategy_id}</span>{" "}
          <Badge>{run.status}</Badge>
        </div>
        <div className="flex gap-6 text-right text-sm">
          <div><div className="text-slate-400 text-xs">Equity</div>{formatInr(run.equity)}</div>
          <div><div className="text-slate-400 text-xs">Cash</div>{formatInr(run.cash)}</div>
        </div>
      </div>

      {run.positions?.length ? (
        <div className="overflow-x-auto mt-3">
          <table className="w-full text-sm">
            <thead className="text-slate-400 text-left">
              <tr>
                <th className="py-1 pr-4">Symbol</th>
                <th className="py-1 pr-4 text-right">Units</th>
                <th className="py-1 pr-4 text-right">Avg</th>
                <th className="py-1 pr-4 text-right">LTP</th>
                <th className="py-1 pr-4 text-right">Unrealized</th>
              </tr>
            </thead>
            <tbody>
              {run.positions.map((p) => (
                <tr key={p.symbol} className="border-t border-slate-800">
                  <td className="py-1 pr-4">{p.symbol} <span className="text-slate-500">({p.lots})</span></td>
                  <td className="py-1 pr-4 text-right">{p.units}</td>
                  <td className="py-1 pr-4 text-right">{formatInr(p.avg_price, 2)}</td>
                  <td className="py-1 pr-4 text-right">{p.ltp != null ? formatInr(p.ltp, 2) : "—"}</td>
                  <td className={`py-1 pr-4 text-right ${p.unrealized_pnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                    {formatInr(p.unrealized_pnl)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="text-slate-500 text-sm mt-3">No open positions.</div>
      )}

      {!stopped && (
        <>
          <div className="mt-3 flex flex-wrap gap-2">
            <button onClick={() => act(() => api.liveRefresh(run.run_id))} className="rounded bg-slate-800 hover:bg-slate-700 px-3 py-1.5 text-xs">Refresh</button>
            <button onClick={() => act(() => api.liveRunDecision(run.run_id))} className="rounded bg-brand hover:bg-brand-light px-3 py-1.5 text-xs">Run decision</button>
            <button onClick={() => setShowOverride((v) => !v)} className="rounded bg-slate-800 hover:bg-slate-700 px-3 py-1.5 text-xs">Intervene…</button>
            <button onClick={() => act(() => api.liveStop(run.run_id))} className="rounded bg-rose-900 hover:bg-rose-800 px-3 py-1.5 text-xs">Stop</button>
          </div>
          {showOverride && <OverridePanel runId={run.run_id} onDone={() => setShowOverride(false)} />}
        </>
      )}
    </Card>
  );
}

export default function LivePage() {
  const location = useLocation();
  const prefill = (location.state as { prefill?: ForwardTestPrefill } | null)?.prefill;
  const { snapshots, trades, connected, seed } = useLiveFeed();
  const runs = Object.values(snapshots).sort((a, b) => b.run_id - a.run_id);
  // Keep a stable ref to seed for action callbacks.
  const seedRef = useRef(seed);
  seedRef.current = seed;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Live (paper)</h1>
        <span className={`text-xs ${connected ? "text-emerald-400" : "text-slate-500"}`}>
          {connected ? "● live" : "○ disconnected"}
        </span>
      </div>

      <StartForm key={prefill?.strategy_id ?? "manual"} prefill={prefill} onStarted={() => seedRef.current()} />

      {runs.length === 0 ? (
        <Card><div className="text-slate-400">No paper runs. Start one above.</div></Card>
      ) : (
        runs.map((run) => <RunCard key={run.run_id} run={run} onChanged={() => seedRef.current()} />)
      )}

      {trades.length > 0 && (
        <Card>
          <div className="text-sm font-medium text-slate-300 mb-2">Recent fills</div>
          <div className="space-y-1 text-sm max-h-60 overflow-y-auto">
            {trades.map((t, i) => (
              <div key={i} className="flex justify-between border-b border-slate-800/50 py-0.5">
                <span>
                  <span className="text-slate-400">#{t.run_id}</span> {t.action} {t.units} {t.ticker}
                  {" "}<Badge>{t.tag}</Badge>
                </span>
                <span className="text-slate-400">{formatInr(t.price, 2)}</span>
              </div>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}
