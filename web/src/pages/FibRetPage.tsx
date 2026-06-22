import { useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, brokers } from "../api/client";
import { Card } from "../components/ui";
import { formatInr } from "../lib/format";
import type { FibRetResult, FibRetRow, OptionsTradeDeploy } from "../types";

const n2 = (v?: number | null) => (v == null ? "—" : v.toFixed(2));
const n1 = (v?: number | null) => (v == null ? "—" : v.toFixed(1));
const pct = (v?: number | null) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`);
const money = (v?: number | null) => (v == null ? "—" : formatInr(v));

interface CsvRow {
  symbol: string;
  ivp?: number; // IV percentile (0–100)
  atmIv?: number;
  futPrice?: number;
  pcr?: number;
}

/** Parse an options-screener CSV export (header: Instrument, …, ATMIV, …, IVPercentile, …, PCR).
 *  Column matching is case-insensitive and order-independent; rows without an Instrument are skipped. */
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

/** Confirm + deploy one screener row as a custom_options short leg (spot-stop + 90% target). */
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
  const [mode, setMode] = useState("PAPER");
  const [lots, setLots] = useState(row.lots ?? 1);
  const [capital, setCapital] = useState(1_000_000);
  const [name, setName] = useState(`${row.symbol}_FibRet`);

  const deploy = useMutation({
    mutationFn: () => {
      const body: OptionsTradeDeploy = {
        name,
        underlying: row.symbol,
        expiry: row.expiry!,
        legs: [{ right: row.side!, strike: row.strike!, side: "sell", lots }],
        lot_size: row.lot_size ?? 0,
        capital,
        // spot-based stop at the 0.786 level: above for a short call, below for a short put.
        spot_upper: row.side === "CE" ? row.stop_level : null,
        spot_lower: row.side === "PE" ? row.stop_level : null,
        target_pct: result.target_pct, // whole percent (e.g. 90) — backend converts to fraction
        mode,
        quote_source: mode === "LIVE" ? "zerodha" : "zerodha",
        broker_account_id: accountId,
        ignore_market_hours: true,
        auto: true,
        notes: `FibRet ${row.side} ${row.strike} · swing ${n2(row.swing_low)}–${n2(row.swing_high)} · stop spot ${n2(row.stop_level)}`,
      };
      return api.deployOptionTrade(body);
    },
  });

  return (
    <div className="fixed inset-0 z-20 flex items-center justify-center bg-black/50 p-4" onClick={onClose}>
      <div className="w-full max-w-lg" onClick={(e) => e.stopPropagation()}>
        <Card className="space-y-3">
          <div className="flex items-center justify-between">
            <div className="font-medium text-slate-200">
              Deploy {row.symbol} — SELL {row.strike} {row.side}
            </div>
            <button onClick={onClose} className="text-slate-500 hover:text-slate-300 px-1">×</button>
          </div>
          <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm">
            <div className="flex justify-between"><span className="text-slate-400">Expiry</span><span>{row.expiry} ({row.dte}d)</span></div>
            <div className="flex justify-between"><span className="text-slate-400">Premium</span><span>{n2(row.premium)} ×{row.lot_size}</span></div>
            <div className="flex justify-between"><span className="text-slate-400">Stop (spot {row.side === "CE" ? "≥" : "≤"})</span><span>{n2(row.stop_level)}</span></div>
            <div className="flex justify-between"><span className="text-slate-400">Target</span><span>book {result.target_pct}% of premium</span></div>
            <div className="flex justify-between"><span className="text-slate-400">Max profit</span><span className="text-emerald-500">{money((row.premium ?? 0) * (row.lot_size ?? 0) * lots)}</span></div>
            <div className="flex justify-between"><span className="text-slate-400">R:R</span><span>{row.reward_risk != null ? `${row.reward_risk.toFixed(2)}x` : "NA"}</span></div>
          </div>
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <label className="flex items-center gap-1.5">Mode
              <select value={mode} onChange={(e) => setMode(e.target.value)} className="rounded bg-slate-800 border border-slate-700 px-2 py-1">
                <option value="PAPER">PAPER</option>
                <option value="LIVE">LIVE (real money)</option>
              </select>
            </label>
            <label className="flex items-center gap-1.5">Lots
              <input type="number" min={1} value={lots} onChange={(e) => setLots(Math.max(1, Number(e.target.value) || 1))}
                className="w-16 rounded bg-slate-800 border border-slate-700 px-2 py-1" />
            </label>
            <label className="flex items-center gap-1.5">Capital
              <input type="number" min={0} step={100000} value={capital} onChange={(e) => setCapital(Number(e.target.value) || 0)}
                className="w-28 rounded bg-slate-800 border border-slate-700 px-2 py-1" />
            </label>
          </div>
          <input value={name} onChange={(e) => setName(e.target.value)}
            className="w-full rounded bg-slate-800 border border-slate-700 px-2 py-1 text-sm" placeholder="Deployment name" />
          {mode === "LIVE" && (
            <div className="text-xs text-amber-500">Real-money LIVE also requires the account to be armed and trading enabled.</div>
          )}
          {deploy.isError && <div className="text-xs text-rose-500">{(deploy.error as Error).message}</div>}
          {deploy.isSuccess ? (
            <div className="text-sm text-emerald-500">
              Deployed. <Link to="/live" className="underline">Open the Live tab →</Link>
            </div>
          ) : (
            <div className="flex gap-2">
              <button onClick={() => deploy.mutate()} disabled={deploy.isPending}
                className="rounded bg-brand hover:bg-brand-light px-3 py-1.5 text-sm text-white disabled:opacity-50">
                {deploy.isPending ? "Deploying…" : `Deploy (${mode})`}
              </button>
              <button onClick={onClose} className="rounded bg-slate-800 hover:bg-slate-700 px-3 py-1.5 text-sm">Cancel</button>
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}

function Th({ children, right }: { children?: React.ReactNode; right?: boolean }) {
  return <th className={`font-medium py-1.5 px-2 ${right ? "text-right" : "text-left"}`}>{children}</th>;
}

export default function FibRetPage() {
  const { data: accounts = [] } = useQuery({ queryKey: ["brokers"], queryFn: brokers.list });
  const sessioned = accounts.filter((a) => a.has_session);

  const [accountId, setAccountId] = useState<number | null>(null);
  const [symbolsText, setSymbolsText] = useState("");
  const [csvRows, setCsvRows] = useState<CsvRow[]>([]);
  const [csvName, setCsvName] = useState("");
  const [ivpMin, setIvpMin] = useState(70);
  const [lookback, setLookback] = useState(20);
  const [minOi, setMinOi] = useState(0);
  const [lots, setLots] = useState(1);
  const [expiry, setExpiry] = useState("");
  const [deployRow, setDeployRow] = useState<FibRetRow | null>(null);

  const effectiveAccount = accountId ?? sessioned[0]?.id ?? null;

  // IVP lookup (from the uploaded CSV) keyed by symbol, for merging into the results table.
  const ivpMap = useMemo(() => new Map(csvRows.map((r) => [r.symbol, r])), [csvRows]);
  // CSV rows that pass the IVP filter, sorted high-IVP first.
  const csvFiltered = useMemo(
    () => csvRows.filter((r) => (r.ivp ?? -1) >= ivpMin).sort((a, b) => (b.ivp ?? 0) - (a.ivp ?? 0)),
    [csvRows, ivpMin],
  );
  // When a CSV is loaded, the filtered names drive the scan; otherwise fall back to the textarea.
  const symbols = useMemo(
    () =>
      csvRows.length
        ? csvFiltered.map((r) => r.symbol)
        : symbolsText.split(/[\s,]+/).map((s) => s.trim().toUpperCase()).filter(Boolean),
    [csvRows.length, csvFiltered, symbolsText],
  );

  const onUpload = (file: File) => {
    setCsvName(file.name);
    const reader = new FileReader();
    reader.onload = () => setCsvRows(parseScreenerCsv(String(reader.result ?? "")));
    reader.readAsText(file);
  };

  const analyze = useMutation({
    mutationFn: (): Promise<FibRetResult> =>
      api.fibretAnalyze({
        broker_account_id: effectiveAccount!,
        symbols,
        expiry: expiry || null,
        swing_lookback: lookback,
        min_oi: minOi,
        lots,
      }),
  });
  const result = analyze.data;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold">FibRet screener</h1>
        <p className="text-sm text-slate-400">
          Fibonacci-retracement option selling. Upload your IVP screener CSV (or paste symbols); for each
          stock the tool finds the recent daily swing, suggests a short option at the 1.618 extension with
          a spot-stop at the 0.786 level, and shows live premium, liquidity, R:R and margin. Pick rows and
          deploy. Needs a logged-in broker session for live chains.
        </p>
      </div>

      <Card className="space-y-3">
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <label className="inline-flex items-center gap-2 rounded bg-slate-800 hover:bg-slate-700 px-3 py-1.5 cursor-pointer">
            📄 Upload screener CSV
            <input
              type="file"
              accept=".csv,text/csv"
              className="hidden"
              onChange={(e) => { const f = e.target.files?.[0]; if (f) onUpload(f); e.target.value = ""; }}
            />
          </label>
          {csvRows.length > 0 && (
            <span className="text-xs text-slate-400">
              {csvName} — {csvRows.length} instruments · {csvFiltered.length} with IVP ≥ {ivpMin}
              <button onClick={() => { setCsvRows([]); setCsvName(""); }} className="ml-2 text-slate-500 hover:text-slate-300 underline">clear</button>
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-end gap-3 text-sm">
          <label className="flex flex-col gap-1">
            <span className="text-slate-400">Broker session</span>
            <select
              value={effectiveAccount ?? ""}
              onChange={(e) => setAccountId(Number(e.target.value) || null)}
              className="rounded bg-slate-800 border border-slate-700 px-2 py-1.5 min-w-[14rem]"
            >
              {sessioned.length === 0 && <option value="">No logged-in account</option>}
              {sessioned.map((a) => (
                <option key={a.id} value={a.id}>{a.label} {a.armed ? "· armed" : ""}</option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-slate-400">Swing lookback (days)</span>
            <input type="number" min={10} value={lookback} onChange={(e) => setLookback(Number(e.target.value) || 60)}
              className="w-28 rounded bg-slate-800 border border-slate-700 px-2 py-1.5" />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-slate-400">IVP ≥ {csvRows.length ? "" : "(CSV only)"}</span>
            <input type="number" min={0} max={100} value={ivpMin} onChange={(e) => setIvpMin(Number(e.target.value) || 0)}
              disabled={csvRows.length === 0}
              className="w-20 rounded bg-slate-800 border border-slate-700 px-2 py-1.5 disabled:opacity-50" />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-slate-400">Min OI</span>
            <input type="number" min={0} value={minOi} onChange={(e) => setMinOi(Number(e.target.value) || 0)}
              className="w-24 rounded bg-slate-800 border border-slate-700 px-2 py-1.5" />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-slate-400">Lots</span>
            <input type="number" min={1} value={lots} onChange={(e) => setLots(Math.max(1, Number(e.target.value) || 1))}
              className="w-20 rounded bg-slate-800 border border-slate-700 px-2 py-1.5" />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-slate-400">Expiry (optional)</span>
            <input type="date" value={expiry} onChange={(e) => setExpiry(e.target.value)}
              className="rounded bg-slate-800 border border-slate-700 px-2 py-1.5" />
          </label>
        </div>
        {csvRows.length === 0 ? (
          <textarea
            value={symbolsText}
            onChange={(e) => setSymbolsText(e.target.value)}
            rows={2}
            placeholder="Watchlist — symbols separated by space, comma or newline (e.g. INFY, BAJFINANCE, RELIANCE) — or upload your screener CSV above"
            className="w-full rounded bg-slate-800 border border-slate-700 px-3 py-2 text-sm font-mono"
          />
        ) : (
          <div className="text-xs text-slate-400 font-mono break-words">
            {symbols.length ? symbols.join(", ") : "No instruments pass the IVP filter — lower IVP ≥."}
          </div>
        )}
        <div className="flex items-center gap-3">
          <button
            onClick={() => analyze.mutate()}
            disabled={analyze.isPending || !effectiveAccount || symbols.length === 0}
            className="rounded bg-brand hover:bg-brand-light px-4 py-1.5 text-sm text-white disabled:opacity-50"
          >
            {analyze.isPending ? "Analyzing…" : `Analyze ${symbols.length || ""}`.trim()}
          </button>
          {analyze.isError && <span className="text-sm text-rose-500">{(analyze.error as Error).message}</span>}
          {result && <span className="text-xs text-slate-500">as of {result.as_of} · stop at {result.stop_fib} · entry at {result.entry_fib} · target {result.target_pct}%</span>}
        </div>
      </Card>

      {result && (
        <Card>
          <div className="overflow-x-auto">
            <table className="w-full text-sm tabular-nums whitespace-nowrap">
              <thead>
                <tr className="text-slate-400 text-xs border-b border-slate-800">
                  <Th>Stock</Th><Th right>IVP</Th><Th right>ATM IV</Th><Th right>Spot</Th><Th>Side</Th><Th>Swing (L→H)</Th>
                  <Th right>Strike</Th><Th right>DTE</Th><Th right>Premium</Th><Th right>OI</Th>
                  <Th right>Stop spot</Th><Th right>R:R</Th><Th right>Max profit</Th>
                  <Th right>Margin</Th><Th right>IV/RV</Th><Th right>Cushion→K</Th><Th></Th>
                </tr>
              </thead>
              <tbody>
                {result.rows.map((r) => (
                  <tr key={r.symbol} className="border-b border-slate-800/40">
                    <td className="py-1.5 px-2 font-medium">{r.symbol}</td>
                    <td className="py-1.5 px-2 text-right">{ivpMap.get(r.symbol)?.ivp ?? "—"}</td>
                    <td className="py-1.5 px-2 text-right">{n1(ivpMap.get(r.symbol)?.atmIv)}</td>
                    {r.error ? (
                      <td colSpan={14} className="py-1.5 px-2 text-rose-500/80 text-xs">{r.error}</td>
                    ) : (
                      <>
                        <td className="py-1.5 px-2 text-right">{n1(r.spot)}</td>
                        <td className="py-1.5 px-2">
                          <span className={r.side === "CE" ? "text-rose-400" : "text-emerald-400"}>SELL {r.side}</span>
                        </td>
                        <td className="py-1.5 px-2 text-xs text-slate-400">{n1(r.swing_low)}→{n1(r.swing_high)}</td>
                        <td className={`py-1.5 px-2 text-right ${r.out_of_range ? "text-amber-500" : ""}`} title={r.note ?? undefined}>
                          {r.strike}{r.out_of_range ? " ⚑" : ""}
                        </td>
                        <td className="py-1.5 px-2 text-right">{r.dte}</td>
                        <td className="py-1.5 px-2 text-right">{n2(r.premium)}</td>
                        <td className={`py-1.5 px-2 text-right ${r.liquid ? "" : "text-amber-500"}`}>{r.oi?.toLocaleString("en-IN")}{r.liquid ? "" : " ⚑"}</td>
                        <td className="py-1.5 px-2 text-right">{n1(r.stop_level)}</td>
                        <td className="py-1.5 px-2 text-right">{r.reward_risk != null ? `${r.reward_risk.toFixed(2)}x` : "NA"}</td>
                        <td className="py-1.5 px-2 text-right text-emerald-500">{money(r.max_profit)}</td>
                        <td className="py-1.5 px-2 text-right">{money(r.margin)}</td>
                        <td className="py-1.5 px-2 text-right">{r.iv_richness != null ? `${r.iv_richness.toFixed(2)}x` : "—"}</td>
                        <td className="py-1.5 px-2 text-right">{pct(r.cushion_to_strike_pct)}</td>
                        <td className="py-1.5 px-2 text-right">
                          <button
                            onClick={() => setDeployRow(r)}
                            disabled={!r.premium || r.out_of_range}
                            title={r.out_of_range ? r.note ?? "1.618 level beyond listed strikes" : undefined}
                            className="rounded bg-emerald-700 hover:bg-emerald-600 text-white px-2.5 py-1 text-xs disabled:opacity-40"
                          >
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
          <div className="text-[11px] text-slate-500 mt-2">
            ⚑ = OI below your min. R:R = max profit ÷ estimated loss if spot hits the stop (BS, current IV).
            IV/RV = live ATM-ish IV ÷ realized vol (sanity check that IV is rich). All gross of charges.
          </div>
        </Card>
      )}

      {deployRow && result && effectiveAccount && (
        <DeployPanel row={deployRow} result={result} accountId={effectiveAccount} onClose={() => setDeployRow(null)} />
      )}
    </div>
  );
}
