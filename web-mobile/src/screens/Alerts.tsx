import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "@shared/api/client";

const ICONS: Record<string, { glyph: string; bg: string; fg: string }> = {
  ERROR: { glyph: "◼", bg: "var(--danger-bg)", fg: "var(--danger)" },
  WARNING: { glyph: "!", bg: "var(--warn-bg)", fg: "var(--warn-text)" },
  SUCCESS: { glyph: "✓", bg: "var(--ok-bg)", fg: "var(--ok-text)" },
  INFO: { glyph: "◆", bg: "var(--opt-bg)", fg: "var(--opt-text)" },
};

const timeFmt = new Intl.DateTimeFormat("en-IN", {
  hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata",
});

/** 07 · Alerts — grouped TODAY / YESTERDAY / EARLIER, unread emphasis, mark-all-read.
 * Rows come from the backend's in-app alert store (every platform alert lands here). */
export default function AlertsScreen() {
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: ["alerts", "full"], queryFn: () => api.alertsList(100),
    refetchInterval: 60_000,
  });
  const markRead = useMutation({
    mutationFn: () => api.alertsMarkRead(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["alerts"] }),
  });

  const today = new Date().toDateString();
  const yesterday = new Date(Date.now() - 86_400_000).toDateString();
  const groups: [string, NonNullable<typeof data>["alerts"]][] = [["Today", []],
    ["Yesterday", []], ["Earlier", []]];
  for (const a of data?.alerts ?? []) {
    const d = a.ts ? new Date(a.ts).toDateString() : "";
    if (d === today) groups[0][1].push(a);
    else if (d === yesterday) groups[1][1].push(a);
    else groups[2][1].push(a);
  }

  return (
    <div className="screen" style={{ paddingTop: "calc(10px + env(safe-area-inset-top))" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <Link to="/live" style={{
          color: "var(--accent-deep)", textDecoration: "none", fontWeight: 700,
          fontSize: 16, minHeight: 44, display: "inline-flex", alignItems: "center",
        }}>‹ Live</Link>
        {(data?.unread ?? 0) > 0 && (
          <button onClick={() => markRead.mutate()} disabled={markRead.isPending}
            style={{ color: "var(--accent-deep)", fontWeight: 700, fontSize: 14.5 }}>
            Mark all read
          </button>
        )}
      </div>
      <div className="page-title">Alerts</div>

      {groups.map(([label, rows]) => rows.length > 0 && (
        <div key={label} style={{ opacity: label === "Today" ? 1 : 0.72 }}>
          <div className="sg" style={{
            fontWeight: 700, fontSize: 13, color: "var(--faint)", margin: "16px 2px 8px",
          }}>{label.toUpperCase()}</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 9 }}>
            {rows.map((a) => {
              const icon = ICONS[a.level] ?? ICONS.INFO;
              return (
                <div key={a.id} className="card" style={{
                  display: "flex", gap: 12, alignItems: "flex-start",
                  borderRadius: 18, padding: "13px 15px",
                }}>
                  <div style={{
                    width: 40, height: 40, borderRadius: 12, flexShrink: 0,
                    background: icon.bg, color: icon.fg, display: "flex",
                    alignItems: "center", justifyContent: "center", fontWeight: 800,
                    fontSize: 16,
                  }}>{icon.glyph}</div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{
                      display: "flex", justifyContent: "space-between",
                      alignItems: "baseline", gap: 8,
                    }}>
                      <span className="sg" style={{
                        fontWeight: 700, fontSize: 15,
                        ...(a.read ? {} : { color: "var(--strong)" }),
                      }}>
                        {!a.read && <span style={{
                          display: "inline-block", width: 7, height: 7, borderRadius: 4,
                          background: "var(--accent)", marginRight: 6,
                          verticalAlign: "middle",
                        }} />}
                        {a.title}
                      </span>
                      <span style={{
                        fontSize: 12, color: "var(--faint)", whiteSpace: "nowrap",
                      }}>{a.ts ? timeFmt.format(new Date(a.ts)) : ""}</span>
                    </div>
                    {a.message && (
                      <div style={{
                        fontSize: 13, color: "var(--muted)", lineHeight: 1.5, marginTop: 2,
                        wordBreak: "break-word",
                      }}>{a.message}</div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ))}
      {(data?.alerts?.length ?? 0) === 0 && (
        <div className="card" style={{ marginTop: 16, color: "var(--muted)", fontSize: 14 }}>
          No alerts yet — order errors, halts, watchdog restarts and data warnings land here.
        </div>
      )}
    </div>
  );
}
