import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  LineSeries,
  LineStyle,
  PriceScaleMode,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type ISeriesApi,
  type IPriceLine,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
} from "lightweight-charts";
import { api } from "../../api/client";
import { Card } from "../ui";
import { formatInr } from "../../lib/format";
import { pairTrades } from "../../lib/roundtrips";
import type { OpenPosition, RoundTrip, StockSeriesPoint } from "../../types";

/** The stock chart behind an equity trade (owner rework, 2026-09-08).
 *
 *  The previous chart drew ~30 bars whatever range was picked, put a log axis from 40 to 300
 *  under a ₹170 stock, and had no way to move around. This one is TradingView's own charting
 *  library: candles, the SuperTrend line coloured by direction, wheel-zoom and drag-pan on the
 *  time axis, a crosshair legend, and markers that SAY what happened ("BUY 348 @174.25",
 *  "ST↑", "SELL @181.40 +₹2,488"). The SuperTrend can be viewed on any timeframe, not only
 *  the run's, and every trade comes with the plain-language reason it was taken, computed from
 *  the same series: the flip that armed it, the pullback and breakout if the run trades
 *  pullbacks, and the red flip or target that closed it. */

const TODAY = new Date().toISOString().slice(0, 10);
const pct = (v: number) => `${v >= 0 ? "+" : ""}${(v * 100).toFixed(1)}%`;
const tone = (v: number) => (v >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400");
const inr2 = (v: number) => formatInr(v, 2);
const daysBetween = (a: string, b: string) => Math.max(0, Math.round((Date.parse(b) - Date.parse(a)) / 86_400_000));
const fmtDate = (d: string) => new Date(d).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
const shiftDays = (d: string, days: number) => new Date(Date.parse(d) + days * 86_400_000).toISOString().slice(0, 10);

const RANGES: { key: string; label: string; days: number }[] = [
  { key: "6m", label: "6M", days: 183 },
  { key: "1y", label: "1Y", days: 365 },
  { key: "3y", label: "3Y", days: 3 * 365 },
  { key: "5y", label: "5Y", days: 5 * 365 },
  { key: "all", label: "All", days: Infinity },
];
const TIMEFRAMES = ["daily", "weekly", "monthly"] as const;

export interface StockItem {
  entryDate: string; entryPrice: number; qty: number;
  exitDate: string | null; exitPrice: number | null;
  exits: { date: string; price: number; units: number; tag: string }[];
  holdingDays: number; pnl: number | null; pnlPct: number | null; open: boolean;
}

export function toItems(rts: RoundTrip[], opens: OpenPosition[]): StockItem[] {
  return [
    ...rts.map((r) => ({
      entryDate: r.entryDate, entryPrice: r.entryPrice, qty: r.qty, exitDate: r.exitDate,
      exitPrice: r.exits[r.exits.length - 1]?.price ?? null, exits: r.exits,
      holdingDays: r.holdingDays, pnl: r.pnl, pnlPct: r.pnlPct, open: false,
    })),
    ...opens.map((o) => ({
      entryDate: o.entryDate, entryPrice: o.entryPrice, qty: o.qty, exitDate: null, exitPrice: null,
      exits: [], holdingDays: daysBetween(o.entryDate, TODAY), pnl: null, pnlPct: null, open: true,
    })),
  ].sort((a, b) => a.entryDate.localeCompare(b.entryDate));
}

type Bar = StockSeriesPoint & { i: number };

/** The bar at or just before ``date`` (trades stamp the decision day; a holiday falls back). */
function barAt(bars: Bar[], date: string): Bar | null {
  let lo = 0, hi = bars.length - 1, ans: Bar | null = null;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (bars[mid].date <= date) { ans = bars[mid]; lo = mid + 1; } else hi = mid - 1;
  }
  return ans;
}

