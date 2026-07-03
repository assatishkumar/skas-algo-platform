import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, brokers } from "../../api/client";
import { ErrorBox, NumberInput } from "../ui";
import { Panel } from "../redesign";

const inputClass =
  "w-full rounded-[10px] bg-[var(--field)] border border-[var(--field-border)] px-2.5 py-1.5 text-sm text-[var(--strong)] focus:outline-none focus:border-[var(--accent)]";
const lbl = "block text-xs text-[var(--muted)] mb-1";

export default function EquityTradeBuilder() {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [symbol, setSymbol] = useState("");
  const [qty, setQty] = useState(0);
  const [capital, setCapital] = useState(200_000);
  const [entryMode, setEntryMode] = useState("immediate");
  const [triggerPrice, setTriggerPrice] = useState(0);
  const [targetPct, setTargetPct] = useState(0);
  const [stopPct, setStopPct] = useState(0);
  const [trailing, setTrailing] = useState(false);
  const [trailPct, setTrailPct] = useState(0);
  const [quoteSource, setQuoteSource] = useState("cache");
  const [accountId, setAccountId] = useState<number | null>(null);
  const [mode, setMode] = useState("PAPER");
  const [ignoreHours, setIgnoreHours] = useState(true);
  const [auto, setAuto] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: accounts } = useQuery({ queryKey: ["brokers"], queryFn: brokers.list });
  const { data: symbols } = useQuery({ queryKey: ["data-symbols"], queryFn: api.dataSymbols, retry: false });
  const sessioned = (accounts ?? []).filter((a) => a.has_session);

  async function deploy() {
    setBusy(true); setError(null);
    const body = {
      name: name.trim() || `${symbol.toUpperCase()} trade`,
      symbol: symbol.trim().toUpperCase(),
      qty: qty > 0 ? Math.round(qty) : 0,
      capital,
      entry_mode: entryMode,
      trigger_price: entryMode === "trigger" && triggerPrice > 0 ? triggerPrice : null,
      target_pct: targetPct > 0 ? targetPct : null,
      stop_pct: stopPct > 0 ? stopPct : null,
      trailing,
      trail_pct: trailing && trailPct > 0 ? trailPct : null,
      mode, quote_source: quoteSource,
      broker_account_id: quoteSource !== "cache" ? accountId : null,
      ignore_market_hours: ignoreHours, auto,
    };
    try {
      await api.deployEquityTrade(body);
      navigate("/live");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const canDeploy = !!symbol.trim() && !(quoteSource !== "cache" && !accountId) &&
    !(entryMode === "trigger" && triggerPrice <= 0);

  return (
    <Panel className="max-w-3xl p-5">
      <div className="grid md:grid-cols-2 gap-3 mb-3">
        <label className="block"><span className={lbl}>Trade name</span>
          <input className={inputClass} placeholder="e.g. RELIANCE swing" value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label className="block"><span className={lbl}>Symbol</span>
          <input className={inputClass} list="eq-symbols" placeholder="RELIANCE" value={symbol}
            onChange={(e) => setSymbol(e.target.value.toUpperCase())} />
          <datalist id="eq-symbols">{(symbols ?? []).slice(0, 2000).map((s) => <option key={s.symbol} value={s.symbol} />)}</datalist>
        </label>
      </div>

      <div className="grid md:grid-cols-3 gap-3 mb-3">
        <label className="block"><span className={lbl}>Quantity (0 = from capital)</span><NumberInput className={inputClass} value={qty} onChange={setQty} /></label>
        <label className="block"><span className={lbl}>Capital (₹)</span><NumberInput className={inputClass} value={capital} onChange={setCapital} /></label>
        <label className="block"><span className={lbl}>Entry</span>
          <select className={inputClass} value={entryMode} onChange={(e) => setEntryMode(e.target.value)}>
            <option value="immediate">Immediate (market)</option>
            <option value="trigger">GTT trigger (on price cross)</option>
          </select></label>
      </div>

      {entryMode === "trigger" && (
        <div className="grid md:grid-cols-3 gap-3 mb-3">
          <label className="block"><span className={lbl}>Trigger price</span><NumberInput className={inputClass} value={triggerPrice} onChange={setTriggerPrice} /></label>
          <div className="md:col-span-2 text-[11px] text-[var(--faint)] self-end pb-2">
            The platform watches the LTP and buys with a market order when price crosses this level
            (either direction from where it is now). Needs the deployment loop running.
          </div>
        </div>
      )}

      <div className="grid md:grid-cols-4 gap-3 mb-3">
        <label className="block"><span className={lbl}>Target % (0 = off)</span><NumberInput step="0.5" className={inputClass} value={targetPct} onChange={setTargetPct} /></label>
        <label className="block"><span className={lbl}>Stop-loss % (0 = off)</span><NumberInput step="0.5" className={inputClass} value={stopPct} onChange={setStopPct} /></label>
        <label className="flex items-center gap-2 text-sm text-[var(--strong)] pb-1.5 self-end">
          <input type="checkbox" checked={trailing} onChange={(e) => setTrailing(e.target.checked)} /> trailing SL</label>
        <label className="block"><span className={lbl}>Trail %</span><NumberInput step="0.5" className={inputClass} value={trailPct} onChange={setTrailPct} disabled={!trailing} /></label>
      </div>

      <div className="grid md:grid-cols-4 gap-3 items-end">
        <label className="block"><span className={lbl}>Mode</span>
          <select className={inputClass} value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="PAPER">Paper (simulated)</option>
            <option value="LIVE">Live (real money)</option>
          </select></label>
        <label className="block"><span className={lbl}>Quotes</span>
          <select className={inputClass} value={quoteSource} onChange={(e) => setQuoteSource(e.target.value)}>
            <option value="cache">Cache (offline)</option>
            <option value="zerodha">Zerodha (live)</option>
            <option value="dhan">Dhan (live)</option>
          </select></label>
        {quoteSource !== "cache" && (
          <label className="block"><span className={lbl}>Account</span>
            <select className={inputClass} value={accountId ?? ""} onChange={(e) => setAccountId(e.target.value ? +e.target.value : null)}>
              <option value="">select…</option>
              {sessioned.filter((a) => (a.broker || "zerodha") === quoteSource).map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}
            </select></label>
        )}
        <div className="flex flex-col gap-1">
          <label className="flex items-center gap-2 text-sm text-[var(--strong)]"><input type="checkbox" checked={ignoreHours} onChange={(e) => setIgnoreHours(e.target.checked)} /> ignore market hours</label>
          <label className="flex items-center gap-2 text-sm text-[var(--strong)]"><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> auto loop</label>
        </div>
      </div>

      {mode === "LIVE" && <div className="mt-2 text-[11px] text-amber-700 dark:text-amber-300">Live places real orders only on an armed broker account with live trading enabled — otherwise it runs as paper.</div>}
      <div className="mt-3 flex items-center gap-3">
        <button onClick={deploy} disabled={busy || !canDeploy}
          className="rounded-md bg-brand hover:bg-brand-light px-4 py-2 text-sm font-medium disabled:opacity-50">
          {busy ? "Deploying…" : "Save & deploy"}
        </button>
        <span className="text-xs text-[var(--faint)]">Long-only managed position; exits on target / stop / trailing.</span>
      </div>
      {error && <div className="mt-2"><ErrorBox message={error} /></div>}
    </Panel>
  );
}
