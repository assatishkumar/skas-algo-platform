import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import { KebabMenu } from "../components/redesign";
import type { DonchianBasket, DonchianBasketLeg, DonchianBasketName, Trade } from "../types";

// ───────────────────────────────────────────────────────── format + path helpers
const inr = (n?: number | null) => (n == null ? "—" : Math.round(Math.abs(n)).toLocaleString("en-IN"));
const signed = (n?: number | null) => (n == null ? "—" : (n >= 0 ? "+₹" : "−₹") + inr(n));
const rupee = (n?: number | null) => (n == null ? "—" : "₹" + inr(n));
function compact(n?: number | null): string {
  if (n == null) return "—";
  const a = Math.abs(n);
  if (a >= 1e7) return `₹${(n / 1e7).toFixed(2)} Cr`;
  if (a >= 1e5) return `₹${(n / 1e5).toFixed(1)}L`;
  return rupee(n);
}
const posCls = (n: number) => (n >= 0 ? "text-[var(--pos)]" : "text-[var(--danger)]");

/** Smoothed cubic sparkline (matches the prototype). */
function spark(W: number, H: number, vals: number[]) {
  const pad = 2, n = vals.length;
  const mn = Math.min(...vals), mx = Math.max(...vals), rng = mx - mn || 1;
  const x = (i: number) => pad + (i * (W - 2 * pad)) / (n - 1);
  const y = (v: number) => H - pad - ((v - mn) * (H - 2 * pad)) / rng;
  let d = `M${x(0).toFixed(1)} ${y(vals[0]).toFixed(1)}`;
  for (let i = 1; i < n; i++) {
    const cx = (x(i - 1) + x(i)) / 2;
    d += ` C${cx.toFixed(1)} ${y(vals[i - 1]).toFixed(1)} ${cx.toFixed(1)} ${y(vals[i]).toFixed(1)} ${x(i).toFixed(1)} ${y(vals[i]).toFixed(1)}`;
  }
  return { line: d, area: `${d} L${x(n - 1).toFixed(1)} ${H} L${x(0).toFixed(1)} ${H} Z` };
}

/** Synthetic per-name sparkline (real per-name history isn't sampled) — trend follows MTM sign. */
function nameSpark(name: string, mtm: number, closed: boolean) {
  let seed = 0; for (let i = 0; i < name.length; i++) seed += name.charCodeAt(i);
  const dir = mtm >= 0 ? 1 : -1;
  const v: number[] = [];
  for (let i = 0; i < 12; i++) v.push(closed ? 50 + Math.sin(seed + i) * 3 - i * 0.4 : 50 + dir * i * 1.6 + Math.sin(seed + i * 0.9) * 5);
  return spark(70, 28, v);
}

/** Line+area path over a {x,value} series mapped into a viewBox; y includes 0. */
function curve(W: number, H: number, vals: number[]) {
  const n = vals.length, pad = 4;
  let mn = Math.min(...vals, 0), mx = Math.max(...vals, 0); if (mx - mn < 1) mx = mn + 1;
  const px = (i: number) => pad + (i * (W - 2 * pad)) / (n - 1);
  const py = (v: number) => H - pad - ((v - mn) * (H - 2 * pad)) / (mx - mn);
  let line = `M${px(0).toFixed(1)} ${py(vals[0]).toFixed(1)}`;
  for (let i = 1; i < n; i++) line += ` L${px(i).toFixed(1)} ${py(vals[i]).toFixed(1)}`;
  return { line, area: `${line} L${px(n - 1).toFixed(1)} ${H} L${px(0).toFixed(1)} ${H} Z`, py };
}

// ───────────────────────────────────────────────────────── per-name derivations
const structChip = (s: string): [string, string] =>
  s === "strangle" ? ["var(--opt-bg)", "var(--opt-text)"]
    : s === "CE-only" || s === "PE-only" ? ["var(--warn-bg)", "var(--warn-text)"]
    : ["var(--chip)", "var(--muted)"];
const isClosed = (n: DonchianBasketName) => n.status === "closed" || n.status === "settled";
const capturedPct = (n: DonchianBasketName) =>
  isClosed(n) ? 100 : Math.max(0, Math.min(100, Math.round(((n.mtm || 0) / (n.credit || 1)) * 100)));
const capBar = (n: DonchianBasketName) => {
  if (isClosed(n)) return "var(--faint)";
  const c = capturedPct(n);
  return c >= 60 ? "var(--pos)" : c >= 25 ? "var(--accent)" : "var(--warn-text)";
};

