import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { brokers } from "../api/client";
import { formatInr } from "../lib/format";
import { formatOptionSymbol } from "../lib/symbol";
import type { BrokerAccount, BrokerBook, BrokerBookRow } from "../types";

const _stamp = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Kolkata", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});

const STATUS: Record<BrokerBookRow["status"], { label: string; color: string; bg: string }> = {
  match: { label: "matches", color: "var(--pos)", bg: "var(--pos-fill)" },
  mismatch: { label: "MISMATCH", color: "var(--danger)", bg: "var(--neg-fill)" },
  platform_only: { label: "platform only", color: "var(--warn-text)", bg: "var(--warn-bg)" },
  broker_only: { label: "broker only", color: "var(--warn-text)", bg: "var(--warn-bg)" },
};

const qty = (n: number) => (n > 0 ? `+${n.toLocaleString("en-IN")}` : n.toLocaleString("en-IN"));

/** The broker's net book beside the platform's, per contract, with the runs behind each line.
 *  A read-only view over the SAME reconciliation the hourly halt runs — it cannot disagree
 *  with it. Quantities are the truth here; no P&L is subtracted, because the broker's P&L is
 *  day-based (overnight F&O rebased to the previous close) and ours is since entry. */
export default function BrokerBookCard({ accounts }: { accounts: BrokerAccount[] }) {
  const withSession = useMemo(() => accounts.filter((a) => a.has_session), [accounts]);
  const [accountId, setAccountId] = useState<number | null>(null);
  const [fnoOnly, setFnoOnly] = useState(true);
  useEffect(() => {
    if (accountId != null && withSession.some((a) => a.id === accountId)) return;
    const pick = withSession.find((a) => a.broker === "zerodha") ?? withSession[0] ?? null;
    setAccountId(pick ? pick.id : null);
  }, [withSession, accountId]);

  const { data, isFetching, refetch } = useQuery({
    queryKey: ["broker-book", accountId],
    queryFn: () => brokers.book(accountId as number),
    enabled: accountId != null,
    refetchInterval: 60000,
    staleTime: 30000,
  });

  const rows = useMemo(
    () => (data?.rows ?? []).filter((r) => !fnoOnly || r.segment === "fno"),
    [data, fnoOnly],
  );
  const hiddenEquity = (data?.rows ?? []).length - rows.length;

  return (
    <div className="mt-6 rounded-[18px] border border-[var(--border)] bg-[var(--card)] p-5">
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-0">
          <div className="font-['Space_Grotesk'] font-bold text-[16px] text-[var(--strong)]">Broker book · net positions</div>
          <div className="text-[12px] text-[var(--muted)]">
            What the broker holds beside what the live runs hold, per contract — the view behind the hourly reconciliation.
          </div>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <select
            className="rounded-[10px] bg-[var(--field)] border border-[var(--field-border)] px-2.5 py-1.5 text-[12.5px] text-[var(--strong)]"
            value={accountId ?? ""}
            onChange={(e) => setAccountId(Number(e.target.value))}
          >
            {accountId == null && <option value="">No logged-in session</option>}
            {accounts.map((a) => (
              <option key={a.id} value={a.id} disabled={!a.has_session}>
                {a.label}{a.has_session ? "" : " · no session"}
              </option>
            ))}
          </select>
          <button
            className="rounded-[10px] border border-[var(--border)] bg-[var(--field)] px-3 py-1.5 text-[12.5px] font-semibold text-[var(--strong)] disabled:opacity-50"
            onClick={() => refetch()}
            disabled={accountId == null || isFetching}
            title="Re-read the broker's positions now"
          >
            {isFetching ? "Reading…" : "Refresh"}
          </button>
          <label className="flex items-center gap-1.5 text-[12px] text-[var(--muted)]">
            <input type="checkbox" checked={fnoOnly} onChange={(e) => setFnoOnly(e.target.checked)} />
            F&O only
          </label>
        </div>
      </div>

      {accountId == null ? (
        <div className="mt-4 text-[13px] text-[var(--faint)]">No account has a live session. Log in above to read a book.</div>
      ) : !data ? (
        <div className="mt-4 text-[13px] text-[var(--faint)]">Reading the broker's positions…</div>
      ) : !data.ok ? (
        /* A failed read must SAY so — an empty table reads as "flat", which is the same false
           comfort as the old 04:50 phantom halt, in the other direction. */
        <div className="mt-4 rounded-[12px] px-4 py-3 text-[13px]" style={{ background: "var(--neg-fill)", color: "var(--danger)" }}>
          <b>Broker book could not be read.</b> {data.error}
          <div className="mt-1 text-[12px] opacity-80">Nothing below is known. Off-hours the session token is often dead — log in again and refresh.</div>
        </div>
      ) : (
        <Book data={data} rows={rows} hiddenEquity={hiddenEquity} />
      )}
    </div>
  );
}

