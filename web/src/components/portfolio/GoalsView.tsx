import { useMemo, useState } from "react";
import {
  expandRecurring, money, pct,
  type Goal, type GoalInput, type Holding, type PortfolioPayload, type ScheduleRow,
} from "../../lib/portfolio";
import {
  Card, ConfirmAction, Field, inputClass, Modal, Notice, selectClass,
} from "./primitives";

const BLANK = (): GoalInput => ({
  name: "", allocations: [], schedule: [], inflation_pct: 6, target_amount: 0,
  target_year: 0, monthly_sip: 0, holding_ids: [], benchmark: "NIFTY 50 TRI",
  expected_return_pct: null,
});

const asInput = (g: Goal): GoalInput => ({
  name: g.name, allocations: g.allocations ?? [], schedule: g.schedule ?? [],
  inflation_pct: g.inflation_pct,
  target_amount: g.target_amount, target_year: g.target_year,
  monthly_sip: g.monthly_sip, holding_ids: g.holding_ids, benchmark: g.benchmark,
  expected_return_pct: g.expected_return_pct ?? null,
});

/** The schedule editor. A goal like "school fees, 2.5 L a year, 2027 to 2030" is described in
 * one line and expanded, rather than typed four times — and a one-off wedding is the same
 * control with the years equal. Individual rows stay editable afterwards, because real plans
 * have an odd year that differs. */
