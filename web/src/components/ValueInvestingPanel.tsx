import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { formatInr } from "../lib/format";
import type { LiveHoldings, LiveHoldingRow } from "../types";

/** value_investing's own tile (owner ask, 2026-09-08). An accumulation run is not described
 *  by realized/unrealized P&L over "positions": the fund-source ETF is money waiting, not an
 *  investment; nothing is ever realized; and the number that matters is invested rupees
 *  against market value, per name, with a money-weighted return. This shows exactly that —
 *  every watchlist name whether or not it has been bought, the pooled rupees each is saving,
 *  and the buys today's 15:05 decision would make (a dry run of the real planner). */

const sign = (v: number | null | undefined) =>
  v == null ? "" : v >= 0 ? "text-[var(--pos)]" : "text-[var(--danger)]";
const pct = (v: number | null | undefined, d = 1) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(d)}%`);
const num = (v: number | null | undefined, d = 2) => (v == null ? "—" : v.toFixed(d));

export function useLiveHoldings(runId: number, version: number, enabled = true) {
  return useQuery({
    queryKey: ["live-holdings", runId, version],
    queryFn: () => api.liveHoldings(runId),
    enabled,
    refetchInterval: 60_000,
    staleTime: 20_000,
  });
}

function Stat({ label, value, sub, cls }: { label: string; value: string; sub?: string; cls?: string }) {
  return (
    <div className="rounded-[12px] bg-[var(--stat)] px-3 py-2">
      <div className="text-[10.5px] uppercase tracking-wide text-[var(--faint)]">{label}</div>
      <div className={`font-semibold tabular-nums text-[15px] ${cls ?? "text-[var(--strong)]"}`}>{value}</div>
      {sub && <div className="text-[11px] text-[var(--faint)]">{sub}</div>}
    </div>
  );
}

function StatusChip({ r }: { r: LiveHoldingRow }) {
  if (r.buys_today) {
    return (
      <span className="ml-2 rounded-full px-1.5 py-0.5 text-[10px] font-bold" style={{ background: "var(--pos-fill)", color: "var(--pos)" }}
        title={`Today's decision would buy ${r.buys_today.units} @ ${r.buys_today.price.toFixed(2)} = ${formatInr(r.buys_today.cost)}`}>
        buys {r.buys_today.units} today
      </span>
    );
  }
  if (r.affordable) {
    return (
      <span className="ml-2 rounded-full px-1.5 py-0.5 text-[10px] font-semibold" style={{ background: "var(--warn-bg)", color: "var(--warn-text)" }}
        title={`Its pot affords ${r.affordable.units} @ ${r.affordable.price.toFixed(2)} = ${formatInr(r.affordable.cost)}, but there is no settled cash to pay for it today`}>
        {r.affordable.units} ready · no cash
      </span>
    );
  }
  if (r.status === "pending") {
    return <span className="ml-2 rounded-full bg-[var(--chip)] px-1.5 py-0.5 text-[10px] font-semibold text-[var(--chip-text)]" title="On the watchlist, not bought yet — its pot is saving up">saving</span>;
  }
  if (r.status === "exited") {
    return <span className="ml-2 rounded-full bg-[var(--chip)] px-1.5 py-0.5 text-[10px] font-semibold text-[var(--chip-text)]">exited</span>;
  }
  if (!r.in_watchlist) {
    return <span className="ml-2 rounded-full px-1.5 py-0.5 text-[10px] font-semibold" style={{ background: "var(--warn-bg)", color: "var(--warn-text)" }} title="Held, but not on the watchlist — adopted from the broker or left by an earlier run">not on list</span>;
  }
  return null;
}

export default function ValueInvestingPanel({ runId, version }: { runId: number; version: number }) {
  const { data, isLoading, error } = useLiveHoldings(runId, version);
  if (isLoading) return <div className="mt-3 text-[12.5px] text-[var(--faint)]">Reading the holdings…</div>;
  if (error || !data) return <div className="mt-3 text-[12.5px] text-[var(--danger)]">Holdings view unavailable: {String((error as Error)?.message ?? "no data")}</div>;
  return <Body h={data} />;
}

