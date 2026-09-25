import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { portfolio as papi } from "../../api/client";
import {
  money, pct, signedMoney,
  type BidsDefaults, type BidsRow, type BidsSuggestion,
} from "../../lib/portfolio";
import { Card, ConfirmAction, inputClass, Kpi, Modal, Notice, Pill } from "./primitives";

/** BIDS — Buy In Dips (owner design 2026-09-25). Every market-linked holding is bought again
 *  each time it falls another X% below its recent high: y, then 2y, 3y … up to N levels, and a
 *  new high resets the ladder. Holdings a running BIDS deployment trades buy AUTOMATICALLY;
 *  everything else lands here as a suggestion to accept (a ledger BUY, your own order) or skip
 *  (the level is consumed; the next X% still fires). services/bids.py is the engine.
 *
 *  A dollar-quoted holding (US stocks) runs its ladder in DOLLARS on its own defaults: the
 *  peak, trigger, amounts and suggestions are all $, and an accepted fill is booked in the
 *  rupee ledger at the holding's last-sync rate. Every figure here carries its own symbol. */

const GROUP_ORDER = ["US stocks", "Mutual funds", "ETFs", "Stocks"];

const MODE_PILL: Record<BidsRow["mode"], { label: string; bg: string; color: string; title: string }> = {
  auto: { label: "AUTO", bg: "var(--ok-bg)", color: "var(--ok-text)",
    title: "A running BIDS deployment on this broker account buys it at 15:05 when a level fires" },
  suggest: { label: "SUGGEST", bg: "var(--chip)", color: "var(--chip-text)",
    title: "No order path (a fund, a US stock, or no BIDS run on its account): a fired level becomes a suggestion above" },
  excluded: { label: "EXCLUDED", bg: "var(--seg)", color: "var(--faint)",
    title: "Left out of BIDS — no level fires" },
};

const PEAK_SOURCE: Record<string, string> = {
  joined: "the price the day it joined BIDS — levels are measured from there, never from an older high",
  high: "a new high since it joined — the ladder reset there",
  manual: "a reference high you typed — the ladder restarted from it",
};

function sym(ccy: string | null | undefined): string {
  const c = (ccy ?? "INR").toUpperCase();
  return c === "INR" ? "₹" : c === "USD" ? "$" : `${c} `;
}

function locale(ccy: string | null | undefined): string {
  return (ccy ?? "INR").toUpperCase() === "INR" ? "en-IN" : "en-US";
}

/** A per-unit price in its own currency: two decimals under 1,000. */
function px(v: number | null | undefined, ccy: string): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  return `${sym(ccy)}${v.toLocaleString(locale(ccy), {
    minimumFractionDigits: v >= 1000 ? 0 : 2, maximumFractionDigits: 2,
  })}`;
}

/** Whole money in its own currency — rupees keep the portfolio's lakh/crore shorthand. */
function amt(v: number, ccy: string): string {
  if (ccy.toUpperCase() === "INR") return money(v);
  const s = v < 0 ? "−" : "";
  return `${s}${sym(ccy)}${Math.round(Math.abs(v)).toLocaleString("en-US")}`;
}

function exact(v: number, ccy: string): string {
  return `${sym(ccy)}${Math.round(v).toLocaleString(locale(ccy))}`;
}

function units(v: number | null): string {
  if (v === null || !Number.isFinite(v)) return "—";
  return v.toLocaleString("en-IN", { maximumFractionDigits: Number.isInteger(v) ? 0 : 3 });
}

function num(v: string): number | null {
  const n = Number(v);
  return v.trim() === "" || !Number.isFinite(n) ? null : n;
}

type Knobs = { dip_pct: string; amount: string; max_levels: string };

function knobsOf(d: BidsDefaults, usd: boolean): Knobs {
  return usd
    ? { dip_pct: String(d.usd_dip_pct), amount: String(d.usd_amount), max_levels: String(d.usd_max_levels) }
    : { dip_pct: String(d.dip_pct), amount: String(d.amount), max_levels: String(d.max_levels) };
}

