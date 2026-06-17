import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { formatInr } from "../lib/format";
import type { Trade } from "../types";

const EXITS = new Set(["SELL", "COVER", "SETTLE"]);
const tone = (v: number) => (v >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400");

/** Executed trades for a live/forward deployment — entry legs + exits with per-leg P&L and the
 *  exit reason. Stays visible after a position closes, so a booked cycle still shows what was
 *  traded, when it exited, and the realized P&L (the live card otherwise goes blank when flat). */
export default function LiveTradesPanel({ runId, version }: { runId: number; version: number }) {
  const { data } = useQuery({
    queryKey: ["liveTrades", runId, version],
    queryFn: () => api.liveTrades(runId),
  });
  const trades: Trade[] = (data?.trades ?? []).slice().sort((a, b) => a.date.localeCompare(b.date));
  if (trades.length === 0) return null;

  const exits = trades.filter((t) => EXITS.has(t.action));
  const realized = exits.reduce((s, t) => s + (t.profit ?? 0), 0);
  const lastExit = exits.length ? exits[exits.length - 1] : null;
  const lastReason = lastExit?.exit_reason;

  return (
    <div className="mt-3 rounded-md border border-slate-800 bg-slate-900/40 p-3">
      <div className="flex items-center justify-between mb-2 flex-wrap gap-2">
        <div className="text-sm font-medium text-slate-300">Trades &amp; exits</div>
        {lastExit && (
          <div className="text-xs text-slate-400">
            Last exit <span className="text-slate-200">{lastExit.date}</span>
            {lastReason && (
              <span className="ml-1 rounded-full bg-slate-800 border border-slate-700 px-1.5 py-0.5 text-[10px] uppercase">
                {lastReason}
              </span>
            )}
            <span className="ml-2">realized </span>
            <span className={tone(realized)}>{formatInr(realized)}</span>
          </div>
        )}
      </div>
      <div className="overflow-x-auto max-h-72 overflow-y-auto">
        <table className="w-full text-xs tabular-nums">
          <thead className="text-slate-400 text-left sticky top-0 bg-slate-900">
            <tr>
              <th className="py-1 pr-3">Date</th>
              <th className="py-1 pr-3">Leg</th>
              <th className="py-1 pr-3">Action</th>
              <th className="py-1 pr-3 text-right">Units</th>
              <th className="py-1 pr-3 text-right">Price</th>
              <th className="py-1 pr-3 text-right">P&amp;L</th>
              <th className="py-1 pr-3">Reason</th>
            </tr>
          </thead>
          <tbody>
            {trades.map((t, i) => {
              const isExit = EXITS.has(t.action);
              return (
                <tr key={i} className="border-t border-slate-800">
                  <td className="py-1 pr-3">{t.date}</td>
                  <td className="py-1 pr-3 font-mono">{t.ticker}</td>
                  <td className={`py-1 pr-3 ${isExit ? "text-rose-700 dark:text-rose-300" : "text-emerald-700 dark:text-emerald-300"}`}>{t.action}</td>
                  <td className="py-1 pr-3 text-right">{t.units}</td>
                  <td className="py-1 pr-3 text-right">{formatInr(t.price, 2)}</td>
                  <td className={`py-1 pr-3 text-right ${isExit ? tone(t.profit ?? 0) : "text-slate-500"}`}>
                    {isExit ? formatInr(t.profit ?? 0) : "—"}
                  </td>
                  <td className="py-1 pr-3 text-slate-400">
                    {t.exit_reason ?? ""}{t.holding_days != null ? ` · ${t.holding_days}d` : ""}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
