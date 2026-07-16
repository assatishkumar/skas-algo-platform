import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "@shared/api/client";
import { formatInr } from "@shared/lib/format";
import { reconstructCycles } from "@shared/lib/optionCycles";
import { buildLivePayoff, computeMetrics, effectiveSpot, type LiveLeg } from "@shared/lib/payoff";
import { buildRoundTrips } from "@shared/lib/roundtrips";
import { formatOptionSymbol, parseOptionSymbol } from "@shared/lib/symbol";
import type { LivePosition, LiveRunSnapshot } from "@shared/types";
import { HistoryPanel } from "../components/charts";

const fmtDay = new Intl.DateTimeFormat("en-GB", {
  day: "2-digit", month: "short", year: "2-digit",
});
const day = (iso?: string | null) => (iso ? fmtDay.format(new Date(iso.slice(0, 10))) : "—");
const sign = (v: number) => ({ color: v >= 0 ? "var(--pos)" : "var(--danger)" });

function Section({ children, style }: {
  children: React.ReactNode; style?: React.CSSProperties;
}) {
  return (
    <div className="card" style={{ borderRadius: 21, marginTop: 13, ...style }}>{children}</div>
  );
}

function Grid2({ items }: { items: [string, React.ReactNode][] }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px 10px" }}>
      {items.map(([label, node]) => (
        <div key={label}>
          <div className="label">{label}</div>
          <div className="sg" style={{ fontWeight: 700, fontSize: 15.5 }}>{node}</div>
        </div>
      ))}
    </div>
  );
}