function KnobBlock({ title, ccy, k, set }: {
  title: string; ccy: string; k: Knobs; set: (k: Knobs) => void;
}) {
  const x = num(k.dip_pct) ?? 0;
  const y = num(k.amount) ?? 0;
  const n = num(k.max_levels) ?? 0;
  const fields: [keyof Knobs, string, string][] = [
    ["dip_pct", "DIP PER LEVEL (%)", "each level is this much further below the recent high"],
    ["amount", `FIRST LEVEL (${sym(ccy).trim()})`, "level k invests k × this"],
    ["max_levels", "MAX LEVELS", "no level beyond this until a new high resets the ladder"],
  ];
  return (
    <div className="rounded-[12px] border border-[var(--divider)] p-3">
      <div className="mb-2 flex flex-wrap items-baseline gap-2">
        <span className="text-[13px] font-extrabold text-[var(--strong)]">{title}</span>
        <span className="text-[11.5px] font-semibold text-[var(--faint)]">
          a full ladder of {n} level{n === 1 ? "" : "s"} at {x}% steps invests {exact(y * (n * (n + 1)) / 2, ccy)} down
          to −{(x * n).toFixed(0)}%
        </span>
      </div>
      <div className="grid grid-cols-3 gap-3">
        {fields.map(([key, label, hint]) => (
          <label key={key} title={hint}>
            <span className="text-[11px] font-extrabold tracking-[.04em] text-[var(--faint)]">{label}</span>
            <input className={`${inputClass} mt-[5px]`} value={k[key]}
              onChange={(e) => set({ ...k, [key]: e.target.value })} />
          </label>
        ))}
      </div>
    </div>
  );
}

function DefaultsCard({ d, onSaved }: { d: BidsDefaults; onSaved: () => void }) {
  const [inr, setInr] = useState<Knobs>(knobsOf(d, false));
  const [usd, setUsd] = useState<Knobs>(knobsOf(d, true));
  const [fund, setFund] = useState(d.fund_source);
  useEffect(() => { setInr(knobsOf(d, false)); setUsd(knobsOf(d, true)); setFund(d.fund_source); }, [d]);
  const save = useMutation({
    mutationFn: () => papi.bidsDefaults({
      dip_pct: num(inr.dip_pct) ?? undefined, amount: num(inr.amount) ?? undefined,
      max_levels: num(inr.max_levels) ?? undefined,
      usd_dip_pct: num(usd.dip_pct) ?? undefined, usd_amount: num(usd.amount) ?? undefined,
      usd_max_levels: num(usd.max_levels) ?? undefined,
      fund_source: fund.trim().toUpperCase(),
    }),
    onSuccess: onSaved,
  });
  return (
    <Card className="mb-4">
      <div className="mb-3 flex flex-wrap items-baseline gap-2">
        <div className="text-[15px] font-bold text-[var(--strong)]">Defaults</div>
        <div className="text-[12px] font-semibold text-[var(--faint)]">
          every holding uses its currency's defaults unless you set its own
        </div>
      </div>
      <div className="grid gap-3 lg:grid-cols-2">
        <KnobBlock title="Rupee holdings · stocks, ETFs, mutual funds" ccy="INR" k={inr} set={setInr} />
        <KnobBlock title="Dollar holdings · US stocks" ccy="USD" k={usd} set={setUsd} />
      </div>
      <div className="mt-3 flex flex-wrap items-end gap-3">
        <label className="w-[220px]" title="the ETF the auto runs sell to pay (T+1, like value investing)">
          <span className="text-[11px] font-extrabold tracking-[.04em] text-[var(--faint)]">FUNDED FROM</span>
          <input className={`${inputClass} mt-[5px]`} value={fund} onChange={(e) => setFund(e.target.value)} />
        </label>
        <button disabled={save.isPending} onClick={() => save.mutate()}
          className="rounded-[11px] bg-[var(--accent)] px-5 py-2 text-[13px] font-extrabold text-white disabled:opacity-50">
          Save defaults
        </button>
        {save.isError && <span className="text-[12px] font-bold text-[var(--danger)]">{String(save.error)}</span>}
      </div>
    </Card>
  );
}

