import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "@shared/api/client";
import { formatInr } from "@shared/lib/format";
import type { LiveRunSnapshot } from "@shared/types";
import { Sparkline } from "../components/charts";
import { useLiveFeed } from "../feed";

const hhmm = (d: Date | null) =>
  d ? d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false }) : "—";

function upnl(r: LiveRunSnapshot): number {
  return (r.positions ?? []).reduce((s, p) => s + p.unrealized_pnl, 0);
}

/** Per-card sparkline off the run's greeks-history P&L series (options runs; equity runs
 * degrade to a flat line). Lazy + cached — one tiny fetch per visible card. */
function CardSpark({ runId, up }: { runId: number; up: boolean }) {
  const { data } = useQuery({
    queryKey: ["spark", runId],
    queryFn: () => api.liveGreeksHistory(runId),
    staleTime: 120_000,
    retry: false,
  });
  const values = (data?.points ?? []).map((p) => p.pnl).filter((v): v is number => v != null);
  return <Sparkline values={values.slice(-60)} up={up} />;
}

/** 02 · Live — paper/real segmented, equity card, strategy cards (per the design). */
export default function LiveScreen() {
  const { runs, updatedAt } = useLiveFeed();
  const [mode, setMode] = useState<"paper" | "real">("paper");
  const { data: alerts } = useQuery({
    queryKey: ["alerts"], queryFn: () => api.alertsList(1), refetchInterval: 120_000,
    retry: false,
  });

  const shown = useMemo(
    () => runs
      .filter((r) =>
        ((r as { mode?: string }).mode ?? "PAPER").toUpperCase()
          === (mode === "real" ? "LIVE" : "PAPER"))
      // In-position runs first — they're the ones that need eyes.
      .sort((a, b) => (b.positions?.length ?? 0) - (a.positions?.length ?? 0)),
    [runs, mode],
  );
  const totals = useMemo(() => {
    const equity = shown.reduce((s, r) => s + (r.equity ?? 0), 0);
    const realized = shown.reduce((s, r) => s + (r.realized_pnl ?? 0), 0);
    const unreal = shown.reduce((s, r) => s + upnl(r), 0);
    const open = shown.reduce((s, r) => s + (r.positions?.length ?? 0), 0);
    return { equity, realized, unreal, open, pnl: realized + unreal };
  }, [shown]);

  return (
    <div className="screen" style={{ paddingTop: "calc(14px + env(safe-area-inset-top))" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div className="page-title">Live</div>
        <Link to="/alerts" aria-label="Alerts" style={{
          position: "relative", width: 44, height: 44, borderRadius: 14,
          background: "var(--chip)", display: "flex", alignItems: "center",
          justifyContent: "center", color: "var(--strong)", textDecoration: "none",
        }}>
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            strokeWidth="2.1" strokeLinecap="round" strokeLinejoin="round">
            <path d="M18 8a6 6 0 0 0-12 0c0 7-3 8-3 8h18s-3-1-3-8M10.3 21a1.9 1.9 0 0 0 3.4 0" />
          </svg>
          {(alerts?.unread ?? 0) > 0 && (
            <span style={{
              position: "absolute", top: 6, right: 6, minWidth: 16, height: 16,
              borderRadius: 8, background: "var(--danger)", color: "#fff",
              fontSize: 10, fontWeight: 800, display: "flex", alignItems: "center",
              justifyContent: "center", padding: "0 4px",
            }}>{alerts!.unread}</span>
          )}
        </Link>
      </div>

      <div className="seg-track" style={{ marginTop: 12 }}>
        {(["paper", "real"] as const).map((m) => (
          <button key={m} className={`seg-item ${mode === m ? "active" : ""}`}
            onClick={() => setMode(m)}>
            {m === "paper" ? "Paper" : "Real"}
          </button>
        ))}
      </div>

      <div className="card" style={{ marginTop: 13, borderRadius: 22, padding: "18px 22px" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div>
            <div style={{ fontSize: 13, fontWeight: 700, color: "var(--muted)" }}>
              Total equity
            </div>
            <div className="sg" style={{ fontWeight: 700, fontSize: 32 }}>
              {formatInr(totals.equity)}
            </div>
          </div>
          <span style={{
            background: totals.pnl >= 0 ? "var(--ok-bg)" : "var(--danger-bg)",
            color: totals.pnl >= 0 ? "var(--ok-text)" : "var(--danger)",
            fontSize: 13, fontWeight: 800, borderRadius: 10, padding: "6px 10px",
          }}>
            {totals.pnl >= 0 ? "+" : ""}{formatInr(totals.pnl)} P&L
          </span>
        </div>
        <div className="divider" style={{ margin: "14px 0 12px" }} />
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8 }}>
          {([["Realized", totals.realized, true], ["Unrealized", totals.unreal, true],
            ["Open", totals.open, false]] as const).map(([label, v, signed]) => (
            <div key={label}>
              <div className="label">{label}</div>
              <div className="sg" style={{
                fontWeight: 700, fontSize: 18,
                color: signed
                  ? ((v as number) >= 0 ? "var(--pos)" : "var(--danger)")
                  : "var(--strong)",
              }}>
                {signed ? formatInr(v as number) : String(v)}
              </div>
            </div>
          ))}
        </div>
      </div>

      <div style={{
        display: "flex", justifyContent: "space-between", alignItems: "baseline",
        margin: "18px 2px 10px",
      }}>
        <span className="sg" style={{ fontWeight: 700, fontSize: 14, color: "var(--muted)" }}>
          {mode.toUpperCase()} DEPLOYMENTS · {shown.length}
        </span>
        <span style={{ fontSize: 12, color: "var(--faint)" }}>updated {hhmm(updatedAt)}</span>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 11 }}>
        {shown.map((r) => {
          const u = upnl(r);
          const paused = r.auto === false;
          return (
            <Link key={r.run_id} to={`/live/${r.run_id}`} className="card"
              style={{ textDecoration: "none", color: "inherit", display: "block" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <span className="sg" style={{
                  fontWeight: 700, fontSize: 16.5, flex: 1, overflow: "hidden",
                  textOverflow: "ellipsis", whiteSpace: "nowrap",
                }}>{r.name}</span>
                <span className={`chip ${paused ? "warn" : "ok"}`}>
                  {paused ? "PAUSED" : "RUNNING"}
                </span>
                {(r.positions?.length ?? 0) > 0 ? (
                  <span className="chip" style={{ background: "var(--pos)", color: "#fff" }}>
                    ● {r.positions!.length} OPEN
                  </span>
                ) : (
                  <span className="chip">FLAT</span>
                )}
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
                  stroke="var(--faint)" strokeWidth="2.4" strokeLinecap="round">
                  <path d="M9 6l6 6-6 6" />
                </svg>
              </div>
              <div style={{ display: "flex", alignItems: "flex-end", gap: 16, marginTop: 12 }}>
                <div style={{ flex: 1 }}>
                  <div className="label">Unrealized</div>
                  <div className="sg" style={{
                    fontWeight: 700, fontSize: 21,
                    color: u >= 0 ? "var(--pos)" : "var(--danger)",
                  }}>{formatInr(u)}</div>
                </div>
                <div style={{ flex: 1 }}>
                  <div className="label">Realized</div>
                  <div className="sg" style={{
                    fontWeight: 700, fontSize: 15,
                    color: (r.realized_pnl ?? 0) >= 0 ? "var(--pos)" : "var(--danger)",
                  }}>{formatInr(r.realized_pnl ?? 0)}</div>
                </div>
                <div style={{ textAlign: "right" }}>
                  <CardSpark runId={r.run_id} up={u + (r.realized_pnl ?? 0) >= 0} />
                  <div style={{ fontSize: 11, color: "var(--faint)" }}>
                    {r.positions?.length ?? 0} open
                  </div>
                </div>
              </div>
            </Link>
          );
        })}
        {!shown.length && (
          <div className="card" style={{ color: "var(--muted)", fontSize: 14 }}>
            No {mode} deployments running.
          </div>
        )}
      </div>
    </div>
  );
}
