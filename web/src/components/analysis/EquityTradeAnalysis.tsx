import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  CartesianGrid,
  ComposedChart,
  Customized,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "../../api/client";
import { Card } from "../ui";
import { formatInr } from "../../lib/format";
import { bySymbol, pairTrades, type SymbolStat } from "../../lib/roundtrips";
import type { OpenPosition, RoundTrip, RunAnalysis } from "../../types";

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

// Trade markers: bigger arrows with a white outline so they read on candles.
// Entry = green ▲ from below (tip up at the fill price); exit = red ▼ from above; book = amber ◆.
function EntryArrow({ cx, cy }: any) {
  if (cx == null || cy == null) return null;
  const w = 8;
  const tip = cy + 5; // tip points up to the price; body sits just below
  return <path d={`M ${cx} ${tip} L ${cx - w} ${tip + 14} L ${cx + w} ${tip + 14} Z`} fill="#059669" stroke="#fff" strokeWidth={1.5} />;
}
function ExitArrow({ cx, cy }: any) {
  if (cx == null || cy == null) return null;
  const w = 8;
  const tip = cy - 5; // tip points down to the price; body sits just above
  return <path d={`M ${cx} ${tip} L ${cx - w} ${tip - 14} L ${cx + w} ${tip - 14} Z`} fill="#e11d48" stroke="#fff" strokeWidth={1.5} />;
}
function BookMarker({ cx, cy }: any) {
  if (cx == null || cy == null) return null;
  const s = 6;
  return <path d={`M ${cx} ${cy - s} L ${cx + s} ${cy} L ${cx} ${cy + s} L ${cx - s} ${cy} Z`} fill="#d97706" stroke="#fff" strokeWidth={1.25} />;
}
// Still-open position marked at the latest bar — a hollow blue dot ("now / holding").
function HoldMarker({ cx, cy }: any) {
  if (cx == null || cy == null) return null;
  return <circle cx={cx} cy={cy} r={5} fill="#0ea5e9" stroke="#fff" strokeWidth={1.5} />;
}

type CandleRow = { t: number; open: number | null; high: number | null; low: number | null; close: number | null };

/** OHLC candlesticks drawn with the chart's own x/y scales (so they respect the time axis,
 *  the focus zoom, and log mode). Rendered inside the ComposedChart via <Customized>. */