function AcceptModal({ s, onClose, onDone }: { s: BidsSuggestion; onClose: () => void; onDone: () => void }) {
  const [qty, setQty] = useState(s.units_hint != null ? String(Number(s.units_hint.toFixed(3))) : "");
  const [price, setPrice] = useState(String(s.price));
  const [onDate, setOnDate] = useState(new Date().toISOString().slice(0, 10));
  const go = useMutation({
    mutationFn: () => papi.bidsAccept(s.id, { units: Number(qty), price: Number(price), on_date: onDate }),
    onSuccess: () => { onDone(); onClose(); },
  });
  const foreign = s.currency.toUpperCase() !== "INR";
  return (
    <Modal title={`Record the buy · ${s.holding} · level ${s.level}`} onClose={onClose} width={520}>
      <div className="mb-3 text-[12.5px] font-semibold text-[var(--muted)]">
        Place the order yourself (your fund platform or US broker), then record what you actually
        got. It lands as a BUY in this holding's ledger; the level stays consumed either way.
        {foreign && " The price is in dollars; the ledger books it in rupees at the rate of the holding's last price sync."}
      </div>
      <div className="grid grid-cols-3 gap-3">
        <label><span className="text-[11px] font-extrabold text-[var(--faint)]">UNITS</span>
          <input className={`${inputClass} mt-[5px]`} value={qty} onChange={(e) => setQty(e.target.value)} /></label>
        <label><span className="text-[11px] font-extrabold text-[var(--faint)]">PRICE ({sym(s.currency).trim()})</span>
          <input className={`${inputClass} mt-[5px]`} value={price} onChange={(e) => setPrice(e.target.value)} /></label>
        <label><span className="text-[11px] font-extrabold text-[var(--faint)]">DATE</span>
          <input type="date" className={`${inputClass} mt-[5px]`} value={onDate} onChange={(e) => setOnDate(e.target.value)} /></label>
      </div>
      <div className="mt-2 text-[12px] font-semibold text-[var(--faint)]">
        = {exact((Number(qty) || 0) * (Number(price) || 0), s.currency)} against the level's {exact(s.amount, s.currency)}
      </div>
      {go.isError && <div className="mt-2 text-[12px] font-bold text-[var(--danger)]">{String(go.error)}</div>}
      <div className="mt-4 flex justify-end gap-2">
        <button onClick={onClose} className="rounded-[11px] px-4 py-2 text-[13px] font-bold text-[var(--muted)]">Cancel</button>
        <button disabled={go.isPending || !(Number(qty) > 0 && Number(price) > 0)} onClick={() => go.mutate()}
          className="rounded-[11px] bg-[var(--accent)] px-5 py-2 text-[13px] font-extrabold text-white disabled:opacity-50">
          Record buy
        </button>
      </div>
    </Modal>
  );
}