function Book({ data, rows, hiddenEquity }: { data: BrokerBook; rows: BrokerBookRow[]; hiddenEquity: number }) {
  const t = data.totals;
  return (
    <>
      {data.mismatch ? (
        <div className="mt-4 rounded-[12px] px-4 py-3 text-[13px]" style={{ background: "var(--neg-fill)", color: "var(--danger)" }}>
          <b>The books disagree</b> — {data.mismatch}. This is what halts a run at the next hourly check.
        </div>
      ) : (
        <div className="mt-4 rounded-[12px] px-4 py-2.5 text-[13px]" style={{ background: "var(--pos-fill)", color: "var(--pos)" }}>
          <b>Books agree.</b> Every contract the live runs hold nets to the broker's figure.
        </div>
      )}

      <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-[12px] tabular-nums text-[var(--muted)]">
        <span><b className="text-[var(--strong)]">{rows.length}</b> contract{rows.length === 1 ? "" : "s"}{hiddenEquity > 0 ? ` · ${hiddenEquity} equity hidden` : ""}</span>
        {t && <span><b style={{ color: t.mismatches ? "var(--danger)" : "var(--strong)" }}>{t.mismatches}</b> mismatch{t.mismatches === 1 ? "" : "es"}</span>}
        {t && t.booked_at_broker !== 0 && (
          <span title="Runs on both sides of one contract: the broker has already netted the matched quantity and booked it — realised on its side, still open on ours">
            already booked at broker <b style={{ color: t.booked_at_broker >= 0 ? "var(--pos)" : "var(--danger)" }}>{formatInr(t.booked_at_broker)}</b>
          </span>
        )}
        {data.as_of && <span>as of {_stamp.format(new Date(data.as_of))}</span>}
      </div>

      {rows.length === 0 ? (
        <div className="mt-4 text-[13px] text-[var(--faint)]">
          Nothing on either side{hiddenEquity > 0 ? " in F&O" : ""}: the live runs hold no contracts on this account and the broker reports none.
        </div>
      ) : (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-[12.5px]">
            <thead>
              <tr className="text-[10.5px] uppercase tracking-wide text-[var(--faint)]">
                <th className="text-left font-semibold pb-2">Contract</th>
                <th className="text-right font-semibold pb-2">Platform</th>
                <th className="text-right font-semibold pb-2">Broker</th>
                <th className="text-right font-semibold pb-2">Diff</th>
                <th className="text-left font-semibold pb-2 pl-3">Status</th>
                <th className="text-right font-semibold pb-2">Booked at broker</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const st = STATUS[r.status];
                return (
                  <tr key={r.tradingsymbol} className="border-t border-[var(--divider)] align-top">
                    <td className="py-2 pr-3">
                      <div className="font-semibold text-[var(--strong)]">{r.symbol ? formatOptionSymbol(r.symbol) : r.tradingsymbol}</div>
                      <div className="text-[11px] text-[var(--faint)] font-mono">{r.tradingsymbol}</div>
                      {r.lots.length > 0 && (
                        <div className="mt-1 space-y-0.5">
                          {r.lots.map((lot, i) => (
                            <div key={i} className="text-[11.5px] text-[var(--muted)] tabular-nums">
                              <span className="font-semibold" style={{ color: lot.direction > 0 ? "var(--pos)" : "var(--danger)" }}>
                                {lot.direction > 0 ? "LONG" : "SHORT"} {lot.units.toLocaleString("en-IN")}
                              </span>
                              {lot.price != null ? ` @ ${lot.price.toFixed(2)}` : ""}
                              {" · "}{lot.name ?? `run ${lot.run_id}`}{lot.run_id != null ? ` #${lot.run_id}` : ""}
                            </div>
                          ))}
                        </div>
                      )}
                    </td>
                    <td className="py-2 text-right tabular-nums text-[var(--strong)]">{qty(r.platform_net)}</td>
                    <td className="py-2 text-right tabular-nums text-[var(--strong)]">{qty(r.broker_net)}</td>
                    <td className="py-2 text-right tabular-nums font-semibold" style={{ color: r.diff === 0 ? "var(--faint)" : "var(--danger)" }}>
                      {r.diff === 0 ? "0" : qty(r.diff)}
                    </td>
                    <td className="py-2 pl-3">
                      <span className="inline-block rounded-full px-2 py-0.5 text-[10.5px] font-bold" style={{ color: st.color, background: st.bg }}>{st.label}</span>
                    </td>
                    <td className="py-2 text-right tabular-nums">
                      {r.booked_at_broker ? (
                        <div title={`${r.booked_at_broker.matched_units.toLocaleString("en-IN")} units matched: short @ ${r.booked_at_broker.avg_short.toFixed(2)} against long @ ${r.booked_at_broker.avg_long.toFixed(2)}`}>
                          <div className="font-semibold" style={{ color: r.booked_at_broker.amount >= 0 ? "var(--pos)" : "var(--danger)" }}>{formatInr(r.booked_at_broker.amount)}</div>
                          <div className="text-[10.5px] text-[var(--faint)]">{r.booked_at_broker.matched_units.toLocaleString("en-IN")} matched</div>
                        </div>
                      ) : <span className="text-[var(--faint)]">—</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <div className="mt-3 space-y-1 text-[11.5px] text-[var(--faint)]">
        {(data.runs_counted?.length ?? 0) > 0 && (
          <div>Counted: {data.runs_counted!.map((r) => `${r.name ?? "run"} #${r.run_id}`).join(", ")} — live runs placing real orders on this account. Paper runs are excluded; they mirror these positions and place nothing.</div>
        )}
        {(data.runs_skipped?.length ?? 0) > 0 && (
          <div style={{ color: "var(--warn-text)" }}>
            Not counted: {data.runs_skipped!.map((r) => `${r.name ?? "run"} #${r.run_id} (${r.reason})`).join(", ")} — the broker is not managing these books right now; their contracts show as broker-only.
          </div>
        )}
        {(data.runs_counted?.length ?? 0) === 0 && (data.runs_skipped?.length ?? 0) === 0 && (
          <div>No live run places real orders on this account; everything the broker holds shows as broker-only.</div>
        )}
        <div>Quantities only. The broker's P&L is day-based (overnight F&O rebased to the previous close) and ours is since entry — they are not comparable and are not subtracted here. "Booked at broker" is the one number that bridges them: where two runs sit on opposite sides of a contract, the broker has already closed the matched quantity.</div>
      </div>
    </>
  );
}
