import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { brokers } from "../api/client";

/** One-tap Zerodha login (owner 2026-10-03). The Kite app's redirect URL points here, so
 *  after signing in at Zerodha the phone lands on /brokers/callback?request_token=… — with
 *  the account id and the asking box's origin when the login link carried them
 *  (redirect_params, added by the login-url route). This page finishes the login itself.
 *
 *  - another allowed origin asked (e.g. the Mac's localhost UI, same Kite app) → hand the
 *    query to that box's own callback page, so the token lands where it was requested;
 *  - NO account in the link (a login started from a client that does not send return_to —
 *    the installed iOS app until it is rebuilt; the owner's first try, 2026-10-03) → the
 *    Zerodha account on this box when there is exactly one, else a choice;
 *  - exchange it here, once. The token is stashed in sessionStorage first and kept on a
 *    401: the app-login → back trip must not lose it. */
const STASH = "skas-kite-callback";
const ALLOWED = /^(https:\/\/[a-z0-9-]+(\.[a-z0-9-]+)*\.ts\.net|http:\/\/(localhost|127\.0\.0\.1)(:\d{1,5})?)$/;

type Acct = { id: number; label: string };
type State =
  | { kind: "working" }
  | { kind: "choose"; token: string; accounts: Acct[] }
  | { kind: "done"; label: string }
  | { kind: "error"; message: string };

function stash(token: string, account: number | null) {
  try { sessionStorage.setItem(STASH, JSON.stringify({ token, account })); } catch { /* private mode */ }
}
function unstash() {
  try { sessionStorage.removeItem(STASH); } catch { /* ignore */ }
}

export default function KiteCallbackPage() {
  const navigate = useNavigate();
  const ran = useRef(false);
  const [state, setState] = useState<State>({ kind: "working" });

  const exchange = (account: number, token: string) => {
    setState({ kind: "working" });
    stash(token, account);
    brokers.login(account, token)
      .then((acct) => {
        unstash();
        setState({ kind: "done", label: (acct as { label?: string })?.label ?? `account ${account}` });
        window.setTimeout(() => navigate("/brokers", { replace: true }), 2500);
      })
      .catch((e: Error) => {
        // A 401 is the APP's own login: the client is already taking us to /login and back
        // here — keep the token for that return. Anything else is final.
        if (!/^401\b/.test(e.message)) unstash();
        setState({ kind: "error", message: e.message });
      });
  };

  useEffect(() => {
    if (ran.current) return;
    ran.current = true;
    const q = new URLSearchParams(window.location.search);
    let token = q.get("request_token") ?? "";
    const rawAccount = q.get("account");
    let account: number | null = rawAccount ? Number(rawAccount) : null;
    const status = q.get("status");
    const returnTo = (q.get("return_to") ?? "").replace(/\/+$/, "");

    if (returnTo && returnTo !== window.location.origin && ALLOWED.test(returnTo)) {
      window.location.replace(`${returnTo}/brokers/callback${window.location.search}`);
      return;
    }
    if (!token) {                       // back from the app login with the query stripped?
      try {
        const saved = JSON.parse(sessionStorage.getItem(STASH) ?? "null");
        if (saved?.token) { token = saved.token; account = saved.account ?? null; }
      } catch { /* nothing stashed */ }
    }
    if (status && status !== "success") {
      setState({ kind: "error", message: `Zerodha returned "${status}" — the login was not completed.` });
      return;
    }
    if (!token) {
      setState({ kind: "error", message: "This link has no request_token. Start the login again from Brokers." });
      return;
    }
    if (account && Number.isFinite(account) && account > 0) {
      exchange(account, token);
      return;
    }
    // no account in the link: find this box's Zerodha account(s)
    stash(token, null);
    brokers.list()
      .then((all) => {
        const z = (all as { id: number; label: string; broker?: string }[])
          .filter((a) => (a.broker ?? "zerodha") === "zerodha")
          .map((a) => ({ id: a.id, label: a.label }));
        if (z.length === 1) exchange(z[0].id, token);
        else if (z.length > 1) setState({ kind: "choose", token, accounts: z });
        else setState({ kind: "error", message: "No Zerodha account is set up on this box." });
      })
      .catch((e: Error) => setState({ kind: "error", message: e.message }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="mx-auto mt-16 max-w-[460px] rounded-[16px] border border-[var(--border)] bg-[var(--card)] p-6 text-center">
      {state.kind === "working" && (
        <div className="text-[15px] font-bold text-[var(--strong)]">Finishing the Zerodha login…</div>
      )}
      {state.kind === "choose" && (
        <>
          <div className="text-[15px] font-bold text-[var(--strong)]">Which Zerodha account is this login for?</div>
          <div className="mt-3 flex flex-col gap-2">
            {state.accounts.map((a) => (
              <button key={a.id} onClick={() => exchange(a.id, state.token)}
                className="rounded-[11px] bg-[var(--accent)] px-4 py-2 text-[13px] font-extrabold text-white">
                {a.label}
              </button>
            ))}
          </div>
        </>
      )}
      {state.kind === "done" && (
        <>
          <div className="text-[17px] font-extrabold text-[var(--pos)]">Zerodha logged in</div>
          <div className="mt-1 text-[13px] text-[var(--muted)]">
            {state.label} — live runs reconnect in the background. Taking you to Brokers…
            (opened from the iOS app? tap Done to go back to it)
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
