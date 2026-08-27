import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { api, brokers } from "../../api/client";
import { ErrorBox, NumberInput } from "../../components/ui";
import { Panel } from "../../components/redesign";
import {
  DEPLOY_REGISTRY, GROUP_LABEL, GROUP_ORDER, defaultsFor,
  type DeployField, type DeploySpec,
} from "../../lib/deploy/registry";
import { STRATEGIES } from "../../lib/strategyDocs";
import EquityTradeBuilder from "../../components/trade/EquityTradeBuilder";

/** Deploy a MANAGED strategy — ONE page for every deployable strategy, rendered from
 *  lib/deploy/registry (see that file for why the old two-page split had to go).
 *  Left rail groups by how long the strategy HOLDS; right pane shows what it does (read from
 *  the same /docs table the reference page uses) and only the knobs it actually has. */

const inputClass =
  "w-full rounded-[10px] bg-[var(--field)] border border-[var(--field-border)] px-2.5 py-1.5 text-sm text-[var(--strong)] focus:outline-none focus:border-[var(--accent)]";
const lbl = "block text-xs text-[var(--muted)] mb-1";

const UNIVERSES = [
  { value: "nifty50", label: "Nifty 50" },
  { value: "nifty500mom50", label: "Nifty500 Momentum 50" },
  { value: "", label: "Custom symbols" },
];