function RuleModal({ r, d, onClose, onDone }: { r: BidsRow; d: BidsDefaults; onClose: () => void; onDone: () => void }) {
  const [dip, setDip] = useState(r.dip_pct != null ? String(r.dip_pct) : "");
  const [amount, setAmount] = useState(r.amount != null ? String(r.amount) : "");
  const [levels, setLevels] = useState(r.max_levels != null ? String(r.max_levels) : "");
  const [peak, setPeak] = useState("");
  const save = useMutation({
    mutationFn: (enabled?: boolean) => papi.bidsRule(r.holding_id, {
      dip_pct: num(dip), amount: num(amount), max_levels: num(levels),
      ...(num(peak) ? { peak: num(peak) } : {}),
      ...(enabled !== undefined ? { enabled } : {}),
    }),
    onSuccess: () => { onDone(); onClose(); },
  });
  const excluded = r.mode === "excluded";
  const usd = r.currency.toUpperCase() !== "INR";
  const def = usd
    ? { dip: d.usd_dip_pct, amount: d.usd_amount, levels: d.usd_max_levels }
    : { dip: d.dip_pct, amount: d.amount, levels: d.max_levels };
  const c = sym(r.currency).trim();
  return (
    <Modal title={`${r.name} · BIDS`} onClose={onClose} width={560}>
      <div className="mb-3 text-[12.5px] font-semibold text-[var(--muted)]">
        Leave a field empty to use the {usd ? "dollar" : "rupee"} default ({def.dip}% · {exact(def.amount, r.currency)} · {def.levels} levels).
      </div>
      <div className="grid grid-cols-3 gap-3">
        <label><span className="text-[11px] font-extrabold text-[var(--faint)]">DIP PER LEVEL (%)</span>
          <input className={`${inputClass} mt-[5px]`} placeholder={String(def.dip)} value={dip} onChange={(e) => setDip(e.target.value)} /></label>
        <label><span className="text-[11px] font-extrabold text-[var(--faint)]">FIRST LEVEL ({c})</span>
          <input className={`${inputClass} mt-[5px]`} placeholder={String(def.amount)} value={amount} onChange={(e) => setAmount(e.target.value)} /></label>
        <label><span className="text-[11px] font-extrabold text-[var(--faint)]">MAX LEVELS</span>
          <input className={`${inputClass} mt-[5px]`} placeholder={String(def.levels)} value={levels} onChange={(e) => setLevels(e.target.value)} /></label>
        <label className="col-span-3" title="Sets a new reference high and restarts the ladder from level 1">
          <span className="text-[11px] font-extrabold text-[var(--faint)]">
            REFERENCE HIGH IN {c} (optional — restarts the ladder from it)
          </span>
          <input className={`${inputClass} mt-[5px]`}
            placeholder={r.peak != null ? `now ${px(r.peak, r.currency)} (${r.peak_source ?? "—"})` : "e.g. the price before the last fall"}
            value={peak} onChange={(e) => setPeak(e.target.value)} />
        </label>
      </div>
      {save.isError && <div className="mt-2 text-[12px] font-bold text-[var(--danger)]">{String(save.error)}</div>}
      <div className="mt-4 flex items-center justify-between">
        <button disabled={save.isPending} onClick={() => save.mutate(excluded)}
          className="text-[12.5px] font-extrabold text-[var(--muted)] hover:text-[var(--strong)]">
          {excluded ? "Include in BIDS" : "Exclude from BIDS"}
        </button>
        <div className="flex gap-2">
          <button onClick={onClose} className="rounded-[11px] px-4 py-2 text-[13px] font-bold text-[var(--muted)]">Cancel</button>
          <button disabled={save.isPending} onClick={() => save.mutate(undefined)}
            className="rounded-[11px] bg-[var(--accent)] px-5 py-2 text-[13px] font-extrabold text-white disabled:opacity-50">
            Save
          </button>
        </div>
      </div>
    </Modal>
  );
}

const GRID = "grid-cols-[2.1fr_.8fr_.9fr_.9fr_1fr_1.1fr_.9fr_.7fr_.6fr_1.5fr_.4fr]";
const MIN_W = "min-w-[1180px]";

