import { useMutation, useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Bar,
  BarChart,
  Brush,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api, brokers } from "../api/client";
import { ErrorBox, NumberInput } from "../components/ui";
import { Panel, Segmented, SessionBanner } from "../components/redesign";
import type { BsCalibrationResult, CalibStats, DonchianStudyResult, StudyLeagueRow } from "../types";

/** Research page — validates the Donchian strangle two ways:
 *  1. Breakout study (cache-only): per expiry-anchored monthly cycle, did each Nifty-50
 *     name stay inside the previous cycle's high/low channel — and if not, when/how far
 *     did it break, did it re-enter, whipsaw, or get closed by the live flip rules?
 *  2. BS calibration (needs a live session): today's Black-Scholes-with-HV premiums vs
 *     the live chain → the vol_multiplier the synthetic backtest should use. */

// Outcome palette — validated (dataviz six checks) on BOTH surfaces (#0f172a dark, #fff light):
// worst adjacent CVD ΔE 33.6, all ≥3:1 contrast. Identity is also carried by the legend +
// tables below, never color alone.
const OUTCOME_COLORS: Record<string, string> = {
  inside: "#059669",       // stayed in the channel — the strangle's friend
  breakout: "#3b82f6",     // broke out, never came back
  "re-entered": "#d97706", // broke out but closed back inside
  whipsaw: "#f43f5e",      // re-entered then breached again — the strangle killer
};
const OUTCOMES = ["inside", "breakout", "re-entered", "whipsaw"] as const;
const HISTO_COLOR = "#3b82f6";

const inputClass =
  "w-full rounded-[10px] bg-[var(--field)] border border-[var(--field-border)] px-3 py-2 text-sm text-[var(--strong)] focus:outline-none focus:border-[var(--accent)]";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="block text-xs uppercase tracking-wide text-[var(--muted)] mb-1">{label}</span>
      {children}
    </label>
  );
}

function Tile({ label, value, hint }: { label: string; value: React.ReactNode; hint?: string }) {
  return (
    <div className="rounded-[12px] border border-[var(--border)] bg-[var(--card)] px-3 py-2">
      <div className="text-[11px] uppercase tracking-wide text-[var(--muted)]">{label}</div>
      <div className="text-lg font-semibold text-[var(--strong)] tabular-nums">{value ?? "—"}</div>
      {hint && <div className="text-[11px] text-[var(--faint)]">{hint}</div>}
    </div>
  );
}

const pct = (v: number | null | undefined) => (v == null ? "—" : `${v}%`);

const STORE_KEY = "research.donchian.params";
type StudyForm = {
  start: string; end: string; buffer: number; basis: "touch" | "close"; maxFlips: number;
};
function loadForm(): StudyForm {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (raw) return { ...defaults, ...JSON.parse(raw) };
  } catch { /* ignore */ }
  return defaults;
}
const defaults: StudyForm = {
  start: "2010-01-01", end: new Date().toISOString().slice(0, 10),
  buffer: 0.5, basis: "touch", maxFlips: 3,
};

// ------------------------------------------------------------------ study section

