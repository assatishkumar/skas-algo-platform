import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, brokers } from "../../api/client";
import { formatInr } from "../../lib/format";
import { bsDelta, computeMetrics, effectiveSpot, impliedVol, type LiveLeg } from "../../lib/payoff";
import type { DoubleDiagonalLeg, OptionChain, OptionTradeLeg } from "../../types";
import { ErrorBox, NumberInput } from "../../components/ui";
import { Segmented } from "../../components/redesign";
import OptionPayoffPreview from "../../components/trade/OptionPayoffPreview";

/** Build a position — a manual multi-leg book (per-leg expiry, so calendars work). Seed it from a
 *  classic structure OR from ONE OF YOUR OWN STRATEGIES' entry structures (delta/offset strikes get
 *  auto-populated + are editable), then deploy ONCE. The Double Diagonal Calendar deploys through
 *  its own auto-managing engine (roll + ±% exits); everything else deploys as a managed position. */

const R = 0.065;
const inputClass =
  "w-full min-w-0 rounded-[10px] bg-[var(--field)] border border-[var(--field-border)] px-2.5 py-1.5 text-sm text-[var(--strong)] focus:outline-none focus:border-[var(--accent)]";
const lbl = "block text-xs text-[var(--muted)] mb-1";
const todayISO = () => new Date().toISOString().slice(0, 10);
const INDEX_LOTS: Record<string, number> = { NIFTY: 65, BANKNIFTY: 35, FINNIFTY: 65, MIDCPNIFTY: 140 };
const daysBetween = (a: string, b: string) => Math.max(Math.round((Date.parse(b) - Date.parse(a)) / 864e5), 0);
const tYears = (exp: string) => Math.max(daysBetween(todayISO(), exp) / 365, 1 / 365);

type ManualLeg = { side: "buy" | "sell"; right: "CE" | "PE"; strike: number; expiry: string; lots: number };
type SeedLeg = {
  side: "buy" | "sell"; right: "CE" | "PE"; far?: boolean; lots?: number;
  delta?: "short" | "hedge" | number; // delta-picked (short/hedge → params; number → fixed target)
  off?: number; // else offset from ATM (points)
};
interface Tpl {
  id: string; kind: "own" | "classic"; name: string; sub: string;
  deploy: "ddc" | "delta_neutral" | "ratio" | "custom"; legs: SeedLeg[];
  strategyId?: string; // for deploy === "ratio"
}

const OWN: Tpl[] = [
  { id: "double_diagonal", kind: "own", name: "double_diagonal · entry", sub: "near short strangle + far hedges — auto-managed",
    deploy: "ddc",
    legs: [{ side: "sell", right: "CE", delta: "short" }, { side: "sell", right: "PE", delta: "short" },
           { side: "buy", right: "CE", far: true, delta: "hedge" }, { side: "buy", right: "PE", far: true, delta: "hedge" }] },
  { id: "delta_neutral", kind: "own", name: "delta_neutral · entry", sub: "18Δ strangle → rolls the cheap side — auto-managed",
    deploy: "delta_neutral",
    legs: [{ side: "sell", right: "CE", delta: 0.18 }, { side: "sell", right: "PE", delta: 0.18 }] },
  { id: "batman_ratio", kind: "own", name: "batman_ratio · entry", sub: "both-side 1:2 ratio wings + tails — auto-managed",
    deploy: "ratio", strategyId: "batman_ratio_monthly",
    legs: [{ side: "buy", right: "CE", off: 300 }, { side: "sell", right: "CE", off: 600, lots: 2 }, { side: "buy", right: "CE", off: 1600, lots: 2 },
           { side: "buy", right: "PE", off: -300 }, { side: "sell", right: "PE", off: -600, lots: 2 }, { side: "buy", right: "PE", off: -1600, lots: 2 }] },
  { id: "hni_weekly", kind: "own", name: "hni_weekly · entry", sub: "1-3-2 call-ratio tent — auto-managed",
    deploy: "ratio", strategyId: "hni_weekly",
    legs: [{ side: "buy", right: "CE", off: 200 }, { side: "sell", right: "CE", off: 400, lots: 3 }, { side: "buy", right: "CE", off: 600, lots: 2 }] },
];

