import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { api, brokers } from "../api/client";
import { Panel } from "../components/redesign";
import { formatInr } from "../lib/format";
import type { DonchianDeployLeg, DonchianPanel, DonchianResult, DonchianRow } from "../types";

const n1 = (v?: number | null) => (v == null ? "—" : v.toFixed(1));
const n2 = (v?: number | null) => (v == null ? "—" : v.toFixed(2));
const money = (v?: number | null) => (v == null ? "—" : formatInr(v));
const pct = (v?: number | null) => (v == null ? "—" : `${v.toFixed(1)}%`);

const inputCls =
  "rounded-[10px] bg-[var(--field)] border border-[var(--field-border)] px-2 py-1 text-[var(--strong)] focus:outline-none focus:border-[var(--accent)]";

// Donchian universe = the Nifty 50, ranked by FREE-FLOAT weight (24 Jun 2026 snapshot). Free-float
// weight tracks tradeable shares → liquidity / options depth, the right sort for selling premium.
// Nestlé (NESTLEIND) is added back at ~0.95% (the source had dropped it). [symbol, free-float weight %]
const NIFTY50_FF: [string, number][] = [
  ["RELIANCE", 9.31], ["HDFCBANK", 6.40], ["BHARTIARTL", 5.99], ["ICICIBANK", 5.16], ["SBIN", 5.00],
  ["TCS", 4.00], ["BAJFINANCE", 3.23], ["LT", 3.01], ["HINDUNILVR", 2.66], ["SUNPHARMA", 2.36],
  ["AXISBANK", 2.26], ["INFY", 2.25], ["ADANIPORTS", 2.19], ["MARUTI", 2.18], ["KOTAKBANK", 2.12],
  ["ADANIENT", 2.09], ["TITAN", 2.01], ["M&M", 2.00], ["ITC", 1.90], ["NTPC", 1.81],
  ["ULTRACEMCO", 1.77], ["BEL", 1.58], ["HCLTECH", 1.58], ["ONGC", 1.58], ["JSWSTEEL", 1.58],
  ["BAJAJFINSV", 1.49], ["BAJAJ-AUTO", 1.43], ["COALINDIA", 1.43], ["POWERGRID", 1.42], ["ASIANPAINT", 1.34],
  ["ETERNAL", 1.30], ["SHRIRAMFIN", 1.26], ["TATASTEEL", 1.24], ["HINDALCO", 1.15], ["GRASIM", 1.12],
  ["EICHERMOT", 1.09], ["INDIGO", 1.05], ["WIPRO", 0.96], ["NESTLEIND", 0.95], ["SBILIFE", 0.93],
  ["TRENT", 0.91], ["JIOFIN", 0.83], ["TECHM", 0.75], ["TMPV", 0.67], ["HDFCLIFE", 0.67],
  ["APOLLOHOSP", 0.65], ["CIPLA", 0.61], ["DRREDDY", 0.58], ["TATACONSUM", 0.57], ["MAXHEALTH", 0.55],
];
const FF_WEIGHT: Record<string, number> = Object.fromEntries(NIFTY50_FF);
const FF_ORDER: Record<string, number> = Object.fromEntries(NIFTY50_FF.map(([s], i) => [s, i]));

const PKEY = "donchian.state.v1";
function loadPersisted(): Record<string, unknown> {
  try { return JSON.parse(localStorage.getItem(PKEY) || "{}"); } catch { return {}; }
}

interface CsvRow { symbol: string; ivp?: number; atmIv?: number; event?: string }

