import { useEffect, useState } from "react";
import { portfolio as papi } from "../../api/client";
import {
  dayLabel, money, signedMoney,
  type AssetClassKey, type LedgerPreview, type PortfolioPayload, type SeedResult,
} from "../../lib/portfolio";
import { Modal, Notice, Segments, selectClass } from "./primitives";

interface BrokerOption { id: number; label: string; broker: string }

const PLACEHOLDER = `Stock / ETF code\tBuy / Sell Date\tBuy Price\tBought Units\t…\tUnits Sold\tSell Price
NSE:ITC\t26-May-25\t442.85\t113\t…
NSE:ITC\t23-Oct-25\t\t\t…\t6\t3,773.80`;

export default function SeedModal({
  payload, brokers, onClose, onSeeded,
}: {
  payload: PortfolioPayload;
  brokers: BrokerOption[];
  onClose: () => void;
  onSeeded: () => void;
}) {
  const [text, setText] = useState("");
  const [preview, setPreview] = useState<LedgerPreview | null>(null);
  const [classes, setClasses] = useState<Record<string, AssetClassKey>>({});
  const [parsing, setParsing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<SeedResult | null>(null);
  const [accountId, setAccountId] = useState<number | null>(brokers[0]?.id ?? null);
  const [replace, setReplace] = useState(true);

  // Parsed on the server, so this preview is exactly what a seed would create.
  useEffect(() => {
    if (!text.trim()) { setPreview(null); return; }
    let live = true;
    setParsing(true);
    const t = window.setTimeout(async () => {
      try {
        const r = await papi.parseLedger(text);
        if (!live) return;
        setPreview(r);
        // Seed the per-symbol class pickers from the server's guess, keeping any the owner
        // has already corrected.
        setClasses((prev) => {
          const next = { ...prev };
          for (const s of r.symbols) if (!next[s.symbol]) next[s.symbol] = s.asset_class;
          return next;
        });
      } catch (e) {
        if (live) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (live) setParsing(false);
      }
    }, 350);
    return () => { live = false; window.clearTimeout(t); };
  }, [text]);

  // Blocked only by a GLOBAL failure or by there being nothing clean to seed. A symbol with
  // unreadable rows is simply left out — holding eleven good symbols hostage to one bad
  // NIFTYBEES line is what made this button permanently dead on a 396-trade paste.
  const seedable = preview?.symbols.filter((s) => s.seedable) ?? [];
  const blocked = !preview || preview.errors.length > 0 || seedable.length === 0;

  if (result) {
    return (
      <Modal title="Seeded" onClose={onClose} width={760}>
        <Notice>
          {result.summary.trades} transactions loaded across {result.summary.holdings}{" "}
          holdings ({result.summary.created} newly created).
        </Notice>
        <div className="mt-3 overflow-x-auto">
          <div className="min-w-[560px]">
            <div className="grid grid-cols-[1.4fr_.7fr_.9fr_1.1fr_1.1fr] gap-2 border-b border-[var(--border)] px-1 py-2 text-[11px] font-extrabold tracking-[.05em] text-[var(--faint)]">
              <span>HOLDING</span><span className="text-right">TRADES</span>
              <span className="text-right">UNITS</span><span className="text-right">COST</span>
              <span className="text-right">REALIZED</span>
            </div>
            {result.seeded.map((r) => (
              <div
                key={r.symbol}
                className="grid grid-cols-[1.4fr_.7fr_.9fr_1.1fr_1.1fr] items-center gap-2 border-b border-[var(--divider)] px-1 py-2 text-[12.5px]"
              >
                <span className="font-extrabold text-[var(--strong)]">
                  {r.symbol}
                  {r.created && (
                    <span className="ml-1.5 text-[10px] font-bold text-[var(--accent-deep)]">NEW</span>
                  )}
                </span>
                <span className="text-right font-semibold tabular-nums text-[var(--muted)]">
                  {r.trades}
                </span>
                <span className="text-right font-semibold tabular-nums text-[var(--muted)]">
                  {r.units?.toLocaleString("en-IN") ?? "—"}
                </span>
                <span className="text-right font-bold tabular-nums text-[var(--strong)]">
                  {money(r.invested ?? 0)}
                </span>
                <span
                  className="text-right font-bold tabular-nums"
                  style={{ color: (r.realized ?? 0) >= 0 ? "var(--pos)" : "var(--danger)" }}
                >
                  {signedMoney(r.realized ?? 0)}
                </span>
              </div>
            ))}
          </div>
        </div>
        <div className="mt-3 text-[11.5px] font-semibold text-[var(--faint)]">
          Realized profit is computed FIFO from the trades. Your sheet's "Booked Profit" column
          is gross proceeds (units × sell price), not profit — so these are new numbers.
        </div>
        <div className="mt-4 flex justify-end">
          <button
            onClick={() => { onSeeded(); onClose(); }}
            className="rounded-[11px] bg-[var(--accent)] px-[22px] py-2.5 text-[13px] font-extrabold text-white"
          >
            Done
          </button>
        </div>
      </Modal>
    );
  }

  return (
    <Modal title="Seed holdings from your tracking sheet" onClose={onClose} width={860}>
      <div className="mb-2 text-[12.5px] font-semibold text-[var(--muted)]">
        Paste the rows straight out of the spreadsheet — buys and sells in their own columns,
        as many symbols as you like. <strong>Copy the cells themselves</strong>: the tab
        characters are what mark the empty columns that tell a buy row from a sell row.
      </div>

      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={7}
        placeholder={PLACEHOLDER}
        className="w-full rounded-[10px] border-[1.5px] border-[var(--field-border)] bg-[var(--field)] p-3 font-mono text-[11.5px] text-[var(--strong)] outline-none focus:border-[var(--accent)]"
      />

      {parsing && (
        <div className="mt-2 text-[12px] font-semibold text-[var(--faint)]">Reading…</div>
      )}
      {error && <div className="mt-2 text-[12px] font-bold text-[var(--danger)]">{error}</div>}

      {preview && preview.errors.length > 0 && (
        <div className="mt-2">
          <Notice tone="warn">
            Nothing can be read from this paste:
            <ul className="mt-1 list-disc pl-5 font-semibold">
              {preview.errors.slice(0, 4).map((e) => <li key={e.line}>{e.message}</li>)}
            </ul>
          </Notice>
        </div>
      )}

      {preview && preview.symbols.some((s) => !s.seedable) && (
        <div className="mt-2">
          <Notice tone="warn">
            {preview.symbols.filter((s) => !s.seedable).length} symbol
            {preview.symbols.filter((s) => !s.seedable).length === 1 ? "" : "s"} will be SKIPPED
            — a partial history inside one holding gives it a cost basis that is wrong forever
            and looks perfectly reasonable. Everything else still seeds.
            <ul className="mt-1 list-disc pl-5 font-semibold">
              {preview.symbols.filter((s) => !s.seedable).flatMap((s) =>
                s.errors.slice(0, 2).map((e) => (
                  <li key={`${s.symbol}-${e.line}`}>
                    <strong>{s.symbol}</strong> line {e.line} — {e.message}
                  </li>
                )))}
            </ul>
          </Notice>
        </div>
      )}

      {preview && preview.warnings.length > 0 && (
        <div className="mt-2">
          <Notice tone="warn">
            These don't block the import, but one number in each row is mistyped — the quantity
            and price are used, the stated total is only a check:
            <ul className="mt-1 list-disc pl-5 font-semibold">
              {preview.warnings.slice(0, 5).map((w) => <li key={w}>{w}</li>)}
            </ul>
          </Notice>
        </div>
      )}

      {preview && preview.symbols.length > 0 && (
        <div className="mt-3">
          <div className="mb-1.5 text-[12px] font-bold text-[var(--muted)]">
            {preview.summary.trades} trades across {preview.summary.symbols} symbols
            {preview.summary.seedable < preview.summary.symbols && (
              <> · <span className="text-[var(--warn-text)]">
                {preview.summary.seedable} will be seeded
              </span></>
            )}
            {" "}— these are the positions the seed would produce. Check them before writing
            anything.
          </div>
          <div className="max-h-[34vh] overflow-y-auto">
            <div className="min-w-[640px]">
              <div className="grid grid-cols-[1.3fr_1fr_.6fr_.9fr_1.1fr_1.1fr] gap-2 border-b border-[var(--border)] px-1 py-2 text-[11px] font-extrabold tracking-[.05em] text-[var(--faint)]">
                <span>SYMBOL</span><span>TYPE</span>
                <span className="text-right">TRADES</span><span className="text-right">UNITS</span>
                <span className="text-right">COST</span><span className="text-right">REALIZED</span>
              </div>
              {preview.symbols.map((s) => (
                <div
                  key={s.symbol}
                  className="grid grid-cols-[1.3fr_1fr_.6fr_.9fr_1.1fr_1.1fr] items-center gap-2 border-b border-[var(--divider)] px-1 py-1.5 text-[12.5px]"
                  style={{ opacity: s.seedable ? 1 : 0.45 }}
                >
                  <span className="truncate">
                    <span className="font-extrabold text-[var(--strong)]">{s.symbol}</span>
                    <span className="block text-[10.5px] font-semibold text-[var(--faint)]">
                      {!s.seedable ? (
                        <span className="text-[var(--danger)]">skipped — unreadable rows</span>
                      ) : (
                        <>{dayLabel(s.first_trade)} → {dayLabel(s.last_trade)}</>
                      )}
                      {s.actions.map((a) => (
                        <span
                          key={a.on_date}
                          className="ml-1"
                          style={{ color: a.problem ? "var(--danger)" : "var(--accent-deep)" }}
                          title={
                            a.problem
                              ? `Free units on ${a.on_date} with nothing held — ${a.problem}. Not applied.`
                              : `${a.on_date}: ${a.units_before} units became ${a.units_after}. `
                                + "Existing lots are re-based — cost per unit divided by the same "
                                + "factor — so total cost, FIFO order and holding periods are all "
                                + "preserved."
                          }
                        >
                          · {a.ratio ? `1:${Number(a.ratio.toFixed(4))} split` : "bad split"}
                        </span>
                      ))}
                      {s.oversold_units > 0 && (
                        <span className="ml-1 text-[var(--danger)]">
                          · sells exceed buys by {s.oversold_units}
                        </span>
                      )}
                    </span>
                  </span>
                  <span>
                    <select
                      value={classes[s.symbol] ?? s.asset_class}
                      onChange={(e) =>
                        setClasses((c) => ({ ...c, [s.symbol]: e.target.value as AssetClassKey }))}
                      className={`${selectClass} !py-1 !text-[11.5px]`}
                    >
                      {(Object.keys(payload.asset_classes) as AssetClassKey[]).map((k) => (
                        <option key={k} value={k}>{payload.asset_classes[k].label}</option>
                      ))}
                    </select>
                  </span>
                  <span className="text-right font-semibold tabular-nums text-[var(--muted)]">
                    {s.trades}
                  </span>
                  <span className="text-right font-semibold tabular-nums text-[var(--muted)]">
                    {s.units.toLocaleString("en-IN")}
                  </span>
                  <span className="text-right font-bold tabular-nums text-[var(--strong)]">
                    {money(s.invested)}
                  </span>
                  <span
                    className="text-right font-bold tabular-nums"
                    style={{ color: s.realized >= 0 ? "var(--pos)" : "var(--danger)" }}
                  >
                    {s.realized === 0 ? "—" : signedMoney(s.realized)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-3">
        <span className="text-[11px] font-extrabold tracking-[.04em] text-[var(--faint)]">
          PRICING
        </span>
        <Segments
          size="sm"
          value={accountId === null ? "manual" : "auto"}
          onChange={(v) => setAccountId(v === "manual" ? null : brokers[0]?.id ?? null)}
          options={[
            { value: "auto", label: "Live from broker" },
            { value: "manual", label: "Enter by hand" },
          ]}
        />
        {accountId !== null && brokers.length > 0 && (
          <select
            value={accountId}
            onChange={(e) => setAccountId(Number(e.target.value))}
            className={`${selectClass} !w-auto !py-1.5`}
          >
            {brokers.map((b) => (
              <option key={b.id} value={b.id}>{b.label} · {b.broker}</option>
            ))}
          </select>
        )}
        {accountId !== null && brokers.length === 0 && (
          <span className="text-[11.5px] font-semibold text-[var(--warn-text)]">
            No broker account connected — these stay manual until you add one.
          </span>
        )}
      </div>

      <label className="mt-2.5 flex items-center gap-2 text-[12.5px] font-semibold text-[var(--muted)]">
        <input type="checkbox" checked={replace} onChange={(e) => setReplace(e.target.checked)} />
        Replace any existing transactions on a matching holding — leave this on when re-pasting
        a corrected sheet, or every buy is counted twice.
      </label>

      <div className="mt-4 flex justify-end gap-2.5">
        <button
          onClick={onClose}
          className="rounded-[11px] border-[1.5px] border-[var(--field-border)] bg-[var(--ghost)] px-[18px] py-2.5 text-[13px] font-extrabold text-[var(--muted)]"
        >
          Cancel
        </button>
        <button
          disabled={busy || parsing || blocked}
          onClick={async () => {
            if (!preview) return;
            setBusy(true);
            setError("");
            try {
              setResult(await papi.seed({
                text,
                symbols: seedable.map((s) => ({
                  symbol: s.symbol,
                  name: s.symbol,
                  asset_class: classes[s.symbol] ?? s.asset_class,
                })),
                broker_account_id: accountId,
                sync: accountId === null ? "manual" : "auto",
                replace,
              }));
            } catch (e) {
              setError(e instanceof Error ? e.message : String(e));
            } finally {
              setBusy(false);
            }
          }}
          className="rounded-[11px] bg-[var(--accent)] px-[22px] py-2.5 text-[13px] font-extrabold text-white disabled:opacity-50"
        >
          {busy ? "Seeding…" : `Seed ${seedable.length || ""} holdings`}
        </button>
      </div>
    </Modal>
  );
}
