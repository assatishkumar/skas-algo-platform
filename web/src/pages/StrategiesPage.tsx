import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { STRATEGIES, META, GROUP_ORDER, type BiasKind } from "../lib/strategyDocs";


const BIAS_PILL: Record<BiasKind, { glyph: string; bg: string; fg: string }> = {
  neutral: { glyph: "≈", bg: "var(--chip)", fg: "var(--chip-text)" },
  income: { glyph: "₹", bg: "var(--warn-bg)", fg: "var(--warn-text)" },
  bull: { glyph: "↑", bg: "var(--ok-bg)", fg: "var(--ok-text)" },
  bear: { glyph: "↓", bg: "var(--rose-bg)", fg: "var(--rose-text)" },
};

const SECTION_DEF = [
  { key: "structure" as const, label: "Structure", accent: "var(--sec-struct)",
    icon: "M12 2 2 7l10 5 10-5-10-5z|M2 17l10 5 10-5|M2 12l10 5 10-5" },
  { key: "entry" as const, label: "Entry", accent: "var(--sec-entry)",
    icon: "M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4|pl:10 17 15 12 10 7|M15 12H3" },
  { key: "exit" as const, label: "Exit", accent: "var(--sec-exit)",
    icon: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4|pl:16 17 21 12 16 7|M21 12H9" },
];

function DocIcon({ d, size = 14, stroke = "currentColor" }: { d: string; size?: number; stroke?: string }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke={stroke}
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      {d.split("|").map((seg, i) =>
        seg.startsWith("pl:") ? <polyline key={i} points={seg.slice(3)} /> : <path key={i} d={seg} />)}
    </svg>
  );
}

/** Bullets beginning BUY/SELL/SKIP render that word as a side chip (option-leg reading). */
function Bullet({ text, accent }: { text: string; accent: string }) {
  const m = /^(BUY|SELL|SKIP)\b\s*/.exec(text);
  const chipStyle = m
    ? m[1] === "BUY"
      ? { background: "var(--ok-bg)", color: "var(--ok-text)" }
      : m[1] === "SELL"
        ? { background: "var(--warn-bg)", color: "var(--warn-text)" }
        : { background: "var(--chip)", color: "var(--faint)" }
    : null;
  return (
    <li className="flex items-start gap-2.5 text-sm text-[var(--muted)] leading-relaxed">
      {m && chipStyle ? (
        <span className="mt-0.5 inline-block min-w-[42px] rounded-[6px] px-1.5 py-0.5 text-center font-['Space_Grotesk'] text-[11px] font-bold"
          style={chipStyle}>{m[1]}</span>
      ) : (
        <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: accent }} />
      )}
      <span>{m ? text.slice(m[0].length) : text}</span>
    </li>
  );
}