function makeCandles(rows: CandleRow[]) {
  return function Candles(props: any) {
    const { xAxisMap, yAxisMap } = props;
    if (!xAxisMap || !yAxisMap) return null;
    const xa: any = xAxisMap[Object.keys(xAxisMap)[0]];
    const ya: any = yAxisMap[Object.keys(yAxisMap)[0]];
    const xScale = xa?.scale;
    const yScale = ya?.scale;
    if (!xScale || !yScale) return null;
    // Candle width ≈ 60% of the median pixel gap between bars (clamped 1..14px).
    const px = rows.map((r) => xScale(r.t));
    let gap = 8;
    if (px.length > 1) {
      const diffs: number[] = [];
      for (let i = 1; i < px.length; i++) diffs.push(Math.abs(px[i] - px[i - 1]));
      diffs.sort((a, b) => a - b);
      gap = diffs[Math.floor(diffs.length / 2)] || 8;
    }
    const w = Math.max(1, Math.min(gap * 0.6, 14));
    return (
      <g>
        {rows.map((r, i) => {
          if (r.open == null || r.close == null || r.high == null || r.low == null) return null;
          const cx = xScale(r.t);
          const up = r.close >= r.open;
          const color = up ? "#10b981" : "#f43f5e";
          const yO = yScale(r.open);
          const yC = yScale(r.close);
          const top = Math.min(yO, yC);
          const h = Math.max(1, Math.abs(yC - yO));
          return (
            <g key={i} stroke={color} fill={color}>
              <line x1={cx} x2={cx} y1={yScale(r.high)} y2={yScale(r.low)} strokeWidth={1} />
              <rect x={cx - w / 2} width={w} y={top} height={h} />
            </g>
          );
        })}
      </g>
    );
  };
}

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
    const isOpen = marker.type === "Open";
    const head = isOpen ? "text-sky-600 dark:text-sky-400" : marker.type === "Exit" ? "text-rose-600 dark:text-rose-400" : "text-amber-600 dark:text-amber-400";
    return (
      <div className={box}>
        <div className={`font-medium ${head}`}>{isOpen ? "Holding" : marker.type} · {fullDate(label)}</div>
        <div>entry {inr2(marker.entryPrice)} → {isOpen ? "now " : ""}{inr2(marker.exitPrice ?? 0)}</div>
        <div>age {marker.ageDays}d</div>
        <div className={tone(marker.pnl ?? 0)}>{isOpen ? "Unrealized " : ""}P&L {formatInr(marker.pnl ?? 0)}</div>
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

type StockItem = {
  entryDate: string;
  entryPrice: number;
  qty: number;
  exitDate: string | null; // null = still open
  exitPrice: number | null;
  holdingDays: number;
  pnl: number | null; // realized; null for an open position (unrealized shown live)
  pnlPct: number | null;
  open: boolean;
};

const TODAY = new Date().toISOString().slice(0, 10);

type Candle = { t: number; high: number | null; close: number | null; stUp: number | null; stDown: number | null };

/** The "prior high" a pullback breakout crossed to enter — mirrors the strategy exactly: the running
 *  max CLOSE since the most recent green (SuperTrend) flip, frozen at the bar where price had pulled
 *  back ≥ pullbackPct from that peak. The entry then fires when close breaks back above it. null if no
 *  flip precedes the entry or no qualifying pullback occurred. */
function priorHigh(rows: Candle[], entryT: number, pullbackPct: number): { pivot: number; flipT: number } | null {
  let idx = -1;
  for (let i = 0; i < rows.length; i++) {
    if (rows[i].t <= entryT) idx = i;
    else break;
  }
  if (idx < 1) return null;
  let flipIdx = -1;
  for (let j = idx; j >= 1; j--) {
    if (rows[j].stUp != null && rows[j - 1].stUp == null) { flipIdx = j; break; } // red→green
  }
  if (flipIdx < 0) return null;
  let peak = -Infinity;
  let pivot: number | null = null;
  for (let k = flipIdx; k < idx && pivot == null; k++) {
    const c = rows[k].close;
    if (c == null) continue;
    if (c > peak) peak = c;
    else if (peak > 0 && (peak - c) / peak >= pullbackPct && c < peak) pivot = peak; // pullback → lock
  }
  return pivot != null ? { pivot, flipT: rows[flipIdx].t } : null;
}

function StockChart({ symbol, rts, opens, stParams, pullback, pullbackPct }: { symbol: string; rts: RoundTrip[]; opens: OpenPosition[]; stParams: Record<string, unknown>; pullback: boolean; pullbackPct: number }) {
  const [range, setRange] = useState("all");
  const [logScale, setLogScale] = useState(true);
  const [chartType, setChartType] = useState<"line" | "candle">("line");
  // A clicked trade focuses the chart on just that position's window; index into `items`.
  const [focusIdx, setFocusIdx] = useState<number | null>(null);
  const hasST = Object.keys(stParams).length > 0;
  // Completed round-trips + still-open positions, oldest entry first.
  const items: StockItem[] = [
    ...rts.map((r) => ({
      entryDate: r.entryDate, entryPrice: r.entryPrice, qty: r.qty, exitDate: r.exitDate,
      exitPrice: r.exits[r.exits.length - 1]?.price ?? null, holdingDays: r.holdingDays,
      pnl: r.pnl, pnlPct: r.pnlPct, open: false,
    })),
    ...opens.map((o) => ({
      entryDate: o.entryDate, entryPrice: o.entryPrice, qty: o.qty, exitDate: null,
      exitPrice: null, holdingDays: daysBetween(o.entryDate, TODAY), pnl: null, pnlPct: null, open: true,
    })),
  ].sort((a, b) => a.entryDate.localeCompare(b.entryDate));
  const focus = focusIdx != null ? items[focusIdx] : null;
  const chartBoxRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (focus) chartBoxRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusIdx]);

  const hasOpen = opens.length > 0;
  const start = items.reduce((m, it) => (it.entryDate < m ? it.entryDate : m), items[0].entryDate);
  // Open positions run to today; closed ones end at their last exit.
  const end = items.reduce((m, it) => {
    const e = it.exitDate ?? TODAY;
    return e > m ? e : m;
  }, hasOpen ? TODAY : items[0].exitDate ?? TODAY);
  const pad = (d: string, days: number) => new Date(Date.parse(d) + days * 86_400_000).toISOString().slice(0, 10);

  const { data, isLoading } = useQuery({
    queryKey: ["stockSeries", symbol, start, end, stParams],
    queryFn: () => api.stockSeries(symbol, { start: pad(start, -60), end: pad(end, 30), ...stParams }),
  });

  const allRows = (data?.points ?? []).map((p) => ({
    t: Date.parse(p.date),
    open: p.open,
    high: p.high,
    low: p.low,
    close: p.close,
    stUp: p.direction != null && p.direction > 0 ? p.supertrend ?? null : null,
    stDown: p.direction != null && p.direction < 0 ? p.supertrend ?? null : null,
  }));
  const maxT = allRows.length ? allRows[allRows.length - 1].t : 0;
  const lastClose = allRows.length ? allRows[allRows.length - 1].close : null;
  const years = RANGES.find((r) => r.key === range)!.years;
  const DAY = 86_400_000;
  // For a focused pullback trade, the "prior high" it broke = the post-flip peak before the entry.
  const focusPivot = focus && pullback && hasST ? priorHigh(allRows, Date.parse(focus.entryDate), pullbackPct) : null;
  // Focused on one trade → clip to [entry, exit] + a buffer (25% of the hold, min 12d) on each
  // side (open positions run to today). Otherwise the range buttons window back from the latest bar.
  let minVisT: number;
  let maxVisT: number;
  if (focus) {
    const fStart = Date.parse(focus.entryDate);
    const fEnd = Date.parse(focus.exitDate ?? TODAY);
    const buf = Math.max((fEnd - fStart) * 0.25, 12 * DAY);
    minVisT = fStart - buf;
    // Reach back to the flip so the pullback setup (flip → peak → dip → breakout) is visible.
    if (focusPivot && focusPivot.flipT < minVisT) minVisT = focusPivot.flipT - 3 * DAY;
    maxVisT = fEnd + buf;
  } else {
    minVisT = years === Infinity ? -Infinity : maxT - years * 365 * DAY;
    maxVisT = Infinity;
  }
  const rows = allRows.filter((r) => r.t >= minVisT && r.t <= maxVisT);

  const entries: Marker[] = [];
  const books: Marker[] = [];
  const exits: Marker[] = [];
  const holdings: Marker[] = [];
  for (const it of items) {
    const et = Date.parse(it.entryDate);
    if (et >= minVisT && et <= maxVisT)
      entries.push({ t: et, y: it.entryPrice, m: { type: "Entry", entryPrice: it.entryPrice, invested: it.entryPrice * it.qty } });
  }
  for (const r of rts) {
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
  // Still-open positions: a "now / holding" dot at the latest bar, with unrealized P&L.
  if (lastClose != null && maxT >= minVisT && maxT <= maxVisT) {
    for (const o of opens) {
      holdings.push({
        t: maxT, y: lastClose,
        m: { type: "Open", entryPrice: o.entryPrice, exitPrice: lastClose,
             ageDays: daysBetween(o.entryDate, TODAY), pnl: (lastClose - o.entryPrice) * o.qty },
      });
    }
  }

  const closes = rows.map((r) => r.close).filter((c): c is number => c != null);
  const yDomain: [number | string, number | string] = logScale && closes.length
    ? [Math.max(1, Math.min(...closes) * 0.9), Math.max(...closes) * 1.1]
    : ["auto", "auto"];
  const realized = rts.reduce((s, r) => s + r.pnl, 0);
  const unrealized = lastClose != null ? opens.reduce((s, o) => s + (lastClose - o.entryPrice) * o.qty, 0) : null;

  return (
    <Card className="mt-3">
      <div className="flex items-center justify-between mb-2 flex-wrap gap-2">
        <div className="font-medium">
          {symbol}
          {rts.length > 0 && (
            <>
              <span className="text-slate-500 text-sm"> · {rts.length} closed · </span>
              <span className={tone(realized)}>{formatInr(realized)}</span>
            </>
          )}
          {hasOpen && unrealized != null && (
            <span className="text-slate-500 text-sm"> · {opens.length} open · <span className={tone(unrealized)}>{formatInr(unrealized)} unreal.</span></span>
          )}
        </div>
        <div className="flex items-center gap-2 text-xs">
          {focus && (
            <button
              onClick={() => setFocusIdx(null)}
              title="Back to the full range"
              className="px-2 py-0.5 rounded bg-brand text-white inline-flex items-center gap-1"
            >
              {focus.entryDate} → {focus.exitDate ?? "now"} ✕
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
          <button onClick={() => setChartType((t) => (t === "candle" ? "line" : "candle"))}
            title="Toggle line / candlestick"
            className={`px-2 py-0.5 rounded ${chartType === "candle" ? "bg-brand text-white" : "bg-slate-800/60 text-slate-400"}`}>
            candles
          </button>
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
            {chartType === "candle" && <Customized component={makeCandles(rows)} />}
            {/* Keep the close line in candle mode but transparent — it still feeds the tooltip. */}
            <Line type="monotone" dataKey="close" stroke={chartType === "candle" ? "transparent" : "#38bdf8"} strokeWidth={1.5} dot={false} name="Price" isAnimationActive={false} />
            <Line type="monotone" dataKey="stUp" stroke="#10b981" strokeWidth={1.5} dot={false} connectNulls={false} name="ST↑" isAnimationActive={false} />
            <Line type="monotone" dataKey="stDown" stroke="#f43f5e" strokeWidth={1.5} dot={false} connectNulls={false} name="ST↓" isAnimationActive={false} />
            {focusPivot && focus && (
              <ReferenceLine
                segment={[{ x: Math.max(focusPivot.flipT, minVisT), y: focusPivot.pivot }, { x: Date.parse(focus.entryDate), y: focusPivot.pivot }]}
                stroke="#f59e0b" strokeDasharray="5 3" strokeWidth={1.5}
                label={{ value: `prior high ${Math.round(focusPivot.pivot)}`, position: "insideTopLeft", fill: "#d97706", fontSize: 10 }}
                ifOverflow="extendDomain" isFront />
            )}
            <Scatter data={books} dataKey="y" shape={<BookMarker />} isAnimationActive={false} />
            <Scatter data={entries} dataKey="y" shape={<EntryArrow />} isAnimationActive={false} />
            <Scatter data={exits} dataKey="y" shape={<ExitArrow />} isAnimationActive={false} />
            <Scatter data={holdings} dataKey="y" shape={<HoldMarker />} isAnimationActive={false} />
          </ComposedChart>
        </ResponsiveContainer>
      )}
      </div>
      <div className="text-xs text-slate-500 mt-1">
        <span className="text-emerald-600 dark:text-emerald-400">▲ entry</span> · <span className="text-amber-600 dark:text-amber-400">◆ 50% book</span> ·{" "}
        <span className="text-rose-600 dark:text-rose-400">▼ exit</span>
        {hasOpen && <> · <span className="text-sky-600 dark:text-sky-400">● holding (now)</span></>}
        {hasST && <> · <span className="text-emerald-600 dark:text-emerald-400">— ST↑</span> / <span className="text-rose-600 dark:text-rose-400">ST↓</span></>}
        {focusPivot && <> · <span className="text-amber-600 dark:text-amber-400">┄ prior high (broken on entry)</span></>}
        {" "}· hover a dot for details · click a trade below to zoom
      </div>

      {/* All trades for this stock (closed + open) */}
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
            {items.map((it, i) => {
              const unreal = it.open && lastClose != null ? (lastClose - it.entryPrice) * it.qty : null;
              return (
                <tr
                  key={`${it.entryDate}-${i}`}
                  onClick={() => setFocusIdx(focusIdx === i ? null : i)}
                  title="Click to zoom the chart to this trade"
                  className={`border-t border-slate-800 cursor-pointer hover:bg-slate-200/60 dark:hover:bg-slate-800/50 ${
                    focusIdx === i ? "bg-brand/10" : ""
                  }`}
                >
                  <td className="py-1 pr-3">{it.entryDate}</td>
                  <td className="py-1 pr-3 text-right">{inr2(it.entryPrice)}</td>
                  <td className="py-1 pr-3">
                    {it.open
                      ? <span className="rounded bg-sky-500/15 text-sky-600 dark:text-sky-400 px-1.5 py-0.5 text-xs">open</span>
                      : it.exitDate}
                  </td>
                  <td className="py-1 pr-3 text-right">{it.exitPrice != null ? inr2(it.exitPrice) : it.open && lastClose != null ? <span className="text-slate-500">{inr2(lastClose)}</span> : "—"}</td>
                  <td className="py-1 pr-3 text-right">{it.holdingDays}d</td>
                  <td className={`py-1 pr-3 text-right ${tone(it.open ? unreal ?? 0 : it.pnl ?? 0)}`}>
                    {it.open
                      ? unreal != null ? <>{formatInr(unreal)} <span className="text-slate-500">unreal.</span></> : <span className="text-slate-500">open</span>
                      : <>{formatInr(it.pnl ?? 0)} <span className="text-slate-500">({pct(it.pnlPct ?? 0)})</span></>}
                  </td>
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
  const { roundTrips, openPositions } = useMemo(() => pairTrades(analysis.trades), [analysis.trades]);
  const stocks = useMemo(() => bySymbol(roundTrips), [roundTrips]);
  const openBySymbol = useMemo(() => {
    const m = new Map<string, OpenPosition[]>();
    for (const o of openPositions) (m.get(o.symbol) ?? m.set(o.symbol, []).get(o.symbol)!).push(o);
    return m;
  }, [openPositions]);
  const stParams = useMemo(() => stParamsFor(analysis), [analysis]);
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
