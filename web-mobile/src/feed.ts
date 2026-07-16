import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api, liveWsUrl } from "@shared/api/client";
import type { LiveRunSnapshot } from "@shared/types";

/** Live snapshot feed: seed via GET /live, then merge WebSocket `snapshot` deltas by
 * run_id (the desktop LivePage pattern). `alert` events invalidate the alerts query so
 * the bell badge updates without polling. Reconnects with a small backoff. */
export function useLiveFeed(): { runs: LiveRunSnapshot[]; updatedAt: Date | null } {
  const [runs, setRuns] = useState<LiveRunSnapshot[]>([]);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const qc = useQueryClient();
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let alive = true;
    let retry = 0;

    const seed = () =>
      api.liveList().then((list) => {
        if (!alive) return;
        setRuns(list);
        setUpdatedAt(new Date());
      }).catch(() => undefined);

    const connect = () => {
      if (!alive) return;
      const ws = new WebSocket(liveWsUrl());
      wsRef.current = ws;
      ws.onopen = () => {
        retry = 0;
        seed(); // re-seed on every (re)connect — WS deltas only patch known runs
      };
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === "snapshot" && msg.run_id != null) {
            setRuns((prev) => {
              const i = prev.findIndex((r) => r.run_id === msg.run_id);
              if (i === -1) return [...prev, msg as LiveRunSnapshot];
              const next = prev.slice();
              next[i] = { ...next[i], ...msg };
              return next;
            });
            setUpdatedAt(new Date());
          } else if (msg.type === "stopped" && msg.run_id != null) {
            setRuns((prev) => prev.filter((r) => r.run_id !== msg.run_id));
          } else if (msg.type === "alert") {
            qc.invalidateQueries({ queryKey: ["alerts"] });
          }
        } catch {
          /* ignore malformed frames */
        }
      };
      ws.onclose = () => {
        if (!alive) return;
        retry += 1;
        setTimeout(connect, Math.min(15000, 1000 * retry));
      };
    };

    seed();
    connect();
    return () => {
      alive = false;
      wsRef.current?.close();
    };
  }, [qc]);

  return { runs, updatedAt };
}