function StudySection() {
  const [form, setForm] = useState<StudyForm>(loadForm);
  const [selectedCycle, setSelectedCycle] = useState<string | null>(null);
  const set = (patch: Partial<StudyForm>) =>
    setForm((f) => {
      const next = { ...f, ...patch };
      localStorage.setItem(STORE_KEY, JSON.stringify(next));
      return next;
    });

  const study = useMutation({
    mutationFn: () =>
      api.researchDonchianStudy({
        universe: "nifty50", start_date: form.start, end_date: form.end,
        buffer_pct: form.buffer, basis: form.basis, max_flips: form.maxFlips, detail: true,
      }),
    onSuccess: () => setSelectedCycle(null),
  });
  const res = study.data;

  return (
    <div className="space-y-4">
      <Panel className="p-4">
        <div className="flex flex-wrap items-end gap-3">
          <Field label="From">
            <input type="date" className={inputClass} value={form.start}
              onChange={(e) => set({ start: e.target.value })} />
          </Field>
          <Field label="To">
            <input type="date" className={inputClass} value={form.end}
              onChange={(e) => set({ end: e.target.value })} />
          </Field>
          <Field label="Breach buffer %">
            <NumberInput step="0.1" className={`${inputClass} w-24`} value={form.buffer}
              onChange={(v) => set({ buffer: v })} />
          </Field>
          <Field label="Breach basis">
            <Segmented
              value={form.basis}
              onChange={(v) => set({ basis: v })}
              options={[
                { value: "touch", label: "Touch (day H/L)" },
                { value: "close", label: "Close" },
              ]}
            />
          </Field>
          <Field label="Max flips">
            <NumberInput className={`${inputClass} w-20`} value={form.maxFlips}
              onChange={(v) => set({ maxFlips: v })} />
          </Field>
          <button
            onClick={() => study.mutate()}
            disabled={study.isPending}
            className="rounded-md bg-[var(--ft)] px-4 py-2 text-sm font-medium disabled:opacity-50"
          >
            {study.isPending ? "Computing…" : "Run study"}
          </button>
        </div>
        <div className="mt-2 text-[11px] text-[var(--faint)]">
          Expiry-anchored cycles: the Donchian range is the previous FULL expiry→expiry window
          (daily high/low); the trade window runs from the day after the monthly expiry to the
          next one. Nifty-50 stocks + the NIFTY 50 index, cached daily bars only.
        </div>
      </Panel>

      {study.error && <ErrorBox message={(study.error as Error).message} />}
      {res && <StudyResults res={res} selectedCycle={selectedCycle} onSelectCycle={setSelectedCycle} />}
    </div>
  );
}

