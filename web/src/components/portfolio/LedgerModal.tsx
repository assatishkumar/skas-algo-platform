import { useEffect, useState } from "react";
import { portfolio as papi } from "../../api/client";
import {
  dayLabel, exactMoney, money, signedMoney,
  type Holding, type TagRow, type TransactionInput, type TransactionRow,
} from "../../lib/portfolio";
import TagPicker from "./TagPicker";
import { ConfirmAction, inputClass, Modal, Notice, Segments } from "./primitives";

interface ParsePreview {
  rows: TransactionInput[];
  errors: string[];
  summary: {
    rows: number; buys: number; sells: number;
    earliest: string | null; latest: string | null;
  };
}

export default function LedgerModal({
  holding, tags, onClose, onChanged, onEdit, onDelete,
}: {
  holding: Holding;
  tags: TagRow[];
  onClose: () => void;
  onChanged: () => void;
  /** Edit/Delete moved here from the table's Actions column (owner, 2026-09-02) — this
   * modal is what a click on a holding opens, so it is where the holding is managed. */
  onEdit?: () => void;
  onDelete?: () => void;
}) {
  const [tab, setTab] = useState<"list" | "add" | "import">("list");
  const [rows, setRows] = useState<TransactionRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const [entry, setEntry] = useState<TransactionInput>({
    on_date: new Date().toISOString().slice(0, 10),
    kind: "buy", units: 0, price: 0, fees: 0,
  });
  // A dollar-quoted holding takes its price in DOLLARS by default: the ledger is INR, so the
  // form converts at USD/INR on the TRADE date and keeps the dollar figure in the note.
  // Typing rupees by hand invited the 2026-11-21 near-miss — "474" in a ₹ box for a $474 buy.
  const foreign = !!holding.native_currency && holding.native_currency !== "INR";
  const [ccy, setCcy] = useState<"native" | "inr">(foreign ? "native" : "inr");
  const inNative = foreign && ccy === "native" && entry.kind !== "bonus";
  const [fx, setFx] = useState<{ rate: number; as_of: string } | null>(null);
  const [fxError, setFxError] = useState("");
  // A split is entered as its RATIO (3 for 1 → 3 : 1); the extra units are derived from the
  // units held on that date, so nobody does the multiplication by hand.
  const [ratio, setRatio] = useState<{ num: number; den: number }>({ num: 2, den: 1 });
  const [paste, setPaste] = useState("");
  const [replace, setReplace] = useState(true);
  const [preview, setPreview] = useState<ParsePreview | null>(null);
  const [parsing, setParsing] = useState(false);

  const reload = async () => {
    setLoading(true);
    try {
      setRows((await papi.transactions(holding.id)).transactions);
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { void reload(); /* eslint-disable-next-line */ }, [holding.id]);

  useEffect(() => {
    if (!inNative || !entry.on_date) { setFx(null); setFxError(""); return; }
    let live = true;
    setFx(null);
    setFxError("");
    const t = window.setTimeout(async () => {
      try {
        const r = await papi.usdInrOn(entry.on_date);
        if (live) setFx(r);
      } catch (e) {
        if (live) setFxError(e instanceof Error ? e.message : String(e));
      }
    }, 250);
    return () => { live = false; window.clearTimeout(t); };
  }, [inNative, entry.on_date]);

  // Units held at the END of the split date — the rows the ledger would apply before it.
  const heldOn = (day: string) =>
    rows.reduce((u, r) => (r.on_date > day ? u
      : r.kind === "sell" ? u - r.units : u + r.units), 0);
  const splitHeld = heldOn(entry.on_date);
  const splitAdds = ratio.num > ratio.den && ratio.den > 0
    ? +(splitHeld * (ratio.num / ratio.den - 1)).toFixed(6) : 0;
  const sym = holding.native_currency === "USD" ? "$" : `${holding.native_currency ?? ""} `;

  const submitEntry = async () => {
    let body: TransactionInput = entry;
    if (entry.kind === "bonus") {
      body = {
        on_date: entry.on_date, kind: "bonus", units: splitAdds, price: 0, fees: 0,
        note: `${ratio.num}:${ratio.den} split — ${splitHeld} held → ${+(splitHeld + splitAdds).toFixed(6)}`,
      };
    } else if (inNative && fx) {
      body = {
        ...entry,
        price: +(entry.price * fx.rate).toFixed(4),
        fees: +(entry.fees * fx.rate).toFixed(2),
        note: `${sym}${entry.price}${entry.fees ? ` + ${sym}${entry.fees} fees` : ""}`
          + ` @ USD/INR ${fx.rate.toFixed(4)} (${fx.as_of})`,
      };
    }
    await papi.addTransaction(holding.id, body);
    setEntry((s) => ({ ...s, units: 0, price: 0, fees: 0 }));
    setTab("list");
  };

  // Parsed on the SERVER — the same code that performs the import, so the preview shown
  // here cannot disagree with what actually lands. Debounced because it is a network call.
  useEffect(() => {
    if (!paste.trim()) { setPreview(null); return; }
    let live = true;
    setParsing(true);
    const t = window.setTimeout(async () => {
      try {
        const r = await papi.parsePaste(paste);
        if (live) setPreview(r);
      } catch (e) {
        if (live) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (live) setParsing(false);
      }
    }, 250);
    return () => { live = false; window.clearTimeout(t); };
  }, [paste]);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try {
      await fn();
      await reload();
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title={holding.name} onClose={onClose} width={680}>
      {/* Tags first: this modal is what a click on a holding opens, and labelling is the
          thing most often wanted there. The ledger is below it. */}
      <div className="mb-3.5">
        <TagPicker holding={holding} tags={tags} onChanged={onChanged} />
      </div>

      <div className="mb-3 flex flex-wrap items-center gap-3">
        <Segments
          size="sm" value={tab} onChange={setTab}
          options={[
            { value: "list", label: `Ledger${rows.length ? ` · ${rows.length}` : ""}` },
            { value: "add", label: "Add one" },
            { value: "import", label: "Paste history" },
          ]}
        />
        {holding.basis === "ledger" && (
          <span className="text-[12px] font-bold text-[var(--muted)]">
            {holding.units?.toLocaleString("en-IN")} units · cost {money(holding.invested)}
            {holding.realized !== 0 && (
              <span style={{ color: holding.realized >= 0 ? "var(--pos)" : "var(--danger)" }}>
                {" "}· realized {signedMoney(holding.realized)}
              </span>
            )}
          </span>
        )}
      </div>

      {holding.opening_missing && (
        <div className="mb-3">
          <Notice tone="warn">
            This ledger began without the position you had typed —{" "}
            <b>{(holding.typed_units ?? 0).toLocaleString("en-IN")} units · cost {money(holding.typed_invested ?? 0)}</b>
            {" "}— so the holding shows only the rows below.{" "}
            <button
              disabled={busy}
              onClick={() => act(() => papi.addOpeningBalance(holding.id))}
              className="font-extrabold text-[var(--accent-deep)] underline disabled:opacity-50"
            >
              Carry it in as the opening row
            </button>
            {" "}(or paste the full history with replace, which needs no opening row).
          </Notice>
        </div>
      )}

      {holding.oversold_units > 0 && (
        <div className="mb-3">
          <Notice tone="warn">
            This ledger sells {holding.oversold_units} more units than it ever buys — a buy row
            is missing, or a sell has the wrong quantity. Cost basis and tax below are wrong
            until it's fixed.
          </Notice>
        </div>
      )}

      {error && (
        <div className="mb-3 text-[12px] font-bold text-[var(--danger)]">{error}</div>
      )}

      {tab === "list" && (
        <div>
          {loading ? (
            <div className="py-8 text-center text-[13px] font-semibold text-[var(--faint)]">
              Loading…
            </div>
          ) : rows.length === 0 ? (
            <div className="py-8 text-center text-[13px] font-semibold text-[var(--faint)]">
              No transactions yet. Until there are, this holding's cost and units are the ones
              you typed — add the history for a real XIRR and per-lot tax.
            </div>
          ) : (
            <div className="max-h-[46vh] overflow-y-auto">
              <div className="grid grid-cols-[1fr_.6fr_.8fr_.9fr_.7fr_.6fr] gap-2 border-b border-[var(--border)] px-1 py-2 text-[11px] font-extrabold tracking-[.05em] text-[var(--faint)]">
                <span>DATE</span><span>TYPE</span>
                <span className="text-right">UNITS</span>
                <span className="text-right">PRICE</span>
                <span className="text-right">FEES</span>
                <span className="text-right" />
              </div>
              {rows.map((t) => (
                <div
                  key={t.id}
                  className="grid grid-cols-[1fr_.6fr_.8fr_.9fr_.7fr_.6fr] items-center gap-2 border-b border-[var(--divider)] px-1 py-2 text-[12.5px]"
                >
                  <span className="font-semibold text-[var(--strong)]">{dayLabel(t.on_date)}</span>
                  <span
                    className="text-[11px] font-extrabold"
                    style={{ color: t.kind === "sell" ? "var(--note)" : "var(--accent-deep)" }}
                    title={t.note ?? undefined}
                  >
                    {t.note?.startsWith("opening balance") ? "OPENING"
                      : t.kind === "bonus" ? (t.note?.includes("split") ? "SPLIT" : "BONUS")
                      : t.kind.toUpperCase()}
                  </span>
                  <span className="text-right font-semibold tabular-nums text-[var(--muted)]">
                    {t.units.toLocaleString("en-IN")}
                  </span>
                  <span className="text-right font-semibold tabular-nums text-[var(--muted)]">
                    {exactMoney(t.price)}
                  </span>
                  <span className="text-right font-semibold tabular-nums text-[var(--faint)]">
                    {t.fees ? exactMoney(t.fees) : "—"}
                  </span>
                  <span className="text-right">
                    <ConfirmAction
                      label="Delete"
                      onConfirm={() => act(() => papi.deleteTransaction(t.id))}
                      className="text-[11.5px] font-extrabold text-[var(--faint)] hover:text-[var(--danger)]"
                    />
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {tab === "add" && (
        <div className="grid grid-cols-5 gap-2.5">
          <label className="col-span-2">
            <span className="text-[11px] font-extrabold tracking-[.04em] text-[var(--faint)]">DATE</span>
            <input
              type="date" className={`${inputClass} mt-[5px]`} value={entry.on_date}
              onChange={(e) => setEntry((s) => ({ ...s, on_date: e.target.value }))}
            />
          </label>

          {entry.kind === "bonus" ? (
            <label className="col-span-2">
              <span className="text-[11px] font-extrabold tracking-[.04em] text-[var(--faint)]">
                RATIO (NEW : OLD)
              </span>
              <div className="mt-[5px] flex items-center gap-2">
                <input
                  type="number" min={1} className={inputClass} value={ratio.num}
                  onChange={(e) => setRatio((r) => ({ ...r, num: Number(e.target.value) }))}
                />
                <span className="font-extrabold text-[var(--muted)]">:</span>
                <input
                  type="number" min={1} className={inputClass} value={ratio.den}
                  onChange={(e) => setRatio((r) => ({ ...r, den: Number(e.target.value) }))}
                />
              </div>
            </label>
          ) : (
            ([
              ["UNITS", "units"],
              [`PRICE (${inNative ? sym.trim() : "₹"})`, "price"],
              [`FEES (${inNative ? sym.trim() : "₹"})`, "fees"],
            ] as const).map(([label, key]) => (
              <label key={key}>
                <span className="text-[11px] font-extrabold tracking-[.04em] text-[var(--faint)]">
                  {label}
                </span>
                <input
                  type="number"
                  className={`${inputClass} mt-[5px]`}
                  value={String(entry[key] ?? "")}
                  onChange={(e) => setEntry((s) => ({ ...s, [key]: Number(e.target.value) }))}
                />
              </label>
            ))
          )}

          <div className="col-span-5 flex flex-wrap items-end gap-5">
            <label>
              <span className="text-[11px] font-extrabold tracking-[.04em] text-[var(--faint)]">
                TYPE
              </span>
              <div className="mt-[5px]">
                <Segments
                  size="sm" value={entry.kind}
                  onChange={(v) => setEntry((s) => ({ ...s, kind: v }))}
                  options={[
                    { value: "buy", label: "Buy" },
                    { value: "sell", label: "Sell" },
                    { value: "bonus", label: "Split / bonus" },
                  ]}
                />
              </div>
            </label>
            {foreign && entry.kind !== "bonus" && (
              <label>
                <span className="text-[11px] font-extrabold tracking-[.04em] text-[var(--faint)]">
                  PRICE IN
                </span>
                <div className="mt-[5px]">
                  <Segments
                    size="sm" value={ccy} onChange={setCcy}
                    options={[
                      { value: "native", label: sym.trim() || String(holding.native_currency) },
                      { value: "inr", label: "₹" },
                    ]}
                  />
                </div>
              </label>
            )}
          </div>

          {entry.kind === "bonus" ? (
            <div className="col-span-5 text-[12.5px] font-semibold text-[var(--muted)]">
              {ratio.num > ratio.den && ratio.den > 0 ? (
                <>
                  Held <b>{splitHeld.toLocaleString("en-IN")}</b> on {dayLabel(entry.on_date)} →
                  adds <b>{splitAdds.toLocaleString("en-IN")}</b> units at no cost, for{" "}
                  <b>{(+(splitHeld + splitAdds).toFixed(6)).toLocaleString("en-IN")}</b>. Total
                  cost is unchanged; each open lot's cost per unit is divided by{" "}
                  {+(ratio.num / ratio.den).toFixed(4)}.
                </>
              ) : (
                <span className="text-[var(--danger)]">
                  The new count must be larger than the old — a reverse split can't be recorded here.
                </span>
              )}
            </div>
          ) : inNative && (
            <div className="col-span-5 text-[12.5px] font-semibold text-[var(--muted)]">
              {fxError ? (
                <span className="text-[var(--danger)]">{fxError} — switch to ₹ to enter it by hand.</span>
              ) : !fx ? (
                "Reading USD/INR for that date…"
              ) : (
                <>
                  {sym}{entry.price || 0} × USD/INR {fx.rate.toFixed(2)}
                  {fx.as_of !== entry.on_date && <> (close of {dayLabel(fx.as_of)})</>} ={" "}
                  <b>{exactMoney(entry.price * fx.rate)}</b> a unit
                  {entry.fees ? <> · fees {exactMoney(entry.fees * fx.rate)}</> : null}. The
                  ledger records rupees; the {sym.trim()} price is kept in the row's note.
                </>
              )}
            </div>
          )}

          <div className="col-span-5 flex justify-end">
            <button
              disabled={
                busy
                || (entry.kind === "bonus" ? !(splitAdds > 0) : !(entry.units > 0))
                || (inNative && !fx)
              }
              onClick={() => act(submitEntry)}
              className="rounded-[11px] bg-[var(--accent)] px-[22px] py-2.5 text-[13px] font-extrabold text-white disabled:opacity-50"
            >
              {entry.kind === "bonus" ? "Add split" : "Add transaction"}
            </button>
          </div>
        </div>
      )}

      {tab === "import" && (
        <div>
          <div className="mb-2 text-[12px] font-semibold text-[var(--muted)]">
            One row per trade: <code>date, buy/sell, units, price, fees</code>. Tab, comma or
            two-space separated — paste straight from a spreadsheet or a broker export. Dates
            read day-first (10/06/2024 is 10 June).
          </div>
          <textarea
            value={paste}
            onChange={(e) => setPaste(e.target.value)}
            rows={8}
            placeholder={"2023-01-10\tbuy\t100\t100.00\t12.50\n10-Jun-2024\tbuy\t100\t200\n10/01/2025\tsell\t120\t250"}
            className="w-full rounded-[10px] border-[1.5px] border-[var(--field-border)] bg-[var(--field)] p-3 font-mono text-[12px] text-[var(--strong)] outline-none focus:border-[var(--accent)]"
          />

          {parsing && (
            <div className="mt-2 text-[12px] font-semibold text-[var(--faint)]">Reading…</div>
          )}

          {preview && preview.errors.length > 0 && (
            <div className="mt-2">
              <Notice tone="warn">
                {preview.errors.length} line{preview.errors.length === 1 ? "" : "s"} can't be
                read. Nothing imports until they're fixed — a partial import that looks
                complete would leave the cost basis wrong by an amount nothing on screen can
                show you:
                <ul className="mt-1 list-disc pl-5 font-semibold">
                  {preview.errors.slice(0, 6).map((e) => <li key={e}>{e}</li>)}
                </ul>
              </Notice>
            </div>
          )}

          {preview && preview.summary.rows > 0 && (
            <div className="mt-2 text-[12px] font-bold text-[var(--muted)]">
              {preview.summary.rows} row{preview.summary.rows === 1 ? "" : "s"} ready ·{" "}
              {preview.summary.buys} buys, {preview.summary.sells} sells
              {preview.summary.earliest && (
                <> · {dayLabel(preview.summary.earliest)} → {dayLabel(preview.summary.latest)}</>
              )}
            </div>
          )}

          <label className="mt-3 flex items-center gap-2 text-[12.5px] font-semibold text-[var(--muted)]">
            <input
              type="checkbox" checked={replace} onChange={(e) => setReplace(e.target.checked)}
            />
            Replace the existing {rows.length} transaction{rows.length === 1 ? "" : "s"} —
            leave this on when re-importing a corrected export, or every buy is counted twice.
          </label>

          <div className="mt-3 flex justify-end">
            <button
              disabled={
                busy || parsing || !preview
                || preview.summary.rows === 0 || preview.errors.length > 0
              }
              onClick={() => act(async () => {
                if (!preview) return;
                await papi.importTransactions(holding.id, preview.rows, replace);
                setPaste("");
                setPreview(null);
                setTab("list");
              })}
              className="rounded-[11px] bg-[var(--accent)] px-[22px] py-2.5 text-[13px] font-extrabold text-white disabled:opacity-50"
            >
              Import {preview?.summary.rows || ""} row
              {preview?.summary.rows === 1 ? "" : "s"}
            </button>
          </div>
        </div>
      )}

      {(onEdit || onDelete) && (
        <div className="mt-4 flex items-center justify-end gap-4 border-t border-[var(--divider)] pt-3 text-[12.5px] font-extrabold">
          {onEdit && (
            <button onClick={onEdit} className="text-[var(--accent-deep)]">
              Edit holding
            </button>
          )}
          {onDelete && (
            <ConfirmAction
              label="Delete holding"
              onConfirm={onDelete}
              className="text-[var(--faint)] hover:text-[var(--danger)]"
            />
          )}
        </div>
      )}
    </Modal>
  );
}