/** 03 · Deployment Detail — the design's sections + pinned Pause / Square-off bar. */
export default function DeploymentDetailScreen() {
  const { id } = useParams();
  const runId = Number(id);
  const qc = useQueryClient();

  const { data: run } = useQuery({
    queryKey: ["run", runId], queryFn: () => api.liveGet(runId), refetchInterval: 30_000,
  });
  const { data: hist } = useQuery({
    queryKey: ["hist", runId], queryFn: () => api.liveGreeksHistory(runId),
    refetchInterval: 60_000, retry: false,
  });
  const { data: tradesData } = useQuery({
    queryKey: ["trades", runId], queryFn: () => api.liveTrades(runId),
    refetchInterval: 60_000, retry: false,
  });

  const positions = useMemo(() => run?.positions ?? [], [run]);
  // Dominant-underlying book (a mixed cp_ratio run can't share one spot axis) + an
  // effective spot: `underlying_spot` is the PRIMARY underlying's, wrong for the other book.
  const { legs, expiry } = useMemo(() => {
    const groups = new Map<string, { legs: LiveLeg[]; positions: LivePosition[] }>();
    for (const p of positions) {
      const o = parseOptionSymbol(p.symbol);
      if (!o) continue;
      const g = groups.get(o.underlying) ?? { legs: [], positions: [] };
      g.legs.push({ strike: o.strike, right: o.right, direction: p.direction ?? 1,
        units: p.units, entry: p.avg_price, ltp: p.ltp });
      g.positions.push(p);
      groups.set(o.underlying, g);
    }
    const [top] = [...groups.values()].sort((a, b) => b.legs.length - a.legs.length);
    if (!top) return { legs: [] as LiveLeg[], expiry: null as string | null };
    const es = top.positions.map((p) => parseOptionSymbol(p.symbol)?.expiry)
      .filter(Boolean) as string[];
    return { legs: top.legs, expiry: es.sort()[0] ?? null };
  }, [positions]);
  const spot = useMemo(
    () => (legs.length ? effectiveSpot(legs, run?.underlying_spot ?? null)
      : run?.underlying_spot ?? null),
    [legs, run?.underlying_spot],
  );
  const metrics = useMemo(
    () => (legs.length && spot && expiry
      ? computeMetrics(legs, spot, expiry, undefined, run?.net_iv ?? undefined) : null),
    [legs, spot, expiry, run?.net_iv],
  );
  const payoff = useMemo(
    () => (legs.length && spot && expiry ? buildLivePayoff(legs, spot, expiry) : null),
    [legs, spot, expiry],
  );
  const cycles = useMemo(() => reconstructCycles(tradesData?.trades ?? []), [tradesData]);

  const pause = useMutation({
    mutationFn: () => api.liveSetControls(runId, { auto: !(run?.auto ?? true) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["run", runId] }),
  });
  const flatten = useMutation({
    mutationFn: () => api.liveFlatten(runId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["run", runId] }),
  });

  if (!run) {
    return (
      <div className="screen" style={{ paddingTop: 60, color: "var(--muted)" }}>Loading…</div>
    );
  }

  const unreal = positions.reduce((s, p) => s + p.unrealized_pnl, 0);
  const realized = run.realized_pnl ?? 0;
  const overall = unreal + realized;
  const netGamma = positions.reduce((s, p) => s + (p.gamma ?? 0) * p.units, 0);
  const netTheta = positions.reduce((s, p) => s + (p.theta ?? 0) * p.units, 0);
  const netVega = positions.reduce((s, p) => s + (p.vega ?? 0) * p.units, 0);
  const mode = ((run as LiveRunSnapshot & { mode?: string }).mode ?? "PAPER").toUpperCase();
  const paused = run.auto === false;
  // Equity books get equity metrics — options concepts (credit/greeks/payoff) are
  // meaningless for a stock basket (the desktop shows deployed/parts/positions instead).
  const isEquity = (run.instrument_class ?? "").toUpperCase() !== "DERIV";
  const histPts = hist?.points ?? [];
  // History P&L headline: the last RECORDED point (a flat book's live unrealized is 0 —
  // the recorded series still shows how the cycle actually ran).
  const lastHistPnl = [...histPts].reverse().find((p) => p.pnl != null)?.pnl ?? unreal;
  const cyclesDesc = [...cycles].reverse();          // newest first
  const closed = cycles.filter((c) => !c.open);
  const cycleWins = closed.filter((c) => c.realized_pnl > 0).length;
  const trades = tradesData?.trades ?? [];
  const exits = [...trades].reverse().slice(0, 20);  // newest first, capped
  // Equity round-trips (the Analysis-page pairing): entry lot → its exits, P&L, hold days.
  const roundTrips = isEquity ? buildRoundTrips(trades) : [];
  const rtWins = roundTrips.filter((r) => r.won).length;
  const rtDesc = [...roundTrips].sort((a, b) => (a.exitDate < b.exitDate ? 1 : -1));

  return (
    <div className="screen" style={{
      paddingTop: "calc(10px + env(safe-area-inset-top))",
      paddingBottom: "calc(150px + env(safe-area-inset-bottom))",
    }}>
      <Link to="/live" style={{
        color: "var(--accent-deep)", textDecoration: "none", fontWeight: 700,
        fontSize: 16, minHeight: 44, display: "inline-flex", alignItems: "center",
      }}>‹ Live</Link>
      <div className="sg" style={{ fontWeight: 700, fontSize: 24, wordBreak: "break-all" }}>
        {run.name}
      </div>
      <div style={{ display: "flex", gap: 6, marginTop: 8, flexWrap: "wrap" }}>
        <span className={`chip ${paused ? "warn" : "ok"}`}>{paused ? "PAUSED" : "RUNNING"}</span>
        <span className={`chip ${mode === "LIVE" ? "danger" : ""}`}>
          {mode === "LIVE" ? "REAL" : "PAPER"}
        </span>
        {run.underlying && (
          <span className="chip">{run.underlying}{expiry ? ` · ${day(expiry)}` : ""}</span>
        )}
        {run.strategy_alert && <span className="chip warn">DATA ⚠</span>}
      </div>

      <Section>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div className="sg" style={{ fontWeight: 700, fontSize: 33, ...sign(overall) }}>
            {formatInr(overall)}
          </div>
          {run.profit_target_amt != null && (
            <span className="chip ok" style={{ fontSize: 11.5 }}>
              target +{formatInr(run.profit_target_amt)}
            </span>
          )}
        </div>
        <div style={{ marginTop: 14 }}>
          <Grid2 items={[
            ["Realized", <span key="r" style={sign(realized)}>{formatInr(realized)}</span>],
            ["Unrealized", <span key="u" style={sign(unreal)}>{formatInr(unreal)}</span>],
            [isEquity ? "Positions" : "Open legs", String(positions.length)],
            isEquity
              ? ["Deployed", run.invested != null ? formatInr(run.invested) : "—"]
              : ["Margin", run.margin_used != null ? formatInr(run.margin_used) : "—"],
          ]} />
        </div>
      </Section>

      {/* Equity capital card — the desktop's deployed/parts/positions/cash view. */}
      {isEquity && (
        <Section>
          <Grid2 items={[
            ["Equity", run.equity != null ? formatInr(run.equity) : "—"],
            ["Cash", run.cash != null ? formatInr(run.cash) : "—"],
            ["Parts deployed", `${run.open_lots ?? 0}${run.parts_total ? ` / ${run.parts_total}` : ""}`],
            ["Taxes paid", run.realized_taxes != null ? formatInr(run.realized_taxes) : "—"],
          ]} />
        </Section>
      )}

      {/* Round-trip stats — the Analysis page's pairing, condensed. */}
      {isEquity && roundTrips.length > 0 && (
        <Section>
          <span className="label">Round trips · {roundTrips.length} closed</span>
          <div style={{ marginTop: 12 }}>
            <Grid2 items={[
              ["Win rate", `${Math.round((100 * rtWins) / roundTrips.length)}% (${rtWins}/${roundTrips.length})`],
              ["Realized total", <span key="t" style={sign(roundTrips.reduce((s, r) => s + r.pnl, 0))}>
                {formatInr(roundTrips.reduce((s, r) => s + r.pnl, 0))}</span>],
              ["Avg hold", `${Math.round(roundTrips.reduce((s, r) => s + r.holdingDays, 0) / roundTrips.length)}d`],
              ["Avg P&L / trip", <span key="a" style={sign(roundTrips.reduce((s, r) => s + r.pnl, 0) / roundTrips.length)}>
                {formatInr(roundTrips.reduce((s, r) => s + r.pnl, 0) / roundTrips.length)}</span>],
            ]} />
          </div>
          <div className="divider" style={{ margin: "12px 0 0" }} />
          {rtDesc.slice(0, 10).map((r, i) => (
            <div key={i} style={{
              padding: "9px 0", borderBottom: i < Math.min(rtDesc.length, 10) - 1
                ? "1px solid var(--divider)" : "none",
              display: "flex", alignItems: "center", gap: 10, fontSize: 12.5,
            }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div className="sg" style={{ fontWeight: 700, fontSize: 13.5 }}>{r.symbol}</div>
                <div style={{ color: "var(--faint)", fontSize: 11 }}>
                  {day(r.entryDate)} → {day(r.exitDate)} · {r.holdingDays}d
                </div>
              </div>
              <div style={{ textAlign: "right" }}>
                <div className="sg" style={{ fontWeight: 700, fontSize: 13.5, ...sign(r.pnl) }}>
                  {formatInr(r.pnl)}
                </div>
                <div style={{ fontSize: 11, ...sign(r.pnlPct) }}>
                  {r.pnlPct >= 0 ? "+" : ""}{r.pnlPct.toFixed(1)}%
                </div>
              </div>
            </div>
          ))}
        </Section>
      )}

      {metrics && spot != null && (
        <Section>
          <div style={{ display: "flex", justifyContent: "space-between" }}>
            <span className="label">Risk envelope · at expiry</span>
            {metrics.pop != null && (
              <span className="sg" style={{ fontWeight: 700, fontSize: 13 }}>
                POP {Math.round(metrics.pop * 100)}%
              </span>
            )}
          </div>
          <BreakevenBand spot={spot} breakevens={metrics.breakevens} />
          <div className="divider" style={{ margin: "12px 0 10px" }} />
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13.5 }}>
            <span>max profit{" "}
              <b className="sg" style={{ color: "var(--pos)" }}>
                {metrics.maxProfitUnlimited ? "Unlimited" : formatInr(metrics.maxProfit)}
              </b>
            </span>
            <span>max loss{" "}
              <b className="sg" style={{ color: "var(--danger)" }}>
                {metrics.maxLossUnlimited ? "Unlimited" : formatInr(metrics.maxLoss)}
              </b>
            </span>
          </div>
        </Section>
      )}

      {!isEquity && positions.length > 0 && (
        <Section>
          <Grid2 items={[
            ["Net credit recd", run.net_credit != null
              ? <span key="nc" style={sign(run.net_credit)}>{formatInr(run.net_credit)}</span>
              : "—"],
            ["Time value", metrics
              ? <span key="tv" style={sign(metrics.timeValue)}>{formatInr(metrics.timeValue)}</span>
              : "—"],
            ["Intrinsic", metrics
              ? <span key="iv" style={sign(metrics.intrinsicValue)}>
                  {formatInr(metrics.intrinsicValue)}</span>
              : "—"],
            ["Reward / risk", metrics?.rewardRisk != null
              ? `${metrics.rewardRisk.toFixed(2)}×` : "—"],
          ]} />
          <div className="divider" style={{ margin: "12px 0 10px" }} />
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13 }}>
            <span className="label">Margin used</span>
            <span className="sg" style={{ fontWeight: 700 }}>
              {run.margin_used != null ? formatInr(run.margin_used) : "—"}
              <span style={{ color: "var(--faint)", fontWeight: 600 }}>
                {" "}· {run.margin_source ?? "model"}
              </span>
            </span>
          </div>
        </Section>
      )}

      {(run.exit_rules?.length ?? 0) > 0 && (
        <div style={{
          marginTop: 13, background: "var(--warn-bg)", color: "var(--warn-text)",
          borderRadius: 14, padding: "12px 14px", fontSize: 12, fontWeight: 600,
          lineHeight: 1.5,
        }}>
          {run.exit_rules!.join(" · ")}
        </div>
      )}

      {!isEquity && positions.length > 0 && (
        <Section>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "14px 10px" }}>
            <GreekCell label="Δ delta" value={(run.net_delta ?? 0).toFixed(1)}
              sub={spot != null ? `Δ-cash ${formatInr((run.net_delta ?? 0) * spot)}` : ""} />
            <GreekCell label="Γ gamma" value={netGamma.toFixed(2)}
              sub="Δ shift per 100-pt move" />
            <GreekCell label="Θ / day" value={formatInr(netTheta)}
              color={netTheta >= 0 ? "var(--pos)" : "var(--danger)"}
              sub={netTheta >= 0 ? "decay earned while flat" : "decay paid"} />
            <GreekCell label="Vega / 1% IV" value={formatInr(netVega)}
              color={netVega >= 0 ? "var(--pos)" : "var(--danger)"}
              sub={netVega >= 0 ? "gains if IV rises" : "loses if IV rises"} />
            <GreekCell label="IV book avg" color="var(--warn-text)"
              value={run.net_iv != null ? `${(run.net_iv * 100).toFixed(1)}%` : "—"}
              sub="premium-weighted" />
            <GreekCell label="Profit target"
              value={run.profit_target_amt != null
                ? `+${formatInr(run.profit_target_amt)}` : "—"}
              sub={run.stop_loss_amt != null ? `stop −${formatInr(run.stop_loss_amt)}` : ""} />
          </div>
        </Section>
      )}

      {payoff && metrics && (
        <Section>
          <span className="label">Payoff at expiry</span>
          <PayoffSvg payoff={payoff} />
          <div style={{ display: "flex", gap: 8, marginTop: 8, flexWrap: "wrap" }}>
            {metrics.breakevens.map((b) => (
              <span key={b} className="sg" style={{
                border: "1.5px solid var(--accent)", color: "var(--accent-deep)",
                borderRadius: 999, padding: "4px 10px", fontWeight: 700, fontSize: 12,
              }}>BE {Math.round(b).toLocaleString("en-IN")}</span>
            ))}
          </div>
        </Section>
      )}

      {histPts.length > 1 && (
        <Section>
          <span className="label" style={{ display: "block", marginBottom: 6 }}>
            History · this cycle
          </span>
          <HistoryPanel label="P&L" value={formatInr(lastHistPnl)}
            color={lastHistPnl >= 0 ? "var(--pos)" : "var(--danger)"}
            fill={lastHistPnl >= 0 ? "var(--ok-bg)" : "var(--danger-bg)"}
            values={histPts.map((p) => p.pnl ?? null)} height={92} />
          <HistoryPanel label="Net Δ · position" value={(run.net_delta ?? 0).toFixed(1)}
            color="var(--opt-text)" zeroLine
            values={histPts.map((p) => p.net_delta ?? null)} height={70} />
          <HistoryPanel label="IV % · book avg"
            value={run.net_iv != null ? `${(run.net_iv * 100).toFixed(1)}%` : "—"}
            color="var(--warn-text)"
            values={histPts.map((p) => (p.net_iv != null ? p.net_iv * 100 : null))}
            height={56} />
          <div style={{ fontSize: 11, color: "var(--faint)", marginTop: 4 }}>
            market hours only · sampled ~1/min
          </div>
        </Section>
      )}

      {positions.length > 0 && (
        <Section style={{ padding: "6px 0 0" }}>
          <div style={{ padding: "10px 18px 8px" }}>
            <span className="label">
              Positions · {positions.length} {isEquity ? "stocks" : "legs"}
            </span>
          </div>
          {positions.map((p) => <LegRow key={p.symbol} p={p} />)}
          <div style={{
            background: "var(--seg)", padding: "11px 18px",
            display: "flex", justifyContent: "space-between",
            borderRadius: "0 0 20px 20px", fontSize: 13, fontWeight: 700,
          }}>
            <span>Net · {positions.length} legs</span>
            <span className="sg" style={sign(unreal)}>{formatInr(unreal)}</span>
          </div>
        </Section>
      )}

      {cycles.length > 0 && (
        <Section style={{ padding: "17px 0 0" }}>
          <div style={{ padding: "0 18px" }}>
            <div style={{ display: "flex", justifyContent: "space-between", flexWrap: "wrap" }}>
              <span className="label">Cycle P&L · {cycles.length} cycle{cycles.length > 1 ? "s" : ""}</span>
              {closed.length > 0 && (
                <span style={{ fontSize: 12, color: "var(--muted)", fontWeight: 700 }}>
                  {closed.length} closed · win {Math.round(100 * cycleWins / closed.length)}%
                  {" · "}total{" "}
                  <span className="sg" style={sign(closed.reduce((s, c) => s + c.realized_pnl, 0))}>
                    {formatInr(closed.reduce((s, c) => s + c.realized_pnl, 0))}
                  </span>
                </span>
              )}
            </div>
          </div>
          <div style={{ marginTop: 10 }}>
            {cyclesDesc.map((c, i) => {
              const move = c.entry_spot && c.exit_spot
                ? (100 * (c.exit_spot - c.entry_spot)) / c.entry_spot : null;
              return (
                <div key={i} style={{
                  padding: "10px 18px", borderTop: "1px solid var(--divider)",
                  display: "flex", alignItems: "center", gap: 10, fontSize: 12.5,
                }}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontWeight: 700 }}>
                      {day(c.entry_date)}
                      <span style={{ color: "var(--faint)" }}>
                        {" "}→ {c.open ? "open" : day(c.exit_date)}
                      </span>
                    </div>
                    <div style={{ color: "var(--faint)", fontSize: 11.5 }}>
                      {c.legs.length} leg{c.legs.length > 1 ? "s" : ""}
                      {c.entry_spot != null ? ` · in ${Math.round(c.entry_spot).toLocaleString("en-IN")}` : ""}
                      {move != null ? ` · move ` : ""}
                      {move != null && (
                        <span style={sign(move)}>{move >= 0 ? "+" : ""}{move.toFixed(1)}%</span>
                      )}
                    </div>
                  </div>
                  <span className="sg" style={{ fontWeight: 700, fontSize: 14, ...sign(c.realized_pnl) }}>
                    {formatInr(c.realized_pnl)}
                  </span>
                  <span className={`chip ${c.open ? "ok" : c.realized_pnl > 0 ? "ok" : "danger"}`}>
                    {c.open ? "OPEN" : c.realized_pnl > 0 ? "WIN" : "LOSS"}
                  </span>
                </div>
              );
            })}
          </div>
          <div style={{ height: 8 }} />
        </Section>
      )}

      {exits.length > 0 && (
        <Section style={{ padding: "17px 0 0" }}>
          <div style={{ padding: "0 18px", display: "flex", justifyContent: "space-between" }}>
            <span className="label">Trades & exits</span>
            <span style={{ fontSize: 11.5, color: "var(--faint)" }}>
              latest {exits.length} of {trades.length}
            </span>
          </div>
          <div style={{ marginTop: 10 }}>
            {exits.map((t, i) => (
              <div key={i} style={{
                padding: "9px 18px", borderTop: "1px solid var(--divider)",
                display: "flex", alignItems: "center", gap: 10, fontSize: 12.5,
              }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="sg" style={{
                    fontWeight: 700, fontSize: 13.5, overflow: "hidden",
                    textOverflow: "ellipsis", whiteSpace: "nowrap",
                  }}>{formatOptionSymbol(t.ticker)}</div>
                  <div style={{ color: "var(--faint)", fontSize: 11 }}>
                    {t.date}{t.tag ? ` · ${t.tag}` : ""}
                  </div>
                </div>
                <span className={`chip ${["SELL", "SHORT"].includes(t.action) ? "danger" : "ok"}`}>
                  {t.action}
                </span>
                <div style={{ textAlign: "right" }}>
                  <div className="sg" style={{ fontWeight: 700, fontSize: 13 }}>
                    ₹{Number(t.price).toFixed(2)}
                  </div>
                  {t.profit != null && (
                    <div className="sg" style={{
                      fontSize: 12, fontWeight: 700, ...sign(Number(t.profit)),
                    }}>{formatInr(Number(t.profit))}</div>
                  )}
                </div>
              </div>
            ))}
          </div>
          <div style={{ height: 8 }} />
        </Section>
      )}

      {(pause.error || flatten.error) && (
        <div style={{ marginTop: 10, fontSize: 13, color: "var(--danger)", fontWeight: 700 }}>
          {String(((pause.error ?? flatten.error) as Error)?.message ?? "action failed")}
        </div>
      )}

      <div style={{
        position: "fixed", left: 0, right: 0, bottom: 0, zIndex: 50,
        background: "var(--tab)", backdropFilter: "blur(12px)",
        WebkitBackdropFilter: "blur(12px)", borderTop: "1px solid var(--border)",
      }}>
        <div style={{
          display: "flex", gap: 10, maxWidth: 560, margin: "0 auto",
          padding: "12px 20px calc(12px + env(safe-area-inset-bottom))",
        }}>
          <button
            onClick={() => {
              const q = paused
                ? "Resume the auto decision loop?"
                : "Pause the auto decision loop? Open positions stay as they are.";
              if (window.confirm(q)) pause.mutate();
            }}
            disabled={pause.isPending}
            style={{
              flex: 1, background: "var(--chip)", color: "var(--strong)",
              borderRadius: 16, padding: 15, fontWeight: 800, fontSize: 15.5,
            }}>
            {paused ? "▶ Resume" : "⏸ Pause"}
          </button>
          <button
            onClick={() => {
              const warn = mode === "LIVE" ? " — this is a REAL-money run" : "";
              if (window.confirm(
                `SQUARE OFF ALL positions of "${run.name}"${warn}? This cannot be undone.`)) {
                flatten.mutate();
              }
            }}
            disabled={flatten.isPending || positions.length === 0}
            style={{
              flex: 1.35, background: "var(--danger-bg)", color: "var(--danger)",
              borderRadius: 16, padding: 15, fontWeight: 800, fontSize: 15.5,
              opacity: positions.length === 0 ? 0.5 : 1,
            }}>
            ⏹ Square off all
          </button>
        </div>
      </div>
    </div>
  );
}

