/** Drill-in §7 — Trade ledger: the per-cycle table + payoff cards the old Analyze page
 *  was built around, kept as a section (owner decision 2026-07-28). */

import { useQuery } from "@tanstack/react-query";
import { api } from "../../../api/client";
import OptionsTradeAnalysis from "../../analysis/OptionsTradeAnalysis";

export default function LedgerSection({ runId }: { runId: number }) {
  const { data, isLoading } = useQuery({
    queryKey: ["runAnalysis", runId],
    queryFn: () => api.runAnalysis(runId),
  });
  if (isLoading || !data) {
    return <div className="text-sm text-[var(--muted)] font-semibold p-6">Loading trades…</div>;
  }
  return <OptionsTradeAnalysis analysis={data} />;
}
