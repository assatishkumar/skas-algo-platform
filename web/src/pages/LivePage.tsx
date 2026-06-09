import { keepPreviousData, useQuery } from "@tanstack/react-query";
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
  WatchRow,
} from "../types";

function fmt(n: number | null, d = 2): string {
  return n == null ? "—" : n.toLocaleString("en-IN", { maximumFractionDigits: d });
}

function SignalsPanel({ runId, version }: { runId: number; version: number }) {
  const [sortBy, setSortBy] = useState<"symbol" | "breakout" | "status">("symbol");
  const { data, isLoading } = useQuery({
    queryKey: ["watchlist", runId, version],
    queryFn: () => api.liveWatchlist(runId),
    // Keep the current rows visible while a refresh fetches, so the table doesn't
    // unmount/flash (which jumped the scroll position on every refresh).
    placeholderData: keepPreviousData,
  });
  if (isLoading) return <div className="text-slate-500 text-sm mt-3">Loading signals…</div>;
  const rows: WatchRow[] = data?.rows ?? [];
  // Would-act pinned on top; the rest in a STABLE order (symbol by default) so rows
  // don't reshuffle as prices wiggle on each refresh.
  const cmp = (a: WatchRow, b: WatchRow) => {
    const ra = a.signal ? 0 : 1;
    const rb = b.signal ? 0 : 1;
    if (ra !== rb) return ra - rb;
    if (sortBy === "breakout") return (a.to_breakout_pct ?? 1e9) - (b.to_breakout_pct ?? 1e9);
    if (sortBy === "status") return a.status.localeCompare(b.status) || a.symbol.localeCompare(b.symbol);
    return a.symbol.localeCompare(b.symbol);
  };
  const sorted = [...rows].sort(cmp);
  const counts: Record<string, number> = {};
  rows.forEach((r) => (counts[r.status] = (counts[r.status] ?? 0) + 1));
  const wouldAct = rows.filter((r) => r.signal).length;

  return (
    <div className="mt-3 border-t border-slate-800 pt-3">
      <div className="flex items-center justify-between mb-2">
        <div className="text-xs text-slate-400">
          {wouldAct > 0 && <span className="text-amber-400 font-semibold">⚡ would act: {wouldAct}  ·  </span>}
          {Object.entries(counts).map(([s, n]) => `${s}: ${n}`).join("  ·  ") || "no symbols"}
        </div>
        <label className="text-xs text-slate-400 flex items-center gap-1">
          sort
          <select
            className="rounded bg-slate-800 border border-slate-700 px-1.5 py-0.5"
            value={sortBy}
            onChange={(e) => setSortBy(e.target.value as "symbol" | "breakout" | "status")}
          >
            <option value="symbol">Symbol</option>
            <option value="breakout">→ breakout</option>
            <option value="status">Status</option>
          </select>
        </label>
      </div>
      <div className="overflow-x-auto max-h-96 overflow-y-auto">
        <table className="w-full text-xs tabular-nums">
          <thead className="text-slate-400 text-left sticky top-0 bg-slate-900">
            <tr>
              <th className="py-1 pr-3">Symbol</th>
              <th className="py-1 pr-3 text-right">LTP</th>
              <th className="py-1 pr-3 text-right">20d low</th>
              <th className="py-1 pr-3 text-right">20d high</th>
              <th className="py-1 pr-3 text-right">→ breakout</th>
              <th className="py-1 pr-3 text-right">P&amp;L</th>
              <th className="py-1 pr-3">Status</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((r) => (
              <tr key={r.symbol} className={`border-t border-slate-800 ${r.held ? "bg-slate-800/40" : ""}`}>
                <td className="py-1 pr-3 font-medium">
                  {r.symbol} {r.tracking && !r.held && <span title="tracking">👁</span>}
                </td>
                <td className="py-1 pr-3 text-right">{fmt(r.ltp)}</td>
                <td className="py-1 pr-3 text-right text-slate-400">{fmt(r.low_20d)}</td>
                <td className="py-1 pr-3 text-right text-slate-400">{fmt(r.high_20d)}</td>
                <td className="py-1 pr-3 text-right text-slate-300">
                  {r.to_breakout_pct == null ? "—" : `+${r.to_breakout_pct.toFixed(1)}%`}
                </td>
                <td className={`py-1 pr-3 text-right ${(r.pnl_pct ?? 0) >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                  {r.pnl_pct == null ? "—" : `${r.pnl_pct >= 0 ? "+" : ""}${r.pnl_pct.toFixed(1)}%`}
                </td>
                <td className="py-1 pr-3">
                  {r.signal && (
                    <span className={`mr-1 font-semibold ${r.signal === "BUY" ? "text-emerald-400" : "text-amber-400"}`}>
                      ⚡{r.signal}
                    </span>
                  )}
                  {r.status}
                  {r.held ? ` · ${r.lots} lot${r.lots > 1 ? "s" : ""}` : ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

const inputClass =
  "w-full rounded-md bg-slate-800 border border-slate-700 px-3 py-2 text-sm focus:outline-none focus:border-brand";

function useLiveFeed() {
  const [snapshots, setSnapshots] = useState<Record<number, LiveRunSnapshot>>({});
  const [trades, setTrades] = useState<(LiveTradeEvent & { run_id: number })[]>([]);
  const [versions, setVersions] = useState<Record<number, number>>({});
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
        setVersions((prev) => ({ ...prev, [msg.run_id]: (prev[msg.run_id] ?? 0) + 1 }));
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

  return { snapshots, trades, versions, connected, seed };
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

function QuoteSwitch({ run, onChanged }: { run: LiveRunSnapshot; onChanged: () => void }) {
  const { data: accounts } = useQuery({ queryKey: ["brokers"], queryFn: brokers.list });
  const sessioned = (accounts ?? []).filter((a) => a.has_session);
  const [open, setOpen] = useState(false);
  const [acct, setAcct] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function go(qs: string, id: number | null) {
    setBusy(true);
    setErr(null);
    try {
      await api.liveSetQuoteSource(run.run_id, qs, id);
      setOpen(false);
      onChanged();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (run.quote_source === "zerodha") {
    return (
      <button onClick={() => go("cache", null)} disabled={busy} className="rounded bg-slate-800 hover:bg-slate-700 px-3 py-1.5 text-xs">
        Use cache quotes
      </button>
    );
  }
  return (
    <span className="inline-flex items-center gap-1">
      {!open ? (
        <button onClick={() => setOpen(true)} className="rounded bg-emerald-900 hover:bg-emerald-800 px-3 py-1.5 text-xs">
          Go live ⚡
        </button>
      ) : (
        <>
          <select className="rounded bg-slate-800 border border-slate-700 px-2 py-1 text-xs" value={acct ?? ""} onChange={(e) => setAcct(e.target.value ? +e.target.value : null)}>
            <option value="">account…</option>
            {sessioned.map((a) => (
              <option key={a.id} value={a.id}>{a.label}</option>
            ))}
          </select>
          <button onClick={() => go("zerodha", acct)} disabled={!acct || busy} className="rounded bg-emerald-900 hover:bg-emerald-800 px-2 py-1 text-xs disabled:opacity-50">
            {busy ? "…" : "Use live"}
          </button>
          <button onClick={() => setOpen(false)} className="text-slate-500 px-1">×</button>
        </>
      )}
      {err && <span className="text-rose-400 text-xs">{err}</span>}
    </span>
  );
}

function RunCard({
  run,
  version,
  onChanged,
}: {
  run: LiveRunSnapshot;
  version: number;
  onChanged: () => void;
}) {
  const [showOverride, setShowOverride] = useState(false);
  const [showSignals, setShowSignals] = useState(false);
  const act = async (fn: () => Promise<unknown>) => {
    await fn();
    onChanged();
  };
  // Refresh relies on the WebSocket snapshot to update the card + bump the signals
  // version (which refetches with keepPreviousData) — no full page re-seed, so the
  // scroll position and sort order stay put.
  const refresh = () => {
    api.liveRefresh(run.run_id).catch(() => {});
  };
  const stopped = run.status === "stopped";
  const upnl = (run.positions ?? []).reduce((s, p) => s + p.unrealized_pnl, 0);
  return (
    <Card>
      <div className="flex items-center justify-between">
        <div>
          <span className="font-medium">{run.name}</span>{" "}
          <span className="text-xs text-slate-400">#{run.run_id} · {run.strategy_id}</span>{" "}
          <Badge>{run.status}</Badge>{" "}
          <Badge>{run.quote_source === "zerodha" ? "live quotes" : "cache quotes"}</Badge>
        </div>
        <div className="flex gap-6 text-right text-sm">
          <div><div className="text-slate-400 text-xs">Equity</div>{formatInr(run.equity)}</div>
          <div><div className="text-slate-400 text-xs">Cash</div>{formatInr(run.cash)}</div>
        </div>
      </div>

      {/* Quick summary: deployed capital, parts, positions, unrealized P&L */}
      <div className="mt-2 grid grid-cols-2 md:grid-cols-4 gap-2 text-sm">
        <div className="rounded-md bg-slate-800/40 px-3 py-2">
          <div className="text-slate-400 text-xs">Deployed</div>
          {formatInr(run.invested ?? 0)}
        </div>
        <div className="rounded-md bg-slate-800/40 px-3 py-2">
          <div className="text-slate-400 text-xs">Parts deployed</div>
          {run.open_lots ?? 0}{run.parts_total ? ` / ${run.parts_total}` : ""}
        </div>
        <div className="rounded-md bg-slate-800/40 px-3 py-2">
          <div className="text-slate-400 text-xs">Positions held</div>
          {run.open_positions ?? 0}
        </div>
        <div className="rounded-md bg-slate-800/40 px-3 py-2">
          <div className="text-slate-400 text-xs">Unrealized P&amp;L</div>
          <span className={upnl >= 0 ? "text-emerald-400" : "text-rose-400"}>{formatInr(upnl)}</span>
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
            <button onClick={refresh} className="rounded bg-slate-800 hover:bg-slate-700 px-3 py-1.5 text-xs">Refresh</button>
            <button onClick={() => act(() => api.liveRunDecision(run.run_id))} className="rounded bg-brand hover:bg-brand-light px-3 py-1.5 text-xs">Run decision</button>
            <button onClick={() => setShowSignals((v) => !v)} className="rounded bg-slate-800 hover:bg-slate-700 px-3 py-1.5 text-xs">
              {showSignals ? "Hide signals" : "Signals"}
            </button>
            <button onClick={() => setShowOverride((v) => !v)} className="rounded bg-slate-800 hover:bg-slate-700 px-3 py-1.5 text-xs">Intervene…</button>
            <QuoteSwitch run={run} onChanged={onChanged} />
            <button onClick={() => act(() => api.liveStop(run.run_id))} className="rounded bg-rose-900 hover:bg-rose-800 px-3 py-1.5 text-xs">Stop</button>
          </div>
          {showOverride && <OverridePanel runId={run.run_id} onDone={() => setShowOverride(false)} />}
          {showSignals && <SignalsPanel runId={run.run_id} version={version} />}
        </>
      )}
    </Card>
  );
}

export default function LivePage() {
  const location = useLocation();
  const prefill = (location.state as { prefill?: ForwardTestPrefill } | null)?.prefill;
  const { snapshots, trades, versions, connected, seed } = useLiveFeed();
  const runs = Object.values(snapshots).sort((a, b) => b.run_id - a.run_id);
  // Keep a stable ref to seed for action callbacks.
  const seedRef = useRef(seed);
  seedRef.current = seed;

  // Auto-refresh: while the page is open, periodically pull quotes for running runs.
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [intervalSec, setIntervalSec] = useState(15);
  const runsRef = useRef(runs);
  runsRef.current = runs;
  useEffect(() => {
    if (!autoRefresh) return;
    const id = setInterval(() => {
      runsRef.current
        .filter((r) => r.status !== "stopped")
        .forEach((r) => api.liveRefresh(r.run_id).catch(() => {}));
    }, intervalSec * 1000);
    return () => clearInterval(id);
  }, [autoRefresh, intervalSec]);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Live (paper)</h1>
        <div className="flex items-center gap-3 text-xs">
          <label className="flex items-center gap-1.5 text-slate-300">
            <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} />
            auto-refresh
          </label>
          <select
            className="rounded bg-slate-800 border border-slate-700 px-1.5 py-0.5"
            value={intervalSec}
            onChange={(e) => setIntervalSec(+e.target.value)}
            disabled={!autoRefresh}
          >
            <option value={5}>5s</option>
            <option value={15}>15s</option>
            <option value={30}>30s</option>
            <option value={60}>60s</option>
          </select>
          <span className={connected ? "text-emerald-400" : "text-slate-500"}>
            {connected ? "● live" : "○ disconnected"}
          </span>
        </div>
      </div>

      <StartForm key={prefill?.strategy_id ?? "manual"} prefill={prefill} onStarted={() => seedRef.current()} />

      {runs.length === 0 ? (
        <Card><div className="text-slate-400">No paper runs. Start one above.</div></Card>
      ) : (
        runs.map((run) => (
          <RunCard
            key={run.run_id}
            run={run}
            version={versions[run.run_id] ?? 0}
            onChanged={() => seedRef.current()}
          />
        ))
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
