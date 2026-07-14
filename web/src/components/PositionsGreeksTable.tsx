import { useMemo, useState } from "react";
import { formatInr } from "../lib/format";
import { parseOptionSymbol } from "../lib/symbol";
import type { LiveRunSnapshot } from "../types";

// Per-share vs per-position greeks unit. Default: per position (matches the design handoff).
type GMode = "share" | "pos";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function fmtExpiry(iso: string): string {
  const p = (iso ?? "").split("-");
  if (p.length !== 3) return iso;
  const m = MONTHS[Number(p[1]) - 1];
  return m ? `${Number(p[2])} ${m} '${p[0].slice(2)}` : iso;
}
function fmtDay(iso?: string | null): string | null {
  if (!iso) return null;
  const p = iso.split("-");
  if (p.length !== 3) return iso;
  const m = MONTHS[Number(p[1]) - 1];
  return m ? `${Number(p[2])} ${m}` : iso;
}

const sign = (v: number) => (v >= 0 ? "text-[var(--pos)]" : "text-[var(--danger)]");
/** signed plain number, fixed decimals (leading + on non-negative). */
function sfx(v: number, d: number): string {
  const s = Math.abs(v).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
  return (v < 0 ? "−" : "+") + s;
}
/** signed rupee (leading + on non-negative; formatInr already signs negatives with −₹). */
const sInr = (v: number, d = 0) => (v >= 0 ? "+" + formatInr(v, d) : formatInr(v, d));

// Column grid — one template shared by header / legs / net row so everything lines up.
const COLS = "1.9fr 0.55fr 1.05fr 0.72fr 0.78fr 0.9fr 0.9fr 0.58fr 1fr";

/** The positions monitor for an options deployment: a full-greeks table (Δ, Γ, Θ/day, Vega, IV per
 *  leg) switchable between per-share and per-position (×units), with a Net row and a net-greeks
 *  strip (incl Δ-cash). Greeks are the per-share, position-signed values the backend attaches from
 *  live quotes (short legs read Θ+, Γ/Vega−); position values are per-share × units, and every net /
 *  Δ-cash is DERIVED here by summing the visible leg values (never stored separately). */
