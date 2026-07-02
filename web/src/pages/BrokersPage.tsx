import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, brokers } from "../api/client";
import { Badge, Card, ErrorBox, Spinner } from "../components/ui";
import type { BrokerConnectRequest } from "../types";

const inputClass =
  "w-full rounded-md bg-slate-800 border border-slate-700 px-3 py-2 text-sm focus:outline-none focus:border-brand";

const EMPTY: BrokerConnectRequest = {
  broker: "zerodha",
  label: "",
  api_key: "",
  api_secret: "",
  user_id: "",
};

function LoginFlow({ id, onDone }: { id: number; onDone: () => void }) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

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
    <div className="mt-3 border-t border-slate-800 pt-3 space-y-2">
      <div className="text-xs text-slate-400">
        1. Open the Kite login, sign in there, and copy the <code>request_token</code> from the
        redirected URL. 2. Paste it below.
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <button onClick={openLogin} className="rounded bg-slate-700 hover:bg-slate-600 px-3 py-1.5 text-xs">
          Open Kite login ↗
        </button>
        <input
          className="flex-1 min-w-[220px] rounded bg-slate-800 border border-slate-700 px-3 py-1.5 text-sm"
          placeholder="paste request_token"
          value={token}
          onChange={(e) => setToken(e.target.value)}
        />
        <button
          onClick={submit}
          disabled={busy || !token.trim()}
          className="rounded bg-brand hover:bg-brand-light px-3 py-1.5 text-xs disabled:opacity-50"
        >
          {busy ? "Exchanging…" : "Submit token"}
        </button>
      </div>
      {err && <ErrorBox message={err} />}
    </div>
  );
}

function RefreshData({ id }: { id: number }) {
  const { data: universeData } = useQuery({ queryKey: ["universes"], queryFn: api.universes });
  const [universe, setUniverse] = useState("nifty50");
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [summary, setSummary] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function refresh() {
    setBusy(true);
    setErr(null);
    setSummary(null);
    setProgress(null);
    try {
      // Resolve the universe to its cached symbols, then refresh in small chunks so the button
      // shows real progress (e.g. "Refreshing 45/491…") instead of one long opaque call.
      const { symbols } = await api.universeSymbols(universe);
      if (!symbols.length) {
        setErr("universe resolved to no cached symbols");
        return;
      }
      const CHUNK = 15;
      let ok = 0;
      let errors = 0;
      let latest: string | undefined;
      setProgress({ done: 0, total: symbols.length });
      for (let i = 0; i < symbols.length; i += CHUNK) {
        const { refreshed } = await brokers.refreshCache(id, { symbols: symbols.slice(i, i + CHUNK) });
        for (const e of Object.values(refreshed)) {
          if (e.error) errors += 1;
          else ok += 1;
          if (e.last_date && (!latest || e.last_date > latest)) latest = e.last_date;
        }
        setProgress({ done: Math.min(i + CHUNK, symbols.length), total: symbols.length });
      }
      setSummary(
        `Refreshed ${ok} symbols on the shared session` +
          (latest ? ` · latest ${latest}` : "") +
          (errors ? ` · ${errors} errors` : ""),
      );
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
      setProgress(null);
    }
  }

  return (
    <div className="mt-3 border-t border-slate-800 pt-3 space-y-2">
      <div className="text-xs text-slate-400">
        Update the historical cache using this same Kite login (data + trading share one session).
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <select
          className="rounded bg-slate-800 border border-slate-700 px-2 py-1.5 text-sm"
          value={universe}
          onChange={(e) => setUniverse(e.target.value)}
        >
          {(universeData ?? []).map((u) => (
            <option key={u.name} value={u.name}>{u.label} ({u.count})</option>
          ))}
        </select>
        <button
          onClick={refresh}
          disabled={busy}
          className="rounded bg-slate-700 hover:bg-slate-600 px-3 py-1.5 text-xs disabled:opacity-50"
        >
          {progress ? `Refreshing ${progress.done}/${progress.total}…` : busy ? "Refreshing…" : "Refresh data"}
        </button>
        {summary && <span className="text-xs text-emerald-600 dark:text-emerald-400">{summary}</span>}
      </div>
      {progress && (
        <div className="h-1.5 w-full max-w-md rounded-full bg-slate-800 overflow-hidden">
          <div
            className="h-full bg-emerald-500 transition-[width] duration-200"
            style={{ width: `${progress.total ? (progress.done / progress.total) * 100 : 0}%` }}
          />
        </div>
      )}
      {err && <ErrorBox message={err} />}
    </div>
  );
}

