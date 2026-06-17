import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, brokers, liveWsUrl } from "../api/client";
import { Badge, Card, StatusPill, timeAgo } from "../components/ui";
import GreeksPanel from "../components/GreeksPanel";
import LivePayoffChart from "../components/LivePayoffChart";
import LiveTradesPanel from "../components/LiveTradesPanel";
import OptionMetricsPanel from "../components/OptionMetricsPanel";
import { formatInr } from "../lib/format";
import { isOptionsStrategy } from "../lib/params";
import type {
  Deployment,
  LiveRunSnapshot,
  LiveTradeEvent,
  LiveWsMessage,
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
          {wouldAct > 0 && <span className="text-amber-600 dark:text-amber-400 font-semibold">⚡ would act: {wouldAct}  ·  </span>}
          {Object.entries(counts).map(([s, n]) => `${s}: ${n}`).join("  ·  ") || "no symbols"}
          <span className="text-slate-600">  —  buy needs a 20-day low (👁) then a breakout; → breakout is % to the 20d high</span>
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
                  {r.excluded && <span title="excluded — no new entries" className="ml-1">🚫</span>}
                </td>
                <td className="py-1 pr-3 text-right">{fmt(r.ltp)}</td>
                <td className="py-1 pr-3 text-right text-slate-400">{fmt(r.low_20d)}</td>
                <td className="py-1 pr-3 text-right text-slate-400">{fmt(r.high_20d)}</td>
                <td className="py-1 pr-3 text-right text-slate-300">
                  {r.to_breakout_pct == null
                    ? "—"
                    : `${r.to_breakout_pct >= 0 ? "+" : ""}${r.to_breakout_pct.toFixed(1)}%`}
                </td>
                <td className={`py-1 pr-3 text-right ${(r.pnl_pct ?? 0) >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400"}`}>
                  {r.pnl_pct == null ? "—" : `${r.pnl_pct >= 0 ? "+" : ""}${r.pnl_pct.toFixed(1)}%`}
                </td>
                <td className="py-1 pr-3">
                  {r.signal && (
                    <span className={`mr-1 font-semibold ${r.signal === "BUY" ? "text-emerald-600 dark:text-emerald-400" : "text-amber-600 dark:text-amber-400"}`}>
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

/** Option-aware intervention: close selected legs/lots and/or open a new leg now.
 *  Replaces the equity book/trail override for options — those rules don't fit a
 *  multi-leg book. After it applies, the strategy adopts whatever book is left. */
function OptionIntervenePanel({ run, onDone }: { run: LiveRunSnapshot; onDone: () => void }) {
  const positions = run.positions ?? [];
  const [closeLots, setCloseLots] = useState<Record<string, number>>({});
  const [right, setRight] = useState("CE");
  const [strike, setStrike] = useState("");
  const [lots, setLots] = useState("1");
  const [side, setSide] = useState("sell");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function apply() {
    const closes = positions
      .filter((p) => (closeLots[p.symbol] ?? 0) > 0)
      .map((p) => ({ symbol: p.symbol, lots: closeLots[p.symbol] }));
    const opens = strike.trim()
      ? [{ right, strike: Number(strike), lots: Math.max(1, Number(lots) || 1), side }]
      : [];
    if (!closes.length && !opens.length) {
      setErr("Pick legs to close or a new leg to open.");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await api.liveManualOrder(run.run_id, { closes, opens });
      onDone();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-3 rounded-md border border-slate-800 bg-slate-900/40 p-3 text-sm">
      <div className="text-xs text-amber-600 dark:text-amber-400/90 mb-2">
        ⚠ Manual changes alter the position's risk (e.g. break a net-zero ratio). The strategy
        then manages whatever book is left.
      </div>
      {positions.length > 0 && (
        <div className="mb-3">
          <div className="text-xs text-slate-400 mb-1">Close legs / lots</div>
          <div className="space-y-1">
            {positions.map((p) => (
              <div key={p.symbol} className="flex items-center gap-2 text-xs">
                <span className="font-mono">{p.symbol}</span>
                <span className="text-slate-500">
                  ({p.lots} lot{p.lots > 1 ? "s" : ""}, {p.direction === -1 ? "short" : "long"})
                </span>
                <span className="ml-auto text-slate-400">close</span>
                <input
                  type="number"
                  min={0}
                  max={p.lots}
                  value={closeLots[p.symbol] ?? 0}
                  onChange={(e) =>
                    setCloseLots({
                      ...closeLots,
                      [p.symbol]: Math.max(0, Math.min(p.lots, Number(e.target.value) || 0)),
                    })
                  }
                  className="w-14 rounded bg-slate-800 border border-slate-700 px-2 py-1"
                />
                <span className="text-slate-500">lots</span>
              </div>
            ))}
          </div>
        </div>
      )}
      <div className="flex flex-wrap items-end gap-2">
        <div className="text-xs text-slate-400 w-full">Open a new leg (strategy's current expiry)</div>
        <select value={side} onChange={(e) => setSide(e.target.value)} className="rounded bg-slate-800 border border-slate-700 px-2 py-1 text-sm">
          <option value="sell">Sell</option>
          <option value="buy">Buy</option>
        </select>
        <select value={right} onChange={(e) => setRight(e.target.value)} className="rounded bg-slate-800 border border-slate-700 px-2 py-1 text-sm">
          <option value="CE">CE</option>
          <option value="PE">PE</option>
        </select>
        <input type="number" placeholder="strike" value={strike} onChange={(e) => setStrike(e.target.value)} className="w-24 rounded bg-slate-800 border border-slate-700 px-2 py-1 text-sm" />
        <input type="number" min={1} placeholder="lots" value={lots} onChange={(e) => setLots(e.target.value)} className="w-16 rounded bg-slate-800 border border-slate-700 px-2 py-1 text-sm" />
        <span className="text-xs text-slate-500">lot-sets</span>
      </div>
      <div className="mt-3 flex items-center gap-3">
        <button onClick={apply} disabled={busy} className="rounded bg-brand hover:bg-brand-light px-3 py-1.5 text-xs font-medium disabled:opacity-50">
          {busy ? "Applying…" : "Apply"}
        </button>
        {err && <span className="text-xs text-rose-600 dark:text-rose-400">{err}</span>}
      </div>
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
        <button onClick={() => setOpen(true)} className="rounded bg-emerald-900 hover:bg-emerald-800 text-white px-3 py-1.5 text-xs">
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
          <button onClick={() => go("zerodha", acct)} disabled={!acct || busy} className="rounded bg-emerald-900 hover:bg-emerald-800 text-white px-2 py-1 text-xs disabled:opacity-50">
            {busy ? "…" : "Use live"}
          </button>
          <button onClick={() => setOpen(false)} className="text-slate-500 px-1">×</button>
        </>
      )}
      {err && <span className="text-rose-600 dark:text-rose-400 text-xs">{err}</span>}
    </span>
  );
}

/** Full live detail for an active deployment — positions, signals, interventions. */
/** Edit a running deployment's loop controls + exclusion list (no new entries). */
function ControlsPanel({ run, onChanged }: { run: LiveRunSnapshot; onChanged: () => void }) {
  const [auto, setAuto] = useState(run.auto);
  const [ignore, setIgnore] = useState(run.ignore_market_hours);
  const [refresh, setRefresh] = useState(String(run.refresh_seconds));
  const [excluded, setExcluded] = useState<string[]>(run.excluded_symbols ?? []);
  const [add, setAdd] = useState("");
  const [lots, setLots] = useState(run.lots != null ? String(run.lots) : "");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const isOptions = run.lots != null; // options strategies expose lot-sets
  const universe = run.universe ?? [];
  const available = universe.filter((s) => !excluded.includes(s));

  const dirty =
    auto !== run.auto ||
    ignore !== run.ignore_market_hours ||
    Number(refresh) !== run.refresh_seconds ||
    (isOptions && Number(lots) !== run.lots) ||
    excluded.slice().sort().join(",") !== (run.excluded_symbols ?? []).slice().sort().join(",");

  function addExcluded() {
    const sym = add.trim().toUpperCase();
    if (!sym) return;
    // Only allow names actually in this deployment's universe.
    if (universe.length && !universe.includes(sym)) {
      setMsg(`"${sym}" is not in this deployment's universe.`);
      return;
    }
    if (!excluded.includes(sym)) setExcluded([...excluded, sym].sort());
    setAdd("");
    setMsg(null);
  }

  async function save() {
    setBusy(true);
    setMsg(null);
    try {
      await api.liveSetControls(run.run_id, {
        auto,
        ignore_market_hours: ignore,
        refresh_seconds: Math.max(5, Number(refresh) || run.refresh_seconds),
        excluded_symbols: excluded,
        ...(isOptions ? { lots: Math.max(1, Number(lots) || (run.lots ?? 1)) } : {}),
      });
      onChanged();
      setMsg("Saved.");
    } catch (e) {
      setMsg((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-3 rounded-md border border-slate-800 bg-slate-900/40 p-3 text-sm">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
          auto loop (refresh + daily decision)
        </label>
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={ignore} onChange={(e) => setIgnore(e.target.checked)} />
          ignore market hours
        </label>
        <label className="flex items-center gap-2">
          refresh every
          <input
            type="number"
            min={5}
            value={refresh}
            onChange={(e) => setRefresh(e.target.value)}
            className="w-20 rounded bg-slate-800 border border-slate-700 px-2 py-1"
          />
          s
        </label>
        {isOptions && (
          <label className="flex items-center gap-2">
            lot-sets
            <input
              type="number"
              min={1}
              value={lots}
              onChange={(e) => setLots(e.target.value)}
              className="w-16 rounded bg-slate-800 border border-slate-700 px-2 py-1"
            />
            <span className="text-xs text-slate-500">(next entry)</span>
          </label>
        )}
        <span className="text-xs text-slate-500">
          {isOptions ? "decides each refresh · entry/exit cadence per strategy" : `daily decision at ${run.decision_time} IST`}
        </span>
      </div>

      <div className="mt-3">
        <div className="text-xs text-slate-400 mb-1">
          Excluded (no new entries; open positions keep being managed)
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          {excluded.length === 0 && <span className="text-xs text-slate-500">None excluded.</span>}
          {excluded.map((s) => (
            <span key={s} className="inline-flex items-center gap-1 rounded-full bg-amber-100 text-amber-700 border border-amber-300 dark:bg-amber-900/30 dark:border-amber-700/40 dark:text-amber-300 px-2 py-0.5 text-xs">
              {s}
              <button onClick={() => setExcluded(excluded.filter((x) => x !== s))} className="hover:text-amber-100">×</button>
            </span>
          ))}
        </div>
        <div className="mt-2 flex items-center gap-2">
          <input
            list={`uni-${run.run_id}`}
            value={add}
            onChange={(e) => setAdd(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && addExcluded()}
            placeholder="Exclude symbol…"
            className="w-48 rounded bg-slate-800 border border-slate-700 px-2 py-1 text-xs"
          />
          <datalist id={`uni-${run.run_id}`}>
            {available.map((s) => (
              <option key={s} value={s} />
            ))}
          </datalist>
          <button onClick={addExcluded} disabled={!add.trim()} className="rounded bg-slate-700 hover:bg-slate-600 px-3 py-1 text-xs disabled:opacity-50">
            Exclude
          </button>
        </div>
      </div>

      <div className="mt-3 flex items-center gap-3">
        <button
          onClick={save}
          disabled={busy || !dirty}
          className="rounded bg-brand hover:bg-brand-light px-3 py-1.5 text-xs font-medium disabled:opacity-50"
        >
          {busy ? "Saving…" : "Save controls"}
        </button>
        {msg && <span className="text-xs text-slate-400">{msg}</span>}
      </div>
    </div>
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
  const [showControls, setShowControls] = useState(false);
  // SST "Signals" is the equity Donchian watchlist (20d high/low breakout) — meaningless
  // for an options strategy, which decides on its own entry/exit schedule.
  const isOptions = run.lots != null;
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
    <div className="mt-3 border-t border-slate-800 pt-3">
      {/* Options: Sensibull-style position metrics (max P/L, breakevens, POP, margin, …).
          Equity: deployed capital / parts / positions / unrealized P&L. */}
      {isOptions ? (
        <OptionMetricsPanel run={run} />
      ) : (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-sm">
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
            <span className={upnl >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400"}>{formatInr(upnl)}</span>
          </div>
        </div>
      )}

      {run.positions?.length ? (
        <div className="overflow-x-auto mt-3">
          <table className="w-full text-sm">
            <thead className="text-slate-400 text-left">
              <tr>
                <th className="py-1 pr-4">Symbol</th>
                <th className="py-1 pr-4 text-right">Units</th>
                <th className="py-1 pr-4 text-right">Avg</th>
                <th className="py-1 pr-4 text-right">LTP</th>
                {isOptions && <th className="py-1 pr-4 text-right">Δ</th>}
                {isOptions && <th className="py-1 pr-4 text-right">IV</th>}
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
                  {isOptions && (
                    <td className="py-1 pr-4 text-right tabular-nums text-slate-300">
                      {p.pos_delta != null ? p.pos_delta.toFixed(1) : "—"}
                    </td>
                  )}
                  {isOptions && (
                    <td className="py-1 pr-4 text-right tabular-nums text-slate-300">
                      {p.iv != null ? `${(p.iv * 100).toFixed(1)}%` : "—"}
                    </td>
                  )}
                  <td className={`py-1 pr-4 text-right ${p.unrealized_pnl >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400"}`}>
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

      {isOptions && run.positions?.length ? (
        <LivePayoffChart positions={run.positions} spot={run.underlying_spot} />
      ) : null}
      {/* Greeks/P&L history + the trade log stay visible after a cycle closes, so a booked
          position still shows how it evolved, when it exited and the realized P&L. */}
      {isOptions ? <GreeksPanel run={run} /> : null}
      {isOptions ? <LiveTradesPanel runId={run.run_id} version={version} /> : null}

      {!stopped && (
        <>
          <div className="mt-3 flex flex-wrap gap-2">
            <button onClick={refresh} className="rounded bg-slate-800 hover:bg-slate-700 px-3 py-1.5 text-xs">Refresh</button>
            <button onClick={() => act(() => api.liveRunDecision(run.run_id))} className="rounded bg-brand hover:bg-brand-light px-3 py-1.5 text-xs">Run decision</button>
            {!isOptions && (
              <button onClick={() => setShowSignals((v) => !v)} className="rounded bg-slate-800 hover:bg-slate-700 px-3 py-1.5 text-xs">
                {showSignals ? "Hide signals" : "Signals"}
              </button>
            )}
            <button onClick={() => setShowControls((v) => !v)} className="rounded bg-slate-800 hover:bg-slate-700 px-3 py-1.5 text-xs">
              {showControls ? "Hide controls" : "Controls"}
            </button>
            <button onClick={() => setShowOverride((v) => !v)} className="rounded bg-slate-800 hover:bg-slate-700 px-3 py-1.5 text-xs">Intervene…</button>
            {isOptions && run.positions?.length ? (
              <button
                onClick={() => {
                  if (confirm("Exit ALL open legs now at live prices?"))
                    act(() => api.liveFlatten(run.run_id));
                }}
                className="rounded bg-rose-900 hover:bg-rose-800 text-white px-3 py-1.5 text-xs"
              >
                Exit all
              </button>
            ) : null}
            {run.on_cache_fallback && (
              <button
                onClick={() => act(() => api.liveReconnectQuotes(run.run_id))}
                title="This run wanted live Zerodha quotes but is on cache fallback. Reconnect now that you're logged in."
                className="rounded bg-amber-700 hover:bg-amber-600 px-3 py-1.5 text-xs"
              >
                Reconnect to live quotes
              </button>
            )}
            <QuoteSwitch run={run} onChanged={onChanged} />
          </div>
          {showControls && <ControlsPanel run={run} onChanged={onChanged} />}
          {showOverride &&
            (isOptions ? (
              <OptionIntervenePanel
                run={run}
                onDone={() => {
                  setShowOverride(false);
                  onChanged();
                }}
              />
            ) : (
              <OverridePanel runId={run.run_id} onDone={() => setShowOverride(false)} />
            ))}
          {showSignals && !isOptions && <SignalsPanel runId={run.run_id} version={version} />}
        </>
      )}
    </div>
  );
}

/** Circular-arrows refresh icon; spins while a manual refresh is in flight. */
function RefreshIcon({ spinning }: { spinning?: boolean }) {
  return (
    <svg
      className={spinning ? "animate-spin" : ""}
      width="13"
      height="13"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <polyline points="23 4 23 10 17 10" />
      <polyline points="1 20 1 14 7 14" />
      <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
    </svg>
  );
}

/** A small green dot that "pings" once whenever `flash` flips true (a fresh tick arrived). */
function LivePulse({ flash, label }: { flash: boolean; label: string }) {
  return (
    <span
      className="inline-flex items-center gap-1 text-[11px] text-emerald-600 dark:text-emerald-400"
      title="Receiving live updates"
    >
      <span className="relative flex h-2 w-2">
        {flash && (
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
        )}
        <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-500" />
      </span>
      {label}
    </span>
  );
}

/** Quote-source / broker-connection chip: green when live & connected, rose when the
 *  zerodha session is down (or it has fallen back to cache), plain badge for cache runs. */
function BrokerChip({ dep }: { dep: Deployment }) {
  if (dep.quote_source !== "zerodha") return <Badge>cache quotes</Badge>;
  const label = dep.broker_label || "Zerodha";
  const fallback = dep.on_cache_fallback === true;
  const ok = dep.broker_connected === true && !fallback;
  const suffix = ok ? "live" : fallback ? "cache" : "offline";
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium ${
        ok
          ? "bg-emerald-100 text-emerald-700 border-emerald-300 dark:bg-emerald-900/40 dark:text-emerald-300 dark:border-emerald-700/50"
          : "bg-rose-100 text-rose-700 border-rose-300 dark:bg-rose-900/40 dark:text-rose-300 dark:border-rose-700/50"
      }`}
      title={
        ok
          ? `Connected to ${label} — live Zerodha quotes`
          : fallback
            ? `${label}: session lost, using cache quotes. Log in again, then Reconnect.`
            : `${label}: not connected. Log in (paste request token) to resume live quotes.`
      }
    >
      <span className={`h-1.5 w-1.5 rounded-full ${ok ? "bg-emerald-500" : "bg-rose-500"}`} />
      {label} · {suffix}
    </span>
  );
}

/** A deployment tile: name, status, key metrics, notes, and per-status actions. */
function DeploymentTile({
  dep,
  snapshot,
  version,
  expanded,
  onToggle,
  onChanged,
}: {
  dep: Deployment;
  snapshot?: LiveRunSnapshot;
  version: number;
  expanded: boolean;
  onToggle: () => void;
  onChanged: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(dep.name);
  const [notes, setNotes] = useState(dep.notes ?? "");
  const [busy, setBusy] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  // Pulse a "live" dot each time a fresh WS snapshot bumps the version for this run.
  const [flash, setFlash] = useState(false);
  const lastVersion = useRef(version);
  useEffect(() => {
    if (version !== lastVersion.current) {
      lastVersion.current = version;
      setFlash(true);
      const t = setTimeout(() => setFlash(false), 900);
      return () => clearTimeout(t);
    }
  }, [version]);

  // Manual refresh: re-poll quotes + mark-to-market now (the backend broadcasts the
  // resulting snapshot, so this also pulses the live dot and updates an expanded panel).
  async function refreshNow() {
    setRefreshing(true);
    try {
      await api.liveRefresh(dep.run_id);
      onChanged();
    } finally {
      setRefreshing(false);
    }
  }

  const m = dep.metrics ?? {};
  // Prefer the live snapshot for active tiles (WS-fresh), fall back to tile metrics.
  const equity = snapshot?.equity ?? m.equity ?? null;
  const upnl =
    snapshot != null
      ? (snapshot.positions ?? []).reduce((s, p) => s + p.unrealized_pnl, 0)
      : m.unrealized_pnl;
  const positions = snapshot?.open_positions ?? m.open_positions ?? 0;

  // Options vs equity (drives badge + which controls make sense). Persisted in the tile,
  // so it resolves even for stopped/archived deployments without a live snapshot.
  const isOptions =
    dep.instrument_class === "DERIV" ||
    isOptionsStrategy(dep.strategy_id) ||
    snapshot?.lots != null;
  const underlying = snapshot?.underlying ?? dep.underlying ?? null;
  // "Pause" = the auto-loop toggle: decisions freeze, marking/P&L keep updating.
  const auto = snapshot?.auto ?? true;
  const paused = dep.status === "active" && snapshot != null && !auto;
  // Options tiles surface margin + net credit/debit instead of equity value.
  const marginUsed = snapshot?.margin_used ?? m.margin_used ?? null;
  const netCredit = snapshot?.net_credit ?? m.net_credit ?? null;
  const realized = snapshot?.realized_pnl ?? m.realized_pnl ?? null;
  // An options run that has flattened (no open legs) → show its booked Realized P&L in place of
  // the now-meaningless live net credit.
  const optFlat = isOptions && positions === 0;

  // When a deployment is opened, pull a fresh snapshot so the positions panel populates
  // immediately instead of waiting for the next WebSocket tick.
  useEffect(() => {
    if (expanded && dep.status === "active") {
      api.liveRefresh(dep.run_id).catch(() => {});
    }
  }, [expanded, dep.status, dep.run_id]);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
    } finally {
      setBusy(false);
      onChanged();
    }
  };

  async function saveEdit() {
    await act(() => api.liveUpdate(dep.run_id, { name: name.trim(), notes: notes.trim() }));
    setEditing(false);
  }

  return (
    <Card className={`flex flex-col ${expanded ? "md:col-span-2" : ""}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          {editing ? (
            <input
              className="w-full rounded bg-slate-800 border border-slate-700 px-2 py-1 text-sm"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          ) : (
            <div className="font-medium truncate">{dep.name}</div>
          )}
          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-slate-400">
            <StatusPill status={dep.status} />
            {dep.status === "active" && <LivePulse flash={flash} label="live" />}
            {paused && (
              <span className="rounded-full bg-amber-100 text-amber-700 border border-amber-300 dark:bg-amber-900/40 dark:border-amber-700/50 dark:text-amber-300 px-2 py-0.5 text-[11px] font-medium">
                Paused
              </span>
            )}
            <span
              className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${
                isOptions
                  ? "bg-indigo-100 text-indigo-700 border border-indigo-300 dark:bg-indigo-900/40 dark:border-indigo-600/50 dark:text-indigo-300"
                  : "bg-slate-200 text-slate-600 border border-slate-300 dark:bg-slate-800 dark:border-slate-700 dark:text-slate-300"
              }`}
              title={isOptions ? "Options (DERIV) strategy" : "Equity (STOCK) strategy"}
            >
              {isOptions ? `OPT${underlying ? ` · ${underlying}` : ""}` : "EQ"}
            </span>
            <Badge>{dep.strategy_id}</Badge>
            <BrokerChip dep={dep} />
            <span>#{dep.run_id}</span>
          </div>
        </div>
        <div className="flex items-start gap-2 shrink-0">
          <div className="text-right text-sm">
            <div className="text-slate-400 text-xs">{isOptions ? "Margin" : "Equity"}</div>
            <div>
              {isOptions
                ? marginUsed != null
                  ? formatInr(marginUsed)
                  : "—"
                : equity != null
                  ? formatInr(equity)
                  : "—"}
            </div>
          </div>
          {expanded && dep.status === "active" && (
            <button
              onClick={onToggle}
              title="Minimize"
              aria-label="Minimize"
              className="rounded p-1 text-slate-400 hover:text-slate-200 hover:bg-slate-800"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="18 15 12 9 6 15" />
              </svg>
            </button>
          )}
        </div>
      </div>

      {/* Notes (preview + inline edit) */}
      {editing ? (
        <textarea
          className="mt-2 w-full rounded bg-slate-800 border border-slate-700 px-2 py-1 text-sm"
          rows={2}
          placeholder="notes"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
        />
      ) : dep.notes ? (
        <div className="mt-2 text-xs text-slate-400 line-clamp-2">{dep.notes}</div>
      ) : null}

      {/* Key metrics */}
      <div className="mt-3 grid grid-cols-3 gap-2 text-sm">
        <div className="rounded-md bg-slate-800/40 px-2.5 py-1.5">
          <div className="text-slate-400 text-[11px] mb-0.5">
            {!isOptions ? "Positions" : optFlat ? "Realized P&L" : netCredit != null && netCredit < 0 ? "Net debit" : "Net credit"}
          </div>
          <div className="font-medium tabular-nums">
            {!isOptions ? (
              positions
            ) : optFlat ? (
              realized != null ? (
                <span className={realized >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400"}>
                  {formatInr(realized)}
                </span>
              ) : (
                "—"
              )
            ) : netCredit != null ? (
              <span className={netCredit >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400"}>
                {formatInr(Math.abs(netCredit))}
              </span>
            ) : (
              "—"
            )}
          </div>
        </div>
        <div className="rounded-md bg-slate-800/40 px-2.5 py-1.5">
          <div className="text-slate-400 text-[11px] mb-0.5">Unrealized</div>
          <div className="font-medium tabular-nums">
            {upnl != null ? (
              <span className={upnl >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400"}>{formatInr(upnl)}</span>
            ) : (
              "—"
            )}
          </div>
        </div>
        <div className="rounded-md bg-slate-800/40 px-2.5 py-1.5">
          <div className="text-slate-400 text-[11px] mb-0.5">{dep.status === "active" ? "Open positions" : "Return"}</div>
          <div className="font-medium tabular-nums">
            {dep.status === "active" ? (
              <>
                {positions}
                <span className="text-slate-500 text-[11px] font-normal"> · {timeAgo(dep.started_at)}</span>
              </>
            ) : m.total_return_pct != null ? (
              `${m.total_return_pct >= 0 ? "+" : ""}${m.total_return_pct.toFixed(1)}%`
            ) : (
              "—"
            )}
          </div>
        </div>
      </div>

      {/* Actions */}
      <div className="mt-auto pt-3 flex flex-wrap items-center gap-2 text-xs">
        {dep.status === "active" ? (
          <>
            {!expanded && (
              <button onClick={onToggle} className="rounded bg-slate-800 hover:bg-slate-700 px-3 py-1.5">
                Open
              </button>
            )}
            <button
              onClick={() => act(() => api.liveSetControls(dep.run_id, { auto: !auto }))}
              disabled={busy}
              title={auto ? "Pause decisions (keeps marking live P&L)" : "Resume the decision loop"}
              className="rounded bg-slate-800 hover:bg-slate-700 px-3 py-1.5 disabled:opacity-50"
            >
              {auto ? "⏸ Pause" : "▶ Resume"}
            </button>
            <button
              onClick={refreshNow}
              disabled={refreshing}
              title="Refresh now — re-poll quotes and mark-to-market"
              className="rounded bg-slate-800 hover:bg-slate-700 px-3 py-1.5 disabled:opacity-50 inline-flex items-center gap-1.5"
            >
              <RefreshIcon spinning={refreshing} />
              Refresh
            </button>
            <button
              onClick={() => act(() => api.liveStop(dep.run_id))}
              disabled={busy}
              className="rounded bg-rose-900 hover:bg-rose-800 text-white px-3 py-1.5 disabled:opacity-50"
            >
              Stop
            </button>
          </>
        ) : (
          <>
            <Link to={`/runs/${dep.run_id}`} className="rounded bg-slate-800 hover:bg-slate-700 px-3 py-1.5">
              Report
            </Link>
            {dep.status === "stopped" ? (
              <button
                onClick={() => act(() => api.liveArchive(dep.run_id))}
                disabled={busy}
                className="rounded bg-slate-800 hover:bg-slate-700 px-3 py-1.5 disabled:opacity-50"
              >
                Archive
              </button>
            ) : (
              <button
                onClick={() => act(() => api.liveUnarchive(dep.run_id))}
                disabled={busy}
                className="rounded bg-slate-800 hover:bg-slate-700 px-3 py-1.5 disabled:opacity-50"
              >
                Unarchive
              </button>
            )}
            <button
              onClick={() => {
                if (confirm(`Delete "${dep.name}" permanently? This removes its report, orders, and positions.`))
                  act(() => api.liveDelete(dep.run_id));
              }}
              disabled={busy}
              className="rounded bg-rose-950 hover:bg-rose-900 text-rose-300 px-3 py-1.5 disabled:opacity-50"
            >
              Delete
            </button>
          </>
        )}
        {editing ? (
          <>
            <button onClick={saveEdit} disabled={busy} className="rounded bg-brand hover:bg-brand-light px-3 py-1.5 disabled:opacity-50">
              Save
            </button>
            <button onClick={() => { setEditing(false); setName(dep.name); setNotes(dep.notes ?? ""); }} className="text-slate-500 px-2">
              Cancel
            </button>
          </>
        ) : (
          <button onClick={() => setEditing(true)} className="ml-auto text-slate-500 hover:text-slate-300">
            Edit name/notes
          </button>
        )}
      </div>

      {/* Inline live detail for an expanded active deployment.
          Minimize is the chevron icon at the card's top-right. */}
      {expanded && dep.status === "active" && snapshot && (
        <RunCard run={snapshot} version={version} onChanged={onChanged} />
      )}
    </Card>
  );
}

function PortfolioBar({ deployments }: { deployments: Deployment[] }) {
  const totals = deployments.reduce(
    (acc, d) => {
      acc.equity += d.metrics?.equity ?? 0;
      acc.invested += d.metrics?.invested ?? 0;
      acc.upnl += d.metrics?.unrealized_pnl ?? 0;
      acc.positions += d.metrics?.open_positions ?? 0;
      return acc;
    },
    { equity: 0, invested: 0, upnl: 0, positions: 0 },
  );
  return (
    <Card>
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3 text-sm">
        <div>
          <div className="text-slate-400 text-xs">Active deployments</div>
          <div className="text-lg font-semibold">{deployments.length}</div>
        </div>
        <div>
          <div className="text-slate-400 text-xs">Total equity</div>
          <div className="text-lg font-semibold">{formatInr(totals.equity)}</div>
        </div>
        <div>
          <div className="text-slate-400 text-xs">Deployed</div>
          <div className="text-lg font-semibold">{formatInr(totals.invested)}</div>
        </div>
        <div>
          <div className="text-slate-400 text-xs">Unrealized P&amp;L</div>
          <div className={`text-lg font-semibold ${totals.upnl >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400"}`}>
            {formatInr(totals.upnl)}
          </div>
        </div>
        <div>
          <div className="text-slate-400 text-xs">Open positions</div>
          <div className="text-lg font-semibold">{totals.positions}</div>
        </div>
      </div>
    </Card>
  );
}

const TABS: { key: string; label: string }[] = [
  { key: "active", label: "Active" },
  { key: "stopped", label: "Stopped" },
  { key: "archived", label: "Archived" },
];

export default function LivePage() {
  const [tab, setTab] = useState("active");
  const [search, setSearch] = useState("");
  const [expanded, setExpanded] = useState<number | null>(null);
  const queryClient = useQueryClient();

  // WebSocket feed keeps active tiles fresh (live equity / positions / fills).
  const { snapshots, trades, versions, connected, seed } = useLiveFeed();

  const { data: deployments = [] } = useQuery({
    queryKey: ["deployments", tab],
    queryFn: () => api.liveDeployments(tab),
    refetchInterval: 15000,
  });

  const onChanged = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ["deployments"] });
    seed();
  }, [queryClient, seed]);

  const q = search.trim().toLowerCase();
  const filtered = q
    ? deployments.filter(
        (d) =>
          d.name.toLowerCase().includes(q) ||
          d.strategy_id.toLowerCase().includes(q) ||
          (d.notes ?? "").toLowerCase().includes(q),
      )
    : deployments;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <h1 className="text-lg font-semibold">Live (paper)</h1>
        <div className="flex items-center gap-3 text-xs">
          <span className={connected ? "text-emerald-600 dark:text-emerald-400" : "text-slate-500"}>
            {connected ? "● live" : "○ disconnected"}
          </span>
          <Link
            to="/live/new"
            className="rounded-md bg-brand hover:bg-brand-light px-3 py-1.5 text-sm font-medium"
          >
            + Deploy new strategy
          </Link>
        </div>
      </div>

      {tab === "active" && filtered.length > 0 && <PortfolioBar deployments={filtered} />}

      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex gap-1 rounded-lg bg-slate-800/50 p-1 text-sm">
          {TABS.map((t) => (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              className={`rounded-md px-3 py-1 ${tab === t.key ? "bg-brand text-white" : "text-slate-400 hover:text-slate-200"}`}
            >
              {t.label}
            </button>
          ))}
        </div>
        <input
          className="rounded-md bg-slate-800 border border-slate-700 px-3 py-1.5 text-sm w-56 focus:outline-none focus:border-brand"
          placeholder="Search name / strategy / notes"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      {filtered.length === 0 ? (
        <Card>
          <div className="text-slate-400">
            {tab === "active" ? (
              <>No active deployments. <Link to="/live/new" className="text-brand hover:underline">Deploy a strategy →</Link></>
            ) : (
              `No ${tab} deployments.`
            )}
          </div>
        </Card>
      ) : (
        <div className="grid gap-3 md:grid-cols-2">
          {filtered.map((dep) => (
            <DeploymentTile
              key={dep.run_id}
              dep={dep}
              snapshot={snapshots[dep.run_id]}
              version={versions[dep.run_id] ?? 0}
              expanded={expanded === dep.run_id}
              onToggle={() => setExpanded((prev) => (prev === dep.run_id ? null : dep.run_id))}
              onChanged={onChanged}
            />
          ))}
        </div>
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
