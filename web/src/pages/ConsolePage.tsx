/** Options Console — the replay screen (design handoff: Skais Options Console, D1/D3/D8).
 *
 *  Phase 2 of docs/PLAN-options-console.md: session bar + scrubber + market strip + the
 *  option chain, driven by a server-side minute cursor over the 1-min option store. The
 *  analysis column and the risk rail are placeholders until P3 — they are laid out at their
 *  final widths (chain 560 · analysis flex · rail 348) so the geometry is real from day one.
 *
 *  All colour comes from --oc-* tokens scoped to .oc-root (see index.css): the handoff's
 *  indigo palette, kept away from the app's teal. Type is IBM Plex Sans via `font-plex`.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type {
  ConsoleAlert, ConsoleChainLeg, ConsoleChainRow, ConsoleLeg, ConsoleLiveRun, ConsoleTicket, ConsoleTicketRow, ConsolePreset, ConsoleProbe, ConsoleRisk,
  ConsoleState,
  SimOpenSpec,
} from "../types";
import PayoffSvg, { toPayoffLegs } from "../components/console/PayoffSvg";
import { buildLivePayoff } from "../lib/payoff";
import { formatOptionSymbol } from "../lib/symbol";
import { computeMetrics } from "../lib/payoff";

/* ------------------------------------------------------------------ formatting
 * The handoff mandates U+2212 for minus and Indian digit grouping. lib/format.ts emits an
 * ASCII hyphen and is used on ~40 other screens, so wrap rather than change it. */
const MINUS = "−";
const num = (v: number | null | undefined, dp = 2) =>
  v == null || !Number.isFinite(v) ? "—"
    : (v < 0 ? MINUS : "") + Math.abs(v).toLocaleString("en-IN",
      { minimumFractionDigits: dp, maximumFractionDigits: dp });
const signed = (v: number | null | undefined, dp = 2) =>
  v == null ? "—" : (v >= 0 ? "+" : MINUS) + Math.abs(v).toLocaleString("en-IN",
    { minimumFractionDigits: dp, maximumFractionDigits: dp });

/** "2026-04-28" → "28 Apr". The handoff's chip row is a date, and the app already refuses
 *  to render raw machine forms to a human (lib/symbol.ts makes the same point about the
 *  pipe in an option ticker reading as an "I"). */
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
/** "2026-08-03" → "Mon 03 Aug 2026". */
const prettyDay = (iso: string) => {
  const d = new Date(`${iso}T00:00:00`);
  const wd = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getDay()];
  return `${wd} ${iso.slice(8, 10)} ${MONTHS[Number(iso.slice(5, 7)) - 1] ?? ""} ${iso.slice(0, 4)}`;
};
const expiryChip = (iso: string) =>
  `${iso.slice(8, 10)} ${MONTHS[Number(iso.slice(5, 7)) - 1] ?? iso.slice(5, 7)}`;

const JOGS: { label: string; minutes?: number; days?: number; op?: "sod" | "eod" }[] = [
  { label: "SOD", op: "sod" }, { label: `${MINUS}1d`, days: -1 },
  { label: `${MINUS}1h`, minutes: -60 }, { label: `${MINUS}30m`, minutes: -30 },
  { label: `${MINUS}15m`, minutes: -15 }, { label: `${MINUS}5m`, minutes: -5 },
  { label: `${MINUS}1m`, minutes: -1 },
];
const JOGS_FWD: typeof JOGS = [
  { label: "+1m", minutes: 1 }, { label: "+5m", minutes: 5 }, { label: "+15m", minutes: 15 },
  { label: "+30m", minutes: 30 }, { label: "+1h", minutes: 60 }, { label: "+1d", days: 1 },
  { label: "EOD", op: "eod" },
];

// shrink-0 is load-bearing: the expiry row scrolls horizontally, and without it flexbox
// compresses the chips until "28 Apr 27d" reads as "28" — the month and the DTE are the
// two things the chip exists to say.
/** The transport ladder, declared ONCE. The popover and the footer both render this, and
 *  the keydown handler below implements exactly these rows — three copies of a keymap is
 *  how a shortcut quietly stops matching what the screen claims it does. */
const KEYS: { keys: string; does: string }[] = [
  { keys: ", .", does: "back / forward 1 hour" },
  { keys: "Shift + , .", does: "15 minutes" },
  { keys: "Alt + , .", does: "1 minute" },
  { keys: "[  ]", does: "previous / next trading day (keeps the time)" },
  { keys: "Home / End", does: "session open / close" },
  { keys: "U", does: "undo the last action" },
  { keys: "Space", does: "play / pause the replay at the chosen speed" },
  { keys: "B", does: "bookmark this minute (again to remove)" },
  { keys: "J / K", does: "next / previous event (fill, bookmark)" },
  { keys: "H", does: "hide or show the option chain" },
  { keys: "?", does: "show or hide this list" },
];

/** Autoplay speeds — replay-minutes per wall-second. Labelled the way the handoff labels
 *  them ("1m/2s" = one minute every two seconds) so the chip says what it does. */
const SPEEDS: { label: string; minutes: number; ms: number }[] = [
  { label: "1m/2s", minutes: 1, ms: 2000 },
  { label: "1m/1s", minutes: 1, ms: 1000 },
  { label: "5m/1s", minutes: 5, ms: 1000 },
  { label: "15m/1s", minutes: 15, ms: 1000 },
  { label: "1h/1s", minutes: 60, ms: 1000 },   // a whole session in ~7 s (owner, 2026-09-10)
];
const ALERT_KINDS: { kind: ConsoleAlert["kind"]; label: string; hint: string }[] = [
  { kind: "target", label: "Target", hint: "₹ total MTM at or above" },
  { kind: "stop", label: "Stop", hint: "₹ loss at or beyond (enter a positive number)" },
  { kind: "delta", label: "|Δ|", hint: "net position delta in units, at or above" },
  { kind: "above", label: "Spot ≥", hint: "spot at or above" },
  { kind: "below", label: "Spot ≤", hint: "spot at or below" },
];

const chipBase = "h-[22px] px-2 rounded-[5px] text-[10.5px] font-semibold leading-[22px] "
  + "border transition select-none shrink-0 whitespace-nowrap "
  + "disabled:opacity-40 disabled:cursor-not-allowed";

function Chip({ children, onClick, active, title, disabled }: {
  children: React.ReactNode; onClick?: () => void; active?: boolean;
  title?: string; disabled?: boolean;
}) {
  return (
    <button type="button" onClick={onClick} title={title} disabled={disabled}
      className={chipBase}
      style={{
        background: active ? "var(--oc-accent-dim)" : "var(--oc-chip)",
        borderColor: active ? "var(--oc-accent)" : "transparent",
        color: active ? "var(--oc-accent)" : "var(--oc-muted)",
      }}>
      {children}
    </button>
  );
}

/** Shortcuts, hidden until asked for. A dense screen should not spend permanent space on a
 *  reference you need twice, but a transport nobody can find is a transport nobody uses —
 *  so: a ? chip, the ? key, click-outside and Esc to dismiss. */