export default function BrokersPage() {
  const { data, isLoading, error, refetch } = useQuery({ queryKey: ["brokers"], queryFn: brokers.list });
  const [form, setForm] = useState<BrokerConnectRequest>(EMPTY);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loginFor, setLoginFor] = useState<number | null>(null);

  const set = (k: keyof BrokerConnectRequest) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  async function run(fn: () => Promise<unknown>, ok: string) {
    setBusy(true);
    setMsg(null);
    try {
      await fn();
      setMsg(ok);
      refetch();
    } catch (e) {
      setMsg((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Brokers</h1>

      <div className="rounded-lg border border-slate-700 bg-slate-900/40 text-slate-300 p-3 text-sm">
        You log in to Kite yourself and paste the <b>request_token</b>; we exchange it for the daily
        access token. Only the <b>API secret</b> is stored (encrypted) — no password, no TOTP.
        <b> Real orders never fire</b> unless an account is <b>armed</b> and the server has
        <code> SKAS_LIVE_TRADING_ENABLED=true</code>.
      </div>

      <Card>
        <div className="text-sm font-medium text-slate-300 mb-3">Connect a broker account</div>
        <div className="grid md:grid-cols-2 gap-3">
          <input className={inputClass} placeholder="label" value={form.label} onChange={set("label")} />
          <input className={inputClass} placeholder="user id (e.g. AB1234)" value={form.user_id} onChange={set("user_id")} />
          <input className={inputClass} placeholder="api key" value={form.api_key} onChange={set("api_key")} />
          <input className={inputClass} type="password" placeholder="api secret" value={form.api_secret} onChange={set("api_secret")} />
        </div>
        <button
          onClick={() => run(() => brokers.connect(form).then(() => setForm(EMPTY)), "Connected (secret stored encrypted).")}
          disabled={busy || !form.label}
          className="mt-3 rounded-md bg-brand hover:bg-brand-light px-4 py-2 text-sm font-medium disabled:opacity-50"
        >
          Connect
        </button>
      </Card>

      {msg && <ErrorBox message={msg} />}

      {isLoading ? (
        <Spinner />
      ) : error ? (
        <ErrorBox message={(error as Error).message} />
      ) : (
        (data ?? []).map((a) => (
          <Card key={a.id}>
            <div className="flex items-center justify-between">
              <div>
                <span className="font-medium">{a.label}</span>{" "}
                <span className="text-xs text-slate-400">{a.broker} · {a.user_id}</span>{" "}
                {a.has_session ? <Badge>session ✓</Badge> : <Badge>no session</Badge>}{" "}
                {a.armed ? <span className="text-amber-600 dark:text-amber-400 text-xs font-semibold">ARMED</span> : <Badge>disarmed</Badge>}
              </div>
              <div className="flex gap-2">
                <button onClick={() => setLoginFor((v) => (v === a.id ? null : a.id))} className="rounded bg-slate-800 hover:bg-slate-700 px-3 py-1.5 text-xs">
                  {a.has_session ? "Re-login" : "Login"}
                </button>
                {a.armed ? (
                  <button onClick={() => run(() => brokers.disarm(a.id), "Disarmed.")} disabled={busy} className="rounded bg-slate-800 hover:bg-slate-700 px-3 py-1.5 text-xs">Disarm</button>
                ) : (
                  <button onClick={() => run(() => brokers.arm(a.id), "Armed.")} disabled={busy} className="rounded bg-amber-900 hover:bg-amber-800 text-white px-3 py-1.5 text-xs">Arm</button>
                )}
                <button onClick={() => run(() => brokers.remove(a.id), "Deleted.")} disabled={busy} className="rounded bg-rose-900 hover:bg-rose-800 text-white px-3 py-1.5 text-xs">Delete</button>
              </div>
            </div>
            {loginFor === a.id && (
              <LoginFlow id={a.id} onDone={() => { setLoginFor(null); refetch(); }} />
            )}
            {a.has_session && <RefreshData id={a.id} />}
            {a.armed && !a.live_trading_enabled && (
              <div className="text-xs text-slate-500 mt-2">
                Armed, but server <code>SKAS_LIVE_TRADING_ENABLED</code> is false — still no real orders.
              </div>
            )}
          </Card>
        ))
      )}
    </div>
  );
}
