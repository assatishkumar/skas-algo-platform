import { useEffect, useMemo, useReducer, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useLocation, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import ReportView from "../components/ReportView";
import { ErrorBox } from "../components/ui";
import RunRail, { SweepBlock, type BatchProgress } from "../components/backtest/RunRail";
import {
  BasicsSection, EntrySection, ExitSection, SizingSection, StrategyParamsSection,
  UniversePeriodSection,
} from "../components/backtest/sections";
import { buildV2Body, toDisplayValue } from "../lib/backtestV2/build";
import type { Basis, StrategyFormSpec } from "../lib/backtestV2/registry";
import { allFields, defaultParams, TRAIL_UI, V2_REGISTRY } from "../lib/backtestV2/registry";
import type { PeriodState } from "../lib/backtestV2/period";
import { resolveWindow, windowLabel as fmtWindow } from "../lib/backtestV2/period";
import { DEFAULT_SIZING, sizingReducer } from "../lib/backtestV2/sizing";
import type { BacktestRequest, BacktestResponse } from "../types";

/** Backtest v2 — the sectioned options form (design_handoff_backtest_v2).
 *
 *  Everything the form can set is either a REAL strategy param (held in one `params` map,
 *  keyed by the actual kwarg, in display units) or sizing state. That single-object design
 *  is deliberate: the classic form's ~60 useStates need carefully ORDERED effects to apply
 *  templates without clobbering per-strategy resets (CLAUDE.md §9); here one effect owns
 *  the whole map, so ordering can't bite.
 */

interface ClonePrefill {
  strategy_id?: string;
  params?: Record<string, unknown>;
  capital?: number;
  start_date?: string;
  end_date?: string;
}

export default function BacktestFormV2({ strategyId, strategies, onStrategyChange }: {
  strategyId: string; strategies: string[]; onStrategyChange: (id: string) => void;
}) {
  const spec: StrategyFormSpec = V2_REGISTRY[strategyId];
  const navigate = useNavigate();
  const location = useLocation();
  const clonePrefill = (location.state as { clonePrefill?: ClonePrefill } | null)?.clonePrefill;

  const [name, setName] = useState("");
  const [notes, setNotes] = useState("");
  const [basis, setBasis] = useState<Basis>(spec.bases[0]);
  const [underlyings, setUnderlyings] = useState<string[]>([spec.underlyings[spec.bases[0]][0]]);
  const [period, setPeriod] = useState<PeriodState>({
    preset: "1Y", customStart: "", customEnd: "" });
  const [params, setParams] = useState<Record<string, number | string | boolean>>(
    () => defaultParams(spec, spec.bases[0]));
  const [sizing, dispatchSizing] = useReducer(sizingReducer, DEFAULT_SIZING);
  const [appliedTemplate, setAppliedTemplate] = useState<string | null>(null);

  const [batch, setBatch] = useState<BatchProgress | null>(null);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<BacktestResponse | null>(null);
  const [lastBody, setLastBody] = useState<BacktestRequest | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [sweepOn, setSweepOn] = useState(false);
  const [sweepField, setSweepField] = useState("");
  const [sweepValues, setSweepValues] = useState("");

  const { data: templatesData } = useQuery({ queryKey: ["templates"], queryFn: api.templates });
  const { data: barStore } = useQuery({
    queryKey: ["option-bars-store"], queryFn: () => api.optionBarsStore(1),
    enabled: basis === "intraday", retry: false });
  const { data: eodCov } = useQuery({
    queryKey: ["dataCoverage", "DERIV", underlyings[0]],
    queryFn: () => api.dataCoverage("DERIV", underlyings[0]),
    enabled: basis === "eod", retry: false });

  // Basis follows the strategy (most support exactly one); prune unsupported underlyings.
  useEffect(() => {
    if (!spec.bases.includes(basis)) setBasis(spec.bases[0]);
  }, [spec, basis]);
  useEffect(() => {
    const ok = spec.underlyings[basis] ?? [];
    setUnderlyings((cur) => {
      const keep = cur.filter((u) => ok.includes(u));
      return keep.length ? keep : ok.slice(0, 1);
    });
  }, [spec, basis]);

  // THE params effect: defaults ⊕ (clone | template). One writer, so no ordering hazard.
  // The clone guard stops a late-arriving templates query from clobbering a clone or the
  // user's edits — the same failure the classic form had to fix.
  const cloneAppliedRef = useRef(false);
  useEffect(() => {
    const base = defaultParams(spec, basis);
    const cloneSrc = clonePrefill?.strategy_id === strategyId && !cloneAppliedRef.current
      ? clonePrefill : null;
    const src = cloneSrc?.params ?? (cloneSrc ? null : templatesData?.templates?.[strategyId]?.params);
    if (src) {
      for (const fld of allFields(spec)) {
        const raw = (src as Record<string, unknown>)[fld.param];
        if (raw !== undefined && raw !== null) base[fld.param] = toDisplayValue(fld, raw);
      }
      // A saved run with trailing disabled comes back as trigger/step 0 — reflect that in
      // the selector rather than showing "Ratchet" over zeroed fields.
      if (spec.exit.trail && Number(base[spec.exit.trail.trigger]) === 0) base[TRAIL_UI] = "off";
      else if (spec.exit.trail && base[spec.exit.trail.mode]) {
        base[TRAIL_UI] = String(base[spec.exit.trail.mode]);
      }
      const sz: Record<string, number | boolean> = {};
      const s = src as Record<string, unknown>;
      if (typeof s.margin_per_lot === "number" && s.margin_per_lot > 0) sz.margin = s.margin_per_lot;
      if (typeof s.margin_per_lotset === "number") sz.margin = s.margin_per_lotset;
      if (typeof s.lots === "number") sz.lots = s.lots;
      if (typeof s.sizing_buffer_pct === "number") sz.buffer = s.sizing_buffer_pct;
      if (s.sizing === "capital" || s.sizing === "margin") sz.mode = true as never;
      const cap = cloneSrc?.capital ?? templatesData?.templates?.[strategyId]?.capital;
      if (typeof cap === "number" && cap > 0) sz.capital = cap;
      dispatchSizing({ type: "reset", v: {
        ...sz,
        mode: s.sizing === "capital" || s.sizing === "margin" ? "capital" : "fixed",
      } as never });
      setAppliedTemplate(cloneSrc ? null : strategyId);
    } else {
      setAppliedTemplate(null);
    }
    if (cloneSrc) {
      cloneAppliedRef.current = true;
      if (cloneSrc.start_date && cloneSrc.end_date) {
        setPeriod({ preset: "CUSTOM", customStart: cloneSrc.start_date, customEnd: cloneSrc.end_date });
      }
      setName((n) => n || `${strategyId} (copy)`);
    }
    setParams(base);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [strategyId, basis, templatesData, clonePrefill]);

  const coverage = basis === "intraday"
    ? { first: barStore?.first_day ?? null, last: barStore?.last_day ?? null }
    : { first: eodCov?.start_date ?? null, last: eodCov?.end_date ?? null };
  const win = useMemo(() => resolveWindow(period, coverage),
    [period, coverage.first, coverage.last]);   // eslint-disable-line react-hooks/exhaustive-deps

  const setParam = (param: string, v: number | string | boolean) =>
    setParams((p) => ({ ...p, [param]: v }));

  const saveMutation = useMutation({
    mutationFn: (b: Parameters<typeof api.backtestSave>[0]) => api.backtestSave(b),
    onSuccess: (d) => { if (d.run_id != null) navigate(`/runs/${d.run_id}`); },
  });

  /** Poll the (single-flight) replay job to completion. */
  async function pollJob(jobId: string, onTick: (done: number, total: number, day: string | null) => void) {
    for (;;) {
      await new Promise((r) => setTimeout(r, 1500));
      const snap = await api.backtestIntradayProgress();
      if (snap.id !== jobId) throw new Error("the replay job was replaced by another run");
      onTick(snap.done, snap.total, snap.day ?? null);
      if (snap.status === "done" && snap.result) return snap.result;
      if (snap.status === "error") throw new Error(snap.error ?? "replay failed");
    }
  }

  async function run() {
    if (!win) { setError("Pick a period first."); return; }
    setError(null);
    setResult(null);
    setRunning(true);
    const state = { name, notes, basis, params, sizing, window: { start: win.start, end: win.end } };
    const multi = underlyings.length > 1;
    const batchId = multi ? crypto.randomUUID().slice(0, 32) : undefined;
    const runIds: number[] = [];
    try {
      for (const [idx, u] of underlyings.entries()) {
        setBatch({ idx, total: underlyings.length, underlying: u, day: null, done: 0, jobTotal: 0 });
        const body: BacktestRequest = {
          ...buildV2Body(spec, state, u),
          // A multi-underlying batch persists each leg (Compare reads saved runs); a single
          // run stays a preview until the user hits Save, as it always has.
          persist: multi,
          ...(multi ? { batch_id: batchId, name: `${name.trim() || strategyId} — ${u}` } : {}),
        };
        setLastBody(body);
        if (basis === "intraday") {
          const { job_id } = await api.backtestIntraday(body);
          const res = await pollJob(job_id, (done, jobTotal, day) =>
            setBatch((b) => (b ? { ...b, done, jobTotal, day } : b)));
          if (multi && res.run_id != null) runIds.push(res.run_id);
          if (!multi) setResult(res);
        } else {
          const res = await api.backtest(body);
          if (multi && res.run_id != null) runIds.push(res.run_id);
          if (!multi) setResult(res);
        }
      }
      if (multi && runIds.length) navigate(`/compare?ids=${runIds.join(",")}`);
    } catch (e) {
      const done = runIds.length ? ` (${runIds.length} run(s) completed: #${runIds.join(", #")})` : "";
      setError(`${(e as Error).message}${done}`);
    } finally {
      setRunning(false);
      setBatch(null);
    }
  }

  async function runSweep() {
    const vals = sweepValues.split(",").map((s) => Number(s.trim()))
      .filter((n) => Number.isFinite(n)).slice(0, 5);
    if (vals.length < 2) { setError("Enter 2–5 numeric values for the swept parameter."); return; }
    if (!win) { setError("Pick a period first."); return; }
    setError(null);
    setRunning(true);
    const batchId = crypto.randomUUID().slice(0, 32);
    const runIds: number[] = [];
    const fld = allFields(spec).find((x) => x.param === sweepField);
    try {
      for (const [i, v] of vals.entries()) {
        setBatch({ idx: i, total: vals.length, underlying: `${sweepField} = ${v}`,
                   day: null, done: 0, jobTotal: 0 });
        const state = { name, notes, basis, params: { ...params, [sweepField]: v }, sizing,
                        window: { start: win.start, end: win.end } };
        const body: BacktestRequest = {
          ...buildV2Body(spec, state, underlyings[0]),
          persist: true, batch_id: batchId,
          name: `${name.trim() || strategyId} (${fld?.label ?? sweepField} ${v})`,
        };
        const res = await api.backtest(body);
        if (res.run_id != null) runIds.push(res.run_id);
      }
      if (runIds.length) navigate(`/compare?ids=${runIds.join(",")}`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRunning(false);
      setBatch(null);
    }
  }

  // Sweep candidates: this strategy's own numeric params (real names, real units).
  const sweepOptions = useMemo(
    () => allFields(spec).filter((f) => f.kind === "number")
      .map((f) => ({ value: f.param, label: f.label })), [spec]);
  useEffect(() => {
    if (!sweepOptions.some((o) => o.value === sweepField)) {
      setSweepField(sweepOptions[0]?.value ?? "");
    }
  }, [sweepOptions, sweepField]);
  const sweepDisabled = basis !== "eod" ? "EOD basis only (an intraday replay runs one job at a time)"
    : underlyings.length > 1 ? "single underlying only" : null;

  return (
    <div>
      <div className="grid items-start gap-[22px] lg:grid-cols-[minmax(0,1fr)_372px]">
      <div>
        <BasicsSection
          name={name} notes={notes} onName={setName} onNotes={setNotes}
          strategyId={strategyId} strategies={strategies} onStrategy={onStrategyChange}
          basis={basis} onBasis={setBasis} spec={spec}
          storeDays={barStore?.days_total} appliedTemplate={appliedTemplate} />
        <UniversePeriodSection spec={spec} basis={basis} underlyings={underlyings}
          onUnderlyings={setUnderlyings} period={period} onPeriod={setPeriod} window={win} />
        <SizingSection spec={spec} sizing={sizing} dispatch={dispatchSizing} />
        <EntrySection spec={spec} basis={basis} params={params} setParam={setParam} />
        <ExitSection spec={spec} basis={basis} params={params} setParam={setParam} />
        <StrategyParamsSection spec={spec} basis={basis} params={params} setParam={setParam} />
      </div>

      <RunRail
        spec={spec} underlyings={underlyings} sizing={sizing} running={running} batch={batch}
        windowLabel={win ? fmtWindow(win) : null}
        onRun={sweepOn && !sweepDisabled ? runSweep : run}
        onSaveTemplate={() => result?.run_id != null && api.setTemplate(result.run_id)
          .then(() => setAppliedTemplate(strategyId))}
        saveTemplateDisabled={result?.run_id == null}
        saveTemplateHint={result?.run_id == null ? "run and save a backtest first" : undefined}
        error={error}
        sweep={
          <SweepBlock enabled={sweepOn} onToggle={setSweepOn} field={sweepField}
            onField={setSweepField} values={sweepValues} onValues={setSweepValues}
            options={sweepOptions} disabledReason={sweepDisabled} />
        } />
      </div>

      {/* Results live OUTSIDE the form grid: the rail is sticky within that grid, and
          keeping the report as a grid row let the pinned rail float over the tables
          (2026-07-18). As a sibling, the rail's travel ends with the form — by the time
          the report is on screen the whole grid has scrolled away. */}
      {result && (
        <div className="mt-[22px] space-y-3">
          <div className="flex items-center gap-3 flex-wrap">
            <h2 className="font-['Space_Grotesk'] font-bold text-[16px] text-[var(--strong)]">Result</h2>
            {result.run_id != null ? (
              <a href={`/runs/${result.run_id}`} className="text-[var(--accent-deep)] text-sm underline">
                open run #{result.run_id}
              </a>
            ) : (
              <>
                <span className="text-[var(--faint)] text-sm">preview · not saved</span>
                <button
                  onClick={() => lastBody && saveMutation.mutate({
                    request: { ...lastBody, name: name.trim() || undefined,
                               notes: notes.trim() || undefined },
                    report: result.report, trades: result.trades,
                  })}
                  disabled={saveMutation.isPending}
                  className="rounded-md bg-[var(--ft)] text-white px-3 py-1.5 text-sm font-medium disabled:opacity-50">
                  {saveMutation.isPending ? "Saving…" : "Save backtest"}
                </button>
              </>
            )}
          </div>
          {saveMutation.error && <ErrorBox message={(saveMutation.error as Error).message} />}
          <ReportView report={result.report} trades={result.trades}
            runId={result.run_id ?? undefined} />
        </div>
      )}
    </div>
  );
}
