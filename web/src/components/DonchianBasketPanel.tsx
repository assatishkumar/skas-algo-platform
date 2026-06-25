import { useState, type ReactNode } from "react";
import { Area, ComposedChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { formatInr } from "../lib/format";
import type { DonchianBasket, DonchianBasketLeg, DonchianBasketName } from "../types";

const money = (v?: number | null) => (v == null ? "—" : formatInr(v));
const n1 = (v?: number | null) => (v == null ? "—" : v.toFixed(1));
const n2 = (v?: number | null) => (v == null ? "—" : v.toFixed(2));
const signed = (v?: number | null) => (v == null ? "—" : `${v >= 0 ? "+" : "−"}${formatInr(Math.abs(v))}`);

const STATUS_COLOR: Record<string, [string, string]> = {
  open: ["var(--ok-bg)", "var(--ok-text)"],
  flipped: ["var(--warn-bg)", "var(--warn-text)"],
  closed: ["var(--chip)", "var(--faint)"],
  settled: ["var(--chip)", "var(--faint)"],
};

const openLeg = (n: DonchianBasketName, right: "CE" | "PE"): DonchianBasketLeg | undefined =>
  n.legs.find((l) => l.open && l.right === right);

/** "K · ₹entry→ltp", breach-flagged. */
function LegMini({ leg }: { leg?: DonchianBasketLeg }) {
  if (!leg) return <span className="text-[var(--faint)]">—</span>;
  return (
    <span className={leg.breached ? "text-[var(--danger)]" : ""}>
      {leg.strike} · ₹{n2(leg.entry)}→{n2(leg.mark)}{leg.breached ? " ⚑" : ""}
    </span>
  );
}

type Sort = { col: string; dir: 1 | -1 };
const VAL: Record<string, (n: DonchianBasketName) => number | string | null | undefined> = {
  name: (n) => n.symbol,
  status: (n) => n.status,
  spot: (n) => n.spot,
  units: (n) => n.units,
  credit: (n) => n.credit,
  mtm: (n) => n.mtm,
  flips: (n) => n.flip_count,
};

function Th({ children, col, right, sort, onSort }: {
  children: ReactNode; col: string; right?: boolean; sort: Sort; onSort: (c: string) => void;
}) {
  const active = sort.col === col;
  return (
    <th onClick={() => onSort(col)}
      className={`py-1 pr-3 cursor-pointer select-none hover:text-[var(--strong)] ${right ? "text-right" : "text-left"} ${active ? "text-[var(--strong)]" : ""}`}>
      {children}{active ? (sort.dir === 1 ? " ↑" : " ↓") : " ⇅"}
    </th>
  );
}

/** Unified per-name basket view for a donchian_strangle_monthly deployment: clubs each name's CE+PE
 *  legs into one sortable row (status, spot, leg entry→LTP, units, credit, MTM, flips), with
 *  book totals + hedge drift and the aggregate (correlated-move) payoff. Replaces the raw leg table. */
export default function DonchianBasketPanel({ basket }: { basket: DonchianBasket }) {
  const { names, hedge, realized_pnl, net_credit, payoff } = basket;
  const [sort, setSort] = useState<Sort>({ col: "mtm", dir: -1 });
  const onSort = (c: string) => setSort((s) => (s.col === c ? { col: c, dir: (s.dir === 1 ? -1 : 1) as 1 | -1 } : { col: c, dir: -1 }));

  const openMtm = names.reduce((s, n) => s + (n.mtm || 0), 0) + (hedge.mtm || 0);
  const drift = hedge.entry_notional > 0 ? (hedge.current_notional / hedge.entry_notional - 1) * 100 : 0;
  const sorted = [...names].sort((a, b) => {
    const va = VAL[sort.col](a), vb = VAL[sort.col](b);
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    if (typeof va === "string" || typeof vb === "string") return String(va).localeCompare(String(vb)) * sort.dir;
    return (va - vb) * sort.dir;
  });

  const ys = payoff.map((p) => p.expiry_pnl);
  const hi = Math.max(0, ...ys), lo = Math.min(0, ...ys);
  const off = hi <= 0 ? 0 : lo >= 0 ? 1 : hi / (hi - lo);

  return (
    <div className="mt-3 border-t border-[var(--divider)] pt-3 space-y-3">
      <div className="text-xs text-[var(--muted)] flex flex-wrap gap-x-5 gap-y-1">
        <span>Net credit <span className="text-[var(--pos)]">{money(net_credit)}</span></span>
        <span>Basket MTM (open) <span className={openMtm >= 0 ? "text-[var(--pos)]" : "text-[var(--danger)]"}>{signed(openMtm)}</span></span>
        <span>Realized (flips) <span className={(realized_pnl ?? 0) >= 0 ? "text-[var(--pos)]" : "text-[var(--danger)]"}>{signed(realized_pnl)}</span></span>
        <span title={hedge.legs.map((l) => `${l.right} ${l.strike}`).join(" · ")}>
          Hedge MTM <span className={hedge.mtm >= 0 ? "text-[var(--pos)]" : "text-[var(--danger)]"}>{signed(hedge.mtm)}</span>
          {hedge.spot != null && <span className="text-[var(--faint)]"> (NIFTY {n1(hedge.spot)})</span>}
        </span>
        <span title="Aggregate notional now vs at entry — the hedge is held even as it over-sizes (spec §8)">
          Notional {money(hedge.current_notional)} / {money(hedge.entry_notional)} entry
          {Math.abs(drift) >= 1 && <span className="text-[var(--warn-text)]"> ({drift.toFixed(0)}% drift)</span>}
        </span>
      </div>

      <div className="overflow-x-auto max-h-80 overflow-y-auto">
        <table className="w-full text-xs tabular-nums">
          <thead className="text-[var(--muted)] sticky top-0 bg-[var(--card)]">
            <tr>
              <Th col="name" sort={sort} onSort={onSort}>Name</Th>
              <Th col="status" sort={sort} onSort={onSort}>Status</Th>
              <Th col="spot" right sort={sort} onSort={onSort}>Spot</Th>
              <th className="py-1 pr-3 text-left">SELL CE</th>
              <th className="py-1 pr-3 text-left">SELL PE</th>
              <Th col="units" right sort={sort} onSort={onSort}>Units</Th>
              <Th col="credit" right sort={sort} onSort={onSort}>Credit</Th>
              <Th col="mtm" right sort={sort} onSort={onSort}>MTM</Th>
              <Th col="flips" right sort={sort} onSort={onSort}>Flips</Th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((n) => {
              const [bg, color] = STATUS_COLOR[n.status] ?? ["var(--chip)", "var(--chip-text)"];
              return (
                <tr key={n.symbol} className="border-t border-[var(--divider)]/40">
                  <td className="py-1 pr-3 font-medium">{n.symbol}</td>
                  <td className="py-1 pr-3"><span className="rounded-full px-2 py-0.5 text-[11px]" style={{ background: bg, color }}>{n.status}</span></td>
                  <td className="py-1 pr-3 text-right">{n1(n.spot)}</td>
                  <td className="py-1 pr-3 text-[var(--danger)]/90"><LegMini leg={openLeg(n, "CE")} /></td>
                  <td className="py-1 pr-3 text-[var(--pos)]/90"><LegMini leg={openLeg(n, "PE")} /></td>
                  <td className="py-1 pr-3 text-right">{n.units || ""}</td>
                  <td className="py-1 pr-3 text-right">{money(n.credit)}</td>
                  <td className={`py-1 pr-3 text-right ${n.mtm >= 0 ? "text-[var(--pos)]" : "text-[var(--danger)]"}`}>{signed(n.mtm)}</td>
                  <td className="py-1 pr-3 text-right">{n.flip_count || ""}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div>
        <div className="text-xs text-[var(--muted)] mb-1">
          Aggregate payoff at expiry — every name <em>and</em> the NIFTY hedge move together by the same %
          (the correlated-crash scenario the hedge is bought for).
        </div>
        <ResponsiveContainer width="100%" height={200}>
          <ComposedChart data={payoff} margin={{ top: 5, right: 12, bottom: 0, left: 12 }}>
            <defs>
              <linearGradient id="donchianPayoff" x1="0" y1="0" x2="0" y2="1">
                <stop offset={off} stopColor="#10b981" stopOpacity={0.5} />
                <stop offset={off} stopColor="#f43f5e" stopOpacity={0.5} />
              </linearGradient>
            </defs>
            <XAxis dataKey="move_pct" type="number" domain={["dataMin", "dataMax"]}
              tick={{ fontSize: 11, fill: "#94a3b8" }} tickFormatter={(v) => `${v}%`} />
            <YAxis tick={{ fontSize: 11, fill: "#94a3b8" }} width={64} tickFormatter={(v) => `${(v / 1e3).toFixed(0)}k`} />
            <Tooltip
              contentStyle={{ background: "rgb(var(--slate-900))", border: "1px solid rgb(var(--slate-700))", color: "rgb(var(--slate-100))" }}
              formatter={(v: number) => [formatInr(v), "P&L at expiry"]}
              labelFormatter={(v: number) => `Move ${v >= 0 ? "+" : ""}${v}% (all names)`}
            />
            <ReferenceLine y={0} stroke="#475569" />
            <ReferenceLine x={0} stroke="#38bdf8" strokeDasharray="3 3" label={{ value: "now", fill: "#38bdf8", fontSize: 10, position: "top" }} />
            <Area type="monotone" dataKey="expiry_pnl" stroke="#94a3b8" strokeWidth={1.5} fill="url(#donchianPayoff)" />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
