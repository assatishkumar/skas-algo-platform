import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { brokers } from "@shared/api/client";
import type { BrokerAccount } from "@shared/types";

/** 06 · Brokers — live-arm card, account cards with session pills, the daily Kite login
 * ritual (open the broker's OAuth page, paste the request_token back). Arming requires a
 * typed confirmation — it enables REAL orders on this account (with the platform's other
 * gates). Accounts are added from the desktop app. */
export default function BrokersScreen() {
  const qc = useQueryClient();
  const { data: accounts } = useQuery({
    queryKey: ["brokers"], queryFn: brokers.list, refetchInterval: 60_000,
  });
  const anyArmed = (accounts ?? []).some((a) => a.armed);

  return (
    <div className="screen" style={{ paddingTop: "calc(14px + env(safe-area-inset-top))" }}>
      <div className="page-title">Brokers</div>

      <ArmCard accounts={accounts ?? []} anyArmed={anyArmed}
        onChanged={() => qc.invalidateQueries({ queryKey: ["brokers"] })} />

      <div className="sg" style={{
        fontWeight: 700, fontSize: 14, color: "var(--muted)", margin: "18px 2px 10px",
      }}>
        ACCOUNTS · {accounts?.length ?? 0}
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 11 }}>
        {(accounts ?? []).map((a) => (
          <AccountCard key={a.id} a={a}
            onChanged={() => qc.invalidateQueries({ queryKey: ["brokers"] })} />
        ))}
      </div>
      <div style={{
        marginTop: 12, border: "1.5px dashed var(--field-border)", borderRadius: 18,
        padding: 15, textAlign: "center", color: "var(--faint)", fontSize: 13.5,
        fontWeight: 700,
      }}>
        Add broker accounts from the desktop app
      </div>
    </div>
  );
}

