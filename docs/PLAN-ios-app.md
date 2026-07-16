# PLAN: iOS App — Live (paper + real) & Broker Login

Status: **SUPERSEDED** (2026-07-16) — built as the 7-screen Capacitor app in `web-mobile/`
per the design handoff; see **`docs/MOBILE.md`** for the as-built architecture and the
build/install runbook. This document is kept for the original decision history.

Original status: **PROPOSED** (2026-06-29)
Scope owner: single-user (skas)
Related: `docs/PLAN-live-options-deployment.md`, `docs/PLAN-app-screens-redesign.md`

## 1. Goal & scope

A minimal native **iOS app** that lets me, from my phone:

1. **Monitor & control Live deployments** — both **paper** (forward-test) and **real** (armed/go-live) runs: see positions, MTM PnL, fills, run the per-strategy decision/refresh, intervene (book/trail/flatten), and watch the live WebSocket feed.
2. **Do the daily broker login** — the Kite request-token ritual (access tokens expire ~06:00 IST), so I can re-auth from the phone each morning instead of needing the laptop.

**Explicitly out of scope** (stays in the web app / desktop, deferred for mobile): Backtest, Data, Trade builders, Analysis, Compare. **Push notifications are deferred** — Telegram + PWA push already cover alerts; revisit later.

## 2. Decisions (locked with the user 2026-06-29)

| Area | Decision |
|------|----------|
| App approach | **Capacitor-wrap the existing React PWA** under `web/` — reuse ~100% of the current UI (`LivePage`, `BrokersPage`, `DeployPage` are already built against the API). No SwiftUI/RN rewrite. |
| Reachability | **Tailscale** private mesh VPN. Phone reaches the Mac's backend over the tailnet (MagicDNS name or 100.x IP). **No public internet exposure.** VPS deploy stays a later/Phase-5 option. |
| Auth | **Shared API bearer token** added to the backend, stored in **iOS Keychain**; **Face ID** to unlock the app and to re-confirm real-money actions (arm / go-live / flatten / manual order). |
| Distribution | Xcode → install on my own iPhone (free personal team to start; **TestFlight** if I enrol in the Apple Developer Program, $99/yr — avoids the 7-day resign). |

## 3. The one architectural change that actually matters

The web app today is **same-origin**: `web/src/api/client.ts` calls a **relative** base `const BASE = "/api/v1"`, and `liveWsUrl()` builds the socket URL from `window.location.host`. That works in dev (vite proxies `/api` → `localhost:8080`, `ws:true`) and in prod (backend serves the built assets at the same origin).

Inside a Capacitor app the web assets are served from **`capacitor://localhost`**, which is *not* the backend's origin. So every `/api/v1/...` call and the `window.location.host` WebSocket would resolve to the app shell, not the server.

**Fix (Stage 1): make the API base absolute and configurable at runtime** — point it at the backend's Tailscale address (e.g. `http://skas-mac.tailnet-xxxx.ts.net:8080`). This is the central refactor; everything else is plumbing. It is designed to be **a no-op for the existing web app** (base falls back to same-origin so dev + the desktop PWA keep working).

## 4. Stages (each independently shippable & testable, matching the platform's incremental ethos)

### Stage 0 — Backend hardening + Tailscale (no app yet)
The API currently has **no auth** (`api/deps.py` is just `get_db`); exposing it to a phone requires a token layer first.

- **API token**: add `api_token: str | None = None` to `config/settings.py` (env `SKAS_API_TOKEN`), and a FastAPI dependency `require_token` (Bearer header check, constant-time compare). Apply it to all `/api/v1` routers **except `health`** in `api/app.py`. For the WebSocket (`/live/ws`) read the token from a **query param** (`?token=...`) since webview sockets can't set headers.
- **CORS / native http**: add `capacitor://localhost` (and the tailnet origin) to `cors_origins`. Recommended: also enable **CapacitorHttp** (Capacitor 6 core) in Stage 2 so `fetch` is patched to native and **bypasses CORS + mixed-content** entirely — keep the CORS entry as a fallback.
- **Tailscale**: install on the Mac (backend host) and iPhone; confirm the phone hits `http://<magicdns>:8080/api/v1/health` (health stays unauthenticated for exactly this check). Backend already binds `0.0.0.0` (`SKAS_API_HOST=0.0.0.0`) — no change.
- **Tests**: request without token → 401; with token → 200; health stays open. Keep parity/other tests green.
- *Effort: ~half a day.*

### Stage 1 — Make the web app backend-URL & token aware
- Resolve an **`API_BASE`**: build-time `import.meta.env.VITE_API_BASE`, overridable at runtime via a value stored in **Capacitor Preferences / localStorage** (set on a Settings screen). Default `""` → same-origin (web/dev unchanged).
- Refactor `web/src/api/client.ts`:
  - `request()` prefixes `API_BASE` and injects `Authorization: Bearer <token>` (token read from secure storage).
  - `liveWsUrl()` derives `ws://`/`wss://` from `API_BASE` (not `window.location.host`) and appends `?token=`.
  - Central 401 handler → bounce to the setup/unlock screen.
