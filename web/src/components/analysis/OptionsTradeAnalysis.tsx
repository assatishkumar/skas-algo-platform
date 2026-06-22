import { useMemo } from "react";
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

function CycleCard({ cycle, points, live }: { cycle: ReconCycle; points: StockSeriesPoint[]; live?: LiveRunSnapshot | null }) {
  // For an OPEN cycle prefer the broker's LIVE spot (and live leg LTPs); fall back to the cached
  // index close (which lags bhavcopy for a just-entered cycle). Closed cycles use the cache only.
  const entrySpot = (cycle.open ? live?.underlying_spot ?? null : null) ?? spotOn(points, cycle.entry_date);
  const exitSpot = spotOn(points, cycle.exit_date);
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
      <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1">
        <div>
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

      <div className="flex flex-wrap gap-x-8 gap-y-1 text-sm">
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
      </div>

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
        {closed ? ` · ${closed} closed` : ""} — each shows all legs and the payoff at entry
        {closed ? " and at exit" : ""}.
      </div>
      {cycles.map((c) => (
        <CycleCard key={`${c.expiry}-${c.entry_date}`} cycle={c} points={points} live={c.open ? live : null} />
      ))}
    </div>
  );
}
