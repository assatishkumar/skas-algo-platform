import { useMemo, useState } from "react";
import { portfolio as papi } from "../../api/client";
import {
  dayLabel, money, pct, priceLabel,
  type Holding, type IncomeView as Income, type PortfolioPayload,
} from "../../lib/portfolio";
import {
  Card, ConfirmAction, inputClass, Kpi, Modal, Notice, selectClass,
} from "./primitives";

const GRID = "grid-cols-[1.9fr_.7fr_1fr_.9fr_1fr_1fr_.9fr]";

function LogPayment({
  rows, onClose, onSaved,
}: { rows: Holding[]; onClose: () => void; onSaved: () => void }) {
  const [holdingId, setHoldingId] = useState<number | "">("");
  const [onDate, setOnDate] = useState(new Date().toISOString().slice(0, 10));
  const [amount, setAmount] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  return (
    <Modal title="Record a distribution" onClose={onClose} width={520}>
      <div className="grid grid-cols-2 gap-3">
        <label className="col-span-2">
          <span className="text-[11px] font-extrabold tracking-[.04em] text-[var(--faint)]">
            HOLDING
          </span>
          <select
            className={`${selectClass} mt-[5px]`}
            value={holdingId}
            onChange={(e) => setHoldingId(e.target.value ? Number(e.target.value) : "")}
          >
            <option value="">Pick a holding…</option>
            {[...rows].sort((a, b) => a.name.localeCompare(b.name)).map((h) => (
              <option key={h.id} value={h.id}>{h.name}</option>
            ))}
          </select>
        </label>
        <label>
          <span className="text-[11px] font-extrabold tracking-[.04em] text-[var(--faint)]">
            DATE CREDITED
          </span>
          <input type="date" className={`${inputClass} mt-[5px]`} value={onDate}
            onChange={(e) => setOnDate(e.target.value)} />
        </label>
        <label>
          <span className="text-[11px] font-extrabold tracking-[.04em] text-[var(--faint)]">
            AMOUNT (₹)
          </span>
          <input type="number" className={`${inputClass} mt-[5px]`} value={amount}
            placeholder="12500" onChange={(e) => setAmount(e.target.value)} />
        </label>
        <label className="col-span-2">
          <span className="text-[11px] font-extrabold tracking-[.04em] text-[var(--faint)]">
            NOTE (optional)
          </span>
          <input className={`${inputClass} mt-[5px]`} value={note} placeholder="FY27 interim"
            onChange={(e) => setNote(e.target.value)} />
        </label>
      </div>
      {error && <div className="mt-2.5 text-[12px] font-bold text-[var(--danger)]">{error}</div>}
      <div className="mt-4 flex justify-end gap-2.5">
        <button onClick={onClose}
          className="rounded-[11px] border-[1.5px] border-[var(--field-border)] bg-[var(--ghost)] px-[18px] py-2.5 text-[13px] font-extrabold text-[var(--muted)]">
          Cancel
        </button>
        <button
          disabled={busy}
          onClick={async () => {
            if (!holdingId) return setError("Pick the holding it was paid on.");
            if (!(Number(amount) > 0)) return setError("Enter the amount received.");
            setBusy(true);
            setError("");
            try {
              await papi.addDividend({
                holding_id: Number(holdingId), on_date: onDate,
                amount: Number(amount), per_unit: null, note: note || null,
              });
              onSaved();
              onClose();
            } catch (e) {
              setError(e instanceof Error ? e.message : String(e));
            } finally {
              setBusy(false);
            }
          }}
          className="rounded-[11px] bg-[var(--accent)] px-[22px] py-2.5 text-[13px] font-extrabold text-white disabled:opacity-50"
        >
          {busy ? "Saving…" : "Record"}
        </button>
      </div>
    </Modal>
  );
}

