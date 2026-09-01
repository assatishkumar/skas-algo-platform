import { useState } from "react";
import { portfolio as papi } from "../../api/client";
import type { Holding, TagRow } from "../../lib/portfolio";

const PALETTE = [
  "#12b3a4", "#5b62e8", "#e8a13c", "#0f9d63", "#d9544a",
  "#8b90f2", "#b07d10", "#c2661d", "#7a8a86", "#0d8a7e",
];

/** Tags on a holding: pick from what exists, or type a new one.
 *
 * Many-to-many by design — a fund can be "child's education" AND "long term" AND "review
 * 2027" at once. That is why tags are kept SEPARATE from the asset class, which stays one per
 * holding: rebalancing has to count each rupee exactly once, and a holding in two classes
 * would be counted twice or not at all.
 */
export default function TagPicker({
  holding, tags, onChanged,
}: { holding: Holding; tags: TagRow[]; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState("");
  const [error, setError] = useState("");
  const applied = new Set(holding.tags.map((t) => t.id));

  const setTags = async (ids: number[]) => {
    setBusy(true);
    setError("");
    try {
      await papi.setHoldingTags(holding.id, ids);
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const create = async () => {
    const name = creating.trim();
    if (!name) return;
    setBusy(true);
    setError("");
    try {
      const made = await papi.createTag(name, PALETTE[tags.length % PALETTE.length]);
      await papi.setHoldingTags(holding.id, [...applied, made.id]);
      setCreating("");
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-[12px] border border-[var(--border)] bg-[var(--stat)] p-3">
      <div className="mb-2 text-[11px] font-extrabold tracking-[.05em] text-[var(--faint)]">
        TAGS
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        {tags.length === 0 && (
          <span className="text-[12px] font-semibold text-[var(--faint)]">
            No tags yet — type one below.
          </span>
        )}
        {tags.map((t) => {
          const on = applied.has(t.id);
          return (
            <button
              key={t.id}
              disabled={busy}
              onClick={() => setTags(
                on ? [...applied].filter((i) => i !== t.id) : [...applied, t.id],
              )}
              title={on ? `Remove ${t.name}` : `Apply ${t.name}`}
              className="inline-flex items-center gap-1.5 rounded-full border-[1.5px] px-3 py-1.5 text-[11.5px] font-extrabold disabled:opacity-50"
              style={{
                background: on ? `${t.color}22` : "var(--card)",
                borderColor: on ? t.color : "var(--border)",
                color: on ? "var(--strong)" : "var(--faint)",
              }}
            >
              <span className="inline-block h-2 w-2 rounded-full" style={{ background: t.color }} />
              {t.name}
              {on && <span className="opacity-60">✕</span>}
            </button>
          );
        })}
      </div>

      <div className="mt-2.5 flex items-center gap-2">
        <input
          value={creating}
          onChange={(e) => setCreating(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void create(); } }}
          placeholder="New tag — e.g. Child's education, Long term"
          className="flex-1 rounded-[9px] border-[1.5px] border-[var(--field-border)] bg-[var(--field)] px-3 py-1.5 text-[12.5px] font-semibold text-[var(--strong)] outline-none focus:border-[var(--accent)]"
        />
        <button
          disabled={busy || !creating.trim()}
          onClick={() => void create()}
          className="rounded-[9px] bg-[var(--accent)] px-3.5 py-1.5 text-[12px] font-extrabold text-white disabled:opacity-50"
        >
          Create &amp; apply
        </button>
      </div>
      {error && <div className="mt-1.5 text-[11.5px] font-bold text-[var(--danger)]">{error}</div>}
    </div>
  );
}
