import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "../../api/client";
import { Card } from "../ui";
import { formatInr } from "../../lib/format";
import { buildRoundTrips, bySymbol, type SymbolStat } from "../../lib/roundtrips";
import type { RoundTrip, RunAnalysis } from "../../types";

const pct = (v: number) => `${v >= 0 ? "+" : ""}${(v * 100).toFixed(1)}%`;
const tone = (v: number) => (v >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400");
const inr2 = (v: number) => formatInr(v, 2);
const dayFmt = (t: number) => new Date(t).toLocaleDateString("en-IN", { month: "short", year: "2-digit" });
const fullDate = (t: number) => new Date(t).toLocaleDateString("en-IN");
const daysBetween = (a: string, b: string) =>
  Math.max(0, Math.round((Date.parse(b) - Date.parse(a)) / 86_400_000));

const RANGES: { key: string; label: string; years: number }[] = [
  { key: "all", label: "All", years: Infinity },
  { key: "5y", label: "5Y", years: 5 },
  { key: "3y", label: "3Y", years: 3 },
  { key: "1y", label: "1Y", years: 1 },
  { key: "6m", label: "6M", years: 0.5 },
];

function Metric({ label, value, valueClass }: { label: string; value: string; valueClass?: string }) {
  return (
    <div className="rounded-md bg-slate-800/40 px-3 py-2">
      <div className="text-slate-400 text-[11px] mb-0.5">{label}</div>
      <div className={`font-medium tabular-nums ${valueClass ?? ""}`}>{value}</div>
    </div>
  );
}

function stParamsFor(analysis: RunAnalysis) {
  if (analysis.strategy_id !== "supertrend_momentum") return {};
  const p = analysis.params ?? {};
  return {
    st_period: Number(p.supertrend_period ?? 10),
    st_multiplier: Number(p.supertrend_multiplier ?? 3),
    st_timeframe: String(p.timeframe ?? "daily"),
  };
}

interface Marker {
  t: number;
  y: number;
  m: { type: string; entryPrice: number; exitPrice?: number; invested?: number; ageDays?: number; pnl?: number };
}

function ChartTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null;
  const marker: Marker["m"] | undefined = payload.map((p: any) => p.payload).find((pl: any) => pl?.m)?.m;
  const box = "rounded bg-slate-900 border border-slate-700 px-2 py-1 text-xs";
  if (marker) {
    if (marker.type === "Entry") {
      return (
        <div className={box}>
          <div className="font-medium text-emerald-600 dark:text-emerald-400">Entry · {fullDate(label)}</div>
          <div>price {inr2(marker.entryPrice)}</div>
          <div>invested {formatInr(marker.invested ?? 0)}</div>
        </div>
      );
    }
    return (
      <div className={box}>
        <div className={`font-medium ${marker.type === "Exit" ? "text-rose-600 dark:text-rose-400" : "text-amber-600 dark:text-amber-400"}`}>
          {marker.type} · {fullDate(label)}
        </div>
        <div>entry {inr2(marker.entryPrice)} → {inr2(marker.exitPrice ?? 0)}</div>
        <div>age {marker.ageDays}d</div>
        <div className={tone(marker.pnl ?? 0)}>P&L {formatInr(marker.pnl ?? 0)}</div>
      </div>
    );
  }
  const close = payload.find((p: any) => p.dataKey === "close")?.value;
  if (close == null) return null;
  return (
    <div className={box}>
      <div className="text-slate-400">{fullDate(label)}</div>
      <div>{inr2(close)}</div>
    </div>
  );
}