function StudyResults({ res, selectedCycle, onSelectCycle }: {
  res: DonchianStudyResult;
  selectedCycle: string | null;
  onSelectCycle: (c: string | null) => void;
}) {
  const a = res.aggregates;
  const timeline = useMemo(
    () => res.cycles.map((c) => ({
      cycle: c.cycle_id,
      inside: c.inside,
      breakout: c.breakout,
      "re-entered": c["re-entered"],
      whipsaw: c.whipsaw,
    })),
    [res.cycles],
  );
  const histo = useMemo(() => {
    const bins = new Map<string, number>();
    const label = (d: number) => (d > 20 ? ">20" : String(d));
    for (const d of res.histograms.days_to_first_breach)
      bins.set(label(d), (bins.get(label(d)) ?? 0) + 1);
    return [...Array.from({ length: 20 }, (_, i) => String(i + 1)), ">20"]
      .map((k) => ({ day: k, count: bins.get(k) ?? 0 }));
  }, [res.histograms.days_to_first_breach]);

  return (
    <>
      <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-2">
        <Tile label="Name-cycles" value={a.name_cycles} hint={`${a.cycles} cycles × ${a.names} names`} />
        <Tile label="Stayed inside" value={pct(a.inside_pct)} hint="never breached the channel" />
        <Tile label="Breakouts up" value={pct(a.breakout_up_pct)} hint="share of first breaches" />
        <Tile label="Median days to breach" value={a.median_days_to_first_breach} hint="trading days into the cycle" />
        <Tile label="Re-entered" value={pct(a.re_entry_pct)} hint="of breakouts, closed back inside" />
        <Tile label="Whipsawed" value={pct(a.whipsaw_pct)} hint="of re-entries, breached again" />
        <Tile label="Closed by flips" value={pct(a.closed_by_flips_pct)} hint={`hit max ${res.params.max_flips} flips`} />
      </div>

      <Panel className="p-4">
        <div className="text-sm font-medium text-[var(--strong)] mb-1">
          Cycle outcomes over time <span className="text-[var(--faint)] font-normal">— names per cycle by what the channel did; click a bar to drill down</span>
        </div>
        <ResponsiveContainer width="100%" height={260}>
          <BarChart
            data={timeline}
            onClick={(s) => s?.activeLabel && onSelectCycle(String(s.activeLabel))}
            margin={{ top: 4, right: 8, left: -18, bottom: 0 }}
          >
            <CartesianGrid stroke="#64748b22" vertical={false} />
            <XAxis dataKey="cycle" tick={{ fontSize: 10, fill: "#94a3b8" }} minTickGap={40} />
            <YAxis tick={{ fontSize: 10, fill: "#94a3b8" }} allowDecimals={false} />
            <Tooltip
              contentStyle={{ background: "var(--card)", border: "1px solid var(--border)", borderRadius: 8, fontSize: 12 }}
              labelStyle={{ color: "var(--strong)" }}
              cursor={{ fill: "#64748b1a" }}
            />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            {OUTCOMES.map((k) => (
              <Bar key={k} dataKey={k} stackId="o" fill={OUTCOME_COLORS[k]} maxBarSize={14} />
            ))}
            <Brush dataKey="cycle" height={18} stroke="#64748b" travellerWidth={8} />
          </BarChart>
        </ResponsiveContainer>
      </Panel>

      <div className="grid lg:grid-cols-2 gap-4">
        <Panel className="p-4">
          <div className="text-sm font-medium text-[var(--strong)] mb-1">
            Days to first breach <span className="text-[var(--faint)] font-normal">— trading days into the cycle</span>
          </div>
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={histo} margin={{ top: 4, right: 8, left: -18, bottom: 0 }}>
              <CartesianGrid stroke="#64748b22" vertical={false} />
              <XAxis dataKey="day" tick={{ fontSize: 10, fill: "#94a3b8" }} />
              <YAxis tick={{ fontSize: 10, fill: "#94a3b8" }} allowDecimals={false} />
              <Tooltip
                contentStyle={{ background: "var(--card)", border: "1px solid var(--border)", borderRadius: 8, fontSize: 12 }}
                labelStyle={{ color: "var(--strong)" }}
                cursor={{ fill: "#64748b1a" }}
                formatter={(v: number) => [v, "name-cycles"]}
              />
              <Bar dataKey="count" fill={HISTO_COLOR} maxBarSize={16} radius={[3, 3, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </Panel>

        <Panel className="p-4">
          <div className="text-sm font-medium text-[var(--strong)] mb-2">
            By VIX at entry <span className="text-[var(--faint)] font-normal">— 2020+ (VIX coverage)</span>
          </div>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wide text-[var(--muted)]">
                <th className="py-1">VIX</th><th>Cycles</th><th>Name-cycles</th>
                <th>Inside</th><th>Whipsaw</th><th>Both edges</th><th>Closed</th>
              </tr>
            </thead>
            <tbody className="tabular-nums">
              {res.vix_split.map((b) => (
                <tr key={b.bucket} className="border-t border-[var(--divider)]">
                  <td className="py-1.5 font-medium">{b.bucket}</td>
                  <td>{b.cycles}</td><td>{b.name_cycles}</td>
                  <td>{pct(b.inside_pct)}</td><td>{pct(b.whipsaw_pct)}</td>
                  <td>{pct(b.both_sides_pct)}</td><td>{pct(b.closed_pct)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-3 text-[11px] text-[var(--faint)]">
            Whipsaw here = % of that bucket's name-cycles whose status ended whipsaw (vs the
            summary tile, which is % of re-entries).
          </div>
        </Panel>
      </div>

      <LeagueTable league={res.league} />

      {selectedCycle && (
        <CycleDetail res={res} cycleId={selectedCycle} onClose={() => onSelectCycle(null)} />
      )}

      <div className="text-[11px] text-[var(--faint)] space-y-0.5">
        {res.caveats.map((c, i) => <div key={i}>⚠ {c}</div>)}
      </div>
    </>
  );
}

function LeagueTable({ league }: { league: StudyLeagueRow[] }) {
  const [sortKey, setSortKey] = useState<keyof StudyLeagueRow>("breach_rate");
  const [desc, setDesc] = useState(true);
  const rows = useMemo(
    () =>
      [...league].sort((x, y) => {
        const a = x[sortKey] ?? -Infinity;
        const b = y[sortKey] ?? -Infinity;
        return desc ? (a < b ? 1 : -1) : a > b ? 1 : -1;
      }),
    [league, sortKey, desc],
  );
  const H = ({ k, label }: { k: keyof StudyLeagueRow; label: string }) => (
    <th
      className="cursor-pointer select-none py-1 pr-2 hover:text-[var(--strong)]"
      onClick={() => (sortKey === k ? setDesc(!desc) : (setSortKey(k), setDesc(true)))}
      title="Sort"
    >
      {label}{sortKey === k ? (desc ? " ↓" : " ↑") : ""}
    </th>
  );
  return (
    <Panel className="p-4 overflow-x-auto">
      <div className="text-sm font-medium text-[var(--strong)] mb-2">
        Per-name league <span className="text-[var(--faint)] font-normal">— across all cycles; click a header to sort</span>
      </div>
      <table className="w-full text-sm whitespace-nowrap">
        <thead>
          <tr className="text-left text-[11px] uppercase tracking-wide text-[var(--muted)]">
            <H k="symbol" label="Name" />
            <H k="cycles" label="Cycles" />
            <H k="breach_rate" label="Breach %" />
            <H k="up" label="Up" />
            <H k="down" label="Down" />
            <H k="re_entries" label="Re-entries" />
            <H k="whipsaws" label="Whipsaws" />
            <H k="both_sides" label="Both edges" />
            <H k="closed_by_flips" label="Closed" />
            <H k="avg_flips" label="Avg flips" />
            <H k="median_breach_day" label="Med breach day" />
            <H k="avg_excursion_pct" label="Avg excursion %" />
          </tr>
        </thead>
        <tbody className="tabular-nums">
          {rows.map((r) => (
            <tr key={r.symbol}
              className={`border-t border-[var(--divider)] ${r.is_index ? "bg-[var(--seg)] font-medium" : ""}`}>
              <td className="py-1.5 pr-2">{r.symbol}{r.is_index ? " (index)" : ""}</td>
              <td className="pr-2">{r.cycles}</td>
              <td className="pr-2">{r.breach_rate}%</td>
              <td className="pr-2">{r.up}</td>
              <td className="pr-2">{r.down}</td>
              <td className="pr-2">{r.re_entries}</td>
              <td className="pr-2">{r.whipsaws}</td>
              <td className="pr-2">{r.both_sides}</td>
              <td className="pr-2">{r.closed_by_flips}</td>
              <td className="pr-2">{r.avg_flips}</td>
              <td className="pr-2">{r.median_breach_day ?? "—"}</td>
              <td className="pr-2">{r.avg_excursion_pct ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Panel>
  );
}

function CycleDetail({ res, cycleId, onClose }: {
  res: DonchianStudyResult; cycleId: string; onClose: () => void;
}) {
  const cyc = res.cycles.find((c) => c.cycle_id === cycleId);
  const rows = (res.detail ?? []).filter((d) => d.cycle_id === cycleId);
  const order: Record<string, number> = { whipsaw: 0, breakout: 1, "re-entered": 2, inside: 3 };
  rows.sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9) || a.symbol.localeCompare(b.symbol));
  if (!cyc) return null;
  return (
    <Panel className="p-4 overflow-x-auto">
      <div className="flex items-center justify-between mb-2">
        <div className="text-sm font-medium text-[var(--strong)]">
          Cycle {cycleId}{" "}
          <span className="text-[var(--faint)] font-normal">
            — range {cyc.range_start} → {cyc.range_end}, traded {cyc.entry_date} → {cyc.expiry}
            {cyc.vix_entry != null ? ` · VIX ${cyc.vix_entry.toFixed(1)}` : ""}
          </span>
        </div>
        <button onClick={onClose} className="text-xs text-[var(--muted)] hover:text-[var(--strong)]">✕ close</button>
      </div>
      <table className="w-full text-sm whitespace-nowrap">
        <thead>
          <tr className="text-left text-[11px] uppercase tracking-wide text-[var(--muted)]">
            <th className="py-1 pr-2">Name</th><th className="pr-2">Outcome</th>
            <th className="pr-2">First breach</th><th className="pr-2">Day</th>
            <th className="pr-2">Max up %</th><th className="pr-2">Max down %</th>
            <th className="pr-2">Flips</th><th className="pr-2">Range</th>
          </tr>
        </thead>
        <tbody className="tabular-nums">
          {rows.map((d) => (
            <tr key={d.symbol} className="border-t border-[var(--divider)]">
              <td className="py-1.5 pr-2">{d.symbol}</td>
              <td className="pr-2">
                <span className="inline-flex items-center gap-1.5">
                  <span className="w-2.5 h-2.5 rounded-sm inline-block"
                    style={{ background: OUTCOME_COLORS[d.status] ?? "#64748b" }} />
                  {d.status}{d.closed_by_flips ? " · closed" : ""}
                  {d.breakout_at_entry ? ` · gapped ${d.breakout_at_entry} at entry` : ""}
                </span>
              </td>
              <td className="pr-2">{d.first_breach_side ?? "—"}</td>
              <td className="pr-2">{d.first_breach_day ?? "—"}</td>
              <td className="pr-2">{d.max_excursion_up_pct > 0 ? d.max_excursion_up_pct.toFixed(1) : "—"}</td>
              <td className="pr-2">{d.max_excursion_down_pct > 0 ? d.max_excursion_down_pct.toFixed(1) : "—"}</td>
              <td className="pr-2">{d.flip_count || "—"}</td>
              <td className="pr-2 text-[var(--muted)]">{d.range_low}–{d.range_high}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Panel>
  );
}

// ------------------------------------------------------------- calibration section

function statsCell(s: CalibStats | null | undefined) {
  return s ? `${s.median} (${s.q1}–${s.q3})` : "—";
}

function CalibrationSection() {
  const { data: accounts = [], isError: brokersError } = useQuery({
    queryKey: ["brokers"], queryFn: brokers.list,
  });
  const sessioned = accounts.filter((a) => a.has_session);
  const [accountId, setAccountId] = useState<number | null>(null);
  const effective = accountId ?? sessioned[0]?.id ?? null;

  const calib = useMutation({
    mutationFn: () => api.researchBsCalibration({ broker_account_id: effective! }),
  });
  const res: BsCalibrationResult | undefined = calib.data;
  const [showRows, setShowRows] = useState(false);

  return (
    <div className="space-y-4">
      <Panel className="p-4 space-y-3">
        <div className="text-sm font-medium text-[var(--strong)]">
          Black-Scholes vs live market{" "}
          <span className="text-[var(--faint)] font-normal">
            — the synthetic backtest prices stock options as BS(spot, K, t, r, HV20 × vol_multiplier).
            This compares those model prices with the LIVE chain at the strategy's strikes and
            suggests the multiplier.
          </span>
        </div>
        <SessionBanner
          backendDown={brokersError}
          hasSession={sessioned.length > 0}
          needs="Live chain quotes for the BS comparison"
        />
        {sessioned.length > 0 && (
          <div className="flex flex-wrap items-end gap-3">
            <Field label="Broker session">
              <select className={inputClass} value={effective ?? ""}
                onChange={(e) => setAccountId(Number(e.target.value))}>
                {sessioned.map((a) => (
                  <option key={a.id} value={a.id}>{a.label} ({a.user_id})</option>
                ))}
              </select>
            </Field>
            <button
              onClick={() => calib.mutate()}
              disabled={calib.isPending || effective == null}
              className="rounded-md bg-[var(--ft)] px-4 py-2 text-sm font-medium disabled:opacity-50"
            >
              {calib.isPending ? "Fetching 50 chains… (~15 s)" : "Compare BS vs market"}
            </button>
          </div>
        )}
      </Panel>

      {calib.error && <ErrorBox message={(calib.error as Error).message} />}

      {res && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
            <Tile
              label="Suggested vol multiplier"
              value={res.aggregates.suggested_vol_multiplier ?? "—"}
              hint="median IV/HV — use in the backtest form"
            />
            <Tile label="BS / market price" value={statsCell(res.aggregates.ratio)}
              hint="median (IQR); <1 = model prices cheap" />
            <Tile label="IV / HV — calls" value={statsCell(res.aggregates.by_right.CE)} />
            <Tile label="IV / HV — puts" value={statsCell(res.aggregates.by_right.PE)}
              hint="puts richer = the usual skew" />
          </div>

          <Panel className="p-4 overflow-x-auto">
            <div className="flex items-center justify-between mb-2">
              <div className="text-sm font-medium text-[var(--strong)]">
                By moneyness{" "}
                <span className="text-[var(--faint)] font-normal">
                  — {res.rows.length} contracts · expiry {res.sell_expiry} · as of {res.as_of}
                  {res.errors.length ? ` · ${res.errors.length} name(s) skipped` : ""}
                </span>
              </div>
              <div className="flex items-center gap-3">
                <Link to="/backtest?tab=new" className="text-xs underline text-[var(--accent-deep)]">
                  use in a backtest →
                </Link>
                <button onClick={() => setShowRows((v) => !v)}
                  className="text-xs text-[var(--muted)] hover:text-[var(--strong)]">
                  {showRows ? "hide contracts" : "show all contracts"}
                </button>
              </div>
            </div>
            <table className="w-full text-sm whitespace-nowrap">
              <thead>
                <tr className="text-left text-[11px] uppercase tracking-wide text-[var(--muted)]">
                  <th className="py-1 pr-2">|Strike − spot|</th>
                  <th className="pr-2">BS / market — median (IQR)</th>
                  <th className="pr-2">IV / HV — median (IQR)</th>
                </tr>
              </thead>
              <tbody className="tabular-nums">
                {res.aggregates.by_moneyness.map((b) => (
                  <tr key={b.bucket} className="border-t border-[var(--divider)]">
                    <td className="py-1.5 pr-2 font-medium">{b.bucket}</td>
                    <td className="pr-2">{statsCell(b.ratio)}</td>
                    <td className="pr-2">{statsCell(b.iv_over_hv)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {showRows && (
              <table className="w-full text-sm whitespace-nowrap mt-4">
                <thead>
                  <tr className="text-left text-[11px] uppercase tracking-wide text-[var(--muted)]">
                    <th className="py-1 pr-2">Contract</th><th className="pr-2">Kind</th>
                    <th className="pr-2">Spot</th><th className="pr-2">Market</th>
                    <th className="pr-2">BS @ HV</th><th className="pr-2">Ratio</th>
                    <th className="pr-2">IV %</th><th className="pr-2">HV %</th>
                    <th className="pr-2">IV/HV</th>
                  </tr>
                </thead>
                <tbody className="tabular-nums">
                  {[...res.rows].sort((a, b) => (b.iv_over_hv ?? 0) - (a.iv_over_hv ?? 0)).map((r, i) => (
                    <tr key={i} className="border-t border-[var(--divider)]">
                      <td className="py-1 pr-2">{r.symbol} {r.strike} {r.right}</td>
                      <td className="pr-2 text-[var(--muted)]">{r.kind}</td>
                      <td className="pr-2">{r.spot}</td>
                      <td className="pr-2">{r.market}</td>
                      <td className="pr-2">{r.bs_price}</td>
                      <td className="pr-2">{r.ratio}</td>
                      <td className="pr-2">{r.market_iv_pct ?? "—"}</td>
                      <td className="pr-2">{r.hv_pct}</td>
                      <td className="pr-2">{r.iv_over_hv ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Panel>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------- page

export default function ResearchPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold">Research — Donchian strangle</h1>
        <div className="text-sm text-[var(--muted)]">
          Pure-price breakout behaviour of the basket (does the channel hold?), and how honest
          the synthetic option pricing is (BS-with-HV vs the live market).
        </div>
      </div>
      <StudySection />
      <CalibrationSection />
    </div>
  );
}
