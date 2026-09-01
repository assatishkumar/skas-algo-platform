import { useEffect, useMemo, useState } from "react";
import { portfolio as papi } from "../../api/client";
import {
  dayLabel, exactMoney,
  toInput,
  type AssetClassKey, type Holding, type HoldingInput, type Kind, type PortfolioPayload,
} from "../../lib/portfolio";
import { Field, inputClass, Modal, Notice, Segments, selectClass } from "./primitives";

interface BrokerOption { id: number; label: string; broker: string }

const blank = (): HoldingInput => ({
  name: "", asset_class: "stk", kind_override: null, invested: 0, units: null,
  value: 0, last_price: null, native_currency: null, native_price: null,
  native_invested: null, day_change: 0, xirr_pct: null,
  buy_month: new Date().toISOString().slice(0, 7),
  sync: "manual", sync_source: null, sync_ref: null, broker_account_id: null,
  units_locked: false, broker_units: {}, excluded_from_buckets: false, note: null,
});

/** AMFI scheme picker. Searches the cached NAV file, so it answers instantly and works with
 * no broker session — funds are the one asset class with a free public price feed. */
function FundPicker({
  value, onPick,
}: { value: string | null; onPick: (isin: string, name: string, nav: number) => void }) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<
    { isin: string; name: string; nav: number; as_of: string }[]
  >([]);
  const [state, setState] = useState<"idle" | "searching" | "empty" | "cold">("idle");

  useEffect(() => {
    if (q.trim().length < 3) { setResults([]); setState("idle"); return; }
    let live = true;
    setState("searching");
    const t = window.setTimeout(async () => {
      try {
        const r = await papi.searchFunds(q);
        if (!live) return;
        setResults(r.results);
        setState(r.results.length ? "idle" : "empty");
      } catch {
        if (live) setState("cold");
      }
    }, 220);
    return () => { live = false; window.clearTimeout(t); };
  }, [q]);

  return (
    <div>
      <input
        className={inputClass}
        value={q}
        placeholder={value ? `Linked to ${value} — search to change` : "Search AMFI by scheme name…"}
        onChange={(e) => setQ(e.target.value)}
      />
      {state === "empty" && (
        <div className="mt-1.5 text-[11.5px] font-semibold text-[var(--faint)]">
          No scheme matches. Every word must appear — try fewer.
        </div>
      )}
      {state === "cold" && (
        <div className="mt-1.5 text-[11.5px] font-semibold text-[var(--warn-text)]">
          No NAV file cached yet — hit "Refresh NAVs" on the page header first.
        </div>
      )}
      {results.length > 0 && (
        <div className="mt-1.5 max-h-[190px] overflow-y-auto rounded-[10px] border border-[var(--border)] bg-[var(--card)]">
          {results.map((r) => (
            <button
              key={r.isin}
              onClick={() => { onPick(r.isin, r.name, r.nav); setResults([]); setQ(""); }}
              className="block w-full px-3 py-2 text-left hover:bg-[var(--row-hover)]"
            >
              <span className="block text-[12.5px] font-bold text-[var(--strong)]">{r.name}</span>
              <span className="block text-[11px] font-semibold text-[var(--faint)]">
                {r.isin} · NAV {r.nav} as of {dayLabel(r.as_of)}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export default function HoldingModal({
  holding, payload, brokers, onClose, onSave,
}: {
  holding: Holding | "new";
  payload: PortfolioPayload;
  brokers: BrokerOption[];
  onClose: () => void;
  onSave: (input: HoldingInput) => void;
}) {
  const isNew = holding === "new";
  const [draft, setDraft] = useState<HoldingInput>(
    () => (isNew ? blank() : toInput(holding)),
  );
  const [error, setError] = useState("");
  const set = (patch: Partial<HoldingInput>) => setDraft((d) => ({ ...d, ...patch }));

  const hasLedger = !isNew && holding.basis === "ledger";
  const defaultKind = payload.asset_classes[draft.asset_class]?.kind;

  // A fund can price off AMFI; a listed instrument off a broker. Offering a source the class
  // has no feed for would create an "auto" holding that silently never updates.
  const sources = useMemo(() => {
    const out: { value: "manual" | "amfi" | "broker" | "global"; label: string }[] = [
      { value: "manual", label: "Manual" },
    ];
    if (draft.asset_class === "mf") out.push({ value: "amfi", label: "AMFI NAV" });
    if (["stk", "etf", "gold", "re"].includes(draft.asset_class)) {
      out.push({ value: "broker", label: "Broker" });
    }
    // US equities and crypto have no Indian broker feed — they price off the global source.
    if (["us", "btc"].includes(draft.asset_class)) {
      out.push({ value: "global", label: "Live (US / crypto)" });
    }
    return out;
  }, [draft.asset_class]);

  const source: "manual" | "amfi" | "broker" | "global" =
    draft.sync === "auto" && draft.sync_source ? draft.sync_source : "manual";

  return (
    <Modal title={isNew ? "Add holding" : "Edit holding"} onClose={onClose} width={540}>
      <div className="grid grid-cols-2 gap-3">
        <Field label="NAME" span={2}>
          <input
            className={inputClass} value={draft.name}
            placeholder="e.g. HDFC Bank / Parag Parikh Flexi Cap"
            onChange={(e) => set({ name: e.target.value })}
          />
        </Field>

        <Field label="TYPE">
          <select
            className={selectClass} value={draft.asset_class}
            onChange={(e) => {
              const next = e.target.value as AssetClassKey;
              // Switching class can invalidate the price source — drop it rather than leave
              // an ETF pointed at an ISIN.
              const stillValid =
                (next === "mf" && source === "amfi")
                || (["stk", "etf", "gold", "re"].includes(next) && source === "broker")
                || (["us", "btc"].includes(next) && source === "global");
              set({
                asset_class: next,
                ...(stillValid ? {} : { sync: "manual", sync_source: null, sync_ref: null }),
              });
            }}
          >
            {(Object.keys(payload.asset_classes) as AssetClassKey[]).map((k) => (
              <option key={k} value={k}>{payload.asset_classes[k].label}</option>
            ))}
          </select>
        </Field>

        <Field label="RISK BUCKET">
          <select
            className={selectClass}
            value={draft.kind_override ?? ""}
            title="Overrides the class default — an arbitrage or balanced fund sits in 'mutual funds' but behaves like debt, and taxes like equity."
            onChange={(e) => set({ kind_override: (e.target.value || null) as Kind | null })}
          >
            <option value="">Default for type ({defaultKind})</option>
            <option value="equity">Equity</option>
            <option value="debt">Debt</option>
            <option value="alt">Alternatives</option>
          </select>
        </Field>

        <Field label="PRICE SOURCE" span={2}>
          <Segments
            size="sm"
            value={source}
            onChange={(v) =>
              set(
                v === "manual"
                  ? { sync: "manual", sync_source: null, sync_ref: null, broker_account_id: null }
                  : { sync: "auto", sync_source: v, sync_ref: null },
              )}
            options={sources.map((s) => ({ value: s.value, label: s.label }))}
          />
          {sources.length === 1 && (
            <div className="mt-1.5 text-[11.5px] font-semibold text-[var(--faint)]">
              No public price feed exists for {payload.asset_classes[draft.asset_class].label} —
              this one is kept up to date by hand.
            </div>
          )}
        </Field>

        {source === "amfi" && (
          <Field label="SCHEME" span={2}>
            <FundPicker
              value={draft.sync_ref}
              onPick={(isin, name, nav) =>
                set({
                  sync_ref: isin, last_price: nav,
                  name: draft.name.trim() ? draft.name : name,
                })}
            />
          </Field>
        )}

        {source === "global" && (
          <Field label="SYMBOL" span={2}>
            <input
              className={inputClass}
              value={draft.sync_ref ?? ""}
              placeholder="e.g. GOOG, NVDA, BTC-INR"
              onChange={(e) => set({ sync_ref: e.target.value.toUpperCase().trim() })}
            />
            <div className="mt-1.5 text-[11.5px] font-semibold text-[var(--faint)]">
              A US ticker prices in USD and is converted at the live USD/INR rate; a
              crypto pair quoted in INR (BTC-INR) needs no conversion.
            </div>
          </Field>
        )}

        {source === "broker" && (
          <>
            <Field label="ACCOUNT">
              <select
                className={selectClass}
                value={draft.broker_account_id ?? ""}
                onChange={(e) =>
                  set({ broker_account_id: e.target.value ? Number(e.target.value) : null })}
              >
                <option value="">Pick an account…</option>
                {brokers.map((b) => (
                  <option key={b.id} value={b.id}>{b.label} · {b.broker}</option>
                ))}
              </select>
            </Field>
            <Field label="TRADING SYMBOL">
              <input
                className={inputClass}
                value={draft.sync_ref ?? ""}
                placeholder="e.g. ITC"
                onChange={(e) => set({ sync_ref: e.target.value.toUpperCase() })}
              />
            </Field>
          </>
        )}

        {hasLedger ? (
          <div className="col-span-2">
            <Notice>
              Units and cost basis come from this holding's {holding.txn_count} transactions —
              edit them in the ledger, not here. Value follows units × the latest price.
            </Notice>
          </div>
        ) : (
          <>
            <Field label="SINCE">
              <input
                type="month" className={inputClass} value={draft.buy_month}
                onChange={(e) => set({ buy_month: e.target.value })}
              />
            </Field>
            <Field label="UNITS (optional)">
              <input
                type="number" className={inputClass} value={draft.units ?? ""}
                placeholder="leave blank if not unit-based"
                onChange={(e) => set({ units: e.target.value ? Number(e.target.value) : null })}
              />
            </Field>
            <Field label="INVESTED (₹)">
              <input
                type="number" className={inputClass} value={draft.invested || ""}
                placeholder="500000"
                onChange={(e) => set({ invested: Number(e.target.value) })}
              />
            </Field>
            <Field label="CURRENT VALUE (₹)">
              <input
                type="number" className={inputClass} value={draft.value || ""}
                placeholder="620000"
                onChange={(e) => set({ value: Number(e.target.value) })}
              />
            </Field>
          </>
        )}

        <Field label="ANNUALISED RETURN (optional)" span={2}>
          <input
            type="number" className={inputClass} value={draft.xirr_pct ?? ""}
            placeholder="leave blank to derive it"
            onChange={(e) => set({ xirr_pct: e.target.value ? Number(e.target.value) : null })}
          />
          <div className="mt-1.5 text-[11.5px] font-semibold text-[var(--faint)]">
            Your broker statement's own XIRR, if you have it — it knows about corporate actions
            and pre-migration history that this ledger doesn't. Blank derives it.
          </div>
        </Field>
      </div>

      {error && <div className="mt-2.5 text-[12px] font-bold text-[var(--danger)]">{error}</div>}

      <div className="mt-4 flex items-center justify-end gap-2.5">
        {!hasLedger && draft.invested > 0 && draft.value > 0 && (
          <span className="mr-auto text-[11.5px] font-semibold text-[var(--faint)]">
            {exactMoney(draft.value - draft.invested)} unrealized
          </span>
        )}
        <button
          onClick={onClose}
          className="rounded-[11px] border-[1.5px] border-[var(--field-border)] bg-[var(--ghost)] px-[18px] py-2.5 text-[13px] font-extrabold text-[var(--muted)]"
        >
          Cancel
        </button>
        <button
          onClick={() => {
            if (!draft.name.trim()) return setError("Give the holding a name.");
            if (source === "amfi" && !draft.sync_ref) {
              return setError("Pick a scheme, or set the price source back to Manual.");
            }
            if (source === "broker" && (!draft.sync_ref || !draft.broker_account_id)) {
              return setError("A broker-priced holding needs an account and a trading symbol.");
            }
            if (source === "global" && !draft.sync_ref) {
              return setError("Enter the symbol to price this from.");
            }
            if (!hasLedger && !(draft.value > 0) && !(draft.units && draft.units > 0)) {
              return setError("Enter a current value, or units for an auto-priced holding.");
            }
            onSave({ ...draft, name: draft.name.trim() });
          }}
          className="rounded-[11px] bg-[var(--accent)] px-[22px] py-2.5 text-[13px] font-extrabold text-white"
        >
          {isNew ? "Add holding" : "Save changes"}
        </button>
      </div>
    </Modal>
  );
}
