import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import OptionTradeBuilder from "../components/trade/OptionTradeBuilder";
import EquityTradeBuilder from "../components/trade/EquityTradeBuilder";
import FibRetPage from "./FibRetPage";

const tabCls = (active: boolean) =>
  `px-3 py-1.5 rounded text-sm font-medium ${active ? "bg-brand text-white" : "text-slate-400 hover:text-slate-200"}`;

export default function TradePage() {
  const [params, setParams] = useSearchParams();
  const top = params.get("tab") === "screener" ? "screener" : "builder";
  const setTop = (t: "builder" | "screener") => {
    const p = new URLSearchParams(params);
    if (t === "screener") p.set("tab", "screener");
    else p.delete("tab");
    setParams(p, { replace: true });
  };
  const [builderTab, setBuilderTab] = useState<"option" | "equity">("option");

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3 flex-wrap">
        <h1 className="text-lg font-semibold">Trade</h1>
        <div className="flex rounded-md bg-slate-800/60 p-0.5">
          {(["builder", "screener"] as const).map((t) => (
            <button key={t} onClick={() => setTop(t)} className={tabCls(top === t)}>
              {t === "builder" ? "Strategy Builder" : "Screener"}
            </button>
          ))}
        </div>
      </div>

      {top === "screener" ? (
        <FibRetPage />
      ) : (
        <div className="space-y-3">
          <div className="flex items-center gap-3 flex-wrap">
            <div className="flex rounded-md bg-slate-800/60 p-0.5">
              {(["option", "equity"] as const).map((t) => (
                <button key={t} onClick={() => setBuilderTab(t)} className={tabCls(builderTab === t)}>
                  {t === "option" ? "Option" : "Equity"}
                </button>
              ))}
            </div>
            <span className="text-xs text-slate-500">
              Build a position, set target / stop-loss, and deploy it live (paper or real). It runs and reports like any deployment on the Live page.
            </span>
          </div>
          {builderTab === "option" ? <OptionTradeBuilder /> : <EquityTradeBuilder />}
        </div>
      )}
    </div>
  );
}
