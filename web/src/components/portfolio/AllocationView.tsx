import { useMemo } from "react";
import {
  byClass, KIND_META, KINDS, money, type Holding, type Kind, type PortfolioPayload,
} from "../../lib/portfolio";
import { Card, Dot } from "./primitives";

const KIND_PILL: Record<Kind, { bg: string; color: string }> = {
  equity: { bg: "var(--tint)", color: "var(--accent-deep)" },
  debt: { bg: "var(--warn-bg)", color: "var(--warn-text)" },
  gold: { bg: "var(--warn-bg)", color: "var(--note)" },
  realestate: { bg: "var(--chip)", color: "var(--chip-text)" },
  crypto: { bg: "var(--opt-bg)", color: "var(--opt-text)" },
};

/** One stacked bar, segments proportional to value. A 2px surface gap between fills keeps
 * adjacent classes readable when two are close in hue. */
function StackedBar({ segments }: { segments: { width: number; color: string; label: string }[] }) {
  return (
    <div className="flex h-3.5 overflow-hidden rounded-lg">
      {segments.filter((s) => s.width > 0).map((s, i) => (
        <span
          key={s.label}
          title={`${s.label} · ${s.width.toFixed(1)}%`}
          className="block h-full"
          style={{
            width: `${s.width}%`,
            background: s.color,
            marginLeft: i === 0 ? 0 : 2,
          }}
        />
      ))}
    </div>
  );
}

export default function AllocationView({
  rows, payload,
}: { rows: Holding[]; payload: PortfolioPayload }) {
  const classes = useMemo(
    () => byClass(rows, payload.asset_classes, payload.class_targets)
      .filter((c) => c.value > 0)
      .sort((a, b) => b.value - a.value),
    [rows, payload.asset_classes, payload.class_targets],
  );
  const total = classes.reduce((a, c) => a + c.value, 0);

  const byKind = useMemo(() => {
    const acc = Object.fromEntries(KINDS.map((k) => [k, 0])) as Record<Kind, number>;
    for (const h of rows) acc[h.kind] = (acc[h.kind] ?? 0) + h.value;
    return acc;
  }, [rows]);

  // Donut as a conic gradient — no charting library for a ten-slice pie, and the segments
  // land on exact percentages rather than a renderer's rounding.
  const donut = useMemo(() => {
    let acc = 0;
    const stops = classes.map((c) => {
      const share = total > 0 ? (c.value / total) * 100 : 0;
      const stop = `${c.color} ${acc.toFixed(2)}% ${(acc + share).toFixed(2)}%`;
      acc += share;
      return stop;
    });
    return stops.length ? `conic-gradient(${stops.join(", ")})` : "var(--seg)";
  }, [classes, total]);

  if (total <= 0) {
    return (
      <Card>
        <div className="py-10 text-center text-[13px] font-semibold text-[var(--faint)]">
          Nothing to allocate yet — add a holding first.
        </div>
      </Card>
    );
  }

  const top = classes[0];

  return (
    <div className="grid items-start gap-3.5 lg:grid-cols-[1.2fr_1fr]">
      <Card pad="p-5">
        <div
          className="mb-3.5 text-[15px] font-extrabold text-[var(--strong)]"
          title="Where your money sits today. Compare each class's share against its target — the gaps feed the Rebalance tab."
        >
          By asset class
        </div>
        <div className="mb-4">
          <StackedBar
            segments={classes.map((c) => ({
              width: (c.value / total) * 100, color: c.color, label: c.label,
            }))}
          />
        </div>
        {classes.map((c) => {
          const pill = KIND_PILL[c.kind];
          return (
            <div
              key={c.key}
              className="grid grid-cols-[16px_1.6fr_1fr_.7fr_.9fr] items-center gap-2.5 border-b border-[var(--divider)] px-0.5 py-[9px] text-[13px]"
            >
              <Dot color={c.color} size={10} />
              <span className="truncate font-extrabold text-[var(--strong)]">{c.label}</span>
              <span className="text-right font-semibold text-[var(--muted)]">{money(c.value)}</span>
              <span className="text-right font-extrabold text-[var(--strong)]">
                {((c.value / total) * 100).toFixed(1)}%
              </span>
              <span className="text-right">
                <span
                  className="rounded-full px-[9px] py-[3px] text-[10.5px] font-extrabold"
                  style={{ background: pill.bg, color: pill.color }}
                >
                  {KIND_META[c.kind].label}
                </span>
              </span>
            </div>
          );
        })}
      </Card>

      <div className="flex flex-col gap-3.5">
        <Card pad="p-5" className="flex items-center gap-[22px]">
          <div className="relative h-[150px] w-[150px] shrink-0">
            <div className="h-[150px] w-[150px] rounded-full" style={{ background: donut }} />
            <div className="absolute inset-[26px] flex flex-col items-center justify-center rounded-full bg-[var(--card)]">
              <span className="text-[10px] font-extrabold text-[var(--faint)]">TOTAL</span>
              <span className="text-[17px] font-bold [font-family:'Space_Grotesk',system-ui,sans-serif] text-[var(--strong)]">
                {money(total)}
              </span>
            </div>
          </div>
          <div className="text-[12.5px] font-semibold leading-relaxed text-[var(--muted)]">
            Largest slice:{" "}
            <strong className="text-[var(--strong)]">
              {top.label} at {((top.value / total) * 100).toFixed(1)}%
            </strong>
            . Keeping any single class under ~30% is the usual guard against one market
            deciding your net worth.
          </div>
        </Card>

        <Card pad="p-5">
          <div
            className="mb-3 text-[15px] font-extrabold text-[var(--strong)]"
            title="Your real risk mix, at the level you rebalance. Drift from these targets changes your exposure even in a month you changed nothing."
          >
            By tag
          </div>
          <div className="mb-3.5">
            <StackedBar
              segments={KINDS.map((k) => ({
                width: (byKind[k] / total) * 100,
                color: KIND_META[k].color,
                label: KIND_META[k].label,
              }))}
            />
          </div>
          <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3 lg:grid-cols-5">
            {KINDS.map((k) => {
              const share = (byKind[k] / total) * 100;
              const target = payload.kind_targets[k] ?? 0;
              const off = share - target;
              return (
                <div key={k} className="rounded-xl bg-[var(--stat)] px-[13px] py-[11px]">
                  <div className="flex items-center gap-1.5 text-[11px] font-extrabold text-[var(--faint)]">
                    <Dot color={KIND_META[k].color} size={8} radius={2} />
                    {KIND_META[k].label}
                  </div>
                  <div className="mt-1 text-[16px] font-bold [font-family:'Space_Grotesk',system-ui,sans-serif] text-[var(--strong)]">
                    {share.toFixed(1)}%
                  </div>
                  <div className="text-[11px] font-bold text-[var(--faint)]">
                    {money(byKind[k])} · target {target}%
                    {Math.abs(off) >= 1 && (
                      <span style={{ color: off > 0 ? "var(--note)" : "var(--accent-deep)" }}>
                        {" "}({off > 0 ? "+" : ""}{off.toFixed(0)})
                      </span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </Card>
      </div>
    </div>
  );
}
