import { useMemo, useState } from "react";
import {
  ageLabel, avgPrice, byClass, dayLabel, dayPct, KIND_META, KINDS, money, nativeAvg,
  nativeLabel, pct, priceLabel, signedMoney, stampLabel, unitsLabel,
  type Kind,
  type AssetClassKey, type ClassAgg, type Holding, type PortfolioPayload,
} from "../../lib/portfolio";
import { Card, Dot, Pill } from "./primitives";

type SortKey =
  | "name" | "tag" | "units" | "avg" | "ltp" | "invested" | "value" | "gain" | "gain_pct"
  | "xirr" | "today" | "today_pct";

/** What each column sorts on. Money columns default to DESCENDING because the question is
 * almost always "what's biggest" — landing on the smallest holding first would make every
 * header a two-click affair. */
const SORTERS: Record<SortKey, { pick: (h: Holding) => number | string | null; desc: boolean }> = {
  name: { pick: (h) => h.name.toLowerCase(), desc: false },
  tag: { pick: (h) => h.kind, desc: false },
  units: { pick: (h) => h.units, desc: true },
  avg: { pick: (h) => avgPrice(h), desc: true },
  ltp: { pick: (h) => h.last_price, desc: true },
  invested: { pick: (h) => h.invested, desc: true },
  value: { pick: (h) => h.value, desc: true },
  // Gain and Today each hold TWO facts — rupees and percent — and they rank differently
  // (₹328 on MANAPPURAM is +31.6%; ₹1.2 L on a big fund is +8%), so each gets its own key.
  gain: { pick: (h) => h.gain, desc: true },
  gain_pct: { pick: (h) => h.gain_pct, desc: true },
  xirr: { pick: (h) => h.xirr_pct, desc: true },
  // A zero day-change is "no market print" (a PF balance, a day-behind NAV) — it sorts
  // last either way, like the null-column rule below.
  today: { pick: (h) => (h.day_change === 0 ? null : h.day_change), desc: true },
  today_pct: { pick: (h) => dayPct(h), desc: true },
};

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
  "minmax(0,1.75fr)", // holding
  "minmax(0,.95fr)",  // tag
  "minmax(0,.8fr)",   // units
  "minmax(0,.8fr)",   // avg
  "minmax(0,.8fr)",   // ltp
  "minmax(0,.9fr)",   // invested
  "minmax(0,.9fr)",   // current
  "minmax(0,1.15fr)", // gain
  "minmax(0,.7fr)",   // return
  "minmax(0,1.05fr)", // today
].join(" ");

const BASIS_HELP: Record<Holding["return_basis"], string> = {
  xirr: "Money-weighted XIRR over this holding's actual transactions.",
  annualised: "No transaction history — cost to value over the period held. "
    + "Correct only if the money went in all at once. Add the ledger for a real XIRR.",
  stated: "The figure you entered yourself — nothing derived here overrides it.",
};

