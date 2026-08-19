import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, brokers } from "../../api/client";
import { ErrorBox, NumberInput } from "../ui";
import { Panel } from "../redesign";

const inputClass =
  "w-full rounded-[10px] bg-[var(--field)] border border-[var(--field-border)] px-2.5 py-1.5 text-sm text-[var(--strong)] focus:outline-none focus:border-[var(--accent)]";
const lbl = "block text-xs text-[var(--muted)] mb-1";

const SIDES = [
  { value: "ce", label: "Calls only (backtest's pick)" },
  { value: "fair_value", label: "Auto by fair value" },
  { value: "both", label: "Both sides" },
  { value: "pe", label: "Puts only" },
];

/** Deploy card for fair_value_calendar — the monthly premium-matched ratio calendar.
 * The ~150/~450/~200 premium hunt reads the LIVE chain, so a broker source is mandatory
 * (the backend enforces it). Deploy default side "ce": the only side the 5-year store
 * replay rewarded — fair-value auto / both / pe backtested negative. */
export default function FairValueCalendarBuilder() {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [sideMode, setSideMode] = useState("ce");
  const [sets, setSets] = useState(1);
  const [prem1, setPrem1] = useState(150);
  const [prem2, setPrem2] = useState(450);
  const [buyPrem, setBuyPrem] = useState(200);
  const [tolerance, setTolerance] = useState(30);
  const [gapCap, setGapCap] = useState(900);
  const [minDte, setMinDte] = useState(4);
  const [targetPct, setTargetPct] = useState(5);
  const [stopPct, setStopPct] = useState(0);
  const [rollTime, setRollTime] = useState("15:00");
  const [entryTime, setEntryTime] = useState("09:30");
  const [forceEntry, setForceEntry] = useState(false);
  const [capital, setCapital] = useState(500_000);
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
      await api.fairValueCalendarDeploy({
        name: name.trim() || "FV calendar",
        underlying: "NIFTY",
        sets: Math.max(1, Math.round(sets)),
        side_mode: sideMode,
        sell_premium_1: prem1,
        sell_premium_2: prem2,
        buy_premium: buyPrem,
        premium_tolerance_pct: tolerance,
        max_gap_points: gapCap,
        min_sold_dte: minDte,
        profit_target_pct: targetPct,
        stop_loss_pct: stopPct,
        roll_time: rollTime,
        entry_time: entryTime,
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
        From the 1st of each month (retried daily): sell a ~₹{prem1} and a ~₹{prem2} near-weekly,
        buy {3 * Math.max(1, sets)} lots of a ~₹{buyPrem} monthly — same side, premium-matched.
        Skips the day when the buy-to-furthest-sell gap exceeds {gapCap} pts. No target by the
        weekly&apos;s expiry → both sells roll to the next weekly at the same strikes; when they
        would land on the buy expiry the <b>cycle closes</b> (the buy leg is never rolled) and a
        fresh one opens next session. Target +{targetPct}% of the frozen broker margin
        (~₹1.3L/set measured Aug 2026).
      </div>

      <div className="grid md:grid-cols-2 gap-3 mb-3">
        <label className="block"><span className={lbl}>Deployment name</span>
          <input className={inputClass} placeholder="FV calendar" value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label className="block"><span className={lbl}>Side</span>
          <select className={inputClass} value={sideMode} onChange={(e) => setSideMode(e.target.value)}>
            {SIDES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
          </select></label>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-3">
        <label className="block"><span className={lbl}>Lot-sets</span>
          <NumberInput className={inputClass} value={sets} onChange={setSets} /></label>
        <label className="block"><span className={lbl}>Sell premium 1 (₹)</span>
          <NumberInput className={inputClass} value={prem1} onChange={setPrem1} /></label>
        <label className="block"><span className={lbl}>Sell premium 2 (₹)</span>
          <NumberInput className={inputClass} value={prem2} onChange={setPrem2} /></label>
        <label className="block"><span className={lbl}>Buy premium (₹)</span>
          <NumberInput className={inputClass} value={buyPrem} onChange={setBuyPrem} /></label>
        <label className="block"><span className={lbl}>Premium tolerance %</span>
          <NumberInput className={inputClass} value={tolerance} onChange={setTolerance} /></label>
        <label className="block"><span className={lbl}>Max gap (pts)</span>
          <NumberInput className={inputClass} value={gapCap} onChange={setGapCap} /></label>
        <label className="block"><span className={lbl}>Sold leg min DTE</span>
          <NumberInput className={inputClass} value={minDte} onChange={setMinDte} /></label>
        <label className="block"><span className={lbl}>Target % of margin</span>
          <NumberInput className={inputClass} value={targetPct} onChange={setTargetPct} /></label>
        <label className="block"><span className={lbl}>Stop % (0 = off)</span>
          <NumberInput className={inputClass} value={stopPct} onChange={setStopPct} /></label>
        <label className="block"><span className={lbl}>Entry time</span>
          <input className={inputClass} value={entryTime} onChange={(e) => setEntryTime(e.target.value)} /></label>
        <label className="block"><span className={lbl}>Roll time (expiry day)</span>
          <input className={inputClass} value={rollTime} onChange={(e) => setRollTime(e.target.value)} /></label>
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
            force entry (skip the month gate)
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
        title={!accountId ? "pick a broker account with a live session — the premium hunt reads the live chain" : undefined}
        className="rounded-[10px] bg-[var(--accent)] px-4 py-2 text-sm font-bold text-white disabled:opacity-50"
      >
        {busy ? "Deploying…" : `Deploy ${mode}`}
      </button>
    </Panel>
  );
}
