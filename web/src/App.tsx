import { NavLink, Route, Routes } from "react-router-dom";
import NewBacktestPage from "./pages/NewBacktestPage";
import RunDetailPage from "./pages/RunDetailPage";
import RunsPage from "./pages/RunsPage";

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

export default function App() {
  return (
    <div className="min-h-screen">
      <header className="border-b border-slate-800 bg-slate-900/60 backdrop-blur sticky top-0 z-10">
        <div className="max-w-6xl mx-auto px-4 h-14 flex items-center gap-2">
          <span className="font-semibold text-brand-light mr-4">SKAS Algo</span>
          <NavItem to="/" label="Runs" />
          <NavItem to="/new" label="New backtest" />
        </div>
      </header>
      <main className="max-w-6xl mx-auto px-4 py-6">
        <Routes>
          <Route path="/" element={<RunsPage />} />
          <Route path="/new" element={<NewBacktestPage />} />
          <Route path="/runs/:id" element={<RunDetailPage />} />
        </Routes>
      </main>
    </div>
  );
}