/** Per-name short-strangle payoff at expiry (credit − ITM intrinsic), −15%…+15%; + breakevens. */
function namePayoff(n: DonchianBasketName) {
  const openLegs = n.legs.filter((l) => l.open);
  const ce = openLegs.find((l) => l.right === "CE");
  const pe = openLegs.find((l) => l.right === "PE");
  const units = n.units || (openLegs[0]?.units ?? 1);
  const credPerUnit = (n.credit || 0) / (units || 1);
  const spot = n.spot || 0;
  const N = 49, lo = -0.15, hi = 0.15, xs: number[] = [], vs: number[] = [];
  for (let i = 0; i < N; i++) {
    const x = lo + ((hi - lo) * i) / (N - 1);
    const S = spot * (1 + x);
    let v = credPerUnit;
    if (ce) v -= Math.max(0, S - ce.strike);
    if (pe) v -= Math.max(0, pe.strike - S);
    xs.push(x); vs.push(v);
  }
  const c = curve(460, 150, vs);
  let beLow: number | null = null, beHigh: number | null = null;
  for (let i = 1; i < N; i++) {
    if (vs[i - 1] < 0 && vs[i] >= 0) beLow = xs[i];
    if (vs[i - 1] >= 0 && vs[i] < 0) beHigh = xs[i];
  }
  const fmt = (x: number) => (x >= 0 ? "+" : "") + (x * 100).toFixed(1) + "%";
  const be = beLow != null && beHigh != null ? `BE ${fmt(beLow)} / ${fmt(beHigh)}`
    : beLow != null ? `BE ${fmt(beLow)}` : beHigh != null ? `BE ${fmt(beHigh)}` : "in profit across range";
  return { line: c.line, area: c.area, zeroY: c.py(0).toFixed(1), curX: (4 + (24 * (460 - 8)) / 48).toFixed(1), be };
}

const legMoneyness = (l: DonchianBasketLeg, spot: number) =>
  l.right === "CE" ? spot > l.strike : spot < l.strike;

// ───────────────────────────────────────────────────────── components
function Chip({ children, bg = "var(--chip)", color = "var(--chip-text)", mono }: { children: React.ReactNode; bg?: string; color?: string; mono?: boolean }) {
  return <span className={`px-[9px] py-[3px] rounded-[7px] text-[11.5px] font-bold ${mono ? "font-['Space_Grotesk']" : ""}`} style={{ background: bg, color }}>{children}</span>;
}

function Kpi({ label, sub, children, wide }: { label: React.ReactNode; sub?: React.ReactNode; children: React.ReactNode; wide?: boolean }) {
  return (
    <div className="rounded-[16px] border border-[var(--border)] bg-[var(--card)] px-5 py-[18px]">
      <div className="text-[12.5px] font-bold text-[var(--muted)] mb-[9px] flex items-baseline justify-between gap-2">{label}</div>
      <div className={`font-['Space_Grotesk'] font-bold tabular-nums leading-none ${wide ? "text-[28px]" : "text-[24px]"}`}>{children}</div>
      {sub && <div className="text-[11.5px] font-semibold text-[var(--faint)] mt-[9px]">{sub}</div>}
    </div>
  );
}

/** Buffer-to-stop gauge: red→track track, current-MTM marker + zero tick. */
function Gauge({ combined, stop, maxGain }: { combined: number; stop: number; maxGain: number }) {
  const span = maxGain + stop || 1;
  const marker = Math.max(0, Math.min(100, ((combined + stop) / span) * 100));
  const zero = Math.max(0, Math.min(100, (stop / span) * 100));
  return (
    <div className="relative h-2 rounded-[5px]" style={{ background: "linear-gradient(90deg, var(--danger) 0%, var(--track) 30%, var(--track) 100%)" }}>
      <div className="absolute -top-[3px] w-[3px] h-[14px] rounded-[2px] bg-[var(--strong)]" style={{ left: `${marker}%` }} />
      <div className="absolute -top-[3px] w-[1.5px] h-[14px] bg-[var(--faint)]" style={{ left: `${zero}%` }} />
    </div>
  );
}

