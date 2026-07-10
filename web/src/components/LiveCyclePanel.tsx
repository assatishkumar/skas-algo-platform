import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { reconstructCycles } from "../lib/optionCycles";
import type { Trade } from "../types";
import { CycleSummary } from "./analysis/OptionsTradeAnalysis";

/** Per-cycle P&L for a live/forward OPTIONS deployment — the same "Cycle P&L" breakdown the
 *  Analysis page shows, rendered on the live card just above Trades & exits so each closed
 *  weekly/monthly cycle's realized P&L is visible without leaving the Live page. Shares the
 *  liveTrades query key with LiveTradesPanel (one fetch); renders nothing until there's ≥1 cycle.
 *  points=[] → the table uses each cycle's entry/exit spot captured at trade time. */
export default function LiveCyclePanel({ runId, version }: { runId: number; version: number }) {
  const { data } = useQuery({
    queryKey: ["liveTrades", runId, version],
    queryFn: () => api.liveTrades(runId),
  });
  const trades: Trade[] = data?.trades ?? [];
  if (trades.length === 0) return null;
  const cycles = reconstructCycles(trades);
  if (cycles.length === 0) return null;
  return (
    <div className="mt-3">
      <CycleSummary cycles={cycles} points={[]} />
    </div>
  );
}
