import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { Card } from "../components/ui";

interface Tile {
  to: string;
  icon: string;
  title: string;
  desc: string;
}

const TILES: Tile[] = [
  { to: "/backtest", icon: "🧪", title: "Backtest", desc: "Run backtests and review existing runs." },
  { to: "/trade", icon: "🛠️", title: "Trade", desc: "Build option strategies and screen setups (FibRet)." },
  { to: "/live", icon: "📡", title: "Live", desc: "Monitor active paper & live deployments." },
  { to: "/docs", icon: "📖", title: "Docs", desc: "Strategy rules, structure, entry / exit & risk." },
  { to: "/data", icon: "🗄️", title: "Data", desc: "Cached stocks, options & futures coverage." },
  { to: "/brokers", icon: "🔌", title: "Brokers", desc: "Connect & manage broker sessions." },
];

export default function HomePage() {
  const { data: live } = useQuery({ queryKey: ["live"], queryFn: api.liveList });
  const activeCount = (live ?? []).filter((r) => r.status === "active").length;

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold">SKAS Algo</h1>
        <p className="text-sm text-slate-400">An options &amp; equity trading platform — backtest, build, screen, deploy and monitor.</p>
      </div>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {TILES.map((t) => (
          <Link key={t.to} to={t.to} className="block group">
            <Card className="h-full transition-colors group-hover:border-brand/60 group-hover:bg-slate-800/40">
              <div className="flex items-start gap-3">
                <div className="text-2xl leading-none">{t.icon}</div>
                <div>
                  <div className="font-medium text-slate-200 flex items-center gap-2">
                    {t.title}
                    {t.to === "/live" && activeCount > 0 && (
                      <span className="rounded-full bg-emerald-900/50 text-emerald-300 text-[11px] px-2 py-0.5">{activeCount} active</span>
                    )}
                  </div>
                  <div className="text-sm text-slate-400 mt-0.5">{t.desc}</div>
                </div>
              </div>
            </Card>
          </Link>
        ))}
      </div>
    </div>
  );
}