/** Sensibull screener CSV → {symbol, IVP, ATMIV, Event}. Event is a date or "-"/empty. */
function parseDonchianCsv(text: string): CsvRow[] {
  const lines = text.split(/\r?\n/).filter((l) => l.trim());
  if (!lines.length) return [];
  const header = lines[0].split(",").map((h) => h.trim().toLowerCase());
  const col = (name: string) => header.indexOf(name);
  const iSym = col("instrument");
  if (iSym < 0) return [];
  const iIvp = col("ivpercentile");
  const iIv = col("atmiv");
  const iEvent = col("event");
  const out: CsvRow[] = [];
  for (let i = 1; i < lines.length; i++) {
    const c = lines[i].split(",");
    const symbol = (c[iSym] ?? "").trim().toUpperCase();
    if (!symbol) continue;
    const num = (idx: number) => {
      if (idx < 0) return undefined;
      const v = Number((c[idx] ?? "").trim());
      return Number.isFinite(v) ? v : undefined;
    };
    const ev = iEvent >= 0 ? (c[iEvent] ?? "").trim() : "";
    out.push({ symbol, ivp: num(iIvp), atmIv: num(iIv), event: ev && ev !== "-" ? ev : undefined });
  }
  return out;
}

const SELECTABLE = new Set(["strangle", "CE-only", "PE-only"]);

function StatusPill({ status }: { status: string }) {
  const map: Record<string, [string, string]> = {
    strangle: ["var(--ok-bg)", "var(--ok-text)"],
    "CE-only": ["var(--chip)", "var(--chip-text)"],
    "PE-only": ["var(--chip)", "var(--chip-text)"],
    "excluded:event": ["var(--warn-bg)", "var(--warn-text)"],
    "excluded:filter": ["var(--chip)", "var(--faint)"],
    error: ["var(--danger-bg, var(--chip))", "var(--danger)"],
  };
  const [bg, color] = map[status] ?? ["var(--chip)", "var(--chip-text)"];
  return (
    <span className="rounded-full px-2 py-0.5 text-[11px] font-medium" style={{ background: bg, color }}>
      {status}
    </span>
  );
}

/** One option leg cell: "strike · ₹collected" (premium × lot size × lots) with skip / illiquid flags.
 *  The per-share premium + bid/ask sit in the tooltip. */
function LegCell({ leg, units }: { leg?: DonchianRow["ce"]; units: number }) {
  if (!leg) return <span className="text-[var(--faint)]">—</span>;
  const collected = leg.premium != null ? leg.premium * units : null;
  const tip = leg.premium != null
    ? `premium ₹${n2(leg.premium)}/sh × ${units} = ${money(collected)}${leg.bid != null ? ` · bid/ask ${n2(leg.bid)}/${n2(leg.ask)}` : ""}`
    : undefined;
  if (leg.skip)
    return <span className="text-[var(--faint)]" title="premium below floor — skipped">{leg.strike} · skip {money(collected)}</span>;
  return (
    <span className={leg.liquid === false ? "text-[var(--warn-text)]" : ""} title={leg.liquid === false ? "wide spread — illiquid" : tip}>
      {leg.strike} · {money(collected)}{leg.liquid === false ? " ⚑" : ""}
    </span>
  );
}

