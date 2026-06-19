import { useState } from "react";
import OptionTradeBuilder from "../components/trade/OptionTradeBuilder";
import EquityTradeBuilder from "../components/trade/EquityTradeBuilder";

export default function TradePage() {
  const [tab, setTab] = useState<"option" | "equity">("option");
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-semibold">Trade</h1>
        <div className="flex rounded-md bg-slate-800/60 p-0.5">
          {(["option", "equity"] as const).map((t) => (
            <button key={t} onClick={() => setTab(t)}
              className={`px-3 py-1.5 rounded text-sm font-medium ${tab === t ? "bg-brand text-white" : "text-slate-400 hover:text-slate-200"}`}>
              {t === "option" ? "Option" : "Equity"}
            </button>
          ))}
        </div>
        <span className="text-xs text-slate-500">
          Build a position, set target / stop-loss, and deploy it live (paper or real). It runs and reports like any deployment on the Live page.
        </span>
      </div>
      {tab === "option" ? <OptionTradeBuilder /> : <EquityTradeBuilder />}
    </div>
  );
}
