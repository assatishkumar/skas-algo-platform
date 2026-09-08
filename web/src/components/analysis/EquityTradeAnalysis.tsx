import { useEffect, useMemo, useRef, useState } from "react";
import { Card } from "../ui";
import StockChart, { stParamsFor } from "./SuperTrendChart";
import { formatInr } from "../../lib/format";
import { bySymbol, pairTrades, type SymbolStat } from "../../lib/roundtrips";
import type { OpenPosition, RunAnalysis } from "../../types";

const pct = (v: number) => `${v >= 0 ? "+" : ""}${(v * 100).toFixed(1)}%`;
const tone = (v: number) => (v >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400");
const inr2 = (v: number) => formatInr(v, 2);
const TODAY = new Date().toISOString().slice(0, 10);
const daysBetween = (a: string, b: string) =>
  Math.max(0, Math.round((Date.parse(b) - Date.parse(a)) / 86_400_000));


function Metric({ label, value, valueClass }: { label: string; value: string; valueClass?: string }) {
  return (
    <div className="rounded-md bg-slate-800/40 px-3 py-2">
      <div className="text-slate-400 text-[11px] mb-0.5">{label}</div>
      <div className={`font-medium tabular-nums ${valueClass ?? ""}`}>{value}</div>
    </div>
  );
}




export default function EquityTradeAnalysis({ analysis }: { analysis: RunAnalysis }) {
  const { roundTrips, openPositions } = useMemo(() => pairTrades(analysis.trades), [analysis.trades]);
  const stocks = useMemo(() => bySymbol(roundTrips), [roundTrips]);
  const openBySymbol = useMemo(() => {
    const m = new Map<string, OpenPosition[]>();
    for (const o of openPositions) (m.get(o.symbol) ?? m.set(o.symbol, []).get(o.symbol)!).push(o);
    return m;
  }, [openPositions]);
  const stParams = useMemo(() => stParamsFor(analysis.strategy_id, analysis.params), [analysis]);
  const pullback = String(analysis.params?.entry_mode ?? "") === "pullback";
  const pullbackPct = Number(analysis.params?.pullback_pct ?? 0.1) || 0.1;
  const [selected, setSelected] = useState<string | null>(null);
  const [sort, setSort] = useState<"pnl" | "winRate" | "trades">("pnl");
  const chartRef = useRef<HTMLDivElement>(null);

  // Scroll the chart into view whenever a stock is selected.
  useEffect(() => {
    if (selected) chartRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [selected]);

  if (roundTrips.length === 0 && openPositions.length === 0) {
    return <Card><div className="text-slate-400 text-sm">No trades in this run yet.</div></Card>;
  }

  const total = roundTrips.reduce((s, r) => s + r.pnl, 0);
  const wins = roundTrips.filter((r) => r.won).length;
  const avgHold = roundTrips.length ? roundTrips.reduce((s, r) => s + r.holdingDays, 0) / roundTrips.length : 0;
  const openInvested = openPositions.reduce((s, o) => s + o.invested, 0);
  const sorted = [...stocks].sort((a, b) =>
    sort === "pnl" ? b.pnl - a.pnl : sort === "winRate" ? b.winRate - a.winRate : b.trades - a.trades);
  const ranked = [...roundTrips].sort((a, b) => b.pnl - a.pnl);
  const best = ranked.slice(0, 3);
  const worst = ranked.slice(-3).reverse();
  const show = (sym: string) => setSelected(sym);

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 md:grid-cols-6 gap-2 text-sm">
        <Metric label="Realized P&L" value={formatInr(total)} valueClass={tone(total)} />
        <Metric label="Win rate" value={roundTrips.length ? `${(wins / roundTrips.length * 100).toFixed(0)}%` : "—"} />
        <Metric label="Round-trips" value={String(roundTrips.length)} />
        <Metric label="Avg holding" value={roundTrips.length ? `${avgHold.toFixed(0)}d` : "—"} />
        <Metric label="Open positions" value={String(openPositions.length)} />
        <Metric label="Invested (open)" value={formatInr(openInvested)} />
      </div>

      {openPositions.length > 0 && (
        <Card>
          <div className="text-sm font-medium text-slate-300 mb-2">
            Open positions <span className="text-slate-500 font-normal">· {openPositions.length} held · {formatInr(openInvested)} deployed</span>
          </div>
          <div className="overflow-x-auto max-h-80 overflow-y-auto">
            <table className="w-full text-sm tabular-nums">
              <thead className="text-slate-400 text-left sticky top-0 bg-slate-900">
                <tr>
                  <th className="py-1 pr-3">Stock</th>
                  <th className="py-1 pr-3">Entry date</th>
                  <th className="py-1 pr-3 text-right">Held</th>
                  <th className="py-1 pr-3 text-right">Qty</th>
                  <th className="py-1 pr-3 text-right">Entry</th>
                  <th className="py-1 pr-3 text-right">Invested</th>
                </tr>
              </thead>
              <tbody>
                {openPositions.map((o, i) => (
                  <tr key={`${o.symbol}-${i}`} onClick={() => show(o.symbol)}
                    className={`border-t border-slate-800 cursor-pointer hover:bg-slate-800/40 ${selected === o.symbol ? "bg-slate-800/60" : ""}`}>
                    <td className="py-1 pr-3 font-medium">{o.symbol}</td>
                    <td className="py-1 pr-3">{o.entryDate}</td>
                    <td className="py-1 pr-3 text-right">{daysBetween(o.entryDate, TODAY)}d</td>
                    <td className="py-1 pr-3 text-right">{o.qty}</td>
                    <td className="py-1 pr-3 text-right">{inr2(o.entryPrice)}</td>
                    <td className="py-1 pr-3 text-right">{formatInr(o.invested)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="text-xs text-slate-500 mt-1">Click a holding to chart it (entry, price-to-now, unrealized P&L).</div>
        </Card>
      )}

      {roundTrips.length > 0 && (
      <div className="grid md:grid-cols-2 gap-3">
        {[["Best trades", best] as const, ["Worst trades", worst] as const].map(([title, list]) => (
          <Card key={title}>
            <div className="text-sm font-medium text-slate-300 mb-2">{title}</div>
            <div className="space-y-1">
              {list.map((r, i) => (
                <button key={`${r.symbol}-${r.entryDate}-${i}`} onClick={() => show(r.symbol)}
                  className="w-full flex justify-between text-sm hover:bg-slate-800/50 rounded px-2 py-1">
                  <span>{r.symbol} <span className="text-slate-500">{r.entryDate.slice(0, 7)} · {r.holdingDays}d</span></span>
                  <span className={tone(r.pnl)}>{formatInr(r.pnl)} ({pct(r.pnlPct)})</span>
                </button>
              ))}
            </div>
          </Card>
        ))}
      </div>
      )}

      {roundTrips.length > 0 && (
      <Card>
        <div className="flex items-center justify-between mb-2">
          <div className="text-sm font-medium text-slate-300">By stock — P&L contribution</div>
          <label className="text-xs text-slate-400 flex items-center gap-1">sort
            <select className="rounded bg-slate-800 border border-slate-700 px-1.5 py-0.5"
              value={sort} onChange={(e) => setSort(e.target.value as typeof sort)}>
              <option value="pnl">P&L</option>
              <option value="winRate">Win rate</option>
              <option value="trades">Trades</option>
            </select>
          </label>
        </div>
        <div className="overflow-x-auto max-h-96 overflow-y-auto">
          <table className="w-full text-sm tabular-nums">
            <thead className="text-slate-400 text-left sticky top-0 bg-slate-900">
              <tr>
                <th className="py-1 pr-3">Stock</th>
                <th className="py-1 pr-3 text-right">P&L</th>
                <th className="py-1 pr-3 text-right">Trades</th>
                <th className="py-1 pr-3 text-right">Win %</th>
                <th className="py-1 pr-3 text-right">Avg hold</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((s: SymbolStat) => (
                <tr key={s.symbol} onClick={() => show(s.symbol)}
                  className={`border-t border-slate-800 cursor-pointer hover:bg-slate-800/40 ${selected === s.symbol ? "bg-slate-800/60" : ""}`}>
                  <td className="py-1 pr-3 font-medium">{s.symbol}</td>
                  <td className={`py-1 pr-3 text-right ${tone(s.pnl)}`}>{formatInr(s.pnl)}</td>
                  <td className="py-1 pr-3 text-right">{s.trades}</td>
                  <td className="py-1 pr-3 text-right">{(s.winRate * 100).toFixed(0)}%</td>
                  <td className="py-1 pr-3 text-right">{s.avgHold.toFixed(0)}d</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="text-xs text-slate-500 mt-1">Click a stock to chart its trades.</div>
      </Card>
      )}

      <div ref={chartRef}>
        {selected && (
          <StockChart key={selected} symbol={selected} stParams={stParams} pullback={pullback} pullbackPct={pullbackPct}
            rts={stocks.find((s) => s.symbol === selected)?.roundTrips ?? []}
            opens={openBySymbol.get(selected) ?? []} />
        )}
      </div>
    </div>
  );
}
