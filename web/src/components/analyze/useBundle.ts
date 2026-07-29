/** Fetch-or-compute the analytics bundle for a run.
 *
 * Flow: GET bundle → 404 means "not computed" → POST compute (background job on the
 * analytics slot) → poll progress every 1.5s → on done, re-GET the (now cached) bundle.
 * A 409 on compute means another run's build is in flight — we keep polling and retry
 * the compute when the slot frees (single-user box, this resolves itself). */

import { useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import type { AnalyticsBundle, ReplayJobSnapshot } from "../../types";

export interface BundleState {
  bundle: AnalyticsBundle | null;
  computing: boolean;
  progress: ReplayJobSnapshot | null;
  error: string | null;
}

export function useBundle(runId: number | null): BundleState {
  const [st, setSt] = useState<BundleState>({
    bundle: null, computing: false, progress: null, error: null,
  });
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    setSt({ bundle: null, computing: false, progress: null, error: null });
    if (runId == null) return () => { alive.current = false; };

    let timer: ReturnType<typeof setTimeout> | null = null;

    const poll = async () => {
      try {
        const p = await api.analyticsProgress();
        if (!alive.current) return;
        if (p.status === "done") {
          try {
            const b = await api.runAnalytics(runId);
            if (alive.current) setSt({ bundle: b, computing: false, progress: null, error: null });
            return;
          } catch {
            // done, but for a DIFFERENT run — kick our own compute
            await kick();
            return;
          }
        }
        if (p.status === "error") {
          setSt((s) => ({ ...s, computing: false, error: p.error ?? "analytics failed" }));
          return;
        }
        setSt((s) => ({ ...s, computing: true, progress: p }));
        timer = setTimeout(poll, 1500);
      } catch (e) {
        if (alive.current) setSt((s) => ({ ...s, error: String(e) }));
      }
    };

    const kick = async () => {
      try {
        const r = await api.computeAnalytics(runId);
        if (!alive.current) return;
        if (r.status === "cached") {
          const b = await api.runAnalytics(runId);
          if (alive.current) setSt({ bundle: b, computing: false, progress: null, error: null });
          return;
        }
        setSt((s) => ({ ...s, computing: true }));
        timer = setTimeout(poll, 1200);
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        // a 409 = another run's build holds the single analytics slot — poll and retry
        if (msg.includes("409")) { timer = setTimeout(poll, 2000); return; }
        if (alive.current) setSt((s) => ({ ...s, computing: false, error: msg }));
      }
    };

    (async () => {
      try {
        const b = await api.runAnalytics(runId);
        if (alive.current) setSt({ bundle: b, computing: false, progress: null, error: null });
      } catch (e) {
        if (!alive.current) return;
        const msg = e instanceof Error ? e.message : String(e);
        // 404 = "not computed yet" — the EXPECTED first-open path → kick the job.
        // (String(e) is "Error: 404: …", which is why startsWith("404") never matched —
        // the run-#242 first-open bug, 2026-07-29.)
        if (msg.includes("404")) await kick();
        else setSt((s) => ({ ...s, error: msg }));
      }
    })();

    return () => { alive.current = false; if (timer) clearTimeout(timer); };
  }, [runId]);

  return st;
}
