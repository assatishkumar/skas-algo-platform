import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Navigate, NavLink, Route, Routes, useLocation } from "react-router-dom";
import { api } from "./api/client";
import { clearToken, isLoggedIn } from "./lib/auth";
import { applyTheme, getTheme, type Theme } from "./lib/theme";
import AnalysisPage from "./pages/AnalysisPage";
import BacktestPage from "./pages/BacktestPage";
import BrokersPage from "./pages/BrokersPage";
import ComparePage from "./pages/ComparePage";
import DataPage from "./pages/DataPage";
import DeployPage from "./pages/DeployPage";
import DonchianLivePage from "./pages/DonchianLivePage";
import HomePage from "./pages/HomePage";
import LivePage from "./pages/LivePage";
import LoginPage from "./pages/LoginPage";
import ResearchPage from "./pages/ResearchPage";
import RunDetailPage from "./pages/RunDetailPage";
import StrategiesPage from "./pages/StrategiesPage";
import TradePage from "./pages/TradePage";

function NavItem({ to, label }: { to: string; label: string }) {
  return (
    <NavLink
      to={to}
      end
      className={({ isActive }) =>
        `px-3 py-2 rounded-md text-sm font-medium ${
          isActive ? "bg-brand text-white" : "text-slate-300 hover:bg-slate-800"
        }`
      }
    >
      {label}
    </NavLink>
  );
}

function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(getTheme());
  const flip = () => {
    const next: Theme = theme === "dark" ? "light" : "dark";
    applyTheme(next);
    setTheme(next);
  };
  return (
    <button
      onClick={flip}
      title={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
      className="rounded-md px-2 py-1.5 text-sm text-slate-400 hover:bg-slate-800 hover:text-slate-200"
    >
      {theme === "dark" ? "☀️" : "🌙"}
    </button>
  );
}

/** Mobile-only bottom tab bar (md:hidden) — the installed-PWA navigation. Home indicator
 * safe-area padded; "More" opens a small sheet with the secondary destinations. Desktop
 * keeps the top nav untouched. */
const MORE_PATHS = ["/trade", "/data", "/brokers", "/docs"];

function TabIcon({ d }: { d: string }) {
  return (
    <svg viewBox="0 0 24 24" className="w-5 h-5" fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d={d} />
    </svg>
  );
}

function MobileTabBar() {
  const [moreOpen, setMoreOpen] = useState(false);
  const path = useLocation().pathname;
  const moreActive = MORE_PATHS.some((p) => path.startsWith(p));
  const tab = (active: boolean) =>
    `flex flex-col items-center justify-center gap-0.5 flex-1 py-2 text-[10px] font-medium ${
      active ? "text-brand-light" : "text-slate-400"
    }`;
  const item = ({ isActive }: { isActive: boolean }) => tab(isActive);
  return (
    <>
      {moreOpen && (
        // Backdrop + sheet — tap anywhere (or a link) to dismiss.
        <div className="md:hidden fixed inset-0 z-20" onClick={() => setMoreOpen(false)}>
          <div className="absolute inset-0 bg-black/40" />
          <div className="absolute bottom-[calc(3.75rem+env(safe-area-inset-bottom))] inset-x-3 rounded-2xl border border-slate-700 bg-slate-900 p-2 grid grid-cols-2 gap-1">
            {[["/trade", "Trade"], ["/data", "Data"], ["/brokers", "Brokers"], ["/docs", "Docs"]].map(([to, label]) => (
              <NavLink key={to} to={to} onClick={() => setMoreOpen(false)}
                className={({ isActive }) =>
                  `rounded-xl px-4 py-3 text-sm font-medium text-center ${
                    isActive ? "bg-brand text-white" : "text-slate-200 hover:bg-slate-800"
                  }`}>
                {label}
              </NavLink>
            ))}
          </div>
        </div>
      )}
      <nav className="md:hidden fixed bottom-0 inset-x-0 z-30 border-t border-slate-800 bg-slate-900/90 backdrop-blur pb-[env(safe-area-inset-bottom)]">
        <div className="flex items-stretch">
          <NavLink to="/" end className={item}>
            <TabIcon d="M3 10.5 12 3l9 7.5V21h-6v-6H9v6H3z" />Home
          </NavLink>
          <NavLink to="/live" className={item}>
            <TabIcon d="M3 12h4l3-8 4 16 3-8h4" />Live
          </NavLink>
          <NavLink to="/backtest" className={item}>
            <TabIcon d="M4 20V10m5.5 10V4m5.5 16v-7m5 7V7" />Backtest
          </NavLink>
          <NavLink to="/research" className={item}>
            <TabIcon d="M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zm10 17-4.8-4.8" />Research
          </NavLink>
          <button onClick={() => setMoreOpen((v) => !v)} className={tab(moreActive || moreOpen)}>
            <TabIcon d="M5 12h.01M12 12h.01M19 12h.01" />More
          </button>
        </div>
      </nav>
    </>
  );
}

