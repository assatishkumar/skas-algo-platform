import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useLocation } from "react-router-dom";
import { api } from "../api/client";
import BacktestFormV2 from "./BacktestFormV2";
import ClassicBacktestForm from "./ClassicBacktestForm";
import { isV2Strategy } from "../lib/backtestV2/registry";

/** New backtest — picks the form the selected strategy deserves.
 *
 *  The nine designed options strategies get the v2 sectioned flow
 *  (design_handoff_backtest_v2); everything else keeps the classic form, unchanged. The
 *  strategy select and its two lists live HERE so switching between the two layouts is
 *  seamless — each form unmounts with its own state, which is exactly what we want (a
 *  half-filled equity form must never leak into an options run). */
export default function NewBacktestPage({ embedded = false }: { embedded?: boolean } = {}) {
  const location = useLocation();
  const clonedId = (location.state as { clonePrefill?: { strategy_id?: string } } | null)
    ?.clonePrefill?.strategy_id;

  const { data: eodData } = useQuery({
    queryKey: ["strategies", "eod"], queryFn: () => api.strategies("eod") });
  const { data: intradayData } = useQuery({
    queryKey: ["strategies", "intraday"], queryFn: () => api.strategies("intraday") });
  const eodList = eodData?.strategies ?? ["sst_lifo"];
  const intradayList = intradayData?.strategies ?? [];
  const strategies = [...eodList, ...intradayList.filter((s) => !eodList.includes(s))];

  // Seed from a clone so the right form mounts immediately (the classic form's own clone
  // effect then re-sets the same id — a harmless no-op).
  const [strategyId, setStrategyId] = useState(clonedId ?? "sst_lifo");

  return isV2Strategy(strategyId) ? (
    <BacktestFormV2 strategyId={strategyId} strategies={strategies}
      onStrategyChange={setStrategyId} />
  ) : (
    <ClassicBacktestForm embedded={embedded} strategyId={strategyId}
      onStrategyChange={setStrategyId} strategies={strategies}
      eodList={eodList} intradayList={intradayList} />
  );
}