function SortHead({
  k, label, sortKey, desc, onSort, title, align = "right",
}: {
  k: SortKey; label: string; sortKey: SortKey; desc: boolean;
  onSort: (k: SortKey) => void; title?: string; align?: "left" | "right";
}) {
  const on = sortKey === k;
  return (
    <button
      onClick={() => onSort(k)}
      title={title}
      className={align === "left" ? "text-left" : "text-right"}
      style={{ color: on ? "var(--accent-deep)" : "var(--faint)" }}
    >
      {label}
      {/* The arrow shows DIRECTION on the active column and only availability elsewhere —
          a row of identical arrows tells you nothing about how the table is ordered. */}
      <span className="ml-0.5 opacity-70">{on ? (desc ? "\u2193" : "\u2191") : "\u21c5"}</span>
    </button>
  );
}

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
  rows, payload, onLedger, onSetTag, density,
}: {
  rows: Holding[];
  payload: PortfolioPayload;
  onLedger: (h: Holding) => void;
  onSetTag: (h: Holding, kind: Kind) => void;
  density: "comfortable" | "compact";
}) {
  const [filter, setFilter] = useState<AssetClassKey | null>(null);
  const [tagFilter, setTagFilter] = useState<Kind | null>(null);
  const [labelFilter, setLabelFilter] = useState<number | null>(null);
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("value");
  const [sortDesc, setSortDesc] = useState(true);

  const sortBy = (k: SortKey) => {
    if (k === sortKey) { setSortDesc((d) => !d); return; }
    setSortKey(k);
    setSortDesc(SORTERS[k].desc);
  };

  const classes = useMemo(
    () => byClass(rows, payload.asset_classes, payload.class_targets),
    [rows, payload.asset_classes, payload.class_targets],
  );

  const list = useMemo(() => {
    const q = search.trim().toLowerCase();
    const filtered = rows.filter(
      (h) =>
        (!filter || h.asset_class === filter) &&
        (!tagFilter || h.kind === tagFilter) &&
        (!labelFilter || h.tags.some((t) => t.id === labelFilter)) &&
        (!q || h.name.toLowerCase().includes(q) || h.class_label.toLowerCase().includes(q)),
    );
    const pick = SORTERS[sortKey].pick;
    return [...filtered].sort((a, b) => {
      const x = pick(a);
      const y = pick(b);
      if (typeof x === "string" || typeof y === "string") {
        const cmp = String(x ?? "").localeCompare(String(y ?? ""));
        return sortDesc ? -cmp : cmp;
      }
      // A holding with no value for this column sorts LAST either way — an unknown return is
      // not "the worst return", and floating it to the top of an ascending sort would say so.
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      return sortDesc ? y - x : x - y;
    });
  }, [rows, filter, tagFilter, labelFilter, search, sortKey, sortDesc]);

  /** Totals over exactly what is on screen — the filter and the search both bite, because a
   * summary that ignored them would contradict the rows directly above it. */
  const summary = useMemo(() => {
    const value = list.reduce((a, h) => a + h.value, 0);
    const invested = list.reduce((a, h) => a + h.invested, 0);
    const day = list.reduce((a, h) => a + h.day_change, 0);
    const priced = list.filter((h) => h.day_change !== 0);
    const priorDay = priced.reduce((a, h) => a + (h.value - h.day_change), 0);
    return {
      value, invested, gain: value - invested,
      gainPct: invested > 0 ? ((value - invested) / invested) * 100 : null,
      day, dayPct: priorDay > 0 ? (day / priorDay) * 100 : null,
    };
  }, [list]);

  const rowPad = density === "compact" ? "py-[7px]" : "py-3";
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

      {/* Tag chips: the tag is a lens as well as a label — "show me the debt" is the
          question the five-way split exists to answer. */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <span className="text-[11px] font-extrabold tracking-[.05em] text-[var(--faint)]">
          TAG
        </span>
        {KINDS.map((k) => {
          const slice = rows.filter((h) => h.kind === k);
          if (!slice.length) return null;
          const on = tagFilter === k;
          const value = slice.reduce((a, h) => a + h.value, 0);
          return (
            <button
              key={k}
              onClick={() => setTagFilter(on ? null : k)}
              className="inline-flex items-center gap-1.5 rounded-full border-[1.5px] px-3 py-1.5 text-[11.5px] font-extrabold"
              style={{
                background: on ? "var(--tint)" : "var(--card)",
                borderColor: on ? "var(--accent)" : "var(--border)",
                color: on ? "var(--strong)" : "var(--muted)",
              }}
            >
              <span className="inline-block h-2 w-2 rounded-full"
                style={{ background: KIND_META[k].color }} />
              {KIND_META[k].label}
              <span className="text-[var(--faint)]">{money(value)}</span>
            </button>
          );
        })}
      </div>

      {payload.tags.length > 0 && (
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <span className="text-[11px] font-extrabold tracking-[.05em] text-[var(--faint)]">
            LABELS
          </span>
          {payload.tags.filter((t) => (t.count ?? 0) > 0).map((t) => {
            const on = labelFilter === t.id;
            const slice = rows.filter((h) => h.tags.some((x) => x.id === t.id));
            return (
              <button
                key={t.id}
                onClick={() => setLabelFilter(on ? null : t.id)}
                className="inline-flex items-center gap-1.5 rounded-full border-[1.5px] px-3 py-1.5 text-[11.5px] font-extrabold"
                style={{
                  background: on ? `${t.color}22` : "var(--card)",
                  borderColor: on ? t.color : "var(--border)",
                  color: on ? "var(--strong)" : "var(--muted)",
                }}
              >
                <span className="inline-block h-2 w-2 rounded-full"
                  style={{ background: t.color }} />
                {t.name}
                <span className="text-[var(--faint)]">
                  {money(slice.reduce((a, h) => a + h.value, 0))}
                </span>
              </button>
            );
          })}
        </div>
      )}

      <Card>
        <div className="mb-3 flex flex-wrap items-center gap-2.5">
          <span className="text-[15px] font-extrabold text-[var(--strong)]">Holdings</span>
          <Pill>{list.length}</Pill>
          {labelFilter && (
            <button
              onClick={() => setLabelFilter(null)}
              className="inline-flex items-center gap-1.5 rounded-full border border-[var(--tint-border)] bg-[var(--tint)] px-3 py-[5px] text-[11.5px] font-extrabold text-[var(--accent-deep)]"
            >
              {payload.tags.find((t) => t.id === labelFilter)?.name} ✕
            </button>
          )}
          {tagFilter && (
            <button
              onClick={() => setTagFilter(null)}
              className="inline-flex items-center gap-1.5 rounded-full border border-[var(--tint-border)] bg-[var(--tint)] px-3 py-[5px] text-[11.5px] font-extrabold text-[var(--accent-deep)]"
            >
              {KIND_META[tagFilter].label} ✕
            </button>
          )}
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

        <div className="max-h-[78vh] overflow-auto">
          <div className="min-w-[1180px]">
            {/* Header + Total pinned as ONE sticky block: the total is a reading OF the
                table, not a row of it — it must neither sort into the body nor scroll away
                with it. Solid card background so body rows slide underneath. */}
            <div className="sticky top-0 z-[2] bg-[var(--card)]">
            <div
              className="grid items-center gap-2 border-b border-[var(--border)] px-2.5 py-2 text-[11px] font-extrabold tracking-[.05em]"
              style={{ gridTemplateColumns: COLS }}
            >
              <SortHead k="name" label="HOLDING" sortKey={sortKey} desc={sortDesc}
                onSort={sortBy} align="left" />
              <SortHead k="tag" label="TAG" sortKey={sortKey} desc={sortDesc} onSort={sortBy}
                align="left"
                title="Equity, debt, gold, real estate or crypto — the level Rebalance works at. Change it here; it also decides the tax regime." />
              <SortHead k="units" label="UNITS" sortKey={sortKey} desc={sortDesc} onSort={sortBy}
                title="Units held. For a holding spread across brokers this is the total; for a PF balance there are no units." />
              <SortHead k="avg" label="AVG" sortKey={sortKey} desc={sortDesc} onSort={sortBy}
                title="Cost per unit — what you paid on average. Blank where units aren't tracked (a PF balance has no per-unit price)." />
              <SortHead k="ltp" label="LTP" sortKey={sortKey} desc={sortDesc} onSort={sortBy}
                title="Last traded price, or a fund's latest NAV. Hover a row's figure for the date it is as of." />
              <SortHead k="invested" label="INVESTED" sortKey={sortKey} desc={sortDesc}
                onSort={sortBy} />
              <SortHead k="value" label="CURRENT" sortKey={sortKey} desc={sortDesc} onSort={sortBy} />
              <span className="flex items-baseline justify-end gap-1.5">
                <SortHead k="gain" label="GAIN" sortKey={sortKey} desc={sortDesc} onSort={sortBy}
                  title="Sort by rupees gained since buying." />
                <SortHead k="gain_pct" label="%" sortKey={sortKey} desc={sortDesc} onSort={sortBy}
                  title="Sort by percent gained since buying." />
              </span>
              <SortHead k="xirr" label="RETURN" sortKey={sortKey} desc={sortDesc} onSort={sortBy}
                title="Annualised return. Above ~12% comfortably beats inflation plus an FD; negative needs a look." />
              <span className="flex items-baseline justify-end gap-1.5">
                <SortHead k="today" label="TODAY" sortKey={sortKey} desc={sortDesc} onSort={sortBy}
                  title="Sort by today's rupee move — market-linked holdings only. A fund's NAV is a day behind by design." />
                <SortHead k="today_pct" label="%" sortKey={sortKey} desc={sortDesc} onSort={sortBy}
                  title="Sort by today's percent move." />
              </span>
            </div>

            {list.length > 0 && (
              <div
                className="grid items-center gap-2 border-b-2 border-[var(--border)] bg-[var(--stat)] px-2.5 py-2.5 text-[12.5px] font-extrabold"
                style={{ gridTemplateColumns: COLS }}
              >
                <span className="text-[var(--strong)]">
                  {filter || tagFilter || labelFilter || search ? "Filtered total" : "Total"}
                  <span className="ml-1.5 text-[11px] font-bold text-[var(--faint)]">
                    {list.length} of {rows.length}
                  </span>
                </span>
                <span /><span /><span /><span />
                <span className="text-right tabular-nums text-[var(--muted)]">
                  {money(summary.invested)}
                </span>
                <span className="text-right tabular-nums text-[var(--strong)]">
                  {money(summary.value)}
                </span>
                <span
                  className="text-right tabular-nums"
                  style={{ color: summary.gain >= 0 ? "var(--pos)" : "var(--danger)" }}
                >
                  {signedMoney(summary.gain)}
                  {summary.gainPct !== null && ` · ${pct(summary.gainPct)}`}
                </span>
                <span />
                <span
                  className="text-right tabular-nums"
                  style={{ color: summary.day > 0 ? "var(--pos)"
                    : summary.day < 0 ? "var(--danger)" : "var(--faint)" }}
                >
                  {summary.day === 0 ? "—" : signedMoney(summary.day)}
                  {summary.dayPct !== null && (
                    <span className="ml-1 opacity-80">· {pct(summary.dayPct)}</span>
                  )}
                </span>
              </div>
            )}
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
                      {h.tags.map((t) => (
                        <span
                          key={t.id}
                          className="ml-1 rounded px-1 py-px text-[10px] font-extrabold"
                          style={{ background: `${t.color}22`, color: t.color }}
                        >
                          {t.name}
                        </span>
                      ))}
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

                  <span>
                    <select
                      value={h.kind}
                      onChange={(e) => onSetTag(h, e.target.value as Kind)}
                      title="The tag this holding is rebalanced under, and taxed by."
                      className="w-full cursor-pointer rounded-[7px] border-[1.5px] border-transparent bg-transparent px-1.5 py-1 text-[11px] font-extrabold outline-none hover:border-[var(--field-border)] focus:border-[var(--accent)] focus:bg-[var(--field)]"
                      style={{ color: KIND_META[h.kind].color }}
                    >
                      {KINDS.map((k) => (
                        <option key={k} value={k}>{KIND_META[k].label}</option>
                      ))}
                    </select>
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

                </div>
              );
            })}
          </div>
        </div>
      </Card>
    </div>
  );
}