function GroupTable({ group, rows, onEdit }: { group: string; rows: BidsRow[]; onEdit: (r: BidsRow) => void }) {
  const value = rows.reduce((s, r) => s + r.position.value_inr, 0);
  const invested = rows.reduce((s, r) => s + r.position.invested_inr, 0);
  const gain = value - invested;
  const ccys = new Set(rows.map((r) => r.position.currency));
  const native = ccys.size === 1 && !ccys.has("INR") ? [...ccys][0] : null;
  const nValue = native ? rows.reduce((s, r) => s + r.position.value, 0) : 0;
  const nInvested = native ? rows.reduce((s, r) => s + r.position.invested, 0) : 0;
  const active = rows.filter((r) => r.mode !== "excluded").length;
  return (
    <Card pad="p-0" className="mb-4 overflow-x-auto">
      <div className={`flex ${MIN_W} flex-wrap items-baseline gap-x-4 gap-y-1 border-b border-[var(--border)] px-5 py-3`}>
        <span className="text-[15px] font-bold text-[var(--strong)]">{group}</span>
        <span className="text-[12px] font-semibold text-[var(--faint)]">{rows.length} holding{rows.length === 1 ? "" : "s"} · {active} in BIDS</span>
        <span className="ml-auto flex flex-wrap items-baseline gap-x-4 text-[12.5px] tabular-nums">
          {native && (
            <span className="font-semibold text-[var(--muted)]">
              {amt(nValue, native)} on {amt(nInvested, native)}
              {nInvested > 0 && <b style={{ color: nValue >= nInvested ? "var(--pos)" : "var(--danger)" }}> {pct((nValue / nInvested - 1) * 100)}</b>}
            </span>
          )}
          <span className="font-bold text-[var(--strong)]">{money(value)}</span>
          <span className="font-bold" style={{ color: gain >= 0 ? "var(--pos)" : "var(--danger)" }}>
            {signedMoney(gain)}{invested > 0 ? ` · ${pct((gain / invested) * 100)}` : ""}
          </span>
        </span>
      </div>
      <div className={`grid ${GRID} ${MIN_W} gap-2 border-b border-[var(--divider)] px-5 py-2 text-[11px] font-extrabold tracking-[.05em] text-[var(--faint)]`}>
        <span>HOLDING</span>
        <span className="text-right">UNITS</span><span className="text-right">AVG PRICE</span>
        <span className="text-right">LTP</span><span className="text-right">VALUE</span>
        <span className="text-right">RETURNS</span>
        <span className="text-right">PEAK</span><span className="text-right">DIP</span>
        <span className="text-right">LEVELS</span><span className="text-right">NEXT LEVEL</span><span />
      </div>
      {rows.map((r) => {
        const pill = MODE_PILL[r.mode];
        const custom = r.dip_pct != null || r.amount != null || r.max_levels != null;
        const p = r.position;
        const pc = p.currency;
        return (
          <div key={r.holding_id} className={`grid ${GRID} ${MIN_W} items-center gap-2 border-b border-[var(--divider)] px-5 py-2.5 text-[13px] ${r.mode === "excluded" ? "opacity-55" : ""}`}>
            <span className="min-w-0">
              <span className="flex items-center gap-2">
                <span className="truncate font-bold text-[var(--strong)]">{r.name}</span>
                <Pill bg={pill.bg} color={pill.color} title={pill.title}>{pill.label}</Pill>
              </span>
              <span className="text-[11.5px] font-semibold text-[var(--faint)]">
                {r.rule.dip_pct}% · {exact(r.rule.amount, r.currency)} · {r.rule.max_levels} lvls{custom ? " · own rule" : ""}
              </span>
            </span>
            <span className="text-right tabular-nums text-[var(--muted)]">{units(p.units)}</span>
            <span className="text-right tabular-nums text-[var(--muted)]">{px(p.avg_price, pc)}</span>
            <span className="text-right tabular-nums text-[var(--strong)]" title={pc !== r.currency ? `ladder reads ${px(r.price, r.currency)}` : undefined}>
              {px(p.ltp, pc)}
            </span>
            <span className="text-right tabular-nums font-semibold text-[var(--strong)]"
              title={pc !== "INR" ? `${money(p.value_inr)} in rupees` : undefined}>
              {amt(p.value, pc)}
            </span>
            <span className="text-right tabular-nums font-bold" style={{ color: p.gain >= 0 ? "var(--pos)" : "var(--danger)" }}
              title={pc !== "INR" ? `${signedMoney(p.gain_inr)} in rupees, currency included` : undefined}>
              {p.invested > 0 ? <>{p.gain >= 0 ? "+" : ""}{amt(p.gain, pc)}<span className="block text-[11.5px]">{p.gain_pct != null ? pct(p.gain_pct) : ""}</span></> : "—"}
            </span>
            <span className="text-right tabular-nums text-[var(--muted)]" title={r.peak_source ? PEAK_SOURCE[r.peak_source] : "set at the next check"}>
              {r.peak != null ? px(r.peak, r.currency) : "—"}
              {r.peak_source && r.peak_source !== "high" && <span className="block text-[10.5px] font-extrabold text-[var(--note)]">{r.peak_source === "joined" ? "joined" : "typed"}</span>}
            </span>
            <span className="text-right tabular-nums font-bold" style={{ color: (r.drawdown_pct ?? 0) < 0 ? "var(--danger)" : "var(--muted)" }}>
              {r.drawdown_pct != null ? pct(r.drawdown_pct) : "—"}
            </span>
            <span className="text-right tabular-nums text-[var(--muted)]">{r.levels_fired}/{r.rule.max_levels}</span>
            <span className="text-right tabular-nums text-[var(--muted)]">
              {r.next ? <>L{r.next.level} at {px(r.next.trigger_price, r.currency)} · <b className="text-[var(--strong)]">{exact(r.next.amount, r.currency)}</b></>
                : r.peak != null ? "capped — waits for a new high" : "—"}
            </span>
            <span className="text-right">
              <button onClick={() => onEdit(r)} className="text-[12px] font-extrabold text-[var(--accent-deep)] hover:underline">Edit</button>
            </span>
          </div>
        );
      })}
    </Card>
  );
}