export default function IncomeView({
  rows, payload, onSetYield, onChanged,
}: {
  rows: Holding[];
  payload: PortfolioPayload;
  onSetYield: (h: Holding, yieldPct: number | null) => void;
  onChanged: () => void;
}) {
  const income: Income = payload.income;
  const [logging, setLogging] = useState(false);
  const [draft, setDraft] = useState<Record<number, string>>({});

  const ledger = useMemo(
    () => [...(payload.dividends ?? [])].sort((a, b) => b.on_date.localeCompare(a.on_date)),
    [payload.dividends],
  );
  const byId = useMemo(() => new Map(rows.map((h) => [h.id, h])), [rows]);

  return (
    <div>
      <div className="mb-3.5 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Kpi
          label="EXPECTED / YEAR" value={money(income.expected_annual)}
          sub={`${money(income.expected_monthly)} a month · ${pct(income.portfolio_yield_pct, 2)} of the book`}
          help="What today's holdings should distribute over a year, from the yield you've entered against each. Derived from current value, so it moves with the position instead of ageing into a stale forecast."
        />
        <Kpi
          label={`RECEIVED ${income.fy}`} value={money(income.received_fy)}
          valueColor="var(--pos)"
          sub={income.expected_annual > 0
            ? `${pct(income.received_fy / income.expected_annual * 100, 0)} of the year's estimate`
            : "no estimate to compare"}
          help="Distributions actually credited this financial year (1 April to 31 March — India's, not the calendar's)."
        />
        <Kpi
          label="RECEIVED ALL TIME" value={money(income.received_total)}
          sub={`${ledger.length} payment${ledger.length === 1 ? "" : "s"} logged`}
          help="Every distribution recorded here. Only what you've logged — this isn't pulled from a broker."
        />
        <Kpi
          label="YIELD NOT SET"
          value={income.unpriced_share_pct > 0 ? `${income.unpriced_share_pct}%` : "—"}
          valueColor={income.unpriced_share_pct > 20 ? "var(--warn-text)" : "var(--faint)"}
          sub={income.unpriced_value > 0 ? `${money(income.unpriced_value)} unaccounted` : "all set"}
          help="Share of the book with no yield entered. Those holdings are left OUT of the expected figure rather than counted as zero — 'unknown' and 'pays nothing' lead to different decisions."
        />
      </div>

      {income.unpriced_share_pct > 20 && (
        <div className="mb-3.5">
          <Notice tone="warn">
            No yield is set on {income.unpriced_share_pct}% of the book, so the expected figure
            above covers only the rest. Enter a yield below on anything that distributes — a
            REIT, an InvIT, an FD, a dividend fund.
          </Notice>
        </div>
      )}

      <Card>
        <div className="mb-3 flex flex-wrap items-center gap-3">
          <span className="text-[15px] font-extrabold text-[var(--strong)]">
            By holding
          </span>
          <span className="text-[11.5px] font-semibold text-[var(--faint)]">
            Yield is typed — no free feed publishes Indian dividend yields, and a guessed one
            would put fictitious income on a planning screen.
          </span>
          <button
            onClick={() => setLogging(true)}
            className="ml-auto rounded-[11px] bg-[var(--accent)] px-[16px] py-2 text-[12.5px] font-extrabold text-white"
          >
            + Record a payment
          </button>
        </div>

        <div className="overflow-x-auto">
          <div className="min-w-[820px]">
            <div className={`grid ${GRID} gap-2.5 border-b border-[var(--border)] px-1 py-2 text-[11px] font-extrabold tracking-[.05em] text-[var(--faint)]`}>
              <span>HOLDING</span>
              <span className="text-right">YIELD</span>
              <span className="text-right">EXPECTED / YR</span>
              <span className="text-right" title="Expected income against what you PAID — what the original decision now returns, rather than what today's price would buy.">
                ON COST
              </span>
              <span className="text-right">RECEIVED {income.fy}</span>
              <span className="text-right">ALL TIME</span>
              <span className="text-right">LAST PAID</span>
            </div>

            {rows.length === 0 && (
              <div className="px-1 py-8 text-center text-[13px] font-semibold text-[var(--faint)]">
                Nothing tracked yet.
              </div>
            )}

            {[...rows]
              .sort((a, b) => {
                const la = income.lines.find((l) => l.holding_id === a.id);
                const lb = income.lines.find((l) => l.holding_id === b.id);
                return (lb?.expected_annual ?? -1) - (la?.expected_annual ?? -1);
              })
              .map((h) => {
                const line = income.lines.find((l) => l.holding_id === h.id);
                const shown = draft[h.id] ?? (h.dividend_yield_pct?.toString() ?? "");
                return (
                  <div
                    key={h.id}
                    className={`grid ${GRID} items-center gap-2.5 border-b border-[var(--divider)] px-1 py-2 text-[12.5px]`}
                  >
                    <span className="truncate font-extrabold text-[var(--strong)]">
                      {h.name}
                      <span className="ml-1.5 text-[10.5px] font-semibold text-[var(--faint)]">
                        {h.class_label}
                      </span>
                    </span>
                    <span className="text-right">
                      <input
                        type="number"
                        value={shown}
                        placeholder="—"
                        onChange={(e) => setDraft((d) => ({ ...d, [h.id]: e.target.value }))}
                        onBlur={() => {
                          const raw = draft[h.id];
                          if (raw === undefined) return;
                          const next = raw.trim() === "" ? null : Number(raw);
                          if (next === null || Number.isFinite(next)) {
                            if (next !== h.dividend_yield_pct) onSetYield(h, next);
                          }
                          setDraft((d) => {
                            const { [h.id]: _drop, ...rest } = d;
                            return rest;
                          });
                        }}
                        className="w-14 rounded-md border-[1.5px] border-transparent bg-transparent px-1 py-0.5 text-right font-semibold tabular-nums text-[var(--muted)] outline-none hover:border-[var(--field-border)] focus:border-[var(--accent)] focus:bg-[var(--field)]"
                      />
                      <span className="text-[var(--faint)]">%</span>
                    </span>
                    <span className="text-right font-bold tabular-nums text-[var(--strong)]">
                      {line?.expected_annual != null ? money(line.expected_annual) : "—"}
                    </span>
                    <span className="text-right font-semibold tabular-nums text-[var(--muted)]">
                      {line?.yield_on_cost_pct != null ? pct(line.yield_on_cost_pct, 2) : "—"}
                    </span>
                    <span
                      className="text-right font-bold tabular-nums"
                      style={{ color: line?.received_fy ? "var(--pos)" : "var(--faint)" }}
                    >
                      {line?.received_fy ? money(line.received_fy) : "—"}
                    </span>
                    <span className="text-right font-semibold tabular-nums text-[var(--muted)]">
                      {line?.received_total ? money(line.received_total) : "—"}
                    </span>
                    <span className="text-right font-semibold text-[var(--faint)]">
                      {line?.last_paid ? dayLabel(line.last_paid) : "—"}
                    </span>
                  </div>
                );
              })}
          </div>
        </div>
      </Card>

      {ledger.length > 0 && (
        <Card className="mt-3.5">
          <div className="mb-3 text-[15px] font-extrabold text-[var(--strong)]">
            Payments received
          </div>
          <div className="max-h-[40vh] overflow-y-auto">
            <div className="grid grid-cols-[1fr_1.6fr_1fr_1.4fr_.6fr] gap-2.5 border-b border-[var(--border)] px-1 py-2 text-[11px] font-extrabold tracking-[.05em] text-[var(--faint)]">
              <span>DATE</span><span>HOLDING</span>
              <span className="text-right">AMOUNT</span><span>NOTE</span><span />
            </div>
            {ledger.map((d) => (
              <div
                key={d.id}
                className="grid grid-cols-[1fr_1.6fr_1fr_1.4fr_.6fr] items-center gap-2.5 border-b border-[var(--divider)] px-1 py-2 text-[12.5px]"
              >
                <span className="font-semibold text-[var(--strong)]">{dayLabel(d.on_date)}</span>
                <span className="truncate font-extrabold text-[var(--strong)]">
                  {byId.get(d.holding_id)?.name ?? d.holding}
                </span>
                <span className="text-right font-bold tabular-nums text-[var(--pos)]">
                  {priceLabel(d.amount)}
                </span>
                <span className="truncate text-[11.5px] font-semibold text-[var(--faint)]">
                  {d.note ?? ""}
                </span>
                <span className="text-right">
                  <ConfirmAction
                    label="Delete"
                    onConfirm={async () => { await papi.deleteDividend(d.id); onChanged(); }}
                    className="text-[11.5px] font-extrabold text-[var(--faint)] hover:text-[var(--danger)]"
                  />
                </span>
              </div>
            ))}
          </div>
        </Card>
      )}

      {logging && (
        <LogPayment rows={rows} onClose={() => setLogging(false)} onSaved={onChanged} />
      )}
    </div>
  );
}
