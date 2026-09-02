import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import { Card } from "../ui";
import { formatInr } from "../../lib/format";
import LivePayoffChart from "../LivePayoffChart";
import { reconstructCycles, type CycleLeg, type ReconCycle } from "../../lib/optionCycles";
import type { LivePosition, LiveRunSnapshot, RunAnalysis, StockSeriesPoint } from "../../types";

// Underlying → the cached index series used for the spot markers (mirrors options_provider.py).
const INDEX_SYMBOL: Record<string, string> = {
  NIFTY: "NIFTY 50",
  BANKNIFTY: "NIFTY BANK",
  FINNIFTY: "NIFTY FIN SERVICE",
  GOLD: "GOLD",
};

function pad(date: string, days: number): string {
  const d = new Date(date);
  d.setDate(d.getDate() + days);
  return d.toISOString().slice(0, 10);
}

/** Close on `date`, else the nearest prior trading day (points sorted ascending). */
function spotOn(points: StockSeriesPoint[], date?: string): number | null {
  if (!date) return null;
  let best: number | null = null;
  for (const p of points) {
    if (p.close == null) continue;
    if (p.date <= date) best = p.close;
    else break;
  }
  return best;
}

/** Synthesize payoff legs from a cycle. The expiry tent always uses entry premiums (the
 *  structure's actual P&L); the dashed value-curve uses the entry or exit premium. */
function toPositions(legs: CycleLeg[], which: "entry" | "exit"): LivePosition[] {
  return legs.map((l) => ({
    symbol: l.symbol,
    units: l.units,
    lots: 0,
    direction: l.side === "long" ? 1 : -1,
    avg_price: l.entry_premium,
    ltp: which === "exit" ? l.exit_price ?? null : l.entry_premium,
    unrealized_pnl: 0,
  }));
}

function netPremium(legs: CycleLeg[]): number {
  // Positive = net credit received; negative = net debit paid.
  return legs.reduce((s, l) => s + (l.side === "short" ? 1 : -1) * l.entry_premium * l.units, 0);
}

