import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import ReportView from "../components/ReportView";
import { ErrorBox, Spinner } from "../components/ui";
import type { ForwardTestPrefill } from "../types";

export default function RunDetailPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const runId = Number(id);
  const { data, isLoading, error } = useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.run(runId),
    enabled: Number.isFinite(runId),
  });

  if (isLoading) return <Spinner />;
  if (error) return <ErrorBox message={(error as Error).message} />;
  if (!data) return null;

  function forwardTest() {
    const prefill: ForwardTestPrefill = {
      strategy_id: data!.strategy_id,
      name: data!.name,
      capital: data!.capital,
      params: data!.params,
    };
    // Carry the backtest's exact config into the Live "start" form.
    navigate("/live", { state: { prefill } });
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <Link to="/" className="text-slate-400 hover:text-slate-200 text-sm">
          ← Runs
        </Link>
        <h1 className="text-lg font-semibold">
          Run #{runId} · {data.strategy_id}
        </h1>
        <button
          onClick={forwardTest}
          className="ml-auto rounded-md bg-brand hover:bg-brand-light px-3 py-1.5 text-sm font-medium"
        >
          Forward-test →
        </button>
      </div>
      <ReportView report={data.report} trades={data.trades} csvUrl={api.tradesCsvUrl(runId)} />
    </div>
  );
}
