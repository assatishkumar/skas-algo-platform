/** Portfolio screen — types, formatting, and the derivations the server deliberately doesn't do.
 *
 * The split with the backend: anything needing a LEDGER or a SOLVER (FIFO lots, XIRR, per-lot
 * tax) arrives already computed, one record per holding. Everything here is arithmetic over
 * those records — allocation, drift, bucket targets, goal progress — so typing in a target box
 * updates every tile without a round trip.
 *
 * Dates: trade dates and NAV dates are DAILY figures with no time of day in the source (AMFI
 * publishes a date; a contract note carries a trade date). They are shown as dates, the same
 * exemption the daily EOD engine has. Everything that does have a clock — a sync, a fetch —
 * carries HH:MM.
 */

export type AssetClassKey =
  | "stk" | "etf" | "mf" | "us" | "btc" | "cash" | "fd" | "ppf" | "epf"
  | "gold" | "re" | "property";

/** The tag a holding carries, and the level rebalancing happens at — coarser than asset
 *  class on purpose: real risk is "how much equity, how much debt", not "how many mid-cap
 *  funds". Crypto is its own tag; averaging it into equity hides the position most worth
 *  stating. */
export type Kind = "equity" | "debt" | "gold" | "realestate" | "crypto";
export const KINDS: Kind[] = ["equity", "debt", "gold", "realestate", "crypto"];

export interface TaxLot {
  on_date: string;
  units: number;
  cost: number;
  value: number;
  gain: number;
  months_held: number;
  regime: string;
  rate: number;
  note: string;
  equity_ltcg: boolean;
}

export interface Disposal {
  sold_on: string;
  bought_on: string;
  units: number;
  cost: number;
  proceeds: number;
  gain: number;
  months_held: number;
  regime: string;
}

export interface Holding {
  id: number;
  name: string;
  asset_class: AssetClassKey;
  class_label: string;
  kind: Kind;
  kind_override: Kind | null;
  units: number | null;
  invested: number;
  value: number;
  gain: number;
  gain_pct: number | null;
  day_change: number;
  xirr_pct: number | null;
  /** How the return was arrived at — the UI says so rather than implying one method. */
  return_basis: "xirr" | "annualised" | "stated";
  buy_month: string;
  age_months: number;
  sync: "auto" | "manual";
  sync_source: "broker" | "amfi" | "global" | null;
  sync_ref: string | null;
  broker_account_id: number | null;
  last_price: number | null;
  price_asof: string | null;
  /** The sleeve's own currency, where it isn't rupees — a US position is read in USD. */
  native_currency: string | null;
  native_price: number | null;
  native_invested: number | null;
  last_synced_at: string | null;
  /** True when units are a total across several brokers — a sync reprices, never rewrites. */
  units_locked: boolean;
  /** Per-source unit breakdown, e.g. {"static": 7757, "account:3": 24}. A sync rewrites
   *  only the slice for the account it read, so a live account can grow inside an
   *  aggregate without wiping the statement-loaded ones. */
  broker_units: Record<string, number>;
  excluded_from_buckets: boolean;
  /** Expected annual distribution as a % of value. Null = unknown, never assumed zero. */
  dividend_yield_pct: number | null;
  /** Fixed deposits: the value is ACCRUED from these, not typed. */
  interest_rate_pct: number | null;
  maturity_date: string | null;
  /** Accounts still being paid into — a PPF at ₹12,500 a month. */
  monthly_contribution: number | null;
  /** Rent, or any payout stated in rupees rather than as a yield. */
  monthly_income: number | null;
  /** User-defined labels. Many-to-many, and separate from the single-valued asset
   *  class so rebalancing counts each rupee exactly once. */
  tags: TagRow[];
  note: string | null;
  basis: "ledger" | "summary";
  txn_count: number;
  oversold_units: number;
  realized: number;
  disposals: Disposal[];
  tax: {
    ltcg: number; stcg: number; exempt: number; harvestable: number;
    estimate: number; lots: TaxLot[];
  };
}