function LegsTable({ legs }: { legs: CycleLeg[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm tabular-nums">
        <thead>
          <tr className="text-slate-400 text-xs border-b border-slate-800">
            <th className="text-left font-medium py-1.5">Leg</th>
            <th className="text-left font-medium py-1.5">Side</th>
            <th className="text-right font-medium py-1.5">Qty</th>
            <th className="text-right font-medium py-1.5">Entry ₹</th>
            <th className="text-right font-medium py-1.5">Exit ₹</th>
            <th className="text-right font-medium py-1.5">P&L</th>
          </tr>
        </thead>
        <tbody>
          {legs.map((l) => (
            <tr key={l.symbol} className="border-b border-slate-800/40">
              <td className="py-1.5 font-medium">
                {l.strike} {l.right}
              </td>
              <td className="py-1.5">
                <span className={l.side === "long" ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400"}>
                  {l.side === "long" ? "Long" : "Short"}
                </span>
              </td>
              <td className="py-1.5 text-right">{l.units}</td>
              <td className="py-1.5 text-right">{l.entry_premium.toFixed(2)}</td>
              <td className="py-1.5 text-right">{l.exit_price != null ? l.exit_price.toFixed(2) : <span className="text-slate-500">open</span>}</td>
              <td className={`py-1.5 text-right ${(l.realized_pnl ?? 0) >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400"}`}>
                {l.realized_pnl != null ? formatInr(l.realized_pnl) : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CycleCard({ cycle, points, live, defaultOpen = false }: {
  cycle: ReconCycle; points: StockSeriesPoint[]; live?: LiveRunSnapshot | null;
  defaultOpen?: boolean;
}) {
  // COLLAPSED by default: an intraday run has 500+ cycles, and mounting 2-3 payoff SVGs
  // per cycle froze the whole Analyze page (owner, 2026-07-17). The legs table + charts
  // mount only when a cycle is expanded — the header/summary row stays cheap.
  const [expanded, setExpanded] = useState(defaultOpen);
  // Prefer the spot captured AT TRADE TIME (immune to a lagging bhavcopy cache); then, for an open
  // cycle, the broker's live spot; finally the cached index close on that date.
  const entrySpot = cycle.entry_spot ?? (cycle.open ? live?.underlying_spot ?? null : null) ?? spotOn(points, cycle.entry_date);
  const exitSpot = cycle.exit_spot ?? spotOn(points, cycle.exit_date);
  const net = netPremium(cycle.legs);
  const liveOpen = cycle.open && !!live?.positions?.length;

  const entryChart = (
    <LivePayoffChart
      positions={toPositions(cycle.legs, "entry")}
      spot={entrySpot}
      asOf={cycle.entry_date}
      spotLabel="entry spot"
      caption={
        <>
          Payoff at entry ({cycle.entry_date}){" "}
          <span className="text-slate-500">— green/red = P&L if held to expiry; dashed = value at entry; line = spot at entry</span>
        </>
      }
    />
  );

  return (
    <Card className="space-y-3">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full text-left cursor-pointer"
        title={expanded ? "Collapse" : "Expand — legs + entry/exit payoff charts"}
      >
        <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1">
          <div>
            <span className="text-slate-500 mr-1.5 text-xs">{expanded ? "▾" : "▸"}</span>
            <span className="font-medium text-slate-200">
              {cycle.underlying} · {cycle.legs.length} legs · exp {cycle.expiry}
            </span>
            <span
              className={`ml-2 rounded px-1.5 py-0.5 text-[11px] ${
                cycle.open
                  ? "bg-sky-900/50 text-sky-300"
                  : "bg-slate-800 text-slate-400"
              }`}
            >
              {cycle.open ? "OPEN" : "CLOSED"}
            </span>
          </div>
          <div className="text-xs text-slate-400">
            Entered {cycle.entry_date}
            {cycle.exit_date ? ` · exited ${cycle.exit_date} (${cycle.holding_days}d)` : ""}
            {cycle.exit_reason ? ` · ${cycle.exit_reason}` : ""}
          </div>
        </div>

        <div className="mt-2 flex flex-wrap gap-x-8 gap-y-1 text-sm">
          <div>
            <span className="text-slate-400 text-xs mr-2">Net {net >= 0 ? "credit" : "debit"}</span>
            <span className="tabular-nums font-medium">{formatInr(Math.abs(net))}</span>
          </div>
          {!cycle.open && (
            <div>
              <span className="text-slate-400 text-xs mr-2">Realized P&L</span>
              <span className={`tabular-nums font-medium ${cycle.realized_pnl >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400"}`}>
                {formatInr(cycle.realized_pnl)}
              </span>
            </div>
          )}
          {!expanded && (
            <span className="text-xs text-slate-500 self-center">click for legs + payoff charts</span>
          )}
        </div>
      </button>

      {!expanded ? null : <>

      <LegsTable legs={cycle.legs} />

      {cycle.open ? (
        liveOpen ? (
          <>
            <LivePayoffChart
              positions={live!.positions}
              spot={live!.underlying_spot}
              spotLabel="live spot"
              caption={
                <>
                  Payoff now — live{" "}
                  <span className="text-slate-500">
                    — green/red = P&L if held to expiry; dashed = current value; line = live spot
                    {live!.underlying_spot != null ? ` (${Math.round(live!.underlying_spot)})` : ""}
                  </span>
                </>
              }
            />
            <div className="text-xs text-slate-500">
              Open — spot, leg LTPs and current value are live from the broker. The exit-side payoff
              appears once the legs are closed.
            </div>
          </>
        ) : (
          <>
            {entryChart}
            <div className="text-xs text-slate-500">
              Still open — exit-side payoff appears once the legs are closed. For the live value,
              greeks and current spot, see the <Link to="/live" className="underline">Live</Link> tab.
            </div>
          </>
        )
      ) : (
        <div className="grid md:grid-cols-2 gap-4">
          {entryChart}
          <LivePayoffChart
            positions={toPositions(cycle.legs, "exit")}
            spot={exitSpot}
            asOf={cycle.exit_date}
            spotLabel="exit spot"
            caption={
              <>
                Payoff at exit ({cycle.exit_date}){" "}
                <span className="text-slate-500">— green/red = P&L if held to expiry; dashed = value at exit; line = spot at exit</span>
              </>
            }
          />
        </div>
      )}
      </>}
    </Card>
  );
}

type SumKey = "entered" | "exited" | "held" | "espot" | "xspot" | "move" | "net" | "pnl" | "legs" | "result";

/** Compact, sortable P&L table across all cycles (one row per weekly/monthly position) + aggregates.
 * Exported so the Live page can show the same per-cycle breakdown on a running deployment (pass
 * points=[] there — the table falls back to each cycle's entry/exit spot captured at trade time). */
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/** The weekday a cycle was entered. Written as "2026-09-01 09:18", which Safari refuses to
 * parse — the space has to become a T. */
function weekdayOf(stamp: string | null | undefined): number | null {
  if (!stamp) return null;
  const d = new Date(String(stamp).replace(" ", "T"));
  return Number.isNaN(d.getTime()) ? null : d.getDay();
}

/** How many cycles fit on screen before the table stops being readable and starts being a
 * scroll. Intraday books reach hundreds of rows within months. */
const PAGE_SIZE = 10;

export function CycleSummary({ cycles, points, runId }: {
  cycles: ReconCycle[]; points: StockSeriesPoint[]; runId?: number;
}) {
  const [sortKey, setSortKey] = useState<SumKey>("entered");
  const [dir, setDir] = useState<1 | -1>(-1);
  const [page, setPage] = useState(0);
  const rows = cycles.map((c) => {
    const eSpot = c.entry_spot ?? spotOn(points, c.entry_date);
    const xSpot = c.exit_spot ?? spotOn(points, c.exit_date);
    return {
      c, eSpot, xSpot,
      move: eSpot && xSpot ? ((xSpot - eSpot) / eSpot) * 100 : null,
      net: netPremium(c.legs),
      pnl: c.realized_pnl,
      held: c.holding_days ?? null,
      legs: c.legs.length,
      result: c.open ? "open" : c.realized_pnl >= 0 ? "win" : "loss",
    };
  });
  const val = (r: (typeof rows)[number]): number | string | null => ({
    entered: r.c.entry_date, exited: r.c.exit_date ?? "", held: r.held, espot: r.eSpot, xspot: r.xSpot,
    move: r.move, net: r.net, pnl: r.pnl, legs: r.legs, result: r.result,
  }[sortKey]);
  const sorted = [...rows].sort((a, b) => {
    const va = val(a), vb = val(b);
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    if (typeof va === "string" || typeof vb === "string") return String(va).localeCompare(String(vb)) * dir;
    return (va - vb) * dir;
  });
  // Re-sorting reorders everything, so staying on page 4 would land you somewhere arbitrary.
  const onSort = (k: SumKey) => {
    setPage(0);
    if (k === sortKey) setDir((d) => (d === 1 ? -1 : 1));
    else { setSortKey(k); setDir(-1); }
  };
  const pages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
  const clamped = Math.min(page, pages - 1);
  const shown = sorted.slice(clamped * PAGE_SIZE, clamped * PAGE_SIZE + PAGE_SIZE);

  const closed = cycles.filter((c) => !c.open);
  const wins = closed.filter((c) => c.realized_pnl > 0).length;
  const total = closed.reduce((s, c) => s + c.realized_pnl, 0);
  const pnls = closed.map((c) => c.realized_pnl);
  const avg = closed.length ? total / closed.length : 0;
  const winRate = closed.length ? (wins / closed.length) * 100 : 0;

  // Per-weekday performance. A strategy that enters every session either has a weekday it
  // is bad at or it does not, and a flat list of a hundred rows will never tell you which.
  const byDay = WEEKDAYS.map((name, idx) => {
    const day = closed.filter((c) => weekdayOf(c.entry_date) === idx);
    const dayWins = day.filter((c) => c.realized_pnl > 0).length;
    const dayTotal = day.reduce((s2, c) => s2 + c.realized_pnl, 0);
    return {
      name, idx, n: day.length, wins: dayWins, total: dayTotal,
      avg: day.length ? dayTotal / day.length : 0,
      winRate: day.length ? (dayWins / day.length) * 100 : 0,
    };
  }).filter((d) => d.n > 0);
  const worstDay = byDay.length
    ? byDay.reduce((a, b) => (b.total < a.total ? b : a)) : null;

  const Th = ({ k, label, right }: { k: SumKey; label: string; right?: boolean }) => (
    <th onClick={() => onSort(k)}
      className={`py-1.5 pr-3 cursor-pointer select-none hover:text-slate-200 ${right ? "text-right" : "text-left"} ${sortKey === k ? "text-slate-200" : ""}`}>
      {label}{sortKey === k ? (dir === 1 ? " ↑" : " ↓") : ""}
    </th>
  );
  const pos = (v: number) => (v >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400");

  return (
    <Card className="space-y-3">
      <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1">
        <span className="font-medium text-slate-200">Cycle P&L — {cycles.length} cycles</span>
        <span className="text-xs text-slate-400">
          {closed.length} closed · win rate <span className="text-slate-200">{winRate.toFixed(0)}%</span> ({wins}/{closed.length})
        </span>
        <span className="text-xs text-slate-400">total <span className={pos(total)}>{formatInr(total)}</span></span>
        <span className="text-xs text-slate-400">avg/cycle <span className={pos(avg)}>{formatInr(avg)}</span></span>
        {pnls.length > 0 && (
          <span className="text-xs text-slate-400">
            best <span className={pos(Math.max(...pnls))}>{formatInr(Math.max(...pnls))}</span> ·
            worst <span className={pos(Math.min(...pnls))}>{formatInr(Math.min(...pnls))}</span>
          </span>
        )}
      </div>
      {byDay.length > 1 && (
        <div>
          <div className="mb-1.5 text-xs text-slate-400">
            By weekday entered
            {worstDay && worstDay.total < 0 && (
              <span className="ml-2 text-rose-400">
                · {worstDay.name} is the only losing day ({formatInr(worstDay.total)} over{" "}
                {worstDay.n})
              </span>
            )}
          </div>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
            {byDay.map((d) => (
              <div key={d.idx} className="rounded-lg border border-slate-800 px-3 py-2">
                <div className="flex items-baseline justify-between">
                  <span className="text-xs font-semibold text-slate-300">{d.name}</span>
                  <span className="text-[11px] text-slate-500">{d.n}</span>
                </div>
                <div className={`text-sm font-medium ${pos(d.total)}`}>{formatInr(d.total)}</div>
                <div className="text-[11px] text-slate-500">
                  {d.winRate.toFixed(0)}% win · avg{" "}
                  <span className={pos(d.avg)}>{formatInr(d.avg)}</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="overflow-x-auto">
        <table className="w-full text-sm tabular-nums">
          <thead className="text-slate-400 text-xs border-b border-slate-800">
            <tr>
              <Th k="entered" label="Entered" />
              <Th k="exited" label="Exited" />
              <Th k="held" label="Held" right />
              <Th k="espot" label="Entry spot" right />
              <Th k="xspot" label="Exit spot" right />
              <Th k="move" label="Move %" right />
              <Th k="net" label="Net" right />
              <Th k="pnl" label="Realized P&L" right />
              <Th k="legs" label="Legs" right />
              <Th k="result" label="Result" />
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => (
              <tr key={`${r.c.underlying}-${r.c.expiry}-${r.c.entry_date}`} className="border-b border-slate-800/40">
                <td className="py-1.5 pr-3">
                  {runId != null ? (
                    <Link to={`/runs/${runId}/cycle/${cycles.indexOf(r.c)}`}
                      className="text-brand hover:underline" title="Open the full cycle lifecycle view">
                      {r.c.entry_date} ↗
                    </Link>
                  ) : r.c.entry_date}
                  {weekdayOf(r.c.entry_date) != null && (
                    <span className="ml-1.5 text-[11px] text-slate-500">
                      {WEEKDAYS[weekdayOf(r.c.entry_date) as number]}
                    </span>
                  )}
                </td>
                <td className="py-1.5 pr-3">{r.c.exit_date ?? <span className="text-slate-500">open</span>}</td>
                <td className="py-1.5 pr-3 text-right">{r.held != null ? `${r.held}d` : "—"}</td>
                <td className="py-1.5 pr-3 text-right">{r.eSpot != null ? Math.round(r.eSpot) : "—"}</td>
                <td className="py-1.5 pr-3 text-right">{r.xSpot != null ? Math.round(r.xSpot) : "—"}</td>
                <td className={`py-1.5 pr-3 text-right ${r.move != null ? pos(r.move) : ""}`}>
                  {r.move != null ? `${r.move >= 0 ? "+" : ""}${r.move.toFixed(1)}%` : "—"}
                </td>
                <td className="py-1.5 pr-3 text-right">{r.net >= 0 ? "+" : "−"}{formatInr(Math.abs(r.net))}</td>
                <td className={`py-1.5 pr-3 text-right ${pos(r.pnl)}`}>{r.c.open ? "—" : formatInr(r.pnl)}</td>
                <td className="py-1.5 pr-3 text-right">{r.legs}</td>
                <td className="py-1.5 pr-3">
                  <span className={`rounded px-1.5 py-0.5 text-[11px] ${
                    r.result === "open" ? "bg-sky-900/50 text-sky-300"
                      : r.result === "win" ? "bg-emerald-900/40 text-emerald-300" : "bg-rose-900/40 text-rose-300"}`}>
                    {r.result}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {pages > 1 && (
        <div className="flex items-center justify-between text-xs text-slate-400">
          <span>
            {clamped * PAGE_SIZE + 1}–{Math.min((clamped + 1) * PAGE_SIZE, sorted.length)} of{" "}
            {sorted.length}
          </span>
          <div className="flex items-center gap-1">
            <button
              onClick={() => setPage(0)} disabled={clamped === 0}
              className="rounded px-2 py-1 hover:bg-slate-800 disabled:opacity-30"
            >
              « First
            </button>
            <button
              onClick={() => setPage((p) => Math.max(0, p - 1))} disabled={clamped === 0}
              className="rounded px-2 py-1 hover:bg-slate-800 disabled:opacity-30"
            >
              ‹ Prev
            </button>
            <span className="px-2 text-slate-300">{clamped + 1} / {pages}</span>
            <button
              onClick={() => setPage((p) => Math.min(pages - 1, p + 1))}
              disabled={clamped >= pages - 1}
              className="rounded px-2 py-1 hover:bg-slate-800 disabled:opacity-30"
            >
              Next ›
            </button>
            <button
              onClick={() => setPage(pages - 1)} disabled={clamped >= pages - 1}
              className="rounded px-2 py-1 hover:bg-slate-800 disabled:opacity-30"
            >
              Last »
            </button>
          </div>
        </div>
      )}
    </Card>
  );
}

export default function OptionsTradeAnalysis({ analysis }: { analysis: RunAnalysis }) {
  const cycles = useMemo(() => reconstructCycles(analysis.trades), [analysis.trades]);

  const underlying = cycles[0]?.underlying ?? "NIFTY";
  const idxSymbol = INDEX_SYMBOL[underlying] ?? underlying;
  const dates = cycles
    .flatMap((c) => [c.entry_date, c.exit_date])
    .filter((d): d is string => !!d)
    .sort();
  const start = dates.length ? pad(dates[0], -10) : undefined;
  const end = dates.length ? pad(dates[dates.length - 1], 2) : undefined;

  const { data: series } = useQuery({
    queryKey: ["optAnalysisSpot", idxSymbol, start, end],
    queryFn: () => api.stockSeries(idxSymbol, { start, end }),
    enabled: cycles.length > 0,
  });
  const points = series?.points ?? [];

  // For an active run with an open cycle, pull the live broker spot + leg LTPs from the running
  // session (404s for stopped/backtest runs → falls back to the cached index close).
  const hasOpen = cycles.some((c) => c.open);
  const { data: live } = useQuery({
    queryKey: ["optAnalysisLive", analysis.run_id],
    queryFn: () => api.liveSnapshot(analysis.run_id),
    enabled: hasOpen,
    retry: false,
    refetchInterval: 30000,
  });

  if (!cycles.length) {
    return (
      <Card>
        <div className="text-slate-300 font-medium mb-1">Options trade analysis</div>
        <div className="text-sm text-slate-400">
          No option positions found for this run yet.
        </div>
      </Card>
    );
  }

  const open = cycles.filter((c) => c.open).length;
  const closed = cycles.length - open;

  return (
    <div className="space-y-4">
      <div className="text-sm text-slate-400">
        {cycles.length} option {cycles.length === 1 ? "cycle" : "cycles"}
        {open ? ` · ${open} open` : ""}
        {closed ? ` · ${closed} closed` : ""} — click a cycle to expand its legs and the
        entry{closed ? "/exit" : ""} payoff charts.
      </div>
      <CycleSummary cycles={cycles} points={points} />
      {cycles.map((c) => (
        <CycleCard key={`${c.underlying}-${c.expiry}-${c.entry_date}`} cycle={c} points={points}
          live={c.open ? live : null}
          // Open cycles (the ones being watched) and tiny runs auto-expand; a 500-cycle
          // intraday run starts fully collapsed — that page froze mounting 1,000+ SVGs.
          defaultOpen={c.open || cycles.length <= 2} />
      ))}
    </div>
  );
}
