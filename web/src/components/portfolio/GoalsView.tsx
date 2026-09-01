import { useMemo, useState } from "react";
import {
  goalView, money, pct,
  type Goal, type GoalInput, type Holding, type PortfolioPayload,
} from "../../lib/portfolio";
import {
  Card, ConfirmAction, Field, inputClass, Modal, Notice, selectClass,
} from "./primitives";

const STATUS_STYLE: Record<string, { bg: string; color: string }> = {
  "On track": { bg: "var(--tint)", color: "var(--accent-deep)" },
  "Slightly behind": { bg: "var(--warn-bg)", color: "var(--warn-text)" },
  Behind: { bg: "rgba(217,84,74,.14)", color: "var(--danger)" },
};

const BLANK = (year: number): GoalInput => ({
  name: "", target_amount: 1_000_000, target_year: year + 10,
  monthly_sip: 0, holding_ids: [], benchmark: "NIFTY 50 TRI",
});

function GoalForm({
  initial, rows, benchmarks, onSave, onClose, title,
}: {
  initial: GoalInput; rows: Holding[]; benchmarks: Record<string, number>;
  onSave: (g: GoalInput) => void; onClose: () => void; title: string;
}) {
  const [draft, setDraft] = useState<GoalInput>(initial);
  const [error, setError] = useState("");
  const set = (patch: Partial<GoalInput>) => setDraft((d) => ({ ...d, ...patch }));

  return (
    <Modal title={title} onClose={onClose} width={520}>
      <div className="grid grid-cols-2 gap-3">
        <Field label="GOAL" span={2}>
          <input
            className={inputClass}
            value={draft.name}
            placeholder="e.g. Retirement / House down payment"
            onChange={(e) => set({ name: e.target.value })}
          />
        </Field>
        <Field label="TARGET (₹)">
          <input
            type="number" className={inputClass} value={draft.target_amount || ""}
            onChange={(e) => set({ target_amount: Number(e.target.value) })}
          />
        </Field>
        <Field label="BY YEAR">
          <input
            type="number" className={inputClass} value={draft.target_year || ""}
            onChange={(e) => set({ target_year: Number(e.target.value) })}
          />
        </Field>
        <Field label="MONTHLY SIP (₹)">
          <input
            type="number" className={inputClass} value={draft.monthly_sip || ""}
            onChange={(e) => set({ monthly_sip: Number(e.target.value) })}
          />
        </Field>
        <Field label="BENCHMARK">
          <select
            className={selectClass} value={draft.benchmark}
            onChange={(e) => set({ benchmark: e.target.value })}
          >
            {Object.entries(benchmarks).map(([name, rate]) => (
              <option key={name} value={name}>{name} · {rate}%</option>
            ))}
          </select>
        </Field>
        <Field label="FUNDED BY" span={2}>
          <div className="max-h-[220px] overflow-y-auto rounded-[10px] border-[1.5px] border-[var(--field-border)] bg-[var(--field)] p-2">
            {rows.length === 0 && (
              <div className="p-2 text-[12px] font-semibold text-[var(--faint)]">
                No holdings to link yet.
              </div>
            )}
            {rows.map((h) => {
              const on = draft.holding_ids.includes(h.id);
              return (
                <label
                  key={h.id}
                  className="flex cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 text-[12.5px] font-semibold text-[var(--strong)] hover:bg-[var(--row-hover)]"
                >
                  <input
                    type="checkbox" checked={on}
                    onChange={() =>
                      set({
                        holding_ids: on
                          ? draft.holding_ids.filter((i) => i !== h.id)
                          : [...draft.holding_ids, h.id],
                      })}
                  />
                  <span className="flex-1 truncate">{h.name}</span>
                  <span className="text-[var(--faint)]">{money(h.value)}</span>
                </label>
              );
            })}
          </div>
        </Field>
      </div>
      {error && <div className="mt-2.5 text-[12px] font-bold text-[var(--danger)]">{error}</div>}
      <div className="mt-4 flex justify-end gap-2.5">
        <button
          onClick={onClose}
          className="rounded-[11px] border-[1.5px] border-[var(--field-border)] bg-[var(--ghost)] px-[18px] py-2.5 text-[13px] font-extrabold text-[var(--muted)]"
        >
          Cancel
        </button>
        <button
          onClick={() => {
            if (!draft.name.trim()) return setError("Give the goal a name.");
            if (!(draft.target_amount > 0)) return setError("Target must be more than zero.");
            if (draft.target_year < new Date().getFullYear()) {
              return setError("The target year is in the past.");
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

  const views = useMemo(
    () => payload.goals.map((g) => goalView(g, rows, payload.benchmarks)),
    [payload.goals, payload.benchmarks, rows],
  );

  const assigned = new Set(payload.goals.flatMap((g) => g.holding_ids));
  const unassigned = rows.filter((h) => !assigned.has(h.id));
  const unassignedValue = unassigned.reduce((a, h) => a + h.value, 0);

  const asInput = (g: Goal): GoalInput => ({
    name: g.name, target_amount: g.target_amount, target_year: g.target_year,
    monthly_sip: g.monthly_sip, holding_ids: g.holding_ids, benchmark: g.benchmark,
  });

  return (
    <div>
      {unassigned.length > 0 && (
        <div className="mb-3.5">
          <Notice tone="warn">
            {money(unassignedValue)} isn't assigned to any goal —{" "}
            {unassigned.slice(0, 4).map((h) => h.name).join(", ")}
            {unassigned.length > 4 ? ` and ${unassigned.length - 4} more` : ""}. Assign it so the
            projections below describe all your money, not part of it.
          </Notice>
        </div>
      )}

      <div className="grid gap-3.5 lg:grid-cols-2">
        {views.map((v) => {
          const style = STATUS_STYLE[v.status];
          const behindBenchmark = v.linkedXirr - v.benchmarkRate;
          return (
            <Card key={v.goal.id}>
              <div className="mb-1 flex items-center gap-2.5">
                <span className="text-[17px] font-bold [font-family:'Space_Grotesk',system-ui,sans-serif] text-[var(--strong)]">
                  {v.goal.name}
                </span>
                <span
                  title={
                    v.status === "On track"
                      ? "Today's money plus the SIP, compounded at this goal's own return, reaches the target by the target year."
                      : "The projection falls short of the target by the target year. Raise the SIP, extend the year, or accept a smaller target."
                  }
                  className="rounded-full px-[11px] py-1 text-[11px] font-extrabold"
                  style={{ background: style.bg, color: style.color }}
                >
                  {v.status}
                </span>
                <span className="ml-auto flex items-center gap-3 text-[12px] font-bold text-[var(--faint)]">
                  {v.goal.monthly_sip > 0 && <span>SIP {money(v.goal.monthly_sip)}/mo</span>}
                  <button onClick={() => setEditing(v.goal)} className="text-[var(--accent-deep)]">
                    Edit
                  </button>
                  <ConfirmAction
                    label="✕"
                    onConfirm={() => onDelete(v.goal.id)}
                    className="text-[var(--faint)] hover:text-[var(--danger)]"
                  />
                </span>
              </div>

              <div className="mb-2.5 text-[12.5px] font-semibold text-[var(--muted)]">
                Target {money(v.goal.target_amount)} by {v.goal.target_year}
              </div>

              <div className="mb-1.5 flex items-baseline gap-2">
                <span className="text-[20px] font-bold [font-family:'Space_Grotesk',system-ui,sans-serif] text-[var(--strong)]">
                  {money(v.current)}
                </span>
                <span className="text-[12.5px] font-bold text-[var(--faint)]">
                  {v.progress.toFixed(0)}% of target
                </span>
              </div>

              <div className="mb-3 h-[9px] overflow-hidden rounded-md bg-[var(--seg)]">
                <span
                  className="block h-full rounded-md bg-[var(--accent)]"
                  style={{ width: `${Math.min(v.progress, 100).toFixed(1)}%` }}
                />
              </div>

              <div className="mb-3 flex flex-wrap gap-1.5">
                {v.names.length === 0 && (
                  <span className="text-[11.5px] font-semibold text-[var(--faint)]">
                    Nothing linked — projected at the benchmark rate.
                  </span>
                )}
                {v.names.map((n) => (
                  <span
                    key={n}
                    className="rounded-full bg-[var(--chip)] px-2.5 py-1 text-[11px] font-bold text-[var(--chip-text)]"
                  >
                    {n}
                  </span>
                ))}
              </div>

              <div className="flex flex-wrap items-center gap-2.5 border-t border-[var(--divider)] pt-3">
                <select
                  value={v.goal.benchmark}
                  onChange={(e) =>
                    onUpdate(v.goal.id, { ...asInput(v.goal), benchmark: e.target.value })}
                  title="Compare this goal's own return against an index or a fixed path. The rates are stated assumptions, not measured returns."
                  className="rounded-[9px] border-[1.5px] border-[var(--field-border)] bg-[var(--field)] px-2.5 py-[7px] text-[12px] font-bold text-[var(--strong)] outline-none"
                >
                  {Object.entries(payload.benchmarks).map(([name, rate]) => (
                    <option key={name} value={name}>{name} · {rate}%</option>
                  ))}
                </select>
                <span
                  className="text-[12px] font-extrabold"
                  style={{ color: behindBenchmark >= 0 ? "var(--pos)" : "var(--danger)" }}
                  title={`This goal's holdings return ${pct(v.linkedXirr)} against the benchmark's assumed ${v.benchmarkRate}%.`}
                >
                  {v.names.length === 0 ? "—" : `${pct(behindBenchmark)} vs benchmark`}
                </span>
                <span className="ml-auto text-[11.5px] font-bold text-[var(--faint)]">
                  {v.eta === null
                    ? "Not reachable on this plan"
                    : v.eta <= v.goal.target_year
                      ? `On this plan: ${v.eta}${v.eta < v.goal.target_year ? " · early" : ""}`
                      : `On this plan: ${v.eta} · ${v.eta - v.goal.target_year}y late`}
                </span>
              </div>
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
          initial={editing === "new" ? BLANK(new Date().getFullYear()) : asInput(editing)}
          rows={rows}
          benchmarks={payload.benchmarks}
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
