import { useEffect, useState } from "react";
import { api, setApiOrigin } from "@shared/api/client";
import { setToken } from "@shared/lib/auth";
import { getSetting, KEYS, setSetting } from "./../storage";

/** 01 · Login — per the design: centered column, gradient logo tile, field cards,
 * accent-deep CTA, Face ID row, paper-first footer note. First run also captures the
 * backend URL (the VPS's Tailscale HTTPS origin). */
export default function LoginScreen({ onAuthed }: { onAuthed: () => void }) {
  const [backend, setBackend] = useState("");
  const [password, setPassword] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getSetting(KEYS.backendUrl).then((v) => v && setBackend(v));
  }, []);

  async function signIn() {
    setBusy(true);
    setError(null);
    try {
      const origin = backend.trim().replace(/\/+$/, "");
      // Served from the backend itself (the /mobile mount, an http(s) page): an empty URL
      // means same-origin — exactly like the desktop app. The native shell (capacitor://)
      // has no meaningful same-origin, so there the URL stays required.
      const inBrowser = /^https?:$/.test(window.location.protocol);
      if (origin && !/^https?:\/\//.test(origin)) {
        throw new Error("Backend URL must start with https:// (the VPS tailnet address)");
      }
      if (!origin && !inBrowser) {
        throw new Error("Backend URL is required in the app (the VPS tailnet address)");
      }
      setApiOrigin(origin);
      await setSetting(KEYS.backendUrl, origin);
      try {
        const res = await api.login(password);
        setToken(res.access_token);
        await setSetting(KEYS.token, res.access_token);
      } catch (e) {
        // A fail-open backend (dev Mac without SKAS_AUTH_*) has no login endpoint enabled;
        // proceed with a sentinel — the VPS (auth ON) never hits this path.
        if (!String((e as Error).message).includes("not configured")) throw e;
        setToken("dev-open");
        await setSetting(KEYS.token, "dev-open");
      }
      onAuthed();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{
      minHeight: "100%", display: "flex", flexDirection: "column",
      justifyContent: "center", padding: "28px",
      paddingTop: "calc(28px + env(safe-area-inset-top))",
    }}>
      <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 10 }}>
        <div style={{
          width: 64, height: 64, borderRadius: 18,
          background: "linear-gradient(135deg, #12b3a4, #0d8a7e)",
          boxShadow: "0 10px 26px rgba(18,179,164,.32)",
          display: "flex", alignItems: "center", justifyContent: "center",
        }}>
          <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="#fff"
            strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
            <path d="M3 14l4-6 4 9 4-12 4 6" />
          </svg>
        </div>
        <div className="sg" style={{ fontWeight: 700, fontSize: 30 }}>SKAS Algo</div>
        <div style={{ fontWeight: 600, fontSize: 15, color: "var(--muted)" }}>
          Systematic strategies, live.
        </div>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 12, marginTop: 34 }}>
        <label style={fieldCard}>
          <span className="label">Backend URL</span>
          <input
            style={fieldInput}
            placeholder="https://vps.tailnet.ts.net — blank = this site"
            autoCapitalize="none"
            autoCorrect="off"
            inputMode="url"
            value={backend}
            onChange={(e) => setBackend(e.target.value)}
          />
        </label>
        <label style={fieldCard}>
          <span className="label">Password</span>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <input
              style={{ ...fieldInput, flex: 1 }}
              type={showPw ? "text" : "password"}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && !busy && signIn()}
            />
            <button onClick={() => setShowPw((s) => !s)} aria-label="show password"
              style={{ color: "var(--faint)", minWidth: 32, minHeight: 32 }}>
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none"
                stroke="currentColor" strokeWidth="2.1" strokeLinecap="round">
                <path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6-10-6-10-6z" />
                <circle cx="12" cy="12" r="2.6" />
              </svg>
            </button>
          </div>
        </label>

        {error && (
          <div style={{
            background: "var(--danger-bg)", color: "var(--danger)",
            borderRadius: 14, padding: "12px 14px", fontSize: 13.5, fontWeight: 600,
          }}>{error}</div>
        )}

        <button className="btn-primary" disabled={busy || !backend || !password}
          onClick={signIn}>
          {busy ? "Signing in…" : "Sign in"}
        </button>

        <button style={{
          display: "flex", alignItems: "center", justifyContent: "center", gap: 8,
          minHeight: 44, color: "var(--accent-deep)", fontWeight: 700, fontSize: 15.5,
        }}>
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            strokeWidth="2.1" strokeLinecap="round" strokeLinejoin="round">
            <path d="M7 3H5a2 2 0 0 0-2 2v2M17 3h2a2 2 0 0 1 2 2v2M7 21H5a2 2 0 0 1-2-2v-2M17 21h2a2 2 0 0 0 2-2v-2M9 9v1M15 9v1M9.5 15a3.5 3.5 0 0 0 5 0" />
          </svg>
          Unlock with Face ID
        </button>
      </div>

      <div style={{
        marginTop: 26, textAlign: "center", fontSize: 12.5, color: "var(--faint)",
      }}>
        Paper-first by design. Real orders need an armed broker session.
      </div>
    </div>
  );
}

const fieldCard: React.CSSProperties = {
  display: "flex", flexDirection: "column", gap: 5,
  background: "var(--field)", border: "1.5px solid var(--field-border)",
  borderRadius: 16, padding: "16px 18px",
};

const fieldInput: React.CSSProperties = {
  border: 0, outline: "none", background: "transparent",
  color: "var(--strong)", fontSize: 16.5, fontWeight: 600, width: "100%",
};