export default function StrategiesPage() {
  const [sel, setSel] = useState(STRATEGIES[0].id);
  const [search, setSearch] = useState("");

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return STRATEGIES;
    return STRATEGIES.filter((r) =>
      r.name.toLowerCase().includes(q) || r.id.toLowerCase().includes(q) ||
      r.bias.toLowerCase().includes(q));
  }, [search]);

  const groups = useMemo(() => {
    const by = new Map<string, typeof STRATEGIES>();
    for (const r of filtered) {
      const g = META[r.id]?.group ?? "Other";
      if (!by.has(g)) by.set(g, []);
      by.get(g)!.push(r);
    }
    return [...GROUP_ORDER, "Other"].filter((g) => by.has(g)).map((g) => [g, by.get(g)!] as const);
  }, [filtered]);

  const cur = STRATEGIES.find((r) => r.id === sel) ?? STRATEGIES[0];
  const meta = META[cur.id];
  const nOpt = STRATEGIES.filter((r) => r.kind === "Options").length;
  const bias = BIAS_PILL[meta?.biasKind ?? "neutral"];

  return (
    <div className="font-['Manrope'] text-[var(--strong)]">
      {/* page header */}
      <div className="flex flex-wrap items-end gap-3">
        <div className="min-w-0 max-w-[680px]">
          <div className="flex items-center gap-2.5">
            <span className="flex h-8 w-8 items-center justify-center rounded-[9px]"
              style={{ background: "var(--doc-bg)", color: "var(--doc)" }}>
              <DocIcon size={16} d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z|M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z" />
            </span>
            <h1 className="font-['Space_Grotesk'] text-[27px] font-bold m-0">Strategy docs</h1>
          </div>
          <p className="mt-1 text-sm text-[var(--muted)]">
            Structure, entry, exit and risk for every strategy on the platform. These mirror the
            engine's implementations — tune the parameters per deployment. (The Trade builders'
            custom positions and the broker smoke-test probe are tools, not strategies, so they
            aren't listed here.)
          </p>
        </div>
        <div className="ml-auto flex items-center rounded-[12px] border border-[var(--border)] bg-[var(--card)] px-3.5 py-2 text-[12px] font-semibold text-[var(--muted)]">
          <span className="mr-3 flex items-baseline gap-1.5">
            <span className="font-['Space_Grotesk'] text-[17px] font-bold tabular-nums text-[var(--strong)]">{STRATEGIES.length}</span>
            strategies
          </span>
          <span className="mr-3 h-4 w-px bg-[var(--divider)]" />
          <span className="flex items-baseline gap-1.5">
            <span className="font-['Space_Grotesk'] text-[17px] font-bold tabular-nums" style={{ color: "var(--doc)" }}>{nOpt}</span>
            options · {STRATEGIES.length - nOpt} equity
          </span>
        </div>
      </div>

      {/* master-detail */}
      <div className="mt-5 grid items-start gap-6 lg:grid-cols-[296px_1fr]">
        {/* index */}
        <div className="overflow-hidden rounded-[18px] border border-[var(--border)] bg-[var(--card)] lg:sticky lg:top-[92px]">
          <div className="border-b border-[var(--divider)] p-3">
            <div className="flex items-center gap-2 rounded-[11px] border border-[var(--field-border)] bg-[var(--field)] px-3 py-2 focus-within:border-[var(--doc)] focus-within:ring-[3px] focus-within:ring-[var(--doc-ring)]">
              <DocIcon size={13} d="M21 21l-4.3-4.3|M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16z" stroke="var(--faint)" />
              <input className="w-full bg-transparent text-sm text-[var(--strong)] placeholder:text-[var(--faint)] focus:outline-none"
                placeholder="Search strategies" value={search} onChange={(e) => setSearch(e.target.value)} />
            </div>
          </div>
          <div className="max-h-[calc(100vh-200px)] overflow-y-auto p-2">
            {groups.length === 0 && (
              <div className="px-3 py-4 text-sm text-[var(--faint)]">No strategy matches "{search}".</div>
            )}
            {groups.map(([g, rows]) => (
              <div key={g} className="mb-1.5">
                <div className="px-3 pb-1 pt-2 text-[10.5px] font-bold uppercase tracking-[0.08em] text-[var(--faint)]">{g}</div>
                {rows.map((r) => {
                  const active = r.id === sel;
                  const b = BIAS_PILL[META[r.id]?.biasKind ?? "neutral"];
                  return (
                    <button key={r.id} onClick={() => setSel(r.id)}
                      className="flex w-full items-center gap-2.5 rounded-[11px] px-3 py-2 text-left hover:bg-[var(--row-hover)]"
                      style={{
                        borderLeft: `3px solid ${active ? "var(--doc)" : "transparent"}`,
                        background: active ? "var(--doc-bg)" : undefined,
                      }}>
                      <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: b.fg }} />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate font-['Space_Grotesk'] text-[13.5px] font-bold"
                          style={{ color: active ? "var(--doc)" : "var(--strong)" }}>{r.name}</span>
                        <span className="block truncate font-['Space_Grotesk'] text-[10.5px] text-[var(--faint)]">{r.id}</span>
                      </span>
                      <span className="shrink-0 rounded-full px-2 py-0.5 text-[10.5px] font-semibold"
                        style={r.kind === "Options"
                          ? { background: "var(--doc-bg)", color: "var(--doc)" }
                          : { background: "var(--ok-bg)", color: "var(--ok-text)" }}>
                        {r.kind}
                      </span>
                    </button>
                  );
                })}
              </div>
            ))}
          </div>
        </div>

        {/* detail pane */}
        <div className="overflow-hidden rounded-[20px] border border-[var(--border)] bg-[var(--card)]">
          <div className="h-[5px]" style={{ background: "linear-gradient(90deg, var(--doc), var(--accent))" }} />
          <div className="px-5 pb-8 pt-6 sm:px-8 sm:pt-7">
            {/* header */}
            <div className="flex flex-wrap items-center gap-2.5">
              <h2 className="font-['Space_Grotesk'] text-[25px] font-bold m-0">{cur.name}</h2>
              <span className="rounded-full px-2.5 py-1 text-[11px] font-bold"
                style={cur.kind === "Options"
                  ? { background: "var(--doc-bg)", color: "var(--doc)" }
                  : { background: "var(--ok-bg)", color: "var(--ok-text)" }}>{cur.kind}</span>
              <span className="inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-[11px] font-bold"
                style={{ background: bias.bg, color: bias.fg }}>
                <span>{bias.glyph}</span>{cur.bias.split("·")[0].trim()}
              </span>
              <code className="ml-auto rounded-[8px] bg-[var(--chip)] px-2.5 py-1 font-['Space_Grotesk'] text-[13px] font-semibold text-[var(--chip-text)]">{cur.id}</code>
            </div>
            <p className="mt-2 max-w-[820px] text-[15px] leading-[1.65] text-[var(--muted)]">{cur.summary}</p>
            {cur.links && (
              <div className="mt-1.5 flex flex-wrap gap-3">
                {cur.links.map((l) => (
                  <a key={l.url} href={l.url} target="_blank" rel="noreferrer"
                    className="text-xs underline underline-offset-2" style={{ color: "var(--doc)" }}>
                    {l.label} ↗
                  </a>
                ))}
              </div>
            )}

            {/* at a glance */}
            {meta && (
              <div className="mt-5 overflow-hidden rounded-[15px] border border-[var(--border)] bg-[var(--stat)]">
                <div className="grid grid-cols-2 sm:grid-cols-3">
                  {meta.facts.map(([label, value], i) => (
                    <div key={label} className={`px-4 py-3 ${i >= 3 ? "border-t border-[var(--divider)]" : ""} ${i % 3 !== 0 ? "sm:border-l sm:border-[var(--divider)]" : ""} ${i % 2 !== 0 ? "max-sm:border-l max-sm:border-[var(--divider)]" : ""} ${i >= 2 ? "max-sm:border-t max-sm:border-[var(--divider)]" : ""}`}>
                      <div className="text-[10.5px] font-bold uppercase tracking-[0.08em] text-[var(--faint)]">{label}</div>
                      <div className="mt-0.5 font-['Space_Grotesk'] text-[15px] font-semibold text-[var(--strong)]">{value}</div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* sections */}
            <div className="mt-4">
              {SECTION_DEF.map((sec) => (
                <div key={sec.key} className="grid gap-2 border-b border-[var(--divider)] py-4 sm:grid-cols-[150px_1fr] sm:gap-0">
                  <div className="flex items-center gap-2.5 self-start">
                    <span className="flex h-[30px] w-[30px] items-center justify-center rounded-[9px]"
                      style={{ background: "color-mix(in srgb, " + sec.accent + " 12%, transparent)", color: sec.accent }}>
                      <DocIcon d={sec.icon} />
                    </span>
                    <span className="font-['Space_Grotesk'] text-[13.5px] font-bold">{sec.label}</span>
                  </div>
                  <ul className="space-y-2.5">
                    {cur[sec.key].map((t, i) => <Bullet key={i} text={t} accent={sec.accent} />)}
                  </ul>
                </div>
              ))}
              {/* risk */}
              <div className="grid gap-2 py-4 sm:grid-cols-[150px_1fr] sm:gap-0">
                <div className="flex items-center gap-2.5 self-start">
                  <span className="flex h-[30px] w-[30px] items-center justify-center rounded-[9px]"
                    style={{ background: "color-mix(in srgb, var(--sec-risk) 12%, transparent)", color: "var(--sec-risk)" }}>
                    <DocIcon d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z|M12 9v4|M12 17h.01" />
                  </span>
                  <span className="font-['Space_Grotesk'] text-[13.5px] font-bold">Risk</span>
                </div>
                <p className="text-sm leading-[1.65] text-[var(--muted)]">{cur.risk}</p>
              </div>
            </div>

            {/* deploy footer */}
            {meta && (
              <div className="mt-2 flex flex-wrap items-center gap-3 rounded-[12px] px-4 py-3"
                style={{ background: "var(--doc-bg)", border: "1px solid var(--doc-border)" }}>
                <DocIcon size={15} d="M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z|M12 16v-4|M12 8h.01" stroke="var(--doc)" />
                <span className="text-[12.5px] text-[var(--muted)]">{meta.deployNote}</span>
                {meta.deployCta && (
                  <Link to={meta.deployCta.to} className="ml-auto inline-flex items-center gap-1.5 text-[12.5px] font-bold"
                    style={{ color: "var(--doc)" }}>
                    {meta.deployCta.label}
                    <DocIcon size={13} d="M5 12h14|pl:12 5 19 12 12 19" stroke="var(--doc)" />
                  </Link>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
