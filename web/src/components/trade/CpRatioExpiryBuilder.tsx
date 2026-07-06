import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, brokers } from "../../api/client";
import { ErrorBox, NumberInput } from "../ui";
import { Panel } from "../redesign";

const inputClass =
  "w-full rounded-[10px] bg-[var(--field)] border border-[var(--field-border)] px-2.5 py-1.5 text-sm text-[var(--strong)] focus:outline-none focus:border-[var(--accent)]";
const lbl = "block text-xs text-[var(--muted)] mb-1";

const UNDERLYINGS = ["NIFTY", "SENSEX"] as const;

/** Deploy card for call_put_ratio_expiry — expiry-day-only 1:3 premium-ratio seller.
 * Strike selection reads the LIVE chain at 09:20, so a broker quote source is mandatory
 * (the backend enforces it too). */
export default function CpRatioExpiryBuilder() {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [unders, setUnders] = useState<string[]>(["NIFTY"]);
  const [sets, setSets] = useState<Record<string, number>>({ NIFTY: 1, SENSEX: 1 });
  const [targetPct, setTargetPct] = useState(1.1);
  const [stopPct, setStopPct] = useState(1.0);
  const [tolerance, setTolerance] = useState(30);
  const [capital, setCapital] = useState(500_000);
  const [accountId, setAccountId] = useState<number | null>(null);
  const [mode, setMode] = useState("PAPER");
  const [auto, setAuto] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: accounts } = useQuery({ queryKey: ["brokers"], queryFn: brokers.list });
  const sessioned = (accounts ?? []).filter((a) => a.has_session && (a.broker || "zerodha") === "zerodha");

  const toggle = (u: string) =>
    setUnders((v) => (v.includes(u) ? v.filter((x) => x !== u) : [...v, u]));

  async function deploy() {
    setBusy(true);
    setError(null);
    try {
      await api.cpRatioExpiryDeploy({
        name: name.trim() || "CP ratio expiry",
        underlyings: unders,
        sets: Object.fromEntries(unders.map((u) => [u, Math.max(1, Math.round(sets[u] ?? 1))])),
        profit_target_pct: targetPct,
        stop_loss_pct: stopPct,
        ratio_tolerance_pct: tolerance,
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

  const canDeploy = unders.length > 0 && !!accountId;

  return (
    <Panel className="max-w-3xl p-5">
      <div className="text-sm text-[var(--muted)] mb-3">
        Expiry day only (NIFTY Tue · SENSEX Thu), once, between 09:20–09:27: buy 1 lot ATM CE + PE,
        then sell 3 lots on each side at the strike trading near <b>⅓ of the ATM premium</b>.
        Exits at +{targetPct}% of margin deployed, −{stopPct}% stop, or 15:20.
      </div>

      <div className="grid md:grid-cols-2 gap-3 mb-3">
        <label className="block"><span className={lbl}>Deployment name</span>
          <input className={inputClass} placeholder="CP ratio expiry" value={name} onChange={(e) => setName(e.target.value)} /></label>
        <div>
          <span className={lbl}>Underlyings · sets (1 set = buy 1 + sell 3/side)</span>
          <div className="flex flex-wrap items-center gap-3">
            {UNDERLYINGS.map((u) => (
              <span key={u} className="inline-flex items-center gap-1.5">
                <label className="flex items-center gap-1.5 text-sm text-[var(--strong)]">
                  <input type="checkbox" checked={unders.includes(u)} onChange={() => toggle(u)} /> {u}
                </label>
                {unders.includes(u) && (
                  <NumberInput className={`${inputClass} !w-16`} value={sets[u] ?? 1}
                    onChange={(v) => setSets((m) => ({ ...m, [u]: v }))} />
                )}
              </span>
            ))}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-3">
        <label className="block"><span className={lbl}>Target (% of margin)</span><NumberInput step="0.1" className={inputClass} value={targetPct} onChange={setTargetPct} /></label>
        <label className="block"><span className={lbl}>Stop (% of margin)</span><NumberInput step="0.1" className={inputClass} value={stopPct} onChange={setStopPct} /></label>
        <label className="block"><span className={lbl}>⅓-strike tolerance %</span><NumberInput className={inputClass} value={tolerance} onChange={setTolerance} /></label>
        <label className="block"><span className={lbl}>Capital (₹)</span><NumberInput className={inputClass} value={capital} onChange={setCapital} /></label>
      </div>

      <div className="grid md:grid-cols-3 gap-3 items-end">
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
        <label className="flex items-center gap-2 pb-2 text-sm text-[var(--strong)]">
          <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> auto loop
        </label>
      </div>

      <div className="mt-2 text-[11px] text-[var(--faint)]">
        Net short 2 lots per side beyond the ⅓ strikes — the margin-based stop is the only guard.
        Deploy any time; it simply waits for the next expiry-day 09:20 window. If no strike trades
        within the tolerance of ⅓ premium, it skips that day.
      </div>

      {mode === "LIVE" && <div className="mt-2 text-[11px] text-amber-700 dark:text-amber-300">Live places real orders only on an armed broker account with live trading enabled — otherwise it runs as paper.</div>}
      <div className="mt-3 flex items-center gap-3">
        <button onClick={deploy} disabled={busy || !canDeploy}
          className="rounded-md bg-brand hover:bg-brand-light px-4 py-2 text-sm font-medium disabled:opacity-50">
          {busy ? "Deploying…" : "Save & deploy"}
        </button>
        <span className="text-xs text-[var(--faint)]">Expiry days only · flat by 15:20.</span>
      </div>
      {error && <div className="mt-2"><ErrorBox message={error} /></div>}
    </Panel>
  );
}