function KeyHelp({ onClose, notes }: { onClose: () => void; notes: string[] }) {
  return (
    <>
      <div className="fixed inset-0 z-40" onClick={onClose} />
      <div className="absolute right-2 top-9 z-50 rounded-[10px] p-3 shadow-lg"
        style={{ background: "var(--oc-surface)", border: "1px solid var(--oc-line)",
          minWidth: 290 }}>
        <div className="mb-2 text-[9.5px] font-semibold uppercase tracking-[.07em]"
          style={{ color: "var(--oc-faint)" }}>Keyboard · transport</div>
        <table className="w-full text-[11.5px]">
          <tbody>
            {KEYS.map((k) => (
              <tr key={k.keys}>
                <td className="py-[3px] pr-3 whitespace-nowrap">
                  <span className="rounded-[4px] px-1.5 py-[1px] font-semibold"
                    style={{ background: "var(--oc-chip)", color: "var(--oc-ink)" }}>
                    {k.keys}
                  </span>
                </td>
                <td className="py-[3px]" style={{ color: "var(--oc-muted)" }}>{k.does}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="mt-2 pt-2 text-[10.5px]"
          style={{ borderTop: "1px solid var(--oc-hair)", color: "var(--oc-faint)" }}>
          Click the page once after loading it — the keys need the page to have focus.
        </div>
        {!!notes.length && (
          <div className="mt-2 pt-2" style={{ borderTop: "1px solid var(--oc-hair)" }}>
            <div className="text-[9.5px] font-semibold uppercase tracking-[.07em] mb-1"
              style={{ color: "var(--oc-faint)" }}>What this screen cannot know</div>
            <ul className="space-y-1 text-[10.5px]" style={{ color: "var(--oc-muted)" }}>
              {notes.map((n) => <li key={n}>· {n}</li>)}
            </ul>
          </div>
        )}
      </div>
    </>
  );
}

const inr0 = (v: number | null | undefined) =>
  v == null || !Number.isFinite(v) ? "—"
    : (v < 0 ? MINUS : "") + "₹" + Math.abs(v).toLocaleString("en-IN", { maximumFractionDigits: 0 });

/** "% of X", or an em dash when X is zero — a percentage of nothing is not a number. */
/** ONE rule for the line under "Margin", so the rail and the strip agree on what the
 *  number is: a measured anchor, Kite's basket for today's equivalent, or the model. */
const marginSub = (risk: ConsoleRisk | null | undefined, perSet?: number) => {
  if (!risk) return "model";
  if (risk.margin_source === "manual") return `anchor · ${inr0(perSet ?? 0)} / lot-set`;
  if (risk.margin_source === "zerodha") {
    // the tile is ~110px wide: the account and the mapped legs live in the ⓘ text
    return risk.margin_note?.shifted ? "Kite · today's equivalent" : "Kite basket";
  }
  if (risk.margin_detail)
    return `est · scan ${inr0(risk.margin_detail.span)} + exp ${inr0(risk.margin_detail.exposure)}`;
  return `${risk.margin_source} · ${pctOf(risk.margin, risk.capital)} of capital`;
};
const marginHelp = (risk: ConsoleRisk | null | undefined) => {
  if (risk?.margin_source === "manual")
    return "Margin is the anchor you typed for one lot-set × the lot-sets held. Every % here is of that figure. Set it to 0 to go back to Kite's figure.";
  if (risk?.margin_source === "zerodha") {
    const n = risk.margin_note;
    const legs = (n?.legs ?? []).map((l) => `${l.side} ${l.lots}× ${l.strike} ${l.right} ${l.expiry.slice(5)}`).join(", ");
    return n?.shifted
      ? `Kite's basket margin (${n?.account ?? "Zerodha"}) for TODAY'S EQUIVALENT of this book: the same moneyness (strikes scaled by today's spot ${n?.spot_today ? inr0(n.spot_today) : ""} over the replay's) and the same days to expiry on the chain Kite lists now — priced as ${legs}. Margin depends on moneyness, DTE and vol, not the calendar year, so this is the closest figure available; across a different vol regime it is still an estimate. Every % here is of it. Click the Margin tile to type the calculator's own number instead.`
      : `Kite's basket margin (${n?.account ?? "Zerodha"}) for exactly these legs (${legs}). Every % here is of it.`;
  }
  return "Margin is estimated the way SPAN is: the book's worst loss over a ±6% move (hedges offset) plus 2% exposure on every short unit. It gets the order of structures right, not the rupees (₹74.8k against Zerodha's ₹90.8k on an iron fly). Log in to a Zerodha account to get Kite's figure, or type the calculator's number as an anchor. Every % here is of this margin.";
};

const pctOf = (a: number | null | undefined, b: number | null | undefined) =>
  a == null || !b || !Number.isFinite(a) ? "—" : `${((a / b) * 100).toFixed(2)}%`;

function Panel({ children, className = "", style }: {
  children: React.ReactNode; className?: string; style?: React.CSSProperties;
}) {
  return (
    <div className={`rounded-[10px] p-3 ${className}`}
      style={{ background: "var(--oc-surface)", border: "1px solid var(--oc-line)", ...style }}>
      {children}
    </div>
  );
}

/** An ⓘ that opens a short explanation on click. Explanatory paragraphs inside a panel
 *  changed the panel's height as state changed, which moved everything under it. */
function InfoIcon({ text, title }: { text: string; title?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <span className="relative inline-block align-middle ml-1">
      <button type="button" onClick={() => setOpen((v) => !v)} title={title ?? "what this means"}
        className="w-[14px] h-[14px] rounded-full text-[9.5px] font-bold leading-[14px]"
        style={{ background: open ? "var(--oc-accent)" : "var(--oc-chip)",
          color: open ? "#fff" : "var(--oc-muted)" }}>i</button>
      {open && (
        <span className="absolute z-30 left-0 top-[18px] w-[260px] rounded-[8px] p-2.5 text-[11px] font-normal normal-case tracking-normal leading-snug shadow-lg"
          style={{ background: "var(--oc-surface)", color: "var(--oc-ink)", border: "1px solid var(--oc-line)" }}
          onClick={() => setOpen(false)}>{text}</span>
      )}
    </span>
  );
}

function Tile({ label, value, sub, tone, onClick }: {
  label: string; value: string; sub?: string; tone?: "pos" | "neg"; onClick?: () => void;
}) {
  return (
    <div className={`rounded-[8px] px-2 py-1.5${onClick ? " cursor-pointer hover:brightness-95" : ""}`}
      style={{ background: "var(--oc-panel2)" }} onClick={onClick}
      title={onClick ? "click to set the margin anchor for one lot-set" : undefined}>
      <div className="text-[9px] font-semibold uppercase tracking-[.06em]"
        style={{ color: "var(--oc-faint)" }}>{label}</div>
      <div className="text-[13px] font-semibold tabular-nums"
        style={{ color: tone === "pos" ? "var(--oc-pos)"
          : tone === "neg" ? "var(--oc-neg)" : "var(--oc-ink)" }}>{value}</div>
      {sub && <div className="text-[10px] whitespace-nowrap overflow-hidden text-ellipsis" title={sub}
        style={{ color: "var(--oc-faint)" }}>{sub}</div>}
    </div>
  );
}

/** The scrubber with the day's events on it. Positions are minutes since the open over
 *  the session length, so a marker sits exactly under where the playhead will be when
 *  the cursor reaches it. Click to seek. */
function Track({ state, onSeek }: { state: ConsoleState | null; onSeek: (at: string) => void }) {
  const [a, b] = state?.session.range ?? ["09:15", "15:40"];
  const mins = (t: string) => Number(t.slice(0, 2)) * 60 + Number(t.slice(3, 5));
  const span = Math.max(1, mins(b) - mins(a));
  const pct = (t: string) => `${Math.max(0, Math.min(100, (100 * (mins(t) - mins(a))) / span))}%`;
  const bar = useRef<HTMLDivElement>(null);
  const seek = (e: React.MouseEvent) => {
    if (!bar.current || !state) return;
    const r = bar.current.getBoundingClientRect();
    const f = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    const m = mins(a) + Math.round(f * span);
    onSeek(`${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`);
  };
  const tr = state?.track;
  return (
    <div ref={bar} onClick={seek} className="flex-1 relative h-4 cursor-pointer" title="click to seek">
      <div className="absolute left-0 right-0 top-[6px] h-1 rounded" style={{ background: "var(--oc-chip)" }} />
      <div className="absolute left-0 top-[6px] h-1 rounded"
        style={{ width: `${state?.session.played_pct ?? 0}%`, background: "var(--oc-accent)" }} />
      {tr?.fills.map((f, i) => (
        <span key={`f${i}`} className="absolute w-[5px] h-[5px] rounded-full -translate-x-1/2"
          title={`${f.action} ${f.at}`}
          style={{ left: pct(f.at), top: 5.5,
            background: f.action === "SHORT" || f.action === "SELL" ? "var(--oc-neg)" : "var(--oc-pos)" }} />
      ))}
      {tr?.alerts.map((x, i) => (
        <span key={`a${i}`} className="absolute w-[2px] h-[10px] -translate-x-1/2"
          title={`${x.kind} fired ${x.at}`}
          style={{ left: pct(x.at), top: 3, background: "var(--oc-caution)" }} />
      ))}
      {tr?.bookmarks.map((t) => (
        <span key={`b${t}`} className="absolute text-[9px] leading-none -translate-x-1/2"
          title={`bookmark ${t}`} style={{ left: pct(t), top: -1, color: "var(--oc-accent)" }}>◇</span>
      ))}
      {state && (
        <span className="absolute w-[9px] h-[9px] rounded-full -translate-x-1/2"
          style={{ left: pct(state.session.clock), top: 3.5, background: "var(--oc-accent)",
            boxShadow: "0 0 0 2px var(--oc-surface)" }} />
      )}
    </div>
  );
}

/** The open book's last 30 minutes (design D6): a line above/below its own zero, the
 *  last point emphasised, the latest value beside it. Shape and sign, not a chart. */
function Sparkline({ points }: { points: { at: string; pnl: number }[] }) {
  const W = 120, H = 16;
  const ys = points.map((p) => p.pnl);
  const lo = Math.min(0, ...ys), hi = Math.max(0, ...ys);
  const px = (i: number) => (points.length > 1 ? (i / (points.length - 1)) * W : W);
  const py = (v: number) => 1 + (1 - (v - lo) / ((hi - lo) || 1)) * (H - 2);
  const last = points[points.length - 1];
  const tone = last.pnl >= 0 ? "var(--oc-pos)" : "var(--oc-neg)";
  return (
    <span className="inline-flex items-center gap-1.5 shrink-0"
      title={`open P&L, last ${points.length} min · ${points[0].at} → ${last.at}`}>
      <svg width={W} height={H} style={{ display: "block" }}>
        <line x1={0} x2={W} y1={py(0)} y2={py(0)} stroke="var(--oc-hair)" strokeWidth={1} />
        <path fill="none" stroke={tone} strokeWidth={1.3}
          d={points.map((p, i) => `${i ? "L" : "M"}${px(i).toFixed(1)},${py(p.pnl).toFixed(1)}`).join(" ")} />
        <circle cx={px(points.length - 1)} cy={py(last.pnl)} r={2} fill={tone} />
      </svg>
      <span className="text-[9px] tabular-nums font-semibold whitespace-nowrap" style={{ color: tone }}>
        {inr0(last.pnl)}
        <span className="font-normal" style={{ color: "var(--oc-faint)" }}> · 30m</span>
      </span>
    </span>
  );
}

function TrackChip({ children, onClick, disabled, title }: {
  children: React.ReactNode; onClick: () => void; disabled?: boolean; title?: string;
}) {
  return (
    <button type="button" onClick={onClick} disabled={disabled} title={title}
      className="h-[16px] px-1.5 rounded-[4px] text-[9.5px] font-semibold whitespace-nowrap disabled:opacity-40"
      style={{ background: "var(--oc-chip)", color: "var(--oc-muted)" }}>{children}</button>
  );
}

/** Design D4: prebuilt structures as cards — the rule in words, the strikes it resolved to
 *  at THIS cursor, credit, margin (labelled), max P/L and POP from the same calculator as
 *  the rail, a DEFINED / UNDEFINED badge, and the reason when it cannot be built. In
 *  replay, Apply trades all its legs as one action; Undo takes them all back. */
function PresetGallery({ presets, state, lots, onApply }: {
  presets: ConsolePreset[]; state: ConsoleState | null; lots: number;
  onApply: (id: string) => void;
}) {
  const spot = state?.market.spot ?? null, expiry = state?.chain.expiry ?? null;
  if (!state) {
    return <div className="h-full flex items-center justify-center text-[12px]"
      style={{ color: "var(--oc-faint)" }}>Opening the session…</div>;
  }
  // Design D4 at its quietest (owner: "just go with payoff charts"): name, badge, the
  // rule, the payoff glyph, and the card itself is the Trade button. The strikes and the
  // numbers live on the rail once the structure is on the book — the picture picks it.
  return (
    <div className="h-full overflow-auto">
      <div className="grid grid-cols-4 gap-2">
        {presets.map((p) => {
          const legs = toPayoffLegs(p.legs);
          const d = p.ok && spot && expiry && legs.length
            ? buildLivePayoff(legs, spot, expiry, state.session.date) : null;
          const legsText = p.legs.map((l) =>
            `${l.side} ${Math.round(l.strike)} ${l.right}${l.lots > 1 ? ` ×${l.lots}` : ""}`).join(" · ");
          return (
            <button key={p.id} type="button" disabled={!p.ok} onClick={() => onApply(p.id)}
              title={p.ok ? `${p.rule} — ${legsText} · click to trade ×${lots}`
                : `${p.rule} — cannot build here: ${p.reason}`}
              className="rounded-[8px] p-2 text-left flex flex-col gap-1 disabled:opacity-50"
              style={{ background: "var(--oc-panel2)", border: "1px solid transparent" }}
              onMouseEnter={(e) => { e.currentTarget.style.borderColor = "var(--oc-accent)"; }}
              onMouseLeave={(e) => { e.currentTarget.style.borderColor = "transparent"; }}>
              <div className="flex items-center justify-between gap-1">
                <b className="text-[11.5px] leading-tight">{p.name}</b>
                <span className="px-1 py-[1px] rounded-[3px] text-[8px] font-bold whitespace-nowrap"
                  style={{ color: p.defined ? "var(--oc-pos)" : "var(--oc-neg)",
                    border: `1px solid ${p.defined ? "var(--oc-pos)" : "var(--oc-neg)"}` }}>
                  {p.defined ? "DEFINED" : "UNDEFINED"}
                </span>
              </div>
              <PayoffGlyph data={d?.data ?? null} />
              <div className="text-[9.5px] leading-tight" style={{ color: "var(--oc-faint)" }}>
                {p.ok ? p.rule : `cannot build: ${p.reason}`}
              </div>
            </button>
          );
        })}
        {!presets.length && (
          <div className="col-span-full py-6 text-center text-[12px]" style={{ color: "var(--oc-faint)" }}>
            Resolving presets against the chain…
          </div>
        )}
      </div>
    </div>
  );
}

/** The design's payoff glyph: the expiry line green where it pays, red where it loses,
 *  soft fills, a zero baseline. Shape only — no axes, no numbers. */
function PayoffGlyph({ data }: { data: { spot: number; expiry: number }[] | null }) {
  const W = 120, H = 44;
  if (!data?.length) {
    return <div style={{ height: H, background: "var(--oc-chip)", borderRadius: 4 }} />;
  }
  const xs = data.map((p) => p.spot), ys = data.map((p) => p.expiry);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const yLo = Math.min(0, ...ys), yHi = Math.max(0, ...ys);
  const px = (v: number) => ((v - x0) / (x1 - x0 || 1)) * W;
  const py = (v: number) => 3 + (1 - (v - yLo) / ((yHi - yLo) || 1)) * (H - 6);
  const runs: { pos: boolean; pts: typeof data }[] = [];
  data.forEach((p, i) => {
    const pos = p.expiry >= 0;
    const last = runs[runs.length - 1];
    if (!last || last.pos !== pos) runs.push({ pos, pts: i ? [data[i - 1], p] : [p] });
    else last.pts.push(p);
  });
  const zero = py(0);
  const area = (sign: 1 | -1) => {
    const seg = data.filter((p) => (sign > 0 ? p.expiry >= 0 : p.expiry <= 0));
    if (seg.length < 2) return "";
    return `M${px(seg[0].spot)},${zero} ` + seg.map((p) => `L${px(p.spot)},${py(p.expiry)}`).join(" ")
      + ` L${px(seg[seg.length - 1].spot)},${zero} Z`;
  };
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} preserveAspectRatio="none">
      <path d={area(1)} fill="var(--oc-pos-fill)" />
      <path d={area(-1)} fill="var(--oc-neg-fill)" />
      <line x1={0} x2={W} y1={zero} y2={zero} stroke="var(--oc-muted)" strokeWidth={0.6} />
      {runs.filter((r) => r.pts.length > 1).map((r, i) => (
        <path key={i} fill="none" strokeWidth={1.4} vectorEffect="non-scaling-stroke"
          stroke={r.pos ? "var(--oc-pos)" : "var(--oc-neg)"}
          d={r.pts.map((p, j) => `${j ? "L" : "M"}${px(p.spot)},${py(p.expiry)}`).join(" ")} />
      ))}
    </svg>
  );
}

/** Design D5, the order ticket: what Commit sends, row by row at the run's own mark, the
 *  cash it moves, the margin it needs, and the risk before → after. On a REAL run the
 *  typed REAL lives here and nowhere else. A limit price is not offered: the executor
 *  builds the broker order without one and LiveBroker works its own touch ladder. */
function OrderTicket({ ticket, staged, risk, riskAfter, mNow, mBefore, isReal, realTyped,
  setRealTyped, capital, orderError, busy, onSend, onClose, onRevert }: {
  ticket: ConsoleTicket; staged: NonNullable<ConsoleState["staged"]>;
  risk: ConsoleRisk; riskAfter: ConsoleRisk | null;
  mNow: ReturnType<typeof computeMetrics> | null; mBefore: ReturnType<typeof computeMetrics> | null;
  isReal: boolean; realTyped: string; setRealTyped: (v: string) => void;
  capital: number; orderError: string | null; busy: boolean;
  onSend: (limits: Record<string, number>) => void; onClose: () => void; onRevert: () => void;
}) {
  const before = risk.margin, after = staged.margin_after;
  const free = capital - after;
  // D5 LMT (2026-09-10): a row switched to LMT carries the owner's price to the run. Paper
  // fills a MARKETABLE limit at the better price and refuses one that is not (nothing is
  // placed); live places it and caps its own ladder at it. Keyed "<role>:<symbol>".
  const [lmt, setLmt] = useState<Record<string, { on: boolean; price: string }>>({});
  const keyOf = (r: ConsoleTicketRow) => `${r.role}:${r.symbol}`;
  const limits: Record<string, number> = {};
  for (const r of ticket.rows) {
    const e = lmt[keyOf(r)];
    if (e?.on) { const v = Number(e.price); if (Number.isFinite(v) && v > 0) limits[keyOf(r)] = v; }
  }
  const badLimit = ticket.rows.some((r) => lmt[keyOf(r)]?.on && !(Number(lmt[keyOf(r)].price) > 0));
  const canSend = !busy && !orderError && !badLimit && ticket.rows.length > 0 && (!isReal || realTyped === "REAL");
  return (
    <div className="fixed inset-0 z-40 flex items-start justify-center pt-16"
      style={{ background: "rgba(20,24,40,.45)" }} onClick={onClose}>
      <div className="oc-root font-plex w-[720px] max-w-[95vw] rounded-[12px] p-4 shadow-2xl"
        style={{ background: "var(--oc-surface)", color: "var(--oc-ink)", border: `1px solid ${isReal ? "var(--oc-neg)" : "var(--oc-line)"}` }}
        onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between">
          <div>
            <div className="text-[9.5px] font-semibold uppercase tracking-[.07em]" style={{ color: "var(--oc-faint)" }}>
              Order ticket · {isReal ? "REAL ORDERS" : "paper"}
            </div>
            <div className="text-[14px] font-semibold">{staged.label}</div>
          </div>
          <span className="px-1.5 py-[1px] rounded-[3px] text-[9px] font-bold"
            style={{ color: mNow && !mNow.maxLossUnlimited ? "var(--oc-pos)" : "var(--oc-neg)",
              border: `1px solid ${mNow && !mNow.maxLossUnlimited ? "var(--oc-pos)" : "var(--oc-neg)"}` }}>
            {mNow && !mNow.maxLossUnlimited ? "DEFINED RISK" : "UNDEFINED RISK"} after
          </span>
        </div>
        {ticket.consequence && (
          /* what Commit does to the RUN — a hand-edit that leaves lots pauses the strategy
             (owner 2026-09-10); said here, before the button, not after */
          <div className="mt-3 px-2.5 py-2 rounded-[6px] text-[11.5px] leading-snug"
            style={{ background: "var(--oc-caution-dim)", borderLeft: "3px solid var(--oc-caution)", color: "var(--oc-ink)" }}>
            {ticket.consequence}
          </div>
        )}
        <table className="w-full mt-3 text-[12px] tabular-nums whitespace-nowrap">
          <thead>
            <tr className="text-[9px] uppercase tracking-[.06em]" style={{ color: "var(--oc-faint)" }}>
              <th className="text-left font-semibold py-1">Side</th><th className="text-left font-semibold">Contract</th>
              <th className="text-right font-semibold">Lots</th><th className="text-right font-semibold">Qty</th>
              <th className="text-right font-semibold">Type</th><th className="text-right font-semibold">Price</th>
              <th className="text-right font-semibold">Cash</th>
            </tr>
          </thead>
          <tbody>
            {ticket.rows.map((r, i) => (
              <tr key={i} style={{ borderTop: "1px solid var(--oc-hair)" }}>
                <td className="py-1.5">
                  <span className="px-1 rounded-[3px] text-[10px] font-bold"
                    style={{ color: r.action === "SELL" ? "var(--oc-neg)" : "var(--oc-pos)",
                      background: r.action === "SELL" ? "var(--oc-neg-fill)" : "var(--oc-pos-fill)" }}>{r.action}</span>
                  <span className="ml-1.5 text-[10px]" style={{ color: "var(--oc-faint)" }}>{r.role === "close" ? "to close" : "to open"}</span>
                </td>
                <td><b>{Math.round(r.strike).toLocaleString("en-IN")} {r.right}</b>
                  <span className="ml-1.5 text-[10.5px]" style={{ color: "var(--oc-muted)" }}>{expiryChip(r.expiry)}</span></td>
                <td className="text-right">×{r.lots}</td>
                <td className="text-right">{r.units.toLocaleString("en-IN")}</td>
                <td className="text-right whitespace-nowrap">
                  {ticket.limit_orders ? (
                    <button type="button"
                      title={lmt[keyOf(r)]?.on ? "LMT — the price you type; click for MKT" : "MKT — the run prices it; click for LMT"}
                      onClick={() => setLmt((m) => ({ ...m, [keyOf(r)]: { on: !m[keyOf(r)]?.on, price: m[keyOf(r)]?.price ?? String(r.price) } }))}
                      className="px-1.5 rounded-[3px] text-[10px] font-bold"
                      style={{ background: lmt[keyOf(r)]?.on ? "var(--oc-accent)" : "var(--oc-chip)", color: lmt[keyOf(r)]?.on ? "#fff" : "var(--oc-muted)" }}>
                      {lmt[keyOf(r)]?.on ? "LMT" : "MKT"}
                    </button>
                  ) : (
                    <><span className="font-semibold">MKT</span>
                      <span className="ml-1 text-[10px]" title="a limit price is not offered here"
                        style={{ color: "var(--oc-faint)", textDecoration: "line-through" }}>LMT</span></>
                  )}
                </td>
                <td className="text-right">
                  {lmt[keyOf(r)]?.on ? (
                    <input value={lmt[keyOf(r)].price} inputMode="decimal" aria-label="limit price"
                      onChange={(e) => setLmt((m) => ({ ...m, [keyOf(r)]: { on: true, price: e.target.value } }))}
                      className="w-[72px] h-[22px] px-1 rounded-[4px] text-right text-[12px] tabular-nums"
                      style={{ background: "var(--oc-panel)", border: "1px solid var(--oc-accent)", color: "var(--oc-ink)" }} />
                  ) : num(r.price)}
                </td>
                <td className="text-right font-semibold" style={{ color: r.cash >= 0 ? "var(--oc-pos)" : "var(--oc-neg)" }}>{inr0(r.cash)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="mt-2 flex items-baseline justify-between text-[11.5px]">
          <span style={{ color: "var(--oc-muted)" }}>fills at {ticket.fill_basis}</span>
          <span>net <b style={{ color: ticket.net_cash >= 0 ? "var(--oc-pos)" : "var(--oc-neg)" }}>
            {ticket.net_cash >= 0 ? "credit" : "debit"} {inr0(Math.abs(ticket.net_cash))}</b></span>
        </div>
        <div className="mt-3 grid grid-cols-2 gap-2">
          <div className="rounded-[8px] p-2.5" style={{ background: "var(--oc-panel2)" }}>
            <div className="text-[9px] font-semibold uppercase tracking-[.06em]" style={{ color: "var(--oc-faint)" }}>Margin impact</div>
            <div className="text-[13px] font-semibold tabular-nums">{inr0(before)} → {inr0(after)}
              <span className="text-[10.5px] font-normal" style={{ color: "var(--oc-faint)" }}> · {staged.margin_source}</span></div>
            <div className="mt-1 h-1.5 rounded" style={{ background: "var(--oc-chip)" }}>
              <div className="h-1.5 rounded" style={{ width: `${Math.max(0, Math.min(100, (100 * after) / (capital || 1)))}%`,
                background: after > capital ? "var(--oc-neg)" : "var(--oc-accent)" }} />
            </div>
            <div className="text-[10px] mt-1 tabular-nums" style={{ color: after > capital ? "var(--oc-neg)" : "var(--oc-faint)" }}>
              {pctOf(after, capital)} of capital · {free >= 0 ? `${inr0(free)} free after` : `${inr0(-free)} SHORT of capital`}
            </div>
          </div>
          <div className="rounded-[8px] p-2.5 text-[11px] tabular-nums" style={{ background: "var(--oc-panel2)" }}>
            <div className="text-[9px] font-semibold uppercase tracking-[.06em]" style={{ color: "var(--oc-faint)" }}>Risk before → after</div>
            <div className="grid grid-cols-[auto_1fr_1fr] gap-x-3 gap-y-0.5 mt-1">
              <span style={{ color: "var(--oc-faint)" }}>max profit</span>
              <span>{mBefore ? (mBefore.maxProfitUnlimited ? "∞" : inr0(mBefore.maxProfit)) : "—"}</span>
              <b style={{ color: "var(--oc-pos)" }}>{mNow ? (mNow.maxProfitUnlimited ? "∞" : inr0(mNow.maxProfit)) : "—"}</b>
              <span style={{ color: "var(--oc-faint)" }}>max loss</span>
              <span>{mBefore ? (mBefore.maxLossUnlimited ? "unlimited" : inr0(mBefore.maxLoss)) : "—"}</span>
              <b style={{ color: "var(--oc-neg)" }}>{mNow ? (mNow.maxLossUnlimited ? "unlimited" : inr0(mNow.maxLoss)) : "—"}</b>
              <span style={{ color: "var(--oc-faint)" }}>POP</span>
              <span>{mBefore?.pop == null ? "—" : `${(mBefore.pop * 100).toFixed(0)}%`}</span>
              <b>{mNow?.pop == null ? "—" : `${(mNow.pop * 100).toFixed(0)}%`}</b>
              <span style={{ color: "var(--oc-faint)" }}>net Δ</span>
              <span>{risk.greeks.delta == null ? "—" : signed(risk.greeks.delta, 1)}</span>
              <b>{riskAfter?.greeks.delta == null ? "—" : signed(riskAfter.greeks.delta, 1)}</b>
            </div>
          </div>
        </div>
        {orderError && (
          <div className="mt-3 px-2 py-1.5 rounded-[6px] text-[11px] font-semibold"
            style={{ background: "var(--oc-neg-fill)", color: "var(--oc-neg)" }}>
            The run is halted on an order error; acknowledge it on the Live page before sending.
          </div>
        )}
        <div className="mt-4 flex items-center gap-2">
          <button type="button" onClick={onRevert} className="underline text-[11.5px]" style={{ color: "var(--oc-muted)" }}>Revert all</button>
          <span className="ml-auto flex items-center gap-2">
            {isReal && (
              <input value={realTyped} onChange={(e) => setRealTyped(e.target.value)} autoFocus
                placeholder="type REAL to send" aria-label="type REAL to confirm real orders"
                className="h-[26px] w-[160px] rounded-[5px] px-2 text-[11px] font-semibold tracking-wide"
                style={{ background: "var(--oc-chip)", color: "var(--oc-neg)", border: "1px solid var(--oc-neg)" }} />
            )}
            <button type="button" onClick={onClose}
              className="px-2.5 h-[26px] rounded-[5px] text-[11.5px]"
              style={{ background: "var(--oc-chip)", color: "var(--oc-muted)" }}>Back esc</button>
            <button type="button" disabled={!canSend} onClick={() => onSend(limits)}
              className="px-3 h-[26px] rounded-[5px] text-[11.5px] font-semibold disabled:opacity-40"
              style={{ background: isReal ? "var(--oc-neg)" : "var(--oc-accent)", color: "#fff" }}>
              {isReal ? `Send ${ticket.rows.length} order${ticket.rows.length === 1 ? "" : "s"} to broker` : `Commit ${ticket.rows.length} order${ticket.rows.length === 1 ? "" : "s"} ⏎`}
            </button>
          </span>
        </div>
      </div>
    </div>
  );
}

function GreekCell({ label, v, dp, unit, inr }: {
  label: string; v: number | null | undefined; dp: number; unit?: string; inr?: boolean;
}) {
  return (
    <div className="rounded-[6px] px-2 py-1" style={{ background: "var(--oc-panel2)" }}>
      <div className="text-[9px] font-semibold uppercase tracking-[.06em]"
        style={{ color: "var(--oc-faint)" }}>{label}</div>
      <div className="text-[12px] font-semibold tabular-nums whitespace-nowrap"
        style={{ color: v == null ? "var(--oc-faint)" : "var(--oc-ink)" }}>
        {v == null ? "—" : inr ? inr0(v) : signed(v, dp)}
        {v != null && unit ? <span className="text-[9.5px] font-normal"
          style={{ color: "var(--oc-faint)" }}> {unit}</span> : null}
      </div>
    </div>
  );
}

/** T+0 value of the book if spot were HERE, right now — and the change against the
 *  current mark, which is the number a hedger actually reads. */
function ScenarioCell({ label, v, base }: {
  label: string; v: number | null | undefined; base: number | null | undefined;
}) {
  const d = v != null && base != null ? v - base : null;
  return (
    <div className="rounded-[6px] px-2 py-1"
      style={{ background: "var(--oc-panel2)", borderLeft: "2px solid var(--oc-accent-dim)" }}>
      <div className="text-[9px] font-semibold uppercase tracking-[.06em] whitespace-nowrap"
        style={{ color: "var(--oc-faint)" }}>T+0 {label}</div>
      <div className="text-[12px] font-semibold tabular-nums whitespace-nowrap"
        style={{ color: v == null ? "var(--oc-faint)"
          : v >= 0 ? "var(--oc-pos)" : "var(--oc-neg)" }}>
        {v == null ? "—" : inr0(v)}
        {d != null && Math.abs(d) >= 1 ? <span className="text-[9.5px] font-normal"
          style={{ color: "var(--oc-faint)" }}> {signed(d, 0)}</span> : null}
      </div>
    </div>
  );
}

/** Armed levels. An alert fires ONCE at the cursor's minute, draws amber on the chart,
 *  and pauses autoplay so the minute stays on screen; step back before it and it is
 *  armed again — in a replay, what has not happened yet has not happened. */
function AlertsCard({ alerts, disabled, onArm, onClear, manualMode }: {
  alerts: ConsoleAlert[]; disabled: boolean;
  onArm: (b: { kind: ConsoleAlert["kind"]; value: number }) => void;
  onClear: (id: string) => void;
  manualMode?: boolean;  // a live run the owner's hand took over: target/stop here ARE the rail's rules
}) {
  const [kind, setKind] = useState<ConsoleAlert["kind"]>("target");
  const [value, setValue] = useState("");
  const meta = ALERT_KINDS.find((k) => k.kind === kind)!;
  const arm = () => {
    const v = Number(value);
    if (!Number.isFinite(v) || !value.trim()) return;
    onArm({ kind, value: v }); setValue("");
  };
  const fmt = (a: ConsoleAlert) =>
    a.kind === "target" ? inr0(a.value) : a.kind === "stop" ? inr0(-Math.abs(a.value))
    : a.kind === "delta" ? `${a.value} units` : Math.round(a.value).toLocaleString("en-IN");
  return (
    <Panel>
      <div className="flex items-center justify-between">
        <span className="text-[9.5px] font-semibold uppercase tracking-[.07em]"
          style={{ color: "var(--oc-faint)" }}>Alerts
          <InfoIcon title="how alerts work" text="An alert fires once, at the first minute it is true, draws on the chart, and pauses playback. Target and Stop are rupees of total MTM (stop as a positive number); |Δ| is net delta in units; spot above/below are index levels. In replay a rewind re-arms it." />
        </span>
        <span className="text-[10px]" style={{ color: "var(--oc-faint)" }}>
          {alerts.length ? `${alerts.filter((a) => a.state === "fired").length}/${alerts.length} fired` : ""}
        </span>
      </div>
      {alerts.length ? (
        <div className="mt-1.5 space-y-1">
          {alerts.map((a) => (
            <div key={a.id} className="flex items-center gap-2 text-[11.5px] tabular-nums rounded-[6px] px-2 py-1"
              style={{ background: a.state === "fired" ? "var(--oc-caution-dim)" : "var(--oc-panel2)" }}>
              <span className="font-semibold w-[52px]">
                {ALERT_KINDS.find((k) => k.kind === a.kind)?.label ?? a.kind}
              </span>
              <span>{fmt(a)}</span>
              {a.rail && (
                <span className="px-1 rounded-[3px] text-[9px] font-bold" title="the manual rail's own rule: it exits the whole book when it trips"
                  style={{ background: "var(--oc-caution-dim)", color: "var(--oc-caution)" }}>EXITS BOOK</span>
              )}
              <span className="ml-auto text-[10px] font-bold px-1.5 rounded-[3px]"
                style={{ color: a.state === "fired" ? "var(--oc-caution)" : "var(--oc-muted)",
                  border: `1px solid ${a.state === "fired" ? "var(--oc-caution)" : "var(--oc-line)"}` }}>
                {a.state === "fired" ? `FIRED ${a.fired_at?.slice(11) ?? ""}` : "ARMED"}
              </span>
              <button type="button" onClick={() => onClear(a.id)} title="remove"
                className="text-[12px] leading-none" style={{ color: "var(--oc-faint)" }}>×</button>
            </div>
          ))}
        </div>
      ) : (
        <div className="mt-1.5 text-[11px]" style={{ color: "var(--oc-faint)" }}>None armed.</div>
      )}
      {manualMode && (
        <div className="mt-1.5 text-[10.5px]" style={{ color: "var(--oc-muted)" }}>
          Manual mode: a target or stop armed here is the run's own rule — it exits the whole book when it trips (optional).
        </div>
      )}
      <div className="mt-2 flex items-center gap-1.5">
        <select value={kind} onChange={(e) => setKind(e.target.value as ConsoleAlert["kind"])}
          disabled={disabled}
          className="h-[24px] rounded-[5px] px-1 text-[11px] font-semibold"
          style={{ background: "var(--oc-chip)", color: "var(--oc-ink)", border: "none" }}>
          {ALERT_KINDS.map((k) => <option key={k.kind} value={k.kind}>{k.label}</option>)}
        </select>
        <input value={value} onChange={(e) => setValue(e.target.value)} disabled={disabled}
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); arm(); } }}
          placeholder={meta.hint} inputMode="decimal"
          className="h-[24px] flex-1 min-w-0 rounded-[5px] px-2 text-[11px] tabular-nums"
          style={{ background: "var(--oc-chip)", color: "var(--oc-ink)", border: "none" }} />
        <button type="button" onClick={arm} disabled={disabled || !value.trim()}
          className="h-[24px] px-2.5 rounded-[5px] text-[11px] font-semibold disabled:opacity-40"
          style={{ background: "var(--oc-accent)", color: "#fff" }}>Arm</button>
      </div>
    </Panel>
  );
}

/** One position row. Strike and size are EDITABLE here, because "I want that leg one
 *  strike higher" is a normal adjustment and re-typing the whole leg is not how anyone
 *  thinks about it. Both go through staging like everything else: a strike change is a
 *  roll (close here, open there) and a size change is a partial exit or a top-up, so the
 *  payoff previews it and the charges are real. */
function LegRow({ leg, grid, onStage, onUnstage, chainExpiry, resetKey, replay }: {
  leg: ConsoleLeg; grid: number; chainExpiry: string | null;
  onStage: (b: Parameters<typeof api.consoleStage>[1]) => void;
  // live/paper: drop the pending change; replay: delete the leg as if never traded
  onUnstage?: (legId: string, kind?: string | null) => void;
  resetKey: string;      // changes when the basket is reverted → the exit selector resets
  replay?: boolean;
}) {
  const [exitLots, setExitLots] = useState(leg.lots);
  useEffect(() => { setExitLots((n) => Math.min(Math.max(1, n), leg.lots)); }, [leg.lots]);
  // A revert clears the STAGED change, but the selector is a local input and kept its
  // "1" — which read as "the exit was not reverted" (owner, 2026-09-10).
  useEffect(() => { setExitLots(leg.lots); /* eslint-disable-line */ }, [resetKey]);
  const value = leg.ltp == null ? null : leg.ltp * leg.units;
  const from = leg.pending_from ?? null;
  const exitedLots = leg.pending === "exit" && from ? from.lots - leg.lots : 0;
  const pendingNote = leg.pending === "exit" ? `PARTIAL EXIT ×${exitedLots}`
    : leg.pending === "roll" && from ? `rolled from ${Math.round(from.strike).toLocaleString("en-IN")}`
    : leg.pending === "resize" && from ? `resized from ×${from.lots}`
    : null;
  return (
    <>
    {/* A pending add is the whole ROW tinted, with a stripe on its left edge and a small
        "new" under the side chip — a badge inside the strike cell widened the column and
        scrolled the table (owner, 2026-09-10). */}
    <tr style={{ opacity: leg.enabled ? 1 : 0.45,
        background: leg.pending === "add" ? "var(--oc-caution-dim)" : undefined,
        boxShadow: leg.pending === "add" ? "inset 3px 0 0 var(--oc-caution)" : undefined }}
      title={leg.pending === "add" ? "new leg — staged, not yet committed" : undefined}>
      <td className="py-1.5">
        <button type="button" title={leg.enabled ? "exclude from the payoff" : "include"}
          onClick={() => onStage({ kind: "toggle", leg_id: leg.id })}
          className="w-[26px] h-[15px] rounded-full align-middle"
          style={{ background: leg.enabled ? "var(--oc-accent)" : "var(--oc-chip)" }}>
          <span className="block w-[11px] h-[11px] rounded-full bg-white"
            style={{ marginLeft: leg.enabled ? 13 : 2 }} />
        </button>
      </td>
      <td>
        <span className="px-1 rounded-[3px] text-[10px] font-bold"
          style={{ color: leg.side === "S" ? "var(--oc-neg)" : "var(--oc-pos)",
            background: leg.side === "S" ? "var(--oc-neg-fill)" : "var(--oc-pos-fill)" }}>
          {leg.side}</span>
        {leg.pending === "add" && (
          <span className="ml-1 text-[8.5px] font-bold uppercase align-middle"
            style={{ color: "var(--oc-caution)" }}>new</span>
        )}
        </td>
      {/* Strike and size are steppers in their OWN columns and ALWAYS visible (a
          hover-reveal moved the button out from under the second click). */}
      <td className="whitespace-nowrap">
        <Stepper title={`roll a strike (${grid} pts)`}
          onDown={() => onStage({ kind: "roll", leg_id: leg.id, strike: leg.strike - grid })}
          onUp={() => onStage({ kind: "roll", leg_id: leg.id, strike: leg.strike + grid })}>
          <b className="whitespace-nowrap">
            {Math.round(leg.strike).toLocaleString("en-IN")} {leg.right}</b>
        </Stepper>
        {leg.tag && leg.tag !== "STRATEGY" && (
          <span className="ml-1 px-1 rounded-[3px] text-[8.5px] font-bold align-middle"
            title={leg.tag === "MIXED" ? "the strategy and your hand both hold this contract" : "opened by hand"}
            style={{ background: "var(--oc-caution-dim)", color: "var(--oc-caution)" }}>{leg.tag}</span>
        )}

      </td>
      {/* the leg's OWN expiry + DTE, amber when it is not the ladder's */}
      <td className="whitespace-nowrap text-[11px]"
        title={leg.expiry !== chainExpiry ? "not the expiry the chain is showing" : undefined}
        style={{ color: leg.expiry !== chainExpiry ? "var(--oc-caution)" : "var(--oc-muted)" }}>
        {expiryChip(leg.expiry)}
        <span className="text-[10px]" style={{ color: "var(--oc-faint)" }}>
          {leg.dte == null ? "" : ` ${leg.dte}d`}</span>
        {leg.expiry !== chainExpiry ? " ⚠" : ""}
      </td>
      <td className="whitespace-nowrap">
        <Stepper title="add to or trim this position"
          onDown={() => onStage({ kind: "resize", leg_id: leg.id, lots: leg.lots - 1 })}
          onUp={() => onStage({ kind: "resize", leg_id: leg.id, lots: leg.lots + 1 })}>
          <span className="whitespace-nowrap">×{leg.lots}
            <span style={{ color: "var(--oc-faint)" }}>
              {" "}· {leg.units.toLocaleString("en-IN")}</span>
          </span>
        </Stepper>
      </td>
      <td className="text-right pl-3">{num(leg.entry)}</td>
      <td className="text-right pl-3">{leg.ltp == null ? "—" : num(leg.ltp)}</td>
      <td className="text-right pl-3 whitespace-nowrap" style={{ color: "var(--oc-muted)" }}
        title={leg.delta == null ? "no solvable print"
          : `Γ ${leg.gamma} · Θ ${leg.theta}/day · V ${leg.vega}/1% (per share)`}>
        {leg.delta == null ? "—" : signed(leg.delta, 2)}
        <span className="text-[10px]" style={{ color: "var(--oc-faint)" }}>
          {leg.iv == null ? "" : ` ${leg.iv.toFixed(1)}`}
        </span>
      </td>
      <td className="text-right pl-3 font-semibold"
        style={{ color: (leg.pnl ?? 0) >= 0 ? "var(--oc-pos)" : "var(--oc-neg)" }}>
        {leg.pnl == null ? "—" : inr0(leg.pnl)}
      </td>
      <td className="text-right" style={{ color: "var(--oc-muted)" }}>
        {value == null ? "—" : inr0(value)}
      </td>
      {/* Exit, compact: a lots dropdown and an exit icon (the StockMock idiom the owner
          asked for, 2026-09-10). One lot held → no dropdown, just the icon. A pending
          add gets a delete icon instead: it was never held, so "exit" is the wrong word. */}
      <td className="text-right whitespace-nowrap">
        {leg.pending === "add" && onUnstage ? (
          <button type="button" onClick={() => onUnstage(leg.id)} title="drop this pending leg"
            className="w-[22px] h-[20px] rounded-[4px] text-[12px]"
            style={{ border: "1px solid var(--oc-line)", color: "var(--oc-neg)" }}>🗑</button>
        ) : (
          <span className="inline-flex items-center gap-1">
            {leg.lots > 1 && (
              <select value={exitLots} onChange={(e) => setExitLots(Number(e.target.value))}
                title={`how many of the ${leg.lots} lots to exit`}
                className="h-[20px] rounded-[4px] px-1 text-[10.5px] tabular-nums"
                style={{ background: "var(--oc-chip)", color: "var(--oc-ink)", border: "none" }}>
                {Array.from({ length: leg.lots }, (_, i) => i + 1).map((n) => (
                  <option key={n} value={n}>{n}</option>
                ))}
              </select>
            )}
            <button type="button"
              onClick={() => onStage({ kind: "exit", leg_id: leg.id, lots: exitLots })}
              title={leg.lots > 1 ? `exit ${exitLots} of ${leg.lots} lots at the cursor's price` : "exit this leg at the cursor's price"}
              className="w-[22px] h-[20px] rounded-full text-[13px] font-bold leading-[18px]"
              style={{ border: "1px solid var(--oc-neg)", color: "var(--oc-neg)" }}>⊖</button>
            {replay && onUnstage && (
              /* replay only: remove the leg as if it was never traded — no P&L, its fills leave
                 the journal (⊖ is an exit and books P&L) */
              <button type="button" onClick={() => onUnstage(leg.id)}
                title="delete this leg as if it was never traded (no P&L booked)"
                className="w-[22px] h-[20px] rounded-[4px] text-[12px]"
                style={{ border: "1px solid var(--oc-line)", color: "var(--oc-muted)" }}>🗑</button>
            )}
            </span>
        )}
      </td>
    </tr>
    {pendingNote && (
      /* the staged change on a HELD leg, as its own muted line — so a partial exit reads
         as "×1 of the ×30 is leaving", not as a leg that shrank (owner, 2026-09-10) */
      <tr style={{ background: "var(--oc-caution-dim)" }}>
        <td /><td />
        <td colSpan={7} className="py-1 text-[10.5px] font-semibold whitespace-nowrap"
          style={{ color: "var(--oc-caution)" }}>
          ↳ {pendingNote} · pending until Commit
        </td>
        <td className="text-right whitespace-nowrap py-1">
          {onUnstage && (
            <button type="button" onClick={() => onUnstage(leg.id, leg.pending)}
              title="take this change back (the leg stays as it was)"
              className="w-[22px] h-[20px] rounded-full text-[12px] leading-[18px]"
              style={{ border: "1px solid var(--oc-pos)", color: "var(--oc-pos)" }}>↺</button>
          )}
        </td>
      </tr>
    )}
    </>
  );
}

/** A value with − / + either side, ALWAYS visible. The hover-reveal version looked tidier
 *  and was unusable: the buttons appeared as the pointer arrived, the row re-flowed, and
 *  the target moved. A control meant to be clicked twice must not move between clicks. */
function Stepper({ children, onDown, onUp, title }: {
  children: React.ReactNode; onDown: () => void; onUp: () => void; title: string;
}) {
  return (
    <span className="inline-flex items-center gap-1.5" title={title}>
      <MiniBtn onClick={onDown}>−</MiniBtn>
      {children}
      <MiniBtn onClick={onUp}>+</MiniBtn>
    </span>
  );
}

function MiniBtn({ children, onClick, disabled }: {
  children: React.ReactNode; onClick: () => void; disabled?: boolean;
}) {
  return (
    <button type="button" onClick={onClick} disabled={disabled}
      className="w-[18px] h-[18px] rounded-[4px] text-[11px] leading-[17px] shrink-0 disabled:opacity-30 disabled:cursor-not-allowed"
      style={{ background: "var(--oc-chip)", color: "var(--oc-muted)" }}>{children}</button>
  );
}

function StripItem({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline gap-1.5 px-3 h-full shrink-0 whitespace-nowrap"
      style={{ borderRight: "1px solid var(--oc-hair)" }}>
      <span className="text-[9.5px] font-semibold uppercase tracking-[.07em]"
        style={{ color: "var(--oc-faint)" }}>{label}</span>
      <span className="text-[12px] font-semibold" style={{ color: "var(--oc-ink)" }}>
        {children}
      </span>
    </div>
  );
}

/* ------------------------------------------------------------------ the chain
 * 32px rows, exact column widths from the handoff. B/S buttons hold their space at rest
 * (55% opacity → 100% on hover) so a row NEVER reflows on hover — the handoff calls that
 * out explicitly and it is the difference between a usable ladder and a jumpy one. */
const COLS = "48px 78px 72px 72px 52px 72px 78px 48px";
// The D9 reflow (design handoff): under 1536px the ladder's columns tighten (456px instead
// of 520px), the chain panel and the rail narrow, and the payoff keeps what is left — at
// 1280px that is ~440px, still a readable chart, where the fixed widths pushed the rail
// off the right edge.
const COLS_NARROW = "40px 70px 62px 66px 46px 62px 70px 40px";

function useNarrow(): boolean {
  const q = "(max-width: 1535px)";
  const [narrow, setNarrow] = useState(() => typeof window !== "undefined" && window.matchMedia(q).matches);
  useEffect(() => {
    const m = window.matchMedia(q);
    const on = () => setNarrow(m.matches);
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, []);
  return narrow;
}

function ChainRow({ row, onPick, onProbe, probed, cols = COLS }: {
  row: ConsoleChainRow; cols?: string;
  onPick: (side: "B" | "S", right: "CE" | "PE", strike: number) => void;
  onProbe: (right: "CE" | "PE", strike: number) => void;
  probed: Record<string, ConsoleProbe>;
}) {
  const dead = !row.ce.quoted && !row.pe.quoted;
  return (
    <div className="grid items-center text-[12px] group"
      style={{
        gridTemplateColumns: cols, height: 32,
        opacity: dead ? 0.45 : 1,
        borderBottom: "1px solid var(--oc-hair)",
        background: (row.ce.held || row.pe.held) ? "var(--oc-accent-dim)"
          : row.atm ? "var(--oc-accent-tint)" : undefined,
        boxShadow: (row.ce.held || row.pe.held)
          ? "inset 2px 0 0 var(--oc-accent)" : undefined,
      }}>
      <Cell align="right" faint>{row.ce.delta == null ? "—" : num(row.ce.delta, 2)}</Cell>
      <PriceCell leg={row.ce} tint={row.itm_ce} right="CE" strike={row.strike} onProbe={onProbe}
        probed={probed[`CE${row.strike}`]} />
      <BsCell tint={row.itm_ce} onB={() => onPick("B", "CE", row.strike)} held={row.ce.held}
        onS={() => onPick("S", "CE", row.strike)} disabled={!row.ce.quoted} />
      <div className="h-full flex items-center justify-center font-semibold"
        style={{
          background: row.atm ? "var(--oc-accent-dim)" : "var(--oc-strike)",
          color: row.atm ? "var(--oc-accent)" : "var(--oc-ink)",
        }}>
        {row.strike.toLocaleString("en-IN")}
        {row.atm && <span className="ml-1 text-[8.5px] font-bold tracking-wide">ATM</span>}
      </div>
      <div className="h-full flex items-center justify-center text-[10.5px]"
        style={{ background: row.atm ? "var(--oc-accent-dim)" : "var(--oc-strike)",
          color: "var(--oc-muted)" }}>
        {row.iv == null ? "—" : num(row.iv, 2)}
      </div>
      <BsCell tint={row.itm_pe} onB={() => onPick("B", "PE", row.strike)} held={row.pe.held}
        onS={() => onPick("S", "PE", row.strike)} disabled={!row.pe.quoted} left />
      <PriceCell leg={row.pe} tint={row.itm_pe} right="PE" strike={row.strike} onProbe={onProbe}
        probed={probed[`PE${row.strike}`]} />
      <Cell align="right" faint>{row.pe.delta == null ? "—" : num(row.pe.delta, 2)}</Cell>
    </div>
  );
}

/** A price cell. When the strike has not printed the cell stays EMPTY — that is the honest
 *  state — but it offers a ⟲ that fetches the last price this contract ever traded at,
 *  which may be an earlier session. The answer renders in italics with its age, so a
 *  reference price can never be mistaken for a live one (owner ask, 2026-09-09). */
function PriceCell({ leg, tint, right, strike, onProbe, probed }: {
  leg: ConsoleChainLeg; tint?: boolean; right: "CE" | "PE"; strike: number;
  onProbe: (right: "CE" | "PE", strike: number) => void;
  probed?: ConsoleProbe;
}) {
  if (leg.quoted) {
    return <Cell align="right" tint={tint} strong>{num(leg.ltp)}</Cell>;
  }
  const ago = probed?.days_back
    ? `${probed.days_back}d`
    : probed?.age_min != null ? `${probed.age_min}m` : "";
  return (
    <div className="h-full flex items-center justify-end gap-1 px-2"
      style={{ background: tint ? "var(--oc-itm)" : undefined }}>
      {probed?.found ? (
        // Italic + muted + an age suffix, on ONE line: the cell is 78px inside a 32px row,
        // and a wrapped reference price pushes the ladder out of alignment.
        <span className="italic whitespace-nowrap text-[11px]" style={{ color: "var(--oc-muted)" }}
          title={`last traded ${probed.at} — a reference price, not a live quote`}>
          {num(probed.ltp)}<span className="text-[8.5px] not-italic"> {ago}</span>
        </span>
      ) : probed ? (
        <span className="text-[10px]" style={{ color: "var(--oc-faint)" }}>no print</span>
      ) : (
        <button type="button" onClick={() => onProbe(right, strike)}
          title="show the last price this contract traded at, even if it was an earlier day"
          className={"w-[17px] h-[16px] rounded-[3px] text-[10px] leading-[15px] opacity-40 "
            + "hover:opacity-100 transition"}
          style={{ color: "var(--oc-accent)", border: "1px solid var(--oc-line)" }}>⟲</button>
      )}
    </div>
  );
}

function Cell({ children, align = "left", faint, strong, tint }: {
  children: React.ReactNode; align?: "left" | "right"; faint?: boolean;
  strong?: boolean; tint?: boolean;
}) {
  return (
    <div className="h-full flex items-center px-2"
      style={{
        justifyContent: align === "right" ? "flex-end" : "flex-start",
        color: faint ? "var(--oc-faint)" : "var(--oc-ink)",
        fontWeight: strong ? 500 : 400,
        fontSize: faint ? 11 : undefined,
        background: tint ? "var(--oc-itm)" : undefined,
      }}>
      {children}
    </div>
  );
}

function BsCell({ onB, onS, disabled, tint, left, held }: {
  onB: () => void; onS: () => void; disabled?: boolean; tint?: boolean; left?: boolean;
  held?: { lots: number; side: "B" | "S"; enabled: boolean } | null;
}) {
  const btn = (label: "B" | "S", fn: () => void) => (
    <button type="button" onClick={fn} disabled={disabled}
      className={"w-[17px] h-[16px] rounded-[3px] text-[9.5px] font-bold leading-[16px] "
        + "opacity-55 group-hover:opacity-100 transition disabled:opacity-20"}
      style={{
        color: label === "B" ? "var(--oc-pos)" : "var(--oc-neg)",
        background: label === "B" ? "var(--oc-pos-fill)" : "var(--oc-neg-fill)",
        border: `1px solid ${label === "B" ? "var(--oc-pos)" : "var(--oc-neg)"}`,
      }}>
      {label}
    </button>
  );
  return (
    <div className="h-full flex items-center gap-1"
      style={{ justifyContent: left ? "flex-start" : "flex-end", paddingInline: 6,
        background: tint ? "var(--oc-itm)" : undefined }}>
      {/* where the position IS. Reading a ladder against a position you are holding in your
          head is how the wrong strike gets clicked. */}
      {held && (
        /* a SOLID chip: an outlined one on the ITM tint was too faint to spot (owner,
           2026-09-10) — the held marker is the one thing on the ladder that must jump out */
        <span className="px-1.5 rounded-[3px] text-[9px] font-bold leading-[16px] shadow-sm"
          title={`you hold ${held.lots} lot(s) here`}
          style={{ opacity: held.enabled ? 1 : 0.5, color: "#fff",
            background: held.side === "S" ? "var(--oc-neg)" : "var(--oc-pos)" }}>
          {held.side}×{held.lots}
        </span>
      )}
      {btn("B", onB)}{btn("S", onS)}
    </div>
  );
}

/* ------------------------------------------------------------------ page */
export default function ConsolePage() {
  // The session lives in the URL, so a replay is bookmarkable and shareable — "look at
  // NIFTY on 01 Apr at 09:30" is the unit of work here, and it also makes a page reload
  // land back where you were instead of the newest day.
  const [params, setParams] = useSearchParams();
  const [underlying, setUnderlying] = useState(params.get("u") ?? "NIFTY");
  const [day, setDay] = useState<string>(params.get("day") ?? "");
  const [state, setState] = useState<ConsoleState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showKeys, setShowKeys] = useState(false);
  const [lots, setLots] = useState(1);
  const opened = useRef(false);
  // Autoplay. The interval only fires a step when the previous one has answered, so a slow
  // backend cannot queue a backlog of steps that keep landing after Pause.
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const firedSeen = useRef<string>("");
  const lastKey = useRef<string>("");
  const [notice, setNotice] = useState<string | null>(null);
  useEffect(() => {
    if (!notice) return;
    const t = window.setTimeout(() => setNotice(null), 6000);
    return () => window.clearTimeout(t);
  }, [notice]);
  const [chainOpen, setChainOpen] = useState(true);
  const narrow = useNarrow();
  const [showPresets, setShowPresets] = useState(false);
  const [showSaves, setShowSaves] = useState(false);
  const [showSaveBox, setShowSaveBox] = useState(false);
  const [saveName, setSaveName] = useState("");
  const [realTyped, setRealTyped] = useState("");
  const [pendingSwitch, setPendingSwitch] = useState<string | null>(null);
  // Commit on a running deployment opens the TICKET (design D5) — the orders as a broker
  // would list them — and the send lives there. Esc closes it.
  const [ticketOpen, setTicketOpen] = useState(false);
  useEffect(() => { if (!state?.staged) setTicketOpen(false); }, [state?.staged]);
  const switchSource = (v: string) => {
    setPendingSwitch(null);
    if (v === "replay") {
      opened.current = false; setState(null); setParams({}, { replace: true }); open.mutate({ underlying, expiry: null });
    } else {
      openLive.mutate(Number(v.slice(4)));
    }
  };
  // The console over a RUNNING deployment: same DTO, no transport, every click staged and
  // applied through the run's own manual-order path. `isLive` = "not a replay".
  const isLive = !!state && state.session.mode !== "replay";
  // SIM mode (the Simulator, 2026-09-11): a replay session that belongs to a manual-backtest
  // strategy. Every action autosaves the tape to the strategy; a flat book after trading
  // offers the cycle for banking; a banked cycle can be watched back read-only (?cycle=).
  const simId = params.get("sim") ? Number(params.get("sim")) : null;
  const simCycleNo = params.get("cycle") ? Number(params.get("cycle")) : null;
  const simReadOnly = simId != null && simCycleNo != null;
  const [sim, setSim] = useState<SimOpenSpec | null>(null);
  const [bankNote, setBankNote] = useState("");
  const [bankBusy, setBankBusy] = useState(false);
  const [bankMsg, setBankMsg] = useState<string | null>(null);
  const simParams = () => (simId != null ? { sim: String(simId), ...(simCycleNo != null ? { cycle: String(simCycleNo) } : {}) } : {});
  const isReal = !!state && state.session.mode === "live";
  const { data: liveRuns } = useQuery({
    queryKey: ["console-live-runs"], queryFn: api.consoleLiveRuns,
    // a backend that just restarted is still rebuilding its runs: poll fast until it says done
    refetchInterval: (q) => (q.state.data?.recovering ? 3_000 : 30_000),
  });
  const openLive = useMutation({
    mutationFn: (run_id: number) => api.consoleOpenLive({ run_id }),
    onSuccess: (s) => {
      setState(s); setDay(s.session.date); setError(null); setPlaying(false);
      setUnderlying(s.session.underlying);
      setParams({ live: String(s.session.run_id ?? "") }, { replace: true });
    },
    onError: (e: Error) => setError(e.message),
  });
  // Live polls: the market moves on its own clock. Paused while a call is in flight so a
  // slow answer cannot land on top of a newer one.
  useEffect(() => {
    if (!isLive || !state) return;
    const t = window.setInterval(async () => {
      const cur = stateRef.current;
      if (!cur || cur.session.mode === "replay") return;
      try { setState(await api.consoleGet(cur.session.id)); } catch { /* the next tick retries */ }
    }, 3000);
    return () => window.clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isLive, state?.session.id]);
  const qc = useQueryClient();
  // The latest state, readable from inside a mutation without re-creating it.
  const stateRef = useRef<ConsoleState | null>(null);
  useEffect(() => { stateRef.current = state; }, [state]);
  // On a running deployment the STAGED book is the one shown — Positions, payoff, margin,
  // MTM, greeks — until Commit applies it or Revert drops it (owner, 2026-09-10). In
  // replay a click already IS the trade, so shown == held.
  const staging = !!state && state.session.mode !== "replay" && !!state.staged;
  const legsShown = staging ? state!.staged!.after_legs : (state?.legs ?? []);
  const risk = staging && state?.staged?.risk_after ? state.staged.risk_after : state?.risk;

  // A session lives in the backend's memory. A restart or an eviction drops it, and until
  // 2026-09-09 every click after that failed with a 404 nobody read — "the lots stepper
  // does nothing" was a dead session. Now any call that finds the session gone reopens
  // one at the same day/minute/expiry, hands back the journal and alerts the page still
  // holds, and retries ONCE against the new id. The book comes back exactly as a seek
  // would rebuild it.
  const lost = (e: unknown) =>
    e instanceof Error && e.message.startsWith("404") && /session not found/.test(e.message);
  const call = async <T,>(fn: (id: string) => Promise<T>): Promise<T> => {
    const cur = stateRef.current;
    if (!cur) throw new Error("no session");
    try {
      return await fn(cur.session.id);
    } catch (e) {
      if (!lost(e)) throw e;
      if (cur.session.mode !== "replay" && cur.session.run_id != null) {
        const again = await api.consoleOpenLive({ run_id: cur.session.run_id,
          expiry: cur.chain.expiry ?? undefined });
        stateRef.current = again; setState(again);
        return fn(again.session.id);
      }
      const fresh = await api.consoleOpen({
        underlying: cur.session.underlying, day: cur.session.date, at: cur.session.clock,
        expiry: cur.chain.expiry ?? undefined, capital: cur.session.capital,
        restore: { journal: cur.journal ?? [], alerts: cur.alerts ?? [], bookmarks: cur.bookmarks ?? [], discarded: cur.discarded ?? [] },
      });
      stateRef.current = fresh;
      setState(fresh);
      setNotice(`Session restored — the backend had dropped it. ${fresh.legs.length} leg${
        fresh.legs.length === 1 ? "" : "s"} rebuilt from the journal.`);
      return fn(fresh.session.id);
    }
  };

  const { data: days } = useQuery({
    queryKey: ["console-days", underlying],
    queryFn: () => api.consoleDays(underlying),
  });

  const open = useMutation({
    // `expiry: null` = a NEW day or underlying: let the backend land on that month's chip
    // (owner, 2026-09-10). Otherwise the URL's chip is kept, so a reload does not move it.
    mutationFn: ({ expiry, ...body }: { underlying: string; day?: string | null; at?: string; expiry?: string | null;
      capital?: number; restore?: SimOpenSpec["restore"] }) =>
      api.consoleOpen({ ...body, at: body.at ?? params.get("at") ?? "09:20",
        restore: body.restore ?? undefined,
        expiry: expiry === null ? undefined : (expiry ?? params.get("expiry") ?? undefined) }),
    onSuccess: (s) => {
      setState(s); setDay(s.session.date); setError(null);
      setParams({ ...simParams(), u: s.session.underlying, day: s.session.date, at: s.session.clock,
        ...(s.chain.expiry ? { expiry: s.chain.expiry } : {}) },
        { replace: true });
    },
    onError: (e: Error) => setError(e.message),
  });

  const move = useMutation({
    mutationFn: (body: Parameters<typeof api.consoleTransport>[1]) =>
      call((id) => api.consoleTransport(id, body)),
    onSuccess: (s, body) => {
      const prev = stateRef.current;
      const stuck = prev && body.op === "step" && (body.minutes ?? 0) > 0
        && prev.session.date === s.session.date && prev.session.clock === s.session.clock
        && s.session.played_pct >= 100;
      if (stuck && s.cycle?.done) {
        setNotice("Every leg has expired — the replay stops at this close. Reset the book, or press +1d to move on regardless.");
      } else if (stuck && !s.session.has_next_day) {
        setNotice(`${prettyDay(s.session.date)} is the last captured session in the 1-min store — nothing to replay past it yet. Today's bars are captured after 16:00 IST.`);
      }
      setState(s); setDay(s.session.date); setError(null);
      setParams({ ...simParams(), u: s.session.underlying, day: s.session.date, at: s.session.clock,
        ...(s.chain.expiry ? { expiry: s.chain.expiry } : {}) },
        { replace: true });
    },
    onError: (e: Error) => { setError(e.message); setPlaying(false); },
  });

  const armAlert = useMutation({
    mutationFn: (body: Parameters<typeof api.consoleArmAlert>[1]) =>
      call((id) => api.consoleArmAlert(id, body)),
    onSuccess: (s) => { setState(s); setError(null); },
    onError: (e: Error) => setError(e.message),
  });
  const clearAlert = useMutation({
    mutationFn: (aid: string) => call((id) => api.consoleClearAlert(id, aid)),
    onSuccess: setState,
  });

  const jump = useMutation({
    mutationFn: (body: { kind: string; pct?: number }) => call((id) => api.consoleJump(id, body)),
    onSuccess: (s) => {
      setState(s); setDay(s.session.date); setError(null);
      if (s.jumped === false) setNotice("No such event in this session's direction.");
      setParams({ ...simParams(), u: s.session.underlying, day: s.session.date, at: s.session.clock,
        ...(s.chain.expiry ? { expiry: s.chain.expiry } : {}) },
        { replace: true });
    },
    onError: (e: Error) => setError(e.message),
  });
  const bookmark = useMutation({
    mutationFn: () => {
      const cur = stateRef.current!;
      const key = `${cur.session.date}T${cur.session.clock}`;
      return cur.bookmarks.includes(key)
        ? call((id) => api.consoleUnbookmark(id, key))
        : call((id) => api.consoleBookmark(id));
    },
    onSuccess: setState,
  });
  // The design's multiplier: every leg's lots × n, as one undoable action. The multiple
  // is READ from the legs (the gcd of their lots), never tracked on the page — a tracked
  // counter drifted the moment Undo rebuilt the book (×2 shown against ×3 held).
  const mult = useMemo(() => {
    const gcd = (a: number, b: number): number => (b ? gcd(b, a % b) : a);
    return legsShown.map((l) => l.lots).reduce((g, n) => gcd(g, n), 0) || 1;
    }, [legsShown]);
  const scale = useMutation({
    mutationFn: (next: number) => call((id) => api.consoleScale(id, next / mult)),
    onSuccess: (s) => { setState(s); setError(null); },
    onError: (e: Error) => setError(e.message),
  });
  const unstage = useMutation({
    mutationFn: (body: { leg_id: string; kind?: string | null }) =>
      call((id) => api.consoleUnstage(id, body)),
    onSuccess: (s) => { setState(s); setError(null); },
    onError: (e: Error) => setError(e.message),
  });
  const applyPreset = useMutation({
    mutationFn: (body: { preset: string; lots: number }) =>
      call((id) => api.consoleApplyPreset(id, body)),
    onSuccess: (s) => { setState(s); setError(null); setShowPresets(false); },
    onError: (e: Error) => setError(e.message),
  });
  const save = useMutation({
    mutationFn: (name: string) => call((id) => api.consoleSave(id, name)),
    onSuccess: (r) => {
      setNotice(`Saved as "${r.name}".`); qc.invalidateQueries({ queryKey: ["console-saved"] });
    },
    onError: (e: Error) => setError(e.message),
  });
  const load = useMutation({
    mutationFn: (file: string) => api.consoleLoad(file),
    onSuccess: (s) => {
      setState(s); setDay(s.session.date); setUnderlying(s.session.underlying);
      setError(null); setShowSaves(false);
      setParams({ ...simParams(), u: s.session.underlying, day: s.session.date, at: s.session.clock,
        ...(s.chain.expiry ? { expiry: s.chain.expiry } : {}) },
        { replace: true });
    },
    onError: (e: Error) => setError(e.message),
  });
  const deleteSaved = useMutation({
    mutationFn: (file: string) => api.consoleDeleteSaved(file),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["console-saved"] }),
  });
  const { data: savedList } = useQuery({
    queryKey: ["console-saved"], queryFn: api.consoleSaved, enabled: showSaves,
  });
  // Presets are resolved server-side against the chain at the CURSOR, so they refetch
  // whenever the cursor, the ladder or the click-size changes.
  const { data: presetData } = useQuery({
    queryKey: ["console-presets", state?.session.id, state?.session.date, state?.session.clock,
      state?.chain.expiry, lots],
    queryFn: () => api.consolePresets(state!.session.id, lots),
    enabled: !!state && (showPresets || !state.legs.length),
  });

  // Autoplay ticks. Stops itself at the session close, on an error, and the moment an
  // alert FIRES — that is the minute the replay exists to look at, so it stays on screen
  // instead of scrolling past at 15 minutes a second. Hidden tab → pause (a replay that
  // ran on unwatched is a replay nobody saw).
  useEffect(() => {
    if (!playing || !state) return;
    const sp = SPEEDS[speed] ?? SPEEDS[1];
    const t = window.setInterval(() => {
      if (move.isPending) return;
      move.mutate({ op: "step", minutes: sp.minutes });
    }, sp.ms);
    return () => window.clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing, speed, state?.session.id]);
  useEffect(() => {
    if (!state) return;
    // The close no longer stops autoplay — a step past 15:40 rolls into the next captured
    // day. Only a cursor that did not move (the last day, pinned at its close) stops it.
    const key = `${state.session.date}T${state.session.clock}`;
    if (playing && lastKey.current === key && state.session.played_pct >= 100) setPlaying(false);
    lastKey.current = key;
    // An alert that was NOT fired at the previous cursor and is now: pause. Compared as
    // sets, so the very first fire counts too (a "was anything fired before" guard
    // silently let the first target scroll past at 15 minutes a second).
    const fired = state.alerts.filter((a) => a.state === "fired").map((a) => a.id);
    const seen = firedSeen.current ? firedSeen.current.split(",") : [];
    if (playing && fired.some((id) => !seen.includes(id))) setPlaying(false);
    firedSeen.current = fired.join(",");
  }, [state, playing]);
  useEffect(() => {
    const onVis = () => { if (document.hidden) setPlaying(false); };
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, []);

  const stage = useMutation({
    mutationFn: (body: Parameters<typeof api.consoleStage>[1]) =>
      call((id) => api.consoleStage(id, body)),
    onSuccess: (s) => { setState(s); setError(null); },
    onError: (e: Error) => setError(e.message),
  });
  const commit = useMutation({
    mutationFn: (limits?: Record<string, number>) =>
      call((id) => api.consoleCommit(id, limits && Object.keys(limits).length ? { limits } : undefined)),
    onSuccess: (s) => { setState(s); setError(null); },
    onError: (e: Error) => setError(e.message),
  });
  const undo = useMutation({
    mutationFn: () => call((id) => api.consoleUndo(id)),
    onSuccess: setState,
  });
  const reset = useMutation({
    mutationFn: () => call((id) => api.consoleReset(id)),
    onSuccess: setState,
  });
  // the manual margin anchor: the broker calculator's figure for ONE lot-set (replay only)
  const [anchorOpen, setAnchorOpen] = useState(false);
  const [anchorText, setAnchorText] = useState("");
  const anchor = useMutation({
    mutationFn: (v: number) => call((id) => api.consoleMarginAnchor(id, v)),
    onSuccess: (st) => { setState(st); setAnchorOpen(false); },
  });
  const anchorEditor = anchorOpen && !isLive ? (
    <div className="flex flex-wrap items-center gap-1.5 mt-1.5 text-[10.5px]" style={{ color: "var(--oc-muted)" }}>
      <span>Margin anchor · ₹ per lot-set</span>
      <input value={anchorText} onChange={(e) => setAnchorText(e.target.value)}
        placeholder={String(state?.session.margin_per_lot_set || "e.g. 90828")}
        inputMode="numeric"
        className="w-[88px] px-1.5 py-[2px] rounded-[4px] border text-[11px] tabular-nums"
        style={{ borderColor: "var(--oc-line)", background: "var(--oc-panel)", color: "var(--oc-ink)" }}
        onKeyDown={(e) => {
          if (e.key === "Enter") { const v = Number(anchorText); if (Number.isFinite(v) && v >= 0) anchor.mutate(v); }
          if (e.key === "Escape") setAnchorOpen(false);
        }} />
      <button type="button" className="underline" disabled={anchor.isPending}
        onClick={() => { const v = Number(anchorText); if (Number.isFinite(v) && v >= 0) anchor.mutate(v); }}>set</button>
      {!!state?.session.margin_per_lot_set && (
        <button type="button" className="underline" onClick={() => anchor.mutate(0)}>clear</button>
      )}
      <button type="button" onClick={() => setAnchorOpen(false)} style={{ color: "var(--oc-faint)" }}>✕</button>
    </div>
  ) : null;
  const discard = useMutation({
    mutationFn: () => call((id) => api.consoleDiscard(id)),
    onSuccess: setState,
  });

  const setFifty = useMutation({
    mutationFn: (on: boolean) => call((id) => api.consoleChain(id, { allow_fifty_strikes: on })),
    onSuccess: setState,
  });
  const pickExpiry = useMutation({
    mutationFn: (expiry: string) => call((id) => api.consoleChain(id, { expiry })),
    onSuccess: (s) => {
      setState(s);
      setParams({ ...simParams(), u: s.session.underlying, day: s.session.date, at: s.session.clock,
        ...(s.chain.expiry ? { expiry: s.chain.expiry } : {}) }, { replace: true });
    },
  });

  // SIM: open where the strategy stands (its open cycle, or its next day), or a banked
  // cycle read-only. The strategy's spec decides day, clock, expiry, capital and the tape.
  const openSim = async () => {
    if (simId == null) return;
    try {
      const spec = simCycleNo != null ? await api.simCycle(simId, simCycleNo) : await api.simOpen(simId);
      setSim(spec);
      setUnderlying(spec.underlying);
      open.mutate({ underlying: spec.underlying, day: spec.day, at: spec.at, expiry: spec.expiry,
        capital: spec.capital, restore: spec.restore ?? undefined });
    } catch (e) { setError((e as Error).message); }
  };
  // autosave the open cycle's tape after every change (debounced); never on a read-only replay
  const journalKey = state ? `${state.session.date}|${state.session.clock}|${state.journal.length}|${state.alerts.length}|${state.bookmarks.length}|${state.discarded?.length ?? 0}` : "";
  useEffect(() => {
    if (simId == null || simReadOnly || !state || state.session.mode !== "replay") return;
    const t = window.setTimeout(() => {
      api.simAutosave(simId, { day: state.session.date, clock: state.session.clock,
        expiry: state.chain.expiry, capital: state.session.capital,
        journal: state.journal, alerts: state.alerts, bookmarks: state.bookmarks,
        discarded: state.discarded ?? [] }).catch(() => {});
    }, 700);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [journalKey, simId, simReadOnly]);
  const simTraded = !!state && state.journal.some((f) => f.action === "BUY" || f.action === "SHORT");
  const simFlat = !!state && (state.risk?.legs_open ?? 0) === 0;
  // the margin the cycle NEEDED: the peak while legs were open (at bank the book is flat and
  // reads ₹0). Reset when a cycle is banked / a new session opens.
  const cycleMargin = useRef<{ value: number; source: string | null }>({ value: 0, source: null });
  useEffect(() => {
    if (simId == null || !state || (state.risk?.legs_open ?? 0) === 0) return;
    if ((state.risk?.margin ?? 0) > cycleMargin.current.value)
      cycleMargin.current = { value: state.risk!.margin, source: state.risk!.margin_source };
  }, [simId, state]);
  useEffect(() => { cycleMargin.current = { value: 0, source: null }; }, [state?.session.id]);
  const bankCycle = async () => {
    if (simId == null || !state) return;
    setBankBusy(true); setBankMsg(null);
    try {
      const out = await api.simBank(simId, { note: bankNote,
        margin: cycleMargin.current.value || null, margin_source: cycleMargin.current.source,
        payload: { day: state.session.date, clock: state.session.clock, expiry: state.chain.expiry,
          capital: state.session.capital, journal: state.journal, alerts: state.alerts, bookmarks: state.bookmarks,
          discarded: state.discarded ?? [] } });
      setBankNote("");
      setBankMsg(`Cycle ${out.banked.n} banked · net ${inr0(out.banked.net)} · equity ${inr0(out.equity)}${out.next_day ? ` · next cycle opens ${out.next_day}` : " · no later captured day"}`);
      // the next cycle: a fresh session at the strategy's next day with the compounded capital
      await openSim();
    } catch (e) { setBankMsg((e as Error).message); }
    finally { setBankBusy(false); }
  };

  // Open a session once the store's day list is known — the newest captured day.
  useEffect(() => {
    if (!days || opened.current) return;
    opened.current = true;
    const liveId = params.get("live");
    if (liveId) { openLive.mutate(Number(liveId)); return; }
    if (simId != null) { openSim(); return; }
    open.mutate({ underlying, day: params.get("day") ?? days.last });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [days]);

  // Transport keys. Keyed on e.CODE, not e.key: Shift+"." is ">" on every layout, so the
  // Shift ladder silently did nothing while the unshifted keys worked (owner, 2026-09-09).
  // The ladder is , . = 1m · Shift = 15m · Alt = 1h · [ ] = 1 day · Home/End = SOD/EOD.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (el && /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName)) return;
      if (e.key === "?" || (e.key === "/" && e.shiftKey)) {
        e.preventDefault(); setShowKeys((v) => !v); return;
      }
      if (e.key === "Escape") {
        setShowKeys(false);
        if (ticketOpen) { setTicketOpen(false); return; }
        if (state?.staged) discard.mutate();
        return;
      }
      if (e.key === "Enter" && state?.staged && state.session.mode !== "replay") {
        e.preventDefault(); if (ticketOpen) { if (!isReal) commit.mutate(undefined); } else setTicketOpen(true); return;
      }
      if ((e.key === "u" || e.key === "U") && state?.session.can_undo) {
        e.preventDefault(); undo.mutate(); return;
      }
      if ((e.code === "Space" || e.key === " ") && state) {
        e.preventDefault(); setPlaying((v) => !v); return;
      }
      if ((e.key === "b" || e.key === "B") && state) { e.preventDefault(); bookmark.mutate(); return; }
      if ((e.key === "h" || e.key === "H") && state) { e.preventDefault(); setChainOpen((v) => !v); return; }
      if ((e.key === "j" || e.key === "J") && state) { e.preventDefault(); jump.mutate({ kind: "next_fill" }); return; }
      if ((e.key === "k" || e.key === "K") && state) { e.preventDefault(); jump.mutate({ kind: "prev_fill" }); return; }
      if (!state || e.metaKey || e.ctrlKey) return;
      if (state.session.mode !== "replay") return;      // live runs on the market's clock
      // Match on code OR key. e.key changes under Shift ("." becomes ">"), which is why the
      // Shift ladder did nothing when this keyed on e.key alone; e.code is stable but is not
      // always populated (synthetic events, some layouts), so accept either.
      const c = e.code, k = e.key;
      const prevDay = c === "BracketLeft" || k === "[" || k === "{";
      const nextDay = c === "BracketRight" || k === "]" || k === "}";
      const back = c === "Comma" || k === "," || k === "<";
      const fwd = c === "Period" || k === "." || k === ">";
      const sod = c === "Home" || k === "Home";
      const eod = c === "End" || k === "End";
      if (!(prevDay || nextDay || back || fwd || sod || eod)) return;
      e.preventDefault();
      if (sod) move.mutate({ op: "sod" });
      else if (eod) move.mutate({ op: "eod" });
      else if (prevDay || nextDay) move.mutate({ op: "day", days: prevDay ? -1 : 1 });
      else {
        // owner 2026-09-10: the bare keys walk the HOUR (the common move), Alt the minute
        const step = e.altKey ? 1 : e.shiftKey ? 15 : 60;
        move.mutate({ op: "step", minutes: (back ? -1 : 1) * step });
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [state, move, commit, discard, undo, bookmark, jump]);

  // Probed reference prices, keyed "CE24000". Cleared whenever the cursor or the ladder
  // moves — a price fetched at 09:30 is not an answer about 11:00.
  const [probed, setProbed] = useState<Record<string, ConsoleProbe>>({});
  const probe = async (right: "CE" | "PE", strike: number) => {
    if (!state) return;
    const key = `${right}${strike}`;
    try {
      const r = await call((id) => api.consoleProbe(id, right, strike));
      setProbed((p) => ({ ...p, [key]: r }));
    } catch {
      setProbed((p) => ({ ...p, [key]: { symbol: key, found: false } }));
    }
  };
  useEffect(() => { setProbed({}); },
    [state?.session.clock, state?.session.date, state?.chain.expiry]);

  // §9: this page renders BEFORE the query resolves, so every read below must survive a
  // null state. Nothing here dot-accesses into a derived map without a guard.
  const rows = state?.chain.rows ?? [];
  // The ladder's own strike step, so "roll a strike" moves exactly one row rather than a
  // guessed 100 — SENSEX and BANKNIFTY do not share NIFTY's grid.
  const gridStep = useMemo(() => {
    const ks = (state?.chain.rows ?? []).map((r) => r.strike).sort((a, b) => a - b);
    const gaps = ks.slice(1).map((k, i) => k - ks[i]).filter((g) => g > 0);
    return gaps.length ? Math.min(...gaps) : 100;
  }, [state?.chain.rows]);
  // The rail, the staged bar and the chart all read ONE calculator (lib/payoff.ts), so they
  // cannot disagree about what the same book is worth. §9: this runs on the first render
  // with no state at all, so every input is guarded.
  const spotNow = state?.market.spot ?? null;
  const expNow = state?.chain.expiry ?? null;
  const mNow = useMemo(() => {
    const legs = toPayoffLegs(legsShown);
    return spotNow && expNow && legs.length
      ? computeMetrics(legs, spotNow, expNow, state?.session.date) : null;
      }, [legsShown, spotNow, expNow, state?.session.date]);
      const mBeforeStaged = useMemo(() => {
        const legs = toPayoffLegs(state?.legs ?? []);
        return spotNow && expNow && legs.length
          ? computeMetrics(legs, spotNow, expNow, state?.session.date) : null;
      }, [state?.legs, spotNow, expNow, state?.session.date]);
      const mStaged = useMemo(() => {
    const legs = toPayoffLegs(state?.staged?.after_legs ?? []);
    return spotNow && expNow && legs.length
      ? computeMetrics(legs, spotNow, expNow, state?.session.date) : null;
  }, [state?.staged, spotNow, expNow, state?.session.date]);
  // T+0 at −1% / spot / +1%: the open book's model value across a narrow window, read at
  // its three ends. The same curve the chart draws dashed, so the tiles and the chart agree.
  const scen = useMemo(() => {
    const legs = toPayoffLegs(legsShown);
    if (!spotNow || !expNow || !legs.length) return null;
    const d = buildLivePayoff(legs, spotNow, expNow, state?.session.date, null, undefined,
      { range: [spotNow * 0.99, spotNow * 1.01] });
    if (!d?.data.length) return null;
    const n = d.data.length;
    return [d.data[0].now, d.data[Math.floor(n / 2)].now, d.data[n - 1].now];
    }, [legsShown, spotNow, expNow, state?.session.date]);
  // the cycle's breakevens: zero crossings of the expiry curve WITH the cycle's realised
  // added — the open book's own crossings move once something has been banked
  const cycleBE = useMemo(() => {
    const legs = toPayoffLegs(legsShown);
    if (!spotNow || !expNow || !legs.length) return [] as number[];
    const off = risk?.realised ?? 0;
    const lo = Math.min(spotNow, ...legs.map((l) => l.strike)) * 0.9;
    const hi = Math.max(spotNow, ...legs.map((l) => l.strike)) * 1.1;
    const d = buildLivePayoff(legs, spotNow, expNow, state?.session.date, null, undefined,
      { range: [lo, hi], offset: off });
    const out: number[] = [];
    const pts = d?.data ?? [];
    for (let i = 1; i < pts.length; i++) {
      const a = pts[i - 1], b = pts[i];
      if ((a.expiry < 0 && b.expiry >= 0) || (a.expiry >= 0 && b.expiry < 0)) {
        out.push(a.spot + (a.expiry / (a.expiry - b.expiry)) * (b.spot - a.spot));
      }
    }
    return out;
  }, [legsShown, spotNow, expNow, state?.session.date, risk?.realised]);
  // one standard deviation of the underlying to the selected expiry: spot × ATM IV × √t,
  // the ATM IV being the ladder's own (the OTM side's), t the chain's DTE (floored at a day)
  const sigma1 = useMemo(() => {
    const atm = state?.chain.rows.find((r) => r.atm);
    const iv = atm?.iv;
    const dte = state?.market.dte;
    if (!spotNow || iv == null || dte == null) return null;
    return spotNow * (iv / 100) * Math.sqrt(Math.max(dte, 1) / 365);
  }, [state?.chain.rows, state?.market.dte, spotNow]);
  const busy = open.isPending || move.isPending;
  const breach = state?.alerts.find((a) => a.kind === "stop" && a.state === "fired") ?? null;
  // legs the MARKET closed (expiry settlement) that are in the cursor's past
  const settled = useMemo(() => (state?.fills ?? []).filter((f) => f.action === "SETTLE"),
    [state?.fills]);

  const payoffPanel = (
                <Panel className="flex flex-col min-h-0">
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-[13px] font-semibold">
                      {showPresets || !legsShown.length ? "Presets" : "Payoff"}
                      {(showPresets || !legsShown.length) && (
                        <span className="ml-2 text-[10.5px] font-normal" style={{ color: "var(--oc-faint)" }}>
                          resolved at {state?.session.clock ?? "—"} on {state?.chain.expiry ? expiryChip(state.chain.expiry) : "—"} · ×{lots}
                        </span>
                      )}
                    </span>
                    <span className="flex items-center gap-2 text-[10.5px]" style={{ color: "var(--oc-faint)" }}>
                      {state?.legs.length ? (
                        <Chip active={showPresets} onClick={() => setShowPresets((v) => !v)}
                          title="prebuilt structures, resolved against the chain at the cursor">
                          {showPresets ? "payoff" : "presets"}</Chip>
                      ) : null}
                      {!showPresets && legsShown.length ? "expiry · T+0 dashed · staged dotted" : null}
                    </span>
                  </div>
                  {/* a fixed chart height, so Positions sits right under it instead of at the
                      bottom of a chart stretched to the rail's height (owner, 2026-09-09) */}
                  <div style={{ height: chainOpen ? 300 : 420 }}>
                    {(showPresets || !legsShown.length) ? (
                      <PresetGallery presets={presetData?.presets ?? []} state={state} lots={lots}
                        onApply={(id) => applyPreset.mutate({ preset: id, lots })} />
                    ) : (
                      <PayoffSvg legs={legsShown} staged={staging ? null : (state?.staged?.after_legs ?? null)}
                        spot={state?.market.spot ?? null} expiry={state?.chain.expiry ?? null}
                        today={state?.session.date ?? ""} alerts={state?.alerts ?? []}
                        realised={risk?.realised ?? 0} sigma={sigma1} underlying={state?.session.underlying} />
                    )}
                  </div>
                  {/* NET GREEKS: Σ per-share greek × units over the enabled legs, the same
                      convention as the Live tile. Beside them, the T+0 book at ±1% of spot —
                      the question the greeks approximate, answered directly. */}
                  {/* two fixed rows — greeks, then the T+0 what-ifs — so the last tile never wraps
                      onto a line of its own (owner, 2026-09-10) */}
                  <div className="mt-2 pt-2 grid grid-cols-4 gap-1.5" style={{ borderTop: "1px solid var(--oc-hair)" }}>
                    <GreekCell label="Δ net" v={risk?.greeks.delta} dp={1} unit="units" />
                    <GreekCell label="Γ net" v={risk?.greeks.gamma} dp={3} />
                    <GreekCell label="Θ /day" v={risk?.greeks.theta} dp={0} inr />
                    <GreekCell label="Vega /1%" v={risk?.greeks.vega} dp={0} inr />
                  </div>
                  <div className="mt-1.5 grid grid-cols-3 gap-1.5">
                    {/* cumulative: the cycle's realised rides on every T+0 figure, so "T+0 spot" IS the
                        cycle MTM and the ±1% tiles are where the cycle would stand (owner, 2026-09-10) */}
                    <ScenarioCell label={`${MINUS}1%`} v={scen ? scen[0] + (risk?.realised ?? 0) : null} base={scen ? scen[1] + (risk?.realised ?? 0) : null} />
                    <ScenarioCell label="spot" v={scen ? scen[1] + (risk?.realised ?? 0) : null} base={scen ? scen[1] + (risk?.realised ?? 0) : null} />
                    <ScenarioCell label="+1%" v={scen ? scen[2] + (risk?.realised ?? 0) : null} base={scen ? scen[1] + (risk?.realised ?? 0) : null} />
                  </div>
                </Panel>
  );


  const mtmPanel = (
                <Panel className={breach ? "border-l-4" : ""}
                  style={breach ? { borderLeftColor: "var(--oc-neg)" } : undefined}>
                  {breach && (
                    <div className="mb-2 px-2 py-1 rounded-[6px] text-[11px] font-semibold"
                      style={{ background: "var(--oc-neg-fill)", color: "var(--oc-neg)" }}>
                      STOP FIRED {breach.fired_at?.slice(11)} · MTM {inr0(breach.fired_value ?? 0)} crossed {inr0(-Math.abs(breach.value))}
                    </div>
                  )}
                  <div className="flex items-start justify-between">
                    <span className="text-[9.5px] font-semibold uppercase tracking-[.07em]"
                      style={{ color: "var(--oc-faint)" }}>Cycle MTM · realised + open
                      <InfoIcon title="how the numbers are made" text={marginHelp(risk)} />
                    </span>
                    <span className="px-1.5 py-[1px] rounded-[3px] text-[9px] font-bold"
                      style={{ background: risk && risk.legs_open ? "var(--oc-chip)" : "transparent",
                        color: "var(--oc-muted)" }}>
                      {!risk?.legs_open ? "NO POSITION"
                        : mNow && !mNow.maxLossUnlimited ? "DEFINED RISK" : "UNDEFINED RISK"}
                    </span>
                  </div>
                  <div className="text-[21px] font-semibold mt-1 flex items-baseline gap-2"
                    style={{ color: !risk?.mtm ? "var(--oc-ink)"
                      : risk.mtm > 0 ? "var(--oc-pos)" : "var(--oc-neg)" }}>
                    {inr0(risk?.mtm ?? 0)}
                    {!!risk?.margin && (
                      <span className="text-[12px] font-semibold" title={`of ${risk.margin_source} margin`}>
                        {pctOf(risk.mtm, risk.margin)} of margin
                      </span>
                    )}
                    {mNow?.rewardRisk ? (
                      <span className="text-[12px] font-semibold" style={{ color: "var(--oc-ink)" }}
                        title="reward : risk — max profit over max loss on the cycle basis">
                        R:R {mNow.rewardRisk.toFixed(1)}
                      </span>
                    ) : null}
                    </div>
                  <div className="text-[11px] mt-0.5" style={{ color: "var(--oc-muted)" }}>
                    {risk?.legs_open ? `${risk.legs_open} legs open` : "no open position"}
                    {risk?.legs_open && state?.cycle?.entry_at
                      ? ` · entered ${state.cycle.entry_at.slice(11)}${state.cycle.entry_at.slice(0, 10) !== state.session.date ? ` on ${expiryChip(state.cycle.entry_at.slice(0, 10))}` : ""}${state.cycle.entry_spot ? ` at ${num(state.cycle.entry_spot, 0)}` : ""}`
                      : ""}
                    {" · "}{isLive ? "as of" : "paused"} {state?.session.clock ?? "—"}
                  </div>
                  {/* A session's realised P&L survives closing the position — it is money you
                      made. Said plainly, because "MTM ₹5,487 · NO POSITION" reads as a bug, and
                      a later structure's rail would otherwise show the previous one's profit as
                      if it were its own. Reset clears it. */}
                  {!!(risk?.realised || risk?.realised_total) && (
                    <div className="text-[11px] mt-1 flex items-center gap-2">
                      <span style={{ color: "var(--oc-faint)" }} title="the cycle = since the book last opened from flat">
                        {risk.legs_open
                          ? `cycle banked ${inr0(risk.realised)} from closed legs`
                          : `${inr0(risk.realised)} banked this cycle · nothing open`}
                        {risk.realised_total !== risk.realised ? ` · ${inr0(risk.realised_total)} in total` : ""}
                      </span>
                      <button type="button" onClick={() => reset.mutate()}
                        className="underline" style={{ color: "var(--oc-muted)" }}>reset</button>
                    </div>
                  )}
                  <div className="grid grid-cols-3 gap-2 mt-3">
                    <Tile label="Margin" value={inr0(risk?.margin ?? 0)}
                      sub={marginSub(risk, state?.session.margin_per_lot_set)}
                      onClick={isLive ? undefined : () => setAnchorOpen((v) => !v)} />
                    <Tile label="POP" value={mNow?.pop == null ? "—" : `${(mNow.pop * 100).toFixed(1)}%`}
                      sub={mNow?.rewardRisk ? `R:R ${mNow.rewardRisk.toFixed(1)}` : "—"} />
                    {/* cycle basis: the open book's max P/L shifted by what the cycle has already banked,
                        and the breakevens of that shifted curve — the same line the chart draws */}
                    <Tile label="Max profit" tone="pos"
                      value={mNow ? (mNow.maxProfitUnlimited ? "unlimited" : inr0(mNow.maxProfit + (risk?.realised ?? 0))) : "—"}
                      sub={pctOf(mNow ? mNow.maxProfit + (risk?.realised ?? 0) : undefined, risk?.margin) + " of margin"} />
                    <Tile label="Max loss" tone="neg"
                      value={mNow ? (mNow.maxLossUnlimited ? "unlimited" : inr0(mNow.maxLoss + (risk?.realised ?? 0))) : "—"}
                      sub={pctOf(mNow ? mNow.maxLoss + (risk?.realised ?? 0) : undefined, risk?.margin) + " of margin"} />
                    <Tile label="Breakeven"
                      value={cycleBE.length
                        ? cycleBE.map((b) => Math.round(b).toLocaleString("en-IN")).join(" / ")
                        : "—"}
                      sub={cycleBE.length && state?.market.spot
                        ? `${signed(100 * (cycleBE[0] / state.market.spot - 1))}% from spot` : "—"} />
                    <Tile label="Open P&L" tone={(risk?.unrealised ?? 0) >= 0 ? "pos" : "neg"}
                      value={`${inr0(risk?.unrealised ?? 0)}${risk?.margin ? ` · ${pctOf(risk.unrealised, risk.margin)}` : ""}`}
                      sub={`banked ${inr0(risk?.realised ?? 0)} · ${inr0(-(risk?.charges ?? 0))} costs`} />
                    <Tile label={risk?.net_credit != null && risk.net_credit < 0 ? "Net debit" : "Net credit"}
                      tone={risk?.net_credit == null ? undefined : risk.net_credit >= 0 ? "pos" : "neg"}
                      value={risk?.net_credit == null ? "—" : inr0(Math.abs(risk.net_credit))}
                      sub={risk?.net_credit == null ? "—" : risk.net_credit >= 0 ? "premium received at entry" : "premium paid at entry"} />
                    </div>
                    {anchorEditor}
                </Panel>
  );

  const alertsCard = (
                <AlertsCard alerts={state?.alerts ?? []} disabled={!state}
                  manualMode={state?.session.managed_by === "manual"}
                  onArm={(b) => armAlert.mutate(b)} onClear={(id) => clearAlert.mutate(id)} />
  );

  const railColumn = (
    <>
      {mtmPanel}
      {alertsCard}
    </>
  );

  /* the rail's numbers as one strip under the chart, for when the chain is hidden and
     the left column belongs to Positions (owner, 2026-09-09) */
  const riskStrip = (
    <Panel className={breach ? "border-l-4" : ""}
      style={breach ? { borderLeftColor: "var(--oc-neg)" } : undefined}>
      {breach && (
        <div className="mb-2 px-2 py-1 rounded-[6px] text-[11px] font-semibold"
          style={{ background: "var(--oc-neg-fill)", color: "var(--oc-neg)" }}>
          STOP FIRED {breach.fired_at?.slice(11)} · MTM {inr0(breach.fired_value ?? 0)} crossed {inr0(-Math.abs(breach.value))}
        </div>
      )}
      <div className="flex flex-wrap gap-1.5 [&>*]:flex-1 [&>*]:min-w-[110px]">
        <Tile label={`Cycle MTM · ${!risk?.legs_open ? "no position" : mNow && !mNow.maxLossUnlimited ? "defined risk" : "undefined risk"}`}
          tone={!risk?.mtm ? undefined : risk.mtm > 0 ? "pos" : "neg"}
          value={`${inr0(risk?.mtm ?? 0)}${risk?.margin ? ` · ${pctOf(risk.mtm, risk.margin)}` : ""}`}
          sub={risk?.realised ? `banked ${inr0(risk.realised)}` : `${risk?.legs_open ?? 0} legs open`} />
        <Tile label="Margin" value={inr0(risk?.margin ?? 0)}
          sub={marginSub(risk, state?.session.margin_per_lot_set)}
          onClick={isLive ? undefined : () => setAnchorOpen((v) => !v)} />
        <Tile label="POP" value={mNow?.pop == null ? "—" : `${(mNow.pop * 100).toFixed(1)}%`}
          sub={mNow?.rewardRisk ? `R:R ${mNow.rewardRisk.toFixed(1)}` : "—"} />
        <Tile label="Max profit" tone="pos"
          value={mNow ? (mNow.maxProfitUnlimited ? "unlimited" : inr0(mNow.maxProfit + (risk?.realised ?? 0))) : "—"}
          sub={pctOf(mNow ? mNow.maxProfit + (risk?.realised ?? 0) : undefined, risk?.margin) + " of margin"} />
        <Tile label="Max loss" tone="neg"
          value={mNow ? (mNow.maxLossUnlimited ? "unlimited" : inr0(mNow.maxLoss + (risk?.realised ?? 0))) : "—"}
          sub={pctOf(mNow ? mNow.maxLoss + (risk?.realised ?? 0) : undefined, risk?.margin) + " of margin"} />
        <Tile label="Breakeven"
          value={cycleBE.length
            ? cycleBE.map((b) => Math.round(b).toLocaleString("en-IN")).join(" / ") : "—"}
          sub={cycleBE.length && state?.market.spot
            ? `${signed(100 * (cycleBE[0] / state.market.spot - 1))}% from spot` : "—"} />
        <Tile label="Open P&L" tone={(risk?.unrealised ?? 0) >= 0 ? "pos" : "neg"}
          value={`${inr0(risk?.unrealised ?? 0)}${risk?.margin ? ` · ${pctOf(risk.unrealised, risk.margin)}` : ""}`}
          sub={`${inr0(-(risk?.charges ?? 0))} costs`} />
        <Tile label={risk?.net_credit != null && risk.net_credit < 0 ? "Net debit" : "Net credit"}
          tone={risk?.net_credit == null ? undefined : risk.net_credit >= 0 ? "pos" : "neg"}
          value={risk?.net_credit == null ? "—" : inr0(Math.abs(risk.net_credit))}
          sub={risk?.net_credit == null ? "—" : "at entry"} />
      </div>
      {anchorEditor}
    </Panel>
  );


  const positionsPanel = (
              <Panel>
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-[13px] font-semibold">Positions</span>
              <span className="text-[10.5px] flex items-center gap-1" style={{ color: "var(--oc-faint)" }}>
                {state?.legs.length ? (
                  <span className="inline-flex items-center gap-1 mr-2"
                    title="multiplier: every leg's lots × n, one action (U undoes)">
                    <MiniBtn disabled={mult <= 1} onClick={() => scale.mutate(mult - 1)}>−</MiniBtn>
                    <b className="text-[11px] tabular-nums" style={{ color: "var(--oc-ink)", minWidth: 22, textAlign: "center" }}>
                      ×{mult}</b>
                    <MiniBtn onClick={() => scale.mutate(mult + 1)}>+</MiniBtn>
                  </span>
                ) : null}
                {legsShown.length} legs · lot {state?.session.lot_size ?? "—"}
                {staging ? (
                  <span className="ml-3 inline-flex items-center gap-2">
                    <span className="px-1.5 py-[1px] rounded-[3px] text-[9px] font-bold"
                      style={{ background: "var(--oc-caution-dim)", color: "var(--oc-caution)" }}>
                      {state!.staged!.items.length} UNCOMMITTED
                    </span>
                    <button type="button"
                      disabled={commit.isPending || !!state?.session.order_error}
                      onClick={() => setTicketOpen(true)}
                      className="px-2 h-[20px] rounded-[4px] text-[10.5px] font-semibold disabled:opacity-40"
                      style={{ background: isReal ? "var(--oc-neg)" : "var(--oc-accent)", color: "#fff" }}>
                      {isReal ? "Review & commit to broker" : "Review & commit"}</button>
                    <button type="button" onClick={() => discard.mutate()} className="underline"
                      style={{ color: "var(--oc-muted)" }}>Revert</button>
                  </span>
                ) : null}
                {state?.session.can_undo ? (
                  <button type="button" className="ml-3 underline"
                    title="undo the last action (U) — the whole action, roll and basket included"
                    onClick={() => undo.mutate()}
                    style={{ color: "var(--oc-accent)" }}>Undo</button>
                ) : null}
                {state?.legs.length ? (
                  <button type="button" className="ml-3 underline"
                    onClick={() => stage.mutate({ kind: "flatten", replace: true })}
                    style={{ color: "var(--oc-neg)" }}>Exit all</button>
                ) : null}
                {!isLive && (state?.legs.length || risk?.realised) ? (
                  <button type="button" className="ml-3 underline"
                    title="clear the book AND this session's realised P&L — a clean slate"
                    onClick={() => reset.mutate()}
                    style={{ color: "var(--oc-muted)" }}>Reset</button>
                ) : null}
              </span>
            </div>
            {!legsShown.length && !state?.closed?.length ? (
              <div className="py-4 text-center text-[12px]" style={{ color: "var(--oc-faint)" }}>
                {settled.length ? (
                  <div className="mb-1" style={{ color: "var(--oc-caution)" }}>
                    Settled at expiry: {settled.map((f) => `${formatOptionSymbol(f.symbol)} @ ${num(f.price)}`).join(" · ")}
                    {" "}· {settled[0].at.slice(0, 10)} 15:30. Banked P&L is in the rail; Undo does not reach a settlement.
                  </div>
                ) : null}
                No position. Click B or S on any strike, or trade a preset.
              </div>
            ) : (
              <div className="overflow-x-auto">
              {/* Auto layout with nowrap cells and a scrolling container: fixed column widths
                  summed past the panel at a laptop width and the Entry/LTP figures printed
                  on top of each other (owner, 2026-09-09). The stepper columns keep a floor
                  so a row does not re-flow as its numbers change length. */}
              <table className="w-full text-[12px] tabular-nums whitespace-nowrap">
                <colgroup>
                  <col style={{ width: 52 }} /><col style={{ width: 40 }} />
                  <col style={{ minWidth: 150 }} /><col style={{ minWidth: 76 }} /><col style={{ minWidth: 120 }} />
                  <col /><col /><col /><col /><col />
                  <col style={{ minWidth: 185 }} />
                </colgroup>
                <thead>
                  <tr className="text-[9px] uppercase tracking-[.06em]"
                    style={{ color: "var(--oc-faint)" }}>
                    <th className="text-left font-semibold py-1">On</th>
                    <th className="text-left font-semibold">Side</th>
                    <th className="text-left font-semibold">Strike</th>
                    <th className="text-left font-semibold">Expiry</th>
                    <th className="text-left font-semibold" title="add to or trim the position">Lots (− +)</th>
                    <th className="text-right font-semibold">Entry</th>
                    <th className="text-right font-semibold">LTP</th>
                    <th className="text-right font-semibold" title="per share, position-signed · IV">Δ · IV</th>
                    <th className="text-right font-semibold">P&amp;L</th>
                    <th className="text-right font-semibold">Value</th>
                    <th className="text-right font-semibold" title="close some or all lots">Exit</th>
                  </tr>
                </thead>
                <tbody>
                  {(state?.closed ?? []).map((c, i) => (
                    /* a leg closed this cycle stays on the table — muted, with its exit and P&L —
                       instead of vanishing (owner, 2026-09-10) */
                    <tr key={`c${i}`} style={{ opacity: 0.6 }} title={`closed ${c.at.replace("T", " ")} · ${c.action}`}>
                      <td className="py-1.5 text-[9px] font-bold uppercase" style={{ color: "var(--oc-faint)" }}>closed</td>
                      <td><span className="px-1 rounded-[3px] text-[10px] font-bold"
                        style={{ color: c.side === "S" ? "var(--oc-neg)" : "var(--oc-pos)",
                          background: c.side === "S" ? "var(--oc-neg-fill)" : "var(--oc-pos-fill)" }}>{c.side}</span></td>
                      <td className="whitespace-nowrap"><b className="line-through" style={{ color: "var(--oc-muted)" }}>
                        {Math.round(c.strike).toLocaleString("en-IN")} {c.right}</b>
                        <span className="ml-1.5 text-[10px]" style={{ color: "var(--oc-faint)" }}>{c.action === "SETTLE" ? "settled" : "exited"} {c.at.slice(11)}</span></td>
                      <td className="whitespace-nowrap text-[11px]" style={{ color: "var(--oc-muted)" }}>{expiryChip(c.expiry)}</td>
                      <td className="whitespace-nowrap">×{c.lots}<span style={{ color: "var(--oc-faint)" }}> · {c.units.toLocaleString("en-IN")}</span></td>
                      <td className="text-right pl-3">{num(c.entry)}</td>
                      <td className="text-right pl-3" title="exit price">{num(c.exit)}</td>
                      <td className="text-right pl-3" style={{ color: "var(--oc-faint)" }}>—</td>
                      <td className="text-right pl-3 font-semibold" style={{ color: c.pnl >= 0 ? "var(--oc-pos)" : "var(--oc-neg)" }}>{inr0(c.pnl)}</td>
                      <td className="text-right" style={{ color: "var(--oc-faint)" }}>—</td>
                      <td className="text-right whitespace-nowrap">
                        {!isLive && (
                          <button type="button" onClick={() => unstage.mutate({ leg_id: c.symbol })}
                            title="delete this leg as if it was never traded (its P&L leaves the cycle)"
                            className="w-[22px] h-[20px] rounded-[4px] text-[12px]"
                            style={{ border: "1px solid var(--oc-line)", color: "var(--oc-muted)" }}>🗑</button>
                        )}
                      </td>
                    </tr>
                  ))}
                  {legsShown.map((l) => (
                    <LegRow key={l.id} leg={l} grid={gridStep} chainExpiry={state?.chain.expiry ?? null}
                      resetKey={state?.staged ? "staged" : "clean"}
                      onStage={(b) => stage.mutate(b)}
                      onUnstage={(legId, kind) => unstage.mutate({ leg_id: legId, kind })}
                      replay={!isLive} />
                  ))}
                </tbody>
              </table>
              </div>
            )}
          </Panel>
  );

  return (
    <div className="oc-root font-plex min-h-[calc(100vh-3.5rem)] min-w-0 max-w-full"
      style={{ background: "var(--oc-ground)", color: "var(--oc-ink)", overflowX: "clip" }}>

      {/* session bar. flex-wrap + min-w-0: a row of non-wrapping chips is the min-content
          width of the page, and inside the app's flex main that WIDENED the whole console
          past the viewport at 1440 (the rail fell off the right edge). */}
      <div className="relative min-h-10 flex flex-wrap items-center gap-x-2 gap-y-1 px-3 py-1 min-w-0"
        style={{ background: "var(--oc-surface)", borderBottom: "1px solid var(--oc-line)" }}>
        {showKeys && <KeyHelp onClose={() => setShowKeys(false)} notes={state?.notes ?? []} />}
        {/* the SOURCE: a past session from the store, or a running deployment. A paper run
            fills on its PaperBroker; a LIVE run fills through LiveBroker — the run's gate,
            never a second order path. */}
        <select value={isLive ? `run:${state?.session.run_id}` : "replay"}
          onChange={(e) => {
            const v = e.target.value;
            // uncommitted changes on a running deployment: ask before they are lost
            if (staging) { setPendingSwitch(v); return; }
            switchSource(v);
          }}
          className="h-[22px] rounded-[5px] px-1.5 text-[10px] font-bold tracking-wide"
          style={{ border: `1px solid ${isReal ? "var(--oc-neg)" : "var(--oc-accent)"}`,
            color: isReal ? "var(--oc-neg)" : "var(--oc-accent)", background: "transparent" }}>
          <option value="replay">REPLAY</option>
          {liveRuns?.recovering && (
            <option value="__recovering" disabled>… backend recovering runs — list is partial</option>
          )}
          {/* populated runs first — the first option in a list of thirty was an empty book,
              which read as "selecting a run shows no legs" (owner, 2026-09-10) */}
          {/* only runs HOLDING positions (owner, 2026-09-10) — a flat run has nothing to
              adjust here; build a fresh book on the Trade page instead */}
          {[...(liveRuns?.runs ?? [])]
            .filter((r: ConsoleLiveRun) => (r.open_positions ?? 0) > 0)
            .sort((a, b) => (b.open_positions ?? 0) - (a.open_positions ?? 0) || a.run_id - b.run_id)
            .map((r: ConsoleLiveRun) => (
            <option key={r.run_id} value={`run:${r.run_id}`}>
              {r.mode === "LIVE" && r.order_broker === "live" ? "LIVE" : "PAPER"} · #{r.run_id} {r.name} · {r.underlying}
              {" · "}{r.open_positions ? `${r.open_positions} leg${r.open_positions === 1 ? "" : "s"}` : "flat"}
            </option>
          ))}
        </select>
        <select value={underlying}
          onChange={(e) => { setUnderlying(e.target.value); opened.current = false; setState(null); }}
          className="h-[22px] rounded-[5px] px-1.5 text-[11px] font-semibold"
          style={{ background: "var(--oc-chip)", color: "var(--oc-ink)", border: "none" }}>
          {(days?.underlyings ?? ["NIFTY"]).map((u) => <option key={u} value={u}>{u}</option>)}
        </select>
        <span className="text-[10.5px]" style={{ color: "var(--oc-faint)" }}>
          lot {state?.session.lot_size ?? "—"}
        </span>
        <input type="date" value={day} min={days?.first ?? undefined} max={days?.last ?? undefined}
          onChange={(e) => { setDay(e.target.value); open.mutate({ underlying, day: e.target.value, at: "09:30", expiry: null }); }}
          className={`h-[22px] rounded-[5px] px-1.5 text-[11px] ${isLive ? "!hidden" : ""}`}
          style={{ background: "var(--oc-chip)", color: "var(--oc-ink)", border: "none" }} />
        <div className="w-px h-5" style={{ background: "var(--oc-line)" }} />

        <div className={`flex items-center gap-1 ${isLive ? "!hidden" : ""}`}>
          {JOGS.map((j) => (
            <Chip key={j.label} disabled={!state || busy}
              onClick={() => move.mutate(j.op ? { op: j.op }
                : j.days ? { op: "day", days: j.days } : { op: "step", minutes: j.minutes })}>
              {j.label}
            </Chip>
          ))}
          <span className="px-2 h-[22px] leading-[22px] rounded-[5px] text-[11px] font-bold mx-0.5"
            style={{ border: "1px solid var(--oc-accent)", color: "var(--oc-accent)" }}>
            {state?.session.clock ?? "—:—"}
          </span>
          {JOGS_FWD.map((j) => (
            <Chip key={j.label} disabled={!state || busy}
              onClick={() => move.mutate(j.op ? { op: j.op }
                : j.days ? { op: "day", days: j.days } : { op: "step", minutes: j.minutes })}>
              {j.label}
            </Chip>
          ))}
        </div>
        <div className={`w-px h-5 ${isLive ? "!hidden" : ""}`} style={{ background: "var(--oc-line)" }} />
        <div className={`flex items-center gap-1 ${isLive ? "!hidden" : ""}`}>
          <button type="button" disabled={!state}
            onClick={() => setPlaying((v) => !v)}
            title={playing ? "pause (Space)" : "play (Space)"}
            className="h-[22px] w-[26px] rounded-[5px] text-[11px] font-bold disabled:opacity-40"
            style={{ background: playing ? "var(--oc-accent)" : "var(--oc-chip)",
              color: playing ? "#fff" : "var(--oc-accent)" }}>
            {playing ? "❚❚" : "▶"}
          </button>
          {SPEEDS.map((sp, i) => (
            <Chip key={sp.label} active={i === speed} disabled={!state}
              title={`${sp.minutes} replay minute${sp.minutes > 1 ? "s" : ""} every ${sp.ms / 1000}s`}
              onClick={() => { setSpeed(i); if (!playing) setPlaying(true); }}>
              {sp.label}
            </Chip>
          ))}
        </div>

        <div className="ml-auto flex items-center gap-2 relative">
          {isLive && (
            <span className="text-[10.5px] font-semibold px-2 h-[22px] leading-[22px] rounded-[5px]"
              style={{ background: isReal ? "var(--oc-neg-fill)" : "var(--oc-accent-dim)",
                color: isReal ? "var(--oc-neg)" : "var(--oc-accent)" }}>
              {isReal ? "REAL ORDERS" : "paper fills"} · {state?.session.run_name} · live clock
              {state?.session.managed_by === "manual" && (
                <span className="ml-1.5 px-1.5 rounded-[3px] font-bold"
                  title={`Manual mode: ${state.session.handover?.strategy_id ?? state.session.strategy_id} is paused after your change — you handle adjustments and exits. A target or stop is optional (Edit params on the Live tile). Resume from the Live page once flat.`}
                  style={{ background: "var(--oc-caution)", color: "#fff" }}>
                  manual mode · {state.session.handover?.strategy_id ?? "strategy"} paused
                </span>
              )}
            </span>
          )}
          {simId != null && (
            <span className="ml-2 px-1.5 py-[1px] rounded-[4px] text-[10px] font-bold"
              title={simReadOnly ? "a banked cycle, replayed read-only" : "the Simulator: every fill autosaves to this strategy; a flat book after trading offers the cycle for banking"}
              style={{ background: simReadOnly ? "var(--oc-chip)" : "var(--oc-accent)", color: simReadOnly ? "var(--oc-muted)" : "#fff" }}>
              SIM · {sim?.name ?? `#${simId}`} · {simReadOnly ? `cycle ${simCycleNo} · read-only` : `cycle ${sim?.cycle_no ?? "?"}`}
              <Link to="/simulator" className="ml-1.5 underline font-normal" style={{ color: "inherit" }}>scoreboard</Link>
            </span>
          )}
          <Chip disabled={!state || isLive} title="bookmark this minute (B)"
            active={!!state && state.bookmarks.includes(`${state.session.date}T${state.session.clock}`)}
            onClick={() => bookmark.mutate()}>◇ mark</Chip>
          <Chip disabled={!state || isLive} active={showSaveBox} title="save this session (day, cursor, book, alerts)"
            onClick={() => { setShowSaveBox((v) => !v); setShowSaves(false);
              setSaveName(`${underlying} ${state?.session.date ?? ""} ${state?.session.clock ?? ""}`); }}>
            ⤓ save</Chip>
          <Chip active={showSaves} title="load a saved session"
            onClick={() => { setShowSaves((v) => !v); setShowSaveBox(false); }}>
            ⤒ load</Chip>
          {showSaveBox && (
            <div className="absolute right-0 top-[26px] z-30 w-[320px] rounded-[10px] p-2 shadow-lg flex items-center gap-1.5"
              style={{ background: "var(--oc-surface)", border: "1px solid var(--oc-line)" }}>
              <input autoFocus value={saveName} onChange={(e) => setSaveName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && saveName.trim()) { save.mutate(saveName.trim()); setShowSaveBox(false); }
                  if (e.key === "Escape") setShowSaveBox(false);
                }}
                placeholder="name this session"
                className="h-[24px] flex-1 min-w-0 rounded-[5px] px-2 text-[11.5px]"
                style={{ background: "var(--oc-chip)", color: "var(--oc-ink)", border: "none" }} />
              <button type="button" disabled={!saveName.trim()}
                onClick={() => { save.mutate(saveName.trim()); setShowSaveBox(false); }}
                className="h-[24px] px-2.5 rounded-[5px] text-[11px] font-semibold disabled:opacity-40"
                style={{ background: "var(--oc-accent)", color: "#fff" }}>Save</button>
            </div>
          )}
          {showSaves && (
            <div className="absolute right-0 top-[26px] z-30 w-[380px] rounded-[10px] p-2 shadow-lg"
              style={{ background: "var(--oc-surface)", border: "1px solid var(--oc-line)" }}>
              <div className="text-[9.5px] font-semibold uppercase tracking-[.07em] px-1 pb-1"
                style={{ color: "var(--oc-faint)" }}>Saved sessions</div>
              {!savedList?.saved.length ? (
                <div className="px-1 py-2 text-[11.5px]" style={{ color: "var(--oc-faint)" }}>
                  Nothing saved yet. ⤓ save keeps the day, the cursor, the book and the alerts.
                </div>
              ) : savedList.saved.map((r) => (
                <div key={r.file} className="flex items-center gap-2 px-1 py-1 text-[11.5px] rounded-[6px] hover:bg-[var(--oc-panel2)]">
                  <button type="button" className="flex-1 text-left" onClick={() => load.mutate(r.file)}>
                    <b>{r.name}</b>
                    <span className="ml-2" style={{ color: "var(--oc-muted)" }}>
                      {r.underlying} · {r.day} {r.clock} · {r.legs} leg{r.legs === 1 ? "" : "s"}
                    </span>
                    <span className="ml-2 text-[10px]" style={{ color: "var(--oc-faint)" }}>
                      {r.saved_at?.slice(0, 16).replace("T", " ")}
                    </span>
                  </button>
                  <button type="button" title="delete" onClick={() => deleteSaved.mutate(r.file)}
                    style={{ color: "var(--oc-faint)" }}>×</button>
                </div>
              ))}
            </div>
          )}
          <button type="button" onClick={() => setShowKeys((v) => !v)}
            title="Keyboard shortcuts (?)"
            className="w-[22px] h-[22px] rounded-[5px] text-[11px] font-bold"
            style={{ background: showKeys ? "var(--oc-accent-dim)" : "var(--oc-chip)",
              color: showKeys ? "var(--oc-accent)" : "var(--oc-muted)" }}>?</button>
          <span className="text-[10px] font-bold px-2 h-[22px] leading-[22px] rounded-[4px]"
            style={{ background: "var(--oc-chip)", color: "var(--oc-muted)" }}>
            {playing ? "PLAYING" : busy ? "…"
              : isLive ? state?.session.status
              : state && !state.session.has_next_day && state.session.played_pct >= 100 ? "END OF DATA"
              : state?.session.status ?? "READY"}
          </span>
        </div>
      </div>

      {/* replay track: the scrubber with the day's EVENTS on it — fills (S red / B green),
          fired alerts (amber), bookmarks (◇) — click anywhere to seek, and jump chips that
          move the cursor to the next such event. Where the design's D6 lives. */}
      <div className="min-h-6 flex items-center gap-2 px-3 min-w-0"
        style={{ background: "var(--oc-surface)", borderBottom: "1px solid var(--oc-hair)" }}>
        <span className="text-[9px] w-[30px]" style={{ color: "var(--oc-faint)" }}>
          {state?.session.range[0] ?? "09:15"}
        </span>
        <Track state={state} onSeek={(at) => move.mutate({ op: "seek", at })} />
        <span className="text-[9px] w-[30px] text-right" style={{ color: "var(--oc-faint)" }}>
          {state?.session.range[1] ?? "15:40"}
        </span>
        {!isLive && !!state?.track.mtm?.length && (
          <>
            <div className="w-px h-3.5" style={{ background: "var(--oc-line)" }} />
            <Sparkline points={state.track.mtm} />
          </>
        )}
        {/* the CYCLE bar: first fill → last expiry, in sessions. The day bar above says
            where in the session you are; this says where in the trade (owner, 2026-09-09). */}
        {state?.cycle?.sessions != null && (
          <>
            <div className="w-px h-3.5" style={{ background: "var(--oc-line)" }} />
            <div className="flex items-center gap-1.5 shrink-0"
              title={`cycle: ${state.cycle.start!} → expiry ${state.cycle.end!} · NSE session ${state.cycle.session_no} of ${state.cycle.sessions}${state.cycle.beyond_data ? ` · the store's last captured day is ${state.cycle.data_until!}; later sessions appear once captured (~16:00 IST daily)` : ""}`}>
              <span className="text-[9px] whitespace-nowrap" style={{ color: "var(--oc-faint)" }}>
                {expiryChip(state.cycle.start!)}
              </span>
              <div className="relative w-[140px] h-1 rounded" style={{ background: "var(--oc-chip)" }}>
                <div className="absolute left-0 top-0 h-1 rounded"
                  style={{ width: `${state.cycle.pct}%`,
                    background: state.cycle.done ? "var(--oc-muted)" : "var(--oc-caution)" }} />
              </div>
              <span className="text-[9px] whitespace-nowrap font-semibold"
                style={{ color: state.cycle.done ? "var(--oc-muted)" : "var(--oc-caution)" }}>
                {state.cycle.done ? "expired" : `exp ${expiryChip(state.cycle.end!)}`}
                <span className="font-normal" style={{ color: "var(--oc-faint)" }}>
                  {" "}· session {state.cycle.session_no}/{state.cycle.sessions}
                  {state.cycle.beyond_data ? ` · data to ${expiryChip(state.cycle.data_until!)}` : ""}
                </span>
              </span>
            </div>
          </>
        )}
        <div className="w-px h-3.5" style={{ background: "var(--oc-line)" }} />
        <div className={`flex items-center gap-1 shrink-0 ${isLive ? "!hidden" : ""}`} title="jump to the next / previous event">
          <TrackChip onClick={() => jump.mutate({ kind: "prev_fill" })} disabled={!state} title="previous fill (K)">‹ fill</TrackChip>
          <TrackChip onClick={() => jump.mutate({ kind: "next_fill" })} disabled={!state} title="next fill (J)">fill ›</TrackChip>
          <TrackChip onClick={() => jump.mutate({ kind: "next_move", pct: 1 })} disabled={!state} title="next 1% move in spot">1% ›</TrackChip>
          <TrackChip onClick={() => jump.mutate({ kind: "next_iv_spike", pct: 1 })} disabled={!state} title="next minute the ATM implied vol is 1 vol point above now">iv ›</TrackChip>
          <TrackChip onClick={() => jump.mutate({ kind: "next_alert" })} disabled={!state || !state.alerts.some((a) => a.state === "armed")}
            title="run forward to the minute an armed alert would fire">alert ›</TrackChip>
          <TrackChip onClick={() => jump.mutate({ kind: "prev_bookmark" })} disabled={!state} title="previous bookmark">‹ ◇</TrackChip>
          <TrackChip onClick={() => jump.mutate({ kind: "next_bookmark" })} disabled={!state} title="next bookmark">◇ ›</TrackChip>
        </div>
      </div>

      {/* market strip */}
      <div className="h-[30px] flex items-center overflow-x-auto"
        style={{ background: "var(--oc-panel2)", borderBottom: "1px solid var(--oc-line)" }}>
        {/* THE DAY, first and loud: a replay's whole meaning is "which session is this", and
            a date input in the toolbar was not saying it (owner, 2026-09-09). */}
        <div className="px-3 h-full flex items-center gap-2 shrink-0"
          style={{ borderRight: "1px solid var(--oc-line)", background: "var(--oc-accent-tint)" }}>
          <span className="text-[13px] font-bold tabular-nums whitespace-nowrap"
            style={{ color: "var(--oc-accent)" }}>
            {state ? prettyDay(state.session.date) : "—"}
          </span>
          <span className="text-[13px] font-semibold tabular-nums" style={{ color: "var(--oc-ink)" }}>
            {state?.session.clock ?? "—:—"}
          </span>
          {state && (
            <span className="text-[10px] font-semibold px-1.5 rounded-[3px]"
              style={{ background: "var(--oc-chip)", color: "var(--oc-muted)" }}>
              {state.session.played_pct >= 100 ? "CLOSED" : state.session.clock < "09:20" ? "OPENING" : "IN SESSION"}
            </span>
          )}
        </div>
        <StripItem label={isLive ? underlying : `${underlying} (parity)`}>
          {num(state?.market.spot ?? null)}
          {state?.cycle?.entry_spot && state.market.spot ? (
            <span className="ml-1.5 text-[10.5px] font-normal" style={{ color: "var(--oc-muted)" }}
              title={`the underlying when this cycle opened (${state.cycle.entry_at?.replace("T", " ") ?? ""})`}>
              entered {num(state.cycle.entry_spot, 0)}
              <span style={{ color: state.market.spot >= state.cycle.entry_spot ? "var(--oc-pos)" : "var(--oc-neg)" }}>
                {" "}{signed(100 * (state.market.spot / state.cycle.entry_spot - 1))}%
              </span>
            </span>
          ) : null}
        </StripItem>
        <StripItem label={`FUT ${state?.market.expiry ?? ""}`}>
          {num(state?.market.fut ?? null)}
          <span className="ml-1.5 text-[10.5px] font-normal" style={{ color: "var(--oc-muted)" }}>
            carry {signed(state?.market.carry ?? null)}
          </span>
        </StripItem>
        <StripItem label="Day (so far)">
          {num(state?.market.day_low ?? null, 0)}–{num(state?.market.day_high ?? null, 0)}
        </StripItem>
        {state?.market.cycle_low != null && state.market.cycle_high != null && (
          <StripItem label="Cycle (so far)">
            <span title={`the underlying's low–high since this cycle opened${state.cycle?.entry_at ? ` (${state.cycle.entry_at.replace("T", " ")})` : ""}`}>
              {num(state.market.cycle_low, 0)}–{num(state.market.cycle_high, 0)}
            </span>
          </StripItem>
        )}
        {state?.market.vix && (state.market.vix.last != null || state.market.vix.prev_close != null) && (
          <StripItem label={isLive ? "India VIX" : state.market.vix.last != null ? "India VIX" : "VIX · prev close"}>
            <span title={isLive ? "the live print" : state.market.vix.last != null
              ? `India VIX at the cursor's minute (self-captured minute bars); prev close ${num(state.market.vix.prev_close ?? null, 2)}`
              : `India VIX at the prior session's close${state.market.vix.prev_date ? ` (${state.market.vix.prev_date})` : ""} — no minute bars captured for this day; a replayed day's own close would be the future`}>
              {num(state.market.vix.last ?? (isLive ? null : state.market.vix.prev_close ?? null), 2)}
              {!isLive && state.market.vix.last != null && state.market.vix.prev_close != null && (
                <span className="ml-1.5 text-[10.5px] font-normal" style={{ color: "var(--oc-muted)" }}>
                  prev {num(state.market.vix.prev_close, 2)}
                </span>
              )}
              {!isLive && state.market.vix.rank_1y != null && (
                <span className="ml-1.5 text-[10.5px] font-normal" style={{ color: "var(--oc-muted)" }}
                  title="where the prior close sits among the last year of VIX closes (0% = the year's low, 100% = its high) — a VIX rank, not an IV rank">
                  rank {Math.round(state.market.vix.rank_1y)}%
                </span>
              )}
              {!isLive && state.market.vix.open != null && state.market.vix.prev_close != null
                && Math.abs(state.market.vix.open - state.market.vix.prev_close) > 0.005 && (
                <span className="ml-1.5 text-[10.5px] font-normal" style={{ color: "var(--oc-muted)" }}>
                  open {num(state.market.vix.open, 2)}
                </span>
              )}
            </span>
          </StripItem>
        )}
        {state?.market.atm_iv != null && (
          <StripItem label="ATM IV">
            <span title="the front expiry's at-the-money implied vol at this minute, solved from the last print the same way the ladder does — our own volatility gauge, minute by minute">
              {num(state.market.atm_iv, 1)}%
            </span>
          </StripItem>
        )}
        {state?.market.iv30 && (
          <StripItem label="IV30">
            <span title={`ATM implied vol on the ~30-day expiry (${state.market.iv30.expiry}, ${state.market.iv30.dte} DTE) — the measure the IV rank is built on`}>
              {num(state.market.iv30.iv, 1)}%
            </span>
            {state.market.iv_rank ? (
              <span className="ml-1.5 text-[10.5px] font-normal" style={{ color: "var(--oc-muted)" }}
                title={`over the last ${state.market.iv_rank.n} sessions before this day: IVR = (now − low ${num(state.market.iv_rank.low, 1)}) / (high ${num(state.market.iv_rank.high, 1)} − low); rank = the share of those days with a lower IV30`}>
                IVR {state.market.iv_rank.ivr != null ? Math.round(state.market.iv_rank.ivr) : "—"} · rank {Math.round(state.market.iv_rank.rank)}%
              </span>
            ) : (
              <span className="ml-1.5 text-[10.5px] font-normal" style={{ color: "var(--oc-faint)" }} title="fewer than 60 sessions of IV history before this day">no rank yet</span>
            )}
          </StripItem>
        )}
        <StripItem label="Expiry">
          {state?.market.expiry ?? "—"}
          <span className="ml-1.5 text-[10.5px] font-normal" style={{ color: "var(--oc-muted)" }}>
            {state?.market.dte ?? "—"} DTE
          </span>
        </StripItem>
        <StripItem label="Quoted">
          {state ? `${state.chain.quoted}/${state.chain.total}` : "—"}
        </StripItem>
        <StripItem label="Capital">{num(state?.session.capital ?? null, 0)}</StripItem>
      </div>

      {simId != null && !simReadOnly && state && (simTraded && simFlat || bankMsg) && (
        /* the Simulator's bank sheet: prompts the moment the book is flat after trading */
        <div className="flex flex-wrap items-center gap-2 px-3 py-2 text-[12px]"
          style={{ background: "var(--oc-caution-dim)", borderBottom: "1px solid var(--oc-hair)" }}>
          {simTraded && simFlat ? (
            <>
              <b>Cycle {sim?.cycle_no ?? "?"} complete</b>
              <span style={{ color: "var(--oc-muted)" }}>
                net {inr0((state.risk?.realised_total ?? 0) - (state.risk?.charges ?? 0))}
                {" · "}charges {inr0(state.risk?.charges ?? 0)}
                {cycleMargin.current.value ? ` · margin ${inr0(cycleMargin.current.value)} (${cycleMargin.current.source})` : ""}
              </span>
              <input value={bankNote} onChange={(e) => setBankNote(e.target.value)} placeholder="note — what you did and why"
                className="flex-1 min-w-[220px] h-[24px] rounded-[5px] px-2 text-[11.5px]"
                style={{ background: "var(--oc-panel)", border: "1px solid var(--oc-line)", color: "var(--oc-ink)" }}
                onKeyDown={(e) => { if (e.key === "Enter") bankCycle(); }} />
              <button type="button" disabled={bankBusy} onClick={bankCycle}
                className="px-3 h-[24px] rounded-[5px] text-[11.5px] font-semibold disabled:opacity-40"
                style={{ background: "var(--oc-accent)", color: "#fff" }}>{bankBusy ? "Banking…" : "Bank cycle"}</button>
              <span className="text-[10.5px]" style={{ color: "var(--oc-faint)" }}>banking writes this cycle to the strategy and opens the next day</span>
            </>
          ) : (
            <span style={{ color: "var(--oc-muted)" }}>{bankMsg}</span>
          )}
        </div>
      )}
      {error && (
        <div className="px-3 py-2 text-[12px]"
          style={{ background: "var(--oc-neg-fill)", color: "var(--oc-neg)" }}>{error}</div>
      )}
      {ticketOpen && state?.staged?.ticket && (
        <OrderTicket ticket={state.staged.ticket} staged={state.staged} risk={state.risk}
          riskAfter={state.staged.risk_after ?? null} mNow={mStaged} mBefore={mBeforeStaged}
          isReal={isReal} realTyped={realTyped} setRealTyped={setRealTyped}
          capital={state.session.capital} orderError={state.session.order_error ?? null}
          busy={commit.isPending}
          onSend={(limits) => { commit.mutate(limits); setRealTyped(""); setTicketOpen(false); }}
          onClose={() => setTicketOpen(false)}
          onRevert={() => { discard.mutate(); setTicketOpen(false); }} />
      )}
      {pendingSwitch && (
        <div className="px-3 py-2 text-[12px] flex items-center gap-3 flex-wrap"
          style={{ background: "var(--oc-caution-dim)", color: "var(--oc-caution)" }}>
          <b>{state?.staged?.items.length ?? 0} uncommitted change{(state?.staged?.items.length ?? 0) === 1 ? "" : "s"}</b>
          on {state?.session.run_name ?? "this run"} will be lost if you leave. Commit or Revert them first, or leave anyway.
          <span className="ml-auto flex gap-2">
            <button type="button" onClick={() => setPendingSwitch(null)}
              className="px-2 h-[22px] rounded-[5px] text-[11px] font-semibold"
              style={{ background: "var(--oc-accent)", color: "#fff" }}>Stay</button>
            <button type="button" onClick={() => { discard.mutate(); switchSource(pendingSwitch); }}
              className="px-2 h-[22px] rounded-[5px] text-[11px] font-semibold"
              style={{ background: "var(--oc-neg)", color: "#fff" }}>Discard and leave</button>
          </span>
        </div>
      )}
      {isLive && state?.session.order_error && (
        <div className="px-3 py-2 text-[12px] font-semibold"
          style={{ background: "var(--oc-neg-fill)", color: "var(--oc-neg)" }}>
          Run halted on an order error — acknowledge it on the Live page before applying anything:
          {" "}{state.session.order_error}
        </div>
      )}
      {notice && (
        <div className="px-3 py-1.5 text-[12px] flex items-center gap-3"
          style={{ background: "var(--oc-caution-dim)", color: "var(--oc-caution)" }}>
          {notice}
          <button type="button" className="ml-auto underline" onClick={() => setNotice(null)}>
            dismiss</button>
        </div>
      )}

      {/* body: chain 560 · analysis flex · rail 348 */}
      <div className="flex gap-2.5 p-2.5 items-start">
        {/* ⟨⟨ Hide collapses the chain to a 36px rail; the analysis column absorbs the
            width and the payoff re-renders at whatever the column now gives it. */}
        {!chainOpen && (
          <button type="button" onClick={() => setChainOpen(true)} title="show the option chain (H)"
            className="w-9 shrink-0 self-stretch rounded-[10px] flex flex-col items-center py-2 gap-3"
            style={{ background: "var(--oc-surface)", border: "1px solid var(--oc-line)",
              minHeight: 420 }}>
            <span className="text-[12px] font-bold" style={{ color: "var(--oc-accent)" }}>⟩⟩</span>
            <span className="text-[9.5px] font-semibold uppercase tracking-[.1em]"
              style={{ writingMode: "vertical-rl", color: "var(--oc-faint)" }}>
              Option chain · {state?.chain.expiry ? expiryChip(state.chain.expiry) : ""}
            </span>
            <span className="text-[9.5px]"
              style={{ writingMode: "vertical-rl", color: "var(--oc-muted)" }}>
              ATM {state?.chain.atm_strike?.toLocaleString("en-IN") ?? "—"} · lot {state?.session.lot_size ?? "—"}
            </span>
          </button>
        )}
        <div className={`${narrow ? "w-[496px]" : "w-[560px]"} shrink-0 rounded-[10px] overflow-hidden`}
          hidden={!chainOpen}
          style={{ background: "var(--oc-surface)", border: "1px solid var(--oc-line)" }}>
          {/* expiry chips */}
          <div className="h-9 flex items-center gap-1 px-2 overflow-x-auto"
            style={{ borderBottom: "1px solid var(--oc-hair)" }}>
            <button type="button" onClick={() => setChainOpen(false)} title="hide the chain (H)"
              className="text-[10px] font-bold pr-1 shrink-0" style={{ color: "var(--oc-accent)" }}>
              ⟨⟨</button>
            <span className="text-[9.5px] font-semibold uppercase tracking-[.07em] pr-1"
              style={{ color: "var(--oc-faint)" }}>Expiry</span>
            {!isLive && (
              /* the §8 escape hatch: NIFTY lists 50-point strikes, the automated strategies never
                 select one, so the ladder is coarsened to 100s by default; this shows the 50s */
              <Chip active={!!state?.chain.listing_grid} disabled={!state}
                title={state?.chain.listing_grid ? "showing every listed strike — click for 100s only" : "show the 50-point strikes too"}
                onClick={() => setFifty.mutate(!state?.chain.listing_grid)}>50s</Chip>
            )}
            {/* the size a B/S click stages — kept beside the ladder because it is part of
                the click, not a property of the position */}
            <span className="flex items-center gap-1 mr-1.5 shrink-0"
              title="lots staged by a B/S click">
              <button type="button" onClick={() => setLots((n) => Math.max(1, n - 1))}
                className="w-[16px] h-[16px] rounded-[3px] text-[11px] leading-[15px]"
                style={{ background: "var(--oc-chip)", color: "var(--oc-muted)" }}>−</button>
              <b className="text-[11px] tabular-nums" style={{ minWidth: 22, textAlign: "center" }}>
                ×{lots}</b>
              <button type="button" onClick={() => setLots((n) => Math.min(99, n + 1))}
                className="w-[16px] h-[16px] rounded-[3px] text-[11px] leading-[15px]"
                style={{ background: "var(--oc-chip)", color: "var(--oc-muted)" }}>+</button>
            </span>
            {(state?.chain.expiries ?? []).slice(0, 8).map((e) => (
              <Chip key={e.iso} active={e.iso === state?.chain.expiry}
                onClick={() => pickExpiry.mutate(e.iso)}
                title={`${e.iso} · ${e.dte} days`}>
                {expiryChip(e.iso)}<span className="opacity-60"> {e.dte}d</span>
              </Chip>
            ))}
          </div>
          {/* column header */}
          <div className="grid text-[9px] font-semibold uppercase tracking-[.06em] h-5 items-center"
            style={{ gridTemplateColumns: narrow ? COLS_NARROW : COLS, color: "var(--oc-faint)",
              borderBottom: "1px solid var(--oc-hair)" }}>
            <div className="text-right px-2">Δ</div><div className="text-right px-2">Call LTP</div>
            <div className="text-right px-1.5">B/S</div>
            <div className="text-center">Strike</div><div className="text-center">IV</div>
            <div className="px-1.5">B/S</div>
            <div className="text-right px-2">Put LTP</div><div className="text-right px-2">Δ</div>
          </div>
          <div className="max-h-[calc(100vh-14rem)] overflow-y-auto">
            {rows.length === 0 && (
              <div className="p-6 text-center text-[12px]" style={{ color: "var(--oc-muted)" }}>
                {busy ? "Loading the chain…" : "No chain for this minute yet."}
              </div>
            )}
            {rows.map((r) => (
              <ChainRow key={r.strike} row={r} probed={probed} onProbe={probe} cols={narrow ? COLS_NARROW : COLS}
                onPick={(side, right, strike) =>
                  stage.mutate({ kind: "add", side, right, strike, lots })} />
            ))}
          </div>
        </div>

        {/* Positions has the FULL width in both layouts and nothing is ever inserted above
            it: a staged bar that appeared between the chart and the table pushed the table
            down on every click (owner, 2026-09-10). The staged summary lives in the Positions
            header and the ticket. */}
        {chainOpen ? (
          /* chain · [payoff | rail] over a full-width Positions. The row's height is the
             PAYOFF's alone: the rail is absolutely positioned inside a stretched box and
             scrolls if it must, so nothing the rail says can move Positions (owner, 2026-09-10). */
          <div className="flex-1 min-w-0 space-y-2.5">
            <div className="flex gap-2.5 items-stretch">
              <div className="flex-1 min-w-0 flex flex-col">
                {payoffPanel}
              </div>
              <div className={`${narrow ? "w-[300px]" : "w-[348px]"} shrink-0 relative`}>
                <div className="absolute inset-0 overflow-y-auto space-y-2.5">
                  {railColumn}
                </div>
              </div>
            </div>
            {positionsPanel}
          </div>
        ) : (
          /* chain hidden: [risk strip + Positions + alerts | payoff] */
          <div className="flex-1 min-w-0 flex gap-2.5 items-start">
            <div className={`w-[54%] ${narrow ? "min-w-[440px]" : "min-w-[520px]"} space-y-2.5`}>
              {riskStrip}
              {positionsPanel}
              {alertsCard}
            </div>
            <div className="flex-1 min-w-0 flex flex-col">
              {payoffPanel}
            </div>
          </div>
        )}
      </div>

      {/* footer — the design's P&L strip; for now it carries the keyboard ladder, because a
          transport nobody can find is a transport nobody uses. */}
      <div className="min-h-[28px] py-1 flex items-center gap-x-4 gap-y-0.5 flex-wrap px-3 text-[10.5px]"
        style={{ background: "var(--oc-panel2)", borderTop: "1px solid var(--oc-line)",
          color: "var(--oc-faint)" }}>
        <span style={{ color: "var(--oc-accent)" }}>
          {isReal ? "changes are shown as done but STAGED — Commit to broker places REAL orders"
            : state?.session.requires_confirm ? "changes are shown as done but STAGED — Commit fills on the paper broker · Revert drops them"
            : "clicks trade at once · U undoes"}
        </span>
        <span title="this cycle's realised">REALISED <b style={{ color: "var(--oc-ink)" }}>{inr0(risk?.realised ?? 0)}</b></span>
        <span>UNREALISED <b style={{ color: "var(--oc-ink)" }}>{inr0(risk?.unrealised ?? 0)}</b></span>
        <span>TOTAL <b style={{ color: "var(--oc-ink)" }}>{inr0(risk?.mtm ?? 0)}</b>
          <span> ({pctOf(risk?.mtm, risk?.margin)} of margin)</span></span>
        <button type="button" onClick={() => setShowKeys(true)}
          className="ml-auto hover:underline" title="Keyboard shortcuts (?)">
          {KEYS.slice(0, 4).map((k) => (
            <span key={k.keys}>
              <b style={{ color: "var(--oc-muted)" }}>{k.keys}</b>{" "}
              {k.does.replace("back / forward ", "").replace(
                "previous / next trading day (keeps the time)", "day")} ·{" "}
            </span>
          ))}
          <b style={{ color: "var(--oc-accent)" }}>?</b> all keys
        </button>
      </div>
    </div>
  );
}