function GreekCell({ label, value, sub, color }: {
  label: string; value: string; sub?: string; color?: string;
}) {
  return (
    <div>
      <div className="label">{label}</div>
      <div className="sg" style={{
        fontWeight: 700, fontSize: 17, color: color ?? "var(--strong)",
      }}>{value}</div>
      {sub ? <div style={{ fontSize: 11.5, color: "var(--faint)" }}>{sub}</div> : null}
    </div>
  );
}

/** Risk band: danger-tinted track, green segment between breakevens, dark spot marker. */
function BreakevenBand({ spot, breakevens }: { spot: number; breakevens: number[] }) {
  const bes = [...breakevens].sort((a, b) => a - b);
  const refs = bes.length ? [spot, ...bes] : [spot];
  const lo = Math.min(...refs) * 0.985;
  const hi = Math.max(...refs) * 1.015;
  const x = (v: number) => `${(((v - lo) / Math.max(1e-9, hi - lo)) * 100).toFixed(2)}%`;
  const beL = bes[0];
  const beR = bes[bes.length - 1];
  return (
    <div style={{ marginTop: 12 }}>
      <div style={{
        position: "relative", height: 10, borderRadius: 5, background: "var(--danger-bg)",
      }}>
        {bes.length >= 2 && (
          <div style={{
            position: "absolute", top: 0, bottom: 0, left: x(beL),
            width: `calc(${x(beR)} - ${x(beL)})`,
            background: "var(--ok-bg)", borderLeft: "2px solid var(--pos)",
            borderRight: "2px solid var(--pos)",
          }} />
        )}
        <div style={{
          position: "absolute", top: -3, width: 3, height: 16, background: "var(--strong)",
          left: `calc(${x(spot)} - 1.5px)`, borderRadius: 2,
        }} />
      </div>
      <div style={{
        display: "flex", justifyContent: "space-between", marginTop: 6,
        fontSize: 11.5, color: "var(--muted)",
      }}>
        <span>{beL != null ? `BE ${Math.round(beL).toLocaleString("en-IN")}` : ""}</span>
        <span className="sg" style={{ fontWeight: 700, color: "var(--strong)" }}>
          spot {Math.round(spot).toLocaleString("en-IN")}
        </span>
        <span>
          {beR != null && beR !== beL ? `BE ${Math.round(beR).toLocaleString("en-IN")}` : ""}
        </span>
      </div>
    </div>
  );
}

