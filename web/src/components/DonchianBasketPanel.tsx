import { Area, ComposedChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { formatInr } from "../lib/format";
import type { DonchianBasket, DonchianBasketName } from "../types";

const money = (v?: number | null) => (v == null ? "—" : formatInr(v));
const n1 = (v?: number | null) => (v == null ? "—" : v.toFixed(1));
const signed = (v?: number | null) => (v == null ? "—" : `${v >= 0 ? "+" : "−"}${formatInr(Math.abs(v))}`);

const STATUS_COLOR: Record<string, [string, string]> = {
  open: ["var(--ok-bg)", "var(--ok-text)"],
  flipped: ["var(--warn-bg)", "var(--warn-text)"],
  closed: ["var(--chip)", "var(--faint)"],
  settled: ["var(--chip)", "var(--faint)"],
};

/** One name's short legs as "CE 2980 / PE 2820", breached strikes flagged. */
function Legs({ name }: { name: DonchianBasketName }) {
  const open = name.legs.filter((l) => l.open);
  if (!open.length) return <span className="text-[var(--faint)]">—</span>;
  return (
    <span className="space-x-2">
      {open.map((l, i) => (
        <span key={i} className={l.breached ? "text-[var(--danger)]" : l.right === "CE" ? "text-[var(--danger)]/80" : "text-[var(--pos)]/80"}>
          {l.right} {l.strike}{l.breached ? " ⚑" : ""}
        </span>
      ))}
    </span>
  );
}

/** Per-name basket breakdown + hedge drift + aggregate (correlated-move) payoff for a
 *  donchian_strangle_monthly deployment. */
export default function DonchianBasketPanel({ basket }: { basket: DonchianBasket }) {
  const { names, hedge, realized_pnl, payoff } = basket;
  const openMtm = names.reduce((s, n) => s + (n.mtm || 0), 0) + (hedge.mtm || 0);
  const drift = hedge.entry_notional > 0 ? (hedge.current_notional / hedge.entry_notional - 1) * 100 : 0;

  // green/red split of the payoff area at P&L = 0.
  const ys = payoff.map((p) => p.expiry_pnl);
  const hi = Math.max(0, ...ys);
  const lo = Math.min(0, ...ys);
  const off = hi <= 0 ? 0 : lo >= 0 ? 1 : hi / (hi - lo);

  return (
    <div className="mt-3 border-t border-[var(--divider)] pt-3 space-y-3">
      <div className="text-xs text-[var(--muted)] flex flex-wrap gap-x-5 gap-y-1">
        <span>Basket MTM (open) <span className={openMtm >= 0 ? "text-[var(--pos)]" : "text-[var(--danger)]"}>{signed(openMtm)}</span></span>
        <span>Realized (flips) <span className={realized_pnl >= 0 ? "text-[var(--pos)]" : "text-[var(--danger)]"}>{signed(realized_pnl)}</span></span>
        <span>Hedge MTM <span className={hedge.mtm >= 0 ? "text-[var(--pos)]" : "text-[var(--danger)]"}>{signed(hedge.mtm)}</span></span>
        <span title="Aggregate notional now vs at entry — the hedge is held even as it over-sizes (spec §8)">
          Notional {money(hedge.current_notional)} / {money(hedge.entry_notional)} entry
          {Math.abs(drift) >= 1 && <span className="text-[var(--warn-text)]"> ({drift.toFixed(0)}% drift)</span>}
        </span>
      </div>

      <div className="overflow-x-auto max-h-72 overflow-y-auto">
        <table className="w-full text-xs tabular-nums">
          <thead className="text-[var(--muted)] text-left sticky top-0 bg-[var(--card)]">
            <tr>
              <th className="py-1 pr-3">Name</th>
              <th className="py-1 pr-3">Status</th>
              <th className="py-1 pr-3 text-right">Spot</th>
              <th className="py-1 pr-3">Short legs</th>
              <th className="py-1 pr-3 text-right">Flips</th>
              <th className="py-1 pr-3 text-right">MTM</th>
            </tr>
          </thead>
          <tbody>
            {names.map((n) => {
              const [bg, color] = STATUS_COLOR[n.status] ?? ["var(--chip)", "var(--chip-text)"];
              return (
                <tr key={n.symbol} className="border-t border-[var(--divider)]/40">
                  <td className="py-1 pr-3 font-medium">{n.symbol}</td>
                  <td className="py-1 pr-3"><span className="rounded-full px-2 py-0.5 text-[11px]" style={{ background: bg, color }}>{n.status}</span></td>
                  <td className="py-1 pr-3 text-right">{n1(n.spot)}</td>
                  <td className="py-1 pr-3"><Legs name={n} /></td>
                  <td className="py-1 pr-3 text-right">{n.flip_count || ""}</td>
                  <td className={`py-1 pr-3 text-right ${n.mtm >= 0 ? "text-[var(--pos)]" : "text-[var(--danger)]"}`}>{signed(n.mtm)}</td>
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
