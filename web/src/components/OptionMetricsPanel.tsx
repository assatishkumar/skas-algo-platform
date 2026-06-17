import { useMemo } from "react";
import { formatInr } from "../lib/format";
import { computeMetrics, type LiveLeg } from "../lib/payoff";
import type { LiveRunSnapshot } from "../types";

function fmtMoney(v: number): string {
  return Number.isFinite(v) ? formatInr(v) : "Unlimited";
}

function Metric({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "pos" | "neg";
}) {
  const color = tone === "pos" ? "text-emerald-600 dark:text-emerald-400" : tone === "neg" ? "text-rose-600 dark:text-rose-400" : "";
  return (
    <div className="rounded-md bg-slate-800/40 px-2.5 py-1.5">
      <div className="text-slate-400 text-[11px] mb-0.5">{label}</div>
      <div className={`font-medium tabular-nums ${color}`}>{value}</div>
      {sub && <div className="text-[10px] text-slate-500">{sub}</div>}
    </div>
  );
}

/** Sensibull-style position metrics for an options deployment — replaces the equity-centric
 *  "deployed / parts deployed / equity" boxes. Derived from the open legs' payoff curve
 *  (max P/L, breakevens, POP, time/intrinsic value) + live margin & net credit. */
export default function OptionMetricsPanel({ run }: { run: LiveRunSnapshot }) {
  const spot = run.underlying_spot ?? null;
  const metrics = useMemo(() => {
    const legs: LiveLeg[] = [];
    let expiry = "";
    for (const p of run.positions ?? []) {
      const parts = p.symbol.split("|"); // UNDERLYING|EXPIRY|STRIKE|RIGHT
      if (parts.length !== 4) continue;
      expiry = parts[1];
      legs.push({
        strike: Number(parts[2]),
        right: parts[3],
        direction: p.direction ?? 1,
        units: p.units,
        entry: p.avg_price,
        ltp: p.ltp,
      });
    }
    if (!legs.length || !spot || !expiry) return null;
    return computeMetrics(legs, spot, expiry, undefined, run.net_iv);
  }, [run.positions, spot, run.net_iv]);

  const margin = run.margin_used ?? null;
  const marginSrc = run.margin_source === "zerodha" ? "Zerodha basket" : run.margin_source === "model" ? "model est." : undefined;
  const netCredit = run.net_credit ?? null;
  const creditLabel = netCredit != null && netCredit < 0 ? "Net debit paid" : "Net credit recd";
  const realized = run.realized_pnl ?? null;
  const targetAmt = run.profit_target_amt ?? null;
  const stopAmt = run.stop_loss_amt ?? null;

  const beText =
    metrics && metrics.breakevens.length && spot
      ? metrics.breakevens
          .map((b) => `${Math.round(b)} (${b >= spot ? "+" : ""}${(((b - spot) / spot) * 100).toFixed(1)}%)`)
          .join(", ")
      : "—";

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-sm">
      {metrics && (
        <>
          <Metric
            label="Profit left"
            value={fmtMoney(metrics.profitLeft)}
            tone={metrics.profitLeft >= 0 ? "pos" : "neg"}
          />
          <Metric
            label="Loss left"
            value={metrics.maxLossUnlimited ? "Unlimited" : fmtMoney(metrics.lossLeft)}
            tone="neg"
          />
          <Metric label="Max profit" value={fmtMoney(metrics.maxProfit)} tone="pos" />
          <Metric
            label="Max loss"
            value={metrics.maxLossUnlimited ? "Unlimited" : fmtMoney(metrics.maxLoss)}
            tone="neg"
          />
          <Metric label="Breakeven" value={beText} />
          <Metric label="POP" value={metrics.pop != null ? `${(metrics.pop * 100).toFixed(0)}%` : "—"} />
          <Metric label="Time value" value={fmtMoney(metrics.timeValue)} />
          <Metric label="Intrinsic value" value={fmtMoney(metrics.intrinsicValue)} />
          <Metric
            label="Reward / Risk"
            value={metrics.rewardRisk != null ? `${metrics.rewardRisk.toFixed(2)}x` : "NA"}
          />
          <Metric
            label="Current P&L"
            value={fmtMoney(metrics.currentPnl)}
            tone={metrics.currentPnl >= 0 ? "pos" : "neg"}
          />
        </>
      )}
      <Metric
        label="Margin used"
        value={margin != null ? formatInr(margin) : "—"}
        sub={marginSrc}
      />
      <Metric
        label={creditLabel}
        value={netCredit != null ? formatInr(Math.abs(netCredit)) : "—"}
        tone={netCredit != null ? (netCredit >= 0 ? "pos" : "neg") : undefined}
      />
      {targetAmt != null && (
        <Metric label="Profit target" value={`+${formatInr(targetAmt)}`} tone="pos" />
      )}
      {stopAmt != null && (
        <Metric label="Stop loss" value={`−${formatInr(Math.abs(stopAmt))}`} tone="neg" />
      )}
      {realized != null && realized !== 0 && (
        <Metric
          label="Realized P&L"
          value={formatInr(realized)}
          tone={realized >= 0 ? "pos" : "neg"}
        />
      )}
    </div>
  );
}
