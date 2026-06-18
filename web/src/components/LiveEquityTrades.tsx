import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { formatInr } from "../lib/format";
import { buildRoundTrips } from "../lib/roundtrips";
import type { RoundTrip, Trade } from "../types";

const tone = (v: number) => (v >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400");
const monthLabel = (ym: string) =>
  new Date(`${ym}-01T00:00:00`).toLocaleDateString("en-IN", { month: "short", year: "numeric" });

/** Weighted-average exit price across a round-trip's exit legs (e.g. a 50%-book + final exit). */
function exitPrice(rt: RoundTrip): number {
  const u = rt.exits.reduce((s, e) => s + e.units, 0);
  return u > 0 ? rt.exits.reduce((s, e) => s + e.price * e.units, 0) / u : 0;
}

/** Realized P&L for an equity live/forward deployment: closed round-trips (entry → exit with P&L)
 *  plus a month-wise realized-P&L summary. The card otherwise only shows OPEN positions. */
export default function LiveEquityTrades({ runId, version }: { runId: number; version: number }) {
  const { data } = useQuery({
    queryKey: ["liveTrades", runId, version],
    queryFn: () => api.liveTrades(runId),
  });
  const trades: Trade[] = data?.trades ?? [];
  const closed = buildRoundTrips(trades);
  if (closed.length === 0) return null;

  const realized = closed.reduce((s, r) => s + r.pnl, 0);
  const wins = closed.filter((r) => r.won).length;

  const byMonth = new Map<string, { pnl: number; n: number }>();
  for (const r of closed) {
    const m = r.exitDate.slice(0, 7); // YYYY-MM
    const cur = byMonth.get(m) ?? { pnl: 0, n: 0 };
    cur.pnl += r.pnl;
    cur.n += 1;
    byMonth.set(m, cur);
  }
  const months = [...byMonth.entries()].sort((a, b) => b[0].localeCompare(a[0]));
  const rows = [...closed].sort((a, b) => b.exitDate.localeCompare(a.exitDate));

  return (
    <div className="mt-3 rounded-md border border-slate-800 bg-slate-900/40 p-3">
      <div className="flex items-center justify-between mb-2 flex-wrap gap-2">
        <div className="text-sm font-medium text-slate-300">
          Exited positions <span className="text-slate-500">({closed.length})</span>
        </div>
        <div className="text-xs text-slate-400">
          win {Math.round((wins / closed.length) * 100)}% · realized{" "}
          <span className={tone(realized)}>{formatInr(realized)}</span>
        </div>
      </div>

      {/* Month-wise realized P&L */}
      <div className="mb-3">
        <div className="text-[11px] text-slate-400 mb-1">Monthly realized P&amp;L</div>
        <div className="flex flex-wrap gap-1.5">
          {months.map(([m, v]) => (
            <span key={m} className="rounded-md bg-slate-800/60 px-2 py-1 text-[11px]">
              <span className="text-slate-400">{monthLabel(m)}</span>{" "}
              <span className={tone(v.pnl)}>{formatInr(v.pnl)}</span>
              <span className="text-slate-500"> · {v.n}</span>
            </span>
          ))}
        </div>
      </div>

      <div className="overflow-x-auto max-h-72 overflow-y-auto">
        <table className="w-full text-xs tabular-nums">
          <thead className="text-slate-400 text-left sticky top-0 bg-slate-900">
            <tr>
              <th className="py-1 pr-3">Exit date</th>
              <th className="py-1 pr-3">Symbol</th>
              <th className="py-1 pr-3 text-right">Qty</th>
              <th className="py-1 pr-3 text-right">Entry</th>
              <th className="py-1 pr-3 text-right">Exit</th>
              <th className="py-1 pr-3 text-right">Hold</th>
              <th className="py-1 pr-3 text-right">P&amp;L</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={`${r.symbol}-${r.exitDate}-${i}`} className="border-t border-slate-800">
                <td className="py-1 pr-3">{r.exitDate}</td>
                <td className="py-1 pr-3 font-medium">{r.symbol}</td>
                <td className="py-1 pr-3 text-right">{r.qty}</td>
                <td className="py-1 pr-3 text-right">{formatInr(r.entryPrice, 2)}</td>
                <td className="py-1 pr-3 text-right">{formatInr(exitPrice(r), 2)}</td>
                <td className="py-1 pr-3 text-right">{r.holdingDays}d</td>
                <td className={`py-1 pr-3 text-right ${tone(r.pnl)}`}>
                  {formatInr(r.pnl)} <span className="text-slate-500">({(r.pnlPct * 100).toFixed(1)}%)</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
