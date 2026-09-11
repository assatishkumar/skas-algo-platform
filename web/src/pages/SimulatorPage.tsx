/** The Simulator (2026-09-11): manual backtesting, cycle by cycle, in the console.
 *
 *  A Simulator strategy is a backtest whose decisions are the owner's. It is stored as an
 *  ordinary run (hidden from the Runs list) so Run detail, Analyze and Compare work on it by
 *  link; this page is the ledger and the scoreboard: create a strategy with its playbook,
 *  open the console on it, and read the banked cycles. Nothing unrealised enters the stats. */
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api/client";
import { Card, ErrorBox, Spinner } from "../components/ui";
import { formatInr } from "../lib/format";
import type { SimCycle, SimDetail } from "../types";

const inr = (v: number | null | undefined) => (v == null ? "—" : formatInr(v));
const pct = (v: number | null | undefined, dp = 1) => (v == null ? "—" : `${v.toFixed(dp)}%`);
const when = (iso: string | null | undefined) => (iso ? iso.slice(0, 16).replace("T", " ") : "—");

function Kpi({ label, value, tone, sub }: { label: string; value: string; tone?: "pos" | "neg"; sub?: string }) {
  return (
    <div className="rounded-lg px-3 py-2" style={{ background: "var(--panel)" }}>
      <div className="text-[10px] font-semibold uppercase tracking-[.06em] text-[var(--faint)]">{label}</div>
      <div className={`text-[15px] font-semibold tabular-nums ${tone === "pos" ? "text-emerald-600 dark:text-emerald-400" : tone === "neg" ? "text-rose-600 dark:text-rose-400" : ""}`}>{value}</div>
      {sub && <div className="text-[10.5px] text-[var(--muted)]">{sub}</div>}
    </div>
  );
}

function stats(d: SimDetail) {
  const c = d.cycle_rows;
  const nets = c.map((x) => x.net);
  const wins = nets.filter((n) => n > 0).length;
  const roms = c.map((x) => x.rom_pct).filter((x): x is number => x != null);
  const held = c.map((x) => (x.entered && x.exited ? (Date.parse(x.exited) - Date.parse(x.entered)) / 86400000 : null)).filter((x): x is number => x != null);
  let peak = d.capital, dd = 0, eq = d.capital;
  for (const n of nets) { eq += n; peak = Math.max(peak, eq); dd = Math.max(dd, peak - eq); }
  return {
    net: nets.reduce((a, b) => a + b, 0), n: c.length, wins,
    avg: c.length ? nets.reduce((a, b) => a + b, 0) / c.length : null,
    best: c.length ? Math.max(...nets) : null, worst: c.length ? Math.min(...nets) : null,
    maxDd: dd, charges: c.reduce((a, x) => a + x.charges, 0),
    avgRom: roms.length ? roms.reduce((a, b) => a + b, 0) / roms.length : null,
    avgHeld: held.length ? held.reduce((a, b) => a + b, 0) / held.length : null,
  };
}

