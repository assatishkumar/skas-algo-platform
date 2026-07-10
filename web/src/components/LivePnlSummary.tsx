import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { formatInr } from "../lib/format";
import { reconstructCycles } from "../lib/optionCycles";
import type { LivePosition, Trade } from "../types";

const sign = (v: number) => (v >= 0 ? "text-[var(--pos)]" : "text-[var(--danger)]");

/** Layered P&L for an OPTIONS deployment, so realized-vs-unrealized reads clearly at a glance:
 *  prior cycles (the strategy's P&L EXCLUDING the current cycle) · this cycle's realized · this
 *  cycle's unrealized · overall. Cycles are reconstructed from the trade feed (shares
 *  LiveTradesPanel's query key → one fetch). Renders nothing until there's ≥1 cycle. */
export default function LivePnlSummary({
  runId,
  version,
  positions,
}: {
  runId: number;
  version: number;
  positions: LivePosition[];
}) {
  const { data } = useQuery({
    queryKey: ["liveTrades", runId, version],
    queryFn: () => api.liveTrades(runId),
  });
  const trades: Trade[] = data?.trades ?? [];
  if (trades.length === 0) return null;
  const cycles = reconstructCycles(trades);
  if (cycles.length === 0) return null;

  const open = cycles.find((c) => c.open) ?? null;             // the current (still-open) cycle
  const prior = cycles.filter((c) => !c.open).reduce((s, c) => s + c.realized_pnl, 0);
  const thisRealized = open?.realized_pnl ?? 0;                 // partial closes within this cycle
  const thisUnrealized = positions.reduce((s, p) => s + p.unrealized_pnl, 0);
  const overall = prior + thisRealized + thisUnrealized;

  const Box = ({ label, sub, v }: { label: string; sub?: string; v: number }) => (
    <div className="rounded-[12px] bg-[var(--stat)] px-2.5 py-2">
      <div className="text-[var(--muted)] text-[11px] mb-0.5">
        {label}
        {sub && <span className="text-[var(--faint)]"> · {sub}</span>}
      </div>
      <div className={`font-semibold tabular-nums ${sign(v)}`}>{formatInr(v)}</div>
    </div>
  );

  return (
    <div className="mb-3 grid grid-cols-2 md:grid-cols-4 gap-2 text-sm">
      <Box label="Prior cycles" sub="strategy P&L" v={prior} />
      <Box label="This cycle" sub="realized" v={thisRealized} />
      <Box label="This cycle" sub="unrealized" v={thisUnrealized} />
      <Box label="Overall" v={overall} />
    </div>
  );
}