function ArmCard({ accounts, anyArmed, onChanged }: {
  accounts: BrokerAccount[]; anyArmed: boolean; onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const target = accounts.find((a) => a.armed) ?? accounts.find((a) => a.has_session);

  async function toggle() {
    if (!target || busy) return;
    if (!anyArmed) {
      // Typed confirmation for ARMING — the destructive direction.
      const typed = window.prompt(
        `ARM "${target.label}" for REAL orders?\n\nReal orders also require ` +
        `SKAS_LIVE_TRADING_ENABLED on the server and a LIVE-mode run.\n\n` +
        `Type ARM to confirm:`);
      if (typed !== "ARM") return;
    } else if (!window.confirm(`Disarm "${target.label}" (back to paper-only)?`)) {
      return;
    }
    setBusy(true);
    try {
      await (anyArmed ? brokers.disarm(target.id) : brokers.arm(target.id));
      onChanged();
    } catch (e) {
      window.alert((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card" style={{
      marginTop: 14, borderRadius: 21,
      border: anyArmed ? "1.5px solid var(--danger)" : "1px solid var(--border)",
    }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div>
          <div className="sg" style={{ fontWeight: 700, fontSize: 16.5 }}>Live trading</div>
          <div style={{
            fontSize: 12.5, marginTop: 3, fontWeight: 600,
            color: anyArmed ? "var(--danger)" : "var(--faint)",
          }}>
            {anyArmed
              ? `ARMED (${accounts.find((a) => a.armed)?.label}) — real orders will be placed`
              : "Disarmed — paper only"}
          </div>
        </div>
        <button onClick={toggle} disabled={busy || !target} aria-label="arm switch"
          style={{
            width: 56, height: 33, borderRadius: 17, position: "relative",
            background: anyArmed ? "var(--danger)" : "var(--seg)",
            transition: "background .15s", opacity: target ? 1 : 0.4,
          }}>
          <span style={{
            position: "absolute", top: 3, left: anyArmed ? 26 : 3, width: 27, height: 27,
            borderRadius: "50%", background: "#fff",
            boxShadow: "0 1px 4px rgba(0,0,0,.25)", transition: "left .15s",
          }} />
        </button>
      </div>
    </div>
  );
}

function AccountCard({ a, onChanged }: { a: BrokerAccount; onChanged: () => void }) {
  const [token, setToken] = useState("");
  const [pasteOpen, setPasteOpen] = useState(false);
  const login = useMutation({
    mutationFn: () => brokers.login(a.id, token.trim()),
    onSuccess: () => {
      setPasteOpen(false);
      setToken("");
      onChanged();
    },
  });

  async function startLogin() {
    try {
      const { login_url } = await brokers.loginUrl(a.id);
      // In-app browser when native; new tab in browser dev. The Kite redirect carries
      // ?request_token=... — paste it back below (deep-link capture is a later step).
      try {
        const mod = await import("@capacitor/browser");
        await mod.Browser.open({ url: login_url });
      } catch {
        window.open(login_url, "_blank");
      }
      setPasteOpen(true);
    } catch (e) {
      window.alert((e as Error).message);
    }
  }

  const zerodha = (a.broker ?? "zerodha") === "zerodha";
  const expiry = a.session_expires_at ? new Date(a.session_expires_at) : null;
  const mins = expiry ? Math.max(0, Math.round((expiry.getTime() - Date.now()) / 60000)) : 0;
  const sessionLabel = a.has_session
    ? (mins >= 60 ? `${Math.floor(mins / 60)}h ${mins % 60}m` : `${mins}m`)
    : null;

  return (
    <div className="card">
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <div style={{
          width: 46, height: 46, borderRadius: 13, display: "flex", alignItems: "center",
          justifyContent: "center", fontWeight: 800, fontSize: 20,
          background: zerodha ? "var(--zerodha-bg)" : "var(--dhan-bg)",
          color: zerodha ? "var(--zerodha)" : "var(--dhan)",
        }}>
          {zerodha ? "Z" : "D"}
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="sg" style={{ fontWeight: 700, fontSize: 16.5 }}>{a.label}</div>
          <div style={{ fontSize: 12.5, color: "var(--faint)" }}>
            {a.broker ?? "zerodha"}{a.user_id ? ` · ${a.user_id}` : ""}
            {a.armed ? " · ARMED" : ""}
          </div>
        </div>
        {sessionLabel ? (
          <span style={{
            display: "inline-flex", alignItems: "center", gap: 6,
            background: "var(--ok-bg)", color: "var(--ok-text)", borderRadius: 999,
            padding: "6px 11px", fontSize: 12.5, fontWeight: 800,
          }}>
            <span style={{
              width: 7, height: 7, borderRadius: 4, background: "var(--ok-text)",
            }} />
            {sessionLabel}
          </span>
        ) : (
          <button onClick={startLogin} style={{
            background: "var(--accent-deep)", color: "#fff", borderRadius: 13,
            padding: "10px 20px", fontWeight: 800, fontSize: 14, minHeight: 40,
          }}>Login</button>
        )}
      </div>
      {pasteOpen && (
        <div style={{ marginTop: 12, display: "flex", gap: 8 }}>
          <input
            style={{
              flex: 1, background: "var(--field)", border: "1.5px solid var(--field-border)",
              borderRadius: 12, padding: "10px 12px", fontSize: 14, color: "var(--strong)",
              outline: "none",
            }}
            placeholder="paste request_token from the redirect URL"
            autoCapitalize="none" autoCorrect="off"
            value={token} onChange={(e) => setToken(e.target.value)}
          />
          <button onClick={() => login.mutate()} disabled={!token.trim() || login.isPending}
            style={{
              background: "var(--accent-deep)", color: "#fff", borderRadius: 12,
              padding: "0 16px", fontWeight: 800, fontSize: 13.5,
            }}>
            {login.isPending ? "…" : "Go"}
          </button>
        </div>
      )}
      {login.error && (
        <div style={{ marginTop: 8, color: "var(--danger)", fontSize: 12.5, fontWeight: 600 }}>
          {(login.error as Error).message}
        </div>
      )}
    </div>
  );
}