function ScheduleEditor({
  rows, onChange,
}: { rows: ScheduleRow[]; onChange: (rows: ScheduleRow[]) => void }) {
  const thisYear = new Date().getFullYear();
  const [amount, setAmount] = useState("");
  const [from, setFrom] = useState(String(thisYear + 1));
  const [to, setTo] = useState(String(thisYear + 1));

  const total = rows.reduce((a, r) => a + r.amount, 0);

  return (
    <div>
      <div className="flex flex-wrap items-end gap-2 rounded-[10px] bg-[var(--stat)] p-2.5">
        <label className="flex-1">
          <span className="text-[10.5px] font-extrabold tracking-[.04em] text-[var(--faint)]">
            AMOUNT PER YEAR (₹, today's money)
          </span>
          <input
            type="number" className={`${inputClass} mt-1 !py-1.5`} value={amount}
            placeholder="700000" onChange={(e) => setAmount(e.target.value)}
          />
        </label>
        <label className="w-20">
          <span className="text-[10.5px] font-extrabold tracking-[.04em] text-[var(--faint)]">
            FROM
          </span>
          <input type="number" className={`${inputClass} mt-1 !py-1.5`} value={from}
            onChange={(e) => setFrom(e.target.value)} />
        </label>
        <label className="w-20">
          <span className="text-[10.5px] font-extrabold tracking-[.04em] text-[var(--faint)]">
            TO
          </span>
          <input type="number" className={`${inputClass} mt-1 !py-1.5`} value={to}
            onChange={(e) => setTo(e.target.value)} />
        </label>
        <button
          onClick={() => {
            const a = Number(amount);
            const f = Number(from);
            const t = Number(to);
            if (!(a > 0) || !(f > 1999) || !(t > 1999)) return;
            onChange(expandRecurring(rows, a, f, t));
            setAmount("");
          }}
          className="rounded-[9px] bg-[var(--accent)] px-3.5 py-2 text-[12px] font-extrabold text-white"
        >
          Add
        </button>
      </div>
      <div className="mt-1.5 text-[11px] font-semibold text-[var(--faint)]">
        Same years for from and to gives a one-off. Amounts are in today's money — inflation is
        applied below.
      </div>

      {rows.length > 0 && (
        <div className="mt-2 max-h-[180px] overflow-y-auto rounded-[10px] border border-[var(--border)]">
          {rows.map((r) => (
            <div
              key={r.year}
              className="grid grid-cols-[70px_1fr_60px] items-center gap-2 border-b border-[var(--divider)] px-2.5 py-1.5 text-[12.5px] last:border-0"
            >
              <span className="font-extrabold text-[var(--strong)]">{r.year}</span>
              <input
                type="number"
                value={r.amount}
                onChange={(e) => onChange(rows.map((x) =>
                  x.year === r.year ? { ...x, amount: Number(e.target.value) } : x))}
                className="w-full rounded-md border-[1.5px] border-transparent bg-transparent px-1 py-0.5 text-right font-semibold tabular-nums text-[var(--muted)] outline-none hover:border-[var(--field-border)] focus:border-[var(--accent)] focus:bg-[var(--field)]"
              />
              <button
                onClick={() => onChange(rows.filter((x) => x.year !== r.year))}
                className="text-right text-[11px] font-extrabold text-[var(--faint)] hover:text-[var(--danger)]"
              >
                remove
              </button>
            </div>
          ))}
        </div>
      )}
      {rows.length > 0 && (
        <div className="mt-1.5 text-[12px] font-bold text-[var(--muted)]">
          {rows.length} year{rows.length === 1 ? "" : "s"} · {money(total)} in today's money
        </div>
      )}
    </div>
  );
}

function GoalForm({
  initial, rows, benchmarks, allocatedPct, onSave, onClose, title,
}: {
  initial: GoalInput; rows: Holding[]; benchmarks: Record<string, number>;
  allocatedPct: Record<string, number>;
  onSave: (g: GoalInput) => void; onClose: () => void; title: string;
}) {
  const [draft, setDraft] = useState<GoalInput>(initial);
  // Frozen at open: this goal's OWN existing share, so editing it doesn't read as a clash
  // with itself.
  const [initialPct] = useState<Record<number, number>>(() =>
    Object.fromEntries(initial.allocations.map((a) => [a.holding_id, a.pct])));
  const [error, setError] = useState("");
  const set = (patch: Partial<GoalInput>) => setDraft((d) => ({ ...d, ...patch }));

  return (
    <Modal title={title} onClose={onClose} width={620}>
      <div className="grid grid-cols-2 gap-3">
        <Field label="GOAL" span={2}>
          <input className={inputClass} value={draft.name}
            placeholder="e.g. Arya college fees / Travel"
            onChange={(e) => set({ name: e.target.value })} />
        </Field>
        <Field label="WHEN IT'S NEEDED" span={2}>
          <ScheduleEditor rows={draft.schedule} onChange={(schedule) => set({ schedule })} />
        </Field>
        <Field label="INFLATION (% a year)">
          <input type="number" className={inputClass} value={draft.inflation_pct}
            onChange={(e) => set({ inflation_pct: Number(e.target.value) })} />
        </Field>
        <Field label="MONTHLY SIP (₹)">
          <input type="number" className={inputClass} value={draft.monthly_sip || ""}
            onChange={(e) => set({ monthly_sip: Number(e.target.value) })} />
        </Field>
        <Field label="GROWS AT (% a year)">
          <input type="number" className={inputClass} placeholder="auto"
            value={draft.expected_return_pct ?? ""}
            onChange={(e) => set({
              expected_return_pct: e.target.value === "" ? null : Number(e.target.value),
            })} />
          <div className="mt-1 text-[11px] font-semibold text-[var(--faint)]">
            Your estimate. Blank = use the linked holdings&apos; own record.
          </div>
        </Field>
        <Field label="FALLBACK IF NOTHING IS LINKED">
          <select className={selectClass} value={draft.benchmark}
            onChange={(e) => set({ benchmark: e.target.value })}>
            {Object.entries(benchmarks).map(([name, rate]) => (
              <option key={name} value={name}>{name} · {rate}%</option>
            ))}
          </select>
        </Field>
        <Field label="FUNDED BY" span={2}>
          <div className="mb-1.5 text-[11.5px] font-semibold text-[var(--faint)]">
            Give each holding a percentage. A fund can back several goals — what it cannot do
            is back two of them with the same rupee, so the share left over from other goals
            is the most you can take.
          </div>
          <div className="max-h-[220px] overflow-y-auto rounded-[10px] border-[1.5px] border-[var(--field-border)] bg-[var(--field)] p-2">
            {rows.length === 0 && (
              <div className="p-2 text-[12px] font-semibold text-[var(--faint)]">
                No holdings to link yet.
              </div>
            )}
            {rows.map((h) => {
              const mine = draft.allocations.find((a) => a.holding_id === h.id);
              // What OTHER goals have taken — this goal's own share must not count against it.
              const elsewhere = Math.max(
                0, (allocatedPct[String(h.id)] ?? 0) - (initialPct[h.id] ?? 0),
              );
              const available = Math.max(0, 100 - elsewhere);
              const on = !!mine;
              return (
                <div
                  key={h.id}
                  className="flex items-center gap-2 rounded-lg px-2 py-1.5 text-[12.5px] hover:bg-[var(--row-hover)]"
                  style={{ opacity: available === 0 && !on ? 0.45 : 1 }}
                >
                  <input
                    type="checkbox" checked={on} disabled={available === 0 && !on}
                    onChange={() => set({
                      allocations: on
                        ? draft.allocations.filter((a) => a.holding_id !== h.id)
                        : [...draft.allocations, { holding_id: h.id, pct: available }],
                    })}
                  />
                  <span className="flex-1 truncate font-semibold text-[var(--strong)]">
                    {h.name}
                    <span className="ml-1.5 text-[11px] font-semibold text-[var(--faint)]">
                      {money(h.value)}
                      {elsewhere > 0 && (
                        <span className="ml-1 text-[var(--note)]">
                          · {elsewhere.toFixed(0)}% taken
                        </span>
                      )}
                    </span>
                  </span>
                  {on && (
                    <>
                      <input
                        type="number" min={1} max={available}
                        value={mine.pct}
                        onChange={(e) => {
                          const pct = Math.max(1, Math.min(available, Number(e.target.value)));
                          set({
                            allocations: draft.allocations.map((a) =>
                              a.holding_id === h.id ? { ...a, pct } : a),
                          });
                        }}
                        className="w-14 rounded-md border-[1.5px] border-[var(--field-border)] bg-[var(--card)] px-1.5 py-0.5 text-right font-bold tabular-nums text-[var(--strong)] outline-none focus:border-[var(--accent)]"
                      />
                      <span className="text-[11px] font-bold text-[var(--faint)]">
                        % · {money(h.value * mine.pct / 100)}
                      </span>
                    </>
                  )}
                </div>
              );
            })}
          </div>
        </Field>
      </div>
      {error && <div className="mt-2.5 text-[12px] font-bold text-[var(--danger)]">{error}</div>}
      <div className="mt-4 flex justify-end gap-2.5">
        <button onClick={onClose}
          className="rounded-[11px] border-[1.5px] border-[var(--field-border)] bg-[var(--ghost)] px-[18px] py-2.5 text-[13px] font-extrabold text-[var(--muted)]">
          Cancel
        </button>
        <button
          onClick={() => {
            if (!draft.name.trim()) return setError("Give the goal a name.");
            if (draft.schedule.length === 0) {
              return setError("Add at least one year — what's needed, and when.");
            }
            onSave({ ...draft, name: draft.name.trim() });
          }}
          className="rounded-[11px] bg-[var(--accent)] px-[22px] py-2.5 text-[13px] font-extrabold text-white"
        >
          Save
        </button>
      </div>
    </Modal>
  );
}

export default function GoalsView({
  rows, payload, onCreate, onUpdate, onDelete,
}: {
  rows: Holding[];
  payload: PortfolioPayload;
  onCreate: (g: GoalInput) => void;
  onUpdate: (id: number, g: GoalInput) => void;
  onDelete: (id: number) => void;
}) {
  const [editing, setEditing] = useState<Goal | "new" | null>(null);
  const [openYears, setOpenYears] = useState<number | null>(null);

  // Unassigned money is what is left AFTER partial allocations — a fund 60% committed to
  // school fees still has 40% funding nothing, and calling it "assigned" would hide it.
  const claimed = payload.goal_allocated_pct ?? {};
  const unassigned = rows.filter((h) => (claimed[String(h.id)] ?? 0) < 99.99);
  const unassignedValue = unassigned.reduce(
    (a, h) => a + h.value * (100 - Math.min(100, claimed[String(h.id)] ?? 0)) / 100, 0);

  const totals = useMemo(() => {
    const t = { today: 0, nominal: 0, pv: 0, linked: 0 };
    for (const g of payload.goals) {
      const p = g.projection;
      if (!p) continue;
      t.today += p.total_today;
      t.nominal += p.total_nominal;
      t.pv += p.pv_required;
      t.linked += g.current_value ?? 0;
    }
    return t;
  }, [payload.goals]);

  return (
    <div>
      {payload.goals.length > 0 && (
        <div className="mb-3.5">
          <Notice>
            <strong>{money(totals.today)}</strong> of goals in today's money is{" "}
            <strong>{money(totals.nominal)}</strong> by the time each one is actually needed —
            that gap is inflation, and it is the whole reason a goal can't be a single number.
            You'd need <strong>{money(totals.pv)}</strong> in hand today to be done with all of
            them; {money(totals.linked)} is linked.
          </Notice>
        </div>
      )}

      {unassigned.length > 0 && (
        <div className="mb-3.5">
          <Notice tone="warn">
            {money(unassignedValue)} isn't assigned to any goal —{" "}
            {unassigned.slice(0, 4).map((h) => h.name).join(", ")}
            {unassigned.length > 4 ? ` and ${unassigned.length - 4} more` : ""}.
          </Notice>
        </div>
      )}

      <div className="grid gap-3.5 lg:grid-cols-2">
        {payload.goals.map((g) => {
          const p = g.projection;
          const short = p?.first_shortfall_year ?? null;
          const funded = p?.funded_pct ?? null;
          const years = p?.schedule ?? [];
          const span = years.length
            ? `${years[0].year}${years.length > 1 ? `–${years[years.length - 1].year}` : ""}`
            : "—";
          const shares = new Map((g.allocations ?? []).map((a) => [a.holding_id, a.pct]));
          const linked = rows.filter((h) => shares.has(h.id))
            .sort((a, b) => b.value * (shares.get(b.id) ?? 0) - a.value * (shares.get(a.id) ?? 0));
          return (
            <Card key={g.id}>
              <div className="mb-1 flex items-center gap-2.5">
                <span className="text-[17px] font-bold [font-family:'Space_Grotesk',system-ui,sans-serif] text-[var(--strong)]">
                  {g.name}
                </span>
                <span
                  className="rounded-full px-[11px] py-1 text-[11px] font-extrabold"
                  title={short
                    ? `The corpus runs out in ${short}. A goal can be fully funded in total and still fail here, because the money is needed before it has finished compounding.`
                    : "Today's linked money plus the SIP covers every year of this schedule."}
                  style={short
                    ? { background: "rgba(217,84,74,.14)", color: "var(--danger)" }
                    : { background: "var(--tint)", color: "var(--accent-deep)" }}
                >
                  {short ? `Short from ${short}` : "On track"}
                </span>
                <span className="ml-auto flex items-center gap-3 text-[12px] font-bold text-[var(--faint)]">
                  {g.monthly_sip > 0 && <span>SIP {money(g.monthly_sip)}/mo</span>}
                  <button onClick={() => setEditing(g)} className="text-[var(--accent-deep)]">
                    Edit
                  </button>
                  <ConfirmAction label="✕" onConfirm={() => onDelete(g.id)}
                    className="text-[var(--faint)] hover:text-[var(--danger)]" />
                </span>
              </div>

              <div className="mb-3 text-[12.5px] font-semibold text-[var(--muted)]">
                {span} · {money(p?.total_today ?? 0)} in today's money ·{" "}
                <span className="text-[var(--note)]">
                  {money(p?.total_nominal ?? 0)} when needed
                </span>{" "}
                <span className="text-[var(--faint)]">
                  (at {g.inflation_pct}% inflation)
                </span>
              </div>

              <div className="mb-1.5 flex items-baseline gap-2">
                <span className="text-[20px] font-bold [font-family:'Space_Grotesk',system-ui,sans-serif] text-[var(--strong)]">
                  {money(g.current_value ?? 0)}
                </span>
                <span
                  className="text-[12.5px] font-bold text-[var(--faint)]"
                  title={`Put ${money(p?.pv_required ?? 0)} in today, growing at ${pct(g.return_pct ?? 0)} a year, and every year of this goal is covered. The bar compares what you have linked against that figure.`}
                >
                  linked{funded !== null && ` · ${funded.toFixed(0)}% funded`}
                </span>
              </div>

              <div className="mb-3 h-[9px] overflow-hidden rounded-md bg-[var(--seg)]">
                <span
                  className="block h-full rounded-md"
                  style={{
                    width: `${Math.min(funded ?? 0, 100).toFixed(1)}%`,
                    background: short ? "var(--danger)" : "var(--accent)",
                  }}
                />
              </div>

              {/* What is actually backing this goal. Without it the funded figure is a number
                  with no provenance — and it is what explains an odd growth rate, since the
                  rate is the value-weighted return of exactly these holdings. */}
              <div className="mb-3 flex flex-wrap items-center gap-1.5">
                {linked.length === 0 ? (
                  <span className="text-[11.5px] font-semibold text-[var(--faint)]">
                    Nothing linked — projected at the {g.benchmark} rate ({pct(g.benchmark_pct ?? 0)}).
                    Link holdings in Edit to use what they actually earn.
                  </span>
                ) : (
                  linked.map((h) => (
                    <span
                      key={h.id}
                      title={`${h.class_label} · ${money(h.value)} total`
                        + (h.xirr_pct !== null ? ` · returns ${pct(h.xirr_pct)}` : "")}
                      className="inline-flex items-center gap-1.5 rounded-full bg-[var(--chip)] px-2.5 py-1 text-[11px] font-bold text-[var(--chip-text)]"
                    >
                      {h.name}
                      {(shares.get(h.id) ?? 100) < 100 && (
                        <span className="text-[var(--note)]">{shares.get(h.id)}%</span>
                      )}
                      <span className="text-[var(--faint)]">
                        {money(h.value * (shares.get(h.id) ?? 100) / 100)}
                      </span>
                    </span>
                  ))
                )}
              </div>

              <div className="flex flex-wrap items-center gap-2.5 border-t border-[var(--divider)] pt-3 text-[11.5px] font-bold text-[var(--faint)]">
                <span
                  title={g.return_source === "assumed"
                    ? "The rate you set on this goal (Edit → Grows at). Clear it to fall back to the linked holdings' own record."
                    : linked.length
                      ? "Value-weighted return of the linked holdings' own record. A short or missing history drags this down — set your own rate in Edit → Grows at if you believe otherwise."
                      : `No holdings linked, so the ${g.benchmark} assumption is used. Or set your own rate in Edit → Grows at.`}
                >
                  grows at {pct(g.return_pct ?? 0)}
                  <span className="ml-1 opacity-70">
                    {g.return_source === "assumed" ? "· your estimate"
                      : g.return_source === "benchmark" ? "· benchmark" : "· holdings\u2019 record"}
                  </span>
                  {linked.length > 0 && (
                    <span className="ml-1 opacity-70">
                      · {linked.length} holding{linked.length === 1 ? "" : "s"}
                    </span>
                  )}
                  {(g.linked_monthly ?? 0) > 0 && (
                    <span
                      className="ml-1 opacity-70"
                      title="Ongoing contributions into the linked holdings (their own monthly contribution, share-weighted) — the projection counts every future instalment."
                    >
                      · +{money(g.linked_monthly ?? 0)}/mo flowing in
                    </span>
                  )}
                </span>
                <button
                  onClick={() => setOpenYears(openYears === g.id ? null : g.id)}
                  className="ml-auto text-[var(--accent-deep)]"
                >
                  {openYears === g.id ? "Hide" : "Year by year"}
                </button>
              </div>

              {openYears === g.id && p && (
                <div className="mt-2 max-h-[240px] overflow-y-auto">
                  <div className="grid grid-cols-[60px_1fr_1fr_1fr] gap-2 border-b border-[var(--border)] px-1 py-1.5 text-[10.5px] font-extrabold tracking-[.05em] text-[var(--faint)]">
                    <span>YEAR</span>
                    <span className="text-right">TODAY'S ₹</span>
                    <span className="text-right">NEEDED THEN</span>
                    <span className="text-right">LEFT AFTER</span>
                  </div>
                  {p.rows.filter((r) => r.amount_needed > 0).map((r) => (
                    <div key={r.year}
                      className="grid grid-cols-[60px_1fr_1fr_1fr] gap-2 border-b border-[var(--divider)] px-1 py-1 text-[12px]">
                      <span className="font-extrabold text-[var(--strong)]">{r.year}</span>
                      <span className="text-right tabular-nums text-[var(--faint)]">
                        {money(r.amount_today)}
                      </span>
                      <span className="text-right font-semibold tabular-nums text-[var(--note)]">
                        {money(r.amount_needed)}
                      </span>
                      <span
                        className="text-right font-bold tabular-nums"
                        style={{ color: r.short ? "var(--danger)" : "var(--muted)" }}
                      >
                        {money(r.corpus_after)}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </Card>
          );
        })}

        <button
          onClick={() => setEditing("new")}
          className="flex min-h-[140px] items-center justify-center rounded-[16px] border-[1.5px] border-dashed border-[var(--field-border)] p-6 text-[13.5px] font-extrabold text-[var(--faint)] hover:border-[var(--accent)] hover:text-[var(--accent-deep)]"
        >
          + New goal
        </button>
      </div>

      {editing && (
        <GoalForm
          title={editing === "new" ? "New goal" : "Edit goal"}
          initial={editing === "new" ? BLANK() : asInput(editing)}
          rows={rows}
          benchmarks={payload.benchmarks}
          allocatedPct={payload.goal_allocated_pct ?? {}}
          onClose={() => setEditing(null)}
          onSave={(g) => {
            if (editing === "new") onCreate(g);
            else onUpdate(editing.id, g);
            setEditing(null);
          }}
        />
      )}
    </div>
  );
}
