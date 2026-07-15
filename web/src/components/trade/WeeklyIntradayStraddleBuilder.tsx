import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, brokers } from "../../api/client";
import { ErrorBox, NumberInput } from "../ui";
import { Panel } from "../redesign";

const inputClass =
  "w-full rounded-[10px] bg-[var(--field)] border border-[var(--field-border)] px-2.5 py-1.5 text-sm text-[var(--strong)] focus:outline-none focus:border-[var(--accent)]";
const lbl = "block text-xs text-[var(--muted)] mb-1";

/** Deploy card for weekly_intraday_straddle — a weekly-cycle intraday SHORT straddle on NIFTY.
 * The ATM strike is locked once per weekly expiry cycle (09:20 on expiry+1, nearest 100) and traded
 * every day: SELL when the combined premium closes below both its VWAP and the prior day's intraday
 * low; exit on a VWAP cross-up or 15:25. Live-chain + Kite option bars → Zerodha account required. */
export default function WeeklyIntradayStraddleBuilder() {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const underlying = "NIFTY"; // v1: NIFTY only
  const [lots, setLots] = useState(1);
  const [entryStart, setEntryStart] = useState("09:20");
  const [entryCutoff, setEntryCutoff] = useState("15:20");
  const [eodExit, setEodExit] = useState("15:25");
  const [maxEntries, setMaxEntries] = useState(3);
  const [stopPct, setStopPct] = useState(0);
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
      await api.weeklyIntradayStraddleDeploy({
        name: name.trim() || `Weekly straddle ${underlying}`,
        underlying,
        lots: Math.max(1, Math.round(lots)),
        entry_start: entryStart,
        entry_cutoff: entryCutoff,
        eod_exit: eodExit,
        candle_minutes: 5,
        max_entries_per_day: Math.max(1, Math.round(maxEntries)),
        stop_loss_pct: stopPct,
        capital,
        refresh_seconds: 15,
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

  return (
    <Panel className="max-w-3xl p-5">
      <div className="text-sm text-[var(--muted)] mb-3">
        Locks the ATM straddle strike once per weekly expiry cycle (at {entryStart} on the first day
        after expiry, nearest 100) and holds it all week. Each day: SELLS the CE + PE when the combined
        premium closes below <em>both</em> its VWAP and the prior day's intraday low; exits when it
        closes back above VWAP or at {eodExit}. Up to {maxEntries} entries/day; intraday only.
      </div>

      <div className="grid md:grid-cols-3 gap-3 mb-3">
        <label className="block"><span className={lbl}>Deployment name</span>
          <input className={inputClass} placeholder={`Weekly straddle ${underlying}`} value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label className="block"><span className={lbl}>Underlying</span>
          <select className={inputClass} value={underlying} disabled>
            <option value="NIFTY">NIFTY (weekly)</option>
          </select></label>
        <label className="block"><span className={lbl}>Lots</span><NumberInput className={inputClass} value={lots} onChange={setLots} /></label>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-3">
        <label className="block"><span className={lbl}>Entry from</span><input className={inputClass} value={entryStart} onChange={(e) => setEntryStart(e.target.value)} /></label>
        <label className="block"><span className={lbl}>Entry cutoff</span><input className={inputClass} value={entryCutoff} onChange={(e) => setEntryCutoff(e.target.value)} /></label>
        <label className="block"><span className={lbl}>Square-off</span><input className={inputClass} value={eodExit} onChange={(e) => setEodExit(e.target.value)} /></label>
        <label className="block"><span className={lbl}>Max entries/day</span><NumberInput className={inputClass} value={maxEntries} onChange={setMaxEntries} /></label>
      </div>

      <div className="grid md:grid-cols-3 gap-3 items-end">
        <label className="block"><span className={lbl}>Stop % (of margin, 0 = off)</span><NumberInput step="0.1" className={inputClass} value={stopPct} onChange={setStopPct} /></label>
        <label className="block"><span className={lbl}>Capital (₹)</span><NumberInput className={inputClass} value={capital} onChange={setCapital} /></label>
        <label className="block"><span className={lbl}>Mode</span>
          <select className={inputClass} value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="PAPER">Paper (simulated)</option>
            <option value="LIVE">Live (real money)</option>
          </select></label>
      </div>

      <div className="grid md:grid-cols-3 gap-3 items-end mt-3">
        <label className="block"><span className={lbl}>Zerodha account (live chain + bars)</span>
          <select className={inputClass} value={accountId ?? ""} onChange={(e) => setAccountId(e.target.value ? +e.target.value : null)}>
            <option value="">select…</option>
            {sessioned.map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}
          </select></label>
        <label className="flex items-center gap-2 text-sm text-[var(--strong)]">
          <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> auto loop
        </label>
      </div>

      <div className="mt-2 text-[11px] text-[var(--faint)]">
        A short straddle carries uncapped tails — turn the stop on for a hard MTM backstop (default off).
        No backtest (intraday); paper-first, then a small live size. Deploy mid-week and it force-starts
        at the current ATM; use the Live-page "force entry" to sell immediately.
      </div>

      {mode === "LIVE" && <div className="mt-2 text-[11px] text-amber-700 dark:text-amber-300">Live places real orders only on an armed broker account with live trading enabled — otherwise it runs as paper.</div>}
      <div className="mt-3 flex items-center gap-3">
        <button onClick={deploy} disabled={busy || !canDeploy}
          className="rounded-md bg-brand hover:bg-brand-light px-4 py-2 text-sm font-medium disabled:opacity-50">
          {busy ? "Deploying…" : "Save & deploy"}
        </button>
        <span className="text-xs text-[var(--faint)]">Weekly-cycle intraday straddle · VWAP + prior-low gate.</span>
      </div>
      {error && <div className="mt-2"><ErrorBox message={error} /></div>}
    </Panel>
  );
}
