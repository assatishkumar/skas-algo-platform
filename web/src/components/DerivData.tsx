import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api, brokers } from "../api/client";
import { formatInr, pct } from "../lib/format";
import type { DerivCoverage, OptionChainRow } from "../types";
import { Card, ErrorBox, Spinner } from "./ui";

const todayISO = () => new Date().toISOString().slice(0, 10);

// Backend caps a single refresh at 120 days/call (MAX_REFRESH_DAYS). Split a longer span into
// ≤120-day windows so a big backfill (or a long incremental gap) auto-chunks with progress.
const WINDOW_DAYS = 120;
function addDaysISO(iso: string, days: number): string {
  const d = new Date(iso + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}
function dateWindows(startISO: string, endISO: string, maxDays = WINDOW_DAYS): [string, string][] {
  const out: [string, string][] = [];
  let cur = startISO;
  while (cur <= endISO) {
    const winEnd = addDaysISO(cur, maxDays - 1);
    out.push([cur, winEnd < endISO ? winEnd : endISO]);
    cur = addDaysISO(out[out.length - 1][1], 1);
  }
  return out;
}

type UnderlyingItem = { key: string; label: string; disabled: boolean; hint: string };

// Options support real NIFTY/BANKNIFTY (bhavcopy) + synthetic GOLD (Black-76, GOLDM specs).
const OPT_UNDERLYINGS: UnderlyingItem[] = [
  { key: "NIFTY", label: "NIFTY", disabled: false, hint: "" },
  { key: "BANKNIFTY", label: "BANKNIFTY", disabled: false, hint: "" },
  { key: "GOLD", label: "GOLD", disabled: false, hint: "synthetic — Black-76 from realized vol, GOLDM specs (100 g, 500-pt strikes)" },
];
// Futures are NSE FUTIDX only; GOLDM (MCX) needs a separate feed.
const FUT_UNDERLYINGS: UnderlyingItem[] = [
  { key: "NIFTY", label: "NIFTY", disabled: false, hint: "" },
  { key: "BANKNIFTY", label: "BANKNIFTY", disabled: false, hint: "" },
  { key: "GOLDM", label: "GOLDM", disabled: true, hint: "MCX commodity — needs a separate data feed" },
];

const inputClass =
  "rounded-md bg-slate-800 border border-slate-700 px-2.5 py-1.5 text-sm focus:outline-none focus:border-brand";

function UnderlyingSelector({ value, onChange, items }: { value: string; onChange: (u: string) => void; items: UnderlyingItem[] }) {
  return (
    <div className="flex gap-2">
      {items.map((u) => (
        <button
          key={u.key}
          disabled={u.disabled}
          title={u.hint}
          onClick={() => !u.disabled && onChange(u.key)}
          className={`px-3 py-1.5 rounded-md text-sm font-medium ${
            value === u.key ? "bg-brand text-white" : "bg-slate-800 text-slate-300"
          } ${u.disabled ? "opacity-40 cursor-not-allowed" : "hover:bg-slate-700"}`}
        >
          {u.label}
          {u.disabled ? " 🔒" : ""}
        </button>
      ))}
    </div>
  );
}

function CoverageCard({
  underlying,
  kind,
  refreshKey,
}: {
  underlying: string;
  kind: "options" | "futures";
  refreshKey: number;
}) {
  const { data, isLoading, error } = useQuery<DerivCoverage>({
    queryKey: [`${kind}-coverage`, underlying, refreshKey],
    queryFn: () => (kind === "options" ? api.optionsCoverage(underlying) : api.futuresCoverage(underlying)),
    retry: false,
  });
  return (
    <Card>
      <div className="text-sm font-medium text-slate-300 mb-2">Coverage — {underlying} {kind}</div>
      {isLoading ? (
        <div className="text-xs text-slate-500">Loading…</div>
      ) : error || !data?.total_records ? (
        <div className="text-xs text-slate-500">
          No cached {kind} for {underlying} yet — refresh a date range below.
        </div>
      ) : (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
          <div>
            <div className="text-slate-400 text-xs">From</div>
            <div className="font-semibold">{data.start_date}</div>
          </div>
          <div>
            <div className="text-slate-400 text-xs">To</div>
            <div className="font-semibold">{data.end_date}</div>
          </div>
          <div>
            <div className="text-slate-400 text-xs">Records</div>
            <div className="font-semibold">{data.total_records.toLocaleString("en-IN")}</div>
          </div>
          <div>
            <div className="text-slate-400 text-xs">Trading days</div>
            <div className="font-semibold">{data.trading_days}</div>
          </div>
        </div>
      )}
    </Card>
  );
}

function RefreshControl({
  underlying,
  kind,
  onDone,
  coverageEnd,
}: {
  underlying: string;
  kind: "options" | "futures";
  onDone: () => void;
  coverageEnd?: string | null; // last cached date → enables incremental "Refresh to latest"
}) {
  const [start, setStart] = useState("2024-07-08");
  const [end, setEnd] = useState(todayISO());
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  // Auto-chunk [s, e] into ≤120-day windows and fetch each in turn, so a long backfill or a wide
  // incremental gap works in one click with a progress indicator instead of the backend rejecting it.
  async function runRange(s: string, e: string) {
    if (s > e) {
      setMsg("nothing to refresh — start is after end");
      return;
    }
    const windows = dateWindows(s, e);
    setBusy(true);
    setMsg(null);
    setProgress({ done: 0, total: windows.length });
    const fn = kind === "options" ? api.optionsRefresh : api.futuresRefresh;
    let rows = 0;
    let days = 0;
    let errs = 0;
    try {
      for (let i = 0; i < windows.length; i += 1) {
        const [ws, we] = windows[i];
        const r = await fn({ underlyings: [underlying], start_date: ws, end_date: we });
        rows += r.rows_saved;
        days += r.days_saved;
        errs += r.errors.length;
        setProgress({ done: i + 1, total: windows.length });
      }
      setMsg(
        `Saved ${rows.toLocaleString("en-IN")} rows over ${days} days` +
          (errs ? ` · ${errs} day(s) failed` : ""),
      );
      onDone();
    } catch (err) {
      setMsg((err as Error).message);
    } finally {
      setBusy(false);
      setProgress(null);
    }
  }

  const today = todayISO();
  // Incremental default: pick up the day after the last cached date; first-ever run backfills from
  // the date-range "From" field.
  const incStart = coverageEnd ? addDaysISO(coverageEnd, 1) : start;
  const upToDate = !!coverageEnd && coverageEnd >= today;

  return (
    <Card>
      <div className="flex flex-wrap items-end gap-3">
        <button
          onClick={() => runRange(incStart, today)}
          disabled={busy || upToDate}
          title={
            upToDate
              ? "Already up to date"
              : `Fetch ${incStart} → ${today}${coverageEnd ? "" : " (initial backfill)"}`
          }
          className="rounded-md bg-emerald-700 hover:bg-emerald-600 text-white px-3 py-1.5 text-sm font-medium disabled:opacity-50"
        >
          {progress
            ? `Refreshing ${progress.done}/${progress.total}…`
            : upToDate
              ? "Up to date"
              : coverageEnd
                ? "Refresh to latest"
                : "Refresh to today"}
        </button>
        <span className="text-xs text-slate-500">or a date range:</span>
        <label className="block">
          <span className="block text-xs text-slate-400 mb-1">From</span>
          <input type="date" className={inputClass} value={start} onChange={(e) => setStart(e.target.value)} />
        </label>
        <label className="block">
          <span className="block text-xs text-slate-400 mb-1">To</span>
          <input type="date" className={inputClass} value={end} onChange={(e) => setEnd(e.target.value)} />
        </label>
        <button
          onClick={() => runRange(start, end)}
          disabled={busy}
          className="rounded-md bg-brand hover:bg-brand-light px-3 py-1.5 text-sm font-medium disabled:opacity-50"
        >
          {busy ? "Refreshing…" : `Refresh ${kind}`}
        </button>
        <span className="text-xs text-slate-500">NSE bhavcopy (auto-chunked ≤120 days/call; ~1 file per trading day)</span>
        {msg && <span className="text-xs text-slate-400">{msg}</span>}
      </div>
      {progress && (
        <div className="mt-2 h-1.5 w-full max-w-md rounded-full bg-slate-800 overflow-hidden">
          <div
            className="h-full bg-emerald-500 transition-[width] duration-200"
            style={{ width: `${progress.total ? (progress.done / progress.total) * 100 : 0}%` }}
          />
        </div>
      )}
    </Card>
  );
}

// ----------------------------------------------------------------- option chain
function fmtNum(v: number | null | undefined, d = 2): string {
  return v == null ? "—" : v.toFixed(d);
}
function fmtOi(v: number | null | undefined): string {
  return v == null ? "—" : v.toLocaleString("en-IN");
}

function OptionChainTable({ rows, atm, greeks }: { rows: OptionChainRow[]; atm: number | null; greeks: boolean }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const atmRowRef = useRef<HTMLTableRowElement>(null);

  // Center the ATM row in the scroll container whenever a chain loads (without
  // scrolling the whole page).
  useEffect(() => {
    const c = containerRef.current;
    const row = atmRowRef.current;
    if (!c || !row) return;
    const cRect = c.getBoundingClientRect();
    const rRect = row.getBoundingClientRect();
    c.scrollTop += rRect.top - cRect.top - c.clientHeight / 2 + rRect.height / 2;
  }, [rows, atm]);

  if (rows.length === 0) return <div className="text-sm text-slate-500">No chain for this date/expiry.</div>;
  return (
    <div ref={containerRef} className="overflow-x-auto max-h-[60vh] overflow-y-auto">
      <table className="w-full text-xs tabular-nums">
        <thead className="text-slate-400 sticky top-0 bg-slate-900">
          <tr>
            <th className="py-1 px-2 text-right">CE OI</th>
            <th className="py-1 px-2 text-right">CE Δ-OI</th>
            {greeks && <th className="py-1 px-2 text-right">CE IV</th>}
            {greeks && <th className="py-1 px-2 text-right">CE δ</th>}
            <th className="py-1 px-2 text-right text-emerald-700 dark:text-emerald-300">CE LTP</th>
            <th className="py-1 px-2 text-center font-semibold">STRIKE</th>
            <th className="py-1 px-2 text-left text-rose-700 dark:text-rose-300">PE LTP</th>
            {greeks && <th className="py-1 px-2 text-left">PE δ</th>}
            {greeks && <th className="py-1 px-2 text-left">PE IV</th>}
            <th className="py-1 px-2 text-left">PE Δ-OI</th>
            <th className="py-1 px-2 text-left">PE OI</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const isAtm = atm != null && r.strike === atm;
            const ceLtp = `py-1 px-2 text-right text-emerald-700 dark:text-emerald-300 ${isAtm ? "bg-emerald-500/20 font-bold ring-1 ring-inset ring-emerald-500/40" : ""}`;
            const peLtp = `py-1 px-2 text-left text-rose-700 dark:text-rose-300 ${isAtm ? "bg-rose-500/20 font-bold ring-1 ring-inset ring-rose-500/40" : ""}`;
            return (
              <tr
                key={r.strike}
                ref={isAtm ? atmRowRef : undefined}
                className={`border-t border-slate-800 ${isAtm ? "bg-amber-900/20" : ""}`}
              >
                <td className="py-1 px-2 text-right text-slate-400">{fmtOi(r.ce?.oi)}</td>
                <td className="py-1 px-2 text-right text-slate-500">{fmtOi(r.ce?.change_in_oi)}</td>
                {greeks && <td className="py-1 px-2 text-right text-slate-400">{r.ce?.iv != null ? pct(r.ce.iv * 100, 1) : "—"}</td>}
                {greeks && <td className="py-1 px-2 text-right text-slate-400">{fmtNum(r.ce?.delta)}</td>}
                <td className={ceLtp}>{fmtNum(r.ce?.ltp ?? r.ce?.close)}</td>
                <td className={`py-1 px-2 text-center font-semibold ${isAtm ? "text-amber-700 dark:text-amber-300" : "text-slate-200"}`}>{r.strike}</td>
                <td className={peLtp}>{fmtNum(r.pe?.ltp ?? r.pe?.close)}</td>
                {greeks && <td className="py-1 px-2 text-left text-slate-400">{fmtNum(r.pe?.delta)}</td>}
                {greeks && <td className="py-1 px-2 text-left text-slate-400">{r.pe?.iv != null ? pct(r.pe.iv * 100, 1) : "—"}</td>}
                <td className="py-1 px-2 text-left text-slate-500">{fmtOi(r.pe?.change_in_oi)}</td>
                <td className="py-1 px-2 text-left text-slate-400">{fmtOi(r.pe?.oi)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function OptionChainViewer({ underlying, coverageEnd }: { underlying: string; coverageEnd?: string | null }) {
  const [date, setDate] = useState<string>(coverageEnd ?? todayISO());
  const [expiry, setExpiry] = useState<string>("");
  const [greeks, setGreeks] = useState(false);

  // Re-seed the date when the underlying's coverage end changes.
  useEffect(() => {
    if (coverageEnd) setDate(coverageEnd);
  }, [coverageEnd]);

  const { data: expiriesData } = useQuery({
    queryKey: ["options-expiries", underlying, date],
    queryFn: () => api.optionsExpiries(underlying, date),
    enabled: !!date,
  });
  const expiries = expiriesData?.expiries ?? [];

  // Default the expiry to the nearest one whenever the list changes.
  useEffect(() => {
    if (expiries.length && !expiries.includes(expiry)) setExpiry(expiries[0]);
  }, [expiries, expiry]);

  const { data: chain, isLoading, error } = useQuery({
    queryKey: ["options-chain", underlying, date, expiry, greeks],
    queryFn: () => api.optionsChain(underlying, date, expiry, greeks),
    enabled: !!date && !!expiry,
  });

  return (
    <Card>
      <div className="flex flex-wrap items-end gap-3 mb-3">
        <div className="text-sm font-medium text-slate-300">Historical option chain</div>
        <label className="block">
          <span className="block text-xs text-slate-400 mb-1">Trade date</span>
          <input type="date" className={inputClass} value={date} onChange={(e) => setDate(e.target.value)} />
        </label>
        <label className="block">
          <span className="block text-xs text-slate-400 mb-1">Expiry</span>
          <select className={inputClass} value={expiry} onChange={(e) => setExpiry(e.target.value)}>
            {expiries.length === 0 && <option value="">—</option>}
            {expiries.map((e) => (
              <option key={e} value={e}>{e}</option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1.5 text-xs text-slate-300 pb-1.5">
          <input type="checkbox" checked={greeks} onChange={(e) => setGreeks(e.target.checked)} />
          show IV / greeks
        </label>
        {chain?.spot != null && (
          <span className="text-xs text-slate-400 pb-1.5">
            spot {formatInr(chain.spot)} · ATM {chain.atm_strike}
          </span>
        )}
        {chain?.synthetic && (
          <span className="text-[11px] text-amber-700 dark:text-amber-300 pb-1.5">⚠ synthetic — model prices (BS), not traded premiums</span>
        )}
      </div>
      {isLoading ? (
        <Spinner />
      ) : error ? (
        <ErrorBox message={(error as Error).message} />
      ) : chain ? (
        <OptionChainTable rows={chain.rows} atm={chain.atm_strike} greeks={greeks} />
      ) : (
        <div className="text-sm text-slate-500">Pick a trade date and expiry.</div>
      )}
    </Card>
  );
}

// GOLD has no bhavcopy — its underlying is the MCX futures series, fetched on the shared
// Kite session (same as equity refresh) and cached as the "GOLD" price series.
function GoldRefreshControl({ onDone }: { onDone: () => void }) {
  const { data: accounts } = useQuery({ queryKey: ["brokers"], queryFn: brokers.list });
  const sessioned = (accounts ?? []).find((a) => a.has_session);
  const [start, setStart] = useState("2020-01-01");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  async function run() {
    if (!sessioned) return;
    setBusy(true);
    setMsg(null);
    try {
      const { refreshed } = await brokers.refreshGold(sessioned.id, { start_date: start });
      const r = refreshed.GOLD;
      setMsg(r?.error ? `Error: ${r.error}` : `Cached ${r?.rows ?? 0} days → ${r?.last_date ?? "?"}`);
      onDone();
    } catch (e) {
      setMsg((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <div className="flex flex-wrap items-end gap-3">
        <label className="block">
          <span className="block text-xs text-slate-400 mb-1">From</span>
          <input type="date" className={inputClass} value={start} onChange={(e) => setStart(e.target.value)} />
        </label>
        <button
          onClick={run}
          disabled={busy || !sessioned}
          className="rounded-md bg-brand hover:bg-brand-light px-3 py-1.5 text-sm font-medium disabled:opacity-50"
        >
          {busy ? "Fetching…" : "Fetch GOLD futures (MCX)"}
        </button>
        <span className="text-xs text-slate-500">
          GOLD futures from your Kite session (MCX) — the synthetic chain is priced off this series.
          {!sessioned && " Log in on Brokers first."}
        </span>
        {msg && <span className="text-xs text-slate-400">{msg}</span>}
      </div>
    </Card>
  );
}

// ------------------------------------------------- self-captured 1-min bar store
const _mb = (b: number) => (b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB` : `${(b / 1e6).toFixed(1)} MB`);
const _dt = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Kolkata", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
  hour12: false,
});
const _hhmm = (iso?: string | null) => (iso ? String(iso).slice(11, 16) : "—");

/** The self-captured 1-min option-bar store (the GFD replacement): freshness, capture
 * config, and a per-day history. Data comes from local Parquet files — no broker needed. */
function IntradayBarsCard() {
  const [captureMsg, setCaptureMsg] = useState<string | null>(null);
  const [captureBusy, setCaptureBusy] = useState(false);
  const { data, isLoading, refetch } = useQuery({
    queryKey: ["option-bars-store"],
    queryFn: () => api.optionBarsStore(30),
    // Poll fast while a capture runs so the row appears as soon as it lands.
    refetchInterval: (q) => (q.state.data?.capture_running ? 5_000 : 60_000),
    retry: false,
  });

  async function captureNow() {
    setCaptureBusy(true);
    setCaptureMsg(null);
    try {
      const out = await api.optionBarsCaptureNow();
      setCaptureMsg(out.started
        ? `Started — capturing ${out.target_day} (+ recent gaps) then backing up…`
        : out.reason ?? "not started");
      refetch();
    } catch (e) {
      setCaptureMsg((e as Error).message); // e.g. 409: capture opens at 15:45 IST
    } finally {
      setCaptureBusy(false);
    }
  }

  if (isLoading || !data) return <Card><Spinner /></Card>;

  const unders = ["NIFTY", "BANKNIFTY", "SENSEX"].filter((u) =>
    data.days.some((d) => d.underlyings[u] != null));
  // Freshness: with capture ON, the newest day going >3 days stale means the EOD job
  // isn't landing (session missing at 15:45 / subscription issue) — flag it.
  const staleDays = data.last_day
    ? Math.round((Date.now() - new Date(`${data.last_day}T15:30:00+05:30`).getTime()) / 86_400_000)
    : null;
  const stale = data.capture.enabled && (staleDays == null || staleDays > 3);

  const Tile = ({ label, value, title }: { label: string; value: string; title?: string }) => (
    <div className="rounded-md bg-slate-800/40 px-3 py-2" title={title}>
      <div className="text-slate-400 text-xs">{label}</div>
      <div className="text-sm font-medium tabular-nums">{value}</div>
    </div>
  );

  return (
    <Card>
      <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
        <div className="text-sm font-medium text-slate-300">
          Intraday 1-min bars · self-captured store
          <span className="ml-2 text-xs text-slate-500">EOD Kite capture + GFD imports</span>
        </div>
        <div className="flex items-center gap-2 text-xs">
          <button
            onClick={captureNow}
            disabled={captureBusy || data.capture_running}
            title={`Capture any missing days (allowed after ${data.capture.after} IST on a trading day; anytime on weekends) + mirror to the backup. Already-captured days are skipped.`}
            className="rounded-md bg-brand hover:bg-brand-light px-2.5 py-1 text-xs font-medium disabled:opacity-50"
          >
            {data.capture_running ? "Capturing…" : captureBusy ? "Starting…" : "Capture & backup now"}
          </button>
          {data.capture.enabled ? (
            <span className="rounded px-2 py-0.5 bg-emerald-950/50 text-emerald-400 border border-emerald-800">
              capture ON · daily ≥ {data.capture.after} IST
            </span>
          ) : (
            <span className="rounded px-2 py-0.5 bg-slate-800 text-slate-400 border border-slate-700"
              title="Set SKAS_OPTION_BARS_CAPTURE_ENABLED=true on the data box to capture daily">
              capture OFF
            </span>
          )}
          {stale && !data.capture_running && (
            <span className="rounded px-2 py-0.5 bg-amber-950/50 text-amber-400 border border-amber-800"
              title="Capture is enabled but the newest stored day is old — check the backend log / Kite session">
              stale ⚠
            </span>
          )}
        </div>
      </div>

      {data.capture_running && data.capture_progress && (
        <div className="mb-2">
          <div className="text-xs text-slate-400 mb-1 tabular-nums">
            Capturing {data.capture_progress.day} · {data.capture_progress.done.toLocaleString("en-IN")}
            /{data.capture_progress.total.toLocaleString("en-IN")} contracts
            · day {data.capture_progress.day_index}/{data.capture_progress.days_total}
            {data.capture_progress.total > 0 &&
              ` · ${Math.round((data.capture_progress.done / data.capture_progress.total) * 100)}%`}
          </div>
          <div className="h-1.5 rounded bg-slate-800 overflow-hidden">
            <div className="h-full bg-brand transition-all"
              style={{ width: `${data.capture_progress.total > 0 ? (data.capture_progress.done / data.capture_progress.total) * 100 : 0}%` }} />
          </div>
        </div>
      )}
      {captureMsg && <div className="mb-2 text-xs text-slate-400">{captureMsg}</div>}
      {(() => {
        // A capture that fetched nothing must LOOK broken, not done (the 2026-07-15
        // "1minute"-interval bug ran 100 minutes of silent failures).
        const ds = data.last_capture?.days ?? [];
        const errs = ds.reduce((s, d) => s + (d.errors ?? 0), 0);
        const got = ds.reduce((s, d) => s + (d.with_data ?? 0), 0);
        if (!ds.length || errs === 0) return null;
        return (
          <div className={`mb-2 text-xs ${got === 0 ? "text-rose-400" : "text-amber-400"}`}>
            {got === 0
              ? `⚠ Last capture FAILED — all ${errs.toLocaleString("en-IN")} contract fetches errored (no data stored). Check the historical-data subscription / backend log, then hit Capture & backup now.`
              : `⚠ Last capture had ${errs.toLocaleString("en-IN")} failed fetches (${got.toLocaleString("en-IN")} contracts stored).`}
          </div>
        );
      })()}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-2 text-sm mb-3">
        <Tile label="Days stored" value={String(data.days_total)}
          title={data.first_day ? `${data.first_day} → ${data.last_day}` : undefined} />
        <Tile label="Rows (1-min bars)" value={data.rows_total.toLocaleString("en-IN")} />
        <Tile label="Disk" value={_mb(data.bytes_total)} title={data.path} />
        <Tile label="Latest day" value={data.last_day ?? "—"} />
        <Tile label="Last capture run"
          value={data.last_capture?.at ? _dt.format(new Date(data.last_capture.at)) : "— (none yet)"}
          title={data.last_capture?.account ? `via ${data.last_capture.account}` : undefined} />
      </div>

      {data.days.length ? (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-slate-400 text-left">
              <tr>
                <th className="py-1 pr-4">Day</th>
                <th className="py-1 pr-4 text-right">Rows</th>
                <th className="py-1 pr-4 text-right">Contracts</th>
                {unders.map((u) => <th key={u} className="py-1 pr-4 text-right">{u}</th>)}
                <th className="py-1 pr-4">Bars</th>
                <th className="py-1 pr-4 text-right">Size</th>
              </tr>
            </thead>
            <tbody className="text-slate-300">
              {data.days.map((d) => (
                <tr key={d.day} className="border-t border-slate-800 tabular-nums">
                  <td className="py-1 pr-4">{d.day}</td>
                  <td className="py-1 pr-4 text-right">{d.rows.toLocaleString("en-IN")}</td>
                  <td className="py-1 pr-4 text-right">{d.contracts}</td>
                  {unders.map((u) => (
                    <td key={u} className="py-1 pr-4 text-right">{d.underlyings[u] ?? "—"}</td>
                  ))}
                  <td className="py-1 pr-4">{_hhmm(d.first_bar)} → {_hhmm(d.last_bar)}</td>
                  <td className="py-1 pr-4 text-right">{_mb(d.bytes)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-2 text-[11px] text-slate-500">
            Universe: {data.capture.underlyings} · expiries ≤ {data.capture.expiry_days}d ·
            strikes ±{data.capture.strike_pct}% of spot · volume + OI captured · showing the
            latest {data.days.length} day{data.days.length === 1 ? "" : "s"} · {data.path}
            {data.capture.backup_dir
              ? <> · backup → {data.capture.backup_dir.includes("CloudStorage/GoogleDrive") ? "Google Drive" : data.capture.backup_dir}</>
              : <> · <span className="text-amber-500/80">no off-box backup configured (SKAS_OPTION_BARS_BACKUP_DIR)</span></>}
          </div>
        </div>
      ) : (
        <div className="text-sm text-slate-500">
          No days stored yet — the first capture lands after {data.capture.after} IST on a
          trading day (or import purchased files: <code>skas-algo import-gfd &lt;csv…&gt;</code>).
        </div>
      )}
    </Card>
  );
}

export function OptionsDataSection() {
  const [underlying, setUnderlying] = useState("NIFTY");
  const [refreshKey, setRefreshKey] = useState(0);
  const isGold = underlying === "GOLD";
  const { data: cov } = useQuery<DerivCoverage>({
    queryKey: ["options-coverage", underlying, refreshKey],
    queryFn: () => api.optionsCoverage(underlying),
    retry: false,
  });
  return (
    <div className="space-y-4">
      <IntradayBarsCard />
      <UnderlyingSelector value={underlying} onChange={setUnderlying} items={OPT_UNDERLYINGS} />
      {isGold && (
        <div className="text-[11px] text-amber-700 dark:text-amber-300/90">
          GOLD is on MCX (no NSE bhavcopy) — its chain is <b>synthetic</b>: Black-76 (options on futures)
          priced from the realized volatility of the cached GOLD futures series × an implied-vol premium,
          on GOLDM specs (100 g lot, ₹500 strikes, expiry ~26th). Useful for mechanics, not market premiums.
        </div>
      )}
      <CoverageCard underlying={underlying} kind="options" refreshKey={refreshKey} />
      {isGold ? (
        <GoldRefreshControl onDone={() => setRefreshKey((k) => k + 1)} />
      ) : (
        <RefreshControl underlying={underlying} kind="options" onDone={() => setRefreshKey((k) => k + 1)} coverageEnd={cov?.end_date} />
      )}
      <OptionChainViewer underlying={underlying} coverageEnd={cov?.end_date} />
    </div>
  );
}

// --------------------------------------------------------------------- futures
function FuturesChart({ underlying, refreshKey }: { underlying: string; refreshKey: number }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["futures-series", underlying, refreshKey],
    queryFn: () => api.futuresSeries(underlying),
    retry: false,
  });
  const points = useMemo(() => (data?.points ?? []).filter((p) => p.close != null), [data]);
  return (
    <Card>
      <div className="text-sm font-medium text-slate-300 mb-3">Front-month futures (continuous)</div>
      {isLoading ? (
        <Spinner />
      ) : error || points.length === 0 ? (
        <div className="text-sm text-slate-500">No cached futures for {underlying} yet — refresh a date range above.</div>
      ) : (
        <ResponsiveContainer width="100%" height={280}>
          <LineChart data={points} margin={{ top: 5, right: 10, bottom: 0, left: 10 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
            <XAxis dataKey="date" tick={{ fontSize: 11, fill: "#94a3b8" }} minTickGap={40} />
            <YAxis
              tick={{ fontSize: 11, fill: "#94a3b8" }}
              width={64}
              domain={["auto", "auto"]}
              tickFormatter={(v) => `${(v / 1e3).toFixed(1)}k`}
            />
            <Tooltip
              contentStyle={{ background: "rgb(var(--slate-900))", border: "1px solid rgb(var(--slate-700))", color: "rgb(var(--slate-100))" }}
              formatter={(v: number) => formatInr(v)}
              labelFormatter={(d, payload) => {
                const exp = payload?.[0]?.payload?.expiry;
                return exp ? `${d} (exp ${exp})` : String(d);
              }}
            />
            <Line type="monotone" dataKey="close" name="Close" stroke="#14b8a6" dot={false} strokeWidth={2} />
          </LineChart>
        </ResponsiveContainer>
      )}
    </Card>
  );
}

export function FuturesDataSection() {
  const [underlying, setUnderlying] = useState("NIFTY");
  const [refreshKey, setRefreshKey] = useState(0);
  const { data: cov } = useQuery<DerivCoverage>({
    queryKey: ["futures-coverage", underlying, refreshKey],
    queryFn: () => api.futuresCoverage(underlying),
    retry: false,
  });
  return (
    <div className="space-y-4">
      <UnderlyingSelector value={underlying} onChange={setUnderlying} items={FUT_UNDERLYINGS} />
      <CoverageCard underlying={underlying} kind="futures" refreshKey={refreshKey} />
      <RefreshControl underlying={underlying} kind="futures" onDone={() => setRefreshKey((k) => k + 1)} coverageEnd={cov?.end_date} />
      <FuturesChart underlying={underlying} refreshKey={refreshKey} />
    </div>
  );
}
