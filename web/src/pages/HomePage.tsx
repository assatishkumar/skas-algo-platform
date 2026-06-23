import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";

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
  const max = 100;
  const n = vals.length;
  const x = (i: number) => pad + (i * (W - 2 * pad)) / (n - 1);
  const y = (v: number) => H - pad - (v * (H - 2 * pad)) / max;
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

function StatCard({ label, value, delta }: { label: string; value: string; delta?: string }) {
  return (
    <div className={`${cardSurface} rounded-[15px] px-5 py-[18px]`}>
      <div className={`text-[13px] font-semibold mb-2 ${textMuted}`}>{label}</div>
      <div className="flex items-baseline gap-[9px]">
        <span className={`${sg} text-[26px] font-bold tabular-nums ${textStrong}`}>{value}</span>
        {delta && <span className="text-[13px] font-bold text-[#0f9d63]">{delta}</span>}
      </div>
    </div>
  );
}

export default function HomePage() {
  const { data: live } = useQuery({ queryKey: ["live"], queryFn: api.liveList });
  const activeCount = (live ?? []).filter((r) => r.status === "active").length;
  const eq = equityPath(480, 160, EQ_SERIES);

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
              {activeCount} {activeCount === 1 ? "strategy" : "strategies"} running live
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
                <div className={`text-[13px] font-semibold ${textMuted}`}>Paper equity · 30d</div>
                <div className={`${sg} text-[30px] font-bold tabular-nums ${textStrong}`}>₹124,300</div>
              </div>
              <span className="rounded-full px-[11px] py-[5px] text-[13px] font-bold bg-[#e6f6ef] dark:bg-[rgba(30,185,128,0.16)] text-[#0f9d63] dark:text-[#5fd8c9]">▲ 8.7%</span>
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
          <StatCard label="Strategies live" value={String(activeCount)} />
          <StatCard label="Win rate" value="63%" delta="+4.1%" />
          <StatCard label="Sharpe (30d)" value="1.84" delta="+0.2" />
          <StatCard label="Paper P&L" value="+₹24.3k" delta="+8.7%" />
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