function PayoffSvg({ payoff }: { payoff: NonNullable<ReturnType<typeof buildLivePayoff>> }) {
  const w = 320;
  const h = 150;
  const pad = 6;
  const xs = payoff.data.map((d) => d.spot);
  const ys = payoff.data.flatMap((d) => [d.expiry, d.now]);
  const x0 = Math.min(...xs);
  const x1 = Math.max(...xs);
  const y0 = Math.min(...ys);
  const y1 = Math.max(...ys);
  const sx = (v: number) => pad + ((v - x0) / Math.max(1e-9, x1 - x0)) * (w - 2 * pad);
  const sy = (v: number) => h - pad - ((v - y0) / Math.max(1e-9, y1 - y0)) * (h - 2 * pad);
  const path = (key: "expiry" | "now") => payoff.data
    .map((d, i) => `${i === 0 ? "M" : "L"}${sx(d.spot).toFixed(1)},${sy(d[key]).toFixed(1)}`)
    .join(" ");
  const zero = sy(0);
  return (
    <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none"
      style={{ marginTop: 8 }}>
      <line x1={pad} y1={zero} x2={w - pad} y2={zero} stroke="var(--divider)" />
      <line x1={sx(payoff.spot)} y1={pad} x2={sx(payoff.spot)} y2={h - pad}
        stroke="var(--strong)" strokeWidth="1.4" strokeDasharray="2 3" opacity="0.6" />
      <path d={path("expiry")} fill="none" stroke="var(--faint)" strokeWidth="1.8" />
      <path d={path("now")} fill="none" stroke="var(--accent)" strokeWidth="1.8"
        strokeDasharray="5 4" />
    </svg>
  );
}

