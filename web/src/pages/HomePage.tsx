import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { formatInr } from "../lib/format";

const ACCENT = "#0aa999"; // brand teal — chart line/area, primary CTA (spec: interchangeable w/ #12b3a4)

// ── ported from the mock's glyph() map: 24×24 viewBox, 2px round line icons ──────────────────
function Glyph({ name, color }: { name: string; color: string }) {
  const p = { fill: "none", stroke: color, strokeWidth: 2, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };
  const inner: Record<string, JSX.Element> = {
    backtest: (
      <>
        <path d="M3.5 12a8.5 8.5 0 1 0 2.2-5.7" {...p} />
        <polyline points="3 3 3.7 7.2 8 6.5" {...p} />
        <path d="M12 8v4l3 1.8" {...p} />
      </>
    ),
    trade: (
      <>
        <path d="M7 6v12" {...p} />
        <polyline points="4 9 7 6 10 9" {...p} />
        <path d="M17 18V6" {...p} />
        <polyline points="14 15 17 18 20 15" {...p} />
      </>
    ),
    live: <polyline points="3 12 7 12 10 5 14 19 17 12 21 12" {...p} />,
    docs: (
      <>
        <line x1="5" y1="7" x2="19" y2="7" {...p} />
        <line x1="5" y1="12" x2="19" y2="12" {...p} />
        <line x1="5" y1="17" x2="13" y2="17" {...p} />
      </>
    ),
    data: (
      <>
        <ellipse cx="12" cy="6" rx="7" ry="3" {...p} />
        <path d="M5 6v6c0 1.66 3.13 3 7 3s7-1.34 7-3V6" {...p} />
        <path d="M5 12v6c0 1.66 3.13 3 7 3s7-1.34 7-3v-6" {...p} />
      </>
    ),
    brokers: (
      <>
        <circle cx="7" cy="7" r="3" {...p} />
        <circle cx="17" cy="17" r="3" {...p} />
        <path d="M9.2 9.2l5.6 5.6" {...p} />
      </>
    ),
  };
  return (
    <svg width="23" height="23" viewBox="0 0 24 24">
      {inner[name]}
    </svg>
  );
}

// ── ported from the mock's buildPath(): cubic-bezier-smoothed line + area under it ───────────
function equityPath(W: number, H: number, vals: number[]) {
  const pad = 10;
  const n = vals.length;
  // Auto-scale to the data's own min/max so a real ₹-equity series (not just the 0..100 placeholder)
  // fills the box.
  const lo = Math.min(...vals);
  const span = Math.max(...vals) - lo || 1;
  const x = (i: number) => pad + (i * (W - 2 * pad)) / Math.max(1, n - 1);
  const y = (v: number) => H - pad - ((v - lo) * (H - 2 * pad)) / span;
  let d = `M${x(0).toFixed(1)} ${y(vals[0]).toFixed(1)}`;
  for (let i = 1; i < n; i++) {
    const cx = (x(i - 1) + x(i)) / 2;
    d += ` C${cx.toFixed(1)} ${y(vals[i - 1]).toFixed(1)} ${cx.toFixed(1)} ${y(vals[i]).toFixed(1)} ${x(i).toFixed(1)} ${y(vals[i]).toFixed(1)}`;
  }
  return { line: d, area: `${d} L${x(n - 1).toFixed(1)} ${H} L${x(0).toFixed(1)} ${H} Z` };
}

// Placeholder equity shape (spec) — replace with a real paper-equity series when available.
const EQ_SERIES = [14, 18, 15, 24, 30, 26, 35, 42, 38, 50, 47, 58, 55, 67, 74, 70, 82, 90];

interface CardDef {
  to: string;
  name: string;
  desc: string;
  glyph: string;
  color: string; // icon stroke
  tile: string; // glyph-tile bg (light + dark via dark:)
}

