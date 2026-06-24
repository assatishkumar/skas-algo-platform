import { Link, useSearchParams } from "react-router-dom";
import NewBacktestPage from "./NewBacktestPage";
import RunsPage from "./RunsPage";
import { Segmented } from "../components/redesign";

/** Backtest hub — a per-strategy leaderboard of runs, or the New-backtest config. */
export default function BacktestPage() {
  const [params, setParams] = useSearchParams();
  const tab: "runs" | "new" = params.get("tab") === "new" ? "new" : "runs";
  const setTab = (t: "runs" | "new") => {
    const p = new URLSearchParams(params);
    if (t === "new") p.set("tab", "new");
    else p.delete("tab");
    setParams(p, { replace: true });
  };

  return (
    <div className="font-['Manrope'] bg-[var(--page)] min-h-[calc(100vh-3.5rem)] text-[var(--strong)]">
      <div className="max-w-[1240px] mx-auto px-8 pt-[30px] pb-16 space-y-4">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div className="flex items-center gap-3 flex-wrap">
            <h1 className="text-[27px] font-bold font-['Space_Grotesk']">Backtest</h1>
            <Segmented value={tab} onChange={setTab} options={[{ value: "runs", label: "Runs" }, { value: "new", label: "New backtest" }]} />
          </div>
          {tab === "runs" && (
            <Link to="/compare" className="rounded-[11px] border border-[var(--border)] bg-[var(--card)] text-[var(--strong)] px-3 py-2 text-sm font-medium hover:bg-[var(--row-hover)]">
              ≡ Compare
            </Link>
          )}
        </div>
        {tab === "new" ? <NewBacktestPage embedded /> : <RunsPage embedded />}
      </div>
    </div>
  );
}