export default function SimulatorPage() {
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const selected = params.get("id") ? Number(params.get("id")) : null;
  const list = useQuery({ queryKey: ["sim", "list"], queryFn: api.simList });
  const detail = useQuery({ queryKey: ["sim", selected], queryFn: () => api.simGet(selected!), enabled: selected != null });
  const days = useQuery({ queryKey: ["consoleDays", "NIFTY"], queryFn: () => api.consoleDays("NIFTY") });
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ name: "", underlying: "NIFTY", capital: "500000", playbook: "", start_day: "" });
  const [playbook, setPlaybook] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const refresh = () => { qc.invalidateQueries({ queryKey: ["sim"] }); };
  const create = useMutation({
    mutationFn: () => api.simCreate({ name: form.name, underlying: form.underlying, capital: Number(form.capital) || 500000,
      playbook: form.playbook || null, start_day: form.start_day || null }),
    onSuccess: (d) => { setCreating(false); setForm({ name: "", underlying: "NIFTY", capital: "500000", playbook: "", start_day: "" }); refresh(); setParams({ id: String(d.id) }); },
  });
  const save = useMutation({ mutationFn: (body: { name?: string; playbook?: string }) => api.simUpdate(selected!, body), onSuccess: () => { setPlaybook(null); refresh(); } });
  const del = useMutation({ mutationFn: () => api.simDelete(selected!), onSuccess: () => { setConfirmDelete(false); setParams({}); refresh(); } });
  const d = detail.data ?? null;
  const st = useMemo(() => (d ? stats(d) : null), [d]);
  const curve = useMemo(() => (d ? [{ date: "start", equity: d.capital }, ...d.equity_curve] : []), [d]);

  return (
    <div className="grid gap-4 md:grid-cols-[300px_1fr]">
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <h1 className="text-lg font-semibold">Simulator</h1>
          <button type="button" onClick={() => setCreating((v) => !v)}
            className="rounded bg-brand text-white px-2.5 py-1 text-xs font-medium">+ New strategy</button>
        </div>
        {creating && (
          <Card>
            <div className="space-y-2 text-sm">
              <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Name (e.g. Weekly iron fly)"
                className="w-full rounded border border-[var(--line)] bg-transparent px-2 py-1" />
              <div className="flex gap-2">
                <select value={form.underlying} onChange={(e) => setForm({ ...form, underlying: e.target.value })}
                  className="rounded border border-[var(--line)] bg-transparent px-2 py-1">
                  {(days.data?.underlyings ?? ["NIFTY", "BANKNIFTY", "SENSEX"]).map((u) => <option key={u} value={u}>{u}</option>)}
                </select>
                <input value={form.capital} onChange={(e) => setForm({ ...form, capital: e.target.value })} inputMode="numeric" placeholder="Capital"
                  className="flex-1 rounded border border-[var(--line)] bg-transparent px-2 py-1 tabular-nums" />
              </div>
              <input type="date" value={form.start_day} min={days.data?.first ?? undefined} max={days.data?.last ?? undefined}
                onChange={(e) => setForm({ ...form, start_day: e.target.value })} title="first day of cycle 1 (a captured day)"
                className="w-full rounded border border-[var(--line)] bg-transparent px-2 py-1" />
              <textarea value={form.playbook} onChange={(e) => setForm({ ...form, playbook: e.target.value })} rows={3}
                placeholder="Playbook — the rule you are testing: entry day, structure, adjustments, exits"
                className="w-full rounded border border-[var(--line)] bg-transparent px-2 py-1" />
              <div className="flex gap-2 justify-end">
                <button type="button" onClick={() => setCreating(false)} className="text-xs text-[var(--muted)] underline">cancel</button>
                <button type="button" disabled={!form.name.trim() || create.isPending} onClick={() => create.mutate()}
                  className="rounded bg-brand text-white px-2.5 py-1 text-xs font-medium disabled:opacity-40">Create</button>
              </div>
              {create.error && <ErrorBox message={(create.error as Error).message} />}
            </div>
          </Card>
        )}
        {list.isLoading && <Spinner />}
        {list.error && <ErrorBox message={(list.error as Error).message} />}
        {(list.data?.strategies ?? []).map((s) => (
          <button key={s.id} type="button" onClick={() => setParams({ id: String(s.id) })}
            className={`w-full text-left rounded-lg border px-3 py-2 ${selected === s.id ? "border-brand bg-brand/5" : "border-[var(--line)]"}`}>
            <div className="flex items-center justify-between text-sm font-medium">
              <span>{s.name}</span>
              <span className="text-[11px] text-[var(--muted)]">{s.underlying} · {s.cycles} cyc{s.open ? " · open" : ""}</span>
            </div>
            <div className="text-[11.5px] tabular-nums text-[var(--muted)]">
              <span className={s.net >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400"}>{s.net >= 0 ? "+" : ""}{inr(s.net)}</span>
              {s.win_rate != null ? ` · ${Math.round(s.win_rate)}%` : ""}{s.last_exit ? ` · last ${s.last_exit.slice(0, 10)}` : " · not started"}
            </div>
          </button>
        ))}
        {list.data && list.data.strategies.length === 0 && !creating && (
          <div className="text-sm text-[var(--muted)]">No strategies yet. Create one, then trade it in the console.</div>
        )}
      </div>

      <div className="space-y-4">
        {selected == null && <Card><div className="text-sm text-[var(--muted)]">Pick a strategy on the left, or create one.</div></Card>}
        {selected != null && detail.isLoading && <Spinner />}
        {selected != null && detail.error && <ErrorBox message={(detail.error as Error).message} />}
        {d && st && (
          <>
            <Card>
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div>
                  <div className="text-base font-semibold">{d.name} <span className="text-[var(--muted)] font-normal">· {d.underlying}</span></div>
                  <div className="text-[12px] text-[var(--muted)] tabular-nums">
                    capital {inr(d.capital)} → equity {inr(d.equity)} ({d.capital_mode}){d.next_day ? ` · next cycle opens ${d.next_day}` : ""}
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <Link to={`/console?sim=${d.id}`} className="rounded bg-brand text-white px-3 py-1.5 text-xs font-semibold">
                    {d.open_cycle ? `Continue cycle ${d.cycles + 1} in console` : `Start cycle ${d.cycles + 1} in console`}
                  </Link>
                  {confirmDelete ? (
                    <span className="text-xs">delete this strategy and its {d.cycles} cycles?
                      <button type="button" onClick={() => del.mutate()} className="ml-1 text-rose-600 underline">yes</button>
                      <button type="button" onClick={() => setConfirmDelete(false)} className="ml-1 underline">no</button></span>
                  ) : (
                    <button type="button" onClick={() => setConfirmDelete(true)} className="text-xs text-[var(--muted)] underline">delete</button>
                  )}
                </div>
              </div>
              <div className="mt-2">
                <div className="text-[10px] font-semibold uppercase tracking-[.06em] text-[var(--faint)]">Playbook · the rule being tested</div>
                <textarea value={playbook ?? d.playbook} onChange={(e) => setPlaybook(e.target.value)} rows={2}
                  className="mt-1 w-full rounded border border-[var(--line)] bg-transparent px-2 py-1 text-sm" />
                {playbook != null && playbook !== d.playbook && (
                  <button type="button" onClick={() => save.mutate({ playbook })} className="mt-1 text-xs underline">save playbook</button>
                )}
              </div>
            </Card>

            <div className="grid grid-cols-2 md:grid-cols-5 gap-2">
              <Kpi label="Net" value={inr(st.net)} tone={st.net >= 0 ? "pos" : "neg"} sub={`${d.metrics["Total Return %"] ?? 0}% on capital`} />
              <Kpi label="Cycles" value={String(st.n)} sub={st.n ? `${st.wins} won · ${Math.round((100 * st.wins) / st.n)}%` : "none banked yet"} />
              <Kpi label="Avg / cycle" value={inr(st.avg)} tone={st.avg != null ? (st.avg >= 0 ? "pos" : "neg") : undefined} sub={st.avgHeld != null ? (st.avgHeld < 1 ? `held ${(st.avgHeld * 24).toFixed(1)} h avg` : `held ${st.avgHeld.toFixed(1)} d avg`) : undefined} />
              <Kpi label="Best · worst" value={`${inr(st.best)} · ${inr(st.worst)}`} />
              <Kpi label="Max drawdown" value={inr(-st.maxDd)} tone="neg" sub={d.metrics["Max Drawdown %"] != null ? `${d.metrics["Max Drawdown %"]}%` : undefined} />
              <Kpi label="Charges" value={inr(st.charges)} />
              <Kpi label="Avg RoM" value={pct(st.avgRom)} sub="net ÷ margin at entry" />
              <Kpi label="CAGR" value={d.metrics["CAGR %"] != null ? pct(d.metrics["CAGR %"] as number) : "—"} sub="only past 3 months of record" />
              <Kpi label="Open cycle" value={d.open_cycle ? `${d.open_cycle.fills} fills` : "none"} sub={d.open_cycle?.entered ? `entered ${when(d.open_cycle.entered)}` : d.open_cycle ? `at ${d.open_cycle.day} ${d.open_cycle.clock}` : undefined} />
              <Kpi label="Record" value={d.run_id ? `run #${d.run_id}` : "—"} sub="hidden from the Runs list" />
            </div>

            {curve.length > 1 && (
              <Card>
                <div className="text-[10px] font-semibold uppercase tracking-[.06em] text-[var(--faint)] mb-1">Equity · banked cycles only</div>
                <div style={{ height: 200 }}>
                  <ResponsiveContainer>
                    <AreaChart data={curve} margin={{ top: 6, right: 12, left: 0, bottom: 0 }}>
                      <CartesianGrid stroke="var(--hair)" vertical={false} />
                      <XAxis dataKey="date" tick={{ fontSize: 10 }} minTickGap={40} />
                      <YAxis tick={{ fontSize: 10 }} width={78} tickFormatter={(v: number) => inr(v)} domain={["auto", "auto"]} />
                      <Tooltip formatter={(v: number) => inr(v)} />
                      <Area type="stepAfter" dataKey="equity" stroke="var(--brand, #3A44C8)" fill="var(--brand, #3A44C8)" fillOpacity={0.12} strokeWidth={2} />
                    </AreaChart>
                  </ResponsiveContainer>
                </div>
              </Card>
            )}

            <Card>
              <div className="flex items-center justify-between mb-2">
                <div className="text-[10px] font-semibold uppercase tracking-[.06em] text-[var(--faint)]">Cycles</div>
                <div className="flex gap-3 text-xs">
                  <Link to={`/runs/${d.run_id}`} className="underline">Open as run</Link>
                  <Link to={`/analyze?run=${d.run_id}`} className="underline">Analyze</Link>
                  <Link to={`/compare?ids=${d.run_id}`} className="underline">Compare</Link>
                </div>
              </div>
              {d.cycle_rows.length === 0 ? (
                <div className="text-sm text-[var(--muted)]">Nothing banked yet. Start the first cycle in the console; the moment the book is flat after trading, a Bank sheet appears there.</div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-[12.5px] tabular-nums whitespace-nowrap">
                    <thead className="text-[10px] uppercase tracking-[.06em] text-[var(--faint)]">
                      <tr><th className="text-left py-1">#</th><th className="text-left">Entered</th><th className="text-left">Exited</th><th className="text-right">Held</th>
                        <th className="text-right">Spot in → out</th><th className="text-right">Move</th><th className="text-right">Legs</th><th className="text-right">Margin</th>
                        <th className="text-right">Net</th><th className="text-right">RoM</th><th className="text-left pl-3">Note</th><th></th></tr>
                    </thead>
                    <tbody>
                      {[...d.cycle_rows].reverse().map((c: SimCycle) => (
                        <tr key={c.n} className="border-t border-[var(--hair)] align-top">
                          <td className="py-1.5">{c.n}</td>
                          <td>{when(c.entered)}</td>
                          <td>{when(c.exited)}</td>
                          <td className="text-right">{c.entered && c.exited ? `${((Date.parse(c.exited) - Date.parse(c.entered)) / 86400000).toFixed(1)}d` : "—"}</td>
                          <td className="text-right">{c.entry_spot?.toLocaleString("en-IN", { maximumFractionDigits: 0 }) ?? "—"} → {c.exit_spot?.toLocaleString("en-IN", { maximumFractionDigits: 0 }) ?? "—"}</td>
                          <td className="text-right">{c.entry_spot && c.exit_spot ? pct((100 * (c.exit_spot - c.entry_spot)) / c.entry_spot, 2) : "—"}</td>
                          <td className="text-right" title={c.symbols.join(", ")}>{c.legs.length}</td>
                          <td className="text-right" title={c.margin_source ?? undefined}>{inr(c.margin)}</td>
                          <td className={`text-right font-semibold ${c.net >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400"}`}>{inr(c.net)}</td>
                          <td className="text-right">{pct(c.rom_pct, 2)}</td>
                          <td className="pl-3 whitespace-normal max-w-[320px] text-[var(--muted)]">{c.note || "—"}{c.tags?.length ? ` · ${c.tags.join(", ")}` : ""}</td>
                          <td className="text-right"><Link to={`/console?sim=${d.id}&cycle=${c.n}`} className="underline" title="watch this cycle back in the console, read-only">⟲</Link></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
          </>
        )}
      </div>
    </div>
  );
}
