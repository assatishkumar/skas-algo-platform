import { formatInr } from "../lib/format";
import type { Holdings } from "../types";

/** The accumulation panel: what the run OWNS at the end, not what it traded.
 *
 *  A buy-and-hold strategy has no round trips, so the trading tiles above describe its cash
 *  management rather than its investing — "win rate" and "realized P&L" measure the ETF sweeps
 *  that funded the buying, and every rupee of real return sits unrealized in open positions.
 *  This is the view that actually measures it. CAGR here is XIRR (money-weighted): units are
 *  bought across years, so a start/end CAGR has no single start date. */
export default function HoldingsReport({ holdings }: { holdings: Holdings }) {
  const { rows, totals, fund, benchmark } = holdings;
  if (!rows?.length) return null;

  const sign = (v: number | null | undefined) =>
    v == null ? "" : v >= 0 ? "text-emerald-600 dark:text-emerald-400"
                            : "text-rose-600 dark:text-rose-400";
  const pct = (v: number | null | undefined, d = 1) => (v == null ? "—" : `${v.toFixed(d)}%`);

  return (
    <div className="rounded-lg border border-slate-200 dark:border-slate-800 p-4 space-y-4">
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <h3 className="font-medium text-slate-800 dark:text-slate-200">Portfolio — what you own</h3>
        <span className="text-xs text-slate-500">
          {totals.names} names · as of {holdings.as_of} · nothing is ever sold
        </span>
      </div>

      {/* headline: the three numbers that describe an accumulation run */}
      <div className="grid gap-px bg-slate-200 dark:bg-slate-800 rounded-md overflow-hidden
                      grid-cols-2 md:grid-cols-4">
        <Tile label="Invested" value={formatInr(totals.invested)} />
        <Tile label="Market value" value={formatInr(totals.value)} />
        <Tile label="Gain" value={formatInr(totals.pnl)} sub={pct(totals.pnl_pct)}
              cls={sign(totals.pnl)} />
        <Tile label="CAGR (XIRR)" value={pct(totals.xirr_pct, 2)} cls={sign(totals.xirr_pct)}
              sub="money-weighted" />
      </div>

      {/* the comparison that answers "should I just have bought the index?" */}
      {benchmark && (
        <div className="rounded-md border border-slate-200 dark:border-slate-800 px-3 py-2 text-sm">
          <span className="text-slate-500">
            The same rupees, on the same days, into <strong>{benchmark.index}</strong>:
          </span>{" "}
          <span className="font-medium tabular-nums">{formatInr(benchmark.value)}</span>{" "}
          <span className="text-slate-500">(XIRR {pct(benchmark.xirr_pct, 2)})</span>
          {benchmark.vs_value != null && (
            <span className={`ml-2 font-medium ${sign(benchmark.vs_value)}`}>
              → this portfolio is {benchmark.vs_value >= 0 ? "ahead by" : "behind by"}{" "}
              {formatInr(Math.abs(benchmark.vs_value))}
              {benchmark.vs_xirr_pts != null && ` (${benchmark.vs_xirr_pts >= 0 ? "+" : ""}${
                benchmark.vs_xirr_pts} pts of XIRR)`}
            </span>
          )}
        </div>
      )}

      <div className="overflow-x-auto">
        <table className="w-full text-sm tabular-nums">
          <thead className="text-xs text-slate-500">
            <tr className="text-right">
              <th className="py-1.5 pr-3 text-left">Stock</th>
              <th className="py-1.5 pr-3">Units</th>
              <th className="py-1.5 pr-3">Avg cost</th>
              <th className="py-1.5 pr-3">Last</th>
              <th className="py-1.5 pr-3">Invested</th>
              <th className="py-1.5 pr-3">Value</th>
              <th className="py-1.5 pr-3">P&amp;L</th>
              <th className="py-1.5 pr-3">P&amp;L %</th>
              <th className="py-1.5 pr-3" title="Money-weighted return on this holding's own buys">
                CAGR
              </th>
              <th className="py-1.5 pr-3">Weight</th>
              <th className="py-1.5 pr-0">Buys</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.symbol} className="border-t border-slate-100 dark:border-slate-800/60 text-right">
                <td className="py-1.5 pr-3 text-left font-medium">{r.symbol}</td>
                <td className="py-1.5 pr-3">{r.units}</td>
                <td className="py-1.5 pr-3">{r.avg_cost?.toFixed(2) ?? "—"}</td>
                <td className="py-1.5 pr-3">{r.last_price?.toFixed(2) ?? "—"}</td>
                <td className="py-1.5 pr-3">{formatInr(r.invested)}</td>
                <td className="py-1.5 pr-3">{formatInr(r.value)}</td>
                <td className={`py-1.5 pr-3 ${sign(r.pnl)}`}>{formatInr(r.pnl)}</td>
                <td className={`py-1.5 pr-3 ${sign(r.pnl_pct)}`}>{pct(r.pnl_pct)}</td>
                <td className={`py-1.5 pr-3 ${sign(r.xirr_pct)}`}>{pct(r.xirr_pct)}</td>
                <td className="py-1.5 pr-3 text-slate-500">{pct(r.weight_pct)}</td>
                <td className="py-1.5 pr-0 text-slate-500">{r.buys}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr className="border-t border-slate-300 dark:border-slate-700 text-right font-medium">
              <td className="py-1.5 pr-3 text-left">Total</td>
              <td colSpan={3} />
              <td className="py-1.5 pr-3">{formatInr(totals.invested)}</td>
              <td className="py-1.5 pr-3">{formatInr(totals.value)}</td>
              <td className={`py-1.5 pr-3 ${sign(totals.pnl)}`}>{formatInr(totals.pnl)}</td>
              <td className={`py-1.5 pr-3 ${sign(totals.pnl_pct)}`}>{pct(totals.pnl_pct)}</td>
              <td className={`py-1.5 pr-3 ${sign(totals.xirr_pct)}`}>{pct(totals.xirr_pct)}</td>
              <td colSpan={2} />
            </tr>
          </tfoot>
        </table>
      </div>

      {fund && (
        <p className="text-xs text-slate-500 leading-relaxed">
          <strong>Fund source {fund.symbol}</strong>: {fund.units.toLocaleString("en-IN")} units
          = {formatInr(fund.value)}.
          {fund.yield_credited > 0 ? (
            <>
              {" "}A price-only backtest cannot see a dividend liquid ETF's yield (NAV stays
              flat and it pays out as units), so at {fund.yield_pct}%/yr the parked balance
              would additionally have earned{" "}
              <strong>{formatInr(fund.yield_credited)}</strong> — stated here, deliberately not
              folded into the equity curve above.
            </>
          ) : (
            <>
              {" "}Set <code>fund_yield_pct</code> to credit what the parked balance really
              earns — a dividend liquid ETF holds NAV flat, so the backtest scores it 0%.
            </>
          )}
        </p>
      )}
    </div>
  );
}

function Tile({ label, value, sub, cls }: {
  label: string; value: string; sub?: string; cls?: string;
}) {
  return (
    <div className="bg-white dark:bg-slate-900 px-3 py-2">
      <div className={`text-lg font-medium tabular-nums ${cls ?? ""}`}>{value}</div>
      <div className="text-xs text-slate-500">{label}{sub ? ` · ${sub}` : ""}</div>
    </div>
  );
}
