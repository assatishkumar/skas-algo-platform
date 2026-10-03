import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { brokers } from "../api/client";

/** One-tap Zerodha login (owner 2026-10-03). The Kite app's redirect URL points here, so
 *  after signing in at Zerodha the phone lands on /brokers/callback?request_token=… with the
 *  account id and the asking box's origin (redirect_params, added by the login-url route).
 *  This page finishes the login itself — the copy-paste of the request_token was the pain.
 *
 *  - another allowed origin asked (e.g. the Mac's localhost UI, same Kite app) → hand the
 *    query to that box's own callback page, so the token lands where it was requested;
 *  - otherwise exchange it here, once. The token is stashed in sessionStorage first: if the
 *    app's own login has expired, the 401 → /login → back trip must not lose it. */
const STASH = "skas-kite-callback";
const ALLOWED = /^(https:\/\/[a-z0-9-]+(\.[a-z0-9-]+)*\.ts\.net|http:\/\/(localhost|127\.0\.0\.1)(:\d{1,5})?)$/;

type State = { kind: "working" } | { kind: "done"; label: string } | { kind: "error"; message: string };

export default function KiteCallbackPage() {
  const navigate = useNavigate();
  const ran = useRef(false);
  const [state, setState] = useState<State>({ kind: "working" });

  useEffect(() => {
    if (ran.current) return;
    ran.current = true;
    const q = new URLSearchParams(window.location.search);
    let token = q.get("request_token") ?? "";
    let account = Number(q.get("account") ?? "");
    const status = q.get("status");
    const returnTo = (q.get("return_to") ?? "").replace(/\/+$/, "");

    if (returnTo && returnTo !== window.location.origin && ALLOWED.test(returnTo)) {
      window.location.replace(`${returnTo}/brokers/callback${window.location.search}`);
      return;
    }
    if (!token) {                       // back from the app login with the query stripped?
      try {
        const saved = JSON.parse(sessionStorage.getItem(STASH) ?? "null");
        if (saved?.token) { token = saved.token; account = Number(saved.account); }
      } catch { /* nothing stashed */ }
    }
    if (status && status !== "success") {
      setState({ kind: "error", message: `Zerodha returned "${status}" — the login was not completed.` });
      return;
    }
    if (!token || !Number.isFinite(account) || account <= 0) {
      setState({ kind: "error", message: "No request_token in this link. Start the login again from Brokers." });
      return;
    }
    try { sessionStorage.setItem(STASH, JSON.stringify({ token, account })); } catch { /* private mode */ }
    brokers.login(account, token)
      .then((acct) => {
        try { sessionStorage.removeItem(STASH); } catch { /* ignore */ }
        setState({ kind: "done", label: (acct as { label?: string })?.label ?? `account ${account}` });
        window.setTimeout(() => navigate("/brokers", { replace: true }), 2000);
      })
      .catch((e: Error) => {
        try { sessionStorage.removeItem(STASH); } catch { /* ignore */ }
        setState({ kind: "error", message: e.message });
      });
  }, [navigate]);

  return (
    <div className="mx-auto mt-16 max-w-[460px] rounded-[16px] border border-[var(--border)] bg-[var(--card)] p-6 text-center">
      {state.kind === "working" && (
        <div className="text-[15px] font-bold text-[var(--strong)]">Finishing the Zerodha login…</div>
      )}
      {state.kind === "done" && (
        <>
          <div className="text-[17px] font-extrabold text-[var(--pos)]">Zerodha logged in</div>
          <div className="mt-1 text-[13px] text-[var(--muted)]">
            {state.label} — live runs reconnect in the background. Taking you to Brokers…
          </div>
        </>
      )}
      {state.kind === "error" && (
        <>
          <div className="text-[16px] font-extrabold text-[var(--danger)]">Login not completed</div>
          <div className="mt-1 break-words text-[13px] text-[var(--muted)]">{state.message}</div>
          <Link to="/brokers" className="mt-4 inline-block rounded-[11px] bg-[var(--accent)] px-4 py-2 text-[13px] font-extrabold text-white">
            Back to Brokers
          </Link>
        </>
      )}
    </div>
  );
}