function Body({ h }: { h: LiveHoldings }) {
  const t = h.totals;
  const today = h.today;
  const fund = h.fund;
  const runway = fund?.runway_days;
  return (
    <div className="mt-3 space-y-3">
      {/* what the run owns, with the fund kept beside it — never inside the totals */}
      <div className="grid grid-cols-2 gap-2 md:grid-cols-3 lg:grid-cols-6 text-sm">
        <Stat label="Invested" value={formatInr(t.invested)} sub={`${t.names} name${t.names === 1 ? "" : "s"} held`} />
        <Stat label="Market value" value={formatInr(t.value)} sub={`as of ${h.as_of}`} />
        <Stat label="Gain" value={formatInr(t.pnl)} sub={pct(t.pnl_pct)} cls={sign(t.pnl)} />
        <Stat label="CAGR (XIRR)" value={pct(t.xirr_pct, 2)} sub="money-weighted, stocks only" cls={sign(t.xirr_pct)} />
        {fund && (
          <Stat label={`${fund.symbol} · fund`} value={formatInr(fund.value)}
            sub={fund.checked === false ? "awaiting broker read" : `${fund.units.toLocaleString("en-IN")} units · ${runway == null ? "—" : runway <= 0 ? "empty" : `${runway} day${runway === 1 ? "" : "s"} of runway`}`}
            cls={fund.checked !== false && runway != null && runway <= 2 ? "text-[var(--warn-text)]" : undefined} />
        )}
        <Stat label="Pooled buckets" value={formatInr(today.pots_total)}
          sub={`₹${(today.daily_budget ?? 0).toLocaleString("en-IN")}/day split across the list`} />
      </div>

      {/* today's decision, before it happens */}
      <div className="rounded-[12px] border border-[var(--border)] bg-[var(--field)] px-3 py-2.5 text-[12.5px]">
        <div className="flex flex-wrap items-center gap-x-5 gap-y-1 tabular-nums">
          <span><span className="text-[var(--faint)]">Spendable today</span>{" "}
            <b>{formatInr(today.spendable ?? 0)}</b>{today.projected ? <span className="text-[var(--faint)]"> · projected</span> : null}</span>
          <span><span className="text-[var(--faint)]">Settling</span> <b>{formatInr(today.settling ?? 0)}</b></span>
          <span>
            <span className="text-[var(--faint)]">{today.shopped_today ? "Bought today" : "Will buy at 15:05"}</span>{" "}
            {today.plan.length ? (
              <b>{today.plan.map((p) => `${p.symbol} ${p.units}`).join(" · ")} = {formatInr(today.plan_total)}</b>
            ) : today.blocked_by === "cash" ? (
              <b className="text-[var(--warn-text)]">nothing — no settled cash to pay with{today.settling ? ` (${formatInr(today.settling)} lands tomorrow)` : ""}</b>
            ) : (
              <b className="text-[var(--faint)]">nothing — no pot affords a share at today's prices</b>
            )}
          </span>
        </div>
        {today.blocked_by === "cash" && today.affordable.length > 0 && (
          <div className="mt-1 text-[12.5px] tabular-nums">
            <span className="text-[var(--faint)]">Ready to buy once cash settles, in today's order:</span>{" "}
            <b>{today.affordable.slice(0, 8).map((p) => `${p.symbol} ${p.units}`).join(" · ")}{today.affordable.length > 8 ? ` · +${today.affordable.length - 8} more` : ""}</b>
            <span className="text-[var(--faint)]"> = {formatInr(today.affordable_total)} of pots</span>
          </div>
        )}
        {today.plan.length > 0 && today.affordable_total > today.plan_total && (
          <div className="mt-1 text-[12.5px] tabular-nums text-[var(--faint)]">
            The pots could buy {formatInr(today.affordable_total)}; settled cash covers {formatInr(today.plan_total)} of it today. The rest waits.
          </div>
        )}
        <div className="mt-1 text-[11px] text-[var(--faint)]">
          Ranked by today's fall, biggest first; a name buys whole shares from its own pot only, and the remainder keeps saving. The list refreshes with prices, so it can change before the decision.
        </div>
      </div>

      {/* every watchlist name */}
      <div className="overflow-x-auto">
        <table className="w-full text-[12.5px] tabular-nums">
          <thead className="text-[10.5px] uppercase tracking-wide text-[var(--faint)]">
            <tr className="text-right">
              <th className="py-1.5 pr-3 text-left">Stock</th>
              <th className="py-1.5 pr-3" title="today's change, the ranking key">Today</th>
              <th className="py-1.5 pr-3">Units</th>
              <th className="py-1.5 pr-3">Avg</th>
              <th className="py-1.5 pr-3">LTP</th>
              <th className="py-1.5 pr-3">Invested</th>
              <th className="py-1.5 pr-3">Value</th>
              <th className="py-1.5 pr-3">P&amp;L</th>
              <th className="py-1.5 pr-3">Return</th>
              <th className="py-1.5 pr-3" title="money-weighted return on this name's own buys; blank until it has been held long enough to mean anything">CAGR</th>
              <th className="py-1.5 pr-3" title="rupees this name has saved up; it buys whole shares from this alone">Pot</th>
              <th className="py-1.5 pr-3">Weight</th>
              <th className="py-1.5 pr-0">Buys</th>
            </tr>
          </thead>
          <tbody>
            {h.rows.map((r) => (
              <tr key={r.symbol} className={`border-t border-[var(--divider)] text-right ${r.status === "held" ? "" : "text-[var(--muted)]"}`}>
                <td className="py-1.5 pr-3 text-left font-medium text-[var(--strong)] whitespace-nowrap">
                  {r.symbol}<StatusChip r={r} />
                </td>
                <td className={`py-1.5 pr-3 ${sign(r.change_pct)}`}>{r.change_pct == null ? "—" : pct(r.change_pct, 2)}</td>
                <td className="py-1.5 pr-3">{r.units || "—"}</td>
                <td className="py-1.5 pr-3">{num(r.avg_cost)}</td>
                <td className="py-1.5 pr-3">{num(r.last_price)}</td>
                <td className="py-1.5 pr-3">{r.invested ? formatInr(r.invested) : "—"}</td>
                <td className="py-1.5 pr-3">{r.value ? formatInr(r.value) : "—"}</td>
                <td className={`py-1.5 pr-3 ${sign(r.pnl)}`}>{r.pnl == null ? "—" : formatInr(r.pnl)}</td>
                <td className={`py-1.5 pr-3 ${sign(r.pnl_pct)}`}>{pct(r.pnl_pct)}</td>
                <td className={`py-1.5 pr-3 ${sign(r.xirr_pct)}`}>{r.xirr_pct == null ? "—" : pct(r.xirr_pct)}</td>
                <td className="py-1.5 pr-3">{r.pot ? formatInr(r.pot) : "—"}</td>
                <td className="py-1.5 pr-3 text-[var(--faint)]">{r.weight_pct == null ? "—" : `${r.weight_pct.toFixed(1)}%`}</td>
                <td className="py-1.5 pr-0 text-[var(--faint)]">{r.buys || "—"}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr className="border-t border-[var(--border)] text-right font-semibold text-[var(--strong)]">
              <td className="py-1.5 pr-3 text-left">Total</td>
              <td colSpan={4} />
              <td className="py-1.5 pr-3">{formatInr(t.invested)}</td>
              <td className="py-1.5 pr-3">{formatInr(t.value)}</td>
              <td className={`py-1.5 pr-3 ${sign(t.pnl)}`}>{formatInr(t.pnl)}</td>
              <td className={`py-1.5 pr-3 ${sign(t.pnl_pct)}`}>{pct(t.pnl_pct)}</td>
              <td className={`py-1.5 pr-3 ${sign(t.xirr_pct)}`}>{t.xirr_pct == null ? "—" : pct(t.xirr_pct)}</td>
              <td className="py-1.5 pr-3">{formatInr(today.pots_total)}</td>
              <td colSpan={2} />
            </tr>
          </tfoot>
        </table>
      </div>
      <div className="text-[11px] text-[var(--faint)]">
        Stocks only. {fund ? `${fund.symbol} is the money waiting to be invested and is shown beside the totals, never inside them. ` : ""}
        Return is on cost; CAGR is money-weighted over each name's own buy dates.
      </div>
    </div>
  );
}
