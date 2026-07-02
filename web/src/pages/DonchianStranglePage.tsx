import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { api, brokers } from "../api/client";
import { Panel, SessionBanner } from "../components/redesign";
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

// v2: portfolio stop/target moved to a % -of-basket-margin basis (+ a leg-level premium target) —
// bump the key so stale v1 values (slPct=2 meant % of notional) don't load as margin %.
const PKEY = "donchian.state.v2";
function loadPersisted(): Record<string, unknown> {
  try {
    const cur = JSON.parse(localStorage.getItem(PKEY) || "{}") as Record<string, unknown>;
    // Migrate from v1 so bumping the key doesn't blank the screener: recover the last result
    // (it seeds the cycle-date fields), the uploaded CSV, and the chosen account — but NOT the
    // stop/target %s (their basis changed → let the new margin defaults apply).
    if (!cur.result || cur.csvRows === undefined) {
      const v1 = JSON.parse(localStorage.getItem("donchian.state.v1") || "{}") as Record<string, unknown>;
      if (!cur.result && v1.result) cur.result = v1.result;
      if (cur.csvRows === undefined && v1.csvRows) { cur.csvRows = v1.csvRows; cur.csvName = v1.csvName; }
      if (cur.accountId == null && v1.accountId != null) cur.accountId = v1.accountId;
    }
    return cur;
  } catch { return {}; }
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
  const { data: accounts = [], isError: brokersError } = useQuery({ queryKey: ["brokers"], queryFn: brokers.list });
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
  // Entry gates ported from the backtest loss study (0 = off) + VIX advisory threshold.
  const [minHvRatio, setMinHvRatio] = useState(saved.minHvRatio ?? 0);
  const [minWidth, setMinWidth] = useState(saved.minWidth ?? 0);
  const [vixWarn, setVixWarn] = useState(saved.vixWarn ?? 20);
  const [lots, setLots] = useState(saved.lots ?? 1);
  // Portfolio params.
  const [hedgeOtm, setHedgeOtm] = useState(saved.hedgeOtm ?? 4.5);
  const [betaWeight, setBetaWeight] = useState(saved.betaWeight ?? false);
  // Portfolio stop + profit target are now % of basket margin (see PKEY v2 note).
  const [slPct, setSlPct] = useState(saved.slPct ?? 4);
  const [targetEnabled, setTargetEnabled] = useState(saved.targetEnabled ?? true);
  const [targetPct, setTargetPct] = useState(saved.targetPct ?? 6);
  // Leg-level profit take: close a single short leg once it captures this % of its OWN premium.
  const [legTargetEnabled, setLegTargetEnabled] = useState(saved.legTargetEnabled ?? true);
  const [legTargetPct, setLegTargetPct] = useState(saved.legTargetPct ?? 80);
  const [flipDelta, setFlipDelta] = useState<"atm" | "30delta">(saved.flipDelta ?? "atm");
  const [breachBuffer, setBreachBuffer] = useState(saved.breachBuffer ?? 0.5);
  // Cycle overrides (blank = auto). Seed from the last persisted result so they show the resolved
  // cycle on reload (not an empty input that the browser renders as a faint "today" placeholder);
  // each Refresh re-syncs them to the cycle the backend actually used.
  const [rangeStart, setRangeStart] = useState<string>(saved.result?.dates?.range_start ?? "");
  const [rangeEnd, setRangeEnd] = useState<string>(saved.result?.dates?.range_end ?? "");
  const [entryDate, setEntryDate] = useState<string>(saved.result?.dates?.entry_date ?? "");
  const [sellExpiry, setSellExpiry] = useState<string>(saved.result?.dates?.sell_expiry ?? "");
  // Deploy.
  const [mode, setMode] = useState<"PAPER" | "LIVE">("PAPER");
  const [deployName, setDeployName] = useState("");
  const [capital, setCapital] = useState(5_000_000);

  const [result, setResult] = useState<DonchianResult | null>(saved.result ?? null);
  const [selected, setSelected] = useState<Set<string>>(
    new Set((saved.result?.rows ?? []).filter((r: DonchianRow) => SELECTABLE.has(r.status)).map((r: DonchianRow) => r.symbol)),
  );
  const [panel, setPanel] = useState<DonchianPanel | null>(null);
  // Why the panel is null, so we never show "Computing…" for a not-ready (no-session /
  // no-selection / errored) state — the bug that made a blocked screener look hung.
  const [portfolioLoading, setPortfolioLoading] = useState(false);
  const [portfolioError, setPortfolioError] = useState<string | null>(null);
  // Manual CE/PE strike overrides per name (incl. for excluded rows). undefined = use the resolved
  // strike; null = don't sell that leg; a number = sell that strike (priced live at entry).
  const [strikeOv, setStrikeOv] = useState<Record<string, { ce?: number | null; pe?: number | null }>>({});

  // Persist screener inputs + last result so the table survives a reload.
  useEffect(() => {
    localStorage.setItem(PKEY, JSON.stringify({
      accountId, csvRows, csvName, ivpMin, hvWindow, skipLegPct, roundOut, requireIvGtHv, breakoutAtm,
      minHvRatio, minWidth, vixWarn,
      lots, hedgeOtm, betaWeight, slPct, targetEnabled, targetPct, legTargetEnabled, legTargetPct,
      flipDelta, breachBuffer, result,
    }));
  }, [accountId, csvRows, csvName, ivpMin, hvWindow, skipLegPct, roundOut, requireIvGtHv, breakoutAtm,
      minHvRatio, minWidth, vixWarn,
      lots, hedgeOtm, betaWeight, slPct, targetEnabled, targetPct, legTargetEnabled, legTargetPct,
      flipDelta, breachBuffer, result]);

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
        min_hv_ratio: minHvRatio, min_channel_width_pct: minWidth,
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
  // Any selected row is deployable (incl. excluded ones). Apply manual strike overrides and un-skip
  // a selected row's resolved legs (selection = intent to sell). Overridden legs price live at entry.
  const selectedRows = useMemo(() => {
    const mk = (cur: DonchianRow["ce"], ov: number | null | undefined): DonchianRow["ce"] => {
      if (ov === null) return null;                       // explicitly skipped
      const strike = ov ?? (cur ? cur.strike : null);     // override, else the resolved strike
      if (strike == null) return null;
      const known = !!cur && cur.strike === strike && cur.premium != null;
      return { strike, premium: known ? cur!.premium : null, bid: known ? cur!.bid ?? null : null,
               ask: known ? cur!.ask ?? null : null, oi: known ? cur!.oi : undefined, skip: false };
    };
    return rows.filter((r) => selected.has(r.symbol)).map((r) => {
      const ov = strikeOv[r.symbol];
      return { ...r, ce: mk(r.ce, ov?.ce), pe: mk(r.pe, ov?.pe) };
    });
  }, [rows, selected, strikeOv]);
  // ISO date strings compare chronologically — range start must be strictly before range end.
  const rangeInvalid = !!(rangeStart && rangeEnd && rangeStart >= rangeEnd);
  const selectedKey = useMemo(() => [...selected].sort().join(","), [selected]);

  // Recompute the portfolio panel whenever the selection (or cycle) changes.
  useEffect(() => {
    const sell = result?.dates.sell_expiry;
    if (!sell || !effectiveAccount || selectedRows.length === 0) { setPanel(null); setPortfolioError(null); setPortfolioLoading(false); return; }
    let cancelled = false;
    setPortfolioLoading(true); setPortfolioError(null);
    api.donchianPortfolio({
      broker_account_id: effectiveAccount, sell_expiry: sell, selected: selectedRows,
      hedge_otm_pct: hedgeOtm, hedge_beta_weight: betaWeight, portfolio_sl_pct: slPct,
      portfolio_target_enabled: targetEnabled, portfolio_target_pct: targetPct,
      portfolio_basis: "margin",
    }).then((p) => { if (!cancelled) { setPanel(p); setPortfolioLoading(false); if (p.basket_margin) setCapital((c) => Math.max(c, Math.ceil(p.basket_margin! * 1.1 / 100000) * 100000)); } })
      .catch((e) => { if (!cancelled) { setPanel(null); setPortfolioLoading(false); setPortfolioError((e as Error).message); } });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedKey, result?.dates.sell_expiry, effectiveAccount, hedgeOtm, betaWeight, slPct, targetEnabled, targetPct]);

  const deploy = useMutation({
    mutationFn: () => {
      const sell = result!.dates.sell_expiry!;
      const legs: DonchianDeployLeg[] = [];
      for (const r of selectedRows) {
        const lps = r.lots ?? 1;
        if (r.ce && !r.ce.skip)  // premium not required — a short fills at the live bid at entry
          legs.push({ underlying: r.symbol, right: "CE", strike: r.ce.strike, side: "sell", lots: lps, spot: r.spot ?? undefined, lot_size: r.lot_size, strike_step: r.strike_step ?? undefined });
        if (r.pe && !r.pe.skip)
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
        portfolio_basis: "margin", leg_target_enabled: legTargetEnabled, leg_target_pct: legTargetPct,
        flip_delta: flipDelta, breach_buffer_pct: breachBuffer,
        mode, quote_source: "zerodha", broker_account_id: effectiveAccount,
        ignore_market_hours: false, auto: true,
      });
    },
    // Jump straight to the new deployment's Live detail so it shows up immediately (with the
    // "entering N/M legs" progress banner) instead of landing on the list.
    onSuccess: (res) => navigate(res?.run_id ? `/live/${res.run_id}` : "/live"),
  });

  const toggle = (sym: string) => setSelected((s) => { const n = new Set(s); n.has(sym) ? n.delete(sym) : n.add(sym); return n; });
  // Select-all / deselect-all (header checkbox) over the deployable rows.
  const deployable = useMemo(() => rows.filter((r) => SELECTABLE.has(r.status)), [rows]);
  const allChecked = deployable.length > 0 && deployable.every((r) => selected.has(r.symbol));
  const toggleAll = () => setSelected((s) => {
    if (allChecked) return new Set();
    const n = new Set(s); deployable.forEach((r) => n.add(r.symbol)); return n;
  });

  // A selected row's CE/PE cell becomes an editable strike picker (any listed strike, or "skip");
  // an overridden strike prices live at entry. Unselected rows keep the read-only LegCell.
  const renderLeg = (r: DonchianRow, side: "ce" | "pe") => {
    const leg = side === "ce" ? r.ce : r.pe;
    const units = (r.lot_size ?? 0) * (r.lots ?? 1);
    if (!selected.has(r.symbol)) return <LegCell leg={leg} units={units} />;
    const ov = strikeOv[r.symbol]?.[side];
    const eff = ov !== undefined ? ov : (leg ? leg.strike : null);
    const known = !!leg && eff === leg.strike && leg.premium != null;
    const color = side === "ce" ? "var(--danger)" : "var(--pos)";
    return (
      <div className="flex flex-col gap-0.5">
        <select className="bg-[var(--field)] border border-[var(--field-border)] rounded px-1.5 py-1 text-xs tabular-nums max-w-[120px]"
          style={{ color: eff == null ? "var(--faint)" : color }} value={eff ?? ""}
          onChange={(e) => { const v = e.target.value === "" ? null : Number(e.target.value);
            setStrikeOv((m) => ({ ...m, [r.symbol]: { ...m[r.symbol], [side]: v } })); }}>
          <option value="">— skip</option>
          {(r.strikes ?? []).map((k) => <option key={k} value={k}>{k}</option>)}
        </select>
        <span className="text-[10px] text-[var(--muted)] tabular-nums">
          {eff == null ? "—" : known ? `${money((leg!.premium ?? 0) * units)} recd` : "live @ entry"}
        </span>
      </div>
    );
  };

  return (
    <div className="space-y-4">
      <p className="text-sm text-[var(--muted)]">
        Monthly basket short-strangle: per name SELL CE at last month's Donchian high and SELL PE at the low
        (cheap/far legs skipped), tail-hedged with notional-matched OTM NIFTY options and a −2% portfolio stop.
        Upload a Sensibull screener CSV (ATMIV / IVP / Event) and pick names. Needs a logged-in broker session.
      </p>

      <SessionBanner backendDown={brokersError} hasSession={sessioned.length > 0} />

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
          <label className="flex flex-col gap-1" title="The logged-in Zerodha account used for live option chains, pricing and basket margin">Broker session
            <select className={inputCls} value={effectiveAccount ?? ""} onChange={(e) => setAccountId(Number(e.target.value))}>
              {sessioned.length === 0 && <option value="">No logged-in session</option>}
              {sessioned.map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}
            </select>
          </label>
          <label className="flex flex-col gap-1" title="Implied Volatility Percentile floor — only sell names whose IVP (from the Sensibull CSV) is at least this. Higher = richer IV vs the name's own 1-year history">IVP ≥
            <input type="number" className={inputCls} value={ivpMin} onChange={(e) => setIvpMin(Number(e.target.value))} />
          </label>
          <label className="flex flex-col gap-1" title="Look-back (trading days) for each name's historical (realised) volatility, used by the 'Require IV > HV' filter">HV window (days)
            <input type="number" className={inputCls} value={hvWindow} onChange={(e) => setHvWindow(Number(e.target.value))} />
          </label>
          <label className="flex flex-col gap-1" title="Premium floor: if a leg's credit is below this % of spot it's too cheap to sell — that name runs single-leg (the thin leg is skipped)">Skip leg &lt; (% spot)
            <input type="number" step="0.1" className={inputCls} value={skipLegPct} onChange={(e) => setSkipLegPct(Number(e.target.value))} />
          </label>
          <label className="flex flex-col gap-1" title="Vol-compression gate (backtest study): exclude names whose recent realized vol (HV over the window above) is below this fraction of their 3-month vol. Squeezed names breach the most — 0.85 was the tested setting. 0 = off">HV ratio ≥ (0=off)
            <input type="number" step="0.05" className={inputCls} value={minHvRatio} onChange={(e) => setMinHvRatio(Number(e.target.value))} />
          </label>
          <label className="flex flex-col gap-1" title="Tight-channel gate (backtest study): exclude names whose Donchian range is narrower than this % of spot — the strikes would hug spot and flip on any normal move. 8 was the tested setting. 0 = off">Channel ≥ % (0=off)
            <input type="number" step="0.5" className={inputCls} value={minWidth} onChange={(e) => setMinWidth(Number(e.target.value))} />
          </label>
          <label className="flex flex-col gap-1" title="Market-stress advisory: when live India VIX is above this at screen time, a warning suggests half lots or skipping the cycle (the backtest's VIX rule; live sizing stays your call)">VIX warn &gt;
            <input type="number" step="1" className={inputCls} value={vixWarn} onChange={(e) => setVixWarn(Number(e.target.value))} />
          </label>
          <label className="flex flex-col gap-1" title="How far out-of-the-money the long NIFTY hedge legs are bought (% from NIFTY spot). The hedge is notional-matched to the basket">Hedge OTM (%)
            <input type="number" step="0.5" className={inputCls} value={hedgeOtm} onChange={(e) => setHedgeOtm(Number(e.target.value))} />
          </label>
          <label className="flex flex-col gap-1" title="Flatten the whole basket if its combined loss reaches this % of the basket margin">Portfolio stop (% margin)
            <input type="number" step="0.5" className={inputCls} value={slPct} onChange={(e) => setSlPct(Number(e.target.value))} />
          </label>
          <label className="flex flex-col gap-1" title="Lot-sets sold per name (the contracts entered = each name's F&O lot size × this)">Lots / name
            <input type="number" className={inputCls} value={lots} onChange={(e) => setLots(Number(e.target.value))} />
          </label>
          <label className="flex items-center gap-2 mt-5" title="Round the Donchian high/low to the nearest listed strike step instead of selling the exact level">
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
          <label className="flex flex-col gap-1" title="Start of the Donchian look-back window — the prior month's high/low set each name's short strikes. Auto-resolved from the monthly calendar; override to re-run a specific cycle">Range start
            <input type="date" className={inputCls} value={rangeStart} onChange={(e) => setRangeStart(e.target.value)} />
          </label>
          <label className="flex flex-col gap-1" title="End of the Donchian look-back window. Auto-resolved; must be after Range start">Range end
            <input type="date" className={inputCls} value={rangeEnd} onChange={(e) => setRangeEnd(e.target.value)} />
          </label>
          <label className="flex flex-col gap-1" title="The date the basket is entered / priced as of. Auto-resolved; override to re-run a specific cycle">Entry date
            <input type="date" className={inputCls} value={entryDate} onChange={(e) => setEntryDate(e.target.value)} />
          </label>
          <label className="flex flex-col gap-1" title="The monthly expiry all short legs (and the NIFTY hedge) are sold for. Auto-resolved from the listed expiries">Sell expiry
            <input type="date" className={inputCls} value={sellExpiry} onChange={(e) => setSellExpiry(e.target.value)} />
          </label>
        </div>

        <div className="flex items-center gap-3">
          <button onClick={() => analyze.mutate()} disabled={analyze.isPending || !effectiveAccount || names.length === 0 || rangeInvalid}
            title={!effectiveAccount ? "Log in a broker session on Brokers first" : rangeInvalid ? "Range start must be before range end" : names.length === 0 ? "No names to screen" : "Re-run the screen"}
            className="rounded bg-[var(--ft)] px-4 py-1.5 text-sm text-white disabled:opacity-50">
            {analyze.isPending ? "Analyzing…" : `Refresh (${names.length})`}
          </button>
          {rangeInvalid && <span className="text-sm text-[var(--danger)]">Range start must be before range end.</span>}
          {analyze.isError && <span className="text-sm text-[var(--danger)]">{(analyze.error as Error).message}</span>}
          {result?.error && <span className="text-sm text-[var(--warn-text)]">{result.error}</span>}
          {result && !result.error && (
            <span className="text-xs text-[var(--faint)]">
              as of {result.as_of} · range {result.dates.range_start}→{result.dates.range_end} · sell {result.dates.sell_expiry}
              {result.vix != null && <> · VIX {result.vix.toFixed(1)}</>}
            </span>
          )}
          {result?.vix != null && vixWarn > 0 && result.vix > vixWarn && (
            <span className="rounded-md px-2 py-1 text-xs font-medium"
              style={{ background: "var(--warn-bg)", color: "var(--warn-text)" }}>
              ⚠ VIX {result.vix.toFixed(1)} &gt; {vixWarn} — stressed month: consider half lots or
              sitting this cycle out (the backtest's VIX rule cut drawdown ~50%)
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
                  <th className="py-1.5 px-2"><input type="checkbox" checked={allChecked} onChange={toggleAll} title="Select / deselect all deployable" /></th>
                  <th className="py-1.5 px-2">Stock</th>
                  <th className="py-1.5 px-2 text-right" title="Free-float index weight (24 Jun 2026)">FF wt</th>
                  <th className="py-1.5 px-2 text-right">IVP</th>
                  <th className="py-1.5 px-2 text-right">Spot</th>
                  <th className="py-1.5 px-2 text-right">Range L–H</th>
                  <th className="py-1.5 px-2">SELL PE</th>
                  <th className="py-1.5 px-2">SELL CE</th>
                  <th className="py-1.5 px-2 text-right" title="Premium collected (CE + PE) at current marks">Premium</th>
                  <th className="py-1.5 px-2 text-right">Margin</th>
                  <th className="py-1.5 px-2">Status</th>
                </tr>
              </thead>
              <tbody>
                {sortedRows.map((r) => {
                  const sel = SELECTABLE.has(r.status) || selected.has(r.symbol);
                  return (
                    <tr key={r.symbol} className={`border-b border-[var(--divider)]/40 ${sel ? "" : "opacity-55"}`}>
                      <td className="py-1.5 px-2">
                        <input type="checkbox" checked={selected.has(r.symbol)} onChange={() => toggle(r.symbol)} />
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
                      <td className="py-1.5 px-2 text-right text-xs text-[var(--muted)]"
                        title={r.hv_ratio != null ? `channel width ${r.width_pct?.toFixed(1)}% of spot · HV ratio ${r.hv_ratio.toFixed(2)}` : undefined}>
                        {r.range_low != null ? `${n1(r.range_low)}–${n1(r.range_high)}` : "—"}
                        {r.width_pct != null && (
                          <span className={minWidth > 0 && r.width_pct < minWidth ? "text-[var(--warn-text)]" : ""}>
                            {" "}({r.width_pct.toFixed(1)}%)
                          </span>
                        )}
                      </td>
                      <td className="py-1.5 px-2 text-[var(--pos)]">{renderLeg(r, "pe")}</td>
                      <td className="py-1.5 px-2 text-[var(--danger)]">{renderLeg(r, "ce")}</td>
                      <td className="py-1.5 px-2 text-right text-[var(--pos)] font-medium">{(() => {
                        const u = (r.lot_size ?? 0) * (r.lots ?? 1);
                        const c = (r.ce && !r.ce.skip && r.ce.premium != null ? r.ce.premium : 0) + (r.pe && !r.pe.skip && r.pe.premium != null ? r.pe.premium : 0);
                        return c > 0 ? money(c * u) : <span className="text-[var(--faint)]">—</span>;
                      })()}</td>
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
              <label className="flex items-center gap-2 text-sm" title="Flatten the whole basket when its combined P&L (all legs + hedge) reaches this % of the basket margin">
                <input type="checkbox" checked={targetEnabled} onChange={(e) => setTargetEnabled(e.target.checked)} />
                Portfolio target <input type="number" step="0.5" className={`${inputCls} w-16`} value={targetPct} onChange={(e) => setTargetPct(Number(e.target.value))} /> % of margin
              </label>
              <label className="flex items-center gap-2 text-sm" title="Close an individual short leg once it has captured this % of its OWN premium (premium decay); the opposite leg of a strangle stays open">
                <input type="checkbox" checked={legTargetEnabled} onChange={(e) => setLegTargetEnabled(e.target.checked)} />
                Leg target <input type="number" className={`${inputCls} w-16`} value={legTargetPct} onChange={(e) => setLegTargetPct(Number(e.target.value))} /> % of premium
              </label>
            </div>
          </div>
          {panel ? (
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
              <div><div className="text-[var(--faint)] text-xs">Aggregate notional</div><div className="font-semibold tabular-nums">{money(panel.agg_notional)}</div></div>
              <div><div className="text-[var(--faint)] text-xs">Premium collected</div><div className="font-semibold tabular-nums">{money(panel.premium_collected)} <span className="text-[var(--muted)]">({pct(panel.premium_pct_of_notional)})</span></div></div>
              <div><div className="text-[var(--faint)] text-xs">Basket margin</div><div className="font-semibold tabular-nums">{money(panel.basket_margin)}</div></div>
              <div><div className="text-[var(--faint)] text-xs">Portfolio stop (−{slPct}% margin)</div><div className="font-semibold tabular-nums text-[var(--danger)]">−{money(panel.portfolio_sl_amount)}</div></div>
              {panel.portfolio_target_amount != null && (
                <div><div className="text-[var(--faint)] text-xs">Portfolio target (+{targetPct}% margin)</div><div className="font-semibold tabular-nums text-[var(--pos)]">+{money(panel.portfolio_target_amount)}</div></div>
              )}
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
          ) : (
            <div className="text-sm text-[var(--muted)]">
              {portfolioLoading ? "Computing portfolio…"
                : portfolioError ? <span className="text-[var(--danger)]">Couldn't price the basket: {portfolioError}</span>
                : !effectiveAccount ? "Log in a broker session (Brokers) to price the basket."
                : selectedRows.length === 0 ? "Select one or more names above to build the portfolio."
                : "Computing portfolio…"}
            </div>
          )}

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
              title={!panel ? "Build the portfolio first — needs a broker session and at least one selected name" : "Deploy this basket + hedge"}
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
