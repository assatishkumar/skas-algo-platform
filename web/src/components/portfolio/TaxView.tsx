import { useMemo, useState } from "react";
import {
  ageLabel, dayLabel, money, signedMoney,
  type Holding, type PortfolioPayload,
} from "../../lib/portfolio";
import { Card, Kpi, Segments } from "./primitives";

const GRID = "grid-cols-[2fr_.9fr_.7fr_1.1fr_.8fr_1.6fr]";

function regimePill(rate: number) {
  if (rate === 0) return { bg: "var(--tint)", color: "var(--accent-deep)" };
  if (rate >= 0.3) return { bg: "var(--warn-bg)", color: "var(--warn-text)" };
  return { bg: "var(--chip)", color: "var(--chip-text)" };
}

/** Indian FY: 1 April to 31 March. A "this year" realized figure keyed on the calendar year
 * would be wrong for nine months of every twelve. */
function financialYear(iso: string): string {
  const d = new Date(`${iso}T00:00:00`);
  const start = d.getMonth() >= 3 ? d.getFullYear() : d.getFullYear() - 1;
  return `FY${String(start + 1).slice(2)}`;
}

function currentFY(today = new Date()): string {
  const start = today.getMonth() >= 3 ? today.getFullYear() : today.getFullYear() - 1;
  return `FY${String(start + 1).slice(2)}`;
}

