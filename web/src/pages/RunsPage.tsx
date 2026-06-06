import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { Badge, Card, ErrorBox, Spinner } from "../components/ui";
import { formatInr, pct } from "../lib/format";

export default function RunsPage() {
  const { data, isLoading, error } = useQuery({ queryKey: ["runs"], queryFn: api.runs });

  if (isLoading) return <Spinner />;
  if (error) return <ErrorBox message={(error as Error).message} />;
  const runs = data ?? [];

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Runs</h1>
        <Link to="/new" className="rounded-md bg-brand hover:bg-brand-light px-3 py-2 text-sm font-medium">
          + New backtest
        </Link>
      </div>

      {runs.length === 0 ? (
        <Card>
          <div className="text-slate-400">
            No runs yet. Start with a <Link to="/new" className="text-brand-light underline">new backtest</Link>.
          </div>
        </Card>
      ) : (
        <div className="grid gap-3">
          {runs.map((r) => (
            <Link key={r.run_id} to={`/runs/${r.run_id}`}>
              <Card className="hover:border-brand transition-colors">
                <div className="flex items-center justify-between">
                  <div>
                    <div className="font-medium">{r.name}</div>
                    <div className="text-xs text-slate-400 mt-0.5">
                      #{r.run_id} · {r.strategy_id} · <Badge>{r.mode}</Badge>
                    </div>
                  </div>
                  <div className="flex gap-6 text-right text-sm">
                    <div>
                      <div className="text-slate-400 text-xs">Return</div>
                      <div className={r.metrics["Total Return %"] >= 0 ? "text-emerald-400" : "text-rose-400"}>
                        {pct(r.metrics["Total Return %"])}
                      </div>
                    </div>
                    <div>
                      <div className="text-slate-400 text-xs">Final equity</div>
                      <div>{formatInr(r.metrics["Final Equity"])}</div>
                    </div>
                    <div>
                      <div className="text-slate-400 text-xs">Trades</div>
                      <div>{r.metrics["Total Trades"]}</div>
                    </div>
                  </div>
                </div>
              </Card>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