export default function DonchianStranglePage() {
  const navigate = useNavigate();
  const { data: accounts = [] } = useQuery({ queryKey: ["brokers"], queryFn: brokers.list });
  const sessioned = accounts.filter((a) => a.has_session);

  const saved = useRef(loadPersisted()).current as any;
  const [accountId, setAccountId] = useState<number | null>(saved.accountId ?? null);
  const [csvRows, setCsvRows] = useState<CsvRow[]>(saved.csvRows ?? []);
  const [csvName, setCsvName] = useState(saved.csvName ?? "");
  // Screener params (spec §3).
  const [ivpMin, setIvpMin] = useState(saved.ivpMin ?? 50);
  const [hvWindow, setHvWindow] = useState(saved.hvWindow ?? 20);
  const [skipLegPct, setSkipLegPct] = useState(saved.skipLegPct ?? 0.5);
  const [roundOut, setRoundOut] = useState(saved.roundOut ?? false);
  const [requireIvGtHv, setRequireIvGtHv] = useState(saved.requireIvGtHv ?? true);
  const [breakoutAtm, setBreakoutAtm] = useState(saved.breakoutAtm ?? true);
  const [lots, setLots] = useState(saved.lots ?? 1);
  // Portfolio params.
  const [hedgeOtm, setHedgeOtm] = useState(saved.hedgeOtm ?? 4.5);
  const [betaWeight, setBetaWeight] = useState(saved.betaWeight ?? false);
  const [slPct, setSlPct] = useState(saved.slPct ?? 2);
  const [targetEnabled, setTargetEnabled] = useState(saved.targetEnabled ?? false);
  const [targetPct, setTargetPct] = useState(saved.targetPct ?? 50);
  const [flipDelta, setFlipDelta] = useState<"atm" | "30delta">(saved.flipDelta ?? "atm");
  const [breachBuffer, setBreachBuffer] = useState(saved.breachBuffer ?? 0.5);
  // Cycle overrides (prefilled from the analyze response; blank = auto).
  const [rangeStart, setRangeStart] = useState("");
  const [rangeEnd, setRangeEnd] = useState("");
  const [entryDate, setEntryDate] = useState("");
  const [sellExpiry, setSellExpiry] = useState("");
  // Deploy.
  const [mode, setMode] = useState<"PAPER" | "LIVE">("PAPER");
  const [deployName, setDeployName] = useState("");
  const [capital, setCapital] = useState(5_000_000);

  const [result, setResult] = useState<DonchianResult | null>(saved.result ?? null);
  const [selected, setSelected] = useState<Set<string>>(
    new Set((saved.result?.rows ?? []).filter((r: DonchianRow) => SELECTABLE.has(r.status)).map((r: DonchianRow) => r.symbol)),
  );
  const [panel, setPanel] = useState<DonchianPanel | null>(null);

  // Persist screener inputs + last result so the table survives a reload.
  useEffect(() => {
    localStorage.setItem(PKEY, JSON.stringify({
      accountId, csvRows, csvName, ivpMin, hvWindow, skipLegPct, roundOut, requireIvGtHv, breakoutAtm,
      lots, hedgeOtm, betaWeight, slPct, targetEnabled, targetPct, flipDelta, breachBuffer, result,
    }));
  }, [accountId, csvRows, csvName, ivpMin, hvWindow, skipLegPct, roundOut, requireIvGtHv, breakoutAtm,
      lots, hedgeOtm, betaWeight, slPct, targetEnabled, targetPct, flipDelta, breachBuffer, result]);

  const effectiveAccount = accountId ?? sessioned[0]?.id ?? null;
  const ivpMap = useMemo(() => new Map(csvRows.map((r) => [r.symbol, r])), [csvRows]);

  const { data: activeDeps = [] } = useQuery({
    queryKey: ["deployments", "active"], queryFn: () => api.liveDeployments("active"), refetchInterval: 30000,
  });
  const deployedUnderlyings = useMemo(
    () => new Set(activeDeps.map((d) => (d.underlying ?? "").toUpperCase()).filter(Boolean)),
    [activeDeps],
  );

  // Always screen the 50 free-float-ranked Nifty names (in that order). A Sensibull CSV, if
  // uploaded, supplies each name's IVP / ATMIV / Event (matched by symbol).
  const names = useMemo(() => {
    const csv = new Map(csvRows.map((r) => [r.symbol, r]));
    return NIFTY50_FF.map(([symbol]) => {
      const c = csv.get(symbol);
      return { symbol, atm_iv: c?.atmIv ?? null, ivp: c?.ivp ?? null, event: c?.event ?? null };
    });
  }, [csvRows]);

  const analyze = useMutation({
    mutationFn: () =>
      api.donchianAnalyze({
        broker_account_id: effectiveAccount as number,
        names,
        range_start: rangeStart || null, range_end: rangeEnd || null,
        entry_date: entryDate || null, sell_expiry: sellExpiry || null,
        ivp_min: ivpMin, require_iv_gt_hv: requireIvGtHv, hv_window: hvWindow,
        skip_leg_min_premium_pct: skipLegPct, round_out: roundOut, breakout_atm: breakoutAtm,
        lots_per_name: lots,
      }),
    onSuccess: (res) => {
      setResult(res);
      setSelected(new Set(res.rows.filter((r) => SELECTABLE.has(r.status)).map((r) => r.symbol)));
      // Sync the date fields to the cycle the backend actually resolved/used. (Not `v || …` —
      // that let a stale/invalid override stick and silently invert the window → all rows "error".
      // A deliberate override the backend accepts is echoed back unchanged, so it's preserved.)
      setRangeStart(res.dates.range_start || "");
      setRangeEnd(res.dates.range_end || "");
      setEntryDate(res.dates.entry_date || "");
      setSellExpiry(res.dates.sell_expiry || "");
    },
  });

  const rows = result?.rows ?? [];
  // Always arrange by free-float weight (highest float / deepest options first).
  const sortedRows = useMemo(
    () => [...rows].sort((a, b) => (FF_ORDER[a.symbol] ?? 999) - (FF_ORDER[b.symbol] ?? 999)),
    [rows],
  );
  const selectedRows = useMemo(() => rows.filter((r) => selected.has(r.symbol) && SELECTABLE.has(r.status)), [rows, selected]);
  // ISO date strings compare chronologically — range start must be strictly before range end.
  const rangeInvalid = !!(rangeStart && rangeEnd && rangeStart >= rangeEnd);
  const selectedKey = useMemo(() => [...selected].sort().join(","), [selected]);

  // Recompute the portfolio panel whenever the selection (or cycle) changes.
  useEffect(() => {
    const sell = result?.dates.sell_expiry;
    if (!sell || !effectiveAccount || selectedRows.length === 0) { setPanel(null); return; }
    let cancelled = false;
    api.donchianPortfolio({
      broker_account_id: effectiveAccount, sell_expiry: sell, selected: selectedRows,
      hedge_otm_pct: hedgeOtm, hedge_beta_weight: betaWeight, portfolio_sl_pct: slPct,
      portfolio_target_enabled: targetEnabled, portfolio_target_pct: targetPct,
    }).then((p) => { if (!cancelled) { setPanel(p); if (p.basket_margin) setCapital((c) => Math.max(c, Math.ceil(p.basket_margin! * 1.1 / 100000) * 100000)); } })
      .catch(() => { if (!cancelled) setPanel(null); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedKey, result?.dates.sell_expiry, effectiveAccount, hedgeOtm, betaWeight, slPct, targetEnabled, targetPct]);

  const deploy = useMutation({
    mutationFn: () => {
      const sell = result!.dates.sell_expiry!;
      const legs: DonchianDeployLeg[] = [];
      for (const r of selectedRows) {
        const lps = r.lots ?? 1;
        if (r.ce && r.ce.premium != null && !r.ce.skip)
          legs.push({ underlying: r.symbol, right: "CE", strike: r.ce.strike, side: "sell", lots: lps, spot: r.spot ?? undefined, lot_size: r.lot_size, strike_step: r.strike_step ?? undefined });
        if (r.pe && r.pe.premium != null && !r.pe.skip)
          legs.push({ underlying: r.symbol, right: "PE", strike: r.pe.strike, side: "sell", lots: lps, spot: r.spot ?? undefined, lot_size: r.lot_size, strike_step: r.strike_step ?? undefined });
      }
      const h = panel?.hedge;
      if (h && h.nifty_lots > 0) {
        if (h.ce_strike) legs.push({ underlying: "NIFTY", right: "CE", strike: h.ce_strike, side: "buy", lots: h.nifty_lots, lot_size: h.nifty_lot_size });
        if (h.pe_strike) legs.push({ underlying: "NIFTY", right: "PE", strike: h.pe_strike, side: "buy", lots: h.nifty_lots, lot_size: h.nifty_lot_size });
      }
      return api.donchianDeploy({
        name: deployName || `Donchian Strangle ${sell}`,
        sell_expiry: sell, legs, capital,
        portfolio_sl_pct: slPct, portfolio_target_enabled: targetEnabled, portfolio_target_pct: targetPct,
        flip_delta: flipDelta, breach_buffer_pct: breachBuffer,
        mode, quote_source: "zerodha", broker_account_id: effectiveAccount,
        ignore_market_hours: false, auto: true,
      });
    },
    onSuccess: () => navigate("/live"),
  });

  const toggle = (sym: string) => setSelected((s) => { const n = new Set(s); n.has(sym) ? n.delete(sym) : n.add(sym); return n; });

  return (
    <div className="space-y-4">
      <p className="text-sm text-[var(--muted)]">
        Monthly basket short-strangle: per name SELL CE at last month's Donchian high and SELL PE at the low
        (cheap/far legs skipped), tail-hedged with notional-matched OTM NIFTY options and a −2% portfolio stop.
        Upload a Sensibull screener CSV (ATMIV / IVP / Event) and pick names. Needs a logged-in broker session.
      </p>

      <Panel className="p-5 space-y-4">
        <div className="flex items-center gap-3 flex-wrap">
          <label className="rounded-[10px] bg-[var(--chip)] text-[var(--chip-text)] px-3 py-1.5 text-sm cursor-pointer">
            📄 Upload screener CSV
            <input type="file" accept=".csv" className="hidden" onChange={(e) => {
              const f = e.target.files?.[0]; if (!f) return;
              f.text().then((t) => { setCsvRows(parseDonchianCsv(t)); setCsvName(f.name); });
            }} />
          </label>
          {csvName && (
            <span className="text-sm text-[var(--muted)]">
              {csvName} — {csvRows.length} names
              <button onClick={() => { setCsvRows([]); setCsvName(""); }} className="ml-2 underline text-[var(--accent-deep)]">clear</button>
            </span>
          )}
        </div>

        <div className="text-xs text-[var(--muted)]">
          Universe: the <span className="font-semibold text-[var(--strong)]">top 50 Nifty names ranked by free-float weight</span>
          {" "}(liquidity / options depth). Upload a Sensibull CSV to supply each name's IVP / ATMIV / Event.
        </div>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
          <label className="flex flex-col gap-1">Broker session
            <select className={inputCls} value={effectiveAccount ?? ""} onChange={(e) => setAccountId(Number(e.target.value))}>
              {sessioned.length === 0 && <option value="">No logged-in session</option>}
              {sessioned.map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}
            </select>
          </label>
          <label className="flex flex-col gap-1">IVP ≥
            <input type="number" className={inputCls} value={ivpMin} onChange={(e) => setIvpMin(Number(e.target.value))} />
          </label>
          <label className="flex flex-col gap-1">HV window (days)
            <input type="number" className={inputCls} value={hvWindow} onChange={(e) => setHvWindow(Number(e.target.value))} />
          </label>
          <label className="flex flex-col gap-1">Skip leg &lt; (% spot)
            <input type="number" step="0.1" className={inputCls} value={skipLegPct} onChange={(e) => setSkipLegPct(Number(e.target.value))} />
          </label>
          <label className="flex flex-col gap-1">Hedge OTM (%)
            <input type="number" step="0.5" className={inputCls} value={hedgeOtm} onChange={(e) => setHedgeOtm(Number(e.target.value))} />
          </label>
          <label className="flex flex-col gap-1">Portfolio stop (% notional)
            <input type="number" step="0.5" className={inputCls} value={slPct} onChange={(e) => setSlPct(Number(e.target.value))} />
          </label>
          <label className="flex flex-col gap-1">Lots / name
            <input type="number" className={inputCls} value={lots} onChange={(e) => setLots(Number(e.target.value))} />
          </label>
          <label className="flex items-center gap-2 mt-5">
            <input type="checkbox" checked={roundOut} onChange={(e) => setRoundOut(e.target.checked)} /> Round-out strikes
          </label>
          <label className="flex items-center gap-2 mt-5" title="Keep only names whose ATM IV exceeds their annualised HV">
            <input type="checkbox" checked={requireIvGtHv} onChange={(e) => setRequireIvGtHv(e.target.checked)} /> Require IV &gt; HV
          </label>
          <label className="flex items-center gap-2 mt-5" title="If spot has broken beyond the Donchian range the would-be ITM leg is skipped and the ATM opposite leg is sold instead (CE breakout → ATM PE)">
            <input type="checkbox" checked={breakoutAtm} onChange={(e) => setBreakoutAtm(e.target.checked)} /> Breakout → ATM leg
          </label>
        </div>

        {/* Cycle anchors (auto-resolved; override + re-run if needed). */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
          <label className="flex flex-col gap-1">Range start
            <input type="date" className={inputCls} value={rangeStart} onChange={(e) => setRangeStart(e.target.value)} />
          </label>
          <label className="flex flex-col gap-1">Range end
            <input type="date" className={inputCls} value={rangeEnd} onChange={(e) => setRangeEnd(e.target.value)} />
          </label>
          <label className="flex flex-col gap-1">Entry date
            <input type="date" className={inputCls} value={entryDate} onChange={(e) => setEntryDate(e.target.value)} />
          </label>
          <label className="flex flex-col gap-1">Sell expiry
            <input type="date" className={inputCls} value={sellExpiry} onChange={(e) => setSellExpiry(e.target.value)} />
          </label>
        </div>

        <div className="flex items-center gap-3">
          <button onClick={() => analyze.mutate()} disabled={analyze.isPending || !effectiveAccount || names.length === 0 || rangeInvalid}
            className="rounded bg-[var(--ft)] px-4 py-1.5 text-sm text-white disabled:opacity-50">
            {analyze.isPending ? "Analyzing…" : `Refresh (${names.length})`}
          </button>
          {rangeInvalid && <span className="text-sm text-[var(--danger)]">Range start must be before range end.</span>}
          {analyze.isError && <span className="text-sm text-[var(--danger)]">{(analyze.error as Error).message}</span>}
          {result?.error && <span className="text-sm text-[var(--warn-text)]">{result.error}</span>}
          {result && !result.error && (
            <span className="text-xs text-[var(--faint)]">
              as of {result.as_of} · range {result.dates.range_start}→{result.dates.range_end} · sell {result.dates.sell_expiry}
            </span>
          )}
        </div>
      </Panel>

      {result && rows.length > 0 && (
        <Panel className="p-4">
          <div className="overflow-x-auto">
            <table className="w-full text-sm tabular-nums whitespace-nowrap">
              <thead>
                <tr className="text-[var(--muted)] text-xs border-b border-[var(--divider)] text-left">
                  <th className="py-1.5 px-2"></th>
                  <th className="py-1.5 px-2">Stock</th>
                  <th className="py-1.5 px-2 text-right" title="Free-float index weight (24 Jun 2026)">FF wt</th>
                  <th className="py-1.5 px-2 text-right">IVP</th>
                  <th className="py-1.5 px-2 text-right">Spot</th>
                  <th className="py-1.5 px-2 text-right">Range L–H</th>
                  <th className="py-1.5 px-2">SELL PE</th>
                  <th className="py-1.5 px-2">SELL CE</th>
                  <th className="py-1.5 px-2 text-right">Margin</th>
                  <th className="py-1.5 px-2">Status</th>
                </tr>
              </thead>
              <tbody>
                {sortedRows.map((r) => {
                  const sel = SELECTABLE.has(r.status);
                  return (
                    <tr key={r.symbol} className={`border-b border-[var(--divider)]/40 ${sel ? "" : "opacity-55"}`}>
                      <td className="py-1.5 px-2">
                        <input type="checkbox" disabled={!sel} checked={selected.has(r.symbol)} onChange={() => toggle(r.symbol)} />
                      </td>
                      <td className="py-1.5 px-2 font-medium">
                        {r.symbol}
                        {deployedUnderlyings.has(r.symbol) && (
                          <Link to="/live" className="ml-2 align-middle rounded-full bg-[var(--chip)] text-[var(--chip-text)] px-1.5 py-0.5 text-[10px] font-semibold">● deployed</Link>
                        )}
                      </td>
                      <td className="py-1.5 px-2 text-right tabular-nums text-[var(--muted)]">{FF_WEIGHT[r.symbol] != null ? `${FF_WEIGHT[r.symbol].toFixed(2)}%` : "—"}</td>
                      <td className="py-1.5 px-2 text-right"
                        title={`ATM IV ${n1(r.atm_iv)}% · HV ${n1(r.hv)}%${r.beta != null ? ` · β ${r.beta.toFixed(2)}` : ""}`}>
                        {ivpMap.get(r.symbol)?.ivp ?? r.ivp ?? "—"}
                        {r.atm_iv != null && r.hv != null && (
                          <span className={r.atm_iv > r.hv ? "text-[var(--pos)]" : "text-[var(--warn-text)]"}> {r.atm_iv > r.hv ? "▲" : "▼"}</span>
                        )}
                      </td>
                      <td className="py-1.5 px-2 text-right">{n1(r.spot)}</td>
                      <td className="py-1.5 px-2 text-right text-xs text-[var(--muted)]">{r.range_low != null ? `${n1(r.range_low)}–${n1(r.range_high)}` : "—"}</td>
                      <td className="py-1.5 px-2 text-[var(--pos)]"><LegCell leg={r.pe} units={(r.lot_size ?? 0) * (r.lots ?? 1)} /></td>
                      <td className="py-1.5 px-2 text-[var(--danger)]"><LegCell leg={r.ce} units={(r.lot_size ?? 0) * (r.lots ?? 1)} /></td>
                      <td className="py-1.5 px-2 text-right">{money(r.margin)}</td>
                      <td className="py-1.5 px-2">
                        <StatusPill status={r.status} />
                        {(() => {
                          const why = r.reason || r.error || (r.status === "excluded:event" && r.event ? `event ${r.event}` : "");
                          return why ? <div className="text-[10px] text-[var(--faint)] mt-0.5 max-w-[200px] whitespace-normal leading-tight">{why}</div> : null;
                        })()}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="text-[11px] text-[var(--faint)] mt-2">
            Greyed rows are excluded (event / filter) or unsupported and not selectable. A "skip" leg is below the
            premium floor → that name runs single-leg. ⚑ = wide bid-ask spread (illiquid).
          </div>
        </Panel>
      )}

      {/* Portfolio panel + deploy (spec §8/§10). */}
      {selectedRows.length > 0 && (
        <Panel className="p-5 space-y-4">
          <div className="flex items-center justify-between flex-wrap gap-2">
            <h3 className="font-bold font-['Space_Grotesk'] text-lg">Portfolio ({selectedRows.length} names)</h3>
            <div className="flex items-center gap-4 flex-wrap">
              <label className="flex items-center gap-2 text-sm" title="Strike used when a breach rolls a name">
                Flip strike
                <select className={`${inputCls} text-sm`} value={flipDelta} onChange={(e) => setFlipDelta(e.target.value as "atm" | "30delta")}>
                  <option value="atm">ATM</option>
                  <option value="30delta">30Δ (live)</option>
                </select>
              </label>
              <label className="flex items-center gap-2 text-sm" title="Spot must clear a short strike by this % to flip; flips are capped to once per name per day">
                Breach buffer <input type="number" step="0.1" className={`${inputCls} w-16`} value={breachBuffer} onChange={(e) => setBreachBuffer(Number(e.target.value))} /> %
              </label>
              <label className="flex items-center gap-2 text-sm" title="Weight hedge lots by each name's beta vs NIFTY">
                <input type="checkbox" checked={betaWeight} onChange={(e) => setBetaWeight(e.target.checked)} /> Beta-weight hedge
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={targetEnabled} onChange={(e) => setTargetEnabled(e.target.checked)} />
                Target at <input type="number" className={`${inputCls} w-16`} value={targetPct} onChange={(e) => setTargetPct(Number(e.target.value))} /> % of premium
              </label>
            </div>
          </div>
          {panel ? (
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
              <div><div className="text-[var(--faint)] text-xs">Aggregate notional</div><div className="font-semibold tabular-nums">{money(panel.agg_notional)}</div></div>
              <div><div className="text-[var(--faint)] text-xs">Premium collected</div><div className="font-semibold tabular-nums">{money(panel.premium_collected)} <span className="text-[var(--muted)]">({pct(panel.premium_pct_of_notional)})</span></div></div>
              <div><div className="text-[var(--faint)] text-xs">Basket margin</div><div className="font-semibold tabular-nums">{money(panel.basket_margin)}</div></div>
              <div><div className="text-[var(--faint)] text-xs">Portfolio stop (−{slPct}%)</div><div className="font-semibold tabular-nums text-[var(--danger)]">−{money(panel.portfolio_sl_amount)}</div></div>
              <div className="col-span-2 md:col-span-4">
                <div className="text-[var(--faint)] text-xs">Index hedge (notional-matched)</div>
                <div className="font-semibold tabular-nums">
                  {panel.hedge.nifty_lots > 0
                    ? <>BUY {panel.hedge.nifty_lots} × NIFTY {panel.hedge.ce_strike} CE + {panel.hedge.pe_strike} PE (~{hedgeOtm}% OTM) · cost {money(panel.hedge.cost)}{" "}
                        <span className={panel.hedge.cap_flag ? "text-[var(--warn-text)]" : "text-[var(--muted)]"}>({pct(panel.hedge.cost_pct_of_premium)} of premium{panel.hedge.cap_flag ? " ⚑ over cap" : ""})</span></>
                    : "notional too small for a whole NIFTY hedge lot"}
                </div>
              </div>
            </div>
          ) : <div className="text-sm text-[var(--muted)]">Computing portfolio…</div>}

          <div className="flex items-center gap-3 flex-wrap border-t border-[var(--divider)] pt-4">
            <input className={`${inputCls} text-sm w-64`} placeholder="Deployment name" value={deployName} onChange={(e) => setDeployName(e.target.value)} />
            <label className="flex flex-col gap-1 text-xs text-[var(--faint)]">Capital
              <input type="number" className={`${inputCls} text-sm w-40`} value={capital} onChange={(e) => setCapital(Number(e.target.value))} />
            </label>
            <div className="flex rounded-[10px] overflow-hidden border border-[var(--field-border)]">
              {(["PAPER", "LIVE"] as const).map((m) => (
                <button key={m} onClick={() => setMode(m)} className={`px-3 py-1.5 text-sm ${mode === m ? "bg-[var(--accent)] text-white" : "bg-[var(--field)] text-[var(--muted)]"}`}>{m}</button>
              ))}
            </div>
            <button onClick={() => deploy.mutate()} disabled={deploy.isPending || !panel}
              className="rounded bg-[var(--ft)] px-4 py-1.5 text-sm text-white disabled:opacity-50 ml-auto">
              {deploy.isPending ? "Deploying…" : "Deploy basket + hedge"}
            </button>
            {deploy.isError && <span className="text-sm text-[var(--danger)] w-full">{(deploy.error as Error).message}</span>}
          </div>
        </Panel>
      )}
    </div>
  );
}