const CARDS: CardDef[] = [
  { to: "/backtest", name: "Backtest", desc: "Run backtests and review existing runs.", glyph: "backtest", color: "#12b3a4", tile: "bg-[#e6f7f4] dark:bg-[rgba(18,179,164,0.16)]" },
  { to: "/trade", name: "Trade", desc: "Build option strategies and screen setups (FibRet).", glyph: "trade", color: "#4f86f7", tile: "bg-[#e9f0fe] dark:bg-[rgba(79,134,247,0.16)]" },
  { to: "/live", name: "Live", desc: "Monitor active paper & live deployments.", glyph: "live", color: "#1eb980", tile: "bg-[#e6f6ef] dark:bg-[rgba(30,185,128,0.16)]" },
  { to: "/docs", name: "Docs", desc: "Strategy rules, structure, entry / exit & risk.", glyph: "docs", color: "#8b6cf0", tile: "bg-[#efebfd] dark:bg-[rgba(139,108,240,0.16)]" },
  { to: "/data", name: "Data", desc: "Cached stocks, options & futures coverage.", glyph: "data", color: "#f0a92e", tile: "bg-[#fdf3df] dark:bg-[rgba(240,169,46,0.16)]" },
  { to: "/brokers", name: "Brokers", desc: "Connect & manage broker sessions.", glyph: "brokers", color: "#f2776b", tile: "bg-[#fdecea] dark:bg-[rgba(242,119,107,0.16)]" },
];

// shared token classes
const cardSurface = "bg-white dark:bg-[#142926] border border-[#eaeeed] dark:border-[#23413c]";
const textStrong = "text-[#0f2723] dark:text-[#f1f6f5]";
const textMuted = "text-[#5c6f6b] dark:text-[#92aaa4]";
const sg = "font-['Space_Grotesk']";

function StatCard({ label, value, tone }: { label: string; value: string; tone?: number }) {
  const color = tone == null ? textStrong : tone >= 0 ? "text-[#0f9d63]" : "text-[#d9544a]";
  return (
    <div className={`${cardSurface} rounded-[15px] px-5 py-[18px]`}>
      <div className={`text-[13px] font-semibold mb-2 ${textMuted}`}>{label}</div>
      <span className={`${sg} text-[26px] font-bold tabular-nums ${color}`}>{value}</span>
    </div>
  );
}