function LegRow({ p }: { p: LivePosition }) {
  const short = (p.direction ?? 1) < 0;
  const isOption = parseOptionSymbol(p.symbol) != null;
  const changePct = p.ltp != null && p.avg_price
    ? (100 * (p.ltp - p.avg_price)) / p.avg_price : null;
  return (
    <div style={{ padding: "11px 18px", borderTop: "1px solid var(--divider)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span className={`chip ${short ? "danger" : "ok"}`}
          style={{ width: 44, justifyContent: "center" }}>
          {short ? "SELL" : "BUY"}
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="sg" style={{
            fontWeight: 700, fontSize: 15, whiteSpace: "nowrap", overflow: "hidden",
            textOverflow: "ellipsis",
          }}>{formatOptionSymbol(p.symbol)}</div>
          <div style={{ fontSize: 11.5, color: "var(--faint)" }}>
            {isOption ? `${p.lots} × ${p.lot_size}` : `${p.units} sh`}
            {p.entry_date ? ` · in ${day(p.entry_date)}` : ""}
          </div>
        </div>
        <div style={{ textAlign: "right" }}>
          <div className="sg" style={{
            fontWeight: 700, fontSize: 15, ...sign(p.unrealized_pnl),
          }}>{formatInr(p.unrealized_pnl)}</div>
          <div style={{ fontSize: 11.5, color: "var(--faint)" }}>
            ₹{p.avg_price.toFixed(2)} → {p.ltp != null ? `₹${p.ltp.toFixed(2)}` : "—"}
          </div>
        </div>
      </div>
      {isOption ? (
        <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
          <span className="chip" style={{ textTransform: "none" }}>
            Δ <b style={{ color: (p.delta ?? 0) >= 0 ? "var(--pos)" : "var(--danger)" }}>
              {p.delta != null ? p.delta.toFixed(2) : "—"}</b>
          </span>
          <span className="chip" style={{ textTransform: "none" }}>
            Θ-day <b style={{ color: (p.theta ?? 0) >= 0 ? "var(--pos)" : "var(--danger)" }}>
              {p.theta != null ? formatInr(p.theta * p.units) : "—"}</b>
          </span>
          <span className="chip" style={{ textTransform: "none" }}>
            IV <b style={{ color: "var(--warn-text)" }}>
              {p.iv != null ? `${(p.iv * 100).toFixed(1)}%` : "—"}</b>
          </span>
        </div>
      ) : changePct != null ? (
        <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
          <span className="chip" style={{ textTransform: "none" }}>
            move <b style={sign(changePct)}>
              {changePct >= 0 ? "+" : ""}{changePct.toFixed(1)}%</b>
          </span>
          <span className="chip" style={{ textTransform: "none" }}>
            value <b>{formatInr((p.ltp ?? p.avg_price) * p.units)}</b>
          </span>
        </div>
      ) : null}
    </div>
  );
}
