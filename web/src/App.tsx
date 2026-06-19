import { useState } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import { applyTheme, getTheme, type Theme } from "./lib/theme";
import BrokersPage from "./pages/BrokersPage";
import ComparePage from "./pages/ComparePage";
import DataPage from "./pages/DataPage";
import DeployPage from "./pages/DeployPage";
import LivePage from "./pages/LivePage";
import NewBacktestPage from "./pages/NewBacktestPage";
import AnalysisPage from "./pages/AnalysisPage";
import RunDetailPage from "./pages/RunDetailPage";
import RunsPage from "./pages/RunsPage";
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

export default function App() {
  return (
    <div className="min-h-screen">
      <header className="border-b border-slate-800 bg-slate-900/60 backdrop-blur sticky top-0 z-10">
        <div className="max-w-6xl mx-auto px-4 h-14 flex items-center gap-2">
          <NavLink to="/" className="font-semibold text-brand-light mr-4 hover:text-brand">
            SKAS Algo
          </NavLink>
          <NavItem to="/" label="Runs" />
          <NavItem to="/new" label="New backtest" />
          <NavItem to="/trade" label="Trade" />
          <NavItem to="/live" label="Live" />
          <NavItem to="/analyze" label="Analyze" />
          <NavItem to="/strategies" label="Strategies" />
          <NavItem to="/data" label="Data" />
          <NavItem to="/brokers" label="Brokers" />
          <div className="ml-auto"><ThemeToggle /></div>
        </div>
      </header>
      <main className="max-w-6xl mx-auto px-4 py-6">
        <Routes>
          <Route path="/" element={<RunsPage />} />
          <Route path="/new" element={<NewBacktestPage />} />
          <Route path="/trade" element={<TradePage />} />
          <Route path="/live" element={<LivePage />} />
          <Route path="/live/new" element={<DeployPage />} />
          <Route path="/analyze" element={<AnalysisPage />} />
          <Route path="/strategies" element={<StrategiesPage />} />
          <Route path="/brokers" element={<BrokersPage />} />
          <Route path="/runs/:id" element={<RunDetailPage />} />
          <Route path="/compare" element={<ComparePage />} />
          <Route path="/data" element={<DataPage />} />
        </Routes>
      </main>
    </div>
  );
}