export default function HomePage() {
  // Real aggregates across ACTIVE PAPER deployments (same source the Live page sums) — replaces the
  // old hardcoded hero/stat numbers.
  const { data: deps } = useQuery({
    queryKey: ["deployments", "active"],
    queryFn: () => api.liveDeployments("active"),
  });
  const paper = (deps ?? []).filter((d) => (d.mode || "").toUpperCase() === "PAPER");
  const sum = (pick: (m: (typeof paper)[number]["metrics"]) => number | null | undefined) =>
    paper.reduce((s, d) => s + (pick(d.metrics) ?? 0), 0);
  const equity = sum((m) => m.equity);
  const realized = sum((m) => m.realized_pnl);
  const pnl = realized + sum((m) => m.unrealized_pnl);
  const costBasis = equity - pnl;
  const pnlPct = costBasis > 0 ? (pnl / costBasis) * 100 : 0;
  const activeCount = paper.length;

  // Win rate, Sharpe and a real 30d equity sparkline (fills in as daily history accumulates).
  const { data: summary } = useQuery({ queryKey: ["live", "summary"], queryFn: api.liveSummary });
  const series = (summary?.equity_series?.length ?? 0) >= 2 ? summary!.equity_series : EQ_SERIES;
  const changePct = summary?.equity_change_pct_30d ?? pnlPct;
  const eq = equityPath(480, 160, series);

  return (
    <div className={`font-['Manrope'] bg-[#f6f8f8] dark:bg-[#0c1a18] min-h-[calc(100vh-3.5rem)] transition-colors`}>
      <div className="max-w-[1200px] mx-auto px-8">
        {/* ── Hero ── */}
        <div className="grid items-center gap-12 pt-[60px] pb-3 lg:grid-cols-[1fr_480px]">
          <div>
            <div className="inline-flex items-center gap-2 rounded-full px-[13px] py-1.5 text-[12.5px] font-bold mb-[22px] bg-[#e6f7f4] dark:bg-[rgba(18,179,164,0.16)] text-[#0d8a7e] dark:text-[#5fd8c9]">
              <span className="relative flex h-[7px] w-[7px]">
                <span className="absolute inline-flex h-full w-full rounded-full bg-[#1eb980] opacity-60 animate-ping" />
                <span className="relative inline-flex h-[7px] w-[7px] rounded-full bg-[#1eb980]" />
              </span>
              {activeCount} {activeCount === 1 ? "strategy" : "strategies"} running (paper)
            </div>
            <h1 className={`${sg} text-[48px] font-bold leading-[1.07] tracking-[-0.02em] mb-[18px] ${textStrong}`}>
              A calm workspace for systematic trading.
            </h1>
            <p className={`text-[17.5px] leading-[1.6] max-w-[450px] mb-[30px] ${textMuted}`}>
              Backtest, build, screen, deploy and monitor options &amp; equity strategies — without the noise.
            </p>
            <div className="flex flex-wrap gap-3">
              <Link
                to="/backtest?tab=new"
                className="inline-flex items-center gap-2 rounded-[13px] px-6 py-3.5 text-[15px] font-bold text-white shadow-[0_8px_20px_rgba(18,179,164,0.32)]"
                style={{ background: ACCENT }}
              >
                New backtest →
              </Link>
              <Link
                to="/live"
                className={`inline-flex items-center rounded-[13px] px-6 py-3.5 text-[15px] font-bold bg-white dark:bg-[#142926] border-[1.5px] border-[#eaeeed] dark:border-[#23413c] ${textStrong}`}
              >
                Open live monitor
              </Link>
            </div>
          </div>

          {/* equity card */}
          <div className={`${cardSurface} rounded-[20px] p-6 shadow-[0_16px_40px_rgba(15,39,35,0.08)] dark:shadow-[0_16px_40px_rgba(0,0,0,0.4)]`}>
            <div className="flex items-start justify-between mb-2.5">
              <div>
                <div className={`text-[13px] font-semibold ${textMuted}`}>Paper equity</div>
                <div className={`${sg} text-[30px] font-bold tabular-nums ${textStrong}`}>{formatInr(equity)}</div>
              </div>
              <span className={`rounded-full px-[11px] py-[5px] text-[13px] font-bold ${changePct >= 0 ? "bg-[#e6f6ef] dark:bg-[rgba(30,185,128,0.16)] text-[#0f9d63] dark:text-[#5fd8c9]" : "bg-[#fdecea] dark:bg-[rgba(242,119,107,0.16)] text-[#d9544a] dark:text-[#f0766a]"}`}>{changePct >= 0 ? "▲" : "▼"} {Math.abs(changePct).toFixed(1)}%</span>
            </div>
            <svg viewBox="0 0 480 160" className="w-full h-40 block">
              <defs>
                <linearGradient id="gHero" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0" stopColor={ACCENT} stopOpacity="0.22" />
                  <stop offset="1" stopColor={ACCENT} stopOpacity="0" />
                </linearGradient>
              </defs>
              <path d={eq.area} fill="url(#gHero)" />
              <path d={eq.line} fill="none" stroke={ACCENT} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>
        </div>

        {/* ── Stats strip ── */}
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 pt-7 pb-2">
          <StatCard label="Active paper strategies" value={String(activeCount)} />
          <StatCard label="Win rate" value={summary?.win_rate != null ? `${summary.win_rate.toFixed(0)}%` : "—"} />
          <StatCard label="Sharpe (30d)" value={summary?.sharpe_30d != null ? summary.sharpe_30d.toFixed(2) : "—"} />
          <StatCard label="Paper P&L" value={formatInr(pnl)} tone={pnl} />
        </div>

        {/* ── Workspace ── */}
        <div className="pt-9 pb-16">
          <div className={`${sg} text-[15px] font-bold mb-[18px] ${textStrong}`}>Workspace</div>
          <div className="grid gap-[18px] sm:grid-cols-2 lg:grid-cols-3">
            {CARDS.map((c) => (
              <Link
                key={c.to}
                to={c.to}
                className={`${cardSurface} rounded-[18px] p-6 block transition-[transform,box-shadow] duration-150 hover:-translate-y-[3px] hover:shadow-[0_16px_32px_rgba(15,39,35,0.08)] dark:hover:shadow-[0_16px_32px_rgba(0,0,0,0.4)]`}
              >
                <div className={`w-12 h-12 rounded-[14px] flex items-center justify-center mb-[18px] ${c.tile}`}>
                  <Glyph name={c.glyph} color={c.color} />
                </div>
                <div className={`${sg} text-[18px] font-bold mb-[7px] ${textStrong}`}>{c.name}</div>
                <div className={`text-[14.5px] leading-[1.5] ${textMuted}`}>{c.desc}</div>
              </Link>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
