import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { formatInr, pct } from "../lib/format";
import type { CycleDetail, CycleDetailEvent, CycleDetailLeg } from "../types";

/** Cycle Detail — the position lifecycle of one options cycle (entry → rolls/hedges → exit),
 *  design_handoff_cycle_detail. Geometry (x = time, y = strike) is computed from the event log
 *  the backend reconstructs; click a flag / event / leg to trace its legs. */

// ---- ladder geometry (viewBox 1120 × 400) ----
const X0 = 50, X1 = 1076, TOP = 44, BOT = 338, AXIS = 352;
const kindColor = (k: string) =>
  k === "entry" ? { bg: "var(--tint)", fg: "var(--accent-deep)" }
  : k === "hedge" ? { bg: "var(--warn-bg)", fg: "var(--warn-text)" }
  : k === "exit" ? { bg: "var(--ok-bg)", fg: "var(--ok-text)" }
  : { bg: "var(--opt-bg)", fg: "var(--opt-text)" };
const signCls = (n: number) => (n >= 0 ? "text-[var(--pos)]" : "text-[var(--danger)]");
const dΔ = (v: number | null) => (v == null ? "—" : (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(1));
const shortDate = (iso: string) =>
  new Date(iso.replace(" ", "T")).toLocaleDateString("en-IN", { day: "numeric", month: "short" });
const hhmm = (iso: string) => (iso.length > 10 ? iso.slice(11, 16) : "");

export default function CycleDetailPage() {
  const { id, index } = useParams();
  const runId = Number(id), idx = Number(index);
  const { data, isLoading, error } = useQuery({
    queryKey: ["cycle-detail", runId, idx],
    queryFn: () => api.cycleDetail(runId, idx),
  });
  const [active, setActive] = useState<string | null>(null);
  const toggle = (evId: string | null) => () => setActive((a) => (a === evId ? null : evId));

  if (isLoading) return <div className="p-8 text-[var(--muted)]">Loading cycle…</div>;
  if (error || !data) return <div className="p-8 text-[var(--danger)]">Couldn't load this cycle.</div>;
  return <CycleDetail m={data} active={active} toggle={toggle} setActive={setActive} />;
}

function CycleDetail({ m, active, toggle, setActive }: {
  m: CycleDetail; active: string | null;
  toggle: (id: string | null) => () => void; setActive: (v: string | null) => void;
}) {
  const geo = useMemo(() => computeGeometry(m), [m]);
  const legInActive = (l: CycleDetailLeg) =>
    !active || l.open_event === active || l.close_event === active;

  // Deterministic: land on the SOURCE page the cycle-date link lives on — the Live page
  // (`/live`, where LiveCyclePanel renders a running deploy's cycle table) for a live cycle,
  // else the run detail. Plain <Link>, independent of browser history.
  const backTo = m.live ? "/live" : `/runs/${m.run_id}`;

  const move = m.underlying_pct ?? 0;
  return (
    <div className="max-w-[1280px] mx-auto px-8 py-6 pb-16">
      {/* breadcrumb + title */}
      <Link to={backTo} className="text-sm font-bold text-[var(--muted)] mb-3 inline-block">
        ← {m.live ? "Live" : "Runs"} · <span className="text-[var(--accent-deep)]">{m.run_name} #{m.run_id}</span> · positions
      </Link>
      <div className="flex items-center gap-3 mb-2 flex-wrap">
        <h1 className="font-[700] text-[26px] font-['Space_Grotesk'] text-[var(--strong)]">
          Cycle {m.index + 1} · {shortDate(m.entered_at)} → {m.exited_at ? shortDate(m.exited_at) : "open"}
        </h1>
        <Chip tone={m.live ? "chip" : m.exit_reason === "target" ? "ok" : m.exit_reason === "stop" ? "danger" : "chip"}>
          {m.live ? "● RUNNING" : (m.exit_reason || "OPEN").toUpperCase() + (m.exit_reason ? " EXIT" : "")}
        </Chip>
        <Chip tone="chip">{m.underlying} · {expiryLabel(m.expiry)}</Chip>
        <Chip tone="chip">{m.legs.length} LEGS · {m.n_rolls + m.n_hedges} ADJ</Chip>
      </div>
      <div className="text-[13.5px] text-[var(--faint)] font-semibold mb-5">
        Entry {shortDate(m.entered_at)} {hhmm(m.entered_at)}
        {m.exited_at && <> · exit {shortDate(m.exited_at)} {hhmm(m.exited_at)}</>}
        {m.entry_spot != null && m.exit_spot != null && <>
          {" "}· spot {m.entry_spot.toLocaleString("en-IN")} → {m.exit_spot.toLocaleString("en-IN")}{" "}
          <span className={signCls(move)}>({pct(move, 1)})</span>
        </>}
        {m.entry_vix != null && m.exit_vix != null &&
          <> · VIX {m.entry_vix.toFixed(1)} → {m.exit_vix.toFixed(1)}</>}
      </div>

      {/* KPI strip */}
      <div className="grid grid-cols-2 md:grid-cols-6 gap-3 mb-5">
        <Kpi label="CYCLE P&L" value={formatInr(m.pnl ?? 0)} cls={signCls(m.pnl ?? 0)} />
        <Kpi label="PREMIUM TRADED" value={formatInr(m.premium_traded)} />
        <Kpi label="DAYS HELD" value={m.days_held != null ? String(Math.round(m.days_held)) : "—"} />
        <Kpi label="ADJUSTMENTS" value={`${m.n_rolls} roll${m.n_rolls === 1 ? "" : "s"}${m.n_hedges ? ` · ${m.n_hedges} hedge` : ""}`} cls="text-[var(--opt-text)]" small />
        <Kpi label="MAX MARGIN" value={m.max_margin != null ? formatInr(m.max_margin) : "—"} />
        <Kpi label="WORST EOD MTM" value={formatInr(m.worst_mtm)} cls="text-[var(--danger)]" />
      </div>

      {/* lifecycle ladder */}
      <div className="bg-[var(--card)] border border-[var(--border)] rounded-[18px] px-6 py-5 mb-[18px]">
        <div className="flex items-baseline gap-3 mb-1 flex-wrap">
          <div className="font-[700] text-[16px] font-['Space_Grotesk'] text-[var(--strong)]">Position lifecycle</div>
          <div className="text-[13px] text-[var(--faint)] font-semibold">
            strikes over time — each bar is a leg's lifespan; arrows are rolls. Click a flag or event to trace its legs.
          </div>
          <span onClick={() => setActive(null)}
            className={`ml-auto text-[12.5px] font-extrabold cursor-pointer ${active ? "text-[var(--accent-deep)]" : "text-[var(--faint)]"}`}>
            {active ? "✕ clear trace" : "nothing traced"}
          </span>
        </div>
        <Ladder m={m} geo={geo} active={active} toggle={toggle} legInActive={legInActive} />
        <MtmStrip m={m} geo={geo} />
        <div className="flex gap-[18px] mt-2.5 text-xs font-bold text-[var(--muted)] flex-wrap">
          <Legend swatch={<span className="inline-block w-[18px] h-2 rounded bg-[var(--opt-text)] align-middle mr-1.5" />}>sell CE</Legend>
          <Legend swatch={<span className="inline-block w-[18px] h-2 rounded bg-[var(--pe)] align-middle mr-1.5" />}>sell PE</Legend>
          <Legend swatch={<span className="inline-block w-[18px] h-2 rounded align-middle mr-1.5" style={{ border: "1.5px dashed var(--opt-text)" }} />}>buy (hedge)</Legend>
          <Legend swatch={<span className="inline-block w-[18px] h-0.5 bg-[var(--faint)] align-middle mr-1.5" />}>spot</Legend>
          <span>◆ hedge added · ⇣ roll</span>
        </div>
      </div>

      {/* timeline + legs table */}
      <div className="grid gap-[18px] items-start" style={{ gridTemplateColumns: "repeat(auto-fit,minmax(460px,1fr))" }}>
        <EventTimeline m={m} active={active} toggle={toggle} />
        <LegsTable m={m} toggle={toggle} legInActive={legInActive} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- geometry
function computeGeometry(m: CycleDetail) {
  const t0 = new Date(m.entered_at.replace(" ", "T")).getTime();
  const tExp = new Date(m.expiry + "T15:30").getTime();
  const span = Math.max(tExp - t0, 1);
  const x = (iso: string | null) => {
    if (!iso) return X1;
    const t = new Date(iso.replace(" ", "T")).getTime();
    return X0 + Math.max(0, Math.min(1, (t - t0) / span)) * (X1 - X0);
  };
  const strikes = m.legs.map((l) => l.strike);
  const spots = m.spot_path.map((p) => p.spot);
  const lo = Math.min(...strikes, ...spots), hi = Math.max(...strikes, ...spots);
  const pad = Math.max((hi - lo) * 0.08, 200);
  const kLo = lo - pad, kHi = hi + pad;
  const y = (k: number) => TOP + ((kHi - k) / Math.max(kHi - kLo, 1)) * (BOT - TOP);
  // gridlines at round strike steps
  const step = niceStep(kHi - kLo);
  const lines: number[] = [];
  for (let k = Math.ceil(kLo / step) * step; k <= kHi; k += step) lines.push(k);
  return { x, y, kLo, kHi, expX: x(m.expiry + "T15:30"), gridStrikes: lines };
}
function niceStep(range: number) {
  const rough = range / 6;
  const pow = Math.pow(10, Math.floor(Math.log10(rough)));
  return [1, 2, 2.5, 5, 10].map((f) => f * pow).find((s) => s >= rough) ?? pow * 10;
}

// ---------------------------------------------------------------- ladder svg
function Ladder({ m, geo, active, toggle, legInActive }: {
  m: CycleDetail; geo: ReturnType<typeof computeGeometry>; active: string | null;
  toggle: (id: string | null) => () => void; legInActive: (l: CycleDetailLeg) => boolean;
}) {
  const { x, y, expX, gridStrikes } = geo;
  const evX: Record<string, number> = {};
  for (const e of m.events) evX[e.id] = x(e.at);
  // same-strike overlap offset (the straddle)
  const yOff: Record<number, number> = {};
  const seen: Record<number, number> = {};
  m.legs.forEach((l) => { yOff[l.ref] = (seen[l.strike] = (seen[l.strike] ?? -1) + 1) * 12 - 0; });
  const spotPts = m.spot_path.map((p) => `${x(p.date + "T15:30").toFixed(0)},${y(p.spot).toFixed(0)}`).join(" ");

  return (
    <svg viewBox="0 0 1120 400" className="w-full block">
      {gridStrikes.map((k) => (
        <g key={k}>
          <line x1={X0} y1={y(k)} x2={X1} y2={y(k)} stroke="var(--divider)" strokeWidth={1} />
          <text x={X0 - 4} y={y(k) + 3.5} textAnchor="end" fontSize={10.5} fontWeight={700} fill="var(--faint)">{k.toLocaleString("en-IN")}</text>
        </g>
      ))}
      <line x1={expX} y1={30} x2={expX} y2={AXIS} stroke="var(--faint)" strokeWidth={1.2} strokeDasharray="4 4" />
      <text x={expX + 4} y={44} fontSize={10.5} fontWeight={800} fill="var(--faint)">EXP</text>
      {/* spot path */}
      <polyline points={spotPts} fill="none" stroke="var(--faint)" strokeWidth={1.6} opacity={0.75} />
      {m.entry_spot != null && <text x={X0 + 4} y={y(m.entry_spot) - 5} fontSize={10.5} fontWeight={700} fill="var(--muted)">spot {m.entry_spot.toLocaleString("en-IN")}</text>}
      {m.exit_spot != null && <text x={X1 - 34} y={y(m.exit_spot) - 5} fontSize={10.5} fontWeight={700} fill="var(--muted)">{m.exit_spot.toLocaleString("en-IN")}</text>}

      {/* roll / hedge connectors */}
      {m.events.filter((e) => e.kind === "roll" || e.kind === "hedge").map((e) => {
        const ex = evX[e.id];
        return e.opened.map((op, i) => {
          const cl = e.closed[0];
          if (!cl) return null;
          const y1 = y(cl.strike), y2 = y(op.strike), up = y2 < y1;
          const isHedge = op.side === "long";
          if (isHedge) return <path key={`h${e.id}${i}`} d={`M${ex},${y2 - 7} l6,7 -6,7 -6,-7 Z`} fill="var(--pe)" />;
          return (
            <g key={`r${e.id}${i}`}>
              <line x1={ex} y1={y1} x2={ex} y2={up ? y2 + 7 : y2 - 7} stroke="var(--faint)" strokeWidth={1.4} strokeDasharray="3 3" />
              <path d={up ? `M${ex - 4},${y2 + 7} L${ex + 4},${y2 + 7} L${ex},${y2} Z` : `M${ex - 4},${y2 - 7} L${ex + 4},${y2 - 7} L${ex},${y2} Z`} fill="var(--faint)" />
            </g>
          );
        });
      })}

      {/* leg bars */}
      {m.legs.map((l) => {
        const x1 = x(l.open_ts), x2 = x(l.close_ts), yy = y(l.strike) + yOff[l.ref] - 5;
        const col = l.right === "CE" ? "var(--opt-text)" : "var(--pe)";
        const buy = l.side === "long";
        const op = legInActive(l) ? 1 : 0.18;
        return (
          <g key={l.ref} style={{ cursor: "pointer" }} onClick={toggle(l.open_event)} opacity={op}>
            <rect x={x1} y={yy} width={Math.max(x2 - x1, 3)} height={10} rx={5}
              fill={buy ? "transparent" : col} stroke={buy ? col : "none"} strokeWidth={buy ? 1.6 : 0} strokeDasharray={buy ? "5 4" : "none"} />
            <text x={x1 + 2} y={yy - 4} fontSize={10.5} fontWeight={700} fill={col}>
              {l.side === "long" ? "BUY" : "SELL"} {l.right} {l.strike.toLocaleString("en-IN")}{buy ? " · hedge" : ""}
            </text>
          </g>
        );
      })}

      {/* event flags */}
      {m.events.map((e) => {
        const c = kindColor(e.kind), ex = evX[e.id], on = active === e.id;
        return (
          <g key={e.id} style={{ cursor: "pointer" }} onClick={toggle(e.id)}>
            <rect x={ex - 13} y={8} width={26} height={19} rx={9.5} fill={on ? "var(--accent-deep)" : c.bg} />
            <text x={ex} y={21} textAnchor="middle" fontSize={9.5} fontWeight={800} fill={on ? "#fff" : c.fg}>{e.id}</text>
          </g>
        );
      })}
      <line x1={X0} y1={AXIS} x2={X1} y2={AXIS} stroke="var(--border)" strokeWidth={1.4} />
      <text x={X0} y={370} fontSize={10.5} fontWeight={700} fill="var(--faint)">{shortDate(m.entered_at).toUpperCase()}</text>
      <text x={expX} y={386} textAnchor="middle" fontSize={10.5} fontWeight={700} fill="var(--faint)">{expiryLabel(m.expiry)}</text>
    </svg>
  );
}

function MtmStrip({ m, geo }: { m: CycleDetail; geo: ReturnType<typeof computeGeometry> }) {
  const [hover, setHover] = useState<number | null>(null);
  if (!m.mtm_series.length) return null;
  const vals = m.mtm_series.map((d) => d.value);
  const lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
  const mtmY = (v: number) => 6 + ((hi - v) / Math.max(hi - lo, 1)) * 80;
  const x = geo.x;
  const px = m.mtm_series.map((d) => x(d.date + "T15:30"));
  const pts = m.mtm_series.map((d, i) => `${px[i].toFixed(0)},${mtmY(d.value).toFixed(0)}`).join(" ");
  const worstI = vals.indexOf(Math.min(...vals));
  const worst = m.mtm_series[worstI];
  const last = m.mtm_series[m.mtm_series.length - 1];

  // map the cursor to the nearest EOD point (svg viewBox is 1120 wide, scaled to the element)
  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const sx = ((e.clientX - r.left) / r.width) * 1120;
    let best = 0;
    for (let i = 1; i < px.length; i++) if (Math.abs(px[i] - sx) < Math.abs(px[best] - sx)) best = i;
    setHover(best);
  };
  const hp = hover != null ? m.mtm_series[hover] : null;
  const hx = hover != null ? px[hover] : 0;
  const anchorEnd = hx > 900;   // flip the tooltip left near the right edge so it doesn't clip

  return (
    <div className="border-t border-[var(--divider)] mt-2 pt-3">
      <div className="flex items-baseline gap-2.5 mb-0.5">
        <span className="text-[11px] text-[var(--faint)] font-extrabold tracking-wider">EOD MTM · GROSS</span>
        <span className="text-xs text-[var(--faint)] font-semibold">
          {hp ? <>{shortDate(hp.date)} · <span className={signCls(hp.value)}>{formatInr(hp.value)}</span></>
              : "dips are what triggered the rolls — hover the line"}
        </span>
        <span className={`ml-auto font-[700] text-[13px] font-['Space_Grotesk'] tabular-nums ${signCls(m.pnl ?? 0)}`}>exit {formatInr(m.pnl ?? 0)}</span>
      </div>
      <svg viewBox="0 0 1120 92" className="w-full block" style={{ cursor: "crosshair" }}
        onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        <line x1={X0} y1={mtmY(0)} x2={X1} y2={mtmY(0)} stroke="var(--divider)" strokeWidth={1} />
        {m.events.filter((e) => e.kind === "roll" || e.kind === "hedge").map((e) => (
          <line key={e.id} x1={x(e.at)} y1={6} x2={x(e.at)} y2={86} stroke="var(--divider)" strokeWidth={1} strokeDasharray="2 3" />
        ))}
        <polyline points={pts} fill="none" stroke="var(--accent-deep)" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" />
        {worst && worst.value < 0 && !hp && <>
          <circle cx={px[worstI]} cy={mtmY(worst.value)} r={3} fill="var(--danger)" />
          <text x={px[worstI] + 8} y={mtmY(worst.value) + 4} fontSize={10} fontWeight={700} fill="var(--danger)">{formatInr(worst.value)} · {shortDate(worst.date)}</text>
        </>}
        {last && !hp && <circle cx={px[px.length - 1]} cy={mtmY(last.value)} r={3.5} fill="var(--pos)" />}
        {/* hover crosshair + tooltip */}
        {hp && <>
          <line x1={hx} y1={2} x2={hx} y2={90} stroke="var(--accent-deep)" strokeWidth={1} strokeDasharray="3 3" opacity={0.6} />
          <circle cx={hx} cy={mtmY(hp.value)} r={4} fill="var(--accent-deep)" stroke="var(--card)" strokeWidth={1.5} />
          <g transform={`translate(${anchorEnd ? hx - 8 : hx + 8}, ${Math.max(mtmY(hp.value) - 20, 6)})`}>
            <rect x={anchorEnd ? -128 : 0} y={0} width={128} height={30} rx={6} fill="var(--card)" stroke="var(--border)" strokeWidth={1} />
            <text x={anchorEnd ? -120 : 8} y={13} fontSize={9.5} fontWeight={800} fill="var(--faint)" letterSpacing="0.05em">{shortDate(hp.date).toUpperCase()}</text>
            <text x={anchorEnd ? -120 : 8} y={25} fontSize={11.5} fontWeight={700} className="font-['Space_Grotesk']"
              fill={hp.value >= 0 ? "var(--pos)" : "var(--danger)"}>{formatInr(hp.value)}</text>
          </g>
        </>}
      </svg>
    </div>
  );
}

// ---------------------------------------------------------------- event timeline
function EventTimeline({ m, active, toggle }: {
  m: CycleDetail; active: string | null; toggle: (id: string | null) => () => void;
}) {
  return (
    <div>
      <div className="font-[700] text-sm font-['Space_Grotesk'] text-[var(--muted)] tracking-wide mb-3">WHAT HAPPENED, IN ORDER</div>
      <div className="relative">
        <div className="absolute left-[18px] top-2.5 bottom-2.5 w-0.5 bg-[var(--divider)]" />
        <div className="flex flex-col gap-3">
          {m.events.map((e) => {
            const c = kindColor(e.kind), on = active === e.id;
            return (
              <div key={e.id} onClick={toggle(e.id)} className="flex gap-3 cursor-pointer relative">
                <div className="flex-none w-[38px] h-[38px] rounded-xl flex items-center justify-center font-[700] text-[13px] font-['Space_Grotesk'] z-[1]"
                  style={{ backgroundColor: c.bg, color: c.fg }}>{e.id}</div>
                <div className="flex-1 min-w-0 bg-[var(--card)] rounded-[14px] px-[15px] py-[13px]"
                  style={{ border: `1.5px solid ${on ? "var(--accent)" : "var(--border)"}` }}>
                  <div className="flex items-baseline gap-2">
                    <span className="font-[700] text-[14.5px] font-['Space_Grotesk'] text-[var(--strong)]">{eventTitle(e)}</span>
                    <span className="ml-auto flex-none text-xs text-[var(--faint)] font-bold tabular-nums">{shortDate(e.at)} · {hhmm(e.at)}</span>
                  </div>
                  <div className="text-[12.5px] text-[var(--muted)] font-semibold leading-[1.55] mt-1.5">{e.reason}</div>
                  <div className="flex flex-col gap-[5px] mt-2.5">
                    {e.closed.map((l, i) => <EvLine key={`c${i}`} tag="CLOSE" l={l} realized />)}
                    {e.opened.map((l, i) => <EvLine key={`o${i}`} tag={l.side === "long" ? "HEDGE" : "OPEN"} l={l} />)}
                  </div>
                  <div className="flex gap-3 mt-2.5 border-t border-[var(--divider)] pt-2.5 text-[11.5px] font-bold text-[var(--faint)] tabular-nums">
                    <span>spot {e.spot?.toLocaleString("en-IN") ?? "—"}</span>
                    <span>net Δ {dΔ(e.net_delta)}</span>
                    <span className={`ml-auto ${e.realized_so_far > 0 ? "text-[var(--pos)]" : ""}`}>realized so far {formatInr(e.realized_so_far)}</span>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function EvLine({ tag, l, realized }: { tag: string; l: CycleDetail["events"][0]["opened"][0]; realized?: boolean }) {
  const tone = tag === "CLOSE" ? { bg: "var(--chip)", fg: "var(--chip-text)" }
    : tag === "HEDGE" ? { bg: "var(--warn-bg)", fg: "var(--warn-text)" }
    : { bg: "var(--tint)", fg: "var(--accent-deep)" };
  const cash = l.cashflow != null ? ` · ${l.side === "long" ? "debit" : "credit"} ${formatInr(Math.abs(l.cashflow))}` : "";
  return (
    <div className="flex items-center gap-2.5">
      <span className="flex-none w-[46px] text-center py-[2.5px] rounded-md font-extrabold text-[9.5px] tracking-wide" style={{ backgroundColor: tone.bg, color: tone.fg }}>{tag}</span>
      <span className="font-[700] text-[13px] font-['Space_Grotesk'] text-[var(--strong)] tabular-nums">{l.side === "long" ? "BUY" : "SELL"} {l.right} {l.strike.toLocaleString("en-IN")}</span>
      <span className="text-[12.5px] text-[var(--muted)] font-semibold tabular-nums">@{l.price?.toFixed(2)}{cash}</span>
      {realized && l.realized != null && <span className={`ml-auto font-[700] text-[12.5px] font-['Space_Grotesk'] tabular-nums ${signCls(l.realized)}`}>{formatInr(l.realized)}</span>}
    </div>
  );
}

// ---------------------------------------------------------------- legs table
function LegsTable({ m, toggle, legInActive }: {
  m: CycleDetail; toggle: (id: string | null) => () => void;
  legInActive: (l: CycleDetailLeg) => boolean;
}) {
  // Chronological by when the leg was OPENED (the timestamp is the source of truth — event
  // ids can repeat across a live campaign); shorts before the hedge within the same instant.
  const rows = [...m.legs].sort((a, b) =>
    a.open_ts.localeCompare(b.open_ts)
    || (a.side === "long" ? 1 : 0) - (b.side === "long" ? 1 : 0) || a.strike - b.strike);
  const cols = "52px 1.2fr 0.9fr 0.9fr 1.3fr 40px 96px";
  const total = m.legs.reduce((s, l) => s + l.pnl, 0);
  return (
    <div className="flex flex-col gap-[18px]">
      <div className="bg-[var(--card)] border border-[var(--border)] rounded-[18px] overflow-x-auto">
        <div className="flex items-baseline gap-2.5 px-5 pt-[18px] pb-3.5">
          <span className="font-[700] text-[16px] font-['Space_Grotesk'] text-[var(--strong)]">Legs · {m.legs.length}</span>
          <span className="text-[12.5px] text-[var(--faint)] font-semibold">opened / closed by event · Δ at open</span>
        </div>
        <div className="grid gap-2.5 px-5 py-2.5 bg-[var(--stat)] border-y border-[var(--divider)] text-[10.5px] font-extrabold tracking-wider text-[var(--faint)]" style={{ gridTemplateColumns: cols }}>
          <span>SIDE</span><span>LEG · Δ</span><span>OPENED</span><span>CLOSED</span><span>ENTRY → EXIT</span><span className="text-right">DAYS</span><span className="text-right">P&L</span>
        </div>
        {rows.map((l) => {
          const oc = l.open_event ? kindColor(m.events.find((e) => e.id === l.open_event)?.kind ?? "roll") : { bg: "var(--chip)", fg: "var(--chip-text)" };
          const cc = l.close_event ? kindColor(m.events.find((e) => e.id === l.close_event)?.kind ?? "roll") : { bg: "var(--chip)", fg: "var(--chip-text)" };
          return (
            <div key={l.ref} onClick={toggle(l.open_event)} className="grid gap-2.5 items-center px-5 py-[11px] border-b border-[var(--divider)] cursor-pointer" style={{ gridTemplateColumns: cols, opacity: legInActive(l) ? 1 : 0.18 }}>
              <span className="text-center py-[2.5px] rounded-md font-extrabold text-[10px]" style={{ backgroundColor: l.side === "long" ? "var(--ok-bg)" : "var(--danger-bg)", color: l.side === "long" ? "var(--ok-text)" : "var(--danger)" }}>{l.side === "long" ? "BUY" : "SELL"}</span>
              <span className="font-[700] text-[13.5px] font-['Space_Grotesk'] text-[var(--strong)] tabular-nums">{l.right} {l.strike.toLocaleString("en-IN")} <span className="text-[var(--faint)] font-semibold">Δ{l.open_delta != null ? (l.open_delta >= 0 ? "+" : "−") + Math.abs(l.open_delta).toFixed(2) : "—"}</span></span>
              <EvBadge id={l.open_event} date={l.open_ts} c={oc} />
              <EvBadge id={l.close_event} date={l.close_ts} c={cc} />
              <span className="text-[12.5px] font-semibold text-[var(--muted)] tabular-nums">{l.open_price?.toFixed(2)} → {l.close_price != null ? l.close_price.toFixed(2) : "—"}</span>
              <span className="text-right text-[12.5px] font-bold text-[var(--muted)] tabular-nums">{l.days != null ? (l.days < 10 ? l.days.toFixed(1) : Math.round(l.days)) : "—"}</span>
              <span className={`text-right font-[700] text-[13px] font-['Space_Grotesk'] tabular-nums ${signCls(l.pnl)}`}>{formatInr(l.pnl)}</span>
            </div>
          );
        })}
        <div className="flex items-center gap-3 px-5 py-[13px] bg-[var(--stat)]">
          <span className="font-[700] text-[13.5px] font-['Space_Grotesk'] text-[var(--strong)]">Net · {m.legs.length} legs</span>
          <span className="text-xs text-[var(--faint)] font-bold">gross of charges</span>
          <span className={`ml-auto font-[700] text-[15px] font-['Space_Grotesk'] tabular-nums ${signCls(total)}`}>{formatInr(total)}</span>
        </div>
      </div>
      <div className="bg-[var(--tint)] border border-[var(--tint-border)] rounded-[14px] px-[18px] py-[15px] text-[13px] text-[var(--muted)] font-semibold leading-[1.6]">
        <strong className="text-[var(--strong)]">Net Δ tells the story.</strong> Each leg's delta is reconstructed from its
        premium (Black-Scholes). A short is a negative delta contribution; the cycle drifts from neutral as spot moves,
        and each roll pulls it back — the "net Δ" on every event above is that running balance.
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- bits
function EvBadge({ id, date, c }: { id: string | null; date: string | null; c: { bg: string; fg: string } }) {
  if (!id) return <span className="text-[12.5px] font-bold text-[var(--faint)]">— open</span>;
  return (
    <span className="text-[12.5px] font-bold text-[var(--muted)]">
      <span className="px-[7px] py-0.5 rounded-md font-extrabold text-[10px]" style={{ backgroundColor: c.bg, color: c.fg }}>{id}</span>{" "}{date ? shortDate(date) : ""}
    </span>
  );
}
function Kpi({ label, value, cls, small }: { label: string; value: string; cls?: string; small?: boolean }) {
  return (
    <div className="bg-[var(--card)] border border-[var(--border)] rounded-[14px] px-4 py-3.5">
      <div className="text-[11px] text-[var(--faint)] font-extrabold tracking-wider">{label}</div>
      <div className={`font-[700] ${small ? "text-[15px]" : "text-[19px]"} font-['Space_Grotesk'] mt-[5px] tabular-nums ${cls ?? "text-[var(--strong)]"}`}>{value}</div>
    </div>
  );
}
function Chip({ children, tone }: { children: React.ReactNode; tone: "ok" | "danger" | "chip" }) {
  const s = tone === "ok" ? "bg-[var(--ok-bg)] text-[var(--ok-text)]"
    : tone === "danger" ? "bg-[var(--danger-bg)] text-[var(--danger)]" : "bg-[var(--chip)] text-[var(--chip-text)]";
  return <span className={`px-3 py-1 rounded-lg font-extrabold text-xs tracking-wide ${s}`}>{children}</span>;
}
function Legend({ swatch, children }: { swatch: React.ReactNode; children: React.ReactNode }) {
  return <span>{swatch}{children}</span>;
}
function eventTitle(e: CycleDetailEvent) {
  if (e.kind === "entry") return "Entry — position opened";
  if (e.kind === "hedge") return "Straddle cap → BE hedge";
  if (e.kind === "exit") return "Exit — closed all legs";
  const op = e.opened[0], cl = e.closed[0];
  if (op && cl) return `Roll — ${op.right === "CE" ? "call" : "put"} side ${op.strike < cl.strike ? "down" : "up"}`;
  return "Roll";
}
function expiryLabel(iso: string) {
  return new Date(iso + "T00:00").toLocaleDateString("en-IN", { day: "numeric", month: "short" }).toUpperCase();
}
