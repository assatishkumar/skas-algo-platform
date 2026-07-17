import react from "@vitejs/plugin-react";
import { resolve } from "node:path";
import { defineConfig } from "vite";

// The mobile companion app (Capacitor webview shell). Shares the desktop app's pure layers
// (api client, types, payoff/cycles/format libs) via the @shared alias — one repo, no drift.
// Deliberately NO vite-plugin-pwa: Capacitor is the shell; a second service worker inside
// the webview only causes stale-asset grief.
export default defineConfig({
  // Relative asset URLs: the SAME dist works inside the Capacitor shell (served at its
  // own root) AND nested under the backend's /mobile/ mount — an absolute "/assets/…"
  // would collide with the DESKTOP SPA's /assets there.
  base: "./",
  plugins: [react()],
  resolve: {
    alias: {
      "@shared": resolve(__dirname, "../web/src"),
    },
  },
  server: {
    host: true,
    fs: { allow: [resolve(__dirname, ".."), resolve(__dirname)] },
    // Browser-dev convenience only (the device app talks to an absolute Tailscale origin).
    proxy: {
      "/api": { target: "http://localhost:8080", changeOrigin: true, ws: true },
    },
  },
  build: { chunkSizeWarningLimit: 1600 },
});