export interface Bucket {
  id: number;
  name: string;
  target_pct: number;
  holding_ids: number[];
  sort_order: number;
}

export interface ScheduleRow {
  year: number;
  /** In TODAY's rupees — the only form a person reliably knows a cost in. */
  amount: number;
}

export interface GoalProjection {
  schedule: ScheduleRow[];
  rows: {
    year: number; amount_today: number; amount_needed: number;
    corpus_after: number; short: boolean;
  }[];
  total_today: number;
  /** What the same stream actually costs, once each year is inflated to when it is needed. */
  total_nominal: number;
  /** What you would need in hand TODAY to be done with it, at the expected return. */
  pv_required: number;
  funded_pct: number | null;
  /** The year the money runs out — a goal can be "fully funded" in total and still fail here. */
  first_shortfall_year: number | null;
  final_corpus: number;
  years: number;
}

export interface Allocation {
  holding_id: number;
  /** Share of that holding funding this goal. Several goals may split one. */
  pct: number;
}

export interface Goal {
  id: number;
  name: string;
  allocations: Allocation[];
  schedule: ScheduleRow[];
  inflation_pct: number;
  target_amount: number;
  target_year: number;
  monthly_sip: number;
  holding_ids: number[];
  benchmark: string;
  sort_order: number;
  current_value?: number;
  return_pct?: number;
  benchmark_pct?: number;
  projection?: GoalProjection;
}

export interface GrowthSeries {
  dates: string[];
  total: number[];
  invested: number[];
  by_holding: Record<string, (number | null)[]>;
  points: number;
  enough_for_trend: boolean;
  since: string | null;
}

export interface PortfolioPayload {
  holdings: Holding[];
  buckets: Bucket[];
  goals: Goal[];
  /** {holding_id: % already claimed by goals} — caps what a new goal may take. */
  goal_allocated_pct: Record<string, number>;
  asset_classes: Record<AssetClassKey, { label: string; kind: Kind; color: string }>;
  class_targets: Record<string, number>;
  kind_targets: Record<Kind, number>;
  kinds: { key: Kind; label: string; color: string }[];
  benchmarks: Record<string, number>;
  tax: { estimate_total: number; equity_ltcg_exemption: number };
  growth: GrowthSeries;
  income: IncomeView;
  tags: TagRow[];
  dividends?: DividendRow[];
  server_time: string;
}

export interface SyncReport {
  updated: string[];
  unchanged: string[];
  issues: { holding: string; reason: string; severity?: string }[];
  discovered: {
    symbol: string; units: number; avg_price: number;
    broker: string; broker_account_id: number;
  }[];
  synced_at: string;
  counts: { updated: number; issues: number; discovered: number };
}

/** Chart palette for holding/bucket series — the design handoff's list, in order. Assigned
 * by rank and never cycled past its length: a 13th line folds into "Other" instead. */
export const CHART_PALETTE = [
  "#12b3a4", "#5b62e8", "#e8a13c", "#0f9d63", "#d9544a", "#8b90f2",
  "#b07d10", "#0d8a7e", "#c2661d", "#7a8a86", "#34d399", "#f0766a",
];

export const KIND_META: Record<Kind, { label: string; color: string }> = {
  equity: { label: "EQUITY", color: "#12b3a4" },
  debt: { label: "DEBT", color: "#e8a13c" },
  gold: { label: "GOLD", color: "#c2661d" },
  realestate: { label: "REAL ESTATE", color: "#7a8a86" },
  crypto: { label: "CRYPTO", color: "#8b90f2" },
};

// ------------------------------------------------------------------ formatting

/** Indian money shorthand. A minus sign, not a hyphen — it aligns with digits. */
export function money(v: number): string {
  const a = Math.abs(v);
  const s = v < 0 ? "−" : "";
  if (a >= 1e7) return `${s}₹${(a / 1e7).toFixed(2)} Cr`;
  if (a >= 1e5) return `${s}₹${(a / 1e5).toFixed(1)} L`;
  if (a >= 1e3) return `${s}₹${Math.round(a / 1e3)}k`;
  return `${s}₹${Math.round(a)}`;
}