export default function TaxView({
  rows, payload,
}: { rows: Holding[]; payload: PortfolioPayload }) {
  const [mode, setMode] = useState<"holding" | "lot">("holding");

  const agg = useMemo(() => {
    const acc = { ltcg: 0, stcg: 0, harvestable: 0 };
    for (const h of rows) {
      acc.ltcg += h.tax.ltcg;
      acc.stcg += h.tax.stcg;
      acc.harvestable += h.tax.harvestable;
    }
    return acc;
  }, [rows]);

  const realized = useMemo(() => {
    const fy = currentFY();
    const all = rows.flatMap((h) => h.disposals.map((d) => ({ ...d, name: h.name })));
    const thisFy = all.filter((d) => financialYear(d.sold_on) === fy);
    return {
      fy,
      rows: [...thisFy].sort((a, b) => b.sold_on.localeCompare(a.sold_on)),
      gain: thisFy.reduce((a, d) => a + d.gain, 0),
      allTime: all.reduce((a, d) => a + d.gain, 0),
      count: all.length,
    };
  }, [rows]);

  const lotRows = useMemo(
    () =>
      rows.flatMap((h) =>
        h.tax.lots
          .filter((l) => l.units > 0)
          .map((l) => ({ ...l, name: h.name, holdingId: h.id })),
      ).sort((a, b) => b.gain - a.gain),
    [rows],
  );

  return (
    <div>
      <div className="mb-3.5 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Kpi
          label="UNREALIZED LTCG" value={money(agg.ltcg)} valueColor="var(--pos)"
          sub="long-term gains on paper"
          help="Gains already past the long-term holding bar — taxed at the lower 12.5% rate if you sold today."
        />
        <Kpi
          label="STCG / HIGHER-RATE" value={money(agg.stcg)}
          sub="short-term, slab and flat-30 gains"
          help="Gains that would be taxed at 20-30% today. Waiting out the holding period often pays more than the trade does."
        />
        <Kpi
          label="EST. TAX IF SOLD TODAY" value={money(payload.tax.estimate_total)}
          valueColor="var(--danger)"
          sub={`after the ${money(payload.tax.equity_ltcg_exemption)} equity exemption`}
          help="Rough bill for liquidating everything now, with the equity LTCG exemption applied once across the whole portfolio. A reason to rebalance with fresh money instead of selling."
        />
        <Kpi
          label="HARVESTABLE LOSSES" value={money(agg.harvestable)} valueColor="var(--note)"
          sub="can offset taxable gains"
          help="Booked losses can offset gains and cut the bill. Crypto losses are excluded — VDA losses cannot be set off against anything."
        />
      </div>

      <Card>
        <div className="mb-3 flex flex-wrap items-center gap-3">
          <span className="text-[15px] font-extrabold text-[var(--strong)]">
            {mode === "holding" ? "If you sold today" : "Open lots"}
          </span>
          <Segments
            size="sm" value={mode} onChange={setMode}
            options={[
              { value: "holding", label: "By holding" },
              { value: "lot", label: "By lot" },
            ]}
          />
          {mode === "lot" && (
            <span className="text-[11.5px] font-semibold text-[var(--faint)]">
              One position can sit on both sides of the 12-month line — this is the view that
              shows it.
            </span>
          )}
        </div>

        <div className="overflow-x-auto">
          <div className="min-w-[820px]">
            <div className={`grid ${GRID} gap-2.5 border-b border-[var(--border)] px-0.5 py-2 text-[11px] font-extrabold tracking-[.05em] text-[var(--faint)]`}>
              <span>{mode === "holding" ? "HOLDING" : "LOT"}</span>
              <span className="text-right">UNREALIZED</span>
              <span className="text-right">HELD</span>
              <span>REGIME</span>
              <span className="text-right">EST. TAX</span>
              <span>NOTE</span>
            </div>

            {mode === "holding" && rows.map((h) => {
              // A holding whose lots straddle the line has no single regime — say so rather
              // than picking one of them and looking authoritative.
              const regimes = [...new Set(h.tax.lots.filter((l) => l.units > 0).map((l) => l.regime))];
              const label = regimes.length > 1 ? `${regimes.length} regimes` : regimes[0] ?? "—";
              const rate = h.tax.lots[0]?.rate ?? 0;
              const pill = regimePill(regimes.length > 1 ? 0.2 : rate);
              return (
                <div
                  key={h.id}
                  className={`grid ${GRID} items-center gap-2.5 border-b border-[var(--divider)] px-0.5 py-2.5 text-[12.5px]`}
                >
                  <span className="truncate font-extrabold text-[var(--strong)]">{h.name}</span>
                  <span
                    className="text-right font-bold tabular-nums"
                    style={{ color: h.gain >= 0 ? "var(--pos)" : "var(--danger)" }}
                  >
                    {signedMoney(h.gain)}
                  </span>
                  <span className="text-right font-semibold text-[var(--muted)]">
                    {ageLabel(h.age_months)}
                  </span>
                  <span>
                    <span
                      className="rounded-full px-2.5 py-1 text-[10.5px] font-extrabold"
                      style={{ background: pill.bg, color: pill.color }}
                    >
                      {label}
                    </span>
                  </span>
                  <span className="text-right font-extrabold tabular-nums text-[var(--strong)]">
                    {h.tax.estimate > 0 ? money(h.tax.estimate) : "—"}
                  </span>
                  <span className="text-[11.5px] font-semibold text-[var(--faint)]">
                    {regimes.length > 1
                      ? "Lots on both sides of the holding-period line — see By lot"
                      : h.tax.lots[0]?.note ?? ""}
                  </span>
                </div>
              );
            })}

            {mode === "lot" && lotRows.map((l, i) => {
              const pill = regimePill(l.rate);
              return (
                <div
                  key={`${l.holdingId}-${i}`}
                  className={`grid ${GRID} items-center gap-2.5 border-b border-[var(--divider)] px-0.5 py-2.5 text-[12.5px]`}
                >
                  <span className="truncate">
                    <span className="font-extrabold text-[var(--strong)]">{l.name}</span>
                    <span className="ml-2 text-[11px] font-semibold text-[var(--faint)]">
                      {l.units.toLocaleString("en-IN")} @ {dayLabel(l.on_date)}
                    </span>
                  </span>
                  <span
                    className="text-right font-bold tabular-nums"
                    style={{ color: l.gain >= 0 ? "var(--pos)" : "var(--danger)" }}
                  >
                    {signedMoney(l.gain)}
                  </span>
                  <span className="text-right font-semibold text-[var(--muted)]">
                    {ageLabel(l.months_held)}
                  </span>
                  <span>
                    <span
                      className="rounded-full px-2.5 py-1 text-[10.5px] font-extrabold"
                      style={{ background: pill.bg, color: pill.color }}
                    >
                      {l.regime}
                    </span>
                  </span>
                  <span className="text-right font-extrabold tabular-nums text-[var(--strong)]">
                    {l.gain > 0 && l.rate > 0 ? money(l.gain * l.rate) : "—"}
                  </span>
                  <span className="text-[11.5px] font-semibold text-[var(--faint)]">{l.note}</span>
                </div>
              );
            })}
          </div>
        </div>

        <div className="pt-3 text-[11.5px] font-semibold text-[var(--faint)]">
          Estimates only — assumes a 30% slab, FY26 rules, and the{" "}
          {money(payload.tax.equity_ltcg_exemption)} equity LTCG exemption applied once across
          holdings. Not tax advice.
        </div>
      </Card>

      {realized.count > 0 && (
        <Card className="mt-3.5">
          <div className="mb-3 flex flex-wrap items-baseline gap-3">
            <span className="text-[15px] font-extrabold text-[var(--strong)]">
              Already realized
            </span>
            <span
              className="text-[13px] font-extrabold"
              style={{ color: realized.gain >= 0 ? "var(--pos)" : "var(--danger)" }}
            >
              {signedMoney(realized.gain)} in {realized.fy}
            </span>
            <span className="text-[12px] font-semibold text-[var(--faint)]">
              · {signedMoney(realized.allTime)} across all {realized.count} disposals
            </span>
          </div>
          {realized.rows.length === 0 ? (
            <div className="text-[12.5px] font-semibold text-[var(--faint)]">
              Nothing sold in {realized.fy} — the figure above is history from earlier years.
            </div>
          ) : (
            <div className="overflow-x-auto">
              <div className="min-w-[640px]">
                <div className="grid grid-cols-[2fr_.8fr_.8fr_1fr_1.2fr] gap-2.5 border-b border-[var(--border)] px-0.5 py-2 text-[11px] font-extrabold tracking-[.05em] text-[var(--faint)]">
                  <span>HOLDING</span>
                  <span className="text-right">SOLD</span>
                  <span className="text-right">UNITS</span>
                  <span className="text-right">GAIN</span>
                  <span>REGIME AT SALE</span>
                </div>
                {realized.rows.map((d, i) => (
                  <div
                    key={i}
                    className="grid grid-cols-[2fr_.8fr_.8fr_1fr_1.2fr] items-center gap-2.5 border-b border-[var(--divider)] px-0.5 py-2 text-[12.5px]"
                  >
                    <span className="truncate font-extrabold text-[var(--strong)]">{d.name}</span>
                    <span className="text-right font-semibold text-[var(--muted)]">
                      {dayLabel(d.sold_on)}
                    </span>
                    <span className="text-right font-semibold tabular-nums text-[var(--muted)]">
                      {d.units.toLocaleString("en-IN")}
                    </span>
                    <span
                      className="text-right font-bold tabular-nums"
                      style={{ color: d.gain >= 0 ? "var(--pos)" : "var(--danger)" }}
                    >
                      {signedMoney(d.gain)}
                    </span>
                    <span className="text-[11.5px] font-semibold text-[var(--faint)]">
                      {d.regime} · held {ageLabel(d.months_held)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
