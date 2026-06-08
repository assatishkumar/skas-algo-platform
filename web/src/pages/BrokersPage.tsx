import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { brokers } from "../api/client";
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
  password: "",
  totp_secret: "",
};

export default function BrokersPage() {
  const { data, isLoading, error, refetch } = useQuery({ queryKey: ["brokers"], queryFn: brokers.list });
  const [form, setForm] = useState<BrokerConnectRequest>(EMPTY);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

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

      <div className="rounded-lg border border-amber-800 bg-amber-950/30 text-amber-300 p-3 text-sm">
        ⚠️ Automating Zerodha login is against Kite ToS; credentials are stored encrypted (Fernet).
        Connecting + login are for live market data. <b>Real orders never fire</b> unless an account is
        <b> armed</b> and the server has <code>SKAS_LIVE_TRADING_ENABLED=true</code>.
      </div>

      <Card>
        <div className="text-sm font-medium text-slate-300 mb-3">Connect a broker account</div>
        <div className="grid md:grid-cols-3 gap-3">
          <input className={inputClass} placeholder="label" value={form.label} onChange={set("label")} />
          <input className={inputClass} placeholder="user id" value={form.user_id} onChange={set("user_id")} />
          <input className={inputClass} placeholder="api key" value={form.api_key} onChange={set("api_key")} />
          <input className={inputClass} placeholder="api secret" value={form.api_secret} onChange={set("api_secret")} />
          <input className={inputClass} type="password" placeholder="password" value={form.password} onChange={set("password")} />
          <input className={inputClass} type="password" placeholder="TOTP secret" value={form.totp_secret} onChange={set("totp_secret")} />
        </div>
        <button
          onClick={() => run(() => brokers.connect(form).then(() => setForm(EMPTY)), "Connected (stored encrypted).")}
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
                {a.armed ? <span className="text-amber-400 text-xs font-semibold">ARMED</span> : <Badge>disarmed</Badge>}
              </div>
              <div className="flex gap-2">
                <button onClick={() => run(() => brokers.login(a.id), "Logged in.")} disabled={busy} className="rounded bg-slate-800 hover:bg-slate-700 px-3 py-1.5 text-xs">Login</button>
                {a.armed ? (
                  <button onClick={() => run(() => brokers.disarm(a.id), "Disarmed.")} disabled={busy} className="rounded bg-slate-800 hover:bg-slate-700 px-3 py-1.5 text-xs">Disarm</button>
                ) : (
                  <button onClick={() => run(() => brokers.arm(a.id), "Armed.")} disabled={busy} className="rounded bg-amber-900 hover:bg-amber-800 px-3 py-1.5 text-xs">Arm</button>
                )}
                <button onClick={() => run(() => brokers.remove(a.id), "Deleted.")} disabled={busy} className="rounded bg-rose-900 hover:bg-rose-800 px-3 py-1.5 text-xs">Delete</button>
              </div>
            </div>
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
