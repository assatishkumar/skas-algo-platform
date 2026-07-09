import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, brokers } from "../../api/client";
import { ErrorBox, NumberInput } from "../ui";
import { Panel } from "../redesign";

const inputClass =
  "w-full rounded-[10px] bg-[var(--field)] border border-[var(--field-border)] px-2.5 py-1.5 text-sm text-[var(--strong)] focus:outline-none focus:border-[var(--accent)]";
const lbl = "block text-xs text-[var(--muted)] mb-1";

/** Deploy card for iron_fly_monthly — an ATM straddle + breakeven wings (iron fly) on the
 * monthly, with the post-iron-fly adjustment. Live-chain-driven → Zerodha account required. */
export default function IronFlyBuilder() {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [underlying, setUnderlying] = useState("BANKNIFTY");
  const [lots, setLots] = useState(1);
  const [adjust, setAdjust] = useState(true);
  const [adjustDelta, setAdjustDelta] = useState(0.175);
  const [cooldown, setCooldown] = useState(15);
  const [targetPct, setTargetPct] = useState(2.5);
  const [stopPct, setStopPct] = useState(0);
  const [forceEntry, setForceEntry] = useState(false);
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
      await api.ironFlyDeploy({
        name: name.trim() || `Iron fly ${underlying}`,
        underlying,
        lots: Math.max(1, Math.round(lots)),
        force_entry: forceEntry,
        ironfly_adjust: adjust,
        adjust_target_delta: adjustDelta,
        adjust_cooldown_min: cooldown,
        profit_target_pct: targetPct,
        stop_loss_pct: stopPct,
        capital,
        mode,
        quote_source: "zerodha",
        broker_account_id: accountId,
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

  return (
    <Panel className="max-w-3xl p-5">
      <div className="text-sm text-[var(--muted)] mb-3">
        Enters the monthly IRON FLY directly on the 2nd trading day after expiry (~11:00): sell the
        ATM CE + PE straddle, buy the wings at ATM ± (CE + PE premium) — the straddle breakevens.
        {adjust && (
          <> When spot breaches a breakeven, it sells a naked ~{Math.round(adjustDelta * 100)}Δ short
          on the untested side and rolls it (close at ≤10Δ / ≤¼ premium, re-sell); it exits everything
          if the payoff can no longer stay positive.</>
        )}{" "}Exits at +{targetPct}% of margin deployed.
      </div>

      <div className="grid md:grid-cols-3 gap-3 mb-3">
        <label className="block"><span className={lbl}>Deployment name</span>
          <input className={inputClass} placeholder={`Iron fly ${underlying}`} value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label className="block"><span className={lbl}>Underlying</span>
          <select className={inputClass} value={underlying} onChange={(e) => setUnderlying(e.target.value)}>
            <option value="NIFTY">NIFTY (monthly)</option>
            <option value="BANKNIFTY">BANKNIFTY (monthly)</option>
            <option value="SENSEX">SENSEX (monthly · BFO)</option>
          </select></label>
        <label className="block"><span className={lbl}>Lots</span><NumberInput className={inputClass} value={lots} onChange={setLots} /></label>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-3">
        <label className="block"><span className={lbl}>Adjust Δ (15-20Δ)</span><NumberInput step="0.01" className={inputClass} value={adjustDelta} onChange={setAdjustDelta} /></label>
        <label className="block"><span className={lbl}>Cooldown (min)</span><NumberInput className={inputClass} value={cooldown} onChange={setCooldown} /></label>
        <label className="block"><span className={lbl}>Profit (% of margin)</span><NumberInput step="0.1" className={inputClass} value={targetPct} onChange={setTargetPct} /></label>
        <label className="block"><span className={lbl}>Stop % (0 = off)</span><NumberInput step="0.1" className={inputClass} value={stopPct} onChange={setStopPct} /></label>
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
        <label className="flex items-center gap-2 text-sm text-[var(--strong)]" title="Sell the untested side and roll it on a breakeven breach; exit-all if the payoff turns fully negative">
          <input type="checkbox" checked={adjust} onChange={(e) => setAdjust(e.target.checked)} /> iron-fly adjustment
        </label>
        <label className="flex items-center gap-2 text-sm text-[var(--strong)]" title="Enter on the next 11:00+ tick instead of waiting for the entry day (2 trading days after expiry)">
          <input type="checkbox" checked={forceEntry} onChange={(e) => setForceEntry(e.target.checked)} /> force entry now (skip the entry-day wait)
        </label>
      </div>
      <div className="mt-1.5 text-[11px] text-[var(--faint)]">
        Recurring monthly. The naked untested-side short adds an uncapped tail — arm the stop above if
        you want a hard MTM floor beyond the payoff-negative exit.
        {underlying === "SENSEX" && " SENSEX has no cached expiry history, so tick ‘force entry now’ to enter — the schedule-based auto-entry (2 days after expiry) can't fire without it."}
      </div>

      {mode === "LIVE" && <div className="mt-2 text-[11px] text-amber-700 dark:text-amber-300">Live places real orders only on an armed broker account with live trading enabled — otherwise it runs as paper.</div>}
      <div className="mt-3 flex items-center gap-3">
        <button onClick={deploy} disabled={busy || !canDeploy}
          className="rounded-md bg-brand hover:bg-brand-light px-4 py-2 text-sm font-medium disabled:opacity-50">
          {busy ? "Deploying…" : "Save & deploy"}
        </button>
        <span className="text-xs text-[var(--faint)]">Monthly iron fly · untested-side adjustment · 2.5% target.</span>
      </div>
      {error && <div className="mt-2"><ErrorBox message={error} /></div>}
    </Panel>
  );
}
