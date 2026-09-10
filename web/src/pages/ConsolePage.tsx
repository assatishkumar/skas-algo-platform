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
import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type {
  ConsoleAlert, ConsoleChainLeg, ConsoleChainRow, ConsoleLeg, ConsoleLiveRun, ConsolePreset, ConsoleProbe,
  ConsoleState,
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
  { keys: ", .", does: "back / forward 1 minute" },
  { keys: "Shift + , .", does: "15 minutes" },
  { keys: "Alt + , .", does: "1 hour" },
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

function Tile({ label, value, sub, tone }: {
  label: string; value: string; sub?: string; tone?: "pos" | "neg";
}) {
  return (
    <div className="rounded-[8px] px-2 py-1.5" style={{ background: "var(--oc-panel2)" }}>
      <div className="text-[9px] font-semibold uppercase tracking-[.06em]"
        style={{ color: "var(--oc-faint)" }}>{label}</div>
      <div className="text-[13px] font-semibold tabular-nums"
        style={{ color: tone === "pos" ? "var(--oc-pos)"
          : tone === "neg" ? "var(--oc-neg)" : "var(--oc-ink)" }}>{value}</div>
      {sub && <div className="text-[10px]" style={{ color: "var(--oc-faint)" }}>{sub}</div>}
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
function AlertsCard({ alerts, disabled, onArm, onClear }: {
  alerts: ConsoleAlert[]; disabled: boolean;
  onArm: (b: { kind: ConsoleAlert["kind"]; value: number }) => void;
  onClear: (id: string) => void;
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
          style={{ color: "var(--oc-faint)" }}>Alerts · fire once, pause the replay</span>
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
        <div className="mt-1.5 text-[11px]" style={{ color: "var(--oc-faint)" }}>
          None armed. A target or stop is rupees of total MTM; |Δ| is net delta in units.
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
function LegRow({ leg, grid, onStage, chainExpiry }: {
  leg: ConsoleLeg; grid: number; chainExpiry: string | null;
  onStage: (b: Parameters<typeof api.consoleStage>[1]) => void;
}) {
  const [exitLots, setExitLots] = useState(leg.lots);
  useEffect(() => { setExitLots((n) => Math.min(Math.max(1, n), leg.lots)); }, [leg.lots]);
  const value = leg.ltp == null ? null : leg.ltp * leg.units;
  return (
    <tr style={{ opacity: leg.enabled ? 1 : 0.45 }}>
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
      </td>
      {/* Strike and size are steppers in their OWN fixed columns and ALWAYS visible.
          Revealing them on hover re-flowed the row as the pointer arrived, so the button
          moved out from under the click; and inside a narrow column the cell wrapped onto
          three lines (owner, 2026-09-09). */}
      <td>
        <Stepper title={`roll a strike (${grid} pts)`}
          onDown={() => onStage({ kind: "roll", leg_id: leg.id, strike: leg.strike - grid })}
          onUp={() => onStage({ kind: "roll", leg_id: leg.id, strike: leg.strike + grid })}>
          <b className="whitespace-nowrap">
            {Math.round(leg.strike).toLocaleString("en-IN")} {leg.right}</b>
        </Stepper>
        {leg.pending && (
          <span className="ml-1.5 px-1 py-[1px] rounded-[3px] text-[8.5px] font-bold align-middle"
            title="staged on the deployment — not yet committed"
            style={{ background: "var(--oc-caution-dim)", color: "var(--oc-caution)" }}>
            {leg.pending.toUpperCase()} · PENDING</span>
        )}
      </td>
      {/* the leg's OWN expiry, with its DTE — and flagged when it is not the ladder's,
          because a 1-DTE leg under an 11 Aug chip settled overnight and read as "my
          position vanished" (owner, 2026-09-09) */}
      <td className="whitespace-nowrap text-[11px]"
        title={leg.expiry !== chainExpiry ? "not the expiry the chain is showing" : undefined}
        style={{ color: leg.expiry !== chainExpiry ? "var(--oc-caution)" : "var(--oc-muted)" }}>
        {expiryChip(leg.expiry)}
        <span className="text-[10px]" style={{ color: "var(--oc-faint)" }}>
          {leg.dte == null ? "" : ` ${leg.dte}d`}</span>
        {leg.expiry !== chainExpiry ? " ⚠" : ""}
      </td>
      <td>
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
      {/* HOW MANY of the held lots to exit — not the position size (that is the Lots
          column). With one lot held there is nothing to choose, and a − 1 + that could not
          move read as a broken size stepper (owner, 2026-09-09): the selector only appears
          for a multi-lot leg, and its buttons grey out at the ends. */}
      <td className="text-right whitespace-nowrap">
        {leg.lots > 1 && (
          <span className="inline-flex items-center gap-[3px] mr-1.5"
            title={`how many of the ${leg.lots} lots to exit`}>
            <MiniBtn disabled={exitLots <= 1}
              onClick={() => setExitLots((n) => Math.max(1, n - 1))}>−</MiniBtn>
            <span className="text-[10.5px] tabular-nums"
              style={{ minWidth: 16, display: "inline-block", textAlign: "center" }}>
              {exitLots}</span>
            <MiniBtn disabled={exitLots >= leg.lots}
              onClick={() => setExitLots((n) => Math.min(leg.lots, n + 1))}>+</MiniBtn>
          </span>
        )}
        <button type="button"
          onClick={() => onStage({ kind: "exit", leg_id: leg.id, lots: exitLots })}
          title={leg.lots > 1 ? `close ${exitLots} of ${leg.lots} lots at the cursor's price`
            : "close this leg at the cursor's price"}
          className="px-2 h-[20px] rounded-[4px] text-[10.5px]"
          style={{ border: "1px solid var(--oc-line)", color: "var(--oc-accent)" }}>
          Exit {exitLots === leg.lots ? "all" : `${exitLots} of ${leg.lots}`}</button>
      </td>
    </tr>
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

/** before → after for one risk number, the staged bar's whole job.
 *
 *  "unlimited" is a VALUE here, not a missing one. An em dash where the answer is "this
 *  tail is open" would read as "no data", and the difference between those two on a naked
 *  short is the entire point of adding the wing. */
function Delta({ label, a, b, unlimitedA, unlimitedB, good }: {
  label: string; a?: number; b?: number;
  unlimitedA?: boolean; unlimitedB?: boolean; good?: boolean;
}) {
  const col = good ? "var(--oc-pos)" : "var(--oc-neg)";
  const show = (v?: number, un?: boolean) =>
    un ? "unlimited" : v == null ? "—" : inr0(v);
  return (
    <span>
      <span style={{ color: "var(--oc-faint)" }}>{label}</span>{" "}
      <span style={{ color: col }}>{show(a, unlimitedA)}</span>
      {(b != null || unlimitedB) && (
        <> → <b style={{ color: col }}>{show(b, unlimitedB)}</b></>
      )}
    </span>
  );
}

function StripItem({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline gap-1.5 px-3 h-full"
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

function ChainRow({ row, onPick, onProbe, probed }: {
  row: ConsoleChainRow;
  onPick: (side: "B" | "S", right: "CE" | "PE", strike: number) => void;
  onProbe: (right: "CE" | "PE", strike: number) => void;
  probed: Record<string, ConsoleProbe>;
}) {
  const dead = !row.ce.quoted && !row.pe.quoted;
  return (
    <div className="grid items-center text-[12px] group"
      style={{
        gridTemplateColumns: COLS, height: 32,
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
        <span className="px-1 rounded-[3px] text-[9px] font-bold leading-[15px]"
          title={`you hold ${held.lots} lot(s) here`}
          style={{ opacity: held.enabled ? 1 : 0.45,
            color: held.side === "S" ? "var(--oc-neg)" : "var(--oc-pos)",
            border: `1px solid ${held.side === "S" ? "var(--oc-neg)" : "var(--oc-pos)"}` }}>
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
  const [showPresets, setShowPresets] = useState(false);
  const [showSaves, setShowSaves] = useState(false);
  const [showSaveBox, setShowSaveBox] = useState(false);
  const [saveName, setSaveName] = useState("");
  const [realTyped, setRealTyped] = useState("");
  const [pendingSwitch, setPendingSwitch] = useState<string | null>(null);
  const switchSource = (v: string) => {
    setPendingSwitch(null);
    if (v === "replay") {
      opened.current = false; setState(null); setParams({}, { replace: true }); open.mutate({ underlying });
    } else {
      openLive.mutate(Number(v.slice(4)));
    }
  };
  // The console over a RUNNING deployment: same DTO, no transport, every click staged and
  // applied through the run's own manual-order path. `isLive` = "not a replay".
  const isLive = !!state && state.session.mode !== "replay";
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
        restore: { journal: cur.journal ?? [], alerts: cur.alerts ?? [] },
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
    mutationFn: (body: { underlying: string; day?: string | null; at?: string }) =>
      api.consoleOpen({ ...body, at: body.at ?? params.get("at") ?? "09:20",
        expiry: params.get("expiry") ?? undefined }),
    onSuccess: (s) => {
      setState(s); setDay(s.session.date); setError(null);
      setParams({ u: s.session.underlying, day: s.session.date, at: s.session.clock,
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
      setParams({ u: s.session.underlying, day: s.session.date, at: s.session.clock,
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
      setParams({ u: s.session.underlying, day: s.session.date, at: s.session.clock,
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
      setParams({ u: s.session.underlying, day: s.session.date, at: s.session.clock,
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
    mutationFn: () => call((id) => api.consoleCommit(id)),
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
  const discard = useMutation({
    mutationFn: () => call((id) => api.consoleDiscard(id)),
    onSuccess: setState,
  });

  const pickExpiry = useMutation({
    mutationFn: (expiry: string) => call((id) => api.consoleChain(id, { expiry })),
    onSuccess: (s) => {
      setState(s);
      setParams({ u: s.session.underlying, day: s.session.date, at: s.session.clock,
        ...(s.chain.expiry ? { expiry: s.chain.expiry } : {}) }, { replace: true });
    },
  });

  // Open a session once the store's day list is known — the newest captured day.
  useEffect(() => {
    if (!days || opened.current) return;
    opened.current = true;
    const liveId = params.get("live");
    if (liveId) { openLive.mutate(Number(liveId)); return; }
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
        if (state?.staged) discard.mutate();
        return;
      }
      if (e.key === "Enter" && state?.staged && state.session.mode !== "live") {
        e.preventDefault(); commit.mutate(); return;
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
        const step = e.altKey ? 60 : e.shiftKey ? 15 : 1;
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
                        realised={risk?.realised ?? 0} />
                    )}
                  </div>
                  {/* NET GREEKS: Σ per-share greek × units over the enabled legs, the same
                      convention as the Live tile. Beside them, the T+0 book at ±1% of spot —
                      the question the greeks approximate, answered directly. */}
                  <div className="mt-2 pt-2 flex flex-wrap gap-1.5 [&>*]:flex-1 [&>*]:min-w-[92px]"
                    style={{ borderTop: "1px solid var(--oc-hair)" }}>
                    <GreekCell label="Δ net" v={risk?.greeks.delta} dp={1} unit="units" />
                    <GreekCell label="Γ net" v={risk?.greeks.gamma} dp={3} />
                    <GreekCell label="Θ /day" v={risk?.greeks.theta} dp={0} inr />
                    <GreekCell label="Vega /1%" v={risk?.greeks.vega} dp={0} inr />
                    <ScenarioCell label={`${MINUS}1%`} v={scen?.[0]} base={scen?.[1]} />
                    <ScenarioCell label="spot" v={scen?.[1]} base={scen?.[1]} />
                    <ScenarioCell label="+1%" v={scen?.[2]} base={scen?.[1]} />
                  </div>
                </Panel>
  );

  const stagedBar = (
    <>
                {/* Only a book that can reach a broker gets an Apply between the click and the
                    trade. In replay this block never renders — the click IS the trade, and Undo
                    is the way back. */}
                {state?.staged && state.session.requires_confirm && (
                  <div className="rounded-[10px] p-3"
                    style={{ border: "1px dashed var(--oc-accent)",
                      background: "var(--oc-accent-tint)" }}>
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="px-1.5 py-[1px] rounded-[3px] text-[9px] font-bold"
                        style={{ background: "var(--oc-accent)", color: "#fff" }}>STAGED</span>
                      <b className="text-[12.5px]">{state.staged.label}</b>
                      <span className="text-[11px]" style={{ color: "var(--oc-muted)" }}>
                        shown everywhere as if done — nothing reaches the run until Commit
                      </span>
                      <span className="ml-auto flex items-center gap-2">
                        {isReal && (
                          <input value={realTyped} onChange={(e) => setRealTyped(e.target.value)}
                            placeholder="type REAL to send" aria-label="type REAL to confirm real orders"
                            className="h-[24px] w-[150px] rounded-[5px] px-2 text-[11px] font-semibold tracking-wide"
                            style={{ background: "var(--oc-chip)", color: "var(--oc-neg)", border: "1px solid var(--oc-neg)" }} />
                        )}
                        <button type="button"
                          disabled={commit.isPending || (isReal && realTyped !== "REAL") || !!state?.session.order_error}
                          onClick={() => { commit.mutate(); setRealTyped(""); }}
                          title={isReal ? "sends REAL orders through the run's LiveBroker" : "fills on the run's paper broker"}
                          className="px-2.5 h-[24px] rounded-[5px] text-[11.5px] font-semibold disabled:opacity-40"
                          style={{ background: isReal ? "var(--oc-neg)" : "var(--oc-accent)", color: "#fff" }}>
                          {isReal ? "Commit to broker" : "Commit ⏎"}</button>
                        <button type="button" onClick={() => discard.mutate()}
                          className="px-2.5 h-[24px] rounded-[5px] text-[11.5px]"
                          style={{ background: "var(--oc-chip)", color: "var(--oc-muted)" }}>
                            Revert esc</button>
                      </span>
                    </div>
                    <div className="mt-1.5 flex gap-5 text-[11.5px] flex-wrap tabular-nums">
                      <Delta label="max profit" good
                        a={mNow?.maxProfit} unlimitedA={mNow?.maxProfitUnlimited}
                        b={mStaged?.maxProfit} unlimitedB={mStaged?.maxProfitUnlimited} />
                      <Delta label="max loss"
                        a={mNow?.maxLoss} unlimitedA={mNow?.maxLossUnlimited}
                        b={mStaged?.maxLoss} unlimitedB={mStaged?.maxLossUnlimited} />
                      <span><span style={{ color: "var(--oc-faint)" }}>margin</span>{" "}
                        {inr0(state.staged.margin_before)} → <b>{inr0(state.staged.margin_after)}</b>
                        <span style={{ color: "var(--oc-faint)" }}> · {state.staged.margin_source}</span>
                      </span>
                    </div>
                  </div>
                )}
    </>
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
                      style={{ color: "var(--oc-faint)" }}>Total MTM · realised + open</span>
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
                  </div>
                  <div className="text-[11px] mt-0.5" style={{ color: "var(--oc-muted)" }}>
                    {risk?.legs_open ? `${risk.legs_open} legs open` : "no open position"} · paused{" "}
                    {state?.session.clock ?? "—"}
                  </div>
                  {/* A session's realised P&L survives closing the position — it is money you
                      made. Said plainly, because "MTM ₹5,487 · NO POSITION" reads as a bug, and
                      a later structure's rail would otherwise show the previous one's profit as
                      if it were its own. Reset clears it. */}
                  {!!risk?.realised && (
                    <div className="text-[11px] mt-1 flex items-center gap-2">
                      <span style={{ color: "var(--oc-faint)" }}>
                        {risk.legs_open
                          ? `includes ${inr0(risk.realised)} banked from closed legs`
                          : `${inr0(risk.realised)} banked this session · nothing open`}
                      </span>
                      <button type="button" onClick={() => reset.mutate()}
                        className="underline" style={{ color: "var(--oc-muted)" }}>reset</button>
                    </div>
                  )}
                  <div className="grid grid-cols-2 gap-2 mt-3">
                    <Tile label="Margin" value={inr0(risk?.margin ?? 0)}
                      sub={risk?.margin_detail
                        ? `est · scan ${inr0(risk.margin_detail.span)} + exposure ${inr0(risk.margin_detail.exposure)}`
                        : `${risk?.margin_source ?? "model"} · ${pctOf(risk?.margin, risk?.capital)} of capital`} />
                    <Tile label="POP" value={mNow?.pop == null ? "—" : `${(mNow.pop * 100).toFixed(1)}%`}
                      sub={mNow?.rewardRisk ? `R:R ${mNow.rewardRisk.toFixed(1)}` : "—"} />
                    <Tile label="Max profit" tone="pos"
                      value={mNow ? (mNow.maxProfitUnlimited ? "unlimited" : inr0(mNow.maxProfit)) : "—"}
                      sub={pctOf(mNow?.maxProfit, risk?.margin) + " of margin"} />
                    <Tile label="Max loss" tone="neg"
                      value={mNow ? (mNow.maxLossUnlimited ? "unlimited" : inr0(mNow.maxLoss)) : "—"}
                      sub={pctOf(mNow?.maxLoss, risk?.margin) + " of margin"} />
                    <Tile label="Breakeven"
                      value={mNow?.breakevens.length
                        ? mNow.breakevens.map((b) => Math.round(b).toLocaleString("en-IN")).join(" / ")
                        : "—"}
                      sub={mNow?.breakevens.length && state?.market.spot
                        ? `${signed(100 * (mNow.breakevens[0] / state.market.spot - 1))}% from spot` : "—"} />
                    <Tile label="Open P&L" tone={(risk?.unrealised ?? 0) >= 0 ? "pos" : "neg"}
                      value={`${inr0(risk?.unrealised ?? 0)}${risk?.margin ? ` · ${pctOf(risk.unrealised, risk.margin)}` : ""}`}
                      sub={`banked ${inr0(risk?.realised ?? 0)} · ${inr0(-(risk?.charges ?? 0))} costs`} />
                  </div>
                  {risk?.margin_source === "model" && !!risk.margin && (
                    <div className="mt-2 text-[10.5px]" style={{ color: "var(--oc-faint)" }}>
                      Estimated the way SPAN is: worst loss over a ±6% move (hedges offset) plus 2%
                      exposure on every short unit. Within ~10% of a Kite basket on a spread; still
                      an estimate.
                    </div>
                  )}
                </Panel>
  );

  const alertsCard = (
                <AlertsCard alerts={state?.alerts ?? []} disabled={!state}
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
        <Tile label={`Total MTM · ${!risk?.legs_open ? "no position" : mNow && !mNow.maxLossUnlimited ? "defined risk" : "undefined risk"}`}
          tone={!risk?.mtm ? undefined : risk.mtm > 0 ? "pos" : "neg"}
          value={`${inr0(risk?.mtm ?? 0)}${risk?.margin ? ` · ${pctOf(risk.mtm, risk.margin)}` : ""}`}
          sub={risk?.realised ? `banked ${inr0(risk.realised)}` : `${risk?.legs_open ?? 0} legs open`} />
        <Tile label="Margin" value={inr0(risk?.margin ?? 0)}
          sub={risk?.margin_detail
            ? `est · scan ${inr0(risk.margin_detail.span)} + exp ${inr0(risk.margin_detail.exposure)}`
            : `${risk?.margin_source ?? "model"}`} />
        <Tile label="POP" value={mNow?.pop == null ? "—" : `${(mNow.pop * 100).toFixed(1)}%`}
          sub={mNow?.rewardRisk ? `R:R ${mNow.rewardRisk.toFixed(1)}` : "—"} />
        <Tile label="Max profit" tone="pos"
          value={mNow ? (mNow.maxProfitUnlimited ? "unlimited" : inr0(mNow.maxProfit)) : "—"}
          sub={pctOf(mNow?.maxProfit, risk?.margin) + " of margin"} />
        <Tile label="Max loss" tone="neg"
          value={mNow ? (mNow.maxLossUnlimited ? "unlimited" : inr0(mNow.maxLoss)) : "—"}
          sub={pctOf(mNow?.maxLoss, risk?.margin) + " of margin"} />
        <Tile label="Breakeven"
          value={mNow?.breakevens.length
            ? mNow.breakevens.map((b) => Math.round(b).toLocaleString("en-IN")).join(" / ") : "—"}
          sub={mNow?.breakevens.length && state?.market.spot
            ? `${signed(100 * (mNow.breakevens[0] / state.market.spot - 1))}% from spot` : "—"} />
        <Tile label="Open P&L" tone={(risk?.unrealised ?? 0) >= 0 ? "pos" : "neg"}
          value={`${inr0(risk?.unrealised ?? 0)}${risk?.margin ? ` · ${pctOf(risk.unrealised, risk.margin)}` : ""}`}
          sub={`${inr0(-(risk?.charges ?? 0))} costs`} />
      </div>
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
                      disabled={commit.isPending || (isReal && realTyped !== "REAL") || !!state?.session.order_error}
                      onClick={() => { commit.mutate(); setRealTyped(""); }}
                      className="px-2 h-[20px] rounded-[4px] text-[10.5px] font-semibold disabled:opacity-40"
                      style={{ background: isReal ? "var(--oc-neg)" : "var(--oc-accent)", color: "#fff" }}>
                      {isReal ? "Commit to broker" : "Commit"}</button>
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
            {!legsShown.length ? (
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
                  {legsShown.map((l) => (
                    <LegRow key={l.id} leg={l} grid={gridStep} chainExpiry={state?.chain.expiry ?? null}
                      onStage={(b) => stage.mutate(b)} />
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
          {[...(liveRuns?.runs ?? [])]
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
          onChange={(e) => { setDay(e.target.value); open.mutate({ underlying, day: e.target.value, at: "09:30" }); }}
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
        {/* the CYCLE bar: first fill → last expiry, in sessions. The day bar above says
            where in the session you are; this says where in the trade (owner, 2026-09-09). */}
        {state?.cycle && (
          <>
            <div className="w-px h-3.5" style={{ background: "var(--oc-line)" }} />
            <div className="flex items-center gap-1.5 shrink-0"
              title={`cycle: ${state.cycle.start} → expiry ${state.cycle.end} · NSE session ${state.cycle.session_no} of ${state.cycle.sessions}${state.cycle.beyond_data ? ` · the store's last captured day is ${state.cycle.data_until}; later sessions appear once captured (~16:00 IST daily)` : ""}`}>
              <span className="text-[9px] whitespace-nowrap" style={{ color: "var(--oc-faint)" }}>
                {expiryChip(state.cycle.start)}
              </span>
              <div className="relative w-[140px] h-1 rounded" style={{ background: "var(--oc-chip)" }}>
                <div className="absolute left-0 top-0 h-1 rounded"
                  style={{ width: `${state.cycle.pct}%`,
                    background: state.cycle.done ? "var(--oc-muted)" : "var(--oc-caution)" }} />
              </div>
              <span className="text-[9px] whitespace-nowrap font-semibold"
                style={{ color: state.cycle.done ? "var(--oc-muted)" : "var(--oc-caution)" }}>
                {state.cycle.done ? "expired" : `exp ${expiryChip(state.cycle.end)}`}
                <span className="font-normal" style={{ color: "var(--oc-faint)" }}>
                  {" "}· session {state.cycle.session_no}/{state.cycle.sessions}
                  {state.cycle.beyond_data ? ` · data to ${expiryChip(state.cycle.data_until)}` : ""}
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
        <StripItem label={`${underlying} (parity)`}>{num(state?.market.spot ?? null)}</StripItem>
        <StripItem label={`FUT ${state?.market.expiry ?? ""}`}>
          {num(state?.market.fut ?? null)}
          <span className="ml-1.5 text-[10.5px] font-normal" style={{ color: "var(--oc-muted)" }}>
            carry {signed(state?.market.carry ?? null)}
          </span>
        </StripItem>
        <StripItem label="Day (so far)">
          {num(state?.market.day_low ?? null, 0)}–{num(state?.market.day_high ?? null, 0)}
        </StripItem>
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

      {error && (
        <div className="px-3 py-2 text-[12px]"
          style={{ background: "var(--oc-neg-fill)", color: "var(--oc-neg)" }}>{error}</div>
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
        <div className="w-[560px] shrink-0 rounded-[10px] overflow-hidden"
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
            style={{ gridTemplateColumns: COLS, color: "var(--oc-faint)",
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
              <ChainRow key={r.strike} row={r} probed={probed} onProbe={probe}
                onPick={(side, right, strike) =>
                  stage.mutate({ kind: "add", side, right, strike, lots })} />
            ))}
          </div>
        </div>

        {chainOpen ? (
          /* chain · payoff+positions · rail */
          <div className="flex-1 min-w-0 space-y-2.5">
            <div className="flex gap-2.5 items-start">
              <div className="flex-1 min-w-0 space-y-2.5 flex flex-col">
                {payoffPanel}
                {stagedBar}
                {positionsPanel}
              </div>
              <div className="w-[348px] shrink-0 space-y-2.5">
                {railColumn}
              </div>
            </div>
          </div>
        ) : (
          /* chain hidden: risk + positions on the left, the chart on the right */
          <div className="flex-1 min-w-0 flex gap-2.5 items-start">
            <div className="w-[50%] min-w-[460px] space-y-2.5">
              {riskStrip}
              {positionsPanel}
              {alertsCard}
            </div>
            <div className="flex-1 min-w-0 space-y-2.5 flex flex-col">
              {payoffPanel}
              {stagedBar}
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
        <span>REALISED <b style={{ color: "var(--oc-ink)" }}>{inr0(risk?.realised ?? 0)}</b></span>
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
