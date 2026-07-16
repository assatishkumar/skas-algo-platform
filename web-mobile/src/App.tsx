import { useEffect, useState } from "react";
import { Navigate, NavLink, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { setApiOrigin, setUnauthorizedHandler } from "@shared/api/client";
import { getToken, setToken } from "@shared/lib/auth";
import { getSetting, KEYS } from "./storage";
import AlertsScreen from "./screens/Alerts";
import BrokersScreen from "./screens/Brokers";
import DeploymentDetailScreen from "./screens/DeploymentDetail";
import LiveScreen from "./screens/Live";
import LoginScreen from "./screens/Login";
import TradeScreen from "./screens/Trade";

/** Bottom tab bar — Live · Trade · Brokers (24px stroke icons, active accent-deep). */
function TabBar() {
  const { pathname } = useLocation();
  const items = [
    {
      to: "/live", label: "Live",
      d: "M3 12h4l3-8 4 16 3-8h4", // pulse
    },
    {
      to: "/trade", label: "Trade",
      d: "M4 20V10m6 10V4m6 16v-7m4 7H2", // bars
    },
    {
      to: "/brokers", label: "Brokers",
      d: "M3 7h18v12H3zM3 11h18M16 15h2", // wallet-ish
    },
  ];
  // Hide on login + push screens per the design (detail/alerts are pushed pages).
  if (pathname === "/login" || pathname.startsWith("/live/") || pathname === "/alerts") {
    return null;
  }
  return (
    <nav className="tabbar">
      {items.map((it) => (
        <NavLink key={it.to} to={it.to}
          className={({ isActive }) => (isActive ? "active" : "")}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2"
            strokeLinecap="round" strokeLinejoin="round"><path d={it.d} /></svg>
          {it.label}
        </NavLink>
      ))}
    </nav>
  );
}

export default function App() {
  const navigate = useNavigate();
  const [booted, setBooted] = useState(false);
  const [authed, setAuthed] = useState(false);

  useEffect(() => {
    // Boot: restore the backend origin + token from durable storage, register the 401
    // handler (webview can't use window.location redirects to a server route).
    (async () => {
      const url = await getSetting(KEYS.backendUrl);
      if (url) setApiOrigin(url);
      const tok = await getSetting(KEYS.token);
      if (tok && !getToken()) setToken(tok); // mirror into localStorage for authHeaders()
      setAuthed(Boolean(tok || getToken()));
      setBooted(true);
    })();
    setUnauthorizedHandler(() => {
      setAuthed(false);
      navigate("/login");
    });
    return () => setUnauthorizedHandler(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (!booted) return null;

  return (
    <>
      <Routes>
        <Route path="/login" element={<LoginScreen onAuthed={() => { setAuthed(true); navigate("/live"); }} />} />
        <Route path="/live" element={authed ? <LiveScreen /> : <Navigate to="/login" replace />} />
        <Route path="/live/:id" element={authed ? <DeploymentDetailScreen /> : <Navigate to="/login" replace />} />
        <Route path="/trade" element={authed ? <TradeScreen /> : <Navigate to="/login" replace />} />
        <Route path="/brokers" element={authed ? <BrokersScreen /> : <Navigate to="/login" replace />} />
        <Route path="/alerts" element={authed ? <AlertsScreen /> : <Navigate to="/login" replace />} />
        <Route path="*" element={<Navigate to={authed ? "/live" : "/login"} replace />} />
      </Routes>
      <TabBar />
    </>
  );
}
