import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { setToken } from "../lib/auth";

// Standalone login (rendered outside the app chrome by App.tsx). Reached when the server
// enforces auth and a request 401s, or directly. On the localhost/dev box where auth is off,
// nobody is redirected here — the API is open — so this only matters on an auth-enabled host.
export default function LoginPage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const next = params.get("next") || "/";
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await api.login(password);
      setToken(res.access_token);
      navigate(next, { replace: true });
    } catch (err) {
      setError((err as Error).message.replace(/^\d+:\s*/, "") || "Login failed");
      setBusy(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center px-4 bg-slate-950">
      <form
        onSubmit={submit}
        className="w-full max-w-sm rounded-2xl border border-slate-800 bg-slate-900 p-8 shadow-xl"
      >
        <div className="flex items-center gap-2 font-semibold text-brand-light mb-6">
          <span className="w-6 h-6 rounded-[7px] bg-brand inline-block" />
          SKAS Algo
        </div>
        <label className="block text-sm text-slate-400 mb-2">Operator password</label>
        <input
          type="password"
          autoFocus
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="w-full rounded-md bg-slate-800 border border-slate-700 px-3 py-2 text-sm text-slate-100 focus:border-brand outline-none"
          placeholder="••••••••"
        />
        {error && <p className="mt-3 text-sm text-rose-400">{error}</p>}
        <button
          type="submit"
          disabled={busy || !password}
          className="mt-5 w-full rounded-md bg-brand hover:bg-brand-light disabled:opacity-40 px-3 py-2 text-sm font-semibold text-white"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
