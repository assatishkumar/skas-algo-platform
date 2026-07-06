import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import OptionTradeBuilder from "../components/trade/OptionTradeBuilder";
import EquityTradeBuilder from "../components/trade/EquityTradeBuilder";
import CpRatioExpiryBuilder from "../components/trade/CpRatioExpiryBuilder";
import DeltaNeutralBuilder from "../components/trade/DeltaNeutralBuilder";
import MomentumThetaBuilder from "../components/trade/MomentumThetaBuilder";
import FibRetPage from "./FibRetPage";
import DonchianStranglePage from "./DonchianStranglePage";
import { Segmented } from "../components/redesign";

const SCREENERS = [
  { id: "fibret", label: "FibRet" },
  { id: "donchian", label: "Donchian Strangle (monthly)" },
  { id: "iv_crush", label: "IV Crush" },
  { id: "momentum", label: "Momentum Breakout" },
];

export default function TradePage() {
  const [params, setParams] = useSearchParams();
  const top: "builder" | "screener" = params.get("tab") === "screener" ? "screener" : "builder";
  const setTop = (t: "builder" | "screener") => {
    const p = new URLSearchParams(params);
    if (t === "screener") p.set("tab", "screener");
    else p.delete("tab");
    setParams(p, { replace: true });
  };
  const [builderTab, setBuilderTab] = useState<"option" | "equity" | "intraday" | "cpre" | "dnm">("option");
  const [screener, setScreener] = useState("fibret");

  return (
    <div className="font-['Manrope'] bg-[var(--page)] min-h-[calc(100vh-3.5rem)] text-[var(--strong)]">
      <div className="max-w-[1240px] mx-auto px-8 pt-[30px] pb-16 space-y-4">
        <div className="flex items-center gap-3 flex-wrap">
          <h1 className="text-[27px] font-bold font-['Space_Grotesk']">Trade</h1>
          <Segmented value={top} onChange={setTop} options={[{ value: "builder", label: "Strategy Builder" }, { value: "screener", label: "Screener" }]} />
        </div>

        {top === "builder" ? (
          <div className="space-y-3">
            <div className="flex items-center gap-3 flex-wrap">
              <Segmented value={builderTab} onChange={setBuilderTab} options={[{ value: "option", label: "Option" }, { value: "equity", label: "Equity" }, { value: "intraday", label: "Intraday theta" }, { value: "cpre", label: "CP ratio expiry" }, { value: "dnm", label: "Delta neutral" }]} />
              <span className="text-sm text-[var(--muted)]">
                Build a position, set target / stop-loss, and deploy it live (paper or real). It runs and reports like any deployment on the Live page.
              </span>
            </div>
            {builderTab === "option" ? <OptionTradeBuilder /> : builderTab === "equity" ? <EquityTradeBuilder /> : builderTab === "intraday" ? <MomentumThetaBuilder /> : builderTab === "cpre" ? <CpRatioExpiryBuilder /> : <DeltaNeutralBuilder />}
          </div>
        ) : (
          <div className="space-y-4">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-sm text-[var(--muted)] mr-1">Screener</span>
              {SCREENERS.map((s) => (
                <button
                  key={s.id}
                  onClick={() => setScreener(s.id)}
                  className={`rounded-full px-3 py-1 text-sm font-medium ${
                    screener === s.id ? "bg-[var(--accent)] text-white" : "bg-[var(--chip)] text-[var(--chip-text)] hover:opacity-80"
                  }`}
                >
                  {s.label}
                </button>
              ))}
            </div>
            {screener === "fibret" ? (
              <FibRetPage />
            ) : screener === "donchian" ? (
              <DonchianStranglePage />
            ) : (
              <div className="rounded-[18px] border border-dashed border-[var(--border)] bg-[var(--card)] p-10 text-center text-[var(--muted)]">
                {SCREENERS.find((s) => s.id === screener)?.label} screener — coming soon.
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
