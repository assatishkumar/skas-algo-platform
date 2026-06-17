import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api, brokers } from "../api/client";
import { FuturesDataSection, OptionsDataSection } from "../components/DerivData";
import { Card, ErrorBox, Spinner } from "../components/ui";
import type { DataSymbol } from "../types";

function FreshnessPill({ stale, staleDays }: { stale: boolean; staleDays: number | null }) {
  const label = staleDays == null ? "no data" : staleDays <= 0 ? "today" : `${staleDays}d old`;
  return (
    <span
      className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${
        stale
          ? "bg-amber-100 text-amber-700 border border-amber-300 dark:bg-amber-900/30 dark:text-amber-300 dark:border-amber-700/40"
          : "bg-emerald-100 text-emerald-700 border border-emerald-300 dark:bg-emerald-900/40 dark:text-emerald-300 dark:border-emerald-700/50"
      }`}
    >
      {label}
    </span>
  );
}

function SymbolDetail({ symbol, onRefreshed }: { symbol: string; onRefreshed: () => void }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["data-symbol", symbol],
    queryFn: () => api.dataSymbol(symbol),
  });
  const { data: accounts } = useQuery({ queryKey: ["brokers"], queryFn: brokers.list });
  const sessioned = (accounts ?? []).find((a) => a.has_session);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  async function refresh() {
    if (!sessioned) return;
    setBusy(true);
    setMsg(null);
    try {
      const { refreshed } = await brokers.refreshCache(sessioned.id, { symbols: [symbol] });
      const r = refreshed[symbol];
      setMsg(r?.error ? `Error: ${r.error}` : `Updated → ${r?.last_date ?? "?"}`);
      onRefreshed();
    } catch (e) {
      setMsg((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (isLoading) return <Spinner />;
  if (error) return <ErrorBox message={(error as Error).message} />;
  if (!data) return null;

  return (
    <Card>
      <div className="flex items-center justify-between gap-3 mb-3">
        <div>
          <div className="font-medium">{data.symbol}</div>
          <div className="text-xs text-slate-400">
            {data.start_date} → {data.end_date} · {data.total_records.toLocaleString("en-IN")} records
          </div>
        </div>
        <div className="text-right">
          <button
            onClick={refresh}
            disabled={busy || !sessioned}
            title={sessioned ? "Refresh on the shared Kite session" : "Log in on Brokers first"}
            className="rounded bg-slate-700 hover:bg-slate-600 px-3 py-1.5 text-xs disabled:opacity-50"
          >
            {busy ? "Refreshing…" : "Refresh"}
          </button>
          {msg && <div className="text-[11px] text-slate-400 mt-1">{msg}</div>}
        </div>
      </div>

      <div className="text-xs text-slate-400 mb-1">Records per year</div>
      <ResponsiveContainer width="100%" height={140}>
        <BarChart data={data.yearly} margin={{ top: 5, right: 5, bottom: 0, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
          <XAxis dataKey="year" tick={{ fontSize: 10, fill: "#94a3b8" }} />
          <YAxis tick={{ fontSize: 10, fill: "#94a3b8" }} width={36} />
          <Tooltip contentStyle={{ background: "rgb(var(--slate-900))", border: "1px solid rgb(var(--slate-700))", color: "rgb(var(--slate-100))" }} />
          <Bar dataKey="count" fill="#14b8a6" />
        </BarChart>
      </ResponsiveContainer>

      {data.recent.length > 0 && (
        <>
          <div className="text-xs text-slate-400 mt-3 mb-1">Recent close</div>
          <ResponsiveContainer width="100%" height={90}>
            <LineChart data={data.recent} margin={{ top: 5, right: 5, bottom: 0, left: 0 }}>
              <XAxis dataKey="date" hide />
              <YAxis domain={["auto", "auto"]} hide />
              <Tooltip contentStyle={{ background: "rgb(var(--slate-900))", border: "1px solid rgb(var(--slate-700))", color: "rgb(var(--slate-100))" }} />
              <Line type="monotone" dataKey="close" stroke="#6366f1" dot={false} strokeWidth={1.5} />
            </LineChart>
          </ResponsiveContainer>
        </>
      )}
    </Card>
  );
}

function DataToolbar({
  symbols,
  accountId,
  onChanged,
}: {
  symbols: DataSymbol[];
  accountId: number | null;
  onChanged: (added?: string) => void;
}) {
  const [newSym, setNewSym] = useState("");
  const [adding, setAdding] = useState(false);
  const [addProgress, setAddProgress] = useState<{ done: number; total: number } | null>(null);
  const [addMsg, setAddMsg] = useState<string | null>(null);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);

  const disabled = accountId == null;

  async function addSymbols() {
    if (accountId == null) return;
    // Accept a comma / space / newline separated list, e.g. "TCS, INFY".
    const requested = Array.from(
      new Set(
        newSym
          .split(/[\s,]+/)
          .map((s) => s.trim().toUpperCase())
          .filter(Boolean),
      ),
    );
    if (requested.length === 0) return;

    const cached = new Set(symbols.map((s) => s.symbol.toUpperCase()));
    const skipped = requested.filter((s) => cached.has(s));
    const toAdd = requested.filter((s) => !cached.has(s));

    if (toAdd.length === 0) {
      setAddMsg(`All ${requested.length} already cached — nothing to add.`);
      return;
    }

    setAdding(true);
    setAddMsg(null);
    const added: string[] = [];
    const failed: string[] = [];
    const CHUNK = 15;
    setAddProgress({ done: 0, total: toAdd.length });
    try {
      for (let i = 0; i < toAdd.length; i += CHUNK) {
        // Full backfill from 2010 for names not yet cached.
        const { refreshed } = await brokers.refreshCache(accountId, {
          symbols: toAdd.slice(i, i + CHUNK),
          start_date: "2010-01-01",
        });
        for (const [sym, r] of Object.entries(refreshed)) {
          if (r?.error || !r?.rows) failed.push(sym);
          else added.push(sym);
        }
        setAddProgress({ done: Math.min(i + CHUNK, toAdd.length), total: toAdd.length });
      }

      const parts: string[] = [];
      if (added.length) parts.push(`Added ${added.length} (${added.join(", ")})`);
      if (skipped.length) parts.push(`skipped ${skipped.length} already cached`);
      if (failed.length) parts.push(`no data for ${failed.length} (${failed.join(", ")})`);
      setAddMsg(parts.join(" · "));

      if (added.length) {
        setNewSym("");
        onChanged(added[0]);
      }
    } catch (e) {
      setAddMsg((e as Error).message);
    } finally {
      setAdding(false);
      setAddProgress(null);
    }
  }

  async function refreshAll() {
    if (accountId == null) return;
    const all = symbols.map((s) => s.symbol);
    const CHUNK = 15;
    setProgress({ done: 0, total: all.length });
    try {
      for (let i = 0; i < all.length; i += CHUNK) {
        await brokers.refreshCache(accountId, { symbols: all.slice(i, i + CHUNK) });
        setProgress({ done: Math.min(i + CHUNK, all.length), total: all.length });
      }
      onChanged();
    } finally {
      setProgress(null);
    }
  }

  return (
    <Card>
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <input
            className="rounded-md bg-slate-800 border border-slate-700 px-3 py-1.5 text-sm w-72 focus:outline-none focus:border-brand"
            placeholder="Add symbols (e.g. TCS, INFY)"
            value={newSym}
            onChange={(e) => setNewSym(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && addSymbols()}
            disabled={disabled}
          />
          <button
            onClick={addSymbols}
            disabled={disabled || adding || !newSym.trim()}
            className="rounded-md bg-brand hover:bg-brand-light px-3 py-1.5 text-sm font-medium disabled:opacity-50"
          >
            {addProgress ? `Adding ${addProgress.done}/${addProgress.total}…` : adding ? "Adding…" : "Add"}
          </button>
        </div>
        <button
          onClick={refreshAll}
          disabled={disabled || progress != null}
          className="rounded-md bg-slate-700 hover:bg-slate-600 px-3 py-1.5 text-sm disabled:opacity-50"
        >
          {progress ? `Refreshing ${progress.done}/${progress.total}…` : "Refresh all"}
        </button>
        {disabled && <span className="text-xs text-slate-500">Log in on Brokers to add/refresh.</span>}
        {addMsg && <span className="text-xs text-slate-400">{addMsg}</span>}
      </div>
    </Card>
  );
}

function StocksDataSection() {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<string | null>(null);

  const { data: summary } = useQuery({ queryKey: ["data-summary"], queryFn: api.dataSummary });
  const { data: accounts } = useQuery({ queryKey: ["brokers"], queryFn: brokers.list });
  const sessionedId = (accounts ?? []).find((a) => a.has_session)?.id ?? null;
  const { data: symbols, isLoading, error } = useQuery({
    queryKey: ["data-symbols"],
    queryFn: api.dataSymbols,
  });

  const { latest, staleCount } = useMemo(() => {
    const rows = symbols ?? [];
    const dates = rows.map((r) => r.last_date).filter(Boolean) as string[];
    return {
      latest: dates.length ? dates.slice().sort()[dates.length - 1] : null,
      staleCount: rows.filter((r) => r.stale).length,
    };
  }, [symbols]);

  if (isLoading) return <Spinner />;
  if (error) return <ErrorBox message={(error as Error).message} />;

  const q = search.trim().toLowerCase();
  const rows: DataSymbol[] = (symbols ?? []).filter((r) => !q || r.symbol.toLowerCase().includes(q));

  const onRefreshed = () => {
    queryClient.invalidateQueries({ queryKey: ["data-symbols"] });
    if (selected) queryClient.invalidateQueries({ queryKey: ["data-symbol", selected] });
  };

  const onToolbarChanged = (added?: string) => {
    queryClient.invalidateQueries({ queryKey: ["data-symbols"] });
    queryClient.invalidateQueries({ queryKey: ["data-summary"] });
    if (added) {
      queryClient.invalidateQueries({ queryKey: ["data-symbol", added] });
      setSelected(added);
    }
  };

  return (
    <div className="space-y-4">
      <Card>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
          <div>
            <div className="text-slate-400 text-xs">Cached symbols</div>
            <div className="text-lg font-semibold">{summary?.symbol_count ?? "—"}</div>
          </div>
          <div>
            <div className="text-slate-400 text-xs">Latest data</div>
            <div className="text-lg font-semibold">{latest ?? "—"}</div>
          </div>
          <div>
            <div className="text-slate-400 text-xs">Stale (&gt;5d)</div>
            <div className={`text-lg font-semibold ${staleCount ? "text-amber-600 dark:text-amber-400" : "text-emerald-600 dark:text-emerald-400"}`}>
              {staleCount}
            </div>
          </div>
          <div className="min-w-0">
            <div className="text-slate-400 text-xs">Cache location</div>
            <div className="text-xs truncate" title={summary?.db_path ?? ""}>{summary?.db_path ?? "—"}</div>
          </div>
        </div>
      </Card>

      <DataToolbar symbols={symbols ?? []} accountId={sessionedId} onChanged={onToolbarChanged} />

      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <input
            className="w-full mb-3 rounded-md bg-slate-800 border border-slate-700 px-3 py-1.5 text-sm focus:outline-none focus:border-brand"
            placeholder={`Search ${symbols?.length ?? ""} symbols`}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <div className="max-h-[60vh] overflow-y-auto">
            <table className="w-full text-sm">
              <thead className="text-slate-400 text-left sticky top-0 bg-slate-900">
                <tr>
                  <th className="py-1 pr-3">Symbol</th>
                  <th className="py-1 pr-3">Last date</th>
                  <th className="py-1 pr-3 text-right">Freshness</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr
                    key={r.symbol}
                    onClick={() => setSelected(r.symbol)}
                    className={`border-t border-slate-800 cursor-pointer hover:bg-slate-800/40 ${selected === r.symbol ? "bg-slate-800/60" : ""}`}
                  >
                    <td className="py-1.5 pr-3 font-medium">{r.symbol}</td>
                    <td className="py-1.5 pr-3 text-slate-400">{r.last_date ?? "—"}</td>
                    <td className="py-1.5 pr-3 text-right">
                      <FreshnessPill stale={r.stale} staleDays={r.stale_days} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>

        <div>
          {selected ? (
            <SymbolDetail symbol={selected} onRefreshed={onRefreshed} />
          ) : (
            <Card>
              <div className="text-slate-400 text-sm">Select a symbol to see its coverage and refresh it.</div>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}

const TABS = [
  { key: "stocks", label: "Stocks" },
  { key: "options", label: "Options" },
  { key: "futures", label: "Futures" },
];

export default function DataPage() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "stocks";
  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Data</h1>
      <div className="flex gap-1 border-b border-slate-800">
        {TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setParams(t.key === "stocks" ? {} : { tab: t.key })}
            className={`px-4 py-2 text-sm font-medium -mb-px border-b-2 ${
              tab === t.key
                ? "border-brand text-slate-100"
                : "border-transparent text-slate-400 hover:text-slate-200"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>
      {tab === "options" ? (
        <OptionsDataSection />
      ) : tab === "futures" ? (
        <FuturesDataSection />
      ) : (
        <StocksDataSection />
      )}
    </div>
  );
}
