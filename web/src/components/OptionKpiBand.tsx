import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { formatInr } from "../lib/format";
import { reconstructCycles } from "../lib/optionCycles";
import { computeMetrics, effectiveSpot, type LiveLeg, type PositionMetrics } from "../lib/payoff";
import { parseOptionSymbol } from "../lib/symbol";
import type { LiveRunSnapshot, Trade } from "../types";

const sign = (v: number) => (v >= 0 ? "text-[var(--pos)]" : "text-[var(--danger)]");
const money = (v: number) => (Number.isFinite(v) ? formatInr(v) : "Unlimited");

function Card({ title, right, children }: { title: string; right?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="rounded-[16px] border border-[var(--border)] bg-[var(--card)] px-4 py-3.5">
      <div className="flex items-center justify-between">
        <div className="text-[10.5px] uppercase tracking-wide text-[var(--faint)]">{title}</div>
        {right}
      </div>
      {children}
    </div>
  );
}

function KV({ label, value, tone }: { label: string; value: string; tone?: number | null }) {
  return (
    <div className="flex items-baseline justify-between gap-2 py-0.5">
      <span className="text-[12px] text-[var(--muted)]">{label}</span>
      <span className={`font-['Space_Grotesk'] font-semibold tabular-nums text-[13px] whitespace-nowrap ${tone == null ? "text-[var(--strong)]" : sign(tone)}`}>
        {value}
      </span>
    </div>
  );
}

/** A breakeven bar for the (short-vol) profit band: green between the breakevens, red outside,
 *  ticks at each BE and a strong spot marker. Only the common 2-BE case gets the bar. */
function BreakevenBar({ bes, spot }: { bes: number[]; spot: number }) {
  if (bes.length < 2) return null;
  const lo = bes[0];
  const hi = bes[bes.length - 1];
  const x0 = Math.min(lo, spot);
  const x1 = Math.max(hi, spot);
  const pad = (x1 - x0) * 0.4 || spot * 0.02;
  const a = x0 - pad;
  const b = x1 + pad;
  const pct = (v: number) => `${(((v - a) / (b - a)) * 100).toFixed(2)}%`;
  return (
    <div className="mt-2">
      <div className="relative h-[9px] rounded-full bg-[var(--neg-fill,rgba(217,84,74,.16))] overflow-hidden">
        <div
          className="absolute inset-y-0 bg-[var(--pos-fill,rgba(15,157,99,.18))]"
          style={{ left: pct(lo), right: `${100 - parseFloat(pct(hi))}%` }}
        />
        {bes.map((be, i) => (
          <div key={i} className="absolute inset-y-0 w-px bg-[var(--pos)]" style={{ left: pct(be) }} />
        ))}
        <div className="absolute -inset-y-0.5 w-[3px] rounded bg-[var(--strong)]" style={{ left: pct(spot) }} />
      </div>
      <div className="mt-1.5 flex justify-between text-[10.5px] tabular-nums text-[var(--muted)]">
        <span>BE {Math.round(lo).toLocaleString("en-IN")} ({((lo - spot) / spot * 100).toFixed(1)}%)</span>
        <span className="text-[var(--strong)] font-semibold">spot {Math.round(spot).toLocaleString("en-IN")}</span>
        <span>BE {Math.round(hi).toLocaleString("en-IN")} (+{((hi - spot) / spot * 100).toFixed(1)}%)</span>
      </div>
    </div>
  );
}

/** Grouped KPI band for an options deployment — four scannable cards replacing the flat KPI wall:
 *  Overall P&L (layered realized/unrealized/prior + profit target), Risk envelope (breakeven bar +
 *  max P/L + POP), Premium & value, and Margin. P&L layering is reconstructed from the trade feed
 *  (shares LiveTradesPanel's query key); risk/premium are derived from the open legs' payoff curve. */
