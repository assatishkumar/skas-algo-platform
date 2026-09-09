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
import type { ConsoleChainRow, ConsoleState } from "../types";

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

function ChainRow({ row, onPick }: {
  row: ConsoleChainRow;
  onPick: (side: "B" | "S", right: "CE" | "PE", strike: number) => void;
}) {
  const dead = !row.ce.quoted && !row.pe.quoted;
  return (
    <div className="grid items-center text-[12px] group"
      style={{
        gridTemplateColumns: COLS, height: 32,
        opacity: dead ? 0.45 : 1,
        borderBottom: "1px solid var(--oc-hair)",
        background: row.atm ? "var(--oc-accent-tint)" : undefined,
      }}>
      <Cell align="right" faint>{row.ce.delta == null ? "—" : num(row.ce.delta, 2)}</Cell>
      <Cell align="right" tint={row.itm_ce} strong>{row.ce.quoted ? num(row.ce.ltp) : "—"}</Cell>
      <BsCell tint={row.itm_ce} onB={() => onPick("B", "CE", row.strike)}
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
      <BsCell tint={row.itm_pe} onB={() => onPick("B", "PE", row.strike)}
        onS={() => onPick("S", "PE", row.strike)} disabled={!row.pe.quoted} left />
      <Cell align="right" tint={row.itm_pe} strong>{row.pe.quoted ? num(row.pe.ltp) : "—"}</Cell>
      <Cell align="right" faint>{row.pe.delta == null ? "—" : num(row.pe.delta, 2)}</Cell>
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
        background: tint ? "var(--oc-accent-tint)" : undefined,
      }}>
      {children}
    </div>
  );
}

function BsCell({ onB, onS, disabled, tint, left }: {
  onB: () => void; onS: () => void; disabled?: boolean; tint?: boolean; left?: boolean;
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
        background: tint ? "var(--oc-accent-tint)" : undefined }}>
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
  const opened = useRef(false);

  const { data: days } = useQuery({
    queryKey: ["console-days", underlying],
    queryFn: () => api.consoleDays(underlying),
  });

  const open = useMutation({
    mutationFn: (body: { underlying: string; day?: string | null }) =>
      api.consoleOpen({ ...body, at: params.get("at") ?? "09:16",
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

  // Space/,/. are the handoff's transport keys. Guarded on inputs so typing a date is safe.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (el && /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName)) return;
      if (!state) return;
      if (e.key === ",") { e.preventDefault(); move.mutate({ op: "step", minutes: e.shiftKey ? -15 : -1 }); }
      if (e.key === ".") { e.preventDefault(); move.mutate({ op: "step", minutes: e.shiftKey ? 15 : 1 }); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [state, move]);

  // §9: this page renders BEFORE the query resolves, so every read below must survive a
  // null state. Nothing here dot-accesses into a derived map without a guard.
  const rows = state?.chain.rows ?? [];
  const atmIndex = useMemo(() => rows.findIndex((r) => r.atm), [rows]);
  const busy = open.isPending || move.isPending;

  return (
    <div className="oc-root font-plex min-h-[calc(100vh-3.5rem)]"
      style={{ background: "var(--oc-ground)", color: "var(--oc-ink)" }}>

      {/* session bar */}
      <div className="h-10 flex items-center gap-2 px-3"
        style={{ background: "var(--oc-surface)", borderBottom: "1px solid var(--oc-line)" }}>
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
              <ChainRow key={r.strike} row={r}
                onPick={() => { /* P3: stages a leg */ }} />
            ))}
          </div>
        </div>

        <div className="flex-1 min-w-0 rounded-[10px] p-4"
          style={{ background: "var(--oc-surface)", border: "1px solid var(--oc-line)",
            minHeight: 320 }}>
          <div className="text-[13px] font-semibold mb-1">Payoff · positions · staging</div>
          <div className="text-[12px]" style={{ color: "var(--oc-muted)" }}>
            Phase 3. The chain is live above — clicking B/S will stage a leg here, previewed
            on the payoff before it is committed.
          </div>
          <ul className="mt-4 space-y-1 text-[11px]" style={{ color: "var(--oc-faint)" }}>
            {(state?.notes ?? []).map((n) => <li key={n}>· {n}</li>)}
          </ul>
        </div>

        <div className="w-[348px] shrink-0 rounded-[10px] p-4"
          style={{ background: "var(--oc-surface)", border: "1px solid var(--oc-line)",
            minHeight: 320 }}>
          <div className="text-[9.5px] font-semibold uppercase tracking-[.07em]"
            style={{ color: "var(--oc-faint)" }}>Total MTM · realised + open</div>
          <div className="text-[21px] font-semibold mt-1">₹0</div>
          <div className="text-[11px] mt-0.5" style={{ color: "var(--oc-muted)" }}>
            no open position · paused {state?.session.clock ?? "—"}
          </div>
          <div className="mt-4 text-[11px]" style={{ color: "var(--oc-faint)" }}>
            Risk tiles, alerts and presets arrive in Phase 3. Margin will be labelled by
            source — the model estimate reads several times a broker basket on a hedged
            spread, so the percentages are only real against a measured anchor.
          </div>
          <div className="mt-3 text-[11px]" style={{ color: "var(--oc-muted)" }}>
            ATM row {atmIndex >= 0 ? `#${atmIndex + 1}` : "—"} · strikes {rows.length} ·
            grid {state?.chain.listing_grid ? "listing (50s)" : "100s"}
          </div>
        </div>
      </div>
    </div>
  );
}