function NameCard({ n, realized, onClick }: { n: DonchianBasketName; realized: number; onClick: () => void }) {
  const closed = isClosed(n);
  // Open name whose legs have no live mark yet (e.g. on cache fallback) — unrealized is UNKNOWN, not ₹0.
  const noMark = !closed && !n.legs.some((l) => l.open && l.mark != null);
  const [sb, sc] = structChip(n.struct);
  const sp = nameSpark(n.symbol, n.mtm || 0, closed);
  const spk = closed ? "var(--faint)" : posCls(n.mtm || 0) === "text-[var(--pos)]" ? "var(--pos)" : "var(--danger)";
  const cap = capturedPct(n);
  return (
    <div onClick={onClick} className="rounded-[16px] border border-[var(--border)] bg-[var(--card)] px-[17px] py-4 cursor-pointer hover:border-[var(--accent)] transition-colors">
      <div className="flex items-center gap-2 mb-3">
        <span className="w-2 h-2 rounded-full" style={{ background: closed ? "var(--faint)" : "var(--pos)" }} />
        <span className="font-['Space_Grotesk'] font-bold text-[15px] text-[var(--strong)]">{n.symbol}</span>
        <span className="ml-auto px-2 py-[2px] rounded-[6px] text-[10px] font-bold" style={{ background: sb, color: sc }}>{closed ? "closed" : n.struct}</span>
      </div>
      <div className="flex items-end justify-between mb-3">
        <div>
          <div className="text-[11px] font-semibold text-[var(--muted)] mb-[3px]">Unrealized</div>
          <div className={`font-['Space_Grotesk'] font-bold text-[19px] tabular-nums ${closed || noMark ? "text-[var(--faint)]" : posCls(n.mtm || 0)}`} title={noMark ? "No live quote — reconnect to refresh" : undefined}>{closed || noMark ? "—" : signed(n.mtm)}</div>
        </div>
        <svg viewBox="0 0 70 28" preserveAspectRatio="none" className="w-[70px] h-7">
          <path d={sp.area} style={{ fill: spk, fillOpacity: 0.13 }} />
          <path d={sp.line} style={{ fill: "none", stroke: spk, strokeWidth: 2, strokeLinecap: "round" }} />
        </svg>
      </div>
      <div className="flex items-center gap-[9px] mb-[11px]">
        <span className="flex-1 h-[6px] rounded-[4px] bg-[var(--track)] overflow-hidden">
          <span className="block h-full" style={{ width: noMark ? "0%" : `${cap}%`, background: capBar(n) }} />
        </span>
        <span className="text-[11px] font-bold text-[var(--muted)] tabular-nums">{closed ? "settled" : noMark ? "—" : `${cap}%`}</span>
      </div>
      <div className="flex items-center justify-between pt-[11px] border-t border-[var(--divider)] text-[11.5px]">
        <span className="text-[var(--muted)] font-semibold">{n.lots ?? 1} lot{(n.lots ?? 1) === 1 ? "" : "s"}</span>
        <span className="text-[var(--muted)] font-semibold">realized <strong className="font-['Space_Grotesk'] font-bold" style={{ color: realized > 0 ? "var(--pos)" : realized < 0 ? "var(--danger)" : "var(--muted)" }}>{realized === 0 ? "₹0" : signed(realized)}</strong></span>
        <span className="font-bold" style={{ color: n.flip_count > 0 ? "var(--warn-text)" : "var(--faint)" }}>{n.flip_count} flip{n.flip_count === 1 ? "" : "s"}</span>
      </div>
    </div>
  );
}

const ENTRY_ACTIONS = new Set(["BUY", "SHORT"]);
const EXIT_ACTIONS = new Set(["SELL", "COVER", "SETTLE"]);
const fmtStrike = (s: number) => (Number.isInteger(s) ? String(s) : String(s));