const CLASSIC: Tpl[] = [
  { id: "short_strangle", kind: "classic", name: "Short strangle", sub: "credit · uncapped", deploy: "custom",
    legs: [{ side: "sell", right: "CE", off: 500 }, { side: "sell", right: "PE", off: -500 }] },
  { id: "iron_condor", kind: "classic", name: "Iron condor", sub: "credit · rangebound", deploy: "custom",
    legs: [{ side: "sell", right: "CE", off: 400 }, { side: "buy", right: "CE", off: 700 }, { side: "sell", right: "PE", off: -400 }, { side: "buy", right: "PE", off: -700 }] },
  { id: "iron_fly", kind: "classic", name: "Iron fly", sub: "credit · pinned", deploy: "custom",
    legs: [{ side: "sell", right: "CE", off: 0 }, { side: "sell", right: "PE", off: 0 }, { side: "buy", right: "CE", off: 600 }, { side: "buy", right: "PE", off: -600 }] },
  { id: "bull_call", kind: "classic", name: "Bull call spread", sub: "debit · bullish", deploy: "custom",
    legs: [{ side: "buy", right: "CE", off: 0 }, { side: "sell", right: "CE", off: 300 }] },
  { id: "calendar", kind: "classic", name: "Calendar (double diagonal)", sub: "near credit · far hedge", deploy: "custom",
    legs: [{ side: "sell", right: "CE", off: 300 }, { side: "sell", right: "PE", off: -300 }, { side: "buy", right: "CE", off: 600, far: true }, { side: "buy", right: "PE", off: -600, far: true }] },
];
const ALL_TPLS = [...OWN, ...CLASSIC];

// Nearest-|delta| OTM strike from a live chain (IV solved off each strike's LTP → BS delta).
function pickDeltaStrike(rows: OptionChain["rows"], right: "CE" | "PE", spot: number, t: number, target: number): number | null {
  let best: { err: number; k: number } | null = null;
  for (const row of rows ?? []) {
    const k = row.strike;
    if ((right === "CE" && k <= spot) || (right === "PE" && k >= spot)) continue;
    const ltp = (right === "CE" ? row.ce : row.pe)?.ltp;
    if (ltp == null || ltp <= 0) continue;
    const iv = impliedVol(ltp, spot, k, t, R, right);
    if (iv == null) continue;
    const err = Math.abs(Math.abs(bsDelta(spot, k, t, R, iv, right)) - target);
    if (!best || err < best.err) best = { err, k };
  }
  return best ? best.k : null;
}

