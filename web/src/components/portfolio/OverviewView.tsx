import { useMemo, useState } from "react";
import {
  ageLabel, avgPrice, byClass, dayLabel, dayPct, money, nativeAvg, nativeLabel, pct,
  priceLabel, signedMoney, stampLabel,
  unitsLabel,
  type AssetClassKey, type ClassAgg, type Holding, type PortfolioPayload,
} from "../../lib/portfolio";
import { Card, ConfirmAction, Dot, Pill } from "./primitives";

type SortKey = "name" | "value" | "gain" | "xirr";

/** Column widths, applied as an INLINE STYLE rather than a Tailwind `grid-cols-[…]` class.
 *
 * Two reasons, both learned the hard way. Tailwind only emits classes it can find as complete
 * literals in the source, so a template that is built up or split across a `+` lands in the
 * DOM with no rule behind it and the grid silently collapses to one column. And every track
 * needs minmax(0, …): a bare `fr` track takes its automatic minimum from its CONTENT, so one
 * long fund name widens the column past its share and shoves the row out from under the
 * headers — `truncate` never fires because the column grows instead of clipping.
 */
const COLS = [
  "minmax(0,2fr)",    // holding
  "minmax(0,.85fr)",  // units
  "minmax(0,.8fr)",   // avg
  "minmax(0,.8fr)",   // ltp
  "minmax(0,.9fr)",   // invested
  "minmax(0,.9fr)",   // current
  "minmax(0,1.15fr)", // gain
  "minmax(0,.7fr)",   // return
  "minmax(0,1fr)",    // today
  "minmax(0,.9fr)",   // actions
].join(" ");

const BASIS_HELP: Record<Holding["return_basis"], string> = {
  xirr: "Money-weighted XIRR over this holding's actual transactions.",
  annualised: "No transaction history — cost to value over the period held. "
    + "Correct only if the money went in all at once. Add the ledger for a real XIRR.",
  stated: "The figure you entered yourself — nothing derived here overrides it.",
};

function ClassCard({
  agg, active, onClick,
}: { agg: ClassAgg; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      title={`${agg.count} holding${agg.count === 1 ? "" : "s"} · click to filter the table to this class, click again to clear`}
      className="rounded-[14px] border-[1.5px] p-[13px_15px] text-left transition-colors hover:border-[var(--accent)]"
      style={{
        background: active ? "var(--tint)" : "var(--card)",
        borderColor: active ? "var(--accent)" : "var(--border)",
      }}
    >
      <div className="flex items-center gap-[7px] text-[12.5px] font-extrabold text-[var(--muted)]">
        <Dot color={agg.color} /> {agg.label}
      </div>
      <div className="mt-[5px] text-[17px] font-bold [font-family:'Space_Grotesk',system-ui,sans-serif] text-[var(--strong)]">
        {money(agg.value)}
      </div>
      <div className="mt-[3px] flex justify-between text-[11.5px] font-bold">
        <span className="text-[var(--faint)]">{agg.share.toFixed(1)}%</span>
        <span style={{ color: agg.xirr >= 0 ? "var(--pos)" : "var(--danger)" }}>
          {pct(agg.xirr)}
        </span>
      </div>
    </button>
  );
}

