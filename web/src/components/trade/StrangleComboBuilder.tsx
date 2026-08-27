import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, brokers } from "../../api/client";
import { ErrorBox, NumberInput } from "../ui";
import { Panel } from "../redesign";

const inputClass =
  "w-full rounded-[10px] bg-[var(--field)] border border-[var(--field-border)] px-2.5 py-1.5 text-sm text-[var(--strong)] focus:outline-none focus:border-[var(--accent)]";
const lbl = "block text-xs text-[var(--muted)] mb-1";

const SAME_STRIKE = [
  { value: "reenter", label: "Re-enter same strike (deck)" },
  { value: "skip", label: "Skip — flat until the strike moves" },
  { value: "hold", label: "Hold — defer the leg stop (risky)" },
];

/** Deploy card for intraday_strangle_combo — the deck's OTM3 strangle with INDEPENDENT
 * legs. One index per deploy; the weekday schedule (Mon NIFTY · Tue both · Wed/Thu SENSEX ·
 * Fri NIFTY) then gates which days that index trades. NIFTY strikes ride the LISTING grid
 * (50-pt steps) via the needs_listing_grid carve-out — the one documented exemption from
 * the round-strikes rule — which is why this deploys from here, not the generic form. */
export default function StrangleComboBuilder() {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [underlying, setUnderlying] = useState("NIFTY");
  const [lots, setLots] = useState(1);
  const [otmSteps, setOtmSteps] = useState(3);
  const [legStop, setLegStop] = useState(40);
  const [legTarget, setLegTarget] = useState(70);
  const [slReentries, setSlReentries] = useState(2);
  const [tgtReentries, setTgtReentries] = useState(2);
  const [sameStrike, setSameStrike] = useState("reenter");
  const [wingSteps, setWingSteps] = useState(0);
  const [mtmStop, setMtmStop] = useState(1500);
  const [entryTime, setEntryTime] = useState("09:16");
  const [exitTime, setExitTime] = useState("15:20");
  const [capital, setCapital] = useState(500_000);
  const [accountId, setAccountId] = useState<number | null>(null);
  const [mode, setMode] = useState("PAPER");
  const [auto, setAuto] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: accounts } = useQuery({ queryKey: ["brokers"], queryFn: brokers.list });
  const sessioned = (accounts ?? []).filter((a) => a.has_session && (a.broker || "zerodha") === "zerodha");

  const days = underlying === "NIFTY" ? "Mon · Tue · Fri" : "Tue · Wed · Thu";

  async function deploy() {
    setBusy(true);
    setError(null);
    try {
      await api.strangleComboDeploy({
        name: name.trim() || `Strangle combo ${underlying}`,
        underlying,
        lots: Math.max(1, Math.round(lots)),
        otm_steps: otmSteps,
        leg_stop_pct: legStop,
        leg_target_pct: legTarget,
        max_sl_reentries: slReentries,
        max_target_reentries: tgtReentries,
        same_strike_action: sameStrike,
        wing_steps: wingSteps,
        mtm_stop_per_lot: mtmStop,
        entry_time: entryTime,
        exit_time: exitTime,
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
        Sell 1 lot OTM{otmSteps} CE + PE on the current weekly at {entryTime}, flat by {exitTime}.
        Each leg is managed <b>independently</b>: {legStop}% stop / {legTarget}% target on its own
        premium, re-entering at a freshly recomputed OTM{otmSteps} ({slReentries}+{tgtReentries}{" "}
        separate budgets). {underlying} trades <b>{days}</b> (the deck&apos;s weekday schedule).
        Overall MTM stop ₹{mtmStop.toLocaleString("en-IN")}/lot, day-cumulative
        {underlying === "SENSEX" && " — the deck runs SENSEX with 0 (off)"}.
        {wingSteps > 0 && ` Wings ${wingSteps} steps beyond each short — defined risk (iron fly).`}
      </div>

      <div className="grid md:grid-cols-2 gap-3 mb-3">
        <label className="block"><span className={lbl}>Deployment name</span>
          <input className={inputClass} placeholder={`Strangle combo ${underlying}`} value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label className="block"><span className={lbl}>Index (one per deploy)</span>
          <select className={inputClass} value={underlying} onChange={(e) => setUnderlying(e.target.value)}>
            <option value="NIFTY">NIFTY (50-pt listing grid)</option>
            <option value="SENSEX">SENSEX (unvalidated — 23 replay days)</option>
          </select></label>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-3">
        <label className="block"><span className={lbl}>Lots</span>
          <NumberInput className={inputClass} value={lots} onChange={setLots} /></label>
        <label className="block"><span className={lbl}>OTM steps (0 = straddle)</span>
          <NumberInput className={inputClass} value={otmSteps} onChange={setOtmSteps} /></label>
        <label className="block"><span className={lbl}>Leg stop %</span>
          <NumberInput className={inputClass} value={legStop} onChange={setLegStop} /></label>
        <label className="block"><span className={lbl}>Leg target %</span>
          <NumberInput className={inputClass} value={legTarget} onChange={setLegTarget} /></label>
        <label className="block"><span className={lbl}>SL re-entries / side</span>
          <NumberInput className={inputClass} value={slReentries} onChange={setSlReentries} /></label>
        <label className="block"><span className={lbl}>Target re-entries / side</span>
          <NumberInput className={inputClass} value={tgtReentries} onChange={setTgtReentries} /></label>
        <label className="block"><span className={lbl}>MTM stop ₹/lot (0 = off)</span>
          <NumberInput className={inputClass} value={mtmStop} onChange={setMtmStop} />
          <span className="mt-1 block text-[11px] text-slate-400">
            Day-cumulative, outranks a leg stop in the same tick. Deck: NIFTY 1500, SENSEX 0.
          </span></label>
        <label className="block"><span className={lbl}>Wings (grid steps, 0 = off)</span>
          <NumberInput className={inputClass} value={wingSteps} onChange={setWingSteps} /></label>
        <label className="block"><span className={lbl}>Same-strike re-entry</span>
          <select className={inputClass} value={sameStrike} onChange={(e) => setSameStrike(e.target.value)}>
            {SAME_STRIKE.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
          </select></label>
        <label className="block"><span className={lbl}>Entry time</span>
          <input className={inputClass} value={entryTime} onChange={(e) => setEntryTime(e.target.value)} /></label>
        <label className="block"><span className={lbl}>Exit time</span>
          <input className={inputClass} value={exitTime} onChange={(e) => setExitTime(e.target.value)} />
          <span className="mt-1 block text-[11px] text-slate-400">
            15:20 — Zerodha auto-squares intraday F&amp;O at 15:26; 15:25 leaves no retry room.
          </span></label>
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
            <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
            auto
          </label>
        </div>
      </div>

      {error && <ErrorBox message={error} />}
      <button
        onClick={deploy}
        disabled={busy || !accountId}
        title={!accountId ? "pick a broker account with a live session — OTM3 selection reads the live chain" : undefined}
        className="rounded-[10px] bg-[var(--accent)] px-4 py-2 text-sm font-bold text-white disabled:opacity-50"
      >
        {busy ? "Deploying…" : `Deploy ${mode}`}
      </button>
    </Panel>
  );
}