function NameDrawer({ n, basket, trades, onClose }: { n: DonchianBasketName; basket: DonchianBasket; trades: Trade[]; onClose: () => void }) {
  const closed = isClosed(n);
  const [sb, sc] = structChip(n.struct);
  const spot = n.spot || 0;
  const openLegs = n.legs.filter((l) => l.open).length;
  const totalLegs = n.legs.length;
  const legsSub = closed ? `${totalLegs} legs · all settled` : `${openLegs} open · ${totalLegs} total`;
  const cap = capturedPct(n);

  // entered/exited timestamps + flip timeline from the trade log, keyed by this name's tickers.
  const meta = useMemo(() => {
    const m = new Map<string, { in?: string; out?: string }>();
    const flips: { date: string; action: string; leg: string; units: number; price: number; pnl: number | null }[] = [];
    for (const t of trades) {
      const p = (t.ticker || "").split("|");
      if (p.length !== 4 || p[0] !== n.symbol) continue;
      const e = m.get(t.ticker) ?? {};
      if (ENTRY_ACTIONS.has(t.action) && !e.in) e.in = t.date;
      if (EXIT_ACTIONS.has(t.action)) e.out = t.date;
      m.set(t.ticker, e);
      if (ENTRY_ACTIONS.has(t.action) || EXIT_ACTIONS.has(t.action)) {
        flips.push({ date: t.date, action: t.action, leg: `${p[2]} ${p[3]}`, units: t.units, price: t.price, pnl: EXIT_ACTIONS.has(t.action) ? t.profit : null });
      }
    }
    return { m, flips };
  }, [trades, n.symbol]);

  const realized = meta.flips.reduce((s, f) => s + (f.pnl || 0), 0); // = Σ the timeline's COVER P&L
  const noMark = !closed && !n.legs.some((l) => l.open && l.mark != null);
  const po = namePayoff(n);
  const tile = (label: string, value: React.ReactNode, color?: string) => (
    <div className="rounded-[12px] bg-[var(--stat)] px-[13px] py-3">
      <div className="text-[10.5px] font-semibold text-[var(--muted)] mb-1">{label}</div>
      <div className="font-['Space_Grotesk'] font-semibold text-[15px] tabular-nums" style={color ? { color } : undefined}>{value}</div>
    </div>
  );

  return (
    <>
      <div onClick={onClose} className="fixed inset-0 z-[50] bg-[var(--overlay)]" />
      <div className="fixed top-0 right-0 bottom-0 z-[51] w-[540px] max-w-full bg-[var(--card)] border-l border-[var(--border)] overflow-y-auto"
        style={{ boxShadow: "-16px 0 50px rgba(0,0,0,.22)", animation: "drawerIn .22s ease" }}>
        <div className="px-[26px] py-6">
          {/* header */}
          <div className="flex items-start gap-3 mb-[18px]">
            <div>
              <div className="flex items-center gap-[10px]">
                <span className="w-[10px] h-[10px] rounded-full" style={{ background: closed ? "var(--faint)" : "var(--pos)" }} />
                <span className="font-['Space_Grotesk'] font-bold text-[22px] text-[var(--strong)]">{n.symbol}</span>
                <span className="px-[9px] py-[3px] rounded-[7px] text-[11px] font-bold" style={{ background: sb, color: sc }}>{closed ? "closed" : n.struct}</span>
              </div>
              <div className="text-[12.5px] text-[var(--muted)] mt-[7px]">
                spot <strong className="text-[var(--strong)] font-['Space_Grotesk']">{spot.toLocaleString("en-IN")}</strong> ·{" "}
                <strong className="text-[var(--strong)] font-['Space_Grotesk']">{n.lots ?? 1} lot{(n.lots ?? 1) === 1 ? "" : "s"}</strong> of {(n.lot_size ?? 0).toLocaleString("en-IN")} · {legsSub}
              </div>
            </div>
            <span onClick={onClose} className="ml-auto w-[34px] h-[34px] rounded-[10px] bg-[var(--chip)] flex items-center justify-center text-[var(--muted)] text-[17px] cursor-pointer">✕</span>
          </div>

          {/* KPI tiles */}
          <div className="grid grid-cols-4 gap-[10px] mb-5">
            {tile("Unrealized", closed || noMark ? "—" : signed(n.mtm), closed || noMark ? "var(--faint)" : (n.mtm || 0) >= 0 ? "var(--pos)" : "var(--danger)")}
            {tile("Realized", realized === 0 ? "₹0" : signed(realized), realized > 0 ? "var(--pos)" : realized < 0 ? "var(--danger)" : "var(--muted)")}
            {tile("Credit", rupee(n.credit))}
            {tile("Captured", closed ? "settled" : `${cap}%`)}
          </div>

          {/* legs */}
          <div className="font-['Space_Grotesk'] font-bold text-[13px] text-[var(--strong)] mb-[10px]">Legs <span className="text-[11.5px] font-semibold text-[var(--faint)]">{legsSub}</span></div>
          <div className="border border-[var(--border)] rounded-[13px] overflow-hidden mb-5">
            {n.legs.map((l, i) => {
              const covered = !l.open;
              const flipIn = l.state === "flip-open" || l.state === "flip-covered";
              const itm = legMoneyness(l, spot);
              const mtm = Math.round(((l.entry || 0) - (l.mark || 0)) * l.units);
              const sym = `${n.symbol}|${basket.expiry ?? ""}|${fmtStrike(l.strike)}|${l.right}`;
              const t = meta.m.get(sym);
              const [lb, lc] = l.right === "CE" ? ["var(--opt-bg)", "var(--opt-text)"] : ["var(--ok-bg)", "var(--ok-text)"];
              return (
                <div key={i} className="grid items-start gap-[11px] px-[14px] py-[13px] border-b border-[var(--divider)] last:border-b-0" style={{ gridTemplateColumns: "84px 1fr auto", opacity: covered ? 0.62 : 1 }}>
                  <span className="px-2 py-[3px] rounded-[6px] text-[11px] font-bold text-center mt-[1px]" style={{ background: lb, color: lc }}>{l.side}</span>
                  <span className="min-w-0">
                    <div>
                      <span className="font-['Space_Grotesk'] font-bold text-[13.5px] text-[var(--strong)]">{l.strike}</span>{" "}
                      <span className="text-[12px] text-[var(--muted)] tabular-nums">₹{(l.entry ?? 0).toFixed(2)} → ₹{(l.mark ?? 0).toFixed(2)}</span>{" "}
                      <span className="text-[10.5px] font-bold" style={{ color: covered ? "var(--faint)" : itm ? "var(--danger)" : "var(--pos)" }}>
                        {covered ? "covered" : flipIn ? `flip-in · ${itm ? "ITM" : "OTM"}` : itm ? "ITM ⚠" : "OTM"}
                      </span>
                    </div>
                    <div className="flex items-center gap-2 mt-[5px] text-[10.5px] font-semibold text-[var(--faint)]">
                      {t?.in && <span><span className="text-[var(--pos)]">▸ in</span> {t.in}</span>}
                      {t?.out && <span><span className="text-[var(--danger)]">◂ out</span> {t.out}</span>}
                    </div>
                  </span>
                  <span className="text-right">
                    <span className="font-['Space_Grotesk'] font-semibold text-[13px] tabular-nums" style={{ color: covered ? "var(--faint)" : mtm >= 0 ? "var(--pos)" : "var(--danger)" }}>{covered ? "₹0" : signed(mtm)}</span>
                    <span className="block text-[10px] font-semibold text-[var(--faint)]">{inr(l.units)} · {n.lots ?? 1} lot{(n.lots ?? 1) === 1 ? "" : "s"}</span>
                  </span>
                </div>
              );
            })}
          </div>

          {/* flip timeline */}
          {n.flip_count > 0 && meta.flips.length > 0 && (
            <div className="mb-5">
              <div className="font-['Space_Grotesk'] font-bold text-[13px] text-[var(--strong)] mb-3">Flip timeline</div>
              {meta.flips.map((f, i) => {
                const cover = EXIT_ACTIONS.has(f.action);
                return (
                  <div key={i} className="flex items-start gap-3 pb-[14px]">
                    <div className="flex flex-col items-center pt-[2px]">
                      <span className="w-[9px] h-[9px] rounded-full" style={{ background: cover ? "var(--danger)" : "var(--pos)" }} />
                      <span className="w-[1.5px] flex-1 min-h-[14px] bg-[var(--divider)] mt-[3px]" />
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-[9px]">
                        <span className="font-['Space_Grotesk'] font-bold text-[12px]" style={{ color: cover ? "var(--danger)" : "var(--pos)" }}>{f.action}</span>
                        <span className="text-[12.5px] text-[var(--strong)] font-['Space_Grotesk']">{n.symbol} {f.leg}</span>
                        <span className="ml-auto font-['Space_Grotesk'] font-semibold text-[12px] tabular-nums" style={{ color: f.pnl == null ? "var(--faint)" : f.pnl >= 0 ? "var(--pos)" : "var(--danger)" }}>{f.pnl == null ? "opened" : signed(f.pnl)}</span>
                      </div>
                      <div className="text-[10.5px] font-semibold text-[var(--faint)] mt-[3px]">{f.date} · {inr(f.units)} @ ₹{f.price.toFixed(2)} · flip</div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {/* payoff */}
          <div className="border border-[var(--border)] rounded-[13px] p-4">
            <div className="flex items-center justify-between mb-[10px]">
              <span className="font-['Space_Grotesk'] font-bold text-[13px] text-[var(--strong)]">Payoff at expiry</span>
              <span className="text-[11.5px] font-semibold text-[var(--faint)]">{po.be}</span>
            </div>
            <svg viewBox="0 0 460 150" preserveAspectRatio="none" className="w-full h-[150px] block">
              <line x1="0" y1={po.zeroY} x2="460" y2={po.zeroY} stroke="var(--divider)" strokeWidth="1" strokeDasharray="3 3" />
              <line x1={po.curX} y1="0" x2={po.curX} y2="150" stroke="var(--faint)" strokeWidth="1" strokeDasharray="2 3" />
              <path d={po.area} style={{ fill: "var(--accent)", fillOpacity: 0.1 }} />
              <path d={po.line} style={{ fill: "none", stroke: "var(--accent-deep)", strokeWidth: 2.2 }} />
            </svg>
            <div className="flex justify-between text-[11px] font-bold text-[var(--faint)] mt-[6px]"><span>−15%</span><span>spot {spot.toLocaleString("en-IN")}</span><span>+15%</span></div>
          </div>
        </div>
      </div>
    </>
  );
}

// ───────────────────────────────────────────────────────── page
export default function DonchianLivePage() {
  const { id } = useParams();
  const runId = Number(id);
  const qc = useQueryClient();
  const [filter, setFilter] = useState<"all" | "open" | "closed">("all");
  const [selName, setSelName] = useState<string | null>(null);

  const { data: snap } = useQuery({ queryKey: ["liveSnapshot", runId], queryFn: () => api.liveSnapshot(runId), refetchInterval: 15000 });
  const { data: deps = [] } = useQuery({ queryKey: ["deployments", "all"], queryFn: () => api.liveDeployments() });
  const { data: tradesData } = useQuery({ queryKey: ["liveTrades", runId], queryFn: () => api.liveTrades(runId) });
  const dep = deps.find((d) => d.run_id === runId);
  const trades = tradesData?.trades ?? [];

  // Per-name realized = Σ profit of the name's closing trades — the authoritative record (matches the
  // flip timeline). Preferred over the strategy's realized_by_name, which is 0 for flips booked before
  // that per-name accounting was added (it only fills going forward).
  const realizedByName = useMemo(() => {
    const m: Record<string, number> = {};
    for (const t of trades) {
      const p = (t.ticker || "").split("|");
      if (p.length === 4 && EXIT_ACTIONS.has(t.action)) m[p[0]] = (m[p[0]] || 0) + (t.profit || 0);
    }
    return m;
  }, [trades]);
  const realizedTotal = Object.values(realizedByName).reduce((s, v) => s + v, 0);

  const act = useMutation({
    mutationFn: (fn: () => Promise<unknown>) => fn(),
    onSettled: () => qc.invalidateQueries({ queryKey: ["liveSnapshot", runId] }),
  });

  const basket = snap?.basket;
  const names = basket?.names ?? [];
  const filtered = useMemo(
    () => names.filter((n) => (filter === "all" ? true : filter === "open" ? !isClosed(n) : isClosed(n))),
    [names, filter],
  );
  const openCount = names.filter((n) => !isClosed(n)).length;

  if (snap && snap.strategy_id !== "donchian_strangle_monthly") {
    return (
      <div className="font-['Manrope'] bg-[var(--page)] min-h-[calc(100vh-3.5rem)] text-[var(--strong)]">
        <div className="max-w-[1320px] mx-auto px-8 pt-8 text-[var(--muted)]">
          This detail view is for Donchian Strangle deployments. <Link to="/live" className="text-[var(--accent-deep)] underline">Back to Live</Link>.
        </div>
      </div>
    );
  }

  const payoffPts = basket?.payoff ?? [];
  const bp = payoffPts.length ? curve(560, 130, payoffPts.map((p) => p.expiry_pnl)) : null;
  const bpCurX = payoffPts.length ? (2 + ((payoffPts.findIndex((p) => p.move_pct === 0)) * (560 - 4)) / (payoffPts.length - 1)).toFixed(1) : "280";

  const combined = basket?.combined_mtm ?? 0;
  const stopAmt = basket?.portfolio_stop_amount ?? 0;
  const buffer = basket?.buffer_to_stop ?? 0;
  const netCredit = basket?.net_credit ?? 0;
  const hedge = basket?.hedge;

  return (
    <div className="font-['Manrope'] bg-[var(--page)] min-h-[calc(100vh-3.5rem)] text-[var(--strong)]">
      <div className="max-w-[1320px] mx-auto px-8 pt-6 pb-20">
        {/* breadcrumb */}
        <div className="mb-[18px] text-[13.5px] font-semibold text-[var(--muted)]">
          <Link to="/live" className="hover:text-[var(--strong)]">Live</Link>
          <span className="text-[var(--faint)] mx-[6px]">/</span>
          <span className="text-[var(--strong)] font-bold">{snap?.name ?? "Donchian Strangle"}</span>
        </div>

        {/* header */}
        <div className="flex items-start gap-4 mb-[18px] flex-wrap">
          <div className="min-w-0">
            <div className="flex items-center gap-3 flex-wrap">
              <h1 className="font-['Space_Grotesk'] font-bold text-[26px] text-[var(--strong)] m-0">{snap?.name ?? "Donchian Strangle"}</h1>
              {dep?.status === "active" && (
                <span className="inline-flex items-center gap-[7px] px-3 py-[6px] rounded-full text-[12px] font-bold" style={{ background: "var(--ok-bg)", color: "var(--ok-text)" }}>
                  <span className="w-[7px] h-[7px] rounded-full bg-current animate-pulse" />active · {dep?.mode === "LIVE" ? "live" : "paper"}
                </span>
              )}
            </div>
            <div className="flex items-center gap-2 flex-wrap mt-[11px]">
              <Chip>{(dep?.mode ?? "PAPER").toLowerCase()}</Chip>
              <Chip bg="var(--opt-bg)" color="var(--opt-text)">OPT · NIFTY basket</Chip>
              <Chip mono>donchian_strangle_monthly</Chip>
              {dep?.broker_label && <Chip bg="var(--ok-bg)" color="var(--ok-text)">● {dep.broker_label}{dep.broker_connected ? " · live" : ""}</Chip>}
              <span className="text-[12px] font-semibold text-[var(--faint)]">#{runId}</span>
              {basket?.expiry && <span className="text-[12px] font-semibold text-[var(--faint)]">· settles at expiry {basket.expiry}{basket.dte != null ? ` · ${basket.dte}d left` : ""}</span>}
            </div>
          </div>
          <div className="ml-auto flex items-center gap-[9px] flex-wrap">
            <button onClick={() => act.mutate(() => api.liveRunDecision(runId))} className="inline-flex items-center gap-[7px] px-[15px] py-[9px] rounded-[11px] text-white font-bold text-[13.5px]" style={{ background: "var(--ft)", boxShadow: "0 6px 14px rgba(13,107,79,.24)" }}>▶ Run decision</button>
            <button onClick={() => act.mutate(() => api.liveSetControls(runId, { auto: !(snap?.auto ?? true) }))} className="px-[15px] py-[9px] rounded-[11px] bg-[var(--chip)] text-[var(--strong)] font-bold text-[13.5px]">{snap?.auto === false ? "Resume" : "‖ Pause"}</button>
            <button onClick={() => act.mutate(() => api.liveRefresh(runId))} className="px-[15px] py-[9px] rounded-[11px] bg-[var(--chip)] text-[var(--strong)] font-bold text-[13.5px]">↻ Refresh</button>
            <button onClick={() => { if (confirm("Exit ALL open legs now at live prices?")) act.mutate(() => api.liveFlatten(runId)); }} className="px-[15px] py-[9px] rounded-[11px] border border-[var(--danger)] text-[var(--danger)] font-bold text-[13.5px]">Exit all</button>
            <KebabMenu items={[{ label: "Open report", onClick: () => { window.location.href = `/runs/${runId}`; } }, { label: "All deployments", onClick: () => { window.location.href = "/live"; } }]} />
          </div>
        </div>

        {dep && dep.quote_source === "zerodha" && (dep.on_cache_fallback || dep.quote_error || dep.broker_connected === false) && (
          <div className="mb-[18px] rounded-[12px] bg-[var(--warn-bg)] px-4 py-3 text-[13px] text-[var(--warn-text)] flex items-center gap-3 flex-wrap">
            <span className="font-bold">⚠ Live quotes disconnected</span>
            <span className="text-[var(--warn-text)]/90">— single-stock option premiums aren't cached, so each open name's Unrealized shows "—" until quotes return. The NIFTY hedge and realized P&L are unaffected.{dep.quote_error ? ` (${dep.quote_error})` : ""}</span>
            <button onClick={() => act.mutate(() => api.liveReconnectQuotes(runId))} className="ml-auto rounded-[9px] bg-[var(--ft)] text-white px-3 py-[6px] text-xs font-semibold">Reconnect to live quotes</button>
          </div>
        )}

        {!basket ? (
          <div className="text-[var(--muted)] py-10">Loading basket…</div>
        ) : (
          <>
            {/* hero KPIs */}
            <div className="grid gap-[14px] mb-[22px]" style={{ gridTemplateColumns: "1.25fr 1fr 1fr 1.35fr 1.1fr" }}>
              <Kpi wide label={<>Combined MTM <span className="text-[11px] font-semibold text-[var(--faint)]">· basket + hedge</span></>}
                sub={<>basket <span className={`font-bold ${posCls(basket.basket_mtm ?? 0)}`}>{signed(basket.basket_mtm)}</span> · hedge <span className={`font-bold ${posCls(basket.hedge_mtm ?? 0)}`}>{signed(basket.hedge_mtm)}</span></>}>
                <span className={posCls(combined)}>{signed(combined)}</span>
              </Kpi>
              <Kpi label="Net credit collected" sub={`premium in · ${names.length} names`}><span className="text-[var(--strong)]">{rupee(netCredit)}</span></Kpi>
              <Kpi label="Realized (flips)" sub={`${basket.total_flips ?? 0} flips · ${basket.closed_count ?? 0} names closed`}>
                <span className={posCls(realizedTotal)}>{signed(realizedTotal)}</span>
              </Kpi>
              <Kpi label={<><span>Buffer to portfolio stop</span><span className="text-[11px] font-semibold text-[var(--faint)]">stop −{rupee(stopAmt)} · 2% notional</span></>}>
                <span className="text-[var(--strong)]">{compact(buffer)} <span className="text-[13px] font-semibold text-[var(--faint)] font-['Manrope']">to stop</span></span>
                <div className="mt-3"><Gauge combined={combined} stop={stopAmt} maxGain={netCredit} /></div>
                <div className="flex justify-between text-[10.5px] font-bold text-[var(--faint)] mt-[6px]"><span>STOP</span><span>0</span><span>MAX +{compact(netCredit)}</span></div>
              </Kpi>
              <Kpi label="Notional · short side"
                sub={<>{compact(hedge?.entry_notional)} entry · <span className="text-[var(--warn-text)] font-bold">{hedge && hedge.entry_notional ? Math.round((hedge.current_notional / hedge.entry_notional - 1) * 100) : 0}% drift</span> · margin {compact(snap?.margin_used)}</>}>
                <span className="text-[var(--strong)]">{compact(hedge?.current_notional)}</span>
              </Kpi>
            </div>

            {/* overview band */}
            <div className="grid gap-4 mb-[22px]" style={{ gridTemplateColumns: "1.5fr 1fr 1fr" }}>
              <div className="rounded-[18px] border border-[var(--border)] bg-[var(--card)] px-5 py-[18px]">
                <div className="flex items-center justify-between mb-[10px]"><span className="font-['Space_Grotesk'] font-bold text-[14px] text-[var(--strong)]">Aggregate payoff at expiry</span><span className="text-[11.5px] font-semibold text-[var(--faint)]">basket + hedge</span></div>
                {bp && (
                  <svg viewBox="0 0 560 130" preserveAspectRatio="none" className="w-full h-[130px] block">
                    <line x1="0" y1="43" x2="560" y2="43" stroke="var(--divider)" strokeWidth="1" strokeDasharray="3 4" />
                    <line x1="0" y1="86" x2="560" y2="86" stroke="var(--divider)" strokeWidth="1" strokeDasharray="3 4" />
                    <line x1={bpCurX} y1="0" x2={bpCurX} y2="130" stroke="var(--accent)" strokeWidth="1.4" strokeDasharray="4 4" />
                    <path d={bp.area} style={{ fill: "var(--accent)", fillOpacity: 0.12 }} />
                    <path d={bp.line} style={{ fill: "none", stroke: "var(--accent-deep)", strokeWidth: 2.2, strokeLinejoin: "round" }} />
                  </svg>
                )}
                <div className="flex justify-between text-[10.5px] font-bold text-[var(--faint)] mt-[5px]"><span>−15%</span><span className="text-[var(--accent-deep)]">now · NIFTY {hedge?.spot != null ? Math.round(hedge.spot).toLocaleString("en-IN") : "—"}</span><span>+15%</span></div>
              </div>
              <div className="rounded-[18px] border border-[var(--border)] bg-[var(--card)] px-5 py-[18px]">
                <div className="flex items-center gap-2 mb-3"><span className="font-['Space_Grotesk'] font-bold text-[14px] text-[var(--strong)]">Index hedge</span>{hedge?.lots != null && <span className="px-2 py-[2px] rounded-[6px] text-[10.5px] font-bold" style={{ background: "var(--ok-bg)", color: "var(--ok-text)" }}>{hedge.lots} lots · notional-matched</span>}</div>
                <div className="flex flex-col gap-[9px]">
                  {(hedge?.legs ?? []).map((l, i) => (
                    <div key={i} className="flex justify-between text-[12.5px]"><span className="text-[var(--muted)] font-semibold">BUY {l.right} {l.strike.toLocaleString("en-IN")} <span className="text-[var(--faint)]">({l.otm_pct != null ? l.otm_pct.toFixed(1) : "—"}% OTM)</span></span><span className="text-[var(--strong)] font-['Space_Grotesk'] font-semibold">₹{(l.entry ?? 0).toFixed(1)}→{(l.mark ?? 0).toFixed(1)}</span></div>
                  ))}
                  <div className="flex justify-between text-[12.5px] pt-[9px] border-t border-[var(--divider)]"><span className="text-[var(--muted)] font-semibold">Hedge MTM</span><span className="font-['Space_Grotesk'] font-bold" style={{ color: (hedge?.mtm ?? 0) >= 0 ? "var(--pos)" : "var(--danger)" }}>{signed(hedge?.mtm)}</span></div>
                  <div className="flex justify-between text-[12.5px]"><span className="text-[var(--muted)] font-semibold">Cost / premium</span><span className="text-[var(--warn-text)] font-['Space_Grotesk'] font-bold">{hedge?.cost_pct != null ? `${hedge.cost_pct.toFixed(1)}%` : "—"} · under cap</span></div>
                </div>
              </div>
              <div className="rounded-[18px] border border-[var(--border)] bg-[var(--card)] px-5 py-[18px]">
                <div className="font-['Space_Grotesk'] font-bold text-[14px] text-[var(--strong)] mb-3">Portfolio stop</div>
                <div className={`font-['Space_Grotesk'] font-bold text-[22px] mb-1 ${posCls(combined)}`}>{signed(combined)}</div>
                <div className="text-[11.5px] font-semibold text-[var(--faint)] mb-[14px]">combined MTM · incl. hedge</div>
                <Gauge combined={combined} stop={stopAmt} maxGain={netCredit} />
                <div className="flex justify-between text-[10.5px] font-bold text-[var(--faint)] mt-[7px]"><span className="text-[var(--danger)]">−{compact(stopAmt)} stop</span><span>{compact(buffer)} buffer</span></div>
              </div>
            </div>

            {/* filter row */}
            <div className="flex items-center gap-3 mb-4 flex-wrap">
              <div className="flex gap-[6px]">
                {(["all", "open", "closed"] as const).map((k) => {
                  const on = filter === k;
                  const label = k === "all" ? `All ${names.length}` : k === "open" ? `Open ${openCount}` : `Closed ${names.length - openCount}`;
                  return <span key={k} onClick={() => setFilter(k)} className="px-[15px] py-[7px] rounded-[9px] text-[12.5px] font-bold cursor-pointer border" style={{ background: on ? "var(--accent)" : "var(--ghost)", color: on ? "#fff" : "var(--strong)", borderColor: on ? "var(--accent)" : "var(--border)" }}>{label}</span>;
                })}
              </div>
              <span className="text-[12.5px] font-semibold text-[var(--faint)]">{filtered.length} names · tap a name for full detail</span>
            </div>

            {/* card grid */}
            <div className="grid grid-cols-4 gap-[14px]">
              {filtered.map((n) => <NameCard key={n.symbol} n={n} realized={realizedByName[n.symbol] ?? n.realized ?? 0} onClick={() => setSelName(n.symbol)} />)}
            </div>
          </>
        )}
      </div>

      {selName && basket && (() => {
        const n = names.find((x) => x.symbol === selName);
        return n ? <NameDrawer n={n} basket={basket} trades={trades} onClose={() => setSelName(null)} /> : null;
      })()}
    </div>
  );
}
