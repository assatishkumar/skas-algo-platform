import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, brokers } from "../api/client";
import { Panel } from "../components/redesign";
import { formatInr } from "../lib/format";
import type { FibRetResult, FibRetRow, OptionChain, OptionChainLeg, OptionsTradeDeploy } from "../types";

const n2 = (v?: number | null) => (v == null ? "—" : v.toFixed(2));
const n1 = (v?: number | null) => (v == null ? "—" : v.toFixed(1));
const pct = (v?: number | null) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`);
const money = (v?: number | null) => (v == null ? "—" : formatInr(v));

/** Relative bid-ask spread (% of mid). null when one-sided / missing. */
function spreadPct(bid?: number | null, ask?: number | null): number | null {
  if (!bid || !ask || bid <= 0 || ask <= 0 || ask < bid) return null;
  const mid = (bid + ask) / 2;
  return mid > 0 ? ((ask - bid) / mid) * 100 : null;
}
const isLiquid = (s: number | null) => s != null && s <= 10;

interface CsvRow {
  symbol: string;
  ivp?: number;
  atmIv?: number;
  futPrice?: number;
  pcr?: number;
}

function parseScreenerCsv(text: string): CsvRow[] {
  const lines = text.split(/\r?\n/).filter((l) => l.trim());
  if (!lines.length) return [];
  const header = lines[0].split(",").map((h) => h.trim().toLowerCase());
  const col = (name: string) => header.indexOf(name);
  const iSym = col("instrument");
  if (iSym < 0) return [];
  const iIvp = col("ivpercentile");
  const iIv = col("atmiv");
  const iFut = col("futureprice");
  const iPcr = col("pcr");
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
    out.push({ symbol, ivp: num(iIvp), atmIv: num(iIv), futPrice: num(iFut), pcr: num(iPcr) });
  }
  return out;
}

const inputCls = "rounded-[10px] bg-[var(--field)] border border-[var(--field-border)] px-2 py-1 text-[var(--strong)]";

function legAt(chain: OptionChain | undefined, strike: number, right: "CE" | "PE"): OptionChainLeg | null {
  const r = chain?.rows.find((x) => x.strike === strike);
  return r ? (right === "CE" ? r.ce : r.pe) : null;
}

/** Premium / bid-ask / spread display + liquidity flag for one option leg. */
function LegLine({ leg }: { leg: OptionChainLeg | null }) {
  const prem = leg?.ltp ?? leg?.close ?? null;
  const s = spreadPct(leg?.bid, leg?.ask);
  const liq = isLiquid(s);
  return (
    <div className="flex flex-wrap items-center gap-x-3 text-xs">
      <span className="text-[var(--muted)]">prem <span className="text-[var(--strong)]">{n2(prem)}</span></span>
      <span className="text-[var(--muted)]">bid/ask <span className="text-[var(--strong)]">{n2(leg?.bid)} / {n2(leg?.ask)}</span></span>
      <span className={liq ? "text-[var(--pos)]" : "text-[var(--warn-text)]"}>
        spread {s == null ? "—" : `${s.toFixed(1)}%`} {liq ? "" : "⚑ illiquid"}
      </span>
      {leg?.oi != null && <span className="text-[var(--faint)]">OI {leg.oi.toLocaleString("en-IN")}</span>}
    </div>
  );
}

/** Confirm + deploy one row as a custom_options short leg (+ optional long hedge), with an
 *  in-modal live-chain strike picker and bid-ask liquidity checks. */
function DeployPanel({
  row,
  result,
  accountId,
  onClose,
}: {
  row: FibRetRow;
  result: FibRetResult;
  accountId: number;
  onClose: () => void;
}) {
  const side = (row.side ?? "CE") as "CE" | "PE";
  const [mode, setMode] = useState("PAPER");
  const [lots, setLots] = useState(row.lots ?? 1);
  const [name, setName] = useState(`${row.symbol}_FibRet`);
  const [shortStrike, setShortStrike] = useState<number>(row.strike ?? 0);
  const [hedgeOn, setHedgeOn] = useState(false);
  const [hedgeStrike, setHedgeStrike] = useState<number | null>(null);

  const chainQ = useQuery({
    queryKey: ["fibChain", accountId, row.symbol, row.expiry],
    queryFn: () => api.optionsLiveChain(row.symbol, row.expiry!, accountId),
    enabled: !!row.expiry,
    retry: false,
    staleTime: 15_000,
  });
  const chain = chainQ.data;
  const strikes = useMemo(
    () => (chain?.rows ?? []).map((r) => r.strike).sort((a, b) => a - b),
    [chain],
  );
  // Hedge is a LONG leg further OTM than the short: above for a short call, below for a short put.
  const hedgeCands = useMemo(
    () => (side === "CE" ? strikes.filter((k) => k > shortStrike) : strikes.filter((k) => k < shortStrike).reverse()),
    [strikes, shortStrike, side],
  );

  const enableHedge = () => {
    setHedgeOn(true);
    if (hedgeStrike == null && hedgeCands.length) setHedgeStrike(hedgeCands[Math.min(2, hedgeCands.length - 1)]);
  };

  const shortLeg = legAt(chain, shortStrike, side);
  const hedgeLeg = hedgeOn && hedgeStrike != null ? legAt(chain, hedgeStrike, side) : null;
  const lot = chain?.lot_size || row.lot_size || 0;
  const shortPrem = shortLeg?.ltp ?? shortLeg?.close ?? row.premium ?? 0;
  const hedgePrem = hedgeLeg?.ltp ?? hedgeLeg?.close ?? 0;
  const netCredit = (shortPrem - (hedgeOn ? hedgePrem : 0)) * lot * lots;
  const targetAmt = (result.target_pct / 100) * netCredit; // book X% of the premium collected
  const shortIlliquid = chain ? !isLiquid(spreadPct(shortLeg?.bid, shortLeg?.ask)) : false;
  const spot = chain?.spot ?? row.spot ?? null;
  const stopCushion =
    spot != null && row.stop_level != null
      ? (side === "CE" ? (row.stop_level - spot) / spot : (spot - row.stop_level) / spot) * 100
      : row.cushion_to_stop_pct ?? null;

  const legs = useMemo<OptionsTradeDeploy["legs"]>(() => {
    const l: OptionsTradeDeploy["legs"] = [{ right: side, strike: shortStrike, side: "sell", lots }];
    if (hedgeOn && hedgeStrike != null) l.push({ right: side, strike: hedgeStrike, side: "buy", lots });
    return l;
  }, [side, shortStrike, hedgeOn, hedgeStrike, lots]);

  // Live margin for the exact structure (real Zerodha basket if sessioned, else model) — shown in
  // place of an arbitrary capital, and used as the deployment's capital.
  const marginQ = useQuery({
    queryKey: ["fibMargin", row.symbol, row.expiry, accountId, lot, JSON.stringify(legs)],
    queryFn: () => api.optionTradeMargin({ underlying: row.symbol, expiry: row.expiry!, lot_size: lot, legs, broker_account_id: accountId }),
    enabled: !!row.expiry && lot > 0,
    retry: false,
  });
  const margin = marginQ.data?.margin ?? null;

  const deploy = useMutation({
    mutationFn: () => {
      const body: OptionsTradeDeploy = {
        name,
        underlying: row.symbol,
        expiry: row.expiry!,
        legs,
        lot_size: lot,
        capital: Math.round(margin ?? row.margin ?? 1_000_000),
        spot_upper: side === "CE" ? row.stop_level : null,
        spot_lower: side === "PE" ? row.stop_level : null,
        target_pct: result.target_pct,
        mode,
        quote_source: "zerodha",
        broker_account_id: accountId,
        ignore_market_hours: true,
        auto: true,
        notes: `FibRet ${side} ${shortStrike}${hedgeOn && hedgeStrike != null ? ` / hedge ${hedgeStrike}` : ""} · stop spot ${n2(row.stop_level)}`,
      };
      return api.deployOptionTrade(body);
    },
  });

  return (
    <div className="fixed inset-0 z-30 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div
        className="w-full max-w-lg max-h-[90vh] overflow-y-auto rounded-xl border border-[var(--border)] bg-[var(--menu)] p-4 shadow-2xl space-y-3"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <div className="font-medium text-[var(--strong)]">
            Deploy {row.symbol} — SELL {side}
          </div>
          <button onClick={onClose} className="text-[var(--faint)] hover:text-[var(--strong)] px-1">×</button>
        </div>

        {/* short leg + strike picker */}
        <div className="rounded-md border border-[var(--divider)] p-2.5 space-y-1.5">
          <div className="flex items-center gap-2 text-sm">
            <span className="text-[var(--danger)] font-medium w-16">SELL {side}</span>
            {chain ? (
              <select value={shortStrike} onChange={(e) => setShortStrike(Number(e.target.value))} className={inputCls}>
                {strikes.map((k) => <option key={k} value={k}>{k}</option>)}
              </select>
            ) : (
              <span className="text-[var(--strong)]">{shortStrike}</span>
            )}
            <span className="text-xs text-[var(--faint)]">× {lot} × {lots} lots</span>
          </div>
          {chainQ.isLoading && <div className="text-xs text-[var(--faint)]">Loading live chain…</div>}
          {chainQ.isError && <div className="text-xs text-[var(--warn-text)]">Couldn't load live chain — deploying at the suggested strike.</div>}
          {chain && <LegLine leg={shortLeg} />}
        </div>

        {/* hedge */}
        <div className="rounded-md border border-[var(--divider)] p-2.5 space-y-1.5">
          {!hedgeOn ? (
            <button onClick={enableHedge} className="text-sm text-[var(--accent-deep)] hover:underline" disabled={!hedgeCands.length}>
              + Buy a hedge ({side === "CE" ? "long call above" : "long put below"}){!hedgeCands.length ? " — no strikes" : ""}
            </button>
          ) : (
            <>
              <div className="flex items-center gap-2 text-sm">
                <span className="text-[var(--pos)] font-medium w-16">BUY {side}</span>
                <select value={hedgeStrike ?? ""} onChange={(e) => setHedgeStrike(Number(e.target.value))} className={inputCls}>
                  {hedgeCands.map((k) => <option key={k} value={k}>{k}</option>)}
                </select>
                <button onClick={() => { setHedgeOn(false); setHedgeStrike(null); }} className="text-xs text-[var(--faint)] hover:text-[var(--strong)] underline">remove</button>
              </div>
              {chain && <LegLine leg={hedgeLeg} />}
            </>
          )}
        </div>

        {/* summary */}
        <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm">
          <div className="flex justify-between"><span className="text-[var(--muted)]">Spot</span><span>{n2(spot)}</span></div>
          <div className="flex justify-between"><span className="text-[var(--muted)]">Expiry</span><span>{row.expiry} ({row.dte}d)</span></div>
          <div className="flex justify-between">
            <span className="text-[var(--muted)]">Stop (spot {side === "CE" ? "≥" : "≤"})</span>
            <span>{n2(row.stop_level)}{stopCushion != null ? ` (${stopCushion >= 0 ? "+" : ""}${stopCushion.toFixed(1)}%)` : ""}</span>
          </div>
          <div className="flex justify-between"><span className="text-[var(--muted)]">{netCredit >= 0 ? "Net credit" : "Net debit"}</span><span className={netCredit >= 0 ? "text-[var(--pos)]" : "text-[var(--danger)]"}>{money(Math.abs(netCredit))}</span></div>
          <div className="flex justify-between"><span className="text-[var(--muted)]">Target (book {result.target_pct}%)</span><span className="text-[var(--pos)]">{money(targetAmt)}</span></div>
          <div className="flex justify-between"><span className="text-[var(--muted)]">Margin{marginQ.data?.source ? ` (${marginQ.data.source === "zerodha" ? "basket" : "est"})` : ""}</span><span>{marginQ.isLoading ? "…" : money(margin)}</span></div>
        </div>

        {shortIlliquid && (
          <div className="text-xs text-[var(--warn-text)]">⚑ The selected short strike has a wide bid-ask spread (&gt;10%) — consider another strike. You can still deploy.</div>
        )}
        {row.out_of_range && shortStrike === row.strike && (
          <div className="text-xs text-[var(--warn-text)]">The suggested strike is the chain edge ({row.note ?? "far OTM"}). Pick a nearer strike above if you want a tradeable contract.</div>
        )}

        <div className="flex flex-wrap items-center gap-3 text-sm">
          <label className="flex items-center gap-1.5">Mode
            <select value={mode} onChange={(e) => setMode(e.target.value)} className={inputCls}>
              <option value="PAPER">PAPER</option>
              <option value="LIVE">LIVE (real money)</option>
            </select>
          </label>
          <label className="flex items-center gap-1.5">Lots
            <input type="number" min={1} value={lots} onChange={(e) => setLots(Math.max(1, Number(e.target.value) || 1))} className={`w-16 ${inputCls}`} />
          </label>
        </div>
        <input value={name} onChange={(e) => setName(e.target.value)} className={`w-full text-sm ${inputCls}`} placeholder="Deployment name" />
        {mode === "LIVE" && (
          <div className="text-xs text-[var(--warn-text)]">Real-money LIVE also requires the account to be armed and trading enabled.</div>
        )}
        {deploy.isError && <div className="text-xs text-[var(--danger)]">{(deploy.error as Error).message}</div>}
        {deploy.isSuccess ? (
          <div className="text-sm text-[var(--pos)]">Deployed. <Link to="/live" className="underline">Open the Live tab →</Link></div>
        ) : (
          <div className="flex gap-2">
            <button onClick={() => deploy.mutate()} disabled={deploy.isPending}
              className="rounded bg-[var(--ft)] px-3 py-1.5 text-sm text-white disabled:opacity-50">
              {deploy.isPending ? "Deploying…" : `Deploy (${mode})`}
            </button>
            <button onClick={onClose} className="rounded bg-[var(--chip)] text-[var(--chip-text)] hover:opacity-80 px-3 py-1.5 text-sm">Cancel</button>
          </div>
        )}
      </div>
    </div>
  );
}

type Sort = { col: string; dir: 1 | -1 };

function Th({ children, right, col, sort, onSort }: {
  children?: React.ReactNode; right?: boolean; col?: string; sort?: Sort | null; onSort?: (c: string) => void;
}) {
  const active = !!col && sort?.col === col;
  return (
    <th
      onClick={col ? () => onSort?.(col) : undefined}
      className={`font-medium py-1.5 px-2 ${right ? "text-right" : "text-left"} ${col ? "cursor-pointer select-none hover:text-[var(--strong)]" : ""} ${active ? "text-[var(--strong)]" : ""}`}
    >
      {children}{active ? (sort!.dir === 1 ? " ↑" : " ↓") : col ? " ⇅" : ""}
    </th>
  );
}

// ── persistence ──────────────────────────────────────────────────────────────────────────────
const PKEY = "fibret.state.v1";
interface Persisted {
  csvRows?: CsvRow[]; csvName?: string; symbolsText?: string; ivpMin?: number; lookback?: number;
  minOi?: number; lots?: number; expiry?: string; accountId?: number | null; result?: FibRetResult | null;
}
function loadPersisted(): Persisted {
  try { return JSON.parse(localStorage.getItem(PKEY) || "{}"); } catch { return {}; }
}

export default function FibRetPage() {
  const { data: accounts = [] } = useQuery({ queryKey: ["brokers"], queryFn: brokers.list });
  const sessioned = accounts.filter((a) => a.has_session);

  const saved = useRef(loadPersisted()).current;
  const [accountId, setAccountId] = useState<number | null>(saved.accountId ?? null);
  const [symbolsText, setSymbolsText] = useState(saved.symbolsText ?? "");
  const [csvRows, setCsvRows] = useState<CsvRow[]>(saved.csvRows ?? []);
  const [csvName, setCsvName] = useState(saved.csvName ?? "");
  const [ivpMin, setIvpMin] = useState(saved.ivpMin ?? 70);
  const [lookback, setLookback] = useState(saved.lookback ?? 20);
  const [lots, setLots] = useState(saved.lots ?? 1);
  const [expiry, setExpiry] = useState(saved.expiry ?? "");
  const [result, setResult] = useState<FibRetResult | null>(saved.result ?? null);
  const [deployRow, setDeployRow] = useState<FibRetRow | null>(null);
  const [sort, setSort] = useState<Sort | null>(null);

  const effectiveAccount = accountId ?? sessioned[0]?.id ?? null;

  const ivpMap = useMemo(() => new Map(csvRows.map((r) => [r.symbol, r])), [csvRows]);

  // Click a column header to sort (first click descending). Rows missing the value sort last.
  const onSort = (c: string) => setSort((s) => (s?.col === c ? { col: c, dir: (s.dir === 1 ? -1 : 1) as 1 | -1 } : { col: c, dir: -1 }));
  const sortVal = (r: FibRetRow, col: string): number | string | null | undefined => {
    switch (col) {
      case "stock": return r.symbol;
      case "ivp": return ivpMap.get(r.symbol)?.ivp;
      case "atmiv": return ivpMap.get(r.symbol)?.atmIv;
      case "spot": return r.spot;
      case "side": return r.side;
      case "swing": return r.swing_low;
      case "strike": return r.strike;
      case "dte": return r.dte;
      case "premium": return r.premium;
      case "oi": return r.oi;
      case "spread": return r.spread_pct;
      case "stop": return r.stop_level;
      case "rr": return r.reward_risk;
      case "maxprofit": return r.max_profit;
      case "margin": return r.margin;
      case "ivrv": return r.iv_richness;
      case "cushK": return r.cushion_to_strike_pct;
      case "cushStop": return r.cushion_to_stop_pct;
      default: return null;
    }
  };
  const sortedRows = useMemo(() => {
    if (!result) return [];
    if (!sort) return result.rows;
    const out = [...result.rows];
    out.sort((a, b) => {
      const va = sortVal(a, sort.col);
      const vb = sortVal(b, sort.col);
      if (va == null && vb == null) return 0;
      if (va == null) return 1; // missing values always last
      if (vb == null) return -1;
      if (typeof va === "string" || typeof vb === "string") return String(va).localeCompare(String(vb)) * sort.dir;
      return (va - vb) * sort.dir;
    });
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [result, sort, ivpMap]);
  const csvFiltered = useMemo(
    () => csvRows.filter((r) => (r.ivp ?? -1) >= ivpMin).sort((a, b) => (b.ivp ?? 0) - (a.ivp ?? 0)),
    [csvRows, ivpMin],
  );
  const symbols = useMemo(
    () =>
      csvRows.length
        ? csvFiltered.map((r) => r.symbol)
        : symbolsText.split(/[\s,]+/).map((s) => s.trim().toUpperCase()).filter(Boolean),
    [csvRows.length, csvFiltered, symbolsText],
  );

  // Persist the uploaded CSV, the inputs and the last results so the screen survives reloads /
  // navigation — retained until replaced or cleared.
  useEffect(() => {
    localStorage.setItem(PKEY, JSON.stringify({
      csvRows, csvName, symbolsText, ivpMin, lookback, lots, expiry, accountId, result,
    }));
  }, [csvRows, csvName, symbolsText, ivpMin, lookback, lots, expiry, accountId, result]);

  const onUpload = (file: File) => {
    setCsvName(file.name);
    const reader = new FileReader();
    reader.onload = () => setCsvRows(parseScreenerCsv(String(reader.result ?? "")));
    reader.readAsText(file);
  };
  const clearCsv = () => { setCsvRows([]); setCsvName(""); };

  const analyze = useMutation({
    mutationFn: (): Promise<FibRetResult> =>
      api.fibretAnalyze({ broker_account_id: effectiveAccount!, symbols, expiry: expiry || null, swing_lookback: lookback, min_oi: 0, lots }),
    onSuccess: (d) => setResult(d),
  });

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold">FibRet screener</h1>
        <p className="text-sm text-[var(--muted)]">
          Fibonacci-retracement option selling. Upload your IVP screener CSV (or paste symbols); for each
          stock the tool finds the recent daily swing, suggests a short option at the 1.618 extension with
          a spot-stop at the 0.786 level, and shows live premium, bid-ask liquidity, R:R and margin. Pick
          rows and deploy. Needs a logged-in broker session for live chains.
        </p>
      </div>

      <Panel className="space-y-3 p-5">
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <label className="inline-flex items-center gap-2 rounded bg-[var(--chip)] text-[var(--chip-text)] hover:opacity-80 px-3 py-1.5 cursor-pointer">
            📄 Upload screener CSV
            <input type="file" accept=".csv,text/csv" className="hidden"
              onChange={(e) => { const f = e.target.files?.[0]; if (f) onUpload(f); e.target.value = ""; }} />
          </label>
          {csvRows.length > 0 && (
            <span className="text-xs text-[var(--muted)]">
              {csvName || "uploaded"} — {csvRows.length} instruments · {csvFiltered.length} with IVP ≥ {ivpMin}
              <button onClick={clearCsv} className="ml-2 text-[var(--faint)] hover:text-[var(--strong)] underline">clear</button>
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-end gap-3 text-sm">
          <label className="flex flex-col gap-1">
            <span className="text-[var(--muted)]">Broker session</span>
            <select value={effectiveAccount ?? ""} onChange={(e) => setAccountId(Number(e.target.value) || null)}
              className="rounded bg-[var(--field)] border border-[var(--field-border)] px-2 py-1.5 min-w-[14rem]">
              {sessioned.length === 0 && <option value="">No logged-in account</option>}
              {sessioned.map((a) => <option key={a.id} value={a.id}>{a.label} {a.armed ? "· armed" : ""}</option>)}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[var(--muted)]">Swing lookback (days)</span>
            <input type="number" min={10} value={lookback} onChange={(e) => setLookback(Number(e.target.value) || 20)}
              className="w-28 rounded bg-[var(--field)] border border-[var(--field-border)] px-2 py-1.5" />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[var(--muted)]">IVP ≥ {csvRows.length ? "" : "(CSV only)"}</span>
            <input type="number" min={0} max={100} value={ivpMin} onChange={(e) => setIvpMin(Number(e.target.value) || 0)}
              disabled={csvRows.length === 0}
              className="w-20 rounded bg-[var(--field)] border border-[var(--field-border)] px-2 py-1.5 disabled:opacity-50" />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[var(--muted)]">Lots</span>
            <input type="number" min={1} value={lots} onChange={(e) => setLots(Math.max(1, Number(e.target.value) || 1))}
              className="w-20 rounded bg-[var(--field)] border border-[var(--field-border)] px-2 py-1.5" />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[var(--muted)]">Expiry (optional)</span>
            <input type="date" value={expiry} onChange={(e) => setExpiry(e.target.value)}
              className="rounded bg-[var(--field)] border border-[var(--field-border)] px-2 py-1.5" />
          </label>
        </div>
        {csvRows.length === 0 ? (
          <textarea value={symbolsText} onChange={(e) => setSymbolsText(e.target.value)} rows={2}
            placeholder="Watchlist — symbols separated by space, comma or newline (e.g. INFY, BAJFINANCE, RELIANCE) — or upload your screener CSV above"
            className="w-full rounded bg-[var(--field)] border border-[var(--field-border)] px-3 py-2 text-sm font-mono" />
        ) : (
          <div className="text-xs text-[var(--muted)] font-mono break-words">
            {symbols.length ? symbols.join(", ") : "No instruments pass the IVP filter — lower IVP ≥."}
          </div>
        )}
        <div className="flex items-center gap-3">
          <button onClick={() => analyze.mutate()} disabled={analyze.isPending || !effectiveAccount || symbols.length === 0}
            className="rounded bg-[var(--ft)] px-4 py-1.5 text-sm text-white disabled:opacity-50">
            {analyze.isPending ? "Analyzing…" : result ? `Refresh (${symbols.length})` : `Analyze ${symbols.length || ""}`.trim()}
          </button>
          {analyze.isError && <span className="text-sm text-[var(--danger)]">{(analyze.error as Error).message}</span>}
          {result && <span className="text-xs text-[var(--faint)]">as of {result.as_of} · stop at {result.stop_fib} · entry at {result.entry_fib} · target {result.target_pct}%</span>}
        </div>
      </Panel>

      {result && (
        <Panel className="p-4">
          <div className="overflow-x-auto">
            <table className="w-full text-sm tabular-nums whitespace-nowrap">
              <thead>
                <tr className="text-[var(--muted)] text-xs border-b border-[var(--divider)]">
                  <Th col="stock" sort={sort} onSort={onSort}>Stock</Th>
                  <Th right col="ivp" sort={sort} onSort={onSort}>IVP</Th>
                  <Th right col="atmiv" sort={sort} onSort={onSort}>ATM IV</Th>
                  <Th right col="spot" sort={sort} onSort={onSort}>Spot</Th>
                  <Th col="side" sort={sort} onSort={onSort}>Side</Th>
                  <Th col="swing" sort={sort} onSort={onSort}>Swing (L→H)</Th>
                  <Th right col="strike" sort={sort} onSort={onSort}>Strike</Th>
                  <Th right col="dte" sort={sort} onSort={onSort}>DTE</Th>
                  <Th right col="premium" sort={sort} onSort={onSort}>Premium</Th>
                  <Th right col="oi" sort={sort} onSort={onSort}>OI</Th>
                  <Th right col="spread" sort={sort} onSort={onSort}>Bid/Ask (spr)</Th>
                  <Th right col="stop" sort={sort} onSort={onSort}>Stop spot</Th>
                  <Th right col="rr" sort={sort} onSort={onSort}>R:R</Th>
                  <Th right col="maxprofit" sort={sort} onSort={onSort}>Max profit</Th>
                  <Th right col="margin" sort={sort} onSort={onSort}>Margin</Th>
                  <Th right col="ivrv" sort={sort} onSort={onSort}>IV/RV</Th>
                  <Th right col="cushK" sort={sort} onSort={onSort}>Cushion→K</Th>
                  <Th right col="cushStop" sort={sort} onSort={onSort}>Cush→Stop</Th>
                  <Th></Th>
                </tr>
              </thead>
              <tbody>
                {sortedRows.map((r) => (
                  <tr key={r.symbol} className="border-b border-[var(--divider)]/40">
                    <td className="py-1.5 px-2 font-medium">{r.symbol}</td>
                    <td className="py-1.5 px-2 text-right">{ivpMap.get(r.symbol)?.ivp ?? "—"}</td>
                    <td className="py-1.5 px-2 text-right">{n1(ivpMap.get(r.symbol)?.atmIv)}</td>
                    {r.error ? (
                      <td colSpan={16} className="py-1.5 px-2 text-[var(--danger)]/80 text-xs">{r.error}</td>
                    ) : (
                      <>
                        <td className="py-1.5 px-2 text-right">{n1(r.spot)}</td>
                        <td className="py-1.5 px-2"><span className={r.side === "CE" ? "text-[var(--danger)]" : "text-[var(--pos)]"}>SELL {r.side}</span></td>
                        <td className="py-1.5 px-2 text-xs text-[var(--muted)]">{n1(r.swing_low)}→{n1(r.swing_high)}</td>
                        <td className={`py-1.5 px-2 text-right ${r.out_of_range ? "text-[var(--warn-text)]" : ""}`} title={r.note ?? undefined}>
                          {r.strike}{r.out_of_range ? " ⚑" : ""}
                        </td>
                        <td className="py-1.5 px-2 text-right">{r.dte}</td>
                        <td className="py-1.5 px-2 text-right">{n2(r.premium)}</td>
                        <td className="py-1.5 px-2 text-right">{r.oi?.toLocaleString("en-IN")}</td>
                        <td className={`py-1.5 px-2 text-right ${r.liquid ? "" : "text-[var(--warn-text)]"}`}
                          title={r.liquid ? undefined : "wide bid-ask spread (>10% of mid) — illiquid"}>
                          {n2(r.bid)}/{n2(r.ask)} {r.spread_pct == null ? "" : `(${r.spread_pct.toFixed(0)}%)`}{r.liquid ? "" : " ⚑"}
                        </td>
                        <td className="py-1.5 px-2 text-right">{n1(r.stop_level)}</td>
                        <td className="py-1.5 px-2 text-right">{r.reward_risk != null ? `${r.reward_risk.toFixed(2)}x` : "NA"}</td>
                        <td className="py-1.5 px-2 text-right text-[var(--pos)]">{money(r.max_profit)}</td>
                        <td className="py-1.5 px-2 text-right">{money(r.margin)}</td>
                        <td className="py-1.5 px-2 text-right">{r.iv_richness != null ? `${r.iv_richness.toFixed(2)}x` : "—"}</td>
                        <td className="py-1.5 px-2 text-right">{pct(r.cushion_to_strike_pct)}</td>
                        <td className="py-1.5 px-2 text-right">{pct(r.cushion_to_stop_pct)}</td>
                        <td className="py-1.5 px-2 text-right">
                          <button onClick={() => setDeployRow(r)}
                            className="rounded bg-[var(--ft)] text-white px-2.5 py-1 text-xs">
                            Deploy
                          </button>
                        </td>
                      </>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="text-[11px] text-[var(--faint)] mt-2">
            ⚑ on Bid/Ask = wide spread (&gt;10% of mid) → illiquid. ⚑ on Strike = 1.618 level beyond listed
            strikes. R:R = max profit ÷ estimated loss if spot hits the stop. IV/RV = live IV ÷ realized vol.
            Deploy lets you change the strike and add a hedge. Gross of charges.
          </div>
        </Panel>
      )}

      {deployRow && result && effectiveAccount && (
        <DeployPanel row={deployRow} result={result} accountId={effectiveAccount} onClose={() => setDeployRow(null)} />
      )}
    </div>
  );
}