/** Every bar where the SuperTrend direction changed, with the new direction. */
function flips(bars: Bar[]): { bar: Bar; dir: number }[] {
  const out: { bar: Bar; dir: number }[] = [];
  let prev: number | null = null;
  for (const b of bars) {
    const d = b.direction ?? null;
    if (d != null && prev != null && Math.sign(d) !== Math.sign(prev)) out.push({ bar: b, dir: d });
    if (d != null) prev = d;
  }
  return out;
}

/** The plain-language story of one trade, from the series it was traded on. */
function explain(it: StockItem, bars: Bar[], st: { period: number; multiplier: number; timeframe: string } | null,
                 pullback: boolean, pullbackPct: number, last: Bar | null): string[] {
  const out: string[] = [];
  const entry = barAt(bars, it.entryDate);
  const stName = st ? `SuperTrend(${st.period}, ${st.multiplier}) on ${st.timeframe} bars` : "the signal";
  if (!entry) return [`Entered ${fmtDate(it.entryDate)} at ${inr2(it.entryPrice)} × ${it.qty}.`];
  const allFlips = flips(bars);
  const greenFlip = [...allFlips].reverse().find((f) => f.dir > 0 && f.bar.date <= it.entryDate) ?? null;
  if (greenFlip) {
    const gap = entry.i - greenFlip.bar.i;
    const line = greenFlip.bar.supertrend != null ? ` (close ${inr2(greenFlip.bar.close ?? 0)} above the line at ${inr2(greenFlip.bar.supertrend)})` : "";
    if (pullback) {
      // flip → peak → dip → breakout, on closes, the way the strategy sees it
      const seg = bars.slice(greenFlip.bar.i, entry.i + 1);
      let peak = seg[0], dip: Bar | null = null;
      for (const b of seg.slice(1)) {
        if ((b.close ?? 0) > (peak.close ?? 0) && !dip) peak = b;
        else if (peak.close && (b.close ?? 0) < (dip?.close ?? Infinity) && b.i < entry.i) {
          if ((peak.close - (b.close ?? 0)) / peak.close >= pullbackPct) dip = dip && (dip.close ?? 0) < (b.close ?? 0) ? dip : b;
        }
      }
      out.push(`${stName} turned green on ${fmtDate(greenFlip.bar.date)}${line}, which armed the setup.`);
      if (dip) {
        out.push(`Price ran to ${inr2(peak.close ?? 0)} on ${fmtDate(peak.date)}, pulled back ${(((peak.close ?? 0) - (dip.close ?? 0)) / (peak.close ?? 1) * 100).toFixed(1)}% to ${inr2(dip.close ?? 0)} on ${fmtDate(dip.date)}, then closed back above ${inr2(peak.close ?? 0)} on ${fmtDate(entry.date)} — the breakout that buys.`);
      } else {
        out.push(`Bought on ${fmtDate(entry.date)} at ${inr2(it.entryPrice)} × ${it.qty} (${formatInr(it.entryPrice * it.qty)}).`);
      }
    } else {
      out.push(`${stName} turned green on ${fmtDate(greenFlip.bar.date)}${line}.`);
      out.push(gap === 0
        ? `The daily decision bought the same session at ${inr2(it.entryPrice)} × ${it.qty} (${formatInr(it.entryPrice * it.qty)}).`
        : `The buy went through ${gap} session${gap === 1 ? "" : "s"} later, on ${fmtDate(entry.date)}, at ${inr2(it.entryPrice)} × ${it.qty} (${formatInr(it.entryPrice * it.qty)}) — live SuperTrend reads the previous completed bar, so a flip is acted on the next decision.`);
    }
  } else {
    out.push(`Entered ${fmtDate(it.entryDate)} at ${inr2(it.entryPrice)} × ${it.qty}; no green flip is visible before it on this timeframe — the run may have adopted or been recovered into the position.`);
  }
  for (const e of it.exits) {
    const bar = barAt(bars, e.date);
    const move = (e.price - it.entryPrice) / it.entryPrice;
    if (e.tag === "BOOK") {
      out.push(`${fmtDate(e.date)}: booked ${e.units} at ${inr2(e.price)} (${pct(move)}) — the profit target; the rest rides until the red flip.`);
      continue;
    }
    const redFlip = bar && [...allFlips].reverse().find((f) => f.dir < 0 && f.bar.i <= bar.i && f.bar.i >= entry.i);
    if (redFlip) {
      out.push(`${fmtDate(e.date)}: sold ${e.units} at ${inr2(e.price)} (${pct(move)}) — ${stName} turned red on ${fmtDate(redFlip.bar.date)}${redFlip.bar.supertrend != null ? ` (close ${inr2(redFlip.bar.close ?? 0)} below the line at ${inr2(redFlip.bar.supertrend)})` : ""}.`);
    } else {
      out.push(`${fmtDate(e.date)}: sold ${e.units} at ${inr2(e.price)} (${pct(move)})${move >= 0 ? " — at the target" : ""}.`);
    }
  }
  if (it.open && last) {
    const line = last.supertrend;
    const dist = line != null && last.close ? (last.close - line) / last.close : null;
    out.push(`Still held: ${it.qty} since ${fmtDate(it.entryDate)} (${it.holdingDays}d), now ${inr2(last.close ?? 0)}, ${formatInr((last.close ?? 0) * it.qty - it.entryPrice * it.qty)} unrealised.`
      + (line != null && dist != null
        ? ` The SuperTrend line sits at ${inr2(line)}, ${(Math.abs(dist) * 100).toFixed(1)}% ${dist >= 0 ? "below" : "ABOVE"} price — a close ${dist >= 0 ? "below it flips red and exits" : "below it already flipped red; the exit fires at the next decision"}.`
        : ""));
  }
  return out;
}

