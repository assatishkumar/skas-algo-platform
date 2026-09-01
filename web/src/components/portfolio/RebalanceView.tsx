import { useMemo, useState } from "react";
import {
  byClass, money, rebalanceMoves, type Holding, type PortfolioPayload,
} from "../../lib/portfolio";
import { Card, Dot, Notice } from "./primitives";

/** Percentage points of drift that count as "on target". Below this, the cost of trading
 * exceeds the benefit of being exactly right. */
const DEAD_BAND = 0.8;

export default function RebalanceView({
  rows, payload, onSaveTargets,
}: {
  rows: Holding[];
  payload: PortfolioPayload;
  onSaveTargets: (targets: Record<string, number>) => void;
}) {
  const [draft, setDraft] = useState<Record<string, string>>({});

  const classes = useMemo(
    () => byClass(rows, payload.asset_classes, payload.class_targets),
    [rows, payload.asset_classes, payload.class_targets],
  );
  const total = classes.reduce((a, c) => a + c.value, 0);
  const shown = classes.filter((c) => c.value > 0 || c.target > 0);
  const targetSum = shown.reduce((a, c) => a + c.target, 0);

  const maxDrift = Math.max(...shown.map((c) => Math.abs(c.drift)), 1);

  const moves = useMemo(
    () =>
      rebalanceMoves(
        shown.map((c) => ({ name: c.label, excess: (c.drift / 100) * total })),
        (DEAD_BAND / 100) * total,
      ),
    [shown, total],
  );

  if (total <= 0) {
    return (
      <Card>
        <div className="py-10 text-center text-[13px] font-semibold text-[var(--faint)]">
          Nothing to rebalance yet — add a holding first.
        </div>
      </Card>
    );
  }

  const adds = shown.filter((c) => c.drift < -DEAD_BAND).sort((a, b) => a.drift - b.drift);
  const trims = shown.filter((c) => c.drift > DEAD_BAND).sort((a, b) => b.drift - a.drift);

  return (
    <div>
      <div className="mb-3.5">
        {adds.length === 0 && trims.length === 0 ? (
          <Notice>
            Every class is within {DEAD_BAND}% of its target — nothing worth trading. Drift this
            small costs more in brokerage and tax than it fixes.
          </Notice>
        ) : (
          <Notice>
            To get back to target:{" "}
            {adds.slice(0, 2).map((c) =>
              `add ${money(Math.abs(c.drift / 100) * total)} to ${c.label}`).join(", ")}
            {trims.length > 0 && adds.length > 0 ? "; " : ""}
            {trims.slice(0, 1).map((c) =>
              `trim ${money((c.drift / 100) * total)} from ${c.label}`).join(", ")}
            . Route fresh SIPs to the underweights first — no tax that way.
          </Notice>
        )}
      </div>

      {Math.abs(targetSum - 100) > 0.5 && (
        <div className="mb-3.5">
          <Notice tone="warn">
            Class targets sum to {targetSum.toFixed(0)}%, not 100% — the drift column is
            measured against a whole that doesn't add up. Adjust the targets below.
          </Notice>
        </div>
      )}

      <Card>
        <div className="overflow-x-auto">
          <div className="min-w-[720px]">
            <div className="grid grid-cols-[1.4fr_.7fr_.6fr_2fr_1.1fr] gap-2.5 border-b border-[var(--border)] px-0.5 py-2 text-[11px] font-extrabold tracking-[.05em] text-[var(--faint)]">
              <span>ASSET CLASS</span>
              <span className="text-right" title="Editable — your intended share of the whole portfolio.">
                TARGET
              </span>
              <span className="text-right">ACTUAL</span>
              <span
                className="text-center"
                title={`Bar left of centre = underweight (add); right = overweight (trim). Within ±${DEAD_BAND}% counts as on target.`}
              >
                DRIFT
              </span>
              <span className="text-right">SUGGESTION</span>
            </div>

            {shown.map((c) => {
              const hold = Math.abs(c.drift) < DEAD_BAND;
              const width = (Math.abs(c.drift) / maxDrift) * 46;
              const amount = (Math.abs(c.drift) / 100) * total;
              return (
                <div
                  key={c.key}
                  className="grid grid-cols-[1.4fr_.7fr_.6fr_2fr_1.1fr] items-center gap-2.5 border-b border-[var(--divider)] px-0.5 py-2.5 text-[13px]"
                >
                  <span className="flex items-center gap-2 font-extrabold text-[var(--strong)]">
                    <Dot color={c.color} /> {c.label}
                  </span>
                  <span className="text-right">
                    <input
                      type="number"
                      value={draft[c.key] ?? String(c.target)}
                      onChange={(e) => setDraft((s) => ({ ...s, [c.key]: e.target.value }))}
                      onBlur={() => {
                        const next = Number(draft[c.key] ?? c.target);
                        if (Number.isFinite(next) && next !== c.target) {
                          onSaveTargets({
                            ...payload.class_targets,
                            [c.key]: Math.max(0, Math.min(100, next)),
                          });
                        }
                        setDraft((s) => {
                          const { [c.key]: _drop, ...rest } = s;
                          return rest;
                        });
                      }}
                      className="w-14 rounded-md border-[1.5px] border-transparent bg-transparent px-1 py-0.5 text-right font-semibold tabular-nums text-[var(--muted)] outline-none hover:border-[var(--field-border)] focus:border-[var(--accent)] focus:bg-[var(--field)]"
                    />
                    <span className="text-[var(--faint)]">%</span>
                  </span>
                  <span className="text-right font-extrabold tabular-nums text-[var(--strong)]">
                    {c.share.toFixed(1)}%
                  </span>
                  <span className="relative block h-3">
                    <span className="absolute -bottom-[3px] -top-[3px] left-1/2 w-[1.5px] bg-[var(--faint)]" />
                    <span
                      className="absolute top-[1.5px] h-[9px] rounded-[5px]"
                      style={{
                        left: c.drift < 0 ? `${50 - width}%` : "50%",
                        width: `${width}%`,
                        background: hold ? "var(--faint)"
                          : c.drift < 0 ? "var(--accent)" : "var(--note)",
                      }}
                    />
                  </span>
                  <span
                    className="text-right font-extrabold"
                    style={{
                      color: hold ? "var(--faint)"
                        : c.drift < 0 ? "var(--accent-deep)" : "var(--note)",
                    }}
                  >
                    {hold ? "Hold" : c.drift < 0 ? `Add ${money(amount)}` : `Trim ${money(amount)}`}
                  </span>
                </div>
              );
            })}
          </div>
        </div>

        {moves.length > 0 && (
          <div className="mt-4 flex flex-col gap-1.5 rounded-xl border border-[var(--tint-border)] bg-[var(--tint)] px-[18px] py-3.5">
            <span className="text-[13.5px] font-extrabold text-[var(--ft)]">
              If you'd rather move existing money than add new
            </span>
            {moves.map((m) => (
              <span key={`${m.from}->${m.to}`} className="text-[13px] font-bold text-[var(--ft)]">
                → Move {money(m.amount)} from {m.from} to {m.to}
              </span>
            ))}
            <span className="mt-1 text-[11.5px] font-semibold text-[var(--ft)] opacity-80">
              Selling realises gains — check the Tax tab before you do this rather than
              directing the next few SIPs.
            </span>
          </div>
        )}
      </Card>
    </div>
  );
}
