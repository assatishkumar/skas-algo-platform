import { useEffect, useState } from "react";
import { portfolio as papi } from "../../api/client";
import {
  dayLabel, exactMoney, money, signedMoney,
  type Holding, type TagRow, type TransactionInput, type TransactionRow,
} from "../../lib/portfolio";
import TagPicker from "./TagPicker";
import { ConfirmAction, inputClass, Modal, Notice, Segments } from "./primitives";

interface ParsePreview {
  rows: { on_date: string; kind: "buy" | "sell"; units: number; price: number; fees: number }[];
  errors: string[];
  summary: {
    rows: number; buys: number; sells: number;
    earliest: string | null; latest: string | null;
  };
}

export default function LedgerModal({
  holding, tags, onClose, onChanged,
}: {
  holding: Holding;
  tags: TagRow[];
  onClose: () => void;
  onChanged: () => void;
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
                    style={{ color: t.kind === "buy" ? "var(--accent-deep)" : "var(--note)" }}
                  >
                    {t.kind.toUpperCase()}
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
          {([
            ["DATE", "date", "on_date"],
            ["UNITS", "number", "units"],
            ["PRICE (₹)", "number", "price"],
            ["FEES (₹)", "number", "fees"],
          ] as const).map(([label, type, key]) => (
            <label key={key} className={key === "on_date" ? "col-span-2" : ""}>
              <span className="text-[11px] font-extrabold tracking-[.04em] text-[var(--faint)]">
                {label}
              </span>
              <input
                type={type}
                className={`${inputClass} mt-[5px]`}
                value={String(entry[key] ?? "")}
                onChange={(e) =>
                  setEntry((s) => ({
                    ...s,
                    [key]: type === "number" ? Number(e.target.value) : e.target.value,
                  }))}
              />
            </label>
          ))}
          <label>
            <span className="text-[11px] font-extrabold tracking-[.04em] text-[var(--faint)]">
              TYPE
            </span>
            <div className="mt-[5px]">
              <Segments
                size="sm" value={entry.kind}
                onChange={(v) => setEntry((s) => ({ ...s, kind: v }))}
                options={[{ value: "buy", label: "Buy" }, { value: "sell", label: "Sell" }]}
              />
            </div>
          </label>
          <div className="col-span-5 flex justify-end">
            <button
              disabled={busy || !(entry.units > 0)}
              onClick={() => act(async () => {
                await papi.addTransaction(holding.id, entry);
                setEntry((s) => ({ ...s, units: 0, price: 0, fees: 0 }));
                setTab("list");
              })}
              className="rounded-[11px] bg-[var(--accent)] px-[22px] py-2.5 text-[13px] font-extrabold text-white disabled:opacity-50"
            >
              Add transaction
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
    </Modal>
  );
}
