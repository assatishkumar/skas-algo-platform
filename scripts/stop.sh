#!/usr/bin/env bash
#
# stop.sh — take the whole local stack down and keep it down: unload the launchd supervisor
# (so the backend does NOT auto-respawn) and stop the Vite web UI.
#
# Note: this only PAUSES the supervisor (launchctl unload) — the agent stays installed, so
# ./scripts/start.sh brings it right back. To remove supervision entirely, use
# ./scripts/uninstall-supervisor.sh.
set -uo pipefail
cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"
LABEL="com.skas.algo"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

# --- 1. Backend: unload the agent FIRST so launchd won't respawn it, then clean up strays --
if [ -f "$PLIST" ] && launchctl list | grep -q "$LABEL"; then
  echo "▶ Stopping backend (unloading launchd $LABEL)…"
  launchctl unload -w "$PLIST"
  sleep 2
else
  echo "✓ Backend supervisor not loaded."
fi
# Any backend not under launchd (a stray nohup run) — exact pattern (a bare 'skas-algo' also
# matches vite/esbuild via the repo path, CLAUDE.md §10).
pkill -9 -f "venv/bin/skas-algo" 2>/dev/null || true
for pid in $(lsof -nP -iTCP:8080 -sTCP:LISTEN -t 2>/dev/null || true); do kill -9 "$pid" 2>/dev/null || true; done

# --- 2. Web: stop Vite (+ its esbuild helper) --------------------------------------------
echo "▶ Stopping web (Vite) on :5173…"
for pid in $(lsof -nP -iTCP:5173 -sTCP:LISTEN -t 2>/dev/null || true); do kill "$pid" 2>/dev/null || true; done
# esbuild service child, scoped to this repo's install so unrelated node is untouched.
pkill -f "$REPO_ROOT/web/node_modules/@esbuild" 2>/dev/null || true
sleep 1

# --- 3. Report ---------------------------------------------------------------------------
be_down=1; lsof -nP -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1 && be_down=0
web_down=1; lsof -nP -iTCP:5173 -sTCP:LISTEN >/dev/null 2>&1 && web_down=0
echo
[ "$be_down" -eq 1 ] && echo "  Backend :8080 → stopped" || echo "  Backend :8080 → STILL UP (check: lsof -iTCP:8080 -sTCP:LISTEN)"
[ "$web_down" -eq 1 ] && echo "  Web     :5173 → stopped" || echo "  Web     :5173 → STILL UP (check: lsof -iTCP:5173 -sTCP:LISTEN)"
echo "✓ Stopped.  Start again with ./scripts/start.sh"