export default function BidsView() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["portfolio-bids"], queryFn: papi.bids });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["portfolio-bids"] });
    qc.invalidateQueries({ queryKey: ["portfolio"] });
  };
  const skip = useMutation({ mutationFn: (sid: number) => papi.bidsSkip(sid), onSuccess: refresh });
  const evaluate = useMutation({ mutationFn: papi.bidsEvaluate, onSuccess: refresh });
  const [accepting, setAccepting] = useState<BidsSuggestion | null>(null);
  const [editing, setEditing] = useState<BidsRow | null>(null);

  if (q.isLoading) return <Card><div className="py-6 text-center text-[13px] font-semibold text-[var(--faint)]">Loading…</div></Card>;
  if (q.isError || !q.data) return <Notice tone="warn">Could not load BIDS: {String(q.error ?? "no data")}</Notice>;
  const { defaults: d, rows, pending, recent } = q.data;

  const active = rows.filter((r) => r.mode !== "excluded");
  const pendingInr = pending.filter((p) => p.currency.toUpperCase() === "INR").reduce((s, p) => s + p.amount, 0);
  const pendingUsd = pending.filter((p) => p.currency.toUpperCase() === "USD").reduce((s, p) => s + p.amount, 0);
  const deepest = active.filter((r) => r.drawdown_pct != null)
    .sort((a, b) => (a.drawdown_pct ?? 0) - (b.drawdown_pct ?? 0))[0];
  const byGroup = new Map<string, BidsRow[]>();
  for (const r of rows) byGroup.set(r.group, [...(byGroup.get(r.group) ?? []), r]);
  const groups = [...byGroup.keys()].sort((a, b) => {
    const ia = GROUP_ORDER.indexOf(a); const ib = GROUP_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });
  const sortRows = (xs: BidsRow[]) => [...xs].sort((a, b) => {
    const rank = (r: BidsRow) => (r.mode === "excluded" ? 1 : 0);
    return rank(a) - rank(b) || (a.drawdown_pct ?? 0) - (b.drawdown_pct ?? 0) || b.position.value_inr - a.position.value_inr;
  });

  return (
    <div>
      <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-4">
        <Kpi label="TO REVIEW" value={String(pending.length)}
          sub={pending.length ? [pendingInr ? money(pendingInr) : "", pendingUsd ? amt(pendingUsd, "USD") : ""].filter(Boolean).join(" + ") + " across the levels" : "nothing pending"}
          valueColor={pending.length ? "var(--note)" : "var(--strong)"}
          help="Dip levels that fired on holdings with no order path here. Accept records the buy you placed; skip consumes the level." />
        <Kpi label="IN BIDS" value={String(active.length)}
          sub={`${rows.filter((r) => r.mode === "auto").length} auto · ${rows.filter((r) => r.mode === "suggest").length} suggest · ${rows.filter((r) => r.mode === "excluded").length} excluded`}
          help="Stocks, ETFs, mutual funds and US stocks. AUTO needs a running BIDS deployment on the holding's broker account." />
        <Kpi label="DEEPEST DIP" value={deepest?.drawdown_pct != null ? pct(deepest.drawdown_pct) : "—"}
          sub={deepest ? deepest.name : "no ladder yet"} valueColor={deepest && (deepest.drawdown_pct ?? 0) < 0 ? "var(--danger)" : "var(--strong)"}
          help="The holding furthest below its recent high right now." />
        <Kpi label="LAST CHECK" value={<button onClick={() => evaluate.mutate()} disabled={evaluate.isPending}
            className="text-[15px] font-extrabold text-[var(--accent-deep)] underline disabled:opacity-50">{evaluate.isPending ? "Checking…" : "Check now"}</button>}
          sub="runs by itself after the 09:30 and 16:00 price passes"
          help="Each holding is evaluated once per new price date, so a second check the same day adds nothing. A box holding a pulled copy of the portfolio (SKAS_PORTFOLIO_MAINTENANCE=0) only checks when you press this." />
      </div>

      {pending.length > 0 && (
        <Card className="mb-4">
          <div className="mb-2 text-[15px] font-bold text-[var(--strong)]">To review</div>
          {pending.map((s) => (
            <div key={s.id} className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-[var(--divider)] py-2.5 text-[13px]">
              <span className="min-w-[180px] font-bold text-[var(--strong)]">{s.holding}</span>
              <Pill>L{s.level}</Pill>
              <span className="tabular-nums text-[var(--muted)]">at {px(s.price, s.currency)} · trigger {px(s.trigger_price, s.currency)} · high {px(s.peak, s.currency)}</span>
              <span className="tabular-nums font-bold text-[var(--strong)]">{exact(s.amount, s.currency)}</span>
              {s.units_hint != null && <span className="tabular-nums text-[var(--faint)]">≈ {units(s.units_hint)} units</span>}
              <span className="text-[var(--faint)]">{s.created_on}</span>
              <span className="ml-auto flex items-center gap-3">
                <button onClick={() => setAccepting(s)} className="rounded-[10px] bg-[var(--accent)] px-4 py-1.5 text-[12.5px] font-extrabold text-white">Accept</button>
                <ConfirmAction label="Skip" onConfirm={() => skip.mutate(s.id)}
                  className="text-[12.5px] font-extrabold text-[var(--faint)] hover:text-[var(--strong)]" />
              </span>
            </div>
          ))}
        </Card>
      )}

      <DefaultsCard d={d} onSaved={refresh} />

      {groups.length === 0 && (
        <Card><div className="py-2 text-[13px] font-semibold text-[var(--faint)]">No stocks, ETFs, mutual funds or US stocks on the portfolio yet.</div></Card>
      )}
      {groups.map((g) => (
        <GroupTable key={g} group={g} rows={sortRows(byGroup.get(g) ?? [])} onEdit={setEditing} />
      ))}

      {recent.length > 0 && (
        <Card>
          <div className="mb-2 text-[15px] font-bold text-[var(--strong)]">Recent</div>
          {recent.slice(0, 12).map((s) => (
            <div key={s.id} className="flex flex-wrap gap-x-4 border-t border-[var(--divider)] py-2 text-[12.5px] text-[var(--muted)]">
              <span className="min-w-[180px] font-semibold text-[var(--strong)]">{s.holding}</span>
              <span>L{s.level} · {exact(s.amount, s.currency)}</span>
              <span className="font-bold uppercase" style={{ color: s.status === "accepted" ? "var(--pos)" : "var(--faint)" }}>{s.status}</span>
              <span>{s.created_on}</span>
            </div>
          ))}
        </Card>
      )}

      {accepting && <AcceptModal s={accepting} onClose={() => setAccepting(null)} onDone={refresh} />}
      {editing && <RuleModal r={editing} d={d} onClose={() => setEditing(null)} onDone={refresh} />}
    </div>
  );
}