- New lightweight **Settings screen**: Backend URL + API token (token write-through to Keychain in Stage 3).
- **Acceptance**: `npm run build` clean; the desktop PWA still works through the vite proxy with `API_BASE=""`.
- *Effort: ~1 day.*

### Stage 2 — Add Capacitor + the iOS shell
- `cd web && npm i -D @capacitor/cli && npm i @capacitor/core @capacitor/ios`; `npx cap init "SKAS Algo" com.skas.algo --web-dir=dist`.
- `capacitor.config.ts`: `webDir: "dist"`, enable `CapacitorHttp`, set iOS scheme; **App Transport Security** exception in `Info.plist` for the plain-`http`/`ws` Tailscale backend (`NSAppTransportSecurity` exception for the tailnet domain — required, iOS blocks cleartext by default).
- `npm run build && npx cap copy && npx cap open ios` → run in Simulator, then on-device.
- **Acceptance**: wrapped app loads the **Live** screen and lists deployments from the Tailscale backend; live WebSocket connects (positions/MTM update).
- *Effort: ~1 day (first Xcode/CocoaPods setup is the time sink).*

### Stage 3 — Native auth: Keychain + Face ID
- Biometric plugin (`capacitor-native-biometric` or `@aparajita/capacitor-biometric-auth`) + secure storage for the token (Keychain).
- **First-run setup**: enter Backend URL + API token → store token in Keychain (not localStorage).
- **App-open Face ID gate**; **biometric re-confirm** before sensitive money actions: `arm` / `go-live` / `flatten` / `manual-order` (these map to existing `brokers.arm`, `liveGoLive`, `liveFlatten`, `liveManualOrder`).
- **Acceptance**: cold open requires Face ID; token never in plaintext; go-live prompts biometrics.
- *Effort: ~1 day.*

### Stage 4 — Mobile UX for Live + Brokers + login ritual
- **Bottom-tab nav** for narrow viewports (Live · Brokers · Settings); hide the desktop-only tabs (Backtest/Data/Trade/etc.) on phone. The full nav in `web/src/App.tsx` stays for desktop.
- Responsive polish on **`LivePage`** cards (positions, fills feed, payoff chart) and **`BrokersPage`**; safe-area insets; map **pull-to-refresh** → `liveRefresh`.
- **Streamlined broker login**: use `@capacitor/browser` (in-app Safari) to open `brokers.loginUrl()`, then **auto-capture the `request_token`** from the Kite redirect (deep-link interception) and POST `brokers.login()` — falling back to the current manual paste. ⚠️ Needs the **Kite developer-console redirect URL** updated to something the app can intercept (custom scheme / universal link).
- **Paper-vs-real clarity**: prominent badge on each Live card; real (armed) actions visually distinct + biometric-gated (from Stage 3).
- *Effort: ~1–2 days.*

### Stage 5 — Distribution & polish
- App icon + splash (reuse the existing PWA `icon-512.png` / theme `#0f766e`), version string, build number.
- Personal-team install (expires weekly) **or** TestFlight (if enrolled). Document the `build → cap copy → archive` release step.
- *Effort: ~half a day + Apple account setup.*

## 5. Security & risk notes

- The API token is a **single shared secret** — rotate via `.env` (`SKAS_API_TOKEN`); **Tailscale is the primary boundary** (no public surface), the token is defence-in-depth.
- **Real-money stays double-gated**: account `armed` **and** `SKAS_LIVE_TRADING_ENABLED=true` — unchanged. The app adds a *third* gate (biometric confirm) on the trigger actions.
- iOS **ATS** will block cleartext to the Tailscale backend unless an Info.plist exception is added (Stage 2). Long-term cleaner option: a VPS with TLS (Phase-5 deploy) removes the ATS exception and the http→https mixed-content footgun.
- Daily Kite token still expires ~06:00 IST — the app makes the ritual nicer, it does **not** remove it.
- Pre-existing platform security debt (committed Kite secrets in sibling repos) is tracked separately in `skas-algo-platform` memory — not in this app's scope.

## 6. Open questions for the user (before Stage 2)

1. **Apple Developer Program** ($99/yr) for TestFlight, or start with the free personal-team (app reinstalled weekly)?
2. **Kite redirect URL**: OK to change the redirect URL in the Zerodha developer console to a deep link so login auto-captures the token? (Otherwise we keep the manual paste — still works.)
3. **Bundle id**: `com.skas.algo` OK, or a preferred reverse-domain?

## 7. Rough total

~4–6 focused days across stages 0–5, front-loaded by Stage 0/1 (the backend token + absolute-base refactor, both reusable if a VPS deploy happens later). Stage 0 and Stage 1 deliver value (a secured, URL-configurable API) even before the app exists.
