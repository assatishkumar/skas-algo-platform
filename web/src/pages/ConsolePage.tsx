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
import { useMutation, useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type {
  ConsoleChainLeg, ConsoleChainRow, ConsoleLeg, ConsoleProbe, ConsoleState,
} from "../types";
import PayoffSvg, { toPayoffLegs } from "../components/console/PayoffSvg";
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
  { keys: "?", does: "show or hide this list" },
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

function Panel({ children, className = "" }: {
  children: React.ReactNode; className?: string;
}) {
  return (
    <div className={`rounded-[10px] p-3 ${className}`}
      style={{ background: "var(--oc-surface)", border: "1px solid var(--oc-line)" }}>
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

/** One position row. Strike and size are EDITABLE here, because "I want that leg one
 *  strike higher" is a normal adjustment and re-typing the whole leg is not how anyone
 *  thinks about it. Both go through staging like everything else: a strike change is a
 *  roll (close here, open there) and a size change is a partial exit or a top-up, so the
 *  payoff previews it and the charges are real. */
function LegRow({ leg, grid, onStage }: {
  leg: ConsoleLeg; grid: number;
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
      <td className="text-right">{num(leg.entry)}</td>
      <td className="text-right">{leg.ltp == null ? "—" : num(leg.ltp)}</td>
      <td className="text-right font-semibold"
        style={{ color: (leg.pnl ?? 0) >= 0 ? "var(--oc-pos)" : "var(--oc-neg)" }}>
        {leg.pnl == null ? "—" : inr0(leg.pnl)}
      </td>
      <td className="text-right" style={{ color: "var(--oc-muted)" }}>
        {value == null ? "—" : inr0(value)}
      </td>
      <td className="text-right whitespace-nowrap">
        <span className="inline-flex items-center gap-[3px] mr-1.5">
          <MiniBtn onClick={() => setExitLots((n) => Math.max(1, n - 1))}>−</MiniBtn>
          <span className="text-[10.5px] tabular-nums"
            style={{ minWidth: 16, display: "inline-block", textAlign: "center" }}>
            {exitLots}</span>
          <MiniBtn onClick={() => setExitLots((n) => Math.min(leg.lots, n + 1))}>+</MiniBtn>
        </span>
        <button type="button"
          onClick={() => onStage({ kind: "exit", leg_id: leg.id, lots: exitLots })}
          className="px-2 h-[20px] rounded-[4px] text-[10.5px]"
          style={{ border: "1px solid var(--oc-line)", color: "var(--oc-accent)" }}>
          Exit {exitLots === leg.lots ? "all" : exitLots}</button>
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

function MiniBtn({ children, onClick }: { children: React.ReactNode; onClick: () => void }) {
  return (
    <button type="button" onClick={onClick}
      className="w-[18px] h-[18px] rounded-[4px] text-[11px] leading-[17px] shrink-0"
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

  const { data: days } = useQuery({
    queryKey: ["console-days", underlying],
    queryFn: () => api.consoleDays(underlying),
  });

  const open = useMutation({
    mutationFn: (body: { underlying: string; day?: string | null }) =>
      api.consoleOpen({ ...body, at: params.get("at") ?? "09:20",
        expiry: params.get("expiry") ?? undefined }),
    onSuccess: (s) => {
      setState(s); setDay(s.session.date); setError(null);
      setParams({ u: s.session.underlying, day: s.session.date, at: s.session.clock },
        { replace: true });
    },
    onError: (e: Error) => setError(e.message),
  });

  const move = useMutation({
    mutationFn: (body: Parameters<typeof api.consoleTransport>[1]) =>
      api.consoleTransport(state!.session.id, body),
    onSuccess: (s) => {
      setState(s); setDay(s.session.date);
      setParams({ u: s.session.underlying, day: s.session.date, at: s.session.clock },
        { replace: true });
    },
    onError: (e: Error) => setError(e.message),
  });

  const stage = useMutation({
    mutationFn: (body: Parameters<typeof api.consoleStage>[1]) =>
      api.consoleStage(state!.session.id, body),
    onSuccess: (s) => { setState(s); setError(null); },
    onError: (e: Error) => setError(e.message),
  });
  const commit = useMutation({
    mutationFn: () => api.consoleCommit(state!.session.id),
    onSuccess: (s) => { setState(s); setError(null); },
    onError: (e: Error) => setError(e.message),
  });
  const undo = useMutation({
    mutationFn: () => api.consoleUndo(state!.session.id),
    onSuccess: setState,
  });
  const reset = useMutation({
    mutationFn: () => api.consoleReset(state!.session.id),
    onSuccess: setState,
  });
  const discard = useMutation({
    mutationFn: () => api.consoleDiscard(state!.session.id),
    onSuccess: setState,
  });

  const pickExpiry = useMutation({
    mutationFn: (expiry: string) => api.consoleChain(state!.session.id, { expiry }),
    onSuccess: setState,
  });

  // Open a session once the store's day list is known — the newest captured day.
  useEffect(() => {
    if (!days || opened.current) return;
    opened.current = true;
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
      if (e.key === "Enter" && state?.staged) { e.preventDefault(); commit.mutate(); return; }
      if ((e.key === "u" || e.key === "U") && state?.session.can_undo) {
        e.preventDefault(); undo.mutate(); return;
      }
      if (!state || e.metaKey || e.ctrlKey) return;
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
  }, [state, move, commit, discard, undo]);

  // Probed reference prices, keyed "CE24000". Cleared whenever the cursor or the ladder
  // moves — a price fetched at 09:30 is not an answer about 11:00.
  const [probed, setProbed] = useState<Record<string, ConsoleProbe>>({});
  const probe = async (right: "CE" | "PE", strike: number) => {
    if (!state) return;
    const key = `${right}${strike}`;
    try {
      const r = await api.consoleProbe(state.session.id, right, strike);
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
  const risk = state?.risk;
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
    const legs = toPayoffLegs(state?.legs ?? []);
    return spotNow && expNow && legs.length
      ? computeMetrics(legs, spotNow, expNow, state?.session.date) : null;
  }, [state?.legs, spotNow, expNow, state?.session.date]);
  const mStaged = useMemo(() => {
    const legs = toPayoffLegs(state?.staged?.after_legs ?? []);
    return spotNow && expNow && legs.length
      ? computeMetrics(legs, spotNow, expNow, state?.session.date) : null;
  }, [state?.staged, spotNow, expNow, state?.session.date]);
  const busy = open.isPending || move.isPending;

  return (
    <div className="oc-root font-plex min-h-[calc(100vh-3.5rem)]"
      style={{ background: "var(--oc-ground)", color: "var(--oc-ink)" }}>

      {/* session bar */}
      <div className="relative h-10 flex items-center gap-2 px-3"
        style={{ background: "var(--oc-surface)", borderBottom: "1px solid var(--oc-line)" }}>
        {showKeys && <KeyHelp onClose={() => setShowKeys(false)} notes={state?.notes ?? []} />}
        <span className="px-2 h-[22px] leading-[22px] rounded-[5px] text-[10px] font-bold tracking-wide"
          style={{ border: "1px solid var(--oc-accent)", color: "var(--oc-accent)" }}>
          REPLAY
        </span>
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
          onChange={(e) => { setDay(e.target.value); open.mutate({ underlying, day: e.target.value }); }}
          className="h-[22px] rounded-[5px] px-1.5 text-[11px]"
          style={{ background: "var(--oc-chip)", color: "var(--oc-ink)", border: "none" }} />
        <div className="w-px h-5" style={{ background: "var(--oc-line)" }} />

        <div className="flex items-center gap-1">
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

        <div className="ml-auto flex items-center gap-2">
          <button type="button" onClick={() => setShowKeys((v) => !v)}
            title="Keyboard shortcuts (?)"
            className="w-[22px] h-[22px] rounded-[5px] text-[11px] font-bold"
            style={{ background: showKeys ? "var(--oc-accent-dim)" : "var(--oc-chip)",
              color: showKeys ? "var(--oc-accent)" : "var(--oc-muted)" }}>?</button>
          <span className="text-[10px] font-bold px-2 h-[22px] leading-[22px] rounded-[4px]"
            style={{ background: "var(--oc-chip)", color: "var(--oc-muted)" }}>
            {busy ? "…" : state?.session.status ?? "READY"}
          </span>
        </div>
      </div>

      {/* scrubber */}
      <div className="h-4 flex items-center gap-2 px-3"
        style={{ background: "var(--oc-surface)", borderBottom: "1px solid var(--oc-hair)" }}>
        <span className="text-[9px]" style={{ color: "var(--oc-faint)" }}>
          {state?.session.range[0] ?? "09:15"}
        </span>
        <div className="flex-1 h-1 rounded" style={{ background: "var(--oc-chip)" }}>
          <div className="h-1 rounded"
            style={{ width: `${state?.session.played_pct ?? 0}%`, background: "var(--oc-accent)" }} />
        </div>
        <span className="text-[9px]" style={{ color: "var(--oc-faint)" }}>
          {state?.session.range[1] ?? "15:40"}
        </span>
      </div>

      {/* market strip */}
      <div className="h-[30px] flex items-center overflow-x-auto"
        style={{ background: "var(--oc-panel2)", borderBottom: "1px solid var(--oc-line)" }}>
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

      {/* body: chain 560 · analysis flex · rail 348 */}
      <div className="flex gap-2.5 p-2.5 items-start">
        <div className="w-[560px] shrink-0 rounded-[10px] overflow-hidden"
          style={{ background: "var(--oc-surface)", border: "1px solid var(--oc-line)" }}>
          {/* expiry chips */}
          <div className="h-9 flex items-center gap-1 px-2 overflow-x-auto"
            style={{ borderBottom: "1px solid var(--oc-hair)" }}>
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

        <div className="flex-1 min-w-0 space-y-2.5">
          <div className="flex gap-2.5 items-start">
            <div className="flex-1 min-w-0 space-y-2.5">
            <Panel>
              <div className="flex items-center justify-between mb-1">
                <span className="text-[13px] font-semibold">Payoff</span>
                <span className="text-[10.5px]" style={{ color: "var(--oc-faint)" }}>
                  expiry · T+0 dashed · staged dotted
                </span>
              </div>
              <PayoffSvg legs={state?.legs ?? []} staged={state?.staged?.after_legs ?? null}
                spot={state?.market.spot ?? null} expiry={state?.chain.expiry ?? null}
                today={state?.session.date ?? ""} />
            </Panel>

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
                    previewed on the chart before commit
                  </span>
                  <span className="ml-auto flex gap-2">
                    <button type="button" onClick={() => commit.mutate()}
                      className="px-2.5 h-[24px] rounded-[5px] text-[11.5px] font-semibold"
                      style={{ background: "var(--oc-accent)", color: "#fff" }}>Apply ⏎</button>
                    <button type="button" onClick={() => discard.mutate()}
                      className="px-2.5 h-[24px] rounded-[5px] text-[11.5px]"
                      style={{ background: "var(--oc-chip)", color: "var(--oc-muted)" }}>
                      Discard esc</button>
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
            </div>
            <div className="w-[348px] shrink-0 space-y-2.5">
            <Panel>
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
              <div className="text-[21px] font-semibold mt-1"
                style={{ color: !risk?.mtm ? "var(--oc-ink)"
                  : risk.mtm > 0 ? "var(--oc-pos)" : "var(--oc-neg)" }}>
                {inr0(risk?.mtm ?? 0)}
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
                  sub={`${risk?.margin_source ?? "model"} · ${pctOf(risk?.margin, risk?.capital)} of capital`} />
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
                  value={inr0(risk?.unrealised ?? 0)}
                  sub={`banked ${inr0(risk?.realised ?? 0)} · ${inr0(-(risk?.charges ?? 0))} costs`} />
              </div>
              {risk?.margin_source === "model" && (
                <div className="mt-2 text-[10.5px]" style={{ color: "var(--oc-caution)" }}>
                  Model margin: span+exposure on the shorts, blind to long hedges — it reads
                  several times a broker basket on a spread. Set a measured anchor to make the
                  percentages real.
                </div>
              )}
            </Panel>
            </div>
          </div>

          {/* Positions spans the payoff AND the rail. A dense table of editable numbers
              needs room: inside a ~400px middle column the leg cell wrapped onto a second
              line, which is what made the row jump as values changed length. */}
          <Panel>
        <div className="flex items-center justify-between mb-1.5">
          <span className="text-[13px] font-semibold">Positions</span>
          <span className="text-[10.5px]" style={{ color: "var(--oc-faint)" }}>
            {state?.legs.length ?? 0} legs · lot {state?.session.lot_size ?? "—"}
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
            {(state?.legs.length || risk?.realised) ? (
              <button type="button" className="ml-3 underline"
                title="clear the book AND this session's realised P&L — a clean slate"
                onClick={() => reset.mutate()}
                style={{ color: "var(--oc-muted)" }}>Reset</button>
            ) : null}
          </span>
        </div>
        {!state?.legs.length ? (
          <div className="py-4 text-center text-[12px]" style={{ color: "var(--oc-faint)" }}>
            No position. Click B or S on any strike to stage a leg.
          </div>
        ) : (
          <table className="w-full text-[12px] tabular-nums table-fixed">
            {/* Fixed widths, so a row cannot re-flow as its numbers change length. */}
            <colgroup>
              <col style={{ width: 52 }} /><col style={{ width: 40 }} />
              <col style={{ width: 190 }} /><col style={{ width: 150 }} />
              <col /><col /><col /><col /><col style={{ width: 215 }} />
            </colgroup>
            <thead>
              <tr className="text-[9px] uppercase tracking-[.06em]"
                style={{ color: "var(--oc-faint)" }}>
                <th className="text-left font-semibold py-1">On</th>
                <th className="text-left font-semibold">Side</th>
                <th className="text-left font-semibold">Strike</th>
                <th className="text-left font-semibold">Lots</th>
                <th className="text-right font-semibold">Entry</th>
                <th className="text-right font-semibold">LTP</th>
                <th className="text-right font-semibold">P&amp;L</th>
                <th className="text-right font-semibold">Value</th>
                <th className="text-right font-semibold">Exit</th>
              </tr>
            </thead>
            <tbody>
              {state.legs.map((l) => (
                <LegRow key={l.id} leg={l} grid={gridStep}
                  onStage={(b) => stage.mutate(b)} />
              ))}
            </tbody>
          </table>
        )}
      </Panel>
        </div>
      </div>


      {/* footer — the design's P&L strip; for now it carries the keyboard ladder, because a
          transport nobody can find is a transport nobody uses. */}
      <div className="h-7 flex items-center gap-4 px-3 text-[10.5px]"
        style={{ background: "var(--oc-panel2)", borderTop: "1px solid var(--oc-line)",
          color: "var(--oc-faint)" }}>
        <span style={{ color: "var(--oc-accent)" }}>
          {state?.session.requires_confirm ? "STAGED — apply to trade" : "clicks trade at once · U undoes"}
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
