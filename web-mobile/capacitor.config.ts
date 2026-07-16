import type { CapacitorConfig } from "@capacitor/cli";

// The iOS shell around the mobile web app. The webview origin is capacitor://localhost;
// every API call goes to the absolute backend origin configured on the Login screen
// (the VPS's Tailscale HTTPS address). CapacitorHttp patches fetch to native, which
// sidesteps webview CORS entirely (the backend's SKAS_CORS_ORIGINS entry is the fallback).
const config: CapacitorConfig = {
  appId: "com.skas.algo",
  appName: "SKAS Algo",
  webDir: "dist",
  plugins: {
    CapacitorHttp: { enabled: true },
  },
  ios: {
    contentInset: "never",
    backgroundColor: "#f6f8f8",
  },
};

export default config;