function Field({ spec, value, onChange }: {
  spec: DeployField;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const body = () => {
    switch (spec.kind) {
      case "toggle":
        return (
          <label className="inline-flex items-center gap-1.5 pt-1 text-sm text-[var(--muted)]">
            <input type="checkbox" checked={Boolean(value)}
              onChange={(e) => onChange(e.target.checked)} />
            {spec.label}
          </label>
        );
      case "select":
        return (
          <select className={inputClass} value={String(value)}
            onChange={(e) => onChange(e.target.value)}>
            {(spec.options ?? []).map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        );
      case "number":
        return (
          <NumberInput className={inputClass} step={spec.step} value={Number(value)}
            onChange={(v) => onChange(v)} />
        );
      default:
        return (
          <input className={inputClass} value={String(value ?? "")}
            onChange={(e) => onChange(e.target.value)} />
        );
    }
  };

  if (spec.kind === "toggle") {
    return (
      <div className={spec.wide ? "md:col-span-4" : ""}>
        {body()}
        {spec.hint && <span className="mt-0.5 block text-[11px] text-[var(--faint)]">{spec.hint}</span>}
      </div>
    );
  }
  return (
    <label className={`block ${spec.wide ? "md:col-span-4" : ""}`}>
      <span className={lbl}>{spec.label}</span>
      {body()}
      {spec.hint && <span className="mt-1 block text-[11px] text-[var(--faint)]">{spec.hint}</span>}
    </label>
  );
}

/** What the strategy DOES — read from the same curated table /docs renders, so the two can
 *  never drift. "Adjustment" lines are the exit bullets that describe a roll/adjust rather
 *  than a close, which is how the decks phrase them. */
function Summary({ id }: { id: string }) {
  const doc = STRATEGIES.find((s) => s.id === id);
  if (!doc) return null;
  const isAdjust = (t: string) => /\broll|adjust|re-?enter|hedge|convert/i.test(t);
  const exits = doc.exit.filter((t) => !isAdjust(t));
  const adjusts = doc.exit.filter(isAdjust);

  const Block = ({ title, items }: { title: string; items: string[] }) =>
    items.length === 0 ? null : (
      <div>
        <div className="text-[10.5px] font-extrabold uppercase tracking-wide text-[var(--faint)]">
          {title}
        </div>
        <ul className="mt-1 space-y-1">
          {items.map((t, i) => (
            <li key={i} className="flex gap-1.5 text-[12.5px] leading-snug text-[var(--muted)]">
              <span className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-[var(--faint)]" />
              <span>{t}</span>
            </li>
          ))}
        </ul>
      </div>
    );

  return (
    <div className="mb-4 rounded-[14px] border border-[var(--border)] bg-[var(--field)] p-4">
      <p className="text-[13px] leading-relaxed text-[var(--strong)]">{doc.summary}</p>
      <div className="mt-3 grid gap-3 md:grid-cols-2">
        <Block title="Structure" items={doc.structure} />
        <Block title="Entry" items={doc.entry} />
        <Block title="Exit" items={exits} />
        <Block title="Adjustment" items={adjusts} />
      </div>
      <div className="mt-3 flex items-center gap-3 border-t border-[var(--divider)] pt-2.5">
        <Link to="/docs" className="text-[12px] font-bold text-[var(--accent)] hover:underline">
          Full documentation →
        </Link>
        <span className="text-[11.5px] text-[var(--faint)]">{doc.risk.slice(0, 150)}…</span>
      </div>
    </div>
  );
}

interface Prefill { strategy_id?: string; name?: string; capital?: number; params?: Record<string, unknown> }

export default function DeployView() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  // "Forward test" on a backtest run lands here with its config in router STATE (both
  // /trade and the legacy /live/new render this component, so the state survives — a
  // <Navigate> redirect would have dropped it silently, App.tsx's documented footgun).
  const pre = (useLocation().state as { prefill?: Prefill } | null)?.prefill;
  const urlId = pre?.strategy_id ?? params.get("strategy");
  const [sel, setSel] = useState(
    () => (urlId && DEPLOY_REGISTRY.some((s) => s.id === urlId) ? urlId : DEPLOY_REGISTRY[0].id),
  );
  const spec: DeploySpec = useMemo(
    () => DEPLOY_REGISTRY.find((s) => s.id === sel) ?? DEPLOY_REGISTRY[0], [sel],
  );

  // strategy params — re-seeded from the registry whenever the strategy changes, so a knob
  // from the previous strategy can never leak into this deploy.
  const [vals, setVals] = useState<Record<string, unknown>>(() => defaultsFor(spec));
  const [name, setName] = useState("");
  const [notes, setNotes] = useState("");
  const [underlying, setUnderlying] = useState(spec.underlyings?.[0] ?? "NIFTY");
  const [universe, setUniverse] = useState(spec.universe ?? "nifty50");
  const [symbols, setSymbols] = useState("");
  const [capital, setCapital] = useState(500_000);
  const [mode, setMode] = useState("PAPER");
  const [quoteSource, setQuoteSource] = useState("zerodha");
  const [accountId, setAccountId] = useState<number | null>(null);
  const [auto, setAuto] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // Registry defaults first, then any prefilled values for THIS strategy — a prefill from
    // another strategy's run must never leak a knob in (the ctor would swallow it).
    const base = defaultsFor(spec);
    if (pre?.strategy_id === spec.id && pre.params) {
      for (const [k, v] of Object.entries(pre.params)) if (k in base) base[k] = v;
      if (pre.capital) setCapital(pre.capital);
    }
    setVals(base);
    setName(pre?.strategy_id === spec.id ? (pre.name ?? "") : "");
    setUnderlying(spec.underlyings?.[0] ?? "NIFTY");
    setUniverse(spec.universe ?? "nifty50");
    setQuoteSource(spec.needsBroker ? "zerodha" : quoteSource === "cache" ? "cache" : "zerodha");
    setError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spec.id]);

  const { data: accounts } = useQuery({ queryKey: ["brokers"], queryFn: brokers.list });
  const sessioned = (accounts ?? []).filter((a) => a.has_session);
  const brokerOk = quoteSource === "cache" || accountId != null;
  const needsAccount = spec.needsBroker && quoteSource === "cache";

  const set = (p: string, v: unknown) => setVals((prev) => ({ ...prev, [p]: v }));
  const visible = spec.fields.filter((fd) => !fd.showIf || fd.showIf(vals));

  async function deploy() {
    setBusy(true);
    setError(null);
    try {
      // `fixed` may carry RUN-level keys (lookback/tax_rate) that are not strategy kwargs —
      // split them out so they land as request fields, not inside `params` where the ctor
      // would swallow them.
      const all = { ...vals, ...(spec.fixed ?? {}) } as Record<string, unknown>;
      const runLevel = ["lookback", "tax_rate", "withdrawal_rate"] as const;
      const strategyParams = { ...all };
      for (const k of runLevel) delete strategyParams[k];
      const num = (k: string, d: number) => (typeof all[k] === "number" ? (all[k] as number) : d);

      if (spec.route) {
        // The strategy's OWN route — it validates prerequisites and maps deploy-layer knobs.
        await api.deployByRoute(spec.route, {
          name: name.trim() || spec.name,
          notes: notes.trim() || undefined,
          underlying,
          ...strategyParams,
          capital, mode, quote_source: quoteSource,
          broker_account_id: quoteSource === "cache" ? null : accountId,
          auto,
        });
      } else {
        const isCustom = spec.instrument === "STOCK" && universe === "";
        await api.liveStart({
          strategy_id: spec.id,
          name: name.trim() || undefined,
          notes: notes.trim() || undefined,
          instrument_class: spec.instrument,
          underlying: spec.instrument === "DERIV" ? underlying : undefined,
          universe: spec.instrument === "DERIV" ? null : isCustom ? null : universe,
          symbols: isCustom ? symbols.split(",").map((s) => s.trim()).filter(Boolean) : [],
          capital,
          params: strategyParams,
          quote_source: quoteSource,
          broker_account_id: quoteSource === "cache" ? null : accountId,
          mode,
          tax_rate: num("tax_rate", 0.2),
          withdrawal_rate: num("withdrawal_rate", 0),
          lookback: num("lookback", 20),
          ignore_market_hours: false,
          auto,
        });
      }
      navigate("/live");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid grid-cols-1 gap-5 lg:grid-cols-[300px_1fr]">
      {/* left rail — grouped by how long the strategy HOLDS */}
      <div className="space-y-4">
        {GROUP_ORDER.map((g) => {
          const items = DEPLOY_REGISTRY.filter((s) => s.group === g);
          if (!items.length) return null;
          return (
            <div key={g}>
              <div className="mb-1.5">
                <div className="text-[11.5px] font-extrabold uppercase tracking-wide text-[var(--faint)]">
                  {GROUP_LABEL[g].title} · {items.length}
                </div>
                <div className="text-[11px] leading-snug text-[var(--faint)] opacity-70">
                  {GROUP_LABEL[g].note}
                </div>
              </div>
              <div className="space-y-1.5">
                {items.map((s) => {
                  const on = s.id === sel;
                  return (
                    <button
                      key={s.id}
                      onClick={() => { setSel(s.id); setParams({ strategy: s.id }); }}
                      className={`w-full rounded-[12px] border-[1.5px] bg-[var(--card)] px-3 py-2.5 text-left transition ${
                        on ? "border-[var(--accent)]" : "border-[var(--border)] hover:border-[var(--field-border)]"
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className={`font-['Space_Grotesk'] text-[13.5px] font-bold ${
                          on ? "text-[var(--accent-deep)]" : "text-[var(--strong)]"}`}>
                          {s.name}
                        </span>
                        <span className="rounded-full bg-[var(--chip)] px-1.5 py-0.5 text-[9px] font-extrabold tracking-wide text-[var(--chip-text)]">
                          {s.cadence}
                        </span>
                      </div>
                      <div className="mt-0.5 text-[11.5px] leading-snug text-[var(--faint)]">{s.blurb}</div>
                    </button>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>

      {/* right — what it does, then only the knobs it has */}
      <div className="min-w-0">
        {spec.custom === "equity" ? <EquityTradeBuilder /> : (
        <Panel className="p-5">
          <Summary id={spec.id} />

          {spec.warn && (
            <div className="mb-3 rounded-[10px] border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[12px] leading-snug text-amber-600 dark:text-amber-400">
              {spec.warn}
            </div>
          )}

          <div className="grid gap-3 md:grid-cols-2 mb-3">
            <label className="block"><span className={lbl}>Deployment name</span>
              <input className={inputClass} placeholder={spec.name} value={name}
                onChange={(e) => setName(e.target.value)} /></label>
            <label className="block"><span className={lbl}>Notes</span>
              <input className={inputClass} placeholder="why / what you're testing" value={notes}
                onChange={(e) => setNotes(e.target.value)} /></label>
          </div>

          {/* instrument selection */}
          <div className="grid gap-3 md:grid-cols-4 mb-3">
            {spec.instrument === "DERIV" ? (
              <label className="block"><span className={lbl}>Underlying</span>
                <select className={inputClass} value={underlying}
                  onChange={(e) => setUnderlying(e.target.value)}>
                  {(spec.underlyings ?? ["NIFTY"]).map((u) => <option key={u} value={u}>{u}</option>)}
                </select></label>
            ) : (
              <>
                <label className="block"><span className={lbl}>Universe</span>
                  <select className={inputClass} value={universe}
                    onChange={(e) => setUniverse(e.target.value)}>
                    {UNIVERSES.map((u) => <option key={u.value} value={u.value}>{u.label}</option>)}
                  </select></label>
                {universe === "" && (
                  <label className="block md:col-span-3"><span className={lbl}>Symbols (comma-separated)</span>
                    <input className={inputClass} placeholder="ITC, HDFCBANK, TITAN" value={symbols}
                      onChange={(e) => setSymbols(e.target.value)} /></label>
                )}
              </>
            )}
          </div>

          {/* THE strategy's own params — nothing else */}
          <div className="grid gap-3 md:grid-cols-4 mb-4">
            {visible.map((fd) => (
              <Field key={fd.param} spec={fd} value={vals[fd.param]}
                onChange={(v) => set(fd.param, v)} />
            ))}
          </div>

          {/* shared run infra — identical for every strategy */}
          <div className="border-t border-[var(--divider)] pt-3.5">
            <div className="grid gap-3 md:grid-cols-4">
              <label className="block"><span className={lbl}>Capital (₹)</span>
                <NumberInput className={inputClass} value={capital} onChange={setCapital} /></label>
              <label className="block"><span className={lbl}>Mode</span>
                <select className={inputClass} value={mode} onChange={(e) => setMode(e.target.value)}>
                  <option value="PAPER">Paper (simulated)</option>
                  <option value="LIVE">Live (real money)</option>
                </select></label>
              <label className="block"><span className={lbl}>Quotes</span>
                <select className={inputClass} value={quoteSource}
                  onChange={(e) => setQuoteSource(e.target.value)}>
                  <option value="zerodha">Zerodha (live)</option>
                  <option value="dhan">Dhan (live)</option>
                  {!spec.needsBroker && <option value="cache">Cache (last close, offline)</option>}
                </select></label>
              <label className="block"><span className={lbl}>Broker account</span>
                <select className={inputClass} value={accountId ?? ""}
                  onChange={(e) => setAccountId(e.target.value ? Number(e.target.value) : null)}
                  disabled={quoteSource === "cache"}>
                  <option value="">{sessioned.length ? "Select account…" : "No logged-in session"}</option>
                  {sessioned.map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}
                </select></label>
            </div>
            <label className="mt-2.5 inline-flex items-center gap-1.5 text-sm text-[var(--muted)]">
              <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
              auto loop (refresh + decisions)
            </label>
          </div>

          {needsAccount && (
            <div className="mt-3 text-[12px] text-amber-600 dark:text-amber-400">
              This strategy reads the LIVE option chain — pick a broker quote source.
            </div>
          )}
          {error && <div className="mt-3"><ErrorBox message={error} /></div>}

          <div className="mt-4 flex items-center gap-3">
            <button
              onClick={deploy}
              disabled={busy || !brokerOk || needsAccount}
              title={!brokerOk ? "select a broker account, or switch Quotes to Cache" : undefined}
              className={`rounded-[10px] px-4 py-2 text-sm font-bold text-white disabled:opacity-50 ${
                mode === "LIVE" ? "bg-[var(--danger,#b91c1c)]" : "bg-[var(--accent)]"}`}
            >
              {busy ? "Deploying…" : `Deploy ${mode === "LIVE" ? "LIVE" : "paper"}`}
            </button>
            <span className="text-[12px] text-[var(--faint)]">
              {mode === "LIVE"
                ? "Real money — orders route to the broker if the account is armed."
                : "Simulated fills · no real orders."}
            </span>
          </div>
        </Panel>
        )}
      </div>
    </div>
  );
}
