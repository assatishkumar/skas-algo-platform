import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Segmented } from "../components/redesign";
import DeployView from "./trade/DeployView";
import BuildView from "./trade/BuildView";
import FibRetPage from "./FibRetPage";
import DonchianStranglePage from "./DonchianStranglePage";

/** Trade — three jobs the old "Strategy Builder" conflated (see design_handoff_trade_v2):
 *  Deploy a managed strategy · Build a manual position (calendars included) · Screener. */

type Mode = "deploy" | "build" | "screener";
const CAPTION: Record<Mode, string> = {
  deploy: "Parameterize a coded strategy — the engine then trades every cycle on its own.",
  build: "Compose a multi-leg position (calendars included) and deploy it once — you own the next cycle.",
  screener: "Scan the universe for a setup, then deploy the resolved basket.",
};

const SCREENERS = [
  { id: "fibret", label: "FibRet" },
  { id: "donchian", label: "Donchian Strangle (monthly)" },
];

export default function TradePage() {
  const [params, setParams] = useSearchParams();
  const raw = params.get("tab");
  const mode: Mode = raw === "screener" ? "screener" : raw === "build" ? "build" : "deploy";
  const setMode = (m: Mode) => {
    const p = new URLSearchParams(params);
    if (m === "deploy") p.delete("tab");
    else p.set("tab", m);
    setParams(p, { replace: true });
  };
  const [screener, setScreener] = useState("fibret");

  return (
    <div className="min-h-[calc(100vh-3.5rem)] bg-[var(--page)] font-['Manrope'] text-[var(--strong)]">
      <div className="mx-auto max-w-[1240px] space-y-4 px-8 pb-16 pt-[30px]">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="font-['Space_Grotesk'] text-[27px] font-bold">Trade</h1>
          <Segmented
            value={mode}
            onChange={setMode}
            options={[
              { value: "deploy", label: "Deploy a strategy" },
              { value: "build", label: "Build a position" },
              { value: "screener", label: "Screener" },
            ]}
          />
        </div>
        <p className="text-[13.5px] text-[var(--muted)]">{CAPTION[mode]}</p>

        {mode === "deploy" && <DeployView />}
        {mode === "build" && <BuildView />}
        {mode === "screener" && (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <span className="mr-1 text-sm text-[var(--muted)]">Screener</span>
              {SCREENERS.map((s) => (
                <button
                  key={s.id}
                  onClick={() => setScreener(s.id)}
                  className={`rounded-full px-3 py-1 text-sm font-medium ${
                    screener === s.id
                      ? "bg-[var(--accent)] text-white"
                      : "bg-[var(--chip)] text-[var(--chip-text)] hover:opacity-80"
                  }`}
                >
                  {s.label}
                </button>
              ))}
            </div>
            {screener === "fibret" ? <FibRetPage /> : <DonchianStranglePage />}
          </div>
        )}
      </div>
    </div>
  );
}
