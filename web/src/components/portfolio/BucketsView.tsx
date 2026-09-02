import { useMemo, useState } from "react";
import {
  money, pct, rebalanceMoves,
  type Bucket, type BucketInput, type Holding,
} from "../../lib/portfolio";
import { Card, ConfirmAction, Kpi, Notice, Pill } from "./primitives";

/** Rupees below which a gap isn't worth a transfer — brokerage and effort exceed the fix. */
const DEAD_BAND = 10_000;

export default function BucketsView({
  rows, buckets, onCreate, onUpdate, onDelete, onSetExcluded,
}: {
  rows: Holding[];
  buckets: Bucket[];
  onCreate: (b: BucketInput) => void;
  onUpdate: (id: number, b: BucketInput) => void;
  onDelete: (id: number) => void;
  onSetExcluded: (h: Holding, excluded: boolean) => void;
}) {
  // Name and target edit locally and save on blur — a request per keystroke would fight the
  // cursor and flood the box.
  const [draft, setDraft] = useState<Record<number, { name?: string; target?: string }>>({});

  const byId = useMemo(() => new Map(rows.map((h) => [h.id, h])), [rows]);
  const netWorth = rows.reduce((a, h) => a + h.value, 0);

  const valueOf = (b: Bucket) =>
    b.holding_ids.reduce((a, id) => a + (byId.get(id)?.value ?? 0), 0);

  const bucketed = buckets.reduce((a, b) => a + valueOf(b), 0);
  const targetSum = buckets.reduce((a, b) => a + b.target_pct, 0);

  const inBucket = new Set(buckets.flatMap((b) => b.holding_ids));
  const loose = rows.filter((h) => !inBucket.has(h.id) && !h.excluded_from_buckets);
  const excluded = rows.filter((h) => !inBucket.has(h.id) && h.excluded_from_buckets);

  const moves = useMemo(
    () =>
      rebalanceMoves(
        buckets.map((b) => ({
          name: b.name,
          excess: valueOf(b) - (b.target_pct / 100) * bucketed,
        })),
        DEAD_BAND,
      ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [buckets, bucketed, rows],
  );

  const misallocated = buckets.reduce((a, b) => {
    const excess = valueOf(b) - (b.target_pct / 100) * bucketed;
    return a + (excess > 0 ? excess : 0);
  }, 0);

  const bucketedRows = rows.filter((h) => inBucket.has(h.id));
  const bucketedXirr = bucketedRows.length
    ? bucketedRows.reduce((a, h) => a + (h.xirr_pct ?? 0) * h.value, 0)
      / (bucketedRows.reduce((a, h) => a + h.value, 0) || 1)
    : 0;
  const largest = [...buckets].sort((a, b) => valueOf(b) - valueOf(a))[0];

  const asInput = (b: Bucket): BucketInput => ({
    name: b.name, target_pct: b.target_pct, holding_ids: b.holding_ids,
  });

  return (
    <div>
      <div className="mb-3.5 grid grid-cols-2 gap-3 lg:grid-cols-5">
        <Kpi
          label="BUCKETED MONEY" size={20} value={money(bucketed)}
          sub={`${netWorth > 0 ? ((bucketed / netWorth) * 100).toFixed(1) : "0.0"}% of net worth`}
          help="Total value inside buckets. The rest sits unassigned or excluded in the trays below."
        />
        <Kpi
          label="BUCKETS" size={20} value={String(buckets.length)}
          sub={`targets sum ${targetSum.toFixed(0)}%`}
          help="Targets should sum to 100% — otherwise each bucket's 'target share' is measured against a total that doesn't exist."
        />
        <Kpi
          label="BUCKETED RETURN" size={20} value={pct(bucketedXirr)}
          valueColor={bucketedXirr >= 0 ? "var(--pos)" : "var(--danger)"}
          sub="value-weighted"
          help="Annualised return of bucketed holdings, weighted by size."
        />
        <Kpi
          label="LARGEST BUCKET" size={20} value={largest ? largest.name : "—"}
          sub={largest && bucketed > 0
            ? `${money(valueOf(largest))} · ${((valueOf(largest) / bucketed) * 100).toFixed(1)}%`
            : ""}
          help="Where most of the bucketed money sits right now."
        />
        <Kpi
          label="TO REBALANCE" size={20} value={money(misallocated)}
          valueColor={misallocated > DEAD_BAND ? "var(--note)" : "var(--pos)"}
          sub={moves.length ? `across ${moves.length} move${moves.length > 1 ? "s" : ""}` : "on target"}
          help="Money sitting in the wrong bucket against your targets. Under ~2% of bucketed money is fine to leave alone."
        />
      </div>

      {buckets.length > 0 && Math.abs(targetSum - 100) > 0.5 && (
        <div className="mb-3.5">
          <Notice tone="warn">
            Bucket targets sum to {targetSum.toFixed(0)}% — set them to total 100% for the
            rebalance figures to mean anything.
          </Notice>
        </div>
      )}

      {moves.length > 0 && (
        <div className="mb-3.5 flex flex-col gap-1.5 rounded-xl border border-[var(--tint-border)] bg-[var(--tint)] px-[18px] py-3.5">
          <span
            className="text-[13.5px] font-extrabold text-[var(--ft)]"
            title="The cheapest path back to your targets. Prefer routing fresh money over selling — no tax that way."
          >
            Rebalance between buckets
          </span>
          {moves.map((m) => (
            <span key={`${m.from}->${m.to}`} className="text-[13px] font-bold text-[var(--ft)]">
              → Move {money(m.amount)} from {m.from} to {m.to}
            </span>
          ))}
        </div>
      )}

      <div className="grid items-start gap-3.5 md:grid-cols-2 xl:grid-cols-3">
        {buckets.map((b) => {
          const value = valueOf(b);
          const actual = bucketed > 0 ? (value / bucketed) * 100 : 0;
          const off = actual - b.target_pct;
          const near = Math.abs(off) < 1;
          const d = draft[b.id] ?? {};
          const available = loose;

          return (
            <Card key={b.id} pad="p-[16px_18px]">
              <div className="mb-2.5 flex items-center gap-2">
                <input
                  value={d.name ?? b.name}
                  title="Bucket name — click to rename"
                  onChange={(e) =>
                    setDraft((s) => ({ ...s, [b.id]: { ...s[b.id], name: e.target.value } }))}
                  onBlur={() => {
                    const name = (d.name ?? b.name).trim();
                    if (name && name !== b.name) onUpdate(b.id, { ...asInput(b), name });
                    setDraft((s) => ({ ...s, [b.id]: { ...s[b.id], name: undefined } }));
                  }}
                  className="min-w-0 flex-1 rounded-[9px] border-[1.5px] border-transparent bg-[var(--stat)] px-2.5 py-[7px] text-[14.5px] font-bold [font-family:'Space_Grotesk',system-ui,sans-serif] text-[var(--strong)] outline-none focus:border-[var(--accent)]"
                />
                <ConfirmAction
                  label="✕"
                  onConfirm={() => onDelete(b.id)}
                  className="text-[15px] font-extrabold text-[var(--faint)] hover:text-[var(--danger)]"
                />
              </div>

              <div className="mb-2 flex flex-wrap items-baseline gap-2.5">
                <span className="text-[20px] font-bold [font-family:'Space_Grotesk',system-ui,sans-serif] text-[var(--strong)]">
                  {money(value)}
                </span>
                <Pill
                  bg={near ? "var(--tint)" : off > 0 ? "var(--warn-bg)" : "var(--chip)"}
                  color={near ? "var(--accent-deep)" : off > 0 ? "var(--warn-text)" : "var(--chip-text)"}
                >
                  {actual.toFixed(1)}% · target {b.target_pct}%
                  {near ? " · on target" : off > 0 ? " · overweight" : " · underweight"}
                </Pill>
              </div>

              <div className="mb-3 flex items-center gap-2">
                <span className="text-[11px] font-extrabold tracking-[.04em] text-[var(--faint)]">
                  TARGET
                </span>
                <input
                  type="number"
                  value={d.target ?? String(b.target_pct)}
                  onChange={(e) =>
                    setDraft((s) => ({ ...s, [b.id]: { ...s[b.id], target: e.target.value } }))}
                  onBlur={() => {
                    const next = Number(d.target ?? b.target_pct);
                    if (Number.isFinite(next) && next !== b.target_pct) {
                      onUpdate(b.id, { ...asInput(b), target_pct: Math.max(0, Math.min(100, next)) });
                    }
                    setDraft((s) => ({ ...s, [b.id]: { ...s[b.id], target: undefined } }));
                  }}
                  className="w-16 rounded-lg border-[1.5px] border-[var(--field-border)] bg-[var(--field)] px-[9px] py-1.5 text-[13px] font-bold text-[var(--strong)] outline-none focus:border-[var(--accent)]"
                />
                <span className="text-[12.5px] font-extrabold text-[var(--muted)]">
                  % of bucketed money
                </span>
              </div>

              {/* A crowded bucket (Growth holds ~30 names) must not set the row height
                  for its neighbours — the grid already items-starts, but the tallest card
                  still stretches the row. Cap the chips and scroll them instead. */}
              <div className="mb-3 flex max-h-[176px] flex-wrap gap-1.5 overflow-y-auto">
                {b.holding_ids.length === 0 && (
                  <span className="text-[11.5px] font-semibold text-[var(--faint)]">
                    Empty — add a holding below.
                  </span>
                )}
                {b.holding_ids.map((id) => {
                  const h = byId.get(id);
                  if (!h) return null;
                  return (
                    <span
                      key={id}
                      className="inline-flex items-center gap-1.5 rounded-full bg-[var(--chip)] px-2.5 py-[5px] text-[11.5px] font-bold text-[var(--chip-text)]"
                    >
                      {h.name}
                      <ConfirmAction
                        label="✕"
                        prompt="Remove?"
                        onConfirm={() =>
                          onUpdate(b.id, {
                            ...asInput(b),
                            holding_ids: b.holding_ids.filter((i) => i !== id),
                          })}
                        className="font-extrabold text-[var(--faint)] hover:text-[var(--danger)]"
                      />
                    </span>
                  );
                })}
              </div>

              <select
                value=""
                onChange={(e) => {
                  const id = Number(e.target.value);
                  if (!id) return;
                  onUpdate(b.id, { ...asInput(b), holding_ids: [...b.holding_ids, id] });
                }}
                className="w-full cursor-pointer rounded-[10px] border-[1.5px] border-dashed border-[var(--field-border)] bg-[var(--field)] px-[11px] py-[9px] text-[12.5px] font-bold text-[var(--muted)] outline-none"
              >
                <option value="">
                  {available.length ? "+ Add a holding…" : "Nothing left to add"}
                </option>
                {available.map((h) => (
                  <option key={h.id} value={h.id}>{h.name} — {money(h.value)}</option>
                ))}
              </select>
            </Card>
          );
        })}

        <button
          onClick={() =>
            onCreate({ name: `Bucket ${buckets.length + 1}`, target_pct: 0, holding_ids: [] })}
          className="flex min-h-[140px] items-center justify-center rounded-[16px] border-[1.5px] border-dashed border-[var(--field-border)] p-6 text-[13.5px] font-extrabold text-[var(--faint)] hover:border-[var(--accent)] hover:text-[var(--accent-deep)]"
        >
          + New bucket
        </button>
      </div>

      {loose.length > 0 && (
        <div className="mt-3.5 flex flex-col gap-2 rounded-xl border border-dashed border-[var(--border)] bg-[var(--card)] px-4 py-3">
          <span
            className="text-[11px] font-extrabold tracking-[.05em] text-[var(--faint)]"
            title="Not counted against any bucket target yet. Add them to a bucket, or exclude them from bucket maths entirely."
          >
            NOT IN ANY BUCKET · {money(loose.reduce((a, h) => a + h.value, 0))}
          </span>
          <div className="flex flex-wrap gap-[7px]">
            {loose.map((h) => (
              <span
                key={h.id}
                className="inline-flex items-center gap-2 rounded-full bg-[var(--chip)] px-[11px] py-1.5 text-[11.5px] font-bold text-[var(--chip-text)]"
              >
                {h.name} — {money(h.value)}
                <button
                  onClick={() => onSetExcluded(h, true)}
                  className="text-[10.5px] font-extrabold tracking-[.03em] text-[var(--faint)] hover:text-[var(--danger)]"
                >
                  EXCLUDE
                </button>
              </span>
            ))}
          </div>
        </div>
      )}

      {excluded.length > 0 && (
        <div className="mt-3 flex flex-col gap-2 rounded-xl border border-[var(--border)] bg-[var(--stat)] px-4 py-3 opacity-[.85]">
          <span
            className="text-[11px] font-extrabold tracking-[.05em] text-[var(--faint)]"
            title="Excluded holdings are invisible to buckets — no add lists, no unassigned tray, no bucket rebalancing. Every other tab still counts them."
          >
            EXCLUDED FROM BUCKETS · {money(excluded.reduce((a, h) => a + h.value, 0))}
          </span>
          <div className="flex flex-wrap gap-[7px]">
            {excluded.map((h) => (
              <span
                key={h.id}
                className="inline-flex items-center gap-2 rounded-full border border-dashed border-[var(--field-border)] bg-[var(--card)] px-[11px] py-1.5 text-[11.5px] font-bold text-[var(--faint)] line-through"
              >
                {h.name} — {money(h.value)}
                <button
                  onClick={() => onSetExcluded(h, false)}
                  className="text-[10.5px] font-extrabold uppercase tracking-[.03em] text-[var(--accent-deep)] no-underline"
                  style={{ textDecoration: "none" }}
                >
                  INCLUDE
                </button>
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
