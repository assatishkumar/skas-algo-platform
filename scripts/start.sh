#!/usr/bin/env bash
#
# start.sh — bring up the whole local stack: the backend (launchd-supervised, auto-restarts on
# crash/reboot) + the Vite web UI. Idempotent: safe to run when things are already up.
#
#   Backend → http://127.0.0.1:8080   (the live loop, order/broker reconciliation, watchdog,
#                                       backups, WebSocket — all in this ONE process)
#   Web UI  → http://localhost:5173    (Vite dev server, hot-reload)
#
# Stop everything with ./scripts/stop.sh · check with ./scripts/status.sh
set -uo pipefail
cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"
LABEL="com.skas.algo"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"

# --- 1. Backend (via the launchd supervisor) ---------------------------------------------
if [ ! -f "$PLIST" ]; then
  echo "▶ Supervisor not installed — installing it (one-time)…"
  "$REPO_ROOT/scripts/install-supervisor.sh"          # generates the plist, loads + health-checks
elif launchctl list | grep -q "$LABEL"; then
  echo "✓ Backend already supervised (launchd $LABEL)."
else
  echo "▶ Starting backend (loading launchd $LABEL)…"
  launchctl load -w "$PLIST"
fi

# --- 2. Web UI (Vite dev server) ---------------------------------------------------------
if lsof -nP -iTCP:5173 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "✓ Web already running on :5173."
else
  echo "▶ Starting web (Vite dev) on :5173 → $LOG_DIR/web.log"
  # nohup + disown so it survives this shell; macOS has no setsid.
  ( cd "$REPO_ROOT/web" && nohup npm run dev > "$LOG_DIR/web.log" 2>&1 & disown )
fi

# --- 3. Wait for readiness + report ------------------------------------------------------
echo -n "▶ Waiting for the backend to answer on :8080… "
start=$(date +%s)
until curl -s -m 3 http://127.0.0.1:8080/api/v1/health >/dev/null 2>&1; do
  sleep 2
  if [ $(( $(date +%s) - start )) -gt 60 ]; then echo "TIMEOUT (check $LOG_DIR/skas-algo.err.log)"; break; fi
done
curl -s -m 3 http://127.0.0.1:8080/api/v1/health >/dev/null 2>&1 && echo "ok"

echo
echo "  Backend : http://127.0.0.1:8080   (logs: $LOG_DIR/skas-algo.{out,err}.log)"
echo "  Web UI  : http://localhost:5173    (logs: $LOG_DIR/web.log)"
echo "✓ Started.  Stop with ./scripts/stop.sh"