/** Signed money — an explicit + on gains, so a column of changes reads at a glance. */
export function signedMoney(v: number): string {
  return (v >= 0 ? "+" : "") + money(v);
}

export function pct(v: number, digits = 1): string {
  return `${v >= 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

/** Exact rupees, grouped Indian-style — for inputs and tooltips where shorthand hides a lakh. */
export function exactMoney(v: number): string {
  return `₹${Math.round(v).toLocaleString("en-IN")}`;
}

export function ageLabel(months: number): string {
  const y = Math.floor(months / 12);
  const m = Math.round(months % 12);
  return y > 0 ? `${y}y ${m}m` : `${m}m`;
}

/** A date the source gives with a time — always shown with HH:MM (house rule). */
export function stampLabel(iso: string | null): string {
  if (!iso) return "never";
  const d = new Date(iso);
  return `${d.toLocaleDateString("en-IN", { day: "numeric", month: "short" })} · ${d
    .toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })}`;
}

/** A DAILY figure — a NAV date, a trade date. No clock exists in the source, so none is shown. */
export function dayLabel(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(`${iso}T00:00:00`);
  return d.toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "2-digit" });
}

export function monthLabel(ym: string): string {
  if (!ym) return "—";
  const [y, m] = ym.split("-").map(Number);
  return new Date(y, (m || 1) - 1, 1)
    .toLocaleDateString("en-IN", { month: "short", year: "numeric" });
}

// ------------------------------------------------------------------ derivations

export interface ClassAgg {
  key: AssetClassKey;
  label: string;
  color: string;
  kind: Kind;
  value: number;
  invested: number;
  share: number;
  target: number;
  drift: number;
  xirr: number;
  count: number;
}

export interface Totals {
  value: number;
  invested: number;
  gain: number;
  gainPct: number;
  dayChange: number;
  blendedXirr: number;
  realized: number;
  byKind: Record<Kind, number>;
  kindShare: Record<Kind, number>;
  autoCount: number;
  manualCount: number;
}

/** Value-weighted mean of a field — the only honest way to blend returns across sizes.
 * A plain average would let a ₹5k position swing the headline as hard as a ₹20L one. */
function valueWeighted(rows: Holding[], pick: (h: Holding) => number | null): number {
  let num = 0;
  let den = 0;
  for (const h of rows) {
    const v = pick(h);
    if (v === null || !Number.isFinite(v)) continue;
    num += v * h.value;
    den += h.value;
  }
  return den > 0 ? num / den : 0;
}

export function totals(rows: Holding[]): Totals {
  const value = rows.reduce((a, h) => a + h.value, 0);
  const invested = rows.reduce((a, h) => a + h.invested, 0);
  const byKind = Object.fromEntries(KINDS.map((k) => [k, 0])) as Record<Kind, number>;
  for (const h of rows) byKind[h.kind] = (byKind[h.kind] ?? 0) + h.value;
  const share = (v: number) => (value > 0 ? (v / value) * 100 : 0);
  return {
    value,
    invested,
    gain: value - invested,
    gainPct: invested > 0 ? ((value - invested) / invested) * 100 : 0,
    dayChange: rows.reduce((a, h) => a + h.day_change, 0),
    blendedXirr: valueWeighted(rows, (h) => h.xirr_pct),
    realized: rows.reduce((a, h) => a + h.realized, 0),
    byKind,
    kindShare: Object.fromEntries(
      KINDS.map((k) => [k, share(byKind[k])]),
    ) as Record<Kind, number>,
    autoCount: rows.filter((h) => h.sync === "auto").length,
    manualCount: rows.filter((h) => h.sync === "manual").length,
  };
}

export function byClass(
  rows: Holding[],
  meta: PortfolioPayload["asset_classes"],
  targets: Record<string, number>,
): ClassAgg[] {
  const total = rows.reduce((a, h) => a + h.value, 0);
  const keys = Object.keys(meta) as AssetClassKey[];
  return keys.map((key) => {
    const members = rows.filter((h) => h.asset_class === key);
    const value = members.reduce((a, h) => a + h.value, 0);
    const shareOf = total > 0 ? (value / total) * 100 : 0;
    const target = targets[key] ?? 0;
    return {
      key,
      label: meta[key].label,
      color: meta[key].color,
      kind: meta[key].kind,
      value,
      invested: members.reduce((a, h) => a + h.invested, 0),
      share: shareOf,
      target,
      drift: shareOf - target,
      xirr: valueWeighted(members, (h) => h.xirr_pct),
      count: members.length,
    };
  });
}

/** Greedy overweight → underweight matching, with a dead band so trivial gaps aren't "moves".
 * Shared by the class Rebalance tab and the bucket rebalance panel — one algorithm, so the two
 * screens can never suggest contradictory transfers. */
export function rebalanceMoves(
  entries: { name: string; excess: number }[],
  deadBand: number,
): { from: string; to: string; amount: number }[] {
  const sources = entries.filter((e) => e.excess > deadBand)
    .map((e) => ({ ...e })).sort((a, b) => b.excess - a.excess);
  const sinks = entries.filter((e) => e.excess < -deadBand)
    .map((e) => ({ name: e.name, excess: -e.excess })).sort((a, b) => b.excess - a.excess);
  const moves: { from: string; to: string; amount: number }[] = [];
  let i = 0;
  let j = 0;
  while (i < sources.length && j < sinks.length) {
    const amount = Math.min(sources[i].excess, sinks[j].excess);
    moves.push({ from: sources[i].name, to: sinks[j].name, amount });
    sources[i].excess -= amount;
    sinks[j].excess -= amount;
    if (sources[i].excess < deadBand) i += 1;
    if (sinks[j].excess < deadBand) j += 1;
  }
  return moves;
}

// ------------------------------------------------------------------ goals

/** Expand "₹X every year from A to B" into schedule rows — how a recurring cost is actually
 * described, rather than typed twenty-three times. Existing years are ADDED to, not replaced:
 * two goals can legitimately land in the same year. */
export function expandRecurring(
  existing: ScheduleRow[], amount: number, from: number, to: number,
): ScheduleRow[] {
  const merged = new Map(existing.map((r) => [r.year, r.amount]));
  for (let y = Math.min(from, to); y <= Math.max(from, to); y += 1) {
    merged.set(y, (merged.get(y) ?? 0) + amount);
  }
  return [...merged.entries()]
    .filter(([, a]) => a > 0)
    .map(([year, amt]) => ({ year, amount: amt }))
    .sort((a, b) => a.year - b.year);
}

// ------------------------------------------------------------------ write payloads

export interface HoldingInput {
  name: string;
  asset_class: AssetClassKey;
  kind_override: Kind | null;
  invested: number;
  units: number | null;
  value: number;
  last_price: number | null;
  native_currency: string | null;
  native_price: number | null;
  native_invested: number | null;
  day_change: number;
  xirr_pct: number | null;
  buy_month: string;
  sync: "auto" | "manual";
  sync_source: "broker" | "amfi" | "global" | null;
  sync_ref: string | null;
  broker_account_id: number | null;
  units_locked: boolean;
  broker_units: Record<string, number>;
  excluded_from_buckets: boolean;
  dividend_yield_pct: number | null;
  interest_rate_pct: number | null;
  maturity_date: string | null;
  monthly_contribution: number | null;
  monthly_income: number | null;
  note: string | null;
}

export interface TransactionRow {
  id: number;
  on_date: string;
  kind: "buy" | "sell";
  units: number;
  price: number;
  fees: number;
  note: string | null;
}

export interface TransactionInput {
  on_date: string;
  kind: "buy" | "sell";
  units: number;
  price: number;
  fees: number;
  note?: string | null;
}

export interface BucketInput {
  name: string;
  target_pct: number;
  holding_ids: number[];
}

export interface GoalInput {
  name: string;
  allocations: Allocation[];
  schedule: ScheduleRow[];
  inflation_pct: number;
  target_amount: number;
  target_year: number;
  monthly_sip: number;
  holding_ids: number[];
  benchmark: string;
}

// ------------------------------------------------------------------ sheet seeding

/** One symbol's net position as the WIDE tracking sheet would produce it — computed by the
 * server's FIFO engine on the preview, so a bad paste is visible before anything is written. */
export interface LedgerSymbol {
  symbol: string;
  name: string;
  asset_class: AssetClassKey;
  trades: number;
  buys: number;
  sells: number;
  units: number;
  invested: number;
  realized: number;
  oversold_units: number;
  first_trade: string | null;
  last_trade: string | null;
  /** Splits and bonuses, with the ratio derived from units held at the time. A 1:10 that
   * reads 1:9.6 means a trade is missing before it — and everything after would be wrong. */
  actions: {
    on_date: string; added: number; ratio: number | null;
    units_before?: number; units_after?: number; problem: string | null;
  }[];
  errors: LedgerError[];
  /** False when this symbol has unreadable rows. Others in the same paste still seed. */
  seedable: boolean;
}

export interface LedgerError {
  line: number;
  symbol: string | null;
  message: string;
}

export interface LedgerPreview {
  symbols: LedgerSymbol[];
  /** GLOBAL failures only (no tabs, a row with no symbol) — these block everything. */
  errors: LedgerError[];
  warnings: string[];
  summary: { symbols: number; seedable: number; trades: number };
}

export interface SeedResult {
  seeded: {
    symbol: string; holding_id: number; created: boolean; trades: number;
    units?: number | null; invested?: number; realized?: number; oversold_units?: number;
  }[];
  warnings: string[];
  summary: { holdings: number; created: number; trades: number };
}

/** A per-unit PRICE, not a portfolio total — so never abbreviated to Cr/L. A holding's
 * average cost and its last traded price are compared against each other and against the
 * market, and "₹73.90 L" for one bitcoin is unreadable where ₹73,90,452 is not. */
export function priceLabel(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  return `₹${v.toLocaleString("en-IN", {
    minimumFractionDigits: v >= 1000 ? 0 : 2,
    maximumFractionDigits: 2,
  })}`;
}

/** Cost per unit. Null when units aren't tracked (EPF, PPF, property) — there is no "per
 * unit" for a balance, and dividing by a missing count would invent one. */
export function avgPrice(h: Holding): number | null {
  return h.units && h.units > 0 ? h.invested / h.units : null;
}

/** Unit counts, which span nine orders of magnitude here — 0.02954 BTC to 1,33,445 ETF units.
 * A fixed precision either hides the bitcoin entirely or prints ".000" on every ETF, so the
 * scale of the number picks the precision. Null where units aren't tracked at all. */
export function unitsLabel(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  if (Number.isInteger(v)) return v.toLocaleString("en-IN");
  const digits = v < 1 ? 6 : v < 1000 ? 3 : 2;
  return v.toLocaleString("en-IN", { maximumFractionDigits: digits });
}

/** Today's move as a percentage — measured against YESTERDAY's value, not today's.
 *
 * The denominator is `value − day_change`, i.e. units × previous close. Dividing by today's
 * value instead is the common slip and it understates every gain and overstates every loss,
 * by more the larger the move. Null when nothing repriced. */
export function dayPct(h: Holding): number | null {
  if (!h.day_change) return null;
  const prior = h.value - h.day_change;
  return prior > 0 ? (h.day_change / prior) * 100 : null;
}

/** Portfolio-level day move, over the MARKET-LINKED holdings only.
 *
 * EPF, PPF and anything hand-entered don't reprice, so folding their value into the
 * denominator would quietly shrink the percentage toward zero and report a calm day on a
 * portfolio that actually moved. The rupee total still covers everything. */
export function dayMove(rows: Holding[]): { rupees: number; pct: number | null } {
  const rupees = rows.reduce((a, h) => a + h.day_change, 0);
  const live = rows.filter((h) => h.sync === "auto" && h.last_price !== null);
  const prior = live.reduce((a, h) => a + (h.value - h.day_change), 0);
  return { rupees, pct: prior > 0 ? (rupees / prior) * 100 : null };
}

const CURRENCY_SIGN: Record<string, string> = { USD: "$", EUR: "€", GBP: "£", INR: "₹" };

/** A price in the holding's OWN currency, shown beside the rupee figure. Returns null for
 * anything already quoted in rupees — "₹268.46 (₹268.46)" is noise, not information. */
export function nativeLabel(v: number | null | undefined, ccy: string | null): string | null {
  if (v === null || v === undefined || !ccy || ccy === "INR") return null;
  const sign = CURRENCY_SIGN[ccy] ?? `${ccy} `;
  return `${sign}${v.toLocaleString("en-US", {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  })}`;
}

/** Average cost in the holding's own currency. Uses the STORED native cost basis — dividing
 * the rupee figure by today's exchange rate would be wrong, because those rupees were spent
 * at the rates of the day, not today's. */
export function nativeAvg(h: Holding): number | null {
  return h.native_invested && h.units ? h.native_invested / h.units : null;
}

/** A Holding as the write payload for it.
 *
 * Written once, on purpose. Every place that saves an edited holding used to hand-copy the
 * seventeen fields, and each new field then had to be remembered in three files — which is
 * how `units_locked` came to be silently dropped on save, quietly unlocking a cross-broker
 * aggregate so the next sync would overwrite its units. One converter, one place to update.
 */
export function toInput(h: Holding): HoldingInput {
  return {
    name: h.name,
    asset_class: h.asset_class,
    kind_override: h.kind_override,
    invested: h.invested,
    units: h.units,
    value: h.value,
    last_price: h.last_price,
    native_currency: h.native_currency,
    native_price: h.native_price,
    native_invested: h.native_invested,
    day_change: h.day_change,
    xirr_pct: h.xirr_pct,
    buy_month: h.buy_month,
    sync: h.sync,
    sync_source: h.sync_source,
    sync_ref: h.sync_ref,
    broker_account_id: h.broker_account_id,
    units_locked: h.units_locked,
    broker_units: h.broker_units,
    excluded_from_buckets: h.excluded_from_buckets,
    dividend_yield_pct: h.dividend_yield_pct,
    interest_rate_pct: h.interest_rate_pct,
    maturity_date: h.maturity_date,
    monthly_contribution: h.monthly_contribution,
    monthly_income: h.monthly_income,
    note: h.note,
  };
}


export interface DividendRow {
  id: number;
  holding_id: number;
  holding?: string;
  on_date: string;
  amount: number;
  per_unit: number | null;
  note: string | null;
}

export interface IncomeLine {
  holding_id: number;
  name: string;
  asset_class: AssetClassKey;
  value: number;
  invested: number;
  yield_pct: number | null;
  monthly_income: number | null;
  expected_annual: number | null;
  yield_on_cost_pct: number | null;
  received_fy: number;
  received_total: number;
  last_paid: string | null;
  payments: number;
}

export interface IncomeView {
  fy: string;
  expected_annual: number;
  expected_monthly: number;
  portfolio_yield_pct: number;
  received_fy: number;
  received_total: number;
  /** Value with NO yield entered — left out of the estimate rather than counted as zero. */
  unpriced_value: number;
  unpriced_share_pct: number;
  lines: IncomeLine[];
}


export interface TagRow {
  id: number;
  name: string;
  color: string;
  count?: number;
}
