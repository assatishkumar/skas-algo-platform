import { useSearchParams } from "react-router-dom";
import NewBacktestPage from "./NewBacktestPage";
import RunsPage from "./RunsPage";

/** Backtest hub — view existing runs or deploy a new backtest. Wraps the existing Runs and
 *  New-backtest pages (rendered embedded so they don't repeat their own page titles). */
export default function BacktestPage() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") === "new" ? "new" : "runs";
  const setTab = (t: "runs" | "new") => {
    const p = new URLSearchParams(params);
    if (t === "new") p.set("tab", "new");
    else p.delete("tab");
    setParams(p, { replace: true });
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-semibold">Backtest</h1>
        <div className="flex rounded-md bg-slate-800/60 p-0.5">
          {(["runs", "new"] as const).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`px-3 py-1.5 rounded text-sm font-medium ${tab === t ? "bg-brand text-white" : "text-slate-400 hover:text-slate-200"}`}
            >
              {t === "runs" ? "Runs" : "New backtest"}
            </button>
          ))}
        </div>
      </div>
      {tab === "new" ? <NewBacktestPage embedded /> : <RunsPage embedded />}
    </div>
  );
}
