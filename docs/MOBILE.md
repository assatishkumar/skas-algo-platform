# Mobile companion app (iOS · Capacitor)

A dedicated 7-screen iPhone app (Login · Live · Deployment Detail · Trade Screener ·
Trade Chain · Brokers · Alerts) for monitoring and acting on the VPS's live/paper
deployments. Built per the design handoff (`design_handoff_mobile_app`, 2026-07); the
implementation lives in **`web-mobile/`** (Vite + React) with the **`ios/`** Capacitor 8
shell inside it.

## Architecture
- **Shares the desktop app's pure layers** via the `@shared` alias → `web/src/{api,lib,types}`:
  the API client, payoff/breakeven math (`lib/payoff.ts`), cycle reconstruction
  (`lib/optionCycles.ts`), formatters and option-symbol rendering. Types never drift.
- **Absolute backend origin**: the webview's origin is `capacitor://localhost`, so the
  client seam (`web/src/api/client.ts`) got `setApiOrigin()` / origin-aware `liveWsUrl()` /
  `setUnauthorizedHandler()` — all **no-ops for the desktop web app** (default = same-origin).
  The Login screen captures the VPS's Tailscale HTTPS address once (stored in Capacitor
  Preferences).
- **Live data**: seed `GET /live`, then WebSocket `snapshot` merges (`?token=` auth) — the
  desktop LivePage pattern. Paper/Real toggle keys off the snapshot's `mode` field.
- **Alerts**: the backend's in-app alert store (`notify/in_app.py` → `alert` table,
  `GET /alerts`, `POST /alerts/mark-read`); WS `{"type":"alert"}` bumps the bell badge.
- No service worker (Capacitor is the shell); fonts bundled via @fontsource.

## Safety
The app adds **no new order paths** — it calls the same audited endpoints behind the same
gates (armed ∧ `SKAS_LIVE_TRADING_ENABLED` ∧ LIVE mode). On top: Square-off and Pause show
confirm dialogs; **ARM requires typing `ARM`**; a **LIVE chain deploy requires typing
`REAL`**; everything else defaults to PAPER. All actions are the owner's hand.

## Server prerequisites (VPS)
- Auth enabled (`SKAS_AUTH_PASSWORD_HASH` + `SKAS_AUTH_JWT_SECRET`) — already the VPS norm.
- `SKAS_CORS_ORIGINS` must include `capacitor://localhost` (CapacitorHttp native fetch
  usually bypasses CORS, this is the belt): e.g.
  `SKAS_CORS_ORIGINS=["capacitor://localhost","http://localhost:5173"]`.
- Reachability: the iPhone joins the same Tailscale tailnet; the backend URL is the
  `https://<vps>.<tailnet>.ts.net` origin that `tailscale serve` exposes.

## Develop (browser, no Xcode)
```bash
cd web-mobile && npm install && npm run dev   # http://localhost:5174-ish
# The vite proxy forwards /api to a local backend; on the Login screen enter the VPS
# origin instead to hit the real box (CORS entry required for browser dev).
```

## Build & install on the iPhone (free personal team)
```bash
cd web-mobile
npm run build && npx cap sync ios
npx cap open ios          # opens Xcode
# Xcode: select your personal team under Signing & Capabilities → run on the device.
```
Free-team signatures expire after **7 days** — re-run from Xcode weekly, or enrol in the
Apple Developer Program ($99/yr) and distribute via TestFlight (no weekly expiry, enables
push notifications later).

## Known next steps
- **Face ID gate**: the Login screen has the button; wiring a biometric plugin
  (cap-8-compatible) is the next increment. Until then the JWT (24 h) in Preferences is
  the session.
- **Kite deep-link login**: today the daily ritual is open-browser + paste the
  `request_token`; auto-capture needs the Kite console redirect URL changed to a link the
  app can intercept.
- Push notifications (needs TestFlight/dev-program signing).