export default function OptionKpiBand({ run, version }: { run: LiveRunSnapshot; version: number }) {
  const spot = run.underlying_spot ?? null;
  const { data } = useQuery({
    queryKey: ["liveTrades", run.run_id, version],
    queryFn: () => api.liveTrades(run.run_id),
  });
  const trades: Trade[] = data?.trades ?? [];

  const { metrics, bandSpot } = useMemo((): {
    metrics: PositionMetrics | null; bandSpot: number | null;
  } => {
    // Dominant-underlying book only — a mixed run (cp_ratio NIFTY+SENSEX) can't share one
    // spot axis; effectiveSpot also swaps in a parity-derived spot when `underlying_spot`
    // belongs to the OTHER index (it's the primary underlying's).
    const groups = new Map<string, { legs: LiveLeg[]; expiry: string }>();
    for (const p of run.positions ?? []) {
      const o = parseOptionSymbol(p.symbol);
      if (!o) continue;
      const g = groups.get(o.underlying) ?? { legs: [], expiry: "" };
      g.expiry = o.expiry;
      g.legs.push({ strike: o.strike, right: o.right, direction: p.direction ?? 1, units: p.units, entry: p.avg_price, ltp: p.ltp });
      groups.set(o.underlying, g);
    }
    const [top] = [...groups.values()].sort((a, b) => b.legs.length - a.legs.length);
    if (!top || !top.legs.length || !top.expiry) return { metrics: null, bandSpot: null };
    const s = effectiveSpot(top.legs, spot);
    if (!s) return { metrics: null, bandSpot: null };
    return { metrics: computeMetrics(top.legs, s, top.expiry, undefined, run.net_iv), bandSpot: s };
  }, [run.positions, spot, run.net_iv]);

  // Layered P&L: prior cycles (excl. current) + this cycle realized + this cycle unrealized.
  const cycles = trades.length ? reconstructCycles(trades) : [];
  const openCycle = cycles.find((c) => c.open) ?? null;
  const prior = cycles.filter((c) => !c.open).reduce((s, c) => s + c.realized_pnl, 0);
  const thisRealized = openCycle?.realized_pnl ?? 0;
  const thisUnrealized = (run.positions ?? []).reduce((s, p) => s + p.unrealized_pnl, 0);
  const overall = prior + thisRealized + thisUnrealized;

  const margin = run.margin_used ?? null;
  const marginSrc = run.margin_source === "zerodha" ? "Zerodha basket" : run.margin_source === "model" ? "model estimate" : null;
  const target = run.profit_target_amt ?? null;
  const targetPct = target != null && margin ? (target / margin) * 100 : null;
  const stop = run.stop_loss_amt ?? null;
  const stopPct = stop != null && margin ? (Math.abs(stop) / margin) * 100 : null;
  const netCredit = run.net_credit ?? null;
  const creditNeg = netCredit != null && netCredit < 0;

  // Near-breakeven warning: how close is spot to the nearest BE (short-vol → the danger edge).
  let nearWarn: string | null = null;
  if (metrics && bandSpot && metrics.breakevens.length) {
    const nearest = metrics.breakevens.reduce((best, be) => (Math.abs(be - bandSpot) < Math.abs(best - bandSpot) ? be : best));
    const pts = Math.round(Math.abs(nearest - bandSpot));
    if (pts <= bandSpot * 0.01) nearWarn = `spot ${pts} pts ${nearest >= bandSpot ? "under upper" : "over lower"} BE`;
  }

  return (
    <div className="grid gap-3.5 grid-cols-1 md:grid-cols-2 xl:grid-cols-[1.15fr_1.45fr_1fr_0.85fr]">
      {/* Overall P&L */}
      <Card title="Overall P&L">
        <div className={`mt-1 font-['Space_Grotesk'] font-bold tabular-nums text-[26px] ${sign(overall)}`}>{formatInr(overall)}</div>
        <div className="text-[11.5px] text-[var(--muted)] tabular-nums">
          <span className={sign(thisRealized)}>{formatInr(thisRealized)}</span> realized ·{" "}
          <span className={sign(thisUnrealized)}>{formatInr(thisUnrealized)}</span> unrealized ·{" "}
          <span className={sign(prior)}>{formatInr(prior)}</span> prior cycles
        </div>
        {/* The number the strategy's exit checks ACTUALLY compare (decision-entry basis —
            fill slippage puts it ₹100s from the book P&L above; run-7 2026-07-17 the book
            said "target achieved" while this measure was still below the target). */}
        {run.strategy_pnl != null && (
          <div className="mt-1 text-[11.5px] text-[var(--muted)] tabular-nums"
            title="The strategy marks its legs from its decision-time entry premiums, not the actual fills — this is the P&L its target/stop checks compare.">
            strategy sees <span className={`font-semibold ${sign(run.strategy_pnl)}`}>{formatInr(run.strategy_pnl)}</span>
            {target != null ? <> vs target <span className="font-semibold text-[var(--pos)]">+{formatInr(target)}</span></> : null}
          </div>
        )}
        {target != null && (
          <div className="mt-2 pt-2 border-t border-[var(--divider)] text-[11.5px] text-[var(--muted)]">
            profit target <span className="font-semibold text-[var(--pos)]">+{formatInr(target)}</span>
            {targetPct != null ? ` · books at +${targetPct.toFixed(1)}% of margin` : ""}
          </div>
        )}
      </Card>

      {/* Risk envelope */}
      <Card
        title="Risk envelope · at expiry"
        right={metrics?.pop != null ? <span className="text-[11px] text-[var(--muted)]">POP <span className="font-semibold text-[var(--strong)] tabular-nums">{(metrics.pop * 100).toFixed(0)}%</span></span> : undefined}
      >
        {metrics && bandSpot ? (
          <>
            <BreakevenBar bes={metrics.breakevens} spot={bandSpot} />
            <div className="mt-2 pt-2 border-t border-[var(--divider)] flex items-center justify-between gap-2 text-[11.5px]">
              <span className="text-[var(--muted)]">max profit <span className={`font-semibold tabular-nums ${sign(metrics.maxProfit)}`}>{metrics.maxProfitUnlimited ? "Unlimited" : money(metrics.maxProfit)}</span></span>
              <span className="text-[var(--muted)]">max loss <span className="font-semibold tabular-nums text-[var(--danger)]">{metrics.maxLossUnlimited ? "Unlimited" : money(metrics.maxLoss)}</span></span>
            </div>
            {nearWarn && (
              <div className="mt-2 inline-flex items-center gap-1 rounded-[6px] bg-[var(--warn-bg)] text-[var(--warn-text)] px-2 py-0.5 text-[11px] font-medium">⚠ {nearWarn}</div>
            )}
          </>
        ) : (
          <div className="mt-2 text-[12px] text-[var(--muted)]">Awaiting live quotes…</div>
        )}
      </Card>

      {/* Premium & value */}
      <Card title="Premium &amp; value">
        <div className="mt-1">
          <KV label={creditNeg ? "Net debit paid" : "Net credit recd"} value={netCredit != null ? formatInr(Math.abs(netCredit)) : "—"} tone={netCredit != null ? (creditNeg ? -1 : 1) : null} />
          {metrics && (
            <>
              <KV label="Time value" value={money(metrics.timeValue)} tone={metrics.timeValue} />
              <KV label="Intrinsic" value={money(metrics.intrinsicValue)} />
              <KV label="Reward / Risk" value={metrics.rewardRisk != null ? `${metrics.rewardRisk.toFixed(2)}×` : "—"} />
            </>
          )}
        </div>
      </Card>

      {/* Margin */}
      <Card title="Margin used">
        <div className="mt-1 font-['Space_Grotesk'] font-bold tabular-nums text-[22px] text-[var(--strong)]">{margin != null ? formatInr(margin) : "—"}</div>
        {marginSrc && <div className="text-[11.5px] text-[var(--muted)]">{marginSrc}</div>}
        {(target != null || stop != null) && (
          <div className="mt-2 pt-2 border-t border-[var(--divider)]">
            {target != null && (
              <KV label="Profit target" value={`+${formatInr(target)}${targetPct != null ? ` · +${targetPct.toFixed(1)}%` : ""}`} tone={1} />
            )}
            {stop != null && (
              <KV label="Stop loss" value={`−${formatInr(Math.abs(stop))}${stopPct != null ? ` · −${stopPct.toFixed(1)}%` : ""}`} tone={-1} />
            )}
          </div>
        )}
      </Card>
    </div>
  );
}
