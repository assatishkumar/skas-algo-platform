import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, brokers } from "../../api/client";
import { formatInr } from "../../lib/format";
import { computeMetrics, type LiveLeg } from "../../lib/payoff";
import type { LivePosition, OptionTradeLeg } from "../../types";
import { Card, ErrorBox, NumberInput, Spinner } from "../ui";
import LivePayoffChart from "../LivePayoffChart";

const inputClass =
  "w-full rounded-md bg-slate-800 border border-slate-700 px-2.5 py-1.5 text-sm focus:outline-none focus:border-brand";
const lbl = "block text-xs text-slate-400 mb-1";
const todayISO = () => new Date().toISOString().slice(0, 10);

// Approximate lot sizes — for the payoff/metrics PREVIEW only. The backend sizes legs with
// the authoritative contract spec in force on the trade date.
const LOT_SIZE: Record<string, number> = { NIFTY: 65, BANKNIFTY: 35, GOLD: 100 };
const UNDERLYINGS = ["NIFTY", "BANKNIFTY", "GOLD"];

type Leg = { right: "CE" | "PE"; strike: number; side: "buy" | "sell"; lots: number; price: number };
const key = (right: string, strike: number) => `${right}:${strike}`;

export default function OptionTradeBuilder() {
  const navigate = useNavigate();
  const [underlying, setUnderlying] = useState("NIFTY");
  const [date, setDate] = useState(todayISO());
  const [expiry, setExpiry] = useState("");
  const [greeks, setGreeks] = useState(false);
  const [legs, setLegs] = useState<Leg[]>([]);
  const [legTargets, setLegTargets] = useState<Record<number, number>>({});
  const [legStops, setLegStops] = useState<Record<number, number>>({});

  // Exit config + deploy controls.
  const [spotUpper, setSpotUpper] = useState(0);
  const [spotLower, setSpotLower] = useState(0);
  const [targetPct, setTargetPct] = useState(0);
  const [stopPct, setStopPct] = useState(0);
  const [name, setName] = useState("");
  const [capital, setCapital] = useState(1_000_000);
  const [quoteSource, setQuoteSource] = useState("cache");
  const [accountId, setAccountId] = useState<number | null>(null);
  const [mode, setMode] = useState("PAPER");
  const [ignoreHours, setIgnoreHours] = useState(true);
  const [auto, setAuto] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: cov } = useQuery({ queryKey: ["opt-cov", underlying], queryFn: () => api.optionsCoverage(underlying), retry: false });
  useEffect(() => { if (cov?.end_date) setDate(cov.end_date); }, [cov?.end_date]);

  const { data: expData } = useQuery({
    queryKey: ["opt-exp", underlying, date], queryFn: () => api.optionsExpiries(underlying, date), enabled: !!date,
  });
  const expiries = expData?.expiries ?? [];
  useEffect(() => { if (expiries.length && !expiries.includes(expiry)) setExpiry(expiries[0]); }, [expiries, expiry]);

  const { data: chain, isLoading, error: chainErr } = useQuery({
    queryKey: ["opt-chain", underlying, date, expiry, greeks], queryFn: () => api.optionsChain(underlying, date, expiry, greeks),
    enabled: !!date && !!expiry,
  });
  const { data: accounts } = useQuery({ queryKey: ["brokers"], queryFn: brokers.list });
  const sessioned = (accounts ?? []).filter((a) => a.has_session);

  // Reset the basket when the underlying or expiry changes (prices are expiry-specific).
  useEffect(() => { setLegs([]); setLegTargets({}); setLegStops({}); }, [underlying, expiry]);

  const lotSize = LOT_SIZE[underlying] ?? 1;
  const spot = chain?.spot ?? null;

  function toggleLeg(right: "CE" | "PE", strike: number, price: number | null | undefined) {
    if (price == null) return;
    setLegs((prev) => {
      const i = prev.findIndex((l) => l.right === right && l.strike === strike);
      if (i >= 0) return prev.filter((_, j) => j !== i);
      return [...prev, { right, strike, side: "sell", lots: 1, price }];
    });
  }
  const selected = useMemo(() => new Map(legs.map((l) => [key(l.right, l.strike), l])), [legs]);

  function updateLeg(i: number, patch: Partial<Leg>) {
    setLegs((prev) => prev.map((l, j) => (j === i ? { ...l, ...patch } : l)));
  }
  function removeLeg(i: number) {
    setLegs((prev) => prev.filter((_, j) => j !== i));
    setLegTargets((m) => { const n = { ...m }; delete n[i]; return n; });
    setLegStops((m) => { const n = { ...m }; delete n[i]; return n; });
  }

  // Preview payoff/metrics from the prospective legs (reuses the live components/lib).
  const liveLegs: LiveLeg[] = legs.map((l) => ({
    strike: l.strike, right: l.right, direction: l.side === "sell" ? -1 : 1,
    units: l.lots * lotSize, entry: l.price, ltp: l.price,
  }));
  const previewPositions: LivePosition[] = legs.map((l) => ({
    symbol: `${underlying}|${expiry}|${l.strike}|${l.right}`,
    units: l.lots * lotSize, lots: l.lots, direction: l.side === "sell" ? -1 : 1,
    avg_price: l.price, ltp: l.price, unrealized_pnl: 0,
  }));
  const netCredit = legs.reduce((s, l) => s + (l.side === "sell" ? 1 : -1) * l.price * l.lots * lotSize, 0);
  const metrics = spot && expiry && liveLegs.length ? computeMetrics(liveLegs, spot, expiry) : null;

  async function deploy() {
    setBusy(true); setError(null);
    const body = {
      name: name.trim() || `${underlying} custom`, underlying, expiry,
      legs: legs.map((l): OptionTradeLeg => ({ right: l.right, strike: l.strike, side: l.side, lots: l.lots })),
      capital,
      spot_upper: spotUpper > 0 ? spotUpper : null,
      spot_lower: spotLower > 0 ? spotLower : null,
      target_pct: targetPct > 0 ? targetPct : null,
      stop_pct: stopPct > 0 ? stopPct : null,
      leg_targets: Object.keys(legTargets).length ? legTargets : null,
      leg_stops: Object.keys(legStops).length ? legStops : null,
      mode, quote_source: quoteSource,
      broker_account_id: quoteSource === "zerodha" ? accountId : null,
      ignore_market_hours: ignoreHours, auto,
    };
    try {
      await api.deployOptionTrade(body);
      navigate("/live");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid lg:grid-cols-2 gap-4">
      {/* Left: the chain */}
      <Card>
        <div className="flex flex-wrap items-end gap-3 mb-3">
          <div className="flex gap-1">
            {UNDERLYINGS.map((u) => (
              <button key={u} onClick={() => setUnderlying(u)}
                className={`px-2.5 py-1.5 rounded-md text-sm font-medium ${underlying === u ? "bg-brand text-white" : "bg-slate-800 text-slate-300 hover:bg-slate-700"}`}>
                {u}
              </button>
            ))}
          </div>
          <label className="block"><span className={lbl}>Trade date</span>
            <input type="date" className={inputClass} value={date} onChange={(e) => setDate(e.target.value)} /></label>
          <label className="block"><span className={lbl}>Expiry</span>
            <select className={inputClass} value={expiry} onChange={(e) => setExpiry(e.target.value)}>
              {expiries.length === 0 && <option value="">—</option>}
              {expiries.map((e) => <option key={e} value={e}>{e}</option>)}
            </select></label>
          <label className="flex items-center gap-1.5 text-xs text-slate-300 pb-1.5">
            <input type="checkbox" checked={greeks} onChange={(e) => setGreeks(e.target.checked)} /> IV / δ</label>
          {spot != null && <span className="text-xs text-slate-400 pb-1.5">spot {formatInr(spot)} · ATM {chain?.atm_strike}</span>}
        </div>
        <div className="text-[11px] text-slate-500 mb-2">Click a CE / PE price to add a leg (defaults to <b>sell</b>; flip B/S below). Click again to remove.</div>
        {isLoading ? <Spinner /> : chainErr ? <ErrorBox message={(chainErr as Error).message} /> : chain ? (
          <SelectableChain rows={chain.rows} atm={chain.atm_strike} greeks={greeks} selected={selected} onToggle={toggleLeg} />
        ) : <div className="text-sm text-slate-500">Pick a date and expiry.</div>}
      </Card>

      {/* Right: the basket, payoff, exits, deploy */}
      <div className="space-y-4">
        <Card>
          <div className="text-sm font-medium text-slate-300 mb-2">
            Position · {legs.length} leg{legs.length === 1 ? "" : "s"}
            {legs.length > 0 && (
              <span className="ml-2 text-slate-500">net {netCredit >= 0 ? "credit" : "debit"}{" "}
                <span className={netCredit >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400"}>{formatInr(Math.abs(netCredit))}</span></span>
            )}
          </div>
          {legs.length === 0 ? (
            <div className="text-sm text-slate-500">No legs yet — click prices in the chain.</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs tabular-nums">
                <thead className="text-slate-400 text-left">
                  <tr><th className="py-1 pr-2">B/S</th><th className="py-1 pr-2">Strike</th><th className="py-1 pr-2">Type</th>
                    <th className="py-1 pr-2 text-right">Lots</th><th className="py-1 pr-2 text-right">Price</th>
                    <th className="py-1 pr-2 text-right">Tgt%</th><th className="py-1 pr-2 text-right">SL%</th><th /></tr>
                </thead>
                <tbody>
                  {legs.map((l, i) => (
                    <tr key={`${l.right}-${l.strike}-${i}`} className="border-t border-slate-800">
                      <td className="py-1 pr-2">
                        <button onClick={() => updateLeg(i, { side: l.side === "sell" ? "buy" : "sell" })}
                          className={`px-1.5 py-0.5 rounded text-[11px] font-semibold ${l.side === "sell" ? "bg-rose-500/20 text-rose-600 dark:text-rose-300" : "bg-emerald-500/20 text-emerald-700 dark:text-emerald-300"}`}>
                          {l.side === "sell" ? "S" : "B"}
                        </button>
                      </td>
                      <td className="py-1 pr-2">{l.strike}</td>
                      <td className="py-1 pr-2">{l.right}</td>
                      <td className="py-1 pr-2 text-right w-16"><NumberInput className={`${inputClass} text-right py-0.5`} value={l.lots} onChange={(n) => updateLeg(i, { lots: Math.max(1, Math.round(n)) })} /></td>
                      <td className="py-1 pr-2 text-right w-20"><NumberInput step="0.05" className={`${inputClass} text-right py-0.5`} value={l.price} onChange={(n) => updateLeg(i, { price: n })} /></td>
                      <td className="py-1 pr-2 text-right w-16"><NumberInput className={`${inputClass} text-right py-0.5`} value={legTargets[i] ?? 0} onChange={(n) => setLegTargets((m) => ({ ...m, [i]: n }))} /></td>
                      <td className="py-1 pr-2 text-right w-16"><NumberInput className={`${inputClass} text-right py-0.5`} value={legStops[i] ?? 0} onChange={(n) => setLegStops((m) => ({ ...m, [i]: n }))} /></td>
                      <td className="py-1 text-right"><button onClick={() => removeLeg(i)} className="text-slate-500 hover:text-rose-500">✕</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="text-[10px] text-slate-500 mt-1">Per-leg Tgt%/SL% are on each leg's own premium (0 = off). Lot size for sizing/preview ≈ {lotSize} ({underlying}).</div>
            </div>
          )}
          {legs.length > 0 && spot && <LivePayoffChart positions={previewPositions} spot={spot} />}
          {metrics && (
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs mt-2">
              <Mini label="Max profit" value={Number.isFinite(metrics.maxProfit) ? formatInr(metrics.maxProfit) : "Unlimited"} tone="pos" />
              <Mini label="Max loss" value={metrics.maxLossUnlimited ? "Unlimited" : formatInr(metrics.maxLoss)} tone="neg" />
              <Mini label="Breakevens" value={metrics.breakevens.length ? metrics.breakevens.map((b) => Math.round(b)).join(", ") : "—"} />
              <Mini label="POP" value={metrics.pop != null ? `${(metrics.pop * 100).toFixed(0)}%` : "—"} />
            </div>
          )}
        </Card>

        {/* Exits */}
        <Card>
          <div className="text-sm font-medium text-slate-300 mb-2">Exit rules <span className="text-slate-500 font-normal">(all optional; 0 = off)</span></div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <label className="block"><span className={lbl}>Target % (on net premium)</span><NumberInput step="1" className={inputClass} value={targetPct} onChange={setTargetPct} /></label>
            <label className="block"><span className={lbl}>Stop % (on net premium)</span><NumberInput step="1" className={inputClass} value={stopPct} onChange={setStopPct} /></label>
            <label className="block"><span className={lbl}>Spot upper band</span><NumberInput className={inputClass} value={spotUpper} onChange={setSpotUpper} /></label>
            <label className="block"><span className={lbl}>Spot lower band</span><NumberInput className={inputClass} value={spotLower} onChange={setSpotLower} /></label>
          </div>
        </Card>

        {/* Deploy */}
        <Card>
          <div className="grid md:grid-cols-2 gap-3 mb-3">
            <label className="block"><span className={lbl}>Strategy name</span>
              <input className={inputClass} placeholder="e.g. Bear call spread" value={name} onChange={(e) => setName(e.target.value)} /></label>
            <label className="block"><span className={lbl}>Capital (₹)</span><NumberInput className={inputClass} value={capital} onChange={setCapital} /></label>
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
              </select></label>
            {quoteSource === "zerodha" && (
              <label className="block"><span className={lbl}>Account</span>
                <select className={inputClass} value={accountId ?? ""} onChange={(e) => setAccountId(e.target.value ? +e.target.value : null)}>
                  <option value="">select…</option>
                  {sessioned.map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}
                </select></label>
            )}
            <div className="flex flex-col gap-1">
              <label className="flex items-center gap-2 text-sm text-slate-300"><input type="checkbox" checked={ignoreHours} onChange={(e) => setIgnoreHours(e.target.checked)} /> ignore market hours</label>
              <label className="flex items-center gap-2 text-sm text-slate-300"><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> auto loop</label>
            </div>
          </div>
          {mode === "LIVE" && <div className="mt-2 text-[11px] text-amber-700 dark:text-amber-300">Live places real orders only on an armed Zerodha account with live trading enabled — otherwise it runs as paper.</div>}
          <div className="mt-3 flex items-center gap-3">
            <button onClick={deploy} disabled={busy || legs.length === 0 || !expiry || (quoteSource === "zerodha" && !accountId)}
              className="rounded-md bg-brand hover:bg-brand-light px-4 py-2 text-sm font-medium disabled:opacity-50">
              {busy ? "Deploying…" : "Save & deploy"}
            </button>
            <span className="text-xs text-slate-500">Deploys against today's cached chain — refresh option data to today (Data tab) if entry doesn't fill.</span>
          </div>
          {error && <div className="mt-2"><ErrorBox message={error} /></div>}
        </Card>
      </div>
    </div>
  );
}

function Mini({ label, value, tone }: { label: string; value: string; tone?: "pos" | "neg" }) {
  const c = tone === "pos" ? "text-emerald-600 dark:text-emerald-400" : tone === "neg" ? "text-rose-600 dark:text-rose-400" : "";
  return (
    <div className="rounded-md bg-slate-800/40 px-2.5 py-1.5">
      <div className="text-slate-400 text-[11px] mb-0.5">{label}</div>
      <div className={`font-medium tabular-nums ${c}`}>{value}</div>
    </div>
  );
}

// Selectable Sensibull-style chain: click a CE/PE price to add/remove a leg.
function SelectableChain({
  rows, atm, greeks, selected, onToggle,
}: {
  rows: { strike: number; ce: { ltp: number | null; close: number | null; oi: number | null; iv?: number | null; delta?: number | null } | null;
          pe: { ltp: number | null; close: number | null; oi: number | null; iv?: number | null; delta?: number | null } | null }[];
  atm: number | null; greeks: boolean;
  selected: Map<string, Leg>; onToggle: (right: "CE" | "PE", strike: number, price: number | null | undefined) => void;
}) {
  if (rows.length === 0) return <div className="text-sm text-slate-500">No chain for this date/expiry.</div>;
  const cell = (right: "CE" | "PE", leg: Leg | undefined) =>
    `cursor-pointer py-1 px-2 ${right === "CE" ? "text-right text-emerald-700 dark:text-emerald-300" : "text-left text-rose-700 dark:text-rose-300"} ` +
    (leg ? (leg.side === "sell" ? "bg-rose-500/20 ring-1 ring-inset ring-rose-500/40 font-semibold" : "bg-emerald-500/20 ring-1 ring-inset ring-emerald-500/40 font-semibold") : "hover:bg-slate-700/40");
  return (
    <div className="overflow-x-auto max-h-[60vh] overflow-y-auto">
      <table className="w-full text-xs tabular-nums">
        <thead className="text-slate-400 sticky top-0 bg-slate-900">
          <tr>
            <th className="py-1 px-2 text-right">CE OI</th>
            {greeks && <th className="py-1 px-2 text-right">CE δ</th>}
            <th className="py-1 px-2 text-right text-emerald-700 dark:text-emerald-300">CE LTP</th>
            <th className="py-1 px-2 text-center">STRIKE</th>
            <th className="py-1 px-2 text-left text-rose-700 dark:text-rose-300">PE LTP</th>
            {greeks && <th className="py-1 px-2 text-left">PE δ</th>}
            <th className="py-1 px-2 text-left">PE OI</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const isAtm = atm != null && r.strike === atm;
            const ceLeg = selected.get(key("CE", r.strike));
            const peLeg = selected.get(key("PE", r.strike));
            const cePrice = r.ce?.ltp ?? r.ce?.close;
            const pePrice = r.pe?.ltp ?? r.pe?.close;
            return (
              <tr key={r.strike} className={`border-t border-slate-800 ${isAtm ? "bg-amber-900/20" : ""}`}>
                <td className="py-1 px-2 text-right text-slate-400">{r.ce?.oi?.toLocaleString("en-IN") ?? "—"}</td>
                {greeks && <td className="py-1 px-2 text-right text-slate-400">{r.ce?.delta?.toFixed(2) ?? "—"}</td>}
                <td className={cell("CE", ceLeg)} onClick={() => onToggle("CE", r.strike, cePrice)}>{cePrice?.toFixed(2) ?? "—"}</td>
                <td className={`py-1 px-2 text-center font-semibold ${isAtm ? "text-amber-700 dark:text-amber-300" : "text-slate-200"}`}>{r.strike}</td>
                <td className={cell("PE", peLeg)} onClick={() => onToggle("PE", r.strike, pePrice)}>{pePrice?.toFixed(2) ?? "—"}</td>
                {greeks && <td className="py-1 px-2 text-left text-slate-400">{r.pe?.delta?.toFixed(2) ?? "—"}</td>}
                <td className="py-1 px-2 text-left text-slate-400">{r.pe?.oi?.toLocaleString("en-IN") ?? "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