export default function OverviewView({
  rows, payload, onEdit, onDelete, onLedger, density,
}: {
  rows: Holding[];
  payload: PortfolioPayload;
  onEdit: (h: Holding) => void;
  onDelete: (h: Holding) => void;
  onLedger: (h: Holding) => void;
  density: "comfortable" | "compact";
}) {
  const [filter, setFilter] = useState<AssetClassKey | null>(null);
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("value");

  const classes = useMemo(
    () => byClass(rows, payload.asset_classes, payload.class_targets),
    [rows, payload.asset_classes, payload.class_targets],
  );

  const list = useMemo(() => {
    const q = search.trim().toLowerCase();
    const filtered = rows.filter(
      (h) =>
        (!filter || h.asset_class === filter) &&
        (!q || h.name.toLowerCase().includes(q) || h.class_label.toLowerCase().includes(q)),
    );
    return [...filtered].sort((a, b) => {
      if (sortKey === "name") return a.name.localeCompare(b.name);
      if (sortKey === "gain") {
        return (b.gain_pct ?? -Infinity) - (a.gain_pct ?? -Infinity);
      }
      if (sortKey === "xirr") return (b.xirr_pct ?? -Infinity) - (a.xirr_pct ?? -Infinity);
      return b.value - a.value;
    });
  }, [rows, filter, search, sortKey]);

  const rowPad = density === "compact" ? "py-[7px]" : "py-3";
  const sortColor = (k: SortKey) =>
    sortKey === k ? "var(--accent-deep)" : "var(--faint)";

  return (
    <div>
      <div className="mb-4 grid grid-cols-2 gap-2.5 sm:grid-cols-3 lg:grid-cols-5">
        {classes.filter((c) => c.count > 0 || c.value > 0).map((c) => (
          <ClassCard
            key={c.key}
            agg={c}
            active={filter === c.key}
            onClick={() => setFilter(filter === c.key ? null : c.key)}
          />
        ))}
      </div>

      <Card>
        <div className="mb-3 flex flex-wrap items-center gap-2.5">
          <span className="text-[15px] font-extrabold text-[var(--strong)]">Holdings</span>
          <Pill>{list.length}</Pill>
          {filter && (
            <button
              onClick={() => setFilter(null)}
              className="inline-flex items-center gap-1.5 rounded-full border border-[var(--tint-border)] bg-[var(--tint)] px-3 py-[5px] text-[11.5px] font-extrabold text-[var(--accent-deep)]"
            >
              {payload.asset_classes[filter].label} ✕
            </button>
          )}
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search holdings…"
            className="ml-auto w-[220px] rounded-[10px] border-[1.5px] border-[var(--field-border)] bg-[var(--field)] px-[13px] py-[9px] text-[13px] font-semibold text-[var(--strong)] outline-none focus:border-[var(--accent)]"
          />
        </div>

        <div className="overflow-x-auto">
          <div className="min-w-[1060px]">
            <div
              className="grid items-center gap-2 border-b border-[var(--border)] px-2.5 py-2 text-[11px] font-extrabold tracking-[.05em]"
              style={{ gridTemplateColumns: COLS }}
            >
              <button onClick={() => setSortKey("name")} className="text-left" style={{ color: sortColor("name") }}>
                HOLDING ↕
              </button>
              <span
                className="text-right text-[var(--faint)]"
                title="Units held. For a holding spread across brokers this is the total; for a PF balance there are no units."
              >
                UNITS
              </span>
              <span
                className="text-right text-[var(--faint)]"
                title="Cost per unit — what you paid on average. Blank where units aren't tracked (a PF balance has no per-unit price)."
              >
                AVG
              </span>
              <span
                className="text-right text-[var(--faint)]"
                title="Last traded price, or a fund's latest NAV. The date it is as of shows on the SOURCE chip."
              >
                LTP
              </span>
              <span className="text-right text-[var(--faint)]">INVESTED</span>
              <button onClick={() => setSortKey("value")} className="text-right" style={{ color: sortColor("value") }}>
                CURRENT ↕
              </button>
              <button onClick={() => setSortKey("gain")} className="text-right" style={{ color: sortColor("gain") }}>
                GAIN ↕
              </button>
              <button
                onClick={() => setSortKey("xirr")}
                title="Annualised return. Above ~12% comfortably beats inflation plus an FD; negative needs a look."
                className="text-right"
                style={{ color: sortColor("xirr") }}
              >
                RETURN ↕
              </button>
              <span
                className="text-right text-[var(--faint)]"
                title="Today's change — market-linked holdings only. A fund's NAV is a day behind by design."
              >
                TODAY
              </span>
              <span className="text-right text-[var(--faint)]">ACTIONS</span>
            </div>

            {list.length === 0 && (
              <div className="px-2.5 py-8 text-center text-[13px] font-semibold text-[var(--faint)]">
                {rows.length === 0
                  ? "Nothing tracked yet — add your first holding to start."
                  : "No holding matches that filter."}
              </div>
            )}

            {list.map((h) => {
              const auto = h.sync === "auto";
              return (
                <div
                  key={h.id}
                  className={`grid items-center gap-2 border-b border-[var(--divider)] px-2.5 ${rowPad} hover:bg-[var(--row-hover)]`}
                  style={{ gridTemplateColumns: COLS }}
                >
                  <span className="min-w-0 overflow-hidden">
                    <button
                      onClick={() => onLedger(h)}
                      title="Open this holding's transactions"
                      className="block w-full truncate text-left text-[13.5px] font-extrabold text-[var(--strong)] hover:text-[var(--accent-deep)]"
                    >
                      {h.name}
                    </button>
                    <span className="block truncate text-[11.5px] font-semibold text-[var(--faint)]">
                      {h.class_label}
                      {h.buy_month ? ` · since ${h.buy_month}` : ""}
                      {h.basis === "ledger" ? ` · ${h.txn_count} txn` : ""}
                      {h.oversold_units > 0 ? " · ⚠ sells exceed buys" : ""}
                      <span
                        className="ml-1"
                        style={{ color: auto ? "var(--accent-deep)" : "var(--faint)" }}
                        title={
                          auto
                            ? "Repriced from " + (h.sync_source === "amfi" ? "AMFI NAV"
                                : h.sync_source === "global" ? "the live US / crypto feed"
                                : "your broker")
                              + ` · last sync ${stampLabel(h.last_synced_at)}`
                              + (h.price_asof ? ` · price as of ${dayLabel(h.price_asof)}` : "")
                            : "Manual — this value holds until you edit it"
                        }
                      >
                        · {auto ? "auto" : "manual"}
                      </span>
                    </span>
                  </span>

                  <span
                    className="text-right text-[12.5px] font-semibold tabular-nums text-[var(--muted)]"
                    title={h.units_locked
                      ? "Total across every broker holding it — a sync reprices it but never rewrites this count."
                      : undefined}
                  >
                    {unitsLabel(h.units)}
                  </span>

                  <span className="text-right text-[12.5px] font-semibold tabular-nums text-[var(--muted)]">
                    {priceLabel(avgPrice(h))}
                    {nativeLabel(nativeAvg(h), h.native_currency) && (
                      <span className="block text-[10.5px] font-semibold text-[var(--faint)]">
                        {nativeLabel(nativeAvg(h), h.native_currency)}
                      </span>
                    )}
                  </span>
                  <span
                    className="text-right text-[12.5px] font-semibold tabular-nums text-[var(--strong)]"
                    title={h.price_asof ? `as of ${dayLabel(h.price_asof)}` : undefined}
                  >
                    {priceLabel(h.last_price)}
                    {nativeLabel(h.native_price, h.native_currency) && (
                      <span className="block text-[10.5px] font-semibold text-[var(--faint)]">
                        {nativeLabel(h.native_price, h.native_currency)}
                      </span>
                    )}
                  </span>
                  <span className="text-right text-[13px] font-semibold tabular-nums text-[var(--muted)]">
                    {money(h.invested)}
                  </span>
                  <span className="text-right text-[13.5px] font-bold [font-family:'Space_Grotesk',system-ui,sans-serif] text-[var(--strong)]">
                    {money(h.value)}
                  </span>
                  <span
                    className="text-right text-[12.5px] font-bold"
                    style={{ color: h.gain >= 0 ? "var(--pos)" : "var(--danger)" }}
                  >
                    {signedMoney(h.gain)}
                    {h.gain_pct !== null && ` · ${pct(h.gain_pct)}`}
                  </span>
                  <span
                    className="text-right text-[12.5px] font-bold"
                    title={BASIS_HELP[h.return_basis] + ` Held ${ageLabel(h.age_months)}.`}
                    style={{
                      color: (h.xirr_pct ?? 0) >= 0 ? "var(--strong)" : "var(--danger)",
                    }}
                  >
                    {h.xirr_pct === null ? "—" : pct(h.xirr_pct)}
                    {h.return_basis !== "xirr" && (
                      <span className="ml-1 text-[10px] text-[var(--faint)]">
                        {h.return_basis === "stated" ? "±" : "≈"}
                      </span>
                    )}
                  </span>
                  <span
                    className="text-right text-[12.5px] font-bold"
                    style={{
                      color: h.day_change > 0 ? "var(--pos)"
                        : h.day_change < 0 ? "var(--danger)" : "var(--faint)",
                    }}
                  >
                    {h.day_change === 0 ? "—" : (
                      <>
                        {signedMoney(h.day_change)}
                        {dayPct(h) !== null && (
                          <span className="ml-1 opacity-80">· {pct(dayPct(h) as number)}</span>
                        )}
                      </>
                    )}
                  </span>

                  <span className="flex justify-end gap-3 text-[12px] font-extrabold">
                    <button onClick={() => onEdit(h)} className="text-[var(--accent-deep)]">
                      Edit
                    </button>
                    <ConfirmAction
                      label="Delete"
                      onConfirm={() => onDelete(h)}
                      className="text-[var(--faint)] hover:text-[var(--danger)]"
                    />
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      </Card>
    </div>
  );
}
