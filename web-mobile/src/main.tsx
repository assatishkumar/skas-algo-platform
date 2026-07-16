import "@fontsource/manrope/500.css";
import "@fontsource/manrope/600.css";
import "@fontsource/manrope/700.css";
import "@fontsource/manrope/800.css";
import "@fontsource/space-grotesk/600.css";
import "@fontsource/space-grotesk/700.css";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import ReactDOM from "react-dom/client";
import { HashRouter } from "react-router-dom";
import App from "./App";
import "./tokens.css";

// System dark-mode → token swap (README: full token set for both themes).
const applyTheme = () => {
  const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = dark ? "dark" : "light";
};
applyTheme();
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);

const qc = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: true } },
});

// Surface any startup crash ON the page — a blank screen inside a webview is undebuggable
// on-device (no console). Only fires while React hasn't mounted; self-removes once the app
// is healthy and is tap-to-dismiss (an SPA never navigates away from a stale banner).
function showFatal(msg: string) {
  const root = document.getElementById("root");
  if (!root || root.childElementCount > 0) return; // app is up — not a startup failure
  const div = document.createElement("div");
  div.id = "fatal-banner";
  div.style.cssText = "margin:40px 20px;padding:16px;border-radius:14px;background:#fdeceb;" +
    "color:#d9544a;font:600 13px/1.5 -apple-system,sans-serif;word-break:break-word;" +
    "white-space:pre-wrap";
  div.textContent = `App failed to start (tap to dismiss):\n${msg}`;
  div.onclick = () => div.remove();
  document.body.appendChild(div);
}
window.addEventListener("error", (e) => showFatal(String(e.error?.stack ?? e.message)));
window.addEventListener("unhandledrejection", (e) => showFatal(String(e.reason)));

// HashRouter: inside the Capacitor webview there is no server-side fallback for deep paths.
try {
  ReactDOM.createRoot(document.getElementById("root")!).render(
    <React.StrictMode>
      <QueryClientProvider client={qc}>
        <HashRouter>
          <App />
        </HashRouter>
      </QueryClientProvider>
    </React.StrictMode>,
  );
} catch (e) {
  showFatal(String((e as Error).stack ?? e));
}
// Healthy mount → clear any banner left from a previous (pre-fix) load.
requestAnimationFrame(() => {
  const root = document.getElementById("root");
  if (root && root.childElementCount > 0) document.getElementById("fatal-banner")?.remove();
});
