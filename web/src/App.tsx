import { useState } from "react";
import { Navigate, NavLink, Route, Routes, useLocation } from "react-router-dom";
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

const FULL_BLEED = new Set(["/", "/backtest", "/live", "/trade"]);

export default function App() {
  const path = useLocation().pathname;
  const isHome = FULL_BLEED.has(path) || /^\/live\/\d+$/.test(path); // donchian detail is full-bleed too
  return (
    <div className="min-h-screen">
      <header className="border-b border-slate-800 bg-slate-900/60 backdrop-blur sticky top-0 z-10">
        <div className="max-w-6xl mx-auto px-4 h-14 flex items-center gap-2">
          <NavLink to="/" className="flex items-center gap-2 font-semibold text-brand-light mr-4 hover:text-brand">
            <span className="w-6 h-6 rounded-[7px] bg-brand inline-block" />
            SKAS Algo
          </NavLink>
          <NavItem to="/" label="Home" />
          <NavItem to="/backtest" label="Backtest" />
          <NavItem to="/trade" label="Trade" />
          <NavItem to="/live" label="Live" />
          <NavItem to="/docs" label="Docs" />
          <NavItem to="/data" label="Data" />
          <NavItem to="/brokers" label="Brokers" />
          <div className="ml-auto"><ThemeToggle /></div>
        </div>
      </header>
      <main className={isHome ? "" : "max-w-6xl mx-auto px-4 py-6"}>
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/backtest" element={<BacktestPage />} />
          <Route path="/trade" element={<TradePage />} />
          <Route path="/live" element={<LivePage />} />
          <Route path="/live/new" element={<DeployPage />} />
          <Route path="/live/:id" element={<DonchianLivePage />} />
          <Route path="/docs" element={<StrategiesPage />} />
          <Route path="/analyze" element={<AnalysisPage />} />
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
    </div>
  );
}
