import { useEffect, useMemo, useRef, useState } from "react";
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

// Known index lot sizes (auto-filled, editable). Live chain supplies the authoritative size.
const INDEX_LOTS: Record<string, number> = { NIFTY: 65, BANKNIFTY: 35, FINNIFTY: 65, MIDCPNIFTY: 140, GOLD: 100 };

type Leg = { right: "CE" | "PE"; strike: number; side: "buy" | "sell"; lots: number; price: number };
const key = (right: string, strike: number) => `${right}:${strike}`;

export default function OptionTradeBuilder() {
  const navigate = useNavigate();
  const [underlying, setUnderlying] = useState("NIFTY");
  const [expiry, setExpiry] = useState("");
  const [greeks, setGreeks] = useState(false);
  const [legs, setLegs] = useState<Leg[]>([]);
  const [legTargets, setLegTargets] = useState<Record<number, number>>({});
  const [legStops, setLegStops] = useState<Record<number, number>>({});
  const [lotSize, setLotSize] = useState(INDEX_LOTS.NIFTY);

  const [targetPct, setTargetPct] = useState(0);
  const [stopPct, setStopPct] = useState(0);
  const [exitAbove, setExitAbove] = useState(0);
  const [exitBelow, setExitBelow] = useState(0);

  const [name, setName] = useState("");
  const [capital, setCapital] = useState(1_000_000);
  const [mode, setMode] = useState("PAPER");
  const [ignoreHours, setIgnoreHours] = useState(true);
  const [auto, setAuto] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Prices source: a logged-in Zerodha account → real-time chain; else the cached EOD chain.
  const { data: accounts } = useQuery({ queryKey: ["brokers"], queryFn: brokers.list });
  const sessioned = useMemo(() => (accounts ?? []).filter((a) => a.has_session), [accounts]);
  const [priceSrc, setPriceSrc] = useState<"cache" | number>("cache");
  const picked = useRef(false);
  useEffect(() => {
    if (!picked.current && sessioned.length) { setPriceSrc(sessioned[0].id); picked.current = true; }
  }, [sessioned]);
  const live = typeof priceSrc === "number";
  const liveAcc = live ? (priceSrc as number) : null;

  // --- underlyings ---
  const liveUnders = useQuery({ queryKey: ["lu", liveAcc], queryFn: () => api.optionsLiveUnderlyings(liveAcc!), enabled: live });
  const cacheUnders = useQuery({ queryKey: ["cu"], queryFn: api.optionsUnderlyings, enabled: !live, retry: false });
  const choices = live ? (liveUnders.data?.underlyings ?? []) : (cacheUnders.data?.available ?? ["NIFTY", "BANKNIFTY", "GOLD"]);

  // --- date (cache only — live is always "now") ---
  const cov = useQuery({ queryKey: ["opt-cov", underlying], queryFn: () => api.optionsCoverage(underlying), enabled: !live, retry: false });
  const date = live ? todayISO() : (cov.data?.end_date ?? todayISO());

  // --- expiries ---
  const liveExp = useQuery({ queryKey: ["le", underlying, liveAcc], queryFn: () => api.optionsLiveExpiries(underlying, liveAcc!), enabled: live });
  const cacheExp = useQuery({ queryKey: ["ce", underlying, date], queryFn: () => api.optionsExpiries(underlying, date), enabled: !live && !!cov.data?.end_date });
  const expiries = (live ? liveExp.data?.expiries : cacheExp.data?.expiries) ?? [];
  useEffect(() => { if (expiries.length && !expiries.includes(expiry)) setExpiry(expiries[0]); }, [expiries, expiry]);

  // --- chain (live refetches every 12s for true real-time premiums) ---
  const liveChainQ = useQuery({
    queryKey: ["lc", underlying, expiry, liveAcc], queryFn: () => api.optionsLiveChain(underlying, expiry, liveAcc!),
    enabled: live && !!expiry, refetchInterval: 12_000,
  });
  const cacheChainQ = useQuery({
    queryKey: ["cc", underlying, date, expiry, greeks], queryFn: () => api.optionsChain(underlying, date, expiry, greeks),
    enabled: !live && !!expiry && !!cov.data?.end_date,
  });
  const chain = live ? liveChainQ.data : cacheChainQ.data;
  const chainLoading = live ? liveChainQ.isLoading : cacheChainQ.isLoading;
  const chainErr = live ? liveChainQ.error : cacheChainQ.error;

  // Reset basket + default lot size when underlying/expiry change.
  useEffect(() => {
    setLegs([]); setLegTargets({}); setLegStops({});
    setLotSize(INDEX_LOTS[underlying.toUpperCase()] ?? 0);
  }, [underlying, expiry]);
  // The live chain carries the authoritative lot size.
  useEffect(() => { if (live && chain?.lot_size) setLotSize(chain.lot_size); }, [live, chain?.lot_size]);

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
  const updateLeg = (i: number, patch: Partial<Leg>) => setLegs((p) => p.map((l, j) => (j === i ? { ...l, ...patch } : l)));
  function removeLeg(i: number) {
    setLegs((p) => p.filter((_, j) => j !== i));
    setLegTargets((m) => { const n = { ...m }; delete n[i]; return n; });
    setLegStops((m) => { const n = { ...m }; delete n[i]; return n; });
  }

  const sz = lotSize > 0 ? lotSize : 1;
  const liveLegs: LiveLeg[] = legs.map((l) => ({
    strike: l.strike, right: l.right, direction: l.side === "sell" ? -1 : 1, units: l.lots * sz, entry: l.price, ltp: l.price,
  }));
  const previewPositions: LivePosition[] = legs.map((l) => ({
    symbol: `${underlying}|${expiry}|${l.strike}|${l.right}`,
    units: l.lots * sz, lots: l.lots, direction: l.side === "sell" ? -1 : 1, avg_price: l.price, ltp: l.price, unrealized_pnl: 0,
  }));
  const netCredit = legs.reduce((s, l) => s + (l.side === "sell" ? 1 : -1) * l.price * l.lots * sz, 0);
  const metrics = spot && expiry && liveLegs.length ? computeMetrics(liveLegs, spot, expiry) : null;

  async function deploy() {
    setBusy(true); setError(null);
    const body = {
      name: name.trim() || `${underlying} custom`, underlying: underlying.toUpperCase(), expiry,
      legs: legs.map((l): OptionTradeLeg => ({ right: l.right, strike: l.strike, side: l.side, lots: l.lots })),
      lot_size: lotSize, capital,
      spot_upper: exitAbove > 0 ? exitAbove : null,
      spot_lower: exitBelow > 0 ? exitBelow : null,
      target_pct: targetPct > 0 ? targetPct : null,
      stop_pct: stopPct > 0 ? stopPct : null,
      leg_targets: Object.keys(legTargets).length ? legTargets : null,
      leg_stops: Object.keys(legStops).length ? legStops : null,
      mode,
      quote_source: live ? "zerodha" : "cache",
      broker_account_id: liveAcc,
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
        <div className="flex flex-wrap items-end gap-3 mb-2">
          <label className="block"><span className={lbl}>Prices</span>
            <select className={inputClass} value={String(priceSrc)}
              onChange={(e) => { picked.current = true; setPriceSrc(e.target.value === "cache" ? "cache" : +e.target.value); }}>
              {sessioned.map((a) => <option key={a.id} value={a.id}>Live · {a.label}</option>)}
              <option value="cache">Cached (EOD)</option>
            </select>
          </label>
          <label className="block"><span className={lbl}>Underlying (any F&amp;O)</span>
            <input className={`${inputClass} w-40`} list="opt-unders" value={underlying}
              onChange={(e) => setUnderlying(e.target.value.toUpperCase())} placeholder="NIFTY / RELIANCE…" />
            <datalist id="opt-unders">{choices.map((u) => <option key={u} value={u} />)}</datalist>
          </label>
          <label className="block"><span className={lbl}>Expiry</span>
            <select className={inputClass} value={expiry} onChange={(e) => setExpiry(e.target.value)}>
              {expiries.length === 0 && <option value="">—</option>}
              {expiries.map((e) => <option key={e} value={e}>{e}</option>)}
            </select></label>
          {!live && <label className="flex items-center gap-1.5 text-xs text-slate-300 pb-2">
            <input type="checkbox" checked={greeks} onChange={(e) => setGreeks(e.target.checked)} /> IV / δ</label>}
        </div>
        <div className="text-[11px] mb-2">
          {live
            ? <span className="text-emerald-600 dark:text-emerald-400">● LIVE premiums (Zerodha, ~12s) </span>
            : <span className="text-amber-600 dark:text-amber-400">○ Cached EOD as of {date} — pick a live Zerodha account above for real-time </span>}
          <span className="text-slate-500">
            {spot != null ? <> · spot <b>{formatInr(spot)}</b> · ATM {chain?.atm_strike}</> : null}
            {" "}· click a CE / PE price to add a leg (defaults to <b>sell</b>; flip B/S in the basket).
          </span>
        </div>
        {chainLoading ? <Spinner /> : chainErr ? <ErrorBox message={(chainErr as Error).message} /> : chain && chain.rows.length ? (
          <SelectableChain rows={chain.rows} atm={chain.atm_strike} greeks={greeks && !live} selected={selected} onToggle={toggleLeg} />
        ) : <div className="text-sm text-slate-500">No chain for {underlying} / {expiry || "—"}{live ? "" : " — refresh its option data on the Data tab."}.</div>}
      </Card>

      {/* Right: basket, payoff, exits, deploy */}
      <div className="space-y-4">
        <Card>
          <div className="flex items-center justify-between mb-2">
            <div className="text-sm font-medium text-slate-300">
              Position · {legs.length} leg{legs.length === 1 ? "" : "s"}
              {legs.length > 0 && (
                <span className="ml-2 text-slate-500 font-normal">net {netCredit >= 0 ? "credit" : "debit"}{" "}
                  <span className={netCredit >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400"}>{formatInr(Math.abs(netCredit))}</span></span>
              )}
            </div>
            <label className="flex items-center gap-1.5 text-xs text-slate-400">lot size
              <NumberInput className={`${inputClass} w-20 py-0.5`} value={lotSize} onChange={(n) => setLotSize(Math.max(0, Math.round(n)))} /></label>
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
                          {l.side === "sell" ? "S" : "B"}</button></td>
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
              <div className="text-[10px] text-slate-500 mt-1">Per-leg Tgt%/SL% are on each leg's own premium (0 = off). Sizing uses lot size {sz} × lots.</div>
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

        {/* Exit rules — split into Target and Stop-loss / exit */}
        <div className="grid md:grid-cols-2 gap-4">
          <Card>
            <div className="text-sm font-medium text-emerald-600 dark:text-emerald-400 mb-2">🎯 Target (book profit)</div>
            <label className="block"><span className={lbl}>Profit target — % of net premium</span>
              <NumberInput step="1" className={inputClass} value={targetPct} onChange={setTargetPct} placeholder="e.g. 50" /></label>
            <div className="text-[10px] text-slate-500 mt-1">Books the whole position when its P&amp;L reaches this % of the net premium taken in. 0 = off.</div>
          </Card>
          <Card>
            <div className="text-sm font-medium text-rose-600 dark:text-rose-400 mb-2">🛑 Stop-loss / exit</div>
            <label className="block mb-2"><span className={lbl}>Stop-loss — % of net premium</span>
              <NumberInput step="1" className={inputClass} value={stopPct} onChange={setStopPct} placeholder="e.g. 100" /></label>
            <div className="grid grid-cols-2 gap-2">
              <label className="block"><span className={lbl}>Exit all if spot ≥</span>
                <NumberInput className={inputClass} value={exitAbove} onChange={setExitAbove} placeholder={spot ? String(Math.round(spot)) : "price"} /></label>
              <label className="block"><span className={lbl}>Exit all if spot ≤</span>
                <NumberInput className={inputClass} value={exitBelow} onChange={setExitBelow} placeholder={spot ? String(Math.round(spot)) : "price"} /></label>
            </div>
            <div className="text-[10px] text-slate-500 mt-1">Exact underlying price (not %). e.g. exit every leg if {underlying} ≥ {exitAbove > 0 ? exitAbove : "960"}. 0 = off.</div>
          </Card>
        </div>

        {/* Deploy */}
        <Card>
          <div className="grid md:grid-cols-2 gap-3 mb-3">
            <label className="block"><span className={lbl}>Strategy name</span>
              <input className={inputClass} placeholder="e.g. Bear call spread" value={name} onChange={(e) => setName(e.target.value)} /></label>
            <label className="block"><span className={lbl}>Capital (₹)</span><NumberInput className={inputClass} value={capital} onChange={setCapital} /></label>
          </div>
          <div className="grid md:grid-cols-3 gap-3 items-end">
            <label className="block"><span className={lbl}>Mode</span>
              <select className={inputClass} value={mode} onChange={(e) => setMode(e.target.value)}>
                <option value="PAPER">Paper (simulated)</option>
                <option value="LIVE">Live (real money)</option>
              </select></label>
            <div className="text-xs text-slate-500">
              Quotes: {live ? <span className="text-emerald-600 dark:text-emerald-400">Zerodha (live)</span> : "Cache (offline)"}
              {live ? "" : " — select a live account in Prices for real orders"}
            </div>
            <div className="flex flex-col gap-1">
              <label className="flex items-center gap-2 text-sm text-slate-300"><input type="checkbox" checked={ignoreHours} onChange={(e) => setIgnoreHours(e.target.checked)} /> ignore market hours</label>
              <label className="flex items-center gap-2 text-sm text-slate-300"><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> auto loop</label>
            </div>
          </div>
          {mode === "LIVE" && <div className="mt-2 text-[11px] text-amber-700 dark:text-amber-300">Live places real orders only on an armed Zerodha account with live trading enabled — otherwise it runs as paper.</div>}
          <div className="mt-3 flex items-center gap-3">
            <button onClick={deploy} disabled={busy || legs.length === 0 || !expiry || lotSize <= 0 || (mode === "LIVE" && !live)}
              className="rounded-md bg-brand hover:bg-brand-light px-4 py-2 text-sm font-medium disabled:opacity-50">
              {busy ? "Deploying…" : "Save & deploy"}
            </button>
            {lotSize <= 0 && <span className="text-xs text-rose-500">Set a lot size first.</span>}
            {mode === "LIVE" && !live && <span className="text-xs text-rose-500">Live mode needs a live Zerodha account in Prices.</span>}
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

// Selectable Sensibull-style chain, mirrored around STRIKE with fixed column widths so it
// stays aligned. Click a CE/PE price to add/remove a leg.
function SelectableChain({
  rows, atm, greeks, selected, onToggle,
}: {
  rows: { strike: number; ce: { ltp: number | null; close: number | null; oi: number | null; delta?: number | null } | null;
          pe: { ltp: number | null; close: number | null; oi: number | null; delta?: number | null } | null }[];
  atm: number | null; greeks: boolean;
  selected: Map<string, Leg>; onToggle: (right: "CE" | "PE", strike: number, price: number | null | undefined) => void;
}) {
  const fmtOi = (v: number | null | undefined) => (v == null ? "—" : v.toLocaleString("en-IN"));
  const priceCell = (right: "CE" | "PE", leg: Leg | undefined) =>
    `cursor-pointer py-1 px-2 text-right font-medium ${right === "CE" ? "text-emerald-700 dark:text-emerald-300" : "text-rose-700 dark:text-rose-300"} ` +
    (leg ? (leg.side === "sell" ? "bg-rose-500/20 ring-1 ring-inset ring-rose-500/40" : "bg-emerald-500/20 ring-1 ring-inset ring-emerald-500/40") : "hover:bg-slate-700/40");
  return (
    <div className="overflow-x-auto max-h-[58vh] overflow-y-auto">
      <table className="w-full table-fixed text-xs tabular-nums">
        <colgroup>
          <col className="w-[18%]" />{greeks && <col className="w-[12%]" />}<col className="w-[18%]" />
          <col className="w-[16%]" /><col className="w-[18%]" />{greeks && <col className="w-[12%]" />}<col className="w-[18%]" />
        </colgroup>
        <thead className="text-slate-400 sticky top-0 bg-slate-900">
          <tr>
            <th className="py-1 px-2 text-right">CE OI</th>
            {greeks && <th className="py-1 px-2 text-right">CE δ</th>}
            <th className="py-1 px-2 text-right text-emerald-700 dark:text-emerald-300">CE LTP</th>
            <th className="py-1 px-2 text-center">STRIKE</th>
            <th className="py-1 px-2 text-right text-rose-700 dark:text-rose-300">PE LTP</th>
            {greeks && <th className="py-1 px-2 text-right">PE δ</th>}
            <th className="py-1 px-2 text-right">PE OI</th>
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
                <td className="py-1 px-2 text-right text-slate-400">{fmtOi(r.ce?.oi)}</td>
                {greeks && <td className="py-1 px-2 text-right text-slate-400">{r.ce?.delta?.toFixed(2) ?? "—"}</td>}
                <td className={priceCell("CE", ceLeg)} onClick={() => onToggle("CE", r.strike, cePrice)}>{cePrice?.toFixed(2) ?? "—"}</td>
                <td className={`py-1 px-2 text-center font-semibold ${isAtm ? "text-amber-700 dark:text-amber-300" : "text-slate-200"}`}>{r.strike}</td>
                <td className={priceCell("PE", peLeg)} onClick={() => onToggle("PE", r.strike, pePrice)}>{pePrice?.toFixed(2) ?? "—"}</td>
                {greeks && <td className="py-1 px-2 text-right text-slate-400">{r.pe?.delta?.toFixed(2) ?? "—"}</td>}
                <td className="py-1 px-2 text-right text-slate-400">{fmtOi(r.pe?.oi)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