export default function BuildView() {
  const navigate = useNavigate();
  const [tplId, setTplId] = useState<string>("double_diagonal");
  const [blank, setBlank] = useState(false);
  const tpl = blank ? null : ALL_TPLS.find((t) => t.id === tplId) ?? null;
  const isDdc = tpl?.deploy === "ddc";
  // "Managed" deploys run the STRATEGY'S own engine (its adjustment/exits) from the manual legs
  // — no generic exits card. Custom deploys use custom_options with the exits card.
  const managedDeploy = tpl?.deploy === "ddc" || tpl?.deploy === "delta_neutral" || tpl?.deploy === "ratio";

  const [underlying, setUnderlying] = useState("NIFTY");
  const [nearExpiry, setNearExpiry] = useState("");
  const [farExpiry, setFarExpiry] = useState("");

  // Prices source (Zerodha session → live chain, else cache).
  const { data: accounts } = useQuery({ queryKey: ["brokers"], queryFn: brokers.list });
  const sessioned = useMemo(() => (accounts ?? []).filter((a) => a.has_session), [accounts]);
  const [priceSrc, setPriceSrc] = useState<"cache" | number>("cache");
  const picked = useRef(false);
  useEffect(() => {
    if (!picked.current && sessioned.length) { setPriceSrc(sessioned[0].id); picked.current = true; }
  }, [sessioned]);
  const live = typeof priceSrc === "number";
  const liveAcc = live ? (priceSrc as number) : null;

  const liveUnders = useQuery({ queryKey: ["lu", liveAcc], queryFn: () => api.optionsLiveUnderlyings(liveAcc!), enabled: live });
  const choices = live ? (liveUnders.data?.underlyings ?? ["NIFTY"]) : ["NIFTY", "BANKNIFTY", "SENSEX"];
  const cov = useQuery({ queryKey: ["opt-cov", underlying], queryFn: () => api.optionsCoverage(underlying), enabled: !live, retry: false });
  const date = live ? todayISO() : (cov.data?.end_date ?? todayISO());
  const liveExp = useQuery({ queryKey: ["le", underlying, liveAcc], queryFn: () => api.optionsLiveExpiries(underlying, liveAcc!), enabled: live });
  const cacheExp = useQuery({ queryKey: ["ce", underlying, date], queryFn: () => api.optionsExpiries(underlying, date), enabled: !live && !!cov.data?.end_date });
  const expiries = useMemo(() => (live ? liveExp.data?.expiries : cacheExp.data?.expiries) ?? [], [live, liveExp.data, cacheExp.data]);
  useEffect(() => {
    if (!expiries.length) return;
    // Default the near to the first expiry with ≥3 DTE (skip a 0-DTE expiry-day weekly, whose
    // premiums are ~0 and would seed nonsensical strikes) and the far to the next ≥8-DTE one.
    if (!expiries.includes(nearExpiry)) {
      const near = expiries.find((e) => daysBetween(todayISO(), e) >= 3) ?? expiries[0];
      setNearExpiry(near);
    }
    if (!expiries.includes(farExpiry)) {
      const nearIdx = Math.max(0, expiries.findIndex((e) => daysBetween(todayISO(), e) >= 3));
      const far = expiries.slice(nearIdx + 1).find((e) => daysBetween(todayISO(), e) >= 8)
        ?? expiries[Math.min(nearIdx + 1, expiries.length - 1)];
      setFarExpiry(far);
    }
  }, [expiries, nearExpiry, farExpiry]);

  const [legs, setLegs] = useState<ManualLeg[]>([]);
  const usesFar = (tpl?.legs.some((l) => l.far) ?? false) || legs.some((l) => l.expiry === farExpiry && farExpiry !== nearExpiry);

  const nearChainQ = useQuery({
    queryKey: ["lc", underlying, nearExpiry, liveAcc], queryFn: () => api.optionsLiveChain(underlying, nearExpiry, liveAcc!),
    enabled: live && !!nearExpiry, refetchInterval: 12_000,
  });
  const farChainQ = useQuery({
    queryKey: ["lc", underlying, farExpiry, liveAcc], queryFn: () => api.optionsLiveChain(underlying, farExpiry, liveAcc!),
    enabled: live && usesFar && !!farExpiry && farExpiry !== nearExpiry, refetchInterval: 12_000,
  });
  const chainByExpiry: Record<string, OptionChain> = {};
  if (nearChainQ.data) chainByExpiry[nearExpiry] = nearChainQ.data;
  if (farChainQ.data) chainByExpiry[farExpiry] = farChainQ.data;
  const spot = nearChainQ.data?.spot ?? null;
  const lotSize = (live && nearChainQ.data?.lot_size) || INDEX_LOTS[underlying.toUpperCase()] || 0;
  const step = 100;

  // Own-strategy params (drive the delta/offset seed + the DDC deploy).
  const [params, setParams] = useState({
    lots: 1, shortDelta: 0.225, hedgeDelta: 0.175, targetDelta: 0.18, bias: "neutral" as "up" | "neutral" | "down",
    nearDte: 5, farDte: 10, target: 1.5, stop: 1.5,
    ratioTarget: 2.5, ratioStop: 3, maxHold: 20,
  });
  const setP = (patch: Partial<typeof params>) => setParams((p) => ({ ...p, ...patch }));

  const ltpOf = (l: ManualLeg): number | null => {
    const row = (chainByExpiry[l.expiry]?.rows ?? []).find((r) => Number(r.strike) === l.strike);
    return (l.right === "CE" ? row?.ce : row?.pe)?.ltp ?? null;
  };

  // Seed the legs table from a template (delta-picked or offset). Called on template/underlying/
  // expiry changes + the "Re-seed" button. Manual edits after a seed persist until re-seeded.
  function seedFromTemplate(t: Tpl | null) {
    if (!t || !spot) return;
    const atm = Math.round(spot / step) * step;
    const seeded = t.legs.map((sl): ManualLeg => {
      const exp = sl.far ? farExpiry : nearExpiry;
      const rows = chainByExpiry[exp]?.rows ?? [];
      const t2 = tYears(exp || nearExpiry);
      const target = sl.delta === "short" ? params.shortDelta : sl.delta === "hedge" ? params.hedgeDelta : typeof sl.delta === "number" ? sl.delta : null;
      let strike: number;
      if (target != null && rows.length) strike = pickDeltaStrike(rows, sl.right, spot, t2, target) ?? atm + (sl.off ?? 0);
      else strike = Math.round((atm + (sl.off ?? 0)) / step) * step;
      return { side: sl.side, right: sl.right, strike, expiry: exp || nearExpiry, lots: (sl.lots ?? 1) * (t.deploy === "ddc" ? params.lots : 1) };
    });
    setLegs(seeded);
  }

  // Re-seed when the template selection changes (once the chain is ready).
  const seededFor = useRef<string>("");
  useEffect(() => {
    const key = blank ? "blank" : `${tplId}:${nearExpiry}:${farExpiry}:${underlying}`;
    if (blank) { if (seededFor.current !== "blank") { setLegs([]); seededFor.current = "blank"; } return; }
    if (!tpl || !spot || !nearExpiry) return;
    // Wait for the chains a template needs.
    if (tpl.legs.some((l) => l.far) && !chainByExpiry[farExpiry]) return;
    if (!chainByExpiry[nearExpiry]) return;
    if (seededFor.current === key) return;
    seedFromTemplate(tpl);
    seededFor.current = key;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tplId, blank, nearExpiry, farExpiry, underlying, spot, nearChainQ.data, farChainQ.data]);

  const addLeg = () => setLegs((p) => [...p, { side: "sell", right: "CE", strike: Math.round((spot ?? 25000) / step) * step, expiry: nearExpiry, lots: 1 }]);
  const updateLeg = (i: number, patch: Partial<ManualLeg>) => setLegs((p) => p.map((l, j) => (j === i ? { ...l, ...patch } : l)));
  const removeLeg = (i: number) => setLegs((p) => p.filter((_, j) => j !== i));

  const [exitMode, setExitMode] = useState<"managed" | "manual">("managed");
  const [targetPct, setTargetPct] = useState(0);
  const [stopPct, setStopPct] = useState(0);
  const [name, setName] = useState("");
  const [mode, setMode] = useState("PAPER");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const liveLegs: LiveLeg[] = useMemo(
    () => legs.map((l) => {
      const ltp = ltpOf(l) ?? 0;
      return { strike: l.strike, right: l.right, direction: l.side === "sell" ? -1 : 1, units: l.lots * (lotSize || 1), entry: ltp, ltp, expiry: l.expiry };
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [legs, lotSize, nearChainQ.data, farChainQ.data],
  );
  const chartSpot = useMemo(() => effectiveSpot(liveLegs, spot), [liveLegs, spot]);
  const metrics = useMemo(() => (liveLegs.length && chartSpot ? computeMetrics(liveLegs, chartSpot, nearExpiry) : null), [liveLegs, chartSpot, nearExpiry]);
  const netCredit = useMemo(() => liveLegs.reduce((s, l) => s + (l.direction < 0 ? 1 : -1) * (l.ltp ?? 0) * l.units, 0), [liveLegs]);

  // Per-leg position delta per SHARE (signed by direction): short call → −, short put → +.
  const legDelta = (l: ManualLeg): number | null => {
    const ltp = ltpOf(l);
    if (ltp == null || ltp <= 0 || !chartSpot) return null;
    const t = tYears(l.expiry);
    const iv = impliedVol(ltp, chartSpot, l.strike, t, R, l.right);
    if (iv == null) return null;
    return (l.side === "sell" ? -1 : 1) * bsDelta(chartSpot, l.strike, t, R, iv, l.right);
  };
  const netDelta = useMemo(
    () => legs.reduce((s, l) => { const d = legDelta(l); return d == null ? s : s + d * l.lots * (lotSize || 1); }, 0),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [legs, chartSpot, lotSize, nearChainQ.data, farChainQ.data],
  );

  async function deploy() {
    setBusy(true); setError(null);
    try {
      const entryLegs: DoubleDiagonalLeg[] = legs.map((l) => ({ side: l.side, right: l.right, strike: l.strike, expiry: l.expiry, lots: l.lots }));
      let snap;
      if (isDdc) {
        snap = await api.doubleDiagonalDeploy({
          name: name.trim() || `Double diagonal ${underlying}`, underlying: underlying.toUpperCase(), lots: params.lots,
          short_target_delta: params.shortDelta, hedge_target_delta: params.hedgeDelta,
          near_min_dte: params.nearDte, far_min_dte: params.farDte, bias: params.bias, bias_skew: 0.05,
          entry_time: "11:00", entry_weekday: 0, recurring: false, force_entry: true,
          adjust_cooldown_min: 15, adjust_close_delta: 0.1, adjust_close_prem_frac: 0.25, min_adjust_dte: 3,
          profit_target_pct: params.target, stop_loss_pct: params.stop, profit_check: "1min", stop_check: "1min", eod_time: "15:20",
          entry_legs: entryLegs, capital: 1_000_000, mode, quote_source: live ? "zerodha" : "cache", broker_account_id: liveAcc, auto: true,
        });
      } else if (tpl?.deploy === "delta_neutral") {
        snap = await api.deltaNeutralDeploy({
          name: name.trim() || `Delta neutral ${underlying}`, underlying: underlying.toUpperCase(), lots: params.lots,
          target_delta: params.targetDelta, force_entry: true, adjust_threshold_pct: 40, adjust_cooldown_min: 15,
          profit_target_pct: params.target, stop_loss_pct: params.stop,
          entry_legs: entryLegs, capital: 1_000_000, mode, quote_source: live ? "zerodha" : "cache", broker_account_id: liveAcc, auto: true,
        });
      } else if (tpl?.deploy === "ratio") {
        snap = await api.ratioDeploy({
          name: name.trim() || `${tpl.name.split(" ")[0]} ${underlying}`, strategy_id: tpl.strategyId!,
          underlying: underlying.toUpperCase(), entry_legs: entryLegs,
          profit_target_pct: params.ratioTarget, stop_loss_pct: params.ratioStop, max_holding_days: params.maxHold,
          capital: 1_000_000, mode, quote_source: live ? "zerodha" : "cache", broker_account_id: liveAcc, auto: true,
        });
      } else {
        const tradeLegs: OptionTradeLeg[] = legs.map((l) => ({ right: l.right, strike: l.strike, side: l.side, lots: l.lots, expiry: l.expiry === nearExpiry ? null : l.expiry }));
        snap = await api.deployOptionTrade({
          name: name.trim() || `${underlying} custom`, underlying: underlying.toUpperCase(), expiry: nearExpiry, legs: tradeLegs, lot_size: lotSize, capital: 1_000_000,
          target_pct: exitMode === "managed" && targetPct > 0 ? targetPct : null, stop_pct: exitMode === "managed" && stopPct > 0 ? stopPct : null,
          mode, quote_source: live ? "zerodha" : "cache", broker_account_id: liveAcc, ignore_market_hours: true, auto: true,
        });
      }
      navigate(`/live?run=${snap.run_id}`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const canDeploy = legs.length > 0 && (managedDeploy ? live : !!spot);
  const secNo = tpl?.kind === "own" ? "04" : "03"; // exits card number (params card = 02 when own)

  return (
    <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1fr)_470px]">
      {/* left — builder sections */}
      <div className="min-w-0 space-y-4">
        {/* 01 start from */}
        <section className="rounded-[16px] border border-[var(--border)] bg-[var(--card)] p-4">
          <div className="mb-3 flex items-center justify-between gap-2">
            <div className="font-['Space_Grotesk'] text-[15px] font-bold">01 · Start from</div>
            <button onClick={() => setBlank((b) => !b)} className={`rounded-[8px] px-2.5 py-1 text-[12.5px] font-bold ${blank ? "bg-[var(--accent)] text-white" : "bg-[var(--chip)] text-[var(--chip-text)]"}`}>
              {blank ? "Blank ✓" : "Blank — from the chain"}
            </button>
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <div>
              <label className={lbl}>Underlying</label>
              <select className={inputClass} value={underlying} onChange={(e) => setUnderlying(e.target.value)}>
                {choices.map((u) => <option key={u} value={u}>{u}</option>)}
              </select>
            </div>
            <div>
              <label className={lbl}>Near / default expiry</label>
              <select className={inputClass} value={nearExpiry} onChange={(e) => setNearExpiry(e.target.value)}>
                {expiries.map((x) => <option key={x} value={x}>{x}</option>)}
              </select>
            </div>
            <div>
              <label className={lbl}>Far expiry (calendar)</label>
              <select className={inputClass} value={farExpiry} onChange={(e) => setFarExpiry(e.target.value)}>
                {expiries.map((x) => <option key={x} value={x}>{x}</option>)}
              </select>
            </div>
          </div>
          <div className="mt-2 text-[12px] text-[var(--faint)]">
            Prices:{" "}
            <select className="rounded bg-[var(--field)] border border-[var(--field-border)] px-1.5 py-0.5 text-[12px]" value={String(priceSrc)}
              onChange={(e) => setPriceSrc(e.target.value === "cache" ? "cache" : Number(e.target.value))}>
              <option value="cache">Cache (offline)</option>
              {sessioned.map((a) => <option key={a.id} value={a.id}>{a.label ?? `Zerodha #${a.id}`} · live</option>)}
            </select>
            {isDdc && !live && <span className="ml-2 text-[var(--danger)]">— the double diagonal needs a live Zerodha chain.</span>}
          </div>

          {!blank && (
            <>
              <div className="mb-2 mt-4 text-[11.5px] font-extrabold uppercase tracking-wide text-[var(--faint)]">From your strategies — entry structure, you manage it</div>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                {OWN.map((t) => (
                  <TplCard key={t.id} t={t} on={tplId === t.id && !blank} yours onClick={() => { setBlank(false); setTplId(t.id); }} />
                ))}
              </div>
              <div className="mb-2 mt-4 text-[11.5px] font-extrabold uppercase tracking-wide text-[var(--faint)]">Classic structures</div>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                {CLASSIC.map((t) => (
                  <TplCard key={t.id} t={t} on={tplId === t.id && !blank} onClick={() => { setBlank(false); setTplId(t.id); }} />
                ))}
              </div>
            </>
          )}
        </section>

        {/* 02 params (own-templates) */}
        {tpl?.kind === "own" && (
          <section className="rounded-[16px] border border-[var(--border)] bg-[var(--card)] p-4">
            <div className="mb-1 font-['Space_Grotesk'] text-[15px] font-bold">02 · Strategy params</div>
            <p className="mb-3 text-[12px] text-[var(--muted)]">Seed the strikes from these; every leg stays editable below. {isDdc ? "The double diagonal auto-manages the ±% margin exits + the untested-short roll." : tpl.deploy === "delta_neutral" ? "delta_neutral runs its cheap-side roll + ±% margin exits from these legs." : tpl.deploy === "ratio" ? "Runs the strategy's OWN engine from these legs — its %-of-margin target/stop + its native time exit." : "Deploys as a managed position (your exits below)."}</p>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              {isDdc ? (
                <>
                  <Field label="Lots" v={params.lots} on={(n) => setP({ lots: n })} />
                  <Field label="Short Δ" v={params.shortDelta} step={0.005} on={(n) => setP({ shortDelta: n })} />
                  <Field label="Hedge Δ" v={params.hedgeDelta} step={0.005} on={(n) => setP({ hedgeDelta: n })} />
                  <div>
                    <label className={lbl}>Bias</label>
                    <select className={inputClass} value={params.bias} onChange={(e) => setP({ bias: e.target.value as "up" | "neutral" | "down" })}>
                      <option value="up">Up-lean</option><option value="neutral">Neutral</option><option value="down">Down-lean</option>
                    </select>
                  </div>
                  <Field label="Near DTE ≥" v={params.nearDte} on={(n) => setP({ nearDte: n })} />
                  <Field label="Far DTE ≥" v={params.farDte} on={(n) => setP({ farDte: n })} />
                  <Field label="Target % marg" v={params.target} step={0.1} on={(n) => setP({ target: n })} />
                  <Field label="Stop % marg" v={params.stop} step={0.1} on={(n) => setP({ stop: n })} />
                </>
              ) : tpl.id === "delta_neutral" ? (
                <>
                  <Field label="Lots" v={params.lots} on={(n) => setP({ lots: n })} />
                  <Field label="Target Δ" v={params.targetDelta} step={0.005} on={(n) => setP({ targetDelta: n })} />
                  <Field label="Target % marg" v={params.target} step={0.1} on={(n) => setP({ target: n })} />
                  <Field label="Stop % marg (0=off)" v={params.stop} step={0.1} on={(n) => setP({ stop: n })} />
                </>
              ) : tpl.deploy === "ratio" ? (
                <>
                  <Field label="Target % marg" v={params.ratioTarget} step={0.1} on={(n) => setP({ ratioTarget: n })} />
                  <Field label="Stop % marg (0=off)" v={params.ratioStop} step={0.1} on={(n) => setP({ ratioStop: n })} />
                  {tpl.id === "batman_ratio" && <Field label="Max holding days" v={params.maxHold} on={(n) => setP({ maxHold: n })} />}
                  <div className="col-span-2 text-[11.5px] text-[var(--faint)] sm:col-span-4">
                    {tpl.id === "hni_weekly"
                      ? "Runs its Friday exit + %-of-margin target/stop."
                      : "Runs its max-holding-days time exit + %-of-margin target/stop."}
                  </div>
                </>
              ) : (
                <div className="col-span-2 text-[12px] text-[var(--faint)] sm:col-span-4">Strikes seeded from the strategy's default offsets — edit any leg below.</div>
              )}
            </div>
            <button onClick={() => tpl && seedFromTemplate(tpl)} className="mt-3 rounded-[8px] bg-[var(--chip)] px-2.5 py-1 text-[12px] font-bold text-[var(--chip-text)]">↻ Re-seed strikes from params</button>
          </section>
        )}

        {/* legs */}
        <section className="rounded-[16px] border border-[var(--border)] bg-[var(--card)] p-4">
          <div className="mb-3 flex items-center justify-between">
            <div className="font-['Space_Grotesk'] text-[15px] font-bold">{tpl?.kind === "own" ? "03" : "02"} · Legs</div>
            <button onClick={addLeg} className="rounded-[8px] bg-[var(--chip)] px-2.5 py-1 text-[12.5px] font-bold text-[var(--chip-text)]">+ Add leg</button>
          </div>
          {legs.length === 0 ? (
            <div className="rounded-[10px] border border-dashed border-[var(--field-border)] px-4 py-6 text-center text-[13px] text-[var(--faint)]">
              No legs yet — pick a template above{live ? "" : " (needs a live chain to seed strikes)"}, or add one.
            </div>
          ) : (
            <>
              <div className="mb-1 grid grid-cols-[64px_52px_minmax(0,1fr)_78px_44px_58px_minmax(52px,0.9fr)_20px] gap-2 px-1 text-[10.5px] font-bold uppercase tracking-wide text-[var(--faint)]">
                <span>Side</span><span>Type</span><span>Expiry</span><span>Strike</span><span>Lots</span><span className="text-right">Δ</span><span className="text-right">LTP</span><span />
              </div>
              <div className="space-y-2">
                {legs.map((l, i) => {
                  const d = legDelta(l);
                  return (
                  <div key={i} className="grid grid-cols-[64px_52px_minmax(0,1fr)_78px_44px_58px_minmax(52px,0.9fr)_20px] items-center gap-2">
                    <select className={`${inputClass} ${l.side === "sell" ? "text-[var(--danger)]" : "text-[var(--pos)]"} font-bold`} value={l.side} onChange={(e) => updateLeg(i, { side: e.target.value as "buy" | "sell" })}>
                      <option value="sell">SELL</option><option value="buy">BUY</option>
                    </select>
                    <select className={inputClass} value={l.right} onChange={(e) => updateLeg(i, { right: e.target.value as "CE" | "PE" })}>
                      <option value="CE">CE</option><option value="PE">PE</option>
                    </select>
                    <select className={inputClass} value={l.expiry} onChange={(e) => updateLeg(i, { expiry: e.target.value })}>
                      {expiries.map((x) => <option key={x} value={x}>{x}</option>)}
                    </select>
                    <input className={inputClass} type="number" step={step} value={l.strike} onChange={(e) => updateLeg(i, { strike: Number(e.target.value) })} />
                    <input className={inputClass} type="number" min={1} value={l.lots} onChange={(e) => updateLeg(i, { lots: Number(e.target.value) })} />
                    <div className={`text-right font-['Space_Grotesk'] text-[12.5px] font-bold tabular-nums ${d == null ? "text-[var(--faint)]" : d >= 0 ? "text-[var(--pos)]" : "text-[var(--danger)]"}`}>
                      {d == null ? "—" : (d >= 0 ? "+" : "−") + Math.abs(d).toFixed(2)}
                    </div>
                    <div className="text-right font-['Space_Grotesk'] text-[13px] font-bold text-[var(--strong)]">{ltpOf(l) != null ? `₹${ltpOf(l)!.toFixed(2)}` : "—"}</div>
                    <button onClick={() => removeLeg(i)} className="text-[var(--faint)] hover:text-[var(--danger)]">✕</button>
                  </div>
                  );
                })}
              </div>
              <div className="mt-2 flex justify-end gap-1 px-1 text-[12px] text-[var(--faint)]">
                Net position Δ:{" "}
                <span className={`font-['Space_Grotesk'] font-bold tabular-nums ${netDelta >= 0 ? "text-[var(--pos)]" : "text-[var(--danger)]"}`}>
                  {(netDelta >= 0 ? "+" : "−") + Math.abs(netDelta).toFixed(1)}
                </span>
              </div>
            </>
          )}
        </section>

        {/* exits — custom deploys only (managed strategies run their own ± margin exits) */}
        {!managedDeploy && (
          <section className="rounded-[16px] border border-[var(--border)] bg-[var(--card)] p-4">
            <div className="mb-3 flex items-center justify-between">
              <div className="font-['Space_Grotesk'] text-[15px] font-bold">{secNo} · Exits</div>
              <Segmented value={exitMode} onChange={setExitMode} options={[{ value: "managed", label: "Managed" }, { value: "manual", label: "Manual" }]} />
            </div>
            {exitMode === "managed" ? (
              <div className="grid grid-cols-2 gap-3">
                <Field label="Target % of net premium" v={targetPct} on={setTargetPct} />
                <Field label="Stop % of net premium (0 = off)" v={stopPct} on={setStopPct} />
              </div>
            ) : (
              <div className="rounded-[10px] bg-[var(--warn-bg)] px-3 py-2 text-[12.5px] text-[var(--warn-text)]">
                Manual — no target/stop. The position sits on Live with MTM + greeks; square it off there.
              </div>
            )}
          </section>
        )}
      </div>

      {/* right — analysis rail */}
      <div className="min-w-0">
        <div className="sticky top-[86px] space-y-3 rounded-[16px] border border-[var(--border)] bg-[var(--card)] p-4">
          <div className="flex items-center justify-between">
            <div className="font-['Space_Grotesk'] text-[14px] font-bold">{tpl ? tpl.name : "Position"} · payoff</div>
            <div className="text-[12px] text-[var(--faint)]">{underlying}{spot ? ` · ${Math.round(spot)}` : ""}</div>
          </div>
          {chartSpot && liveLegs.length ? (
            <OptionPayoffPreview legs={liveLegs} spot={chartSpot} expiry={nearExpiry} />
          ) : (
            <div className="py-10 text-center text-[13px] text-[var(--faint)]">
              {live ? "Pick a template / add legs to see the payoff." : "Pick a live Zerodha account for real premiums."}
            </div>
          )}

          <div className="grid grid-cols-2 gap-2 rounded-[13px] bg-[var(--stat)] p-3 text-[13px]">
            <Stat label={netCredit >= 0 ? "Net credit" : "Net debit"} value={formatInr(Math.abs(netCredit))} tone={netCredit >= 0 ? "pos" : "danger"} />
            <Stat label="Max profit" value={metrics ? (metrics.maxProfitUnlimited ? "uncapped" : formatInr(metrics.maxProfit)) : "—"} tone="pos" />
            <Stat label="Max loss" value={metrics ? (metrics.maxLossUnlimited ? "uncapped" : formatInr(metrics.maxLoss)) : "—"} tone="danger" />
            <Stat label="Breakevens" value={metrics && metrics.breakevens.length ? metrics.breakevens.map((b) => Math.round(b)).join(" · ") : "—"} />
          </div>

          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className={lbl}>Name</label>
              <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} placeholder={isDdc ? `Double diagonal ${underlying}` : `${underlying} position`} />
            </div>
            <div>
              <label className={lbl}>Mode</label>
              <select className={inputClass} value={mode} onChange={(e) => setMode(e.target.value)}>
                <option value="PAPER">Paper</option><option value="LIVE">Real</option>
              </select>
            </div>
          </div>

          {error && <ErrorBox message={error} />}
          <button disabled={busy || !canDeploy} onClick={deploy} className="w-full rounded-[13px] bg-[var(--ft)] py-2.5 text-sm font-bold text-white disabled:opacity-40">
            {busy ? "Deploying…" : isDdc ? "Deploy double diagonal" : tpl?.deploy === "delta_neutral" ? "Deploy delta neutral" : tpl?.deploy === "ratio" ? `Deploy ${tpl.id === "hni_weekly" ? "HNI weekly" : "batman"}` : `Deploy position · ${legs.length} legs`}
          </button>
          <div className="text-center text-[11.5px] text-[var(--faint)]">
            One-shot: deploys once and never re-enters.{managedDeploy ? " Auto-manages roll + ±% exits from these legs." : ""}
          </div>
        </div>
      </div>
    </div>
  );
}

function TplCard({ t, on, yours, onClick }: { t: Tpl; on: boolean; yours?: boolean; onClick: () => void }) {
  return (
    <button onClick={onClick} className={`rounded-[12px] border px-3 py-2 text-left ${on ? "border-[var(--accent)] bg-[var(--tint)]" : "border-[var(--field-border)] bg-[var(--field)] hover:border-[var(--accent)]"}`}>
      <div className="flex items-center justify-between gap-1">
        <span className="font-['Space_Grotesk'] text-[12.5px] font-bold text-[var(--strong)]">{t.name}</span>
        {yours && <span className="rounded bg-[var(--opt-bg)] px-1 py-0.5 text-[8.5px] font-extrabold text-[var(--opt-text)]">YOURS</span>}
      </div>
      <div className="text-[11px] leading-tight text-[var(--faint)]">{t.sub}</div>
    </button>
  );
}

function Field({ label, v, on, step }: { label: string; v: number; on: (n: number) => void; step?: number }) {
  return (
    <div>
      <label className={lbl}>{label}</label>
      <NumberInput value={v} onChange={on} step={step != null ? String(step) : undefined} className={inputClass} />
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: "pos" | "danger" }) {
  const color = tone === "pos" ? "text-[var(--pos)]" : tone === "danger" ? "text-[var(--danger)]" : "text-[var(--strong)]";
  return (
    <div>
      <div className="text-[10.5px] uppercase tracking-wide text-[var(--faint)]">{label}</div>
      <div className={`font-['Space_Grotesk'] font-bold ${color}`}>{value}</div>
    </div>
  );
}
