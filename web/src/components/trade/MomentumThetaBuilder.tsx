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

/** Deploy card for momentum_theta_gainer_intra — intraday 15-min SuperTrend(7,3) +
 * daily-pivot ATM weekly option seller. SENSEX is live-only (no cached BSE data), so it
 * requires a Zerodha quote source; the backend enforces the same rule. */
export default function MomentumThetaBuilder() {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [unders, setUnders] = useState<string[]>(["NIFTY"]);
  const [lots, setLots] = useState<Record<string, number>>({ NIFTY: 1, SENSEX: 1 });
  const [stPeriod, setStPeriod] = useState(7);
  const [stMult, setStMult] = useState(3);
  const [candleMin, setCandleMin] = useState(15);
  const [maxTrades, setMaxTrades] = useState(3);
  const [capital, setCapital] = useState(500_000);
  const [minDte, setMinDte] = useState(0);
  const [quoteSource, setQuoteSource] = useState("zerodha");
  const [accountId, setAccountId] = useState<number | null>(null);
  const [mode, setMode] = useState("PAPER");
  const [auto, setAuto] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: accounts } = useQuery({ queryKey: ["brokers"], queryFn: brokers.list });
  const sessioned = (accounts ?? []).filter((a) => a.has_session);

  const toggle = (u: string) =>
    setUnders((v) => (v.includes(u) ? v.filter((x) => x !== u) : [...v, u]));

  async function deploy() {
    setBusy(true);
    setError(null);
    try {
      await api.momentumThetaDeploy({
        name: name.trim() || "Momentum theta intra",
        underlyings: unders,
        lots: Object.fromEntries(unders.map((u) => [u, Math.max(1, Math.round(lots[u] ?? 1))])),
        st_period: stPeriod,
        st_multiplier: stMult,
        candle_minutes: candleMin,
        max_trades_per_day: maxTrades,
        min_dte: minDte,
        capital,
        mode,
        quote_source: quoteSource,
        broker_account_id: quoteSource !== "cache" ? accountId : null,
        auto,
      });
      navigate("/live");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const sensexOnCache = unders.includes("SENSEX") && quoteSource === "cache";
  const canDeploy = unders.length > 0 && !(quoteSource !== "cache" && !accountId) && !sensexOnCache;

  return (
    <Panel className="max-w-3xl p-5">
      <div className="text-sm text-[var(--muted)] mb-3">
        On a closed {candleMin}-min candle: close above SuperTrend({stPeriod},{stMult}) <b>and</b> above
        pivot R1 → sell the ATM weekly <b>put</b>; below SuperTrend and S1 → sell the ATM <b>call</b>.
        Exits on a SuperTrend flip or 15:20. Max {maxTrades} entries per underlying per day.
      </div>

      <div className="grid md:grid-cols-2 gap-3 mb-3">
        <label className="block"><span className={lbl}>Deployment name</span>
          <input className={inputClass} placeholder="Momentum theta intra" value={name} onChange={(e) => setName(e.target.value)} /></label>
        <div>
          <span className={lbl}>Underlyings · lots</span>
          <div className="flex flex-wrap items-center gap-3">
            {UNDERLYINGS.map((u) => (
              <span key={u} className="inline-flex items-center gap-1.5">
                <label className="flex items-center gap-1.5 text-sm text-[var(--strong)]">
                  <input type="checkbox" checked={unders.includes(u)} onChange={() => toggle(u)} /> {u}
                </label>
                {unders.includes(u) && (
                  <NumberInput className={`${inputClass} !w-16`} value={lots[u] ?? 1}
                    onChange={(v) => setLots((m) => ({ ...m, [u]: v }))} />
                )}
              </span>
            ))}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mb-3">
        <label className="block"><span className={lbl}>ST period</span><NumberInput className={inputClass} value={stPeriod} onChange={setStPeriod} /></label>
        <label className="block"><span className={lbl}>ST multiplier</span><NumberInput step="0.5" className={inputClass} value={stMult} onChange={setStMult} /></label>
        <label className="block"><span className={lbl}>Candle (min)</span><NumberInput className={inputClass} value={candleMin} onChange={setCandleMin} /></label>
        <label className="block"><span className={lbl}>Max trades/day</span><NumberInput className={inputClass} value={maxTrades} onChange={setMaxTrades} /></label>
        <label className="block"><span className={lbl}>Min DTE (0 = 0DTE)</span><NumberInput className={inputClass} value={minDte} onChange={setMinDte} /></label>
      </div>

      <div className="grid md:grid-cols-4 gap-3 items-end">
        <label className="block"><span className={lbl}>Capital (₹)</span><NumberInput className={inputClass} value={capital} onChange={setCapital} /></label>
        <label className="block"><span className={lbl}>Mode</span>
          <select className={inputClass} value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="PAPER">Paper (simulated)</option>
            <option value="LIVE">Live (real money)</option>
          </select></label>
        <label className="block"><span className={lbl}>Quotes</span>
          <select className={inputClass} value={quoteSource} onChange={(e) => setQuoteSource(e.target.value)}>
            <option value="zerodha">Zerodha (live)</option>
            <option value="cache">Cache (offline — NIFTY only)</option>
          </select></label>
        {quoteSource !== "cache" && (
          <label className="block"><span className={lbl}>Account</span>
            <select className={inputClass} value={accountId ?? ""} onChange={(e) => setAccountId(e.target.value ? +e.target.value : null)}>
              <option value="">select…</option>
              {sessioned.filter((a) => (a.broker || "zerodha") === quoteSource).map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}
            </select></label>
        )}
      </div>

      <div className="mt-2 space-y-1">
        <label className="flex items-center gap-2 text-sm text-[var(--strong)]">
          <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> auto loop
        </label>
        {sensexOnCache && (
          <div className="text-[11px] text-amber-700 dark:text-amber-300">
            SENSEX has no cached data — switch quotes to Zerodha (its options quote via BFO).
          </div>
        )}
        <div className="text-[11px] text-[var(--faint)]">
          With a Zerodha session the deploy seeds ~7 days of real 15-min bars, so SuperTrend and
          pivots are live immediately. On the cache source it cold-starts: SuperTrend after ~{2 * stPeriod}
          {" "}candles, entries from day 2 (pivots need a prior day of bars).
        </div>
      </div>

      {mode === "LIVE" && <div className="mt-2 text-[11px] text-amber-700 dark:text-amber-300">Live places real orders only on an armed broker account with live trading enabled — otherwise it runs as paper.</div>}
      <div className="mt-3 flex items-center gap-3">
        <button onClick={deploy} disabled={busy || !canDeploy}
          className="rounded-md bg-brand hover:bg-brand-light px-4 py-2 text-sm font-medium disabled:opacity-50">
          {busy ? "Deploying…" : "Save & deploy"}
        </button>
        <span className="text-xs text-[var(--faint)]">Sells naked ATM weeklies intraday — flat by 15:20 every day.</span>
      </div>
      {error && <div className="mt-2"><ErrorBox message={error} /></div>}
    </Panel>
  );
}