function StockChart({ symbol, rts, stParams }: { symbol: string; rts: RoundTrip[]; stParams: Record<string, unknown> }) {
  const [range, setRange] = useState("all");
  const [logScale, setLogScale] = useState(true);
  // A clicked trade focuses the chart on just that round-trip's window; index into sortedRts.
  const [focusIdx, setFocusIdx] = useState<number | null>(null);
  const hasST = Object.keys(stParams).length > 0;
  const sortedRts = [...rts].sort((a, b) => a.entryDate.localeCompare(b.entryDate));
  const focus = focusIdx != null ? sortedRts[focusIdx] : null;
  const chartBoxRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (focus) chartBoxRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusIdx]);

  const start = rts.reduce((m, r) => (r.entryDate < m ? r.entryDate : m), rts[0].entryDate);
  const end = rts.reduce((m, r) => (r.exitDate > m ? r.exitDate : m), rts[0].exitDate);
  const pad = (d: string, days: number) => new Date(Date.parse(d) + days * 86_400_000).toISOString().slice(0, 10);

  const { data, isLoading } = useQuery({
    queryKey: ["stockSeries", symbol, start, end, stParams],
    queryFn: () => api.stockSeries(symbol, { start: pad(start, -30), end: pad(end, 30), ...stParams }),
  });

  const allRows = (data?.points ?? []).map((p) => ({
    t: Date.parse(p.date),
    close: p.close,
    stUp: p.direction != null && p.direction > 0 ? p.supertrend ?? null : null,
    stDown: p.direction != null && p.direction < 0 ? p.supertrend ?? null : null,
  }));
  const maxT = allRows.length ? allRows[allRows.length - 1].t : 0;
  const years = RANGES.find((r) => r.key === range)!.years;
  const DAY = 86_400_000;
  // Focused on one trade → clip to [entry, exit] + a buffer (25% of the hold, min 12d) on each
  // side. Otherwise the range buttons window back from the latest bar.
  let minVisT: number;
  let maxVisT: number;
  if (focus) {
    const fStart = Date.parse(focus.entryDate);
    const fEnd = Date.parse(focus.exitDate);
    const buf = Math.max((fEnd - fStart) * 0.25, 12 * DAY);
    minVisT = fStart - buf;
    maxVisT = fEnd + buf;
  } else {
    minVisT = years === Infinity ? -Infinity : maxT - years * 365 * DAY;
    maxVisT = Infinity;
  }
  const rows = allRows.filter((r) => r.t >= minVisT && r.t <= maxVisT);

  const entries: Marker[] = [];
  const books: Marker[] = [];
  const exits: Marker[] = [];
  for (const r of rts) {
    const et = Date.parse(r.entryDate);
    if (et >= minVisT && et <= maxVisT)
      entries.push({ t: et, y: r.entryPrice, m: { type: "Entry", entryPrice: r.entryPrice, invested: r.entryPrice * r.qty } });
    for (const e of r.exits) {
      const xt = Date.parse(e.date);
      if (xt < minVisT || xt > maxVisT) continue;
      const isBook = e.tag === "BOOK";
      (isBook ? books : exits).push({
        t: Date.parse(e.date), y: e.price,
        m: { type: isBook ? "50% book" : "Exit", entryPrice: r.entryPrice, exitPrice: e.price,
             ageDays: daysBetween(r.entryDate, e.date), pnl: (e.price - r.entryPrice) * e.units },
      });
    }
  }

  const closes = rows.map((r) => r.close).filter((c): c is number => c != null);
  const yDomain: [number | string, number | string] = logScale && closes.length
    ? [Math.max(1, Math.min(...closes) * 0.9), Math.max(...closes) * 1.1]
    : ["auto", "auto"];
  const total = rts.reduce((s, r) => s + r.pnl, 0);

  return (
    <Card className="mt-3">
      <div className="flex items-center justify-between mb-2 flex-wrap gap-2">
        <div className="font-medium">
          {symbol} <span className="text-slate-500 text-sm">· {rts.length} trade{rts.length > 1 ? "s" : ""} · </span>
          <span className={tone(total)}>{formatInr(total)}</span>
        </div>
        <div className="flex items-center gap-2 text-xs">
          {focus && (
            <button
              onClick={() => setFocusIdx(null)}
              title="Back to the full range"
              className="px-2 py-0.5 rounded bg-brand text-white inline-flex items-center gap-1"
            >
              {focus.entryDate} → {focus.exitDate} ✕
            </button>
          )}
          <div className="flex rounded bg-slate-800/60 p-0.5">
            {RANGES.map((r) => (
              <button key={r.key} onClick={() => { setRange(r.key); setFocusIdx(null); }}
                className={`px-2 py-0.5 rounded ${!focus && range === r.key ? "bg-brand text-white" : "text-slate-400 hover:text-slate-200"}`}>
                {r.label}
              </button>
            ))}
          </div>
          <button onClick={() => setLogScale((v) => !v)}
            className={`px-2 py-0.5 rounded ${logScale ? "bg-brand text-white" : "bg-slate-800/60 text-slate-400"}`}>
            log
          </button>
        </div>
      </div>
      <div ref={chartBoxRef} className="scroll-mt-4">
      {isLoading ? (
        <div className="text-slate-500 text-sm">Loading {symbol} chart…</div>
      ) : rows.length === 0 ? (
        <div className="text-slate-500 text-sm">No price data cached for {symbol}.</div>
      ) : (
        <ResponsiveContainer width="100%" height={320}>
          <ComposedChart data={rows} margin={{ top: 8, right: 12, bottom: 0, left: 8 }}>
            <CartesianGrid stroke="#1e293b" />
            <XAxis dataKey="t" type="number" scale="time"
              domain={focus ? [minVisT, maxVisT] : ["dataMin", "dataMax"]}
              allowDataOverflow={!!focus}
              tick={{ fontSize: 11, fill: "#94a3b8" }} tickFormatter={dayFmt} allowDuplicatedCategory={false} />
            <YAxis scale={logScale ? "log" : "auto"} domain={yDomain} allowDataOverflow={logScale}
              tick={{ fontSize: 11, fill: "#94a3b8" }} width={60}
              tickFormatter={(v) => Math.round(v).toString()} />
            <Tooltip content={<ChartTooltip />} />
            <Line type="monotone" dataKey="close" stroke="#38bdf8" strokeWidth={1.5} dot={false} name="Price" isAnimationActive={false} />
            <Line type="monotone" dataKey="stUp" stroke="#10b981" strokeWidth={1.5} dot={false} connectNulls={false} name="ST↑" isAnimationActive={false} />
            <Line type="monotone" dataKey="stDown" stroke="#f43f5e" strokeWidth={1.5} dot={false} connectNulls={false} name="ST↓" isAnimationActive={false} />
            <Scatter data={entries} dataKey="y" fill="#10b981" isAnimationActive={false} />
            <Scatter data={books} dataKey="y" fill="#f59e0b" isAnimationActive={false} />
            <Scatter data={exits} dataKey="y" fill="#f43f5e" isAnimationActive={false} />
          </ComposedChart>
        </ResponsiveContainer>
      )}
      </div>
      <div className="text-xs text-slate-500 mt-1">
        <span className="text-emerald-600 dark:text-emerald-400">● entry</span> · <span className="text-amber-600 dark:text-amber-400">● 50% book</span> ·{" "}
        <span className="text-rose-600 dark:text-rose-400">● exit</span>
        {hasST && <> · <span className="text-emerald-600 dark:text-emerald-400">— ST↑</span> / <span className="text-rose-600 dark:text-rose-400">ST↓</span></>}
        {" "}· hover a dot for details · click a trade below to zoom
      </div>

      {/* All trades for this stock */}
      <div className="overflow-x-auto mt-3">
        <table className="w-full text-sm tabular-nums">
          <thead className="text-slate-400 text-left">
            <tr>
              <th className="py-1 pr-3">Entry date</th>
              <th className="py-1 pr-3 text-right">Entry</th>
              <th className="py-1 pr-3">Exit date</th>
              <th className="py-1 pr-3 text-right">Exit</th>
              <th className="py-1 pr-3 text-right">Hold</th>
              <th className="py-1 pr-3 text-right">P&L</th>
            </tr>
          </thead>
          <tbody>
            {sortedRts.map((r, i) => {
              const last = r.exits[r.exits.length - 1];
              return (
                <tr
                  key={`${r.entryDate}-${i}`}
                  onClick={() => setFocusIdx(focusIdx === i ? null : i)}
                  title="Click to zoom the chart to this trade"
                  className={`border-t border-slate-800 cursor-pointer hover:bg-slate-200/60 dark:hover:bg-slate-800/50 ${
                    focusIdx === i ? "bg-brand/10" : ""
                  }`}
                >
                  <td className="py-1 pr-3">{r.entryDate}</td>
                  <td className="py-1 pr-3 text-right">{inr2(r.entryPrice)}</td>
                  <td className="py-1 pr-3">{r.exitDate}{r.exits.length > 1 ? <span className="text-slate-500"> (+book)</span> : null}</td>
                  <td className="py-1 pr-3 text-right">{last ? inr2(last.price) : "—"}</td>
                  <td className="py-1 pr-3 text-right">{r.holdingDays}d</td>
                  <td className={`py-1 pr-3 text-right ${tone(r.pnl)}`}>{formatInr(r.pnl)} <span className="text-slate-500">({pct(r.pnlPct)})</span></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

export default function EquityTradeAnalysis({ analysis }: { analysis: RunAnalysis }) {
  const roundTrips = useMemo(() => buildRoundTrips(analysis.trades), [analysis.trades]);
  const stocks = useMemo(() => bySymbol(roundTrips), [roundTrips]);
  const stParams = useMemo(() => stParamsFor(analysis), [analysis]);
  const [selected, setSelected] = useState<string | null>(null);
  const [sort, setSort] = useState<"pnl" | "winRate" | "trades">("pnl");
  const chartRef = useRef<HTMLDivElement>(null);

  // Scroll the chart into view whenever a stock is selected.
  useEffect(() => {
    if (selected) chartRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [selected]);

  if (roundTrips.length === 0) {
    return <Card><div className="text-slate-400 text-sm">No completed round-trips in this run yet.</div></Card>;
  }

  const total = roundTrips.reduce((s, r) => s + r.pnl, 0);
  const wins = roundTrips.filter((r) => r.won).length;
  const avgHold = roundTrips.reduce((s, r) => s + r.holdingDays, 0) / roundTrips.length;
  const sorted = [...stocks].sort((a, b) =>
    sort === "pnl" ? b.pnl - a.pnl : sort === "winRate" ? b.winRate - a.winRate : b.trades - a.trades);
  const ranked = [...roundTrips].sort((a, b) => b.pnl - a.pnl);
  const best = ranked.slice(0, 3);
  const worst = ranked.slice(-3).reverse();
  const show = (sym: string) => setSelected(sym);

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 md:grid-cols-5 gap-2 text-sm">
        <Metric label="Realized P&L" value={formatInr(total)} valueClass={tone(total)} />
        <Metric label="Win rate" value={`${(wins / roundTrips.length * 100).toFixed(0)}%`} />
        <Metric label="Round-trips" value={String(roundTrips.length)} />
        <Metric label="Stocks traded" value={String(stocks.length)} />
        <Metric label="Avg holding" value={`${avgHold.toFixed(0)}d`} />
      </div>

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

      <div ref={chartRef}>
        {selected && (
          <StockChart key={selected} symbol={selected} stParams={stParams}
            rts={stocks.find((s) => s.symbol === selected)?.roundTrips ?? []} />
        )}
      </div>
    </div>
  );
}
