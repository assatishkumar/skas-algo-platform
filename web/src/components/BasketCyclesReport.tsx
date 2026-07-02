import { Fragment, useMemo, useState } from "react";
import { Card } from "./ui";
import { formatInr } from "../lib/format";
import { formatOptionSymbol } from "../lib/symbol";
import type { BasketCycle, BasketNameRow } from "../types";

/** Cycle-first positions view for the Donchian basket backtest: one row per monthly
 *  cycle (P&L, peak margin, exit reason) → expand to the names (lots, premium, flips,
 *  P&L) → expand a name to its individual legs (entry/exit price + reason). Replaces
 *  the generic per-leg positions table, which is unreadable at ~50 underlyings. */

const REASONS = ["ALL", "expiry", "portfolio_stop", "portfolio_target"] as const;

function ExitBadge({ reason }: { reason: string }) {
  const tone =
    reason === "portfolio_stop"
      ? "bg-rose-100 text-rose-700 dark:bg-rose-950/60 dark:text-rose-300"
      : reason === "portfolio_target"
        ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/60 dark:text-emerald-300"
        : "bg-slate-200 text-slate-600 dark:bg-slate-800 dark:text-slate-300";
  return <span className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${tone}`}>{reason}</span>;
}

function Pnl({ v, pct }: { v: number; pct?: number | null }) {
  const cls = v >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400";
  return (
    <span className={`tabular-nums ${cls}`}>
      {formatInr(v)}
      {pct != null && <span className="text-[11px] opacity-80"> ({pct.toFixed(1)}%)</span>}
    </span>
  );
}

const th = "py-1.5 pr-3 text-left text-[11px] uppercase tracking-wide text-slate-400 whitespace-nowrap";
const td = "py-1.5 pr-3 whitespace-nowrap tabular-nums";

function LegsTable({ n }: { n: BasketNameRow }) {
  return (
    <table className="w-full text-xs ml-2">
      <thead>
        <tr>
          <th className={th}>Leg</th>
          <th className={th}>Side</th>
          <th className={th}>Units</th>
          <th className={th}>Entry</th>
          <th className={th}>Exit</th>
          <th className={th}>Reason</th>
          <th className={`${th} text-right`}>P&L</th>
        </tr>
      </thead>
      <tbody>
        {n.legs.map((l, i) => (
          <tr key={i} className="border-t border-slate-200/60 dark:border-slate-800">
            <td className={td}>{formatOptionSymbol(l.symbol)}</td>
            <td className={td}>
              <span className={l.side === "sell" ? "text-rose-500" : "text-emerald-500"}>
                {l.side.toUpperCase()}
              </span>
            </td>
            <td className={td}>{l.units}</td>
            <td className={td}>
              {l.entry_date ?? "—"} @ {l.entry_price != null ? `₹${l.entry_price.toFixed(2)}` : "—"}
            </td>
            <td className={td}>
              {l.exit_date ?? "—"} @ {l.exit_price != null ? `₹${l.exit_price.toFixed(2)}` : "—"}
            </td>
            <td className={td}>{l.exit_reason}</td>
            <td className={`${td} text-right`}><Pnl v={l.pnl} /></td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function NamesTable({ cycle }: { cycle: BasketCycle }) {
  const [openName, setOpenName] = useState<string | null>(null);
  return (
    <div className="rounded-md bg-slate-100/60 dark:bg-slate-950/40 p-2 my-1">
      <table className="w-full text-xs">
        <thead>
          <tr>
            <th className={th}>Name</th>
            <th className={th}>Lots × size</th>
            <th className={`${th} text-right`}>Premium collected</th>
            <th className={th}>Flips</th>
            <th className={th}>Legs</th>
            <th className={`${th} text-right`}>Charges</th>
            <th className={`${th} text-right`}>Net P&L</th>
          </tr>
        </thead>
        <tbody>
          {cycle.name_rows.map((n) => (
            <Fragment key={n.name}>
              <tr
                onClick={() => setOpenName(openName === n.name ? null : n.name)}
                className="border-t border-slate-200/60 dark:border-slate-800 cursor-pointer hover:bg-slate-200/40 dark:hover:bg-slate-900/60"
              >
                <td className={td}>
                  <span className="mr-1 text-slate-400">{openName === n.name ? "▾" : "▸"}</span>
                  {n.name}
                  {n.side === "hedge" && (
                    <span className="ml-1.5 rounded bg-sky-100 text-sky-700 dark:bg-sky-950/60 dark:text-sky-300 px-1 py-0.5 text-[10px]">
                      hedge
                    </span>
                  )}
                </td>
                <td className={td}>{n.lots ?? "?"} × {n.lot_size}</td>
                <td className={`${td} text-right`}>{n.side === "hedge" ? "—" : formatInr(n.premium)}</td>
                <td className={td}>{n.flips || "—"}</td>
                <td className={td}>{n.legs.length}</td>
                <td className={`${td} text-right text-slate-400`}>{formatInr(n.charges)}</td>
                <td className={`${td} text-right`}><Pnl v={n.pnl_net} /></td>
              </tr>
              {openName === n.name && (
                <tr>
                  <td colSpan={7} className="pb-2"><LegsTable n={n} /></td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function BasketCyclesReport({ cycles }: { cycles: BasketCycle[] }) {
  const [reason, setReason] = useState<(typeof REASONS)[number]>("ALL");
  const [openCycle, setOpenCycle] = useState<string | null>(null);
  const rows = useMemo(
    () => (reason === "ALL" ? cycles : cycles.filter((c) => c.exit_reason === reason)),
    [cycles, reason],
  );
  const counts = useMemo(() => {
    const m: Record<string, number> = { ALL: cycles.length };
    for (const c of cycles) m[c.exit_reason] = (m[c.exit_reason] ?? 0) + 1;
    return m;
  }, [cycles]);
  const totals = useMemo(
    () => ({
      pnl: rows.reduce((s, c) => s + c.pnl_net, 0),
      premium: rows.reduce((s, c) => s + c.premium_collected, 0),
      wins: rows.filter((c) => c.pnl_net > 0).length,
    }),
    [rows],
  );

  return (
    <Card>
      <div className="flex items-center justify-between flex-wrap gap-2 mb-2">
        <div className="text-sm font-medium text-slate-300">
          Cycles{" "}
          <span className="text-slate-500 font-normal">
            ({rows.length} · {totals.wins} profitable · net <Pnl v={totals.pnl} /> · click a
            cycle for its names, a name for its legs)
          </span>
        </div>
        <div className="flex gap-1">
          {REASONS.map((r) => (
            <button
              key={r}
              onClick={() => setReason(r)}
              className={`rounded-md px-2 py-1 text-[11px] ${
                reason === r
                  ? "bg-emerald-700 text-white"
                  : "bg-slate-200 text-slate-600 dark:bg-slate-800 dark:text-slate-300 hover:opacity-80"
              }`}
            >
              {r}{counts[r] != null && r !== "ALL" ? ` (${counts[r]})` : ""}
            </button>
          ))}
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr>
              <th className={th}>Cycle</th>
              <th className={th}>Entry → Exit</th>
              <th className={th}>Names</th>
              <th className={`${th} text-right`}>Premium collected</th>
              <th className={th}>Flips</th>
              <th className={`${th} text-right`}>Peak margin</th>
              <th className={th}>Exit</th>
              <th className={`${th} text-right`}>Net P&L (RoM)</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((c) => (
              <Fragment key={c.expiry}>
                <tr
                  onClick={() => setOpenCycle(openCycle === c.expiry ? null : c.expiry)}
                  className="border-t border-slate-200/60 dark:border-slate-800 cursor-pointer hover:bg-slate-100/60 dark:hover:bg-slate-900/60"
                >
                  <td className={`${td} font-medium`}>
                    <span className="mr-1 text-slate-400">{openCycle === c.expiry ? "▾" : "▸"}</span>
                    {c.cycle}
                  </td>
                  <td className={td}>{c.entry_date} → {c.exit_date}</td>
                  <td className={td}>{c.names}</td>
                  <td className={`${td} text-right`}>{formatInr(c.premium_collected)}</td>
                  <td className={td}>{c.flips || "—"}</td>
                  <td className={`${td} text-right`}>
                    {c.margin_peak != null ? formatInr(c.margin_peak) : "—"}
                  </td>
                  <td className={td}><ExitBadge reason={c.exit_reason} /></td>
                  <td className={`${td} text-right`}>
                    <Pnl v={c.pnl_net} pct={c.return_on_margin_pct} />
                  </td>
                </tr>
                {openCycle === c.expiry && (
                  <tr>
                    <td colSpan={8}><NamesTable cycle={c} /></td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
      <div className="mt-2 text-[11px] text-slate-500">
        Premium collected includes flip re-entries; Net P&L is after F&O charges; RoM = net P&L ÷
        that cycle's peak modelled margin. The NIFTY row is the long tail hedge.
      </div>
    </Card>
  );
}
