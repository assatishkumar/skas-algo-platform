import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { VitePWA } from "vite-plugin-pwa";

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      includeAssets: ["favicon.svg"],
      manifest: {
        name: "SKAS Algo Platform",
        short_name: "SKAS Algo",
        description: "Backtest, forward-test, and live trading from one engine.",
        theme_color: "#0f766e",
        background_color: "#0b1220",
        display: "standalone",
        start_url: "/",
        icons: [
          { src: "icon-192.png", sizes: "192x192", type: "image/png" },
          { src: "icon-512.png", sizes: "512x512", type: "image/png" },
          {
            src: "icon-512.png",
            sizes: "512x512",
            type: "image/png",
            purpose: "maskable",
          },
        ],
      },
    }),
  ],
  server: {
    port: 5173,
    // Listen on all interfaces so the phone can hit http://100.66.208.5:5173 over
    // Tailscale directly (the backend already binds 0.0.0.0). The HTTPS path via
    // `tailscale serve` remains the better one — that's what makes the PWA installable.
    host: true,
    // Tailscale serve fronts the dev server at https://<mac>.tail54707b.ts.net (HTTPS =
    // installable PWA on the phone; tailnet-only, never public). Vite 5.4.12+ rejects
    // unknown Host headers, so allow the tailnet domain; Vite itself stays localhost-bound.
    allowedHosts: [".ts.net"],
    proxy: {
      // ws: true so the live WebSocket (/api/v1/live/ws) is proxied too.
      "/api": { target: "http://localhost:8080", changeOrigin: true, ws: true },
    },
  },
});
