import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api, brokers } from "../api/client";
import { ErrorBox, Spinner } from "../components/ui";
import type { BrokerAccount } from "../types";

/** Brokers — broker account management, rebuilt per the Claude Design handoff
 * (design_handoff_brokers): two-column layout (sticky Connect + security cards left,
 * account cards right), broker tiles, validation-gated Connect, and an impossible-to-miss
 * ARMED treatment (gradient strip + danger ring + pulsing header pill). All data binds to
 * the real brokers API; the server flag renders read-only (it's server config). */

// Broker brand tints (handoff literals, not theme vars).
const BRAND: Record<string, { bg: string; fg: string; letter: string; sub: string }> = {
  zerodha: { bg: "#fdece7", fg: "#e8551f", letter: "Z", sub: "Kite Connect" },
  dhan: { bg: "#e7f0fd", fg: "#2f6bd6", letter: "D", sub: "Access token" },
};

const HINTS: Record<string, string> = {
  zerodha:
    "Enter your Kite Connect app credentials. After connecting, hit Login to paste Kite's request_token — SKAS exchanges it for the daily access token.",
  dhan:
    "Enter your Dhan client ID and a portal-generated access token (My Profile → DhanHQ Trading APIs). No password or TOTP is ever stored. Live quotes additionally need Dhan's paid Data APIs plan.",
};

/** Tiny inline icon: `d` is |-separated segments — raw path, `pl:` polyline, `rc:` rect. */
function Icon({ d, size = 15, className = "" }: { d: string; size?: number; className?: string }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className}>
      {d.split("|").map((seg, i) => {
        if (seg.startsWith("pl:")) return <polyline key={i} points={seg.slice(3)} />;
        if (seg.startsWith("rc:")) {
          const [x, y, w, h, r] = seg.slice(3).split(",").map(Number);
          return <rect key={i} x={x} y={y} width={w} height={h} rx={r} />;
        }
        return <path key={i} d={seg} />;
      })}
    </svg>
  );
}

const I = {
  shield: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
  bolt: "M13 2 3 14h9l-1 8 10-12h-9l1-8z",
  login: "M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4|pl:10 17 15 12 10 7|M15 12H3",
  lockOpen: "rc:3,11,18,11,2|M7 11V7a5 5 0 0 1 9.9-1",
  lock: "rc:3,11,18,11,2|M7 11V7a5 5 0 0 1 10 0v4",
  trash: "pl:3 6 5 6 21 6|M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2",
  db: "M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5|M21 12c0 1.66-4 3-9 3s-9-1.34-9-3",
  refresh: "M20.49 15a9 9 0 1 1-2.12-9.36L23 10|pl:23 4 23 10 17 10",
  check: "pl:20 6 9 17 4 12",
};

function expiresIn(iso: string | null): string {
  if (!iso) return "";
  const ms = new Date(iso).getTime() - Date.now();
  if (ms <= 0) return "expired";
  const h = Math.floor(ms / 3_600_000);
  const m = Math.floor((ms % 3_600_000) / 60_000);
  return h > 0 ? `expires ${h}h ${m}m` : `expires ${m}m`;
}

function lastRefreshText(id: number): string {
  const ts = Number(localStorage.getItem(`brokers.lastRefresh.${id}`) || 0);
  if (!ts) return "never refreshed";
  const mins = Math.floor((Date.now() - ts) / 60_000);
  if (mins < 1) return "updated just now";
  if (mins < 60) return `updated ${mins}m ago`;
  const h = Math.floor(mins / 60);
  return h < 24 ? `updated ${h}h ago` : `updated ${Math.floor(h / 24)}d ago`;
}

function Notice({ text, ok }: { text: string; ok: boolean }) {
  return ok ? (
    <div className="rounded-[13px] px-4 py-2.5 text-sm"
      style={{ background: "var(--ok-bg)", color: "var(--ok-text)" }}>
      ✓ {text}
    </div>
  ) : (
    <ErrorBox message={text} />
  );
}

// ----------------------------------------------------------------- connect card

type FormState = { label: string; userId: string; apiKey: string; apiSecret: string; token: string };
const EMPTY_FORM: FormState = { label: "", userId: "", apiKey: "", apiSecret: "", token: "" };