const FULL_BLEED = new Set(["/", "/backtest", "/live", "/trade", "/brokers"]);

function LogoutButton() {
  // Only meaningful when a token exists (auth-enabled host). Hard redirect so all state resets.
  if (!isLoggedIn()) return null;
  return (
    <button
      onClick={() => {
        clearToken();
        window.location.assign("/login");
      }}
      title="Log out"
      className="rounded-md px-2 py-1.5 text-sm text-slate-400 hover:bg-slate-800 hover:text-slate-200"
    >
      ⎋
    </button>
  );
}

/** Quiet, glanceable indicator that the daily historical cache was refreshed (once each
 * trading morning, in the background). Non-intrusive — a small chip, no toast/modal. */
function DataStatusChip() {
  const { data } = useQuery({
    queryKey: ["live", "summary"],
    queryFn: api.liveSummary,
    refetchInterval: 5 * 60_000, // pick up the morning refresh within ~5 min
    staleTime: 60_000,
  });
  const r = data?.last_cache_refresh;
  if (!r) return null;
  const hhmm = new Date(r.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const title = `Historical data refreshed at ${hhmm} — ${r.ok}/${r.symbols} symbols`
    + (r.errors ? `, ${r.errors} error(s)` : "");
  return (
    <span title={title}
      className="hidden sm:inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-semibold text-slate-400">
      <span className={r.errors ? "text-amber-400" : "text-emerald-400"}>{r.errors ? "⚠" : "✓"}</span>
      Data {hhmm}
    </span>
  );
}

export default function App() {
  const path = useLocation().pathname;
  // Login is a standalone screen — no header/nav/tab-bar chrome behind it.
  if (path === "/login") return <LoginPage />;
  const isHome = FULL_BLEED.has(path) || /^\/live\/\d+$/.test(path); // donchian detail is full-bleed too
  return (
    <div className="min-h-screen">
      {/* pt-[env(...)]: the PWA draws under the translucent iOS status bar — pad the blur
          bar so content clears the clock/battery (env() = 0 in desktop browsers). */}
      <header className="border-b border-slate-800 bg-slate-900/60 backdrop-blur sticky top-0 z-10 pt-[env(safe-area-inset-top)]">
        <div className="max-w-6xl mx-auto px-4 h-14 flex items-center gap-2">
          <NavLink to="/" className="flex items-center gap-2 font-semibold text-brand-light mr-4 hover:text-brand">
            <span className="w-6 h-6 rounded-[7px] bg-brand inline-block" />
            SKAS Algo
          </NavLink>
          {/* Desktop nav — the phone gets the bottom tab bar instead. */}
          <nav className="hidden md:flex items-center gap-2">
            <NavItem to="/" label="Home" />
            <NavItem to="/backtest" label="Backtest" />
            <NavItem to="/trade" label="Trade" />
            <NavItem to="/live" label="Live" />
            <NavItem to="/docs" label="Docs" />
            <NavItem to="/research" label="Research" />
            <NavItem to="/data" label="Data" />
            <NavItem to="/brokers" label="Brokers" />
          </nav>
          <div className="ml-auto flex items-center gap-1"><DataStatusChip /><ThemeToggle /><LogoutButton /></div>
        </div>
      </header>
      <main className={`${isHome ? "" : "max-w-6xl mx-auto px-4 py-6"} pb-24 md:pb-0`}>
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/backtest" element={<BacktestPage />} />
          <Route path="/trade" element={<TradePage />} />
          <Route path="/live" element={<LivePage />} />
          <Route path="/live/new" element={<DeployPage />} />
          <Route path="/live/:id" element={<DonchianLivePage />} />
          <Route path="/docs" element={<StrategiesPage />} />
          <Route path="/analyze" element={<AnalysisPage />} />
          <Route path="/research" element={<ResearchPage />} />
          <Route path="/brokers" element={<BrokersPage />} />
          <Route path="/runs/:id" element={<RunDetailPage />} />
          <Route path="/compare" element={<ComparePage />} />
          <Route path="/data" element={<DataPage />} />
          {/* legacy routes → new IA */}
          <Route path="/new" element={<Navigate to="/backtest?tab=new" replace />} />
          <Route path="/runs" element={<Navigate to="/backtest" replace />} />
          <Route path="/strategies" element={<Navigate to="/docs" replace />} />
          <Route path="/fibret" element={<Navigate to="/trade?tab=screener" replace />} />
        </Routes>
      </main>
      <MobileTabBar />
    </div>
  );
}