function cssVar(el: HTMLElement, name: string, fallback: string): string {
  const v = getComputedStyle(el).getPropertyValue(name).trim();
  return v || fallback;
}

export default function StockChart({ symbol, rts, opens, stParams, pullback, pullbackPct }: {
  symbol: string; rts: RoundTrip[]; opens: OpenPosition[];
  stParams: Record<string, unknown>; pullback: boolean; pullbackPct: number;
}) {
  const runTf = String(stParams.st_timeframe ?? "daily");
  const [timeframe, setTimeframe] = useState<string>(TIMEFRAMES.includes(runTf as never) ? runTf : "daily");
  const [range, setRange] = useState("1y");
  const [logScale, setLogScale] = useState(false);
  const [showFlips, setShowFlips] = useState(true);
  const [focusIdx, setFocusIdx] = useState<number | null>(null);
  const items = useMemo(() => toItems(rts, opens), [rts, opens]);
  const st = stParams.st_period != null
    ? { period: Number(stParams.st_period), multiplier: Number(stParams.st_multiplier), timeframe }
    : null;

  // Six years back, always covering the trades — enough for the range buttons and a weekly or
  // monthly SuperTrend to converge.
  const firstEntry = items[0]?.entryDate ?? TODAY;
  const start = shiftDays(firstEntry < shiftDays(TODAY, -6 * 365) ? firstEntry : TODAY, -6 * 365 - 30);
  const { data, isLoading, error } = useQuery({
    queryKey: ["stSeries", symbol, start, timeframe, st?.period, st?.multiplier],
    queryFn: () => api.stockSeries(symbol, {
      start, end: TODAY,
      ...(st ? { st_period: st.period, st_multiplier: st.multiplier, st_timeframe: timeframe } : {}),
    }),
    staleTime: 5 * 60_000,
  });
  const bars: Bar[] = useMemo(
    () => (data?.points ?? []).filter((p) => p.close != null).map((p, i) => ({ ...p, i })),
    [data],
  );
  const last = bars.length ? bars[bars.length - 1] : null;
  const focus = focusIdx != null ? items[focusIdx] : null;

  // ---- the chart ----
  const boxRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candlesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const segRefs = useRef<ISeriesApi<"Line">[]>([]);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const linesRef = useRef<IPriceLine[]>([]);
  const [legend, setLegend] = useState<Bar | null>(null);

  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const text = cssVar(el, "--muted", "#64748b");
    const grid = cssVar(el, "--divider", "rgba(148,163,184,0.18)");
    const chart = createChart(el, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: text, fontSize: 11 },
      grid: { vertLines: { color: grid }, horzLines: { color: grid } },
      rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.12, bottom: 0.08 } },
      timeScale: { borderVisible: false, rightOffset: 6, minBarSpacing: 2 },
      crosshair: { mode: CrosshairMode.Normal },
      handleScroll: true,
      handleScale: true,
    });
    const candles = chart.addSeries(CandlestickSeries, {
      upColor: "#10b981", downColor: "#f43f5e", borderVisible: false,
      wickUpColor: "#10b981", wickDownColor: "#f43f5e",
      priceLineVisible: false,
    });
    chartRef.current = chart; candlesRef.current = candles;
    markersRef.current = createSeriesMarkers(candles, []);
    return () => { chart.remove(); chartRef.current = null; markersRef.current = null; segRefs.current = []; };
  }, []);

  // data + markers
  useEffect(() => {
    const chart = chartRef.current, candles = candlesRef.current;
    if (!chart || !candles || !bars.length) return;
    candles.setData(bars.map((b) => ({ time: b.date as Time, open: b.open ?? b.close!, high: b.high ?? b.close!, low: b.low ?? b.close!, close: b.close! })));
    // The SuperTrend line: one series per run of one direction, so a flip is a BREAK in the
    // line (the way TradingView's own Supertrend plots it) and never a slanted join from the
    // end of one colour to the start of the next. Whitespace in a single series did not
    // break it — the library bridged the gap.
    for (const sr of segRefs.current) chart.removeSeries(sr);
    segRefs.current = [];
    let seg: { time: Time; value: number }[] = [];
    let segDir = 0;
    const flush = () => {
      if (seg.length) {
        const sr = chart.addSeries(LineSeries, {
          color: segDir > 0 ? "#10b981" : "#f43f5e", lineWidth: 2, lineStyle: LineStyle.Solid,
          priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
        });
        sr.setData(seg);
        segRefs.current.push(sr);
      }
      seg = [];
    };
    for (const b of bars) {
      const d = Math.sign(b.direction ?? 0);
      if (b.supertrend == null || d === 0) { flush(); segDir = 0; continue; }
      if (d !== segDir) { flush(); segDir = d; }
      seg.push({ time: b.date as Time, value: b.supertrend });
    }
    flush();

    // MARKERS say only what happened, in one word, in a colour that is not the candles' or
    // the line's: the library draws marker text in the marker's own colour straight over the
    // bars, so a long green label on green candles vanished (owner, 2026-09-08). The DETAILS
    // go on price lines — a dashed line at the entry with its label on the price axis, the
    // way TradingView shows an average price — for every open position, and for a focused
    // trade its entry and exit. The flip dots stay small and unlabelled unless focused.
    const BUY = "#2563eb", SELL_WIN = "#0284c7", SELL_LOSS = "#dc2626", BOOK = "#d97706";
    const marks: SeriesMarker<Time>[] = [];
    const byDate = new Set(bars.map((b) => b.date));
    const snap = (d: string) => barAt(bars, d)?.date ?? d;
    if (showFlips) {
      for (const f of flips(bars)) {
        marks.push({ time: f.bar.date as Time, position: f.dir > 0 ? "belowBar" : "aboveBar",
          color: f.dir > 0 ? "#10b981" : "#f43f5e", shape: "circle", size: 0.45 });
      }
    }
    for (const it of items) {
      marks.push({ time: snap(it.entryDate) as Time, position: "belowBar", color: BUY, shape: "arrowUp", size: 2, text: "BUY" });
      for (const e of it.exits) {
        const move = (e.price - it.entryPrice) * e.units;
        marks.push({ time: snap(e.date) as Time, position: "aboveBar",
          color: e.tag === "BOOK" ? BOOK : move >= 0 ? SELL_WIN : SELL_LOSS,
          shape: e.tag === "BOOK" ? "square" : "arrowDown", size: e.tag === "BOOK" ? 1.2 : 2,
          text: e.tag === "BOOK" ? "BOOK" : "SELL" });
      }
    }
    marks.sort((a, b) => String(a.time).localeCompare(String(b.time)));
    markersRef.current?.setMarkers(marks.filter((m) => byDate.has(String(m.time))));

    for (const pl of linesRef.current) candles.removePriceLine(pl);
    linesRef.current = [];
    const line = (price: number, color: string, title: string, style = LineStyle.Dashed) =>
      linesRef.current.push(candles.createPriceLine({ price, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title }));
    for (const it of items) {
      if (it.open && last?.close != null) {
        line(it.entryPrice, BUY, `BUY ${it.qty} @${it.entryPrice.toFixed(2)} · ${formatInr((last.close - it.entryPrice) * it.qty)}`);
      }
    }
    setLegend(last);
    const onMove = (param: { time?: Time }) => {
      if (!param.time) { setLegend(last); return; }
      setLegend(barAt(bars, String(param.time)));
    };
    chart.subscribeCrosshairMove(onMove);
    return () => chart.unsubscribeCrosshairMove(onMove);
  }, [bars, items, showFlips, last]);

  // a focused CLOSED trade gets its entry and exit lines while it is focused
  useEffect(() => {
    const candles = candlesRef.current;
    if (!candles || !focus || focus.open) return;
    const made: IPriceLine[] = [
      candles.createPriceLine({ price: focus.entryPrice, color: "#2563eb", lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: `BUY ${focus.qty} @${focus.entryPrice.toFixed(2)}` }),
      ...focus.exits.map((e) => candles.createPriceLine({
        price: e.price, color: e.tag === "BOOK" ? "#d97706" : (e.price >= focus.entryPrice ? "#0284c7" : "#dc2626"),
        lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true,
        title: `${e.tag === "BOOK" ? "BOOK" : "SELL"} ${e.units} @${e.price.toFixed(2)} · ${formatInr((e.price - focus.entryPrice) * e.units)}`,
      })),
    ];
    return () => { for (const pl of made) { try { candles.removePriceLine(pl); } catch { /* chart gone */ } } };
  }, [focus]);

  // visible range: a focused trade, else the range buttons (they set the window; the wheel
  // and drag take it from there — the button is a starting point, not a cage)
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !bars.length) return;
    const lastDate = bars[bars.length - 1].date;
    if (focus) {
      const from = shiftDays(focus.entryDate, -Math.max(20, focus.holdingDays * 0.4));
      const to = shiftDays(focus.exitDate ?? lastDate, Math.max(10, focus.holdingDays * 0.25));
      chart.timeScale().setVisibleRange({ from: from as Time, to: (to > lastDate ? lastDate : to) as Time });
      return;
    }
    const r = RANGES.find((x) => x.key === range)!;
    if (!isFinite(r.days)) chart.timeScale().fitContent();
    else chart.timeScale().setVisibleRange({ from: shiftDays(lastDate, -r.days) as Time, to: lastDate as Time });
  }, [bars, range, focus]);

  useEffect(() => {
    chartRef.current?.priceScale("right").applyOptions({ mode: logScale ? PriceScaleMode.Logarithmic : PriceScaleMode.Normal });
  }, [logScale]);

  const realized = rts.reduce((s, r) => s + r.pnl, 0);
  const unrealized = last?.close != null ? opens.reduce((s, o) => s + (last.close! - o.entryPrice) * o.qty, 0) : null;
  const legendBar = legend ?? last;
  const held = legendBar ? items.find((it) => it.entryDate <= legendBar.date && (it.open || (it.exitDate ?? "") >= legendBar.date)) : null;

  return (
    <Card className="mt-3">
      <div className="flex items-center justify-between mb-2 flex-wrap gap-2">
        <div className="font-medium">
          {symbol}
          {rts.length > 0 && <><span className="text-slate-500 text-sm"> · {rts.length} closed · </span><span className={tone(realized)}>{formatInr(realized)}</span></>}
          {opens.length > 0 && unrealized != null && <span className="text-slate-500 text-sm"> · {opens.length} open · <span className={tone(unrealized)}>{formatInr(unrealized)} unreal.</span></span>}
        </div>
        <div className="flex items-center gap-2 text-xs flex-wrap">
          <div className="flex rounded bg-[var(--field)] p-0.5" title="SuperTrend timeframe — the run's is preselected; the others show the same indicator on longer bars">
            {TIMEFRAMES.map((tf) => (
              <button key={tf} onClick={() => setTimeframe(tf)}
                className={`px-2 py-0.5 rounded capitalize ${timeframe === tf ? "bg-brand text-white" : "text-[var(--muted)] hover:text-[var(--strong)]"}`}>
                {tf}{tf === runTf ? " ·run" : ""}
              </button>
            ))}
          </div>
          <div className="flex rounded bg-[var(--field)] p-0.5">
            {focus && (
              <button onClick={() => setFocusIdx(null)} className="px-2 py-0.5 rounded bg-brand text-white" title="Back to the range">trade ✕</button>
            )}
            {RANGES.map((r) => (
              <button key={r.key} onClick={() => { setRange(r.key); setFocusIdx(null); }}
                className={`px-2 py-0.5 rounded ${!focus && range === r.key ? "bg-brand text-white" : "text-[var(--muted)] hover:text-[var(--strong)]"}`}>{r.label}</button>
            ))}
          </div>
          <button onClick={() => setShowFlips((v) => !v)} className={`px-2 py-0.5 rounded ${showFlips ? "bg-brand text-white" : "bg-[var(--field)] text-[var(--muted)]"}`} title="Mark every SuperTrend flip">flips</button>
          <button onClick={() => setLogScale((v) => !v)} className={`px-2 py-0.5 rounded ${logScale ? "bg-brand text-white" : "bg-[var(--field)] text-[var(--muted)]"}`} title="Logarithmic price axis">log</button>
        </div>
      </div>

      <div className="relative">
        {/* crosshair legend: the bar under the cursor, else the latest */}
        {legendBar && (
          <div className="pointer-events-none absolute left-2 top-1 z-10 rounded bg-[var(--card)]/85 px-2 py-1 text-[11px] tabular-nums text-slate-500 backdrop-blur-sm">
            <span className="font-medium text-[var(--strong)]">{fmtDate(legendBar.date)}</span>
            {" "}O {legendBar.open?.toFixed(2)} H {legendBar.high?.toFixed(2)} L {legendBar.low?.toFixed(2)} C <span className="text-[var(--strong)]">{legendBar.close?.toFixed(2)}</span>
            {legendBar.supertrend != null && (
              <> · ST <span className={(legendBar.direction ?? 0) > 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400"}>
                {legendBar.supertrend.toFixed(2)} {(legendBar.direction ?? 0) > 0 ? "↑ green" : "↓ red"}</span></>
            )}
            {held && legendBar.close != null && (
              <> · held since {held.entryDate} <span className={tone(legendBar.close - held.entryPrice)}>{formatInr((legendBar.close - held.entryPrice) * held.qty)}</span></>
            )}
          </div>
        )}
        <div ref={boxRef} className="h-[420px] w-full" />
        {isLoading && <div className="absolute inset-0 grid place-items-center text-sm text-slate-500">Loading {symbol}…</div>}
        {error && <div className="absolute inset-0 grid place-items-center text-sm text-rose-500">No cached history for {symbol}.</div>}
      </div>
      <div className="mt-1 text-xs text-slate-500">
        Scroll to zoom, drag to pan, hover for the bar. <span className="text-blue-600">▲ BUY</span> · <span className="text-amber-600">■ BOOK</span> · <span className="text-sky-600">▼ SELL</span> (red when it lost) · dashed line = an open position's entry, labelled on the axis · <span className="text-emerald-600 dark:text-emerald-400">— ST↑</span> / <span className="text-rose-600 dark:text-rose-400">— ST↓</span>{showFlips ? " · small dots = every flip" : ""} · click a trade below to frame it with its entry and exit lines.
      </div>

      {/* why: one paragraph per trade, from the series above */}
      {bars.length > 0 && (
        <div className="mt-3 space-y-2">
          {items.map((it, i) => (
            <div key={`${it.entryDate}-${i}`}
              onClick={() => setFocusIdx(focusIdx === i ? null : i)}
              className={`cursor-pointer rounded-md border px-3 py-2 text-[12.5px] leading-relaxed ${focusIdx === i ? "border-brand bg-brand/5" : "border-slate-200 dark:border-slate-800"}`}>
              <div className="flex flex-wrap items-baseline gap-x-3 text-sm">
                <span className="font-medium">{fmtDate(it.entryDate)}{it.open ? " → now" : ` → ${fmtDate(it.exitDate!)}`}</span>
                <span className="text-slate-500">{it.holdingDays}d · {it.qty} × {inr2(it.entryPrice)}</span>
                {it.open
                  ? unrealized != null && last?.close != null && <span className={tone((last.close - it.entryPrice) * it.qty)}>{formatInr((last.close - it.entryPrice) * it.qty)} unrealised</span>
                  : <span className={tone(it.pnl ?? 0)}>{formatInr(it.pnl ?? 0)} ({pct(it.pnlPct ?? 0)})</span>}
              </div>
              <ul className="mt-1 list-disc pl-4 text-slate-500 dark:text-slate-400">
                {explain(it, bars, st, pullback, pullbackPct, last).map((s, j) => <li key={j}>{s}</li>)}
              </ul>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}


/** The SuperTrend settings a chart should overlay for a run of ``strategyId`` — only the
 *  SuperTrend strategies have one; everything else gets bare candles. */
export function stParamsFor(strategyId: string | null | undefined, params: Record<string, unknown> | null | undefined): Record<string, unknown> {
  if (strategyId !== "supertrend_momentum") return {};
  const p = params ?? {};
  return {
    st_period: Number(p.supertrend_period ?? 10),
    st_multiplier: Number(p.supertrend_multiplier ?? 3),
    st_timeframe: String(p.timeframe ?? "daily"),
  };
}

/** The same chart from a LIVE tile: fetch the deployment's own trades, pair them into round
 *  trips and open positions for ``symbol``, and hand them to the chart (owner ask, 2026-09-08:
 *  "make this table clickable and bring the chart with the same details"). */
export function LiveStockChart({ runId, symbol, strategyId, params, version }: {
  runId: number; symbol: string; strategyId: string; params: Record<string, unknown> | undefined; version: number;
}) {
  const { data, isLoading } = useQuery({
    queryKey: ["liveTrades", runId, version],
    queryFn: () => api.liveTrades(runId),
    staleTime: 30_000,
  });
  const { rts, opens } = useMemo(() => {
    const { roundTrips, openPositions } = pairTrades(data?.trades ?? []);
    return { rts: roundTrips.filter((r) => r.symbol === symbol), opens: openPositions.filter((o) => o.symbol === symbol) };
  }, [data, symbol]);
  if (isLoading) return <div className="mt-3 text-sm text-slate-500">Loading {symbol} trades…</div>;
  const p = params ?? {};
  return (
    <StockChart key={symbol} symbol={symbol} rts={rts} opens={opens}
      stParams={stParamsFor(strategyId, p)}
      pullback={String(p.entry_mode ?? "") === "pullback"}
      pullbackPct={Number(p.pullback_pct ?? 0.1) || 0.1} />
  );
}
