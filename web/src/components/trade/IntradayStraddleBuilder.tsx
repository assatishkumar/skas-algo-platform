import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, brokers } from "../../api/client";
import { ErrorBox, NumberInput } from "../ui";
import { Panel } from "../redesign";

const inputClass =
  "w-full rounded-[10px] bg-[var(--field)] border border-[var(--field-border)] px-2.5 py-1.5 text-sm text-[var(--strong)] focus:outline-none focus:border-[var(--accent)]";
const lbl = "block text-xs text-[var(--muted)] mb-1";

/** Deploy card for intraday_straddle — a daily intraday short straddle on the nearest weekly,
 * with a fixed %-of-margin stop and a trailing stop. Live-chain-driven → Zerodha account required. */
export default function IntradayStraddleBuilder() {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [underlying, setUnderlying] = useState("NIFTY");
  const [lots, setLots] = useState(1);
  const [strikeDelta, setStrikeDelta] = useState(0);
  const [entryTime, setEntryTime] = useState("09:18");
  const [exitTime, setExitTime] = useState("15:25");
  const [stopPct, setStopPct] = useState(2);
  const [trailTrigger, setTrailTrigger] = useState(1);
  const [trailStep, setTrailStep] = useState(0.5);
  const [trailMode, setTrailMode] = useState("ratchet");
  const [capital, setCapital] = useState(1_000_000);
  const [accountId, setAccountId] = useState<number | null>(null);
  const [mode, setMode] = useState("PAPER");
  const [auto, setAuto] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: accounts } = useQuery({ queryKey: ["brokers"], queryFn: brokers.list });
  const sessioned = (accounts ?? []).filter((a) => a.has_session && (a.broker || "zerodha") === "zerodha");

  async function deploy() {
    setBusy(true);
    setError(null);
    try {
      await api.intradayStraddleDeploy({
        name: name.trim() || `Straddle ${underlying}`,
        underlying,
        lots: Math.max(1, Math.round(lots)),
        strike_delta: strikeDelta,
        entry_time: entryTime,
        entry_window_end: "15:00",
        exit_time: exitTime,
        stop_loss_pct: stopPct,
        trail_trigger_pct: trailTrigger,
        trail_step_pct: trailStep,
        trail_mode: trailMode,
        capital,
        refresh_seconds: 20,
        mode,
        quote_source: "zerodha",
        broker_account_id: accountId,
        ignore_market_hours: false,
        auto,
      });
      navigate("/live");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const canDeploy = !!accountId;
  const trailing = trailTrigger > 0 && trailStep > 0;

  return (
    <Panel className="max-w-3xl p-5">
      <div className="text-sm text-[var(--muted)] mb-3">
        Sells an intraday {strikeDelta > 0 ? `~${strikeDelta}Δ` : "ATM"} straddle (CE + PE) on the
        nearest weekly at {entryTime} and exits at {exitTime}. Stop at −{stopPct}% of the broker
        margin
        {trailing && (
          <> , trailing {trailMode === "below_peak"
            ? <> 0.5%-below-peak (from +{trailTrigger}%)</>
            : <> +{trailStep}% per +{trailTrigger}% of profit</>}</>
        )}. Recurs every trading day.
      </div>

      <div className="grid md:grid-cols-3 gap-3 mb-3">
        <label className="block"><span className={lbl}>Deployment name</span>
          <input className={inputClass} placeholder={`Straddle ${underlying}`} value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label className="block"><span className={lbl}>Underlying</span>
          <select className={inputClass} value={underlying} onChange={(e) => setUnderlying(e.target.value)}>
            <option value="NIFTY">NIFTY (weekly)</option>
            <option value="BANKNIFTY">BANKNIFTY (weekly)</option>
          </select></label>
        <label className="block"><span className={lbl}>Lots</span><NumberInput className={inputClass} value={lots} onChange={setLots} /></label>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-3">
        <label className="block"><span className={lbl}>Strike Δ (0 = ATM)</span><NumberInput step="0.05" className={inputClass} value={strikeDelta} onChange={setStrikeDelta} /></label>
        <label className="block"><span className={lbl}>Entry time</span><input className={inputClass} value={entryTime} onChange={(e) => setEntryTime(e.target.value)} /></label>
        <label className="block"><span className={lbl}>Exit time</span><input className={inputClass} value={exitTime} onChange={(e) => setExitTime(e.target.value)} /></label>
        <label className="block"><span className={lbl}>Stop % (of margin)</span><NumberInput step="0.1" className={inputClass} value={stopPct} onChange={setStopPct} /></label>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-3 gap-3 mb-3">
        <label className="block"><span className={lbl}>Trail trigger % (0 = off)</span><NumberInput step="0.1" className={inputClass} value={trailTrigger} onChange={setTrailTrigger} /></label>
        <label className="block"><span className={lbl}>Trail step % (0 = off)</span><NumberInput step="0.1" className={inputClass} value={trailStep} onChange={setTrailStep} /></label>
        <label className="block"><span className={lbl}>Trail mode</span>
          <select className={inputClass} value={trailMode} onChange={(e) => setTrailMode(e.target.value)}>
            <option value="ratchet">Ratchet (step the stop up)</option>
            <option value="below_peak">Trail 0.5% below peak</option>
          </select></label>
      </div>

      <div className="grid md:grid-cols-3 gap-3 items-end">
        <label className="block"><span className={lbl}>Capital (₹)</span><NumberInput className={inputClass} value={capital} onChange={setCapital} /></label>
        <label className="block"><span className={lbl}>Mode</span>
          <select className={inputClass} value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="PAPER">Paper (simulated)</option>
            <option value="LIVE">Live (real money)</option>
          </select></label>
        <label className="block"><span className={lbl}>Zerodha account (live chain required)</span>
          <select className={inputClass} value={accountId ?? ""} onChange={(e) => setAccountId(e.target.value ? +e.target.value : null)}>
            <option value="">select…</option>
            {sessioned.map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}
          </select></label>
      </div>

      <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1">
        <label className="flex items-center gap-2 text-sm text-[var(--strong)]">
          <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> auto loop
        </label>
      </div>
      <div className="mt-1.5 text-[11px] text-[var(--faint)]">
        A short straddle carries uncapped tails — the stop is the only guard. No backtest (intraday);
        paper-first, then a small live size. If you deploy after {entryTime}, use the Live-page "force
        entry" to enter now.
      </div>

      {mode === "LIVE" && <div className="mt-2 text-[11px] text-amber-700 dark:text-amber-300">Live places real orders only on an armed broker account with live trading enabled — otherwise it runs as paper.</div>}
      <div className="mt-3 flex items-center gap-3">
        <button onClick={deploy} disabled={busy || !canDeploy}
          className="rounded-md bg-brand hover:bg-brand-light px-4 py-2 text-sm font-medium disabled:opacity-50">
          {busy ? "Deploying…" : "Save & deploy"}
        </button>
        <span className="text-xs text-[var(--faint)]">Daily intraday straddle · {entryTime}→{exitTime} · fixed + trailing stop.</span>
      </div>
      {error && <div className="mt-2"><ErrorBox message={error} /></div>}
    </Panel>
  );
}
