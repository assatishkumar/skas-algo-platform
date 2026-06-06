import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import ReportView from "../components/ReportView";
import { ErrorBox, Spinner } from "../components/ui";

export default function RunDetailPage() {
  const { id } = useParams();
  const runId = Number(id);
  const { data, isLoading, error } = useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.run(runId),
    enabled: Number.isFinite(runId),
  });

  if (isLoading) return <Spinner />;
  if (error) return <ErrorBox message={(error as Error).message} />;
  if (!data) return null;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <Link to="/" className="text-slate-400 hover:text-slate-200 text-sm">
          ← Runs
        </Link>
        <h1 className="text-lg font-semibold">
          Run #{runId} · {data.strategy_id}
        </h1>
      </div>
      <ReportView report={data.report} trades={data.trades} csvUrl={api.tradesCsvUrl(runId)} />
    </div>
  );
}