export default function PositionsGreeksTable({ run }: { run: LiveRunSnapshot }) {
  const [mode, setMode] = useState<GMode>("pos");
  const spot = run.underlying_spot ?? null;

  const legs = useMemo(() => {
    return (run.positions ?? []).flatMap((p) => {
      const o = parseOptionSymbol(p.symbol);
      if (!o) return [];
      const units = p.units;
      const mult = mode === "share" ? 1 : units;
      const g = (v: number | null | undefined) => (v == null ? null : v * mult);
      return [{
        key: p.symbol,
        side: (p.direction ?? 1) >= 0 ? "BUY" : "SELL",
        strike: o.strike,
        right: o.right,
        underlying: o.underlying,
        expiry: o.expiry,
        entry: fmtDay(p.entry_date),
        units,
        lots: p.lot_size && p.lot_size > 0 ? Math.round(units / p.lot_size) : null,
        lotSize: p.lot_size ?? null,
        avg: p.avg_price,
        ltp: p.ltp,
        dlt: g(p.delta),
        gam: g(p.gamma),
        the: g(p.theta),
        veg: g(p.vega),
        posDelta: p.pos_delta ?? (p.delta != null ? p.delta * units : null),
        iv: p.iv ?? null,
        unreal: p.unrealized_pnl,
      }];
    });
  }, [run.positions, mode]);

  const net = useMemo(() => {
    const acc = { dlt: 0, gam: 0, the: 0, veg: 0, unreal: 0, posDelta: 0, ivNum: 0, ivDen: 0 };
    for (const l of legs) {
      acc.dlt += l.dlt ?? 0;
      acc.gam += l.gam ?? 0;
      acc.the += l.the ?? 0;
      acc.veg += l.veg ?? 0;
      acc.unreal += l.unreal;
      acc.posDelta += l.posDelta ?? 0;
      if (l.iv != null) {
        const w = Math.abs(l.avg * l.units); // premium-weighted book IV
        acc.ivNum += l.iv * w;
        acc.ivDen += w;
      }
    }
    return acc;
  }, [legs]);

  if (!legs.length) return null;

  const deltaCash = spot != null ? net.posDelta * spot : null; // net POSITION Δ × spot (always position)
  const ivBook = net.ivDen > 0 ? net.ivNum / net.ivDen : null;
  const lotSets = run.lots ?? legs[0]?.lots ?? null;
  const lotSize = legs[0]?.lotSize ?? null;
  const unitTag = mode === "share" ? "SHARE" : "POS";

  // Per-mode greek formatters (per the spec: share Δ 3dp/Γ 6dp/Θ,Vega 1dp; pos Δ 1dp/Γ 4dp/Θ,Vega ₹).
  const fDelta = (v: number) => sfx(v, mode === "share" ? 3 : 1);
  const fGamma = (v: number) => sfx(v, mode === "share" ? 6 : 4);
  const fFlow = (v: number) => (mode === "share" ? sfx(v, 1) : sInr(v)); // Θ/day, Vega/1%

  const Cell = ({ v, fmt, dim }: { v: number | null; fmt: (n: number) => string; dim?: boolean }) =>
    v == null ? (
      <div className="text-right tabular-nums text-[var(--faint)]">—</div>
    ) : (
      <div className={`text-right tabular-nums whitespace-nowrap font-['Space_Grotesk'] font-semibold text-[13px] ${dim ? "text-[var(--muted)]" : sign(v)}`}>
        {fmt(v)}
      </div>
    );

  const Tile = ({ label, value, sub, tone }: { label: string; value: string; sub: string; tone: number | null }) => (
    <div className="rounded-[12px] bg-[var(--stat)] px-3 py-2.5">
      <div className="text-[10.5px] uppercase tracking-wide text-[var(--faint)]">{label}</div>
      <div className={`mt-0.5 font-['Space_Grotesk'] font-bold tabular-nums text-[17px] ${tone == null ? "text-[var(--strong)]" : sign(tone)}`}>
        {value}
      </div>
      <div className="text-[11px] text-[var(--muted)] mt-0.5">{sub}</div>
    </div>
  );

  return (
    <div className="mt-3 rounded-[18px] border border-[var(--border)] bg-[var(--card)] overflow-hidden">
      {/* header */}
      <div className="flex flex-wrap items-center justify-between gap-2 px-4 pt-3.5 pb-2.5">
        <div>
          <div className="font-['Space_Grotesk'] font-bold text-[15px] text-[var(--strong)]">Positions</div>
          <div className="text-[11.5px] text-[var(--muted)]">
            {legs.length} legs{lotSets != null ? ` · ${lotSets} lots each` : ""}{lotSize != null ? ` · lot ${lotSize}` : ""} · greeks from live quotes, ~1/min
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-[10.5px] uppercase tracking-wide text-[var(--faint)]">Greeks</span>
          <div className="inline-flex rounded-[8px] bg-[var(--seg)] p-0.5 text-[12px] font-medium">
            {(["share", "pos"] as GMode[]).map((m) => (
              <button
                key={m}
                onClick={() => setMode(m)}
                className={`rounded-[6px] px-2.5 py-1 transition ${mode === m ? "bg-[var(--card)] text-[var(--strong)] shadow-sm" : "text-[var(--muted)]"}`}
              >
                {m === "share" ? "Per share" : "Per position"}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* table */}
      <div className="overflow-x-auto">
        <div className="min-w-[760px]">
          {/* column headers */}
          <div
            className="grid items-end gap-x-2 px-4 pb-1.5 border-b border-[var(--divider)] text-[10px] uppercase tracking-wide text-[var(--faint)]"
            style={{ gridTemplateColumns: COLS }}
          >
            <div>Symbol</div>
            <div className="text-right">Units</div>
            <div className="text-right">Avg → LTP</div>
            <div className="text-right">Δ · {unitTag}</div>
            <div className="text-right">Γ · {unitTag}</div>
            <div className="text-right">Θ/day · {unitTag}</div>
            <div className="text-right">Vega · {unitTag}</div>
            <div className="text-right">IV</div>
            <div className="text-right">Unrealized</div>
          </div>

          {/* legs */}
          {legs.map((l) => (
            <div
              key={l.key}
              className="grid items-center gap-x-2 px-4 py-2 border-b border-[var(--divider)]"
              style={{ gridTemplateColumns: COLS }}
            >
              <div className="flex items-center gap-2 min-w-0">
                <span
                  className={`inline-flex justify-center min-w-[44px] rounded-[6px] px-1.5 py-0.5 text-[10px] font-bold tracking-wide ${
                    l.side === "BUY" ? "bg-[var(--ok-bg)] text-[var(--ok-text)]" : "bg-[var(--rose-bg)] text-[var(--rose-text)]"
                  }`}
                >
                  {l.side}
                </span>
                <div className="min-w-0">
                  <div className="font-['Space_Grotesk'] font-bold text-[14px] text-[var(--strong)] whitespace-nowrap">
                    {l.strike} {l.right}
                  </div>
                  <div className="text-[11px] text-[var(--muted)] truncate">
                    {l.underlying} · {fmtExpiry(l.expiry)}{l.entry ? ` · in ${l.entry}` : ""}
                  </div>
                </div>
              </div>
              <div className="text-right">
                <div className="font-['Space_Grotesk'] font-semibold tabular-nums text-[13px] text-[var(--strong)]">{l.units}</div>
                {l.lots != null && l.lotSize != null && (
                  <div className="text-[10.5px] text-[var(--faint)] tabular-nums">{l.lots} × {l.lotSize}</div>
                )}
              </div>
              <div className="text-right tabular-nums whitespace-nowrap text-[12.5px] text-[var(--muted)]">
                {formatInr(l.avg, 2)} <span className="text-[var(--faint)]">→</span>{" "}
                <span className="font-semibold text-[var(--strong)]">{l.ltp != null ? formatInr(l.ltp, 2) : "—"}</span>
              </div>
              <Cell v={l.dlt} fmt={fDelta} />
              <Cell v={l.gam} fmt={fGamma} />
              <Cell v={l.the} fmt={fFlow} />
              <Cell v={l.veg} fmt={fFlow} />
              <div className="text-right tabular-nums text-[12.5px] text-[var(--warn-text)]">
                {l.iv != null ? `${(l.iv * 100).toFixed(1)}%` : "—"}
              </div>
              <div className={`text-right tabular-nums whitespace-nowrap font-['Space_Grotesk'] font-semibold text-[13px] ${sign(l.unreal)}`}>
                {formatInr(l.unreal)}
              </div>
            </div>
          ))}

          {/* net row */}
          <div
            className="grid items-center gap-x-2 px-4 py-2 bg-[var(--stat)] border-b border-[var(--divider)]"
            style={{ gridTemplateColumns: COLS }}
          >
            <div className="font-['Space_Grotesk'] font-bold text-[13px] text-[var(--strong)]">Net · {legs.length} legs</div>
            <div />
            <div />
            <Cell v={net.dlt} fmt={fDelta} />
            <Cell v={net.gam} fmt={fGamma} />
            <Cell v={net.the} fmt={fFlow} />
            <Cell v={net.veg} fmt={fFlow} />
            <div />
            <div className={`text-right tabular-nums whitespace-nowrap font-['Space_Grotesk'] font-bold text-[13px] ${sign(net.unreal)}`}>
              {formatInr(net.unreal)}
            </div>
          </div>
        </div>
      </div>

      {/* net-greeks strip */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-2 p-3">
        <Tile
          label={`Net Δ · ${unitTag}`}
          value={fDelta(net.dlt)}
          sub={deltaCash != null ? `Δ-cash ${sInr(deltaCash)} · position` : "Δ-cash —"}
          tone={net.dlt}
        />
        <Tile
          label={`Net Γ · ${unitTag}`}
          value={mode === "pos" ? sfx(net.gam * 100, 2) : sfx(net.gam, 6)}
          sub="Δ shift per 100-pt move"
          tone={net.gam}
        />
        <Tile
          label={`Net Θ/day · ${unitTag}`}
          value={fFlow(net.the)}
          sub="decay earned while flat"
          tone={net.the}
        />
        <Tile
          label={`Net Vega/1% IV · ${unitTag}`}
          value={fFlow(net.veg)}
          sub="loses if IV rises"
          tone={net.veg}
        />
        <Tile
          label="IV · book avg"
          value={ivBook != null ? `${(ivBook * 100).toFixed(1)}%` : "—"}
          sub="premium-weighted"
          tone={null}
        />
      </div>
    </div>
  );
}
