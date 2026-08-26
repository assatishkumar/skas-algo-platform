import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, brokers } from "../../api/client";
import { ErrorBox, NumberInput } from "../ui";
import { Panel } from "../redesign";

const inputClass =
  "w-full rounded-[10px] bg-[var(--field)] border border-[var(--field-border)] px-2.5 py-1.5 text-sm text-[var(--strong)] focus:outline-none focus:border-[var(--accent)]";
const lbl = "block text-xs text-[var(--muted)] mb-1";

/** Deploy card for volcano_calendar — the monthly PE butterfly + CE calendar. Strike
 * selection and the 4% center-credit rule read the LIVE chain across two expiries (with
 * an IV solve on the far CE), so a broker source is mandatory (the backend enforces it).
 * margin_per_set is effectively required: it is the only margin number that exists BEFORE
 * the order does, and it anchors both the ±2% exits and the 4% walk. */
export default function VolcanoCalendarBuilder() {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [lots, setLots] = useState(1);
  const [marginPerSet, setMarginPerSet] = useState(190_000);
  const [ceOffset, setCeOffset] = useState(200);
  const [maxCreditPct, setMaxCreditPct] = useState(4);
  const [targetPct, setTargetPct] = useState(2);
  const [stopPct, setStopPct] = useState(2);
  const [entryTime, setEntryTime] = useState("15:16");
  const [cycleExitTime, setCycleExitTime] = useState("15:15");
  const [forceEntry, setForceEntry] = useState(false);
  const [capital, setCapital] = useState(500_000);
  const [accountId, setAccountId] = useState<number | null>(null);
  const [mode, setMode] = useState("PAPER");
  const [auto, setAuto] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: accounts } = useQuery({ queryKey: ["brokers"], queryFn: brokers.list });
  const sessioned = (accounts ?? []).filter((a) => a.has_session && (a.broker || "zerodha") === "zerodha");

  const rupees = marginPerSet * Math.max(1, Math.round(lots));

  async function deploy() {
    setBusy(true);
    setError(null);
    try {
      await api.volcanoCalendarDeploy({
        name: name.trim() || "Volcano calendar",
        underlying: "NIFTY",
        lots: Math.max(1, Math.round(lots)),
        margin_per_set: marginPerSet,
        ce_offset: ceOffset,
        max_credit_pct: maxCreditPct,
        profit_target_pct: targetPct,
        stop_loss_pct: stopPct,
        entry_time: entryTime,
        cycle_exit_time: cycleExitTime,
        force_entry: forceEntry,
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

  return (
    <Panel className="max-w-3xl p-5">
      <div className="text-sm text-[var(--muted)] mb-3">
        On the <b>last Friday</b> of each month at {entryTime} (holiday → the prior session):
        buy ATM PE, sell 2× ATM−400 PE, buy ATM−800 PE on the <b>next month&apos;s</b> monthly,
        plus a CE calendar at ATM+{ceOffset} — short that monthly, long the month after, one
        shared strike. If the payoff at spot on the near expiry exceeds {maxCreditPct}% of
        margin, the calendar steps out 100 pts until it doesn&apos;t. Target
        +{targetPct}% / stop −{stopPct}% of ₹{rupees.toLocaleString("en-IN")} (manual);
        neither by the near expiry → close all five legs at {cycleExitTime} that day.
      </div>

      <div className="grid md:grid-cols-2 gap-3 mb-3">
        <label className="block"><span className={lbl}>Deployment name</span>
          <input className={inputClass} placeholder="Volcano calendar" value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label className="block"><span className={lbl}>Lot-sets (1 set = 5 legs / 6 lots)</span>
          <NumberInput className={inputClass} value={lots} onChange={setLots} /></label>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-3">
        <label className="block"><span className={lbl}>Margin per lot-set (₹)</span>
          <NumberInput className={inputClass} value={marginPerSet} onChange={setMarginPerSet} />
          <span className="mt-1 block text-[11px] text-slate-400">
            Anchors the ±% exits AND the {maxCreditPct}% rule — no broker margin exists
            before entry, so measure your basket on Kite. Editable on the running tile.
          </span></label>
        <label className="block"><span className={lbl}>CE offset from ATM (pts)</span>
          <NumberInput className={inputClass} value={ceOffset} onChange={setCeOffset} /></label>
        <label className="block"><span className={lbl}>Max center credit %</span>
          <NumberInput className={inputClass} value={maxCreditPct} onChange={setMaxCreditPct} /></label>
        <label className="block"><span className={lbl}>Target % of margin</span>
          <NumberInput className={inputClass} value={targetPct} onChange={setTargetPct} /></label>
        <label className="block"><span className={lbl}>Stop % of margin</span>
          <NumberInput className={inputClass} value={stopPct} onChange={setStopPct} /></label>
        <label className="block"><span className={lbl}>Entry time (last Friday)</span>
          <input className={inputClass} value={entryTime} onChange={(e) => setEntryTime(e.target.value)} /></label>
        <label className="block"><span className={lbl}>Expiry-day exit time</span>
          <input className={inputClass} value={cycleExitTime} onChange={(e) => setCycleExitTime(e.target.value)} /></label>
        <label className="block"><span className={lbl}>Capital (₹)</span>
          <NumberInput className={inputClass} value={capital} onChange={setCapital} /></label>
      </div>

      <div className="grid md:grid-cols-3 gap-3 mb-3">
        <label className="block"><span className={lbl}>Broker account (quotes)</span>
          <select className={inputClass} value={accountId ?? ""} onChange={(e) => setAccountId(e.target.value ? Number(e.target.value) : null)}>
            <option value="">{sessioned.length ? "Select account…" : "No logged-in session"}</option>
            {sessioned.map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}
          </select></label>
        <label className="block"><span className={lbl}>Mode</span>
          <select className={inputClass} value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="PAPER">PAPER</option>
            <option value="LIVE">LIVE</option>
          </select></label>
        <div className="flex items-end gap-4 pb-1">
          <label className="inline-flex items-center gap-1.5 text-sm text-[var(--muted)]">
            <input type="checkbox" checked={forceEntry} onChange={(e) => setForceEntry(e.target.checked)} />
            force entry (skip the last-Friday wait)
          </label>
          <label className="inline-flex items-center gap-1.5 text-sm text-[var(--muted)]">
            <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
            auto
          </label>
        </div>
      </div>

      {error && <ErrorBox message={error} />}
      <button
        onClick={deploy}
        disabled={busy || !accountId}
        title={!accountId ? "pick a broker account with a live session — strike selection reads the live chain across two expiries" : undefined}
        className="rounded-[10px] bg-[var(--accent)] px-4 py-2 text-sm font-bold text-white disabled:opacity-50"
      >
        {busy ? "Deploying…" : `Deploy ${mode}`}
      </button>
    </Panel>
  );
}