function ConnectCard({ onDone }: { onDone: (msg: string, id: number) => void }) {
  const [broker, setBroker] = useState<"zerodha" | "dhan">("zerodha");
  const [f, setF] = useState<FormState>(EMPTY_FORM);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const set = (k: keyof FormState) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setF((v) => ({ ...v, [k]: e.target.value }));

  const ready = broker === "zerodha"
    ? !!(f.userId.trim() && f.apiKey.trim() && f.apiSecret.trim())
    : !!(f.userId.trim() && f.token.trim());

  async function connect() {
    setBusy(true);
    setErr(null);
    try {
      const label = f.label.trim() || (broker === "zerodha" ? "Zerodha account" : "Dhan account");
      const acct = await brokers.connect({
        broker, label, user_id: f.userId.trim(),
        api_key: broker === "zerodha" ? f.apiKey.trim() : "",
        api_secret: broker === "zerodha" ? f.apiSecret.trim() : "",
      });
      let msg = `${label} saved — hit Login on its card to start the session.`;
      if (broker === "dhan") {
        // Dhan's pasted token IS the session — one step, no separate login hop.
        await brokers.login(acct.id, f.token.trim());
        msg = `${label} connected — session active.`;
      }
      setF(EMPTY_FORM);
      onDone(msg, acct.id);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const fieldCls =
    "w-full rounded-[11px] bg-[var(--field)] border border-[var(--field-border)] px-3 py-2 font-['Space_Grotesk'] font-medium text-[13.5px] text-[var(--strong)] focus:outline-none focus:border-[var(--accent)] focus:ring-[3px] focus:ring-[var(--accent-ring)]";
  const FieldLabel = ({ text, required }: { text: string; required?: boolean }) => (
    <span className="block text-[11.5px] font-bold text-[var(--muted)] mb-1.5">
      {text}{" "}
      <span style={{ color: required ? "var(--danger)" : "var(--faint)" }} className="font-semibold">
        {required ? "required" : "optional"}
      </span>
    </span>
  );

  return (
    <div className="rounded-[18px] border border-[var(--border)] bg-[var(--card)] p-[22px]">
      <div className="font-['Space_Grotesk'] font-bold text-[16px] text-[var(--strong)]">
        Connect a broker account
      </div>
      <p className="mt-1 text-[12.5px] leading-relaxed text-[var(--muted)]">{HINTS[broker]}</p>

      {/* broker tiles */}
      <div className="mt-4 grid grid-cols-2 gap-2.5">
        {(["zerodha", "dhan"] as const).map((b) => {
          const brand = BRAND[b];
          const sel = broker === b;
          return (
            <button key={b} type="button"
              onClick={() => { setBroker(b); setF(EMPTY_FORM); setErr(null); }}
              className="flex items-center gap-2.5 rounded-[13px] px-3 py-2.5 text-left"
              style={sel
                ? { background: "var(--accent-ring)", border: "1.5px solid var(--accent)" }
                : { background: "var(--field)", border: "1px solid var(--field-border)" }}>
              <span className="flex h-8 w-8 items-center justify-center rounded-[10px] font-['Space_Grotesk'] font-bold"
                style={{ background: brand.bg, color: brand.fg }}>{brand.letter}</span>
              <span>
                <span className="block font-['Space_Grotesk'] font-bold text-[13.5px] text-[var(--strong)]">
                  {b === "zerodha" ? "Zerodha" : "Dhan"}
                </span>
                <span className="block text-[11px] font-semibold text-[var(--faint)]">{brand.sub}</span>
              </span>
            </button>
          );
        })}
      </div>

      {/* adaptive fields */}
      <div className="mt-4 grid grid-cols-2 gap-3">
        <label className="col-span-2 block">
          <FieldLabel text="Label" />
          <input className={fieldCls} placeholder={broker === "zerodha" ? "e.g. Satish Kite" : "e.g. Satish Dhan"}
            value={f.label} onChange={set("label")} />
        </label>
        <label className="block">
          <FieldLabel text={broker === "zerodha" ? "User ID" : "Client ID"} required />
          <input className={fieldCls} placeholder={broker === "zerodha" ? "AB1234" : "1000123456"}
            value={f.userId} onChange={set("userId")} />
        </label>
        {broker === "zerodha" ? (
          <>
            <label className="block">
              <FieldLabel text="API key" required />
              <input className={fieldCls} placeholder="kc_…" value={f.apiKey} onChange={set("apiKey")} />
            </label>
            <label className="col-span-2 block">
              <FieldLabel text="API secret" required />
              <input className={fieldCls} type="password" placeholder="••••••••"
                value={f.apiSecret} onChange={set("apiSecret")} />
            </label>
          </>
        ) : (
          <label className="block">
            <FieldLabel text="Access token" required />
            <input className={fieldCls} type="password" placeholder="paste JWT"
              value={f.token} onChange={set("token")} />
          </label>
        )}
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <button onClick={connect} disabled={!ready || busy}
          className="inline-flex items-center gap-2 rounded-[12px] px-4 py-2.5 text-sm font-bold text-white"
          style={ready && !busy
            ? { background: "var(--ft)", boxShadow: "0 6px 14px rgba(13,107,79,.24)" }
            : { background: "var(--chip)", color: "var(--faint)", cursor: "not-allowed" }}>
          <Icon d={I.bolt} size={14} /> {busy ? "Connecting…" : "Connect"}
        </button>
        <span className="text-[12px] font-semibold text-[var(--faint)]">
          {ready ? "ready — secrets encrypted on save" : "fill the required fields to continue"}
        </span>
      </div>
      {err && <div className="mt-3"><ErrorBox message={err} /></div>}
    </div>
  );
}

function SecurityCard() {
  const rows: [string, string][] = [
    ["Encrypted at rest", "API secrets and tokens are sealed with server-side encryption; never shown again after saving."],
    ["No password, no TOTP", "you authenticate on the broker's own login page and paste back a short-lived token."],
    ["Two-key live gate", "orders require both an armed account and the server flag; either one off means simulation only."],
  ];
  return (
    <div className="rounded-[18px] border border-[var(--border)] bg-[var(--card)] px-[22px] py-5">
      <div className="font-['Space_Grotesk'] font-bold text-[16px] text-[var(--strong)]">How SKAS keeps this safe</div>
      <div className="mt-3 space-y-3">
        {rows.map(([t, body]) => (
          <div key={t} className="flex gap-2.5 text-[12.5px] leading-relaxed text-[var(--muted)]">
            <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-[7px]"
              style={{ background: "var(--ok-bg)", color: "var(--ok-text)" }}>
              <Icon d={I.check} size={11} />
            </span>
            <span><strong className="text-[var(--strong)]">{t}</strong> — {body}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ login flow

function LoginFlow({ id, broker, onDone }: { id: number; broker: string; onDone: () => void }) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const isDhan = broker === "dhan";

  async function openLogin() {
    setErr(null);
    try {
      const { login_url } = await brokers.loginUrl(id);
      window.open(login_url, "_blank", "noopener");
    } catch (e) {
      setErr((e as Error).message);
    }
  }
  async function submit() {
    setBusy(true);
    setErr(null);
    try {
      await brokers.login(id, token.trim());
      setToken("");
      onDone();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-4 border-t border-[var(--divider)] pt-4 space-y-2.5">
      <div className="text-[12.5px] text-[var(--muted)]">
        {isDhan
          ? <>Generate an access token on Dhan (My Profile → DhanHQ Trading APIs) and paste it — valid ~24h.</>
          : <>Open the Kite login, sign in there, and copy the <code>request_token</code> from the redirected URL, then paste it.</>}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <button onClick={openLogin}
          className="rounded-[11px] bg-[var(--chip)] px-3 py-2 text-xs font-bold text-[var(--chip-text)]">
          {isDhan ? "Open Dhan ↗" : "Open Kite login ↗"}
        </button>
        <input
          className="min-w-[200px] flex-1 rounded-[11px] border border-[var(--field-border)] bg-[var(--field)] px-3 py-2 font-['Space_Grotesk'] text-[13px] text-[var(--strong)] focus:outline-none focus:border-[var(--accent)]"
          placeholder={isDhan ? "paste access token" : "paste request_token"}
          value={token}
          onChange={(e) => setToken(e.target.value)}
        />
        <button onClick={submit} disabled={busy || !token.trim()}
          className="rounded-[11px] px-3 py-2 text-xs font-bold text-white disabled:opacity-50"
          style={{ background: "var(--ft)" }}>
          {busy ? "Saving…" : "Submit token"}
        </button>
      </div>
      {err && <ErrorBox message={err} />}
    </div>
  );
}

// ---------------------------------------------------------------- account card

// Index SPOT series the strategies HARD-require (they sit in no equity universe and drive the
// options backtest calendar). Kept as a floor on top of the full cached set below, so they're
// refreshed even on a cold cache.
const INDEX_SPOTS = ["NIFTY 50", "NIFTY BANK"];

/** One-tap end-to-end order-path probe: 1 lot cheap OTM option or 1 share of a stock,
 *  buy → ~60s → sell → the run stops itself. Lives HERE because it answers the Brokers
 *  page's question — "will real orders actually work right now?" — and the §1 gates
 *  fully apply (LIVE on a disarmed account paper-fills and wears the "orders PAPER"
 *  chip, a useful negative test in itself). */
function SmokeTestCard({ accounts, onAction }: {
  accounts: BrokerAccount[];
  onAction: (fn: () => Promise<unknown>, ok: string) => void;
}) {
  const sessioned = accounts.filter((a) => a.has_session && a.broker === "zerodha");
  const [leg, setLeg] = useState<"option" | "stock">("option");
  const [underlying, setUnderlying] = useState("NIFTY");
  const [right, setRight] = useState<"CE" | "PE">("CE");
  const [symbol, setSymbol] = useState("ITC");
  const [mode, setMode] = useState("PAPER");
  const [accountId, setAccountId] = useState<number | null>(null);
  const acct = accountId ?? sessioned[0]?.id ?? null;
  const field = "w-full rounded-[10px] bg-[var(--field)] border border-[var(--field-border)] px-2.5 py-1.5 text-[13px] text-[var(--strong)]";

  function deploy() {
    if (mode === "LIVE") {
      const typed = window.prompt(
        "LIVE smoke test: if this account is ARMED and the server flag is on, this places a REAL " +
        "buy and a REAL sell at the broker (1 " + (leg === "option" ? "lot" : "share") + ").\n\nType REAL to continue.");
      if (typed !== "REAL") return;
    }
    onAction(
      () => api.smokeTestDeploy({
        leg, mode, broker_account_id: acct, quote_source: "zerodha",
        ...(leg === "option" ? { underlying, right } : { symbol: symbol.trim().toUpperCase() }),
      }),
      `Smoke test deployed (${mode}) — watch the Live page: buy → 60s → sell, then the run stops itself. ` +
      "Expect Telegram: reconcile OK before entry, and again after exit.",
    );
  }

  return (
    <div className="rounded-[18px] border border-[var(--border)] bg-[var(--card)] px-[22px] py-5">
      <div className="font-['Space_Grotesk'] font-bold text-[16px] text-[var(--strong)]">Broker smoke test</div>
      <p className="mt-1 text-[12.5px] leading-relaxed text-[var(--muted)]">
        Buys the smallest possible position ({leg === "option" ? "1 lot of a ₹5–20 OTM weekly option" : "1 share"}),
        holds ~60s, sells, and stops itself — proving login → order → fill → reconcile → exit end to end.
      </p>
      <div className="mt-3 grid grid-cols-2 gap-2">
        <label className="text-[11px] font-semibold text-[var(--faint)] col-span-2">
          What to trade
          <div className="mt-1 grid grid-cols-2 gap-1.5">
            {(["option", "stock"] as const).map((v) => (
              <button key={v} onClick={() => setLeg(v)}
                className={`rounded-[10px] px-2 py-1.5 text-[12.5px] font-semibold border ${leg === v
                  ? "border-transparent bg-[var(--accent,#0f766e)] text-white"
                  : "border-[var(--field-border)] bg-[var(--field)] text-[var(--muted)]"}`}>
                {v === "option" ? "OTM option · 1 lot" : "Stock · 1 share"}
              </button>
            ))}
          </div>
        </label>
        {leg === "option" ? (
          <>
            <label className="text-[11px] font-semibold text-[var(--faint)]">
              Underlying
              <select className={field} value={underlying} onChange={(e) => setUnderlying(e.target.value)}>
                {["NIFTY", "BANKNIFTY", "SENSEX"].map((u) => <option key={u}>{u}</option>)}
              </select>
            </label>
            <label className="text-[11px] font-semibold text-[var(--faint)]">
              Right
              <select className={field} value={right} onChange={(e) => setRight(e.target.value as "CE" | "PE")}>
                <option>CE</option><option>PE</option>
              </select>
            </label>
          </>
        ) : (
          <label className="text-[11px] font-semibold text-[var(--faint)] col-span-2">
            Symbol
            <input className={field} value={symbol} onChange={(e) => setSymbol(e.target.value)} />
          </label>
        )}
        <label className="text-[11px] font-semibold text-[var(--faint)]">
          Account
          <select className={field} value={acct ?? ""} onChange={(e) => setAccountId(Number(e.target.value))}>
            {sessioned.length === 0 && <option value="">No logged-in session</option>}
            {sessioned.map((a) => <option key={a.id} value={a.id}>{a.label}{a.armed ? " · ARMED" : ""}</option>)}
          </select>
        </label>
        <label className="text-[11px] font-semibold text-[var(--faint)]">
          Mode
          <select className={field} value={mode} onChange={(e) => setMode(e.target.value)}>
            <option>PAPER</option><option>LIVE</option>
          </select>
        </label>
      </div>
      <button onClick={deploy} disabled={acct == null}
        title={acct == null ? "Needs a logged-in Zerodha session" : undefined}
        className={`mt-3 w-full rounded-[11px] px-3 py-2 text-[13px] font-bold disabled:opacity-50 ${mode === "LIVE"
          ? "bg-[var(--danger)] text-white" : "bg-[var(--chip)] text-[var(--chip-text)]"}`}>
        {mode === "LIVE" ? "Deploy LIVE smoke test" : "Deploy paper smoke test"}
      </button>
    </div>
  );
}

function AccountCard({ a, loginOpen, onToggleLogin, onAction, onLoggedIn }: {
  a: BrokerAccount;
  loginOpen: boolean;
  onToggleLogin: () => void;
  onAction: (fn: () => Promise<unknown>, ok: string) => void;
  onLoggedIn: () => void;
}) {
  const brand = BRAND[a.broker] ?? BRAND.zerodha;
  const [busy, setBusy] = useState<null | "stocks" | "options">(null);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [cacheMsg, setCacheMsg] = useState<string | null>(null);
  const showCache = a.broker === "zerodha" && a.has_session;

  // All equity + index daily bars: the Nifty-500 universe PLUS every symbol already in the cache
  // — that's where ALL the index series live (NIFTY 50/BANK, NIFTY 100/200/500, ALPHA 50,
  // sectorals, INDIA VIX…). Union + dedupe so "indices" means every cached index, not just the
  // two strategy spots (which used to be the only indices refreshed, so the rest silently lagged).
  async function refreshStocks() {
    setBusy("stocks");
    setCacheMsg(null);
    setProgress(null);
    try {
      const [{ symbols: stocks }, cached] = await Promise.all([
        api.universeSymbols("nifty500"),
        api.dataSymbols(),
      ]);
      const symbols = Array.from(
        new Set([...INDEX_SPOTS, ...cached.map((s) => s.symbol), ...stocks]),
      );
      const CHUNK = 15;
      let ok = 0, errors = 0;
      setProgress({ done: 0, total: symbols.length });
      for (let i = 0; i < symbols.length; i += CHUNK) {
        // Chunked so the button shows real progress instead of one long opaque call.
        const { refreshed } = await brokers.refreshCache(a.id, { symbols: symbols.slice(i, i + CHUNK) });
        for (const e of Object.values(refreshed)) e.error ? errors++ : ok++;
        setProgress({ done: Math.min(i + CHUNK, symbols.length), total: symbols.length });
      }
      localStorage.setItem(`brokers.lastRefresh.${a.id}`, String(Date.now()));
      setCacheMsg(`refreshed ${ok} stocks + indices${errors ? ` · ${errors} errors` : ""}`);
    } catch (e) {
      setCacheMsg((e as Error).message);
    } finally {
      setBusy(null);
      setProgress(null);
    }
  }

  // NIFTY + BANKNIFTY option bhavcopy (last ~60 days).
  async function refreshOptions() {
    setBusy("options");
    setCacheMsg(null);
    try {
      const today = new Date();
      const from = new Date(today);
      from.setDate(from.getDate() - 60);
      const iso = (d: Date) => d.toISOString().slice(0, 10);
      const res = await api.optionsRefresh({
        underlyings: ["NIFTY", "BANKNIFTY"], start_date: iso(from), end_date: iso(today),
      });
      localStorage.setItem(`brokers.lastRefresh.${a.id}`, String(Date.now()));
      setCacheMsg(`NIFTY + BN options: ${res.days_saved} day(s) refreshed`);
    } catch (e) {
      setCacheMsg(`options failed: ${(e as Error).message}`);
    } finally {
      setBusy(null);
    }
  }

  const chipBtn =
    "inline-flex items-center gap-1.5 rounded-[11px] bg-[var(--chip)] px-3 py-2 text-xs font-bold text-[var(--chip-text)] hover:opacity-85";

  return (
    <div id={`broker-card-${a.id}`}
      className="overflow-hidden rounded-[16px] bg-[var(--card)]"
      style={a.armed
        ? { border: "1px solid var(--danger)", boxShadow: "0 0 0 3px var(--danger-ring)" }
        : { border: "1px solid var(--border)" }}>
      {a.armed && (
        <div className="h-1" style={{ background: "linear-gradient(90deg, var(--danger), var(--danger-deep))" }} />
      )}
      <div className="px-5 py-[18px]">
        <div className="flex flex-wrap items-start gap-3.5">
          <span className="flex h-11 w-11 items-center justify-center rounded-[12px] font-['Space_Grotesk'] font-bold text-[18px]"
            style={{ background: brand.bg, color: brand.fg }}>{brand.letter}</span>
          <div className="min-w-0">
            <div className="flex flex-wrap items-baseline gap-2">
              <span className="font-['Space_Grotesk'] font-bold text-[17px] text-[var(--strong)]">{a.label}</span>
              <span className="font-['Space_Grotesk'] text-[12px] font-semibold text-[var(--faint)]">
                {a.broker} · {a.user_id}
              </span>
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <span className="inline-flex items-center gap-1.5 rounded-[7px] px-2 py-1 text-[11px] font-bold"
                style={a.has_session
                  ? { background: "var(--ok-bg)", color: "var(--ok-text)" }
                  : { background: "var(--chip)", color: "var(--muted)" }}>
                <span className="h-1.5 w-1.5 rounded-full"
                  style={{ background: a.has_session ? "var(--pos)" : "var(--faint)" }} />
                {a.has_session ? `session · ${expiresIn(a.session_expires_at)}` : "no session"}
              </span>
              <span className="inline-flex items-center gap-1.5 rounded-[7px] px-2 py-1 text-[11px] font-bold"
                style={a.armed
                  ? { background: "var(--warn-bg)", color: "var(--warn-text)" }
                  : { background: "var(--chip)", color: "var(--muted)" }}>
                {a.armed ? "● armed for live orders" : "disarmed"}
              </span>
            </div>
          </div>
          <div className="ml-auto flex items-center gap-2">
            <button onClick={onToggleLogin} className={chipBtn}>
              <Icon d={I.login} size={13} /> {a.has_session ? "Re-login" : "Login"}
            </button>
            {a.armed ? (
              <button
                onClick={() => onAction(() => brokers.disarm(a.id), `${a.label} disarmed.`)}
                className="inline-flex items-center gap-1.5 rounded-[11px] px-3 py-2 text-xs font-bold text-white"
                style={{ background: "var(--danger)" }}>
                <Icon d={I.lock} size={13} /> Disarm
              </button>
            ) : (
              <button
                onClick={() => onAction(() => brokers.arm(a.id), `${a.label} ARMED for live orders.`)}
                className="inline-flex items-center gap-1.5 rounded-[11px] bg-transparent px-3 py-2 text-xs font-bold"
                style={{ border: "1px solid var(--warn-text)", color: "var(--warn-text)" }}>
                <Icon d={I.lockOpen} size={13} /> Arm
              </button>
            )}
            <button
              title="Delete account"
              onClick={() => {
                if (window.confirm(`Delete ${a.label}? Its encrypted credentials are removed.`))
                  onAction(() => brokers.remove(a.id), `${a.label} deleted.`);
              }}
              className="flex h-[38px] w-[38px] items-center justify-center rounded-[11px] text-[var(--faint)] hover:text-[var(--danger)]"
              style={{ border: "1px solid var(--border)" }}>
              <Icon d={I.trash} size={14} />
            </button>
          </div>
        </div>

        {loginOpen && <LoginFlow id={a.id} broker={a.broker} onDone={onLoggedIn} />}

        {showCache && (
          <div className="mt-4 border-t border-[var(--divider)] pt-4">
            <div className="flex items-start gap-2 text-[12.5px] text-[var(--muted)]">
              <Icon d={I.db} size={14} className="mt-0.5 shrink-0" />
              <span>Historical cache shares this Kite session — refresh candles without a second login.
                <b>Stocks + indices</b> = Nifty 500 + every cached index (NIFTY 50/BANK, sectorals,
                INDIA VIX…); <b>Options</b> = ~60 days of NIFTY &amp; BANKNIFTY bhavcopy.</span>
            </div>
            <div className="mt-2.5 flex flex-wrap items-center gap-2.5">
              <button onClick={refreshStocks} disabled={!!busy}
                className="inline-flex items-center gap-1.5 rounded-[11px] px-3.5 py-2 text-xs font-bold text-white"
                style={busy ? { background: "var(--chip)", color: "var(--muted)" } : { background: "var(--accent)" }}>
                <Icon d={I.refresh} size={13} className={busy === "stocks" ? "animate-spin" : ""} />
                {busy === "stocks"
                  ? `Refreshing ${progress?.done ?? 0}/${progress?.total ?? 0}…`
                  : "Refresh stocks + indices"}
              </button>
              <button onClick={refreshOptions} disabled={!!busy}
                className="inline-flex items-center gap-1.5 rounded-[11px] px-3.5 py-2 text-xs font-bold"
                style={busy
                  ? { background: "var(--chip)", color: "var(--muted)" }
                  : { border: "1px solid var(--accent)", color: "var(--accent)" }}>
                <Icon d={I.refresh} size={13} className={busy === "options" ? "animate-spin" : ""} />
                {busy === "options" ? "Refreshing options…" : "Refresh options"}
              </button>
              <span className="text-[12px] font-semibold text-[var(--faint)]">
                {cacheMsg ?? lastRefreshText(a.id)}
              </span>
            </div>
            {busy === "stocks" && progress && (
              <div className="mt-2 h-1.5 w-full max-w-md overflow-hidden rounded-full bg-[var(--track)]">
                <div className="h-full transition-[width] duration-200"
                  style={{ background: "var(--accent)", width: `${progress.total ? (progress.done / progress.total) * 100 : 0}%` }} />
              </div>
            )}
          </div>
        )}

        {a.armed && !a.live_trading_enabled && (
          <div className="mt-3 text-xs text-[var(--faint)]">
            Armed, but server <code>SKAS_LIVE_TRADING_ENABLED</code> is false — still no real orders.
          </div>
        )}
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------- page

export default function BrokersPage() {
  const { data, isLoading, error, refetch } = useQuery({ queryKey: ["brokers"], queryFn: brokers.list });
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [loginFor, setLoginFor] = useState<number | null>(null);
  const accounts = useMemo(() => data ?? [], [data]);

  const sessions = accounts.filter((a) => a.has_session).length;
  const armed = accounts.filter((a) => a.armed).length;
  const liveFlag = accounts.some((a) => a.live_trading_enabled);

  async function onAction(fn: () => Promise<unknown>, ok: string) {
    setMsg(null);
    try {
      await fn();
      setMsg({ text: ok, ok: true });
      refetch();
    } catch (e) {
      setMsg({ text: (e as Error).message, ok: false });
    }
  }

  function onConnected(text: string, id: number) {
    setMsg({ text, ok: true });
    setLoginFor(id);
    refetch().then(() =>
      setTimeout(() =>
        document.getElementById(`broker-card-${id}`)?.scrollIntoView({ behavior: "smooth", block: "center" }), 120),
    );
  }

  return (
    <div className="font-['Manrope'] bg-[var(--page)] min-h-[calc(100vh-3.5rem)] text-[var(--strong)]">
      <div className="mx-auto max-w-[1320px] px-4 sm:px-8 pt-7 pb-[90px]">
        {/* page header */}
        <div className="flex flex-wrap items-start gap-3">
          <div className="min-w-0 max-w-[640px]">
            <h1 className="font-['Space_Grotesk'] text-[27px] font-bold m-0">Brokers</h1>
            <p className="mt-1 text-sm text-[var(--muted)]">
              Connect a broker to place live orders. You log in on the broker's own site and paste a
              token — SKAS never sees your password or TOTP.
            </p>
          </div>
          <div className="ml-auto flex items-center gap-2.5">
            <div className="flex items-center rounded-[12px] border border-[var(--border)] bg-[var(--card)] px-3.5 py-2 text-[12px] font-semibold text-[var(--muted)]">
              <span className="mr-3 flex items-baseline gap-1.5">
                <span className="font-['Space_Grotesk'] text-[17px] font-bold tabular-nums text-[var(--strong)]">{accounts.length}</span>
                accounts
              </span>
              <span className="mr-3 h-4 w-px bg-[var(--divider)]" />
              <span className="flex items-baseline gap-1.5">
                <span className="font-['Space_Grotesk'] text-[17px] font-bold tabular-nums" style={{ color: "var(--pos)" }}>{sessions}</span>
                live sessions
              </span>
            </div>
            <span className="inline-flex items-center gap-2 rounded-[12px] px-3.5 py-2.5 text-[12px] font-bold"
              style={armed > 0
                ? { background: "var(--warn-bg)", color: "var(--warn-text)" }
                : { background: "var(--card)", color: "var(--muted)", border: "1px solid var(--border)" }}>
              <span className={`h-2 w-2 rounded-full ${armed > 0 ? "animate-pulse" : ""}`}
                style={{ background: armed > 0 ? "var(--danger)" : "var(--pos)" }} />
              {armed > 0 ? `${armed} armed` : "none armed"}
            </span>
          </div>
        </div>

        {/* server-flag banner (read-only — the flag is server config, per the handoff note) */}
        <div className="mt-5 flex items-center gap-3 rounded-[13px] px-[18px] py-3 text-sm"
          style={liveFlag
            ? { background: "var(--warn-bg)", color: "var(--warn-text)" }
            : { background: "var(--stat)", color: "var(--muted)" }}>
          <Icon d={I.shield} size={17} className="shrink-0" />
          <span>
            Real orders never fire unless an account is <b>armed</b> and the server flag{" "}
            <code className="rounded bg-[var(--chip)] px-1.5 py-0.5 font-['Space_Grotesk'] text-[12px] text-[var(--chip-text)]">
              SKAS_LIVE_TRADING_ENABLED
            </code>{" "}
            is <b style={{ color: liveFlag ? "var(--pos)" : "var(--danger)" }}>{String(liveFlag)}</b>.
          </span>
        </div>

        {msg && <div className="mt-4"><Notice text={msg.text} ok={msg.ok} /></div>}

        {/* two-column grid — collapses to one column below lg */}
        <div className="mt-5 grid items-start gap-[22px] lg:grid-cols-[428px_1fr]">
          <div className="space-y-4 lg:sticky lg:top-[92px]">
            <ConnectCard onDone={onConnected} />
            <SmokeTestCard accounts={accounts} onAction={onAction} />
            <SecurityCard />
          </div>

          <div>
            <div className="flex items-baseline gap-2.5">
              <span className="font-['Space_Grotesk'] text-[16px] font-bold">Connected accounts</span>
              <span className="text-[12px] font-semibold text-[var(--faint)]">
                {accounts.length} total · sessions expire daily
              </span>
            </div>
            <div className="mt-3.5 space-y-3.5">
              {isLoading ? (
                <Spinner />
              ) : error ? (
                <ErrorBox message={(error as Error).message} />
              ) : (
                accounts.map((a) => (
                  <AccountCard key={a.id} a={a}
                    loginOpen={loginFor === a.id}
                    onToggleLogin={() => setLoginFor((v) => (v === a.id ? null : a.id))}
                    onAction={onAction}
                    onLoggedIn={() => {
                      setLoginFor(null);
                      setMsg({ text: `${a.label}: session active.`, ok: true });
                      refetch();
                    }}
                  />
                ))
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
