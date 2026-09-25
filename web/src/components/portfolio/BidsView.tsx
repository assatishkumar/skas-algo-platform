import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { portfolio as papi } from "../../api/client";
import {
  exactMoney, money, pct, priceLabel,
  type BidsDefaults, type BidsRow, type BidsSuggestion,
} from "../../lib/portfolio";
import { Card, ConfirmAction, inputClass, Kpi, Modal, Notice, Pill } from "./primitives";

/** BIDS — Buy In Dips (owner design 2026-09-25). Every market-linked holding is bought again
 *  each time it falls another X% below its recent high: y, then 2y, 3y … up to N levels, and a
 *  new high resets the ladder. Holdings a running BIDS deployment trades buy AUTOMATICALLY;
 *  everything else lands here as a suggestion to accept (a ledger BUY, your own order) or skip
 *  (the level is consumed; the next X% still fires). services/bids.py is the engine. */

const CLASS_LABEL: Record<string, string> = { stk: "Stock", etf: "ETF", mf: "Mutual fund", us: "US" };

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

function num(v: string): number | null {
  const n = Number(v);
  return v.trim() === "" || !Number.isFinite(n) ? null : n;
}

function DefaultsCard({ d, onSaved }: { d: BidsDefaults; onSaved: () => void }) {
  const [form, setForm] = useState({
    dip_pct: String(d.dip_pct), amount: String(d.amount), max_levels: String(d.max_levels),
    fund_source: d.fund_source,
  });
  useEffect(() => {
    setForm({
      dip_pct: String(d.dip_pct), amount: String(d.amount), max_levels: String(d.max_levels),
      fund_source: d.fund_source,
    });
  }, [d]);
  const save = useMutation({
    mutationFn: () => papi.bidsDefaults({
      dip_pct: num(form.dip_pct) ?? undefined, amount: num(form.amount) ?? undefined,
      max_levels: num(form.max_levels) ?? undefined, fund_source: form.fund_source.trim().toUpperCase(),
    }),
    onSuccess: onSaved,
  });
  const x = num(form.dip_pct) ?? 0;
  const y = num(form.amount) ?? 0;
  const n = num(form.max_levels) ?? 0;
  const fields: [keyof typeof form, string, string][] = [
    ["dip_pct", "DIP PER LEVEL (%)", "each level is this much further below the recent high"],
    ["amount", "FIRST LEVEL (₹)", "level k invests k × this"],
    ["max_levels", "MAX LEVELS", "no level beyond this until a new high resets the ladder"],
    ["fund_source", "FUNDED FROM", "the ETF the auto runs sell to pay (T+1, like value investing)"],
  ];
  return (
    <Card className="mb-4">
      <div className="mb-3 flex flex-wrap items-baseline gap-2">
        <div className="text-[15px] font-bold text-[var(--strong)]">Defaults</div>
        <div className="text-[12px] font-semibold text-[var(--faint)]">
          every holding uses these unless you set its own — a full ladder of {n} level{n === 1 ? "" : "s"} at
          {" "}{x}% steps invests {exactMoney(y * (n * (n + 1)) / 2)} down to −{(x * n).toFixed(0)}%
        </div>
      </div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {fields.map(([key, label, hint]) => (
          <label key={key} title={hint}>
            <span className="text-[11px] font-extrabold tracking-[.04em] text-[var(--faint)]">{label}</span>
            <input className={`${inputClass} mt-[5px]`} value={form[key]}
              onChange={(e) => setForm((f) => ({ ...f, [key]: e.target.value }))} />
          </label>
        ))}
      </div>
      <div className="mt-3 flex items-center gap-3">
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
  const [units, setUnits] = useState(s.units_hint != null ? String(Number(s.units_hint.toFixed(3))) : "");
  const [price, setPrice] = useState(String(s.price));
  const [onDate, setOnDate] = useState(new Date().toISOString().slice(0, 10));
  const go = useMutation({
    mutationFn: () => papi.bidsAccept(s.id, { units: Number(units), price: Number(price), on_date: onDate }),
    onSuccess: () => { onDone(); onClose(); },
  });
  return (
    <Modal title={`Record the buy · ${s.holding} · level ${s.level}`} onClose={onClose} width={520}>
      <div className="mb-3 text-[12.5px] font-semibold text-[var(--muted)]">
        Place the order yourself (your fund platform or US broker), then record what you actually
        got. It lands as a BUY in this holding's ledger; the level stays consumed either way.
      </div>
      <div className="grid grid-cols-3 gap-3">
        <label><span className="text-[11px] font-extrabold text-[var(--faint)]">UNITS</span>
          <input className={`${inputClass} mt-[5px]`} value={units} onChange={(e) => setUnits(e.target.value)} /></label>
        <label><span className="text-[11px] font-extrabold text-[var(--faint)]">PRICE</span>
          <input className={`${inputClass} mt-[5px]`} value={price} onChange={(e) => setPrice(e.target.value)} /></label>
        <label><span className="text-[11px] font-extrabold text-[var(--faint)]">DATE</span>
          <input type="date" className={`${inputClass} mt-[5px]`} value={onDate} onChange={(e) => setOnDate(e.target.value)} /></label>
      </div>
      <div className="mt-2 text-[12px] font-semibold text-[var(--faint)]">
        = {exactMoney((Number(units) || 0) * (Number(price) || 0))} against the level's {exactMoney(s.amount)}
      </div>
      {go.isError && <div className="mt-2 text-[12px] font-bold text-[var(--danger)]">{String(go.error)}</div>}
      <div className="mt-4 flex justify-end gap-2">
        <button onClick={onClose} className="rounded-[11px] px-4 py-2 text-[13px] font-bold text-[var(--muted)]">Cancel</button>
        <button disabled={go.isPending || !(Number(units) > 0 && Number(price) > 0)} onClick={() => go.mutate()}
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
  return (
    <Modal title={`${r.name} · BIDS`} onClose={onClose} width={560}>
      <div className="mb-3 text-[12.5px] font-semibold text-[var(--muted)]">
        Leave a field empty to use the default ({d.dip_pct}% · {exactMoney(d.amount)} · {d.max_levels} levels).
      </div>
      <div className="grid grid-cols-3 gap-3">
        <label><span className="text-[11px] font-extrabold text-[var(--faint)]">DIP PER LEVEL (%)</span>
          <input className={`${inputClass} mt-[5px]`} placeholder={String(d.dip_pct)} value={dip} onChange={(e) => setDip(e.target.value)} /></label>
        <label><span className="text-[11px] font-extrabold text-[var(--faint)]">FIRST LEVEL (₹)</span>
          <input className={`${inputClass} mt-[5px]`} placeholder={String(d.amount)} value={amount} onChange={(e) => setAmount(e.target.value)} /></label>
        <label><span className="text-[11px] font-extrabold text-[var(--faint)]">MAX LEVELS</span>
          <input className={`${inputClass} mt-[5px]`} placeholder={String(d.max_levels)} value={levels} onChange={(e) => setLevels(e.target.value)} /></label>
        <label className="col-span-3" title="Sets a new reference high and restarts the ladder from level 1">
          <span className="text-[11px] font-extrabold text-[var(--faint)]">
            REFERENCE HIGH (optional — restarts the ladder from it)
          </span>
          <input className={`${inputClass} mt-[5px]`} placeholder={r.peak != null ? `now ${priceLabel(r.peak)} (${r.peak_source ?? "—"})` : "e.g. the price before the last fall"}
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

const GRID = "grid-cols-[2fr_.8fr_1fr_.9fr_.8fr_.7fr_1.3fr_.5fr]";

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
  const pendingAmount = pending.reduce((s, p) => s + p.amount, 0);
  const deepest = active.filter((r) => r.drawdown_pct != null)
    .sort((a, b) => (a.drawdown_pct ?? 0) - (b.drawdown_pct ?? 0))[0];
  const sorted = [...rows].sort((a, b) => {
    const rank = (r: BidsRow) => (r.mode === "excluded" ? 1 : 0);
    return rank(a) - rank(b) || (a.drawdown_pct ?? 0) - (b.drawdown_pct ?? 0);
  });

  return (
    <div>
      <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-4">
        <Kpi label="TO REVIEW" value={String(pending.length)} sub={pending.length ? `${money(pendingAmount)} across the levels` : "nothing pending"}
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
          sub="runs by itself at 09:30 and 16:00"
          help="Each holding is evaluated once per new price date, so a second check the same day adds nothing." />
      </div>

      {pending.length > 0 && (
        <Card className="mb-4">
          <div className="mb-2 text-[15px] font-bold text-[var(--strong)]">To review</div>
          {pending.map((s) => (
            <div key={s.id} className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-[var(--divider)] py-2.5 text-[13px]">
              <span className="min-w-[180px] font-bold text-[var(--strong)]">{s.holding}</span>
              <Pill>L{s.level}</Pill>
              <span className="tabular-nums text-[var(--muted)]">at {priceLabel(s.price)} · trigger {priceLabel(s.trigger_price)} · high {priceLabel(s.peak)}</span>
              <span className="tabular-nums font-bold text-[var(--strong)]">{exactMoney(s.amount)}</span>
              {s.units_hint != null && <span className="tabular-nums text-[var(--faint)]">≈ {s.units_hint.toLocaleString("en-IN", { maximumFractionDigits: 3 })} units</span>}
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

      <Card pad="p-0" className="mb-4 overflow-x-auto">
        <div className={`grid ${GRID} min-w-[900px] gap-2 border-b border-[var(--border)] px-5 py-2.5 text-[11px] font-extrabold tracking-[.05em] text-[var(--faint)]`}>
          <span>HOLDING</span><span>MODE</span><span className="text-right">PEAK</span>
          <span className="text-right">PRICE</span><span className="text-right">DIP</span>
          <span className="text-right">LEVELS</span><span className="text-right">NEXT LEVEL</span><span />
        </div>
        {sorted.length === 0 && (
          <div className="px-5 py-6 text-[13px] font-semibold text-[var(--faint)]">No stocks, ETFs, mutual funds or US stocks on the portfolio yet.</div>
        )}
        {sorted.map((r) => {
          const pill = MODE_PILL[r.mode];
          const custom = r.dip_pct != null || r.amount != null || r.max_levels != null;
          return (
            <div key={r.holding_id} className={`grid ${GRID} min-w-[900px] items-center gap-2 border-b border-[var(--divider)] px-5 py-2.5 text-[13px] ${r.mode === "excluded" ? "opacity-55" : ""}`}>
              <span className="min-w-0">
                <span className="block truncate font-bold text-[var(--strong)]">{r.name}</span>
                <span className="text-[11.5px] font-semibold text-[var(--faint)]">
                  {CLASS_LABEL[r.asset_class] ?? r.asset_class} · {r.rule.dip_pct}% · {exactMoney(r.rule.amount)} · {r.rule.max_levels} lvls{custom ? " · own rule" : ""}
                </span>
              </span>
              <span><Pill bg={pill.bg} color={pill.color} title={pill.title}>{pill.label}</Pill></span>
              <span className="text-right tabular-nums text-[var(--muted)]" title={r.peak_source ? PEAK_SOURCE[r.peak_source] : "set at the next check"}>
                {r.peak != null ? priceLabel(r.peak) : "—"}
                {r.peak_source && r.peak_source !== "high" && <span className="ml-1 text-[10.5px] font-extrabold text-[var(--note)]">{r.peak_source === "joined" ? "joined" : "typed"}</span>}
              </span>
              <span className="text-right tabular-nums text-[var(--strong)]">{r.price != null ? priceLabel(r.price) : "—"}</span>
              <span className="text-right tabular-nums font-bold" style={{ color: (r.drawdown_pct ?? 0) < 0 ? "var(--danger)" : "var(--muted)" }}>
                {r.drawdown_pct != null ? pct(r.drawdown_pct) : "—"}
              </span>
              <span className="text-right tabular-nums text-[var(--muted)]">{r.levels_fired}/{r.rule.max_levels}</span>
              <span className="text-right tabular-nums text-[var(--muted)]">
                {r.next ? <>L{r.next.level} at {priceLabel(r.next.trigger_price)} · <b className="text-[var(--strong)]">{exactMoney(r.next.amount)}</b></>
                  : r.peak != null ? "capped — waits for a new high" : "—"}
              </span>
              <span className="text-right">
                <button onClick={() => setEditing(r)} className="text-[12px] font-extrabold text-[var(--accent-deep)] hover:underline">Edit</button>
              </span>
            </div>
          );
        })}
      </Card>

      {recent.length > 0 && (
        <Card>
          <div className="mb-2 text-[15px] font-bold text-[var(--strong)]">Recent</div>
          {recent.slice(0, 12).map((s) => (
            <div key={s.id} className="flex flex-wrap gap-x-4 border-t border-[var(--divider)] py-2 text-[12.5px] text-[var(--muted)]">
              <span className="min-w-[180px] font-semibold text-[var(--strong)]">{s.holding}</span>
              <span>L{s.level} · {exactMoney(s.amount)}</span>
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
