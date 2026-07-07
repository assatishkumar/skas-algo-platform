#!/usr/bin/env bash
#
# install-supervisor.sh — put the skas-algo backend under launchd supervision so it is
# ALWAYS UP: auto-starts at login and auto-restarts within 15s if it ever exits.
#
# WHY: stops are engine-side only (no broker GTT), so a backend that is DOWN with open real
# positions = unmanaged exposure. Supervision means a crash/reboot self-heals in seconds and
# recovery re-manages the book. See docs/ARCHITECTURE.md → "Failure modes & recovery".
#
# This STOPS any backend currently running (nohup or a previous agent) and hands :8080 to
# launchd. Reversible: ./scripts/uninstall-supervisor.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SKAS_BIN="$REPO_ROOT/venv/bin/skas-algo"
LOG_DIR="$REPO_ROOT/logs"
LABEL="com.skas.algo"
PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

[ -x "$SKAS_BIN" ] || { echo "✗ $SKAS_BIN not found/executable — is the venv set up?"; exit 1; }
mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"

echo "▶ Stopping any backend currently on :8080 (handing it to launchd)…"
# Exact pattern — a bare 'skas-algo' also matches vite/esbuild via the repo path (CLAUDE.md §10).
pkill -9 -f "venv/bin/skas-algo" 2>/dev/null || true
sleep 2
for pid in $(lsof -nP -iTCP:8080 -sTCP:LISTEN -t 2>/dev/null || true); do kill -9 "$pid" 2>/dev/null || true; done

echo "▶ Generating $PLIST_DEST from the template…"
sed -e "s#__SKAS_BIN__#$SKAS_BIN#g" \
    -e "s#__REPO_ROOT__#$REPO_ROOT#g" \
    -e "s#__LOG_DIR__#$LOG_DIR#g" \
    "$REPO_ROOT/scripts/com.skas.algo.plist.template" > "$PLIST_DEST"
plutil -lint "$PLIST_DEST"

echo "▶ Loading the agent…"
launchctl unload "$PLIST_DEST" 2>/dev/null || true   # in case an old one is loaded
launchctl load -w "$PLIST_DEST"

echo "▶ Waiting for health on :8080…"
for _ in $(seq 1 30); do
  if curl -s -m 3 http://127.0.0.1:8080/api/v1/health >/dev/null 2>&1; then
    echo "✓ Supervisor installed and backend healthy."
    echo "  Logs:   $LOG_DIR/skas-algo.{out,err}.log"
    echo "  Status: launchctl list | grep $LABEL"
    echo "  Stop/remove: ./scripts/uninstall-supervisor.sh"
    exit 0
  fi
  sleep 2
done

echo "✗ Backend did not become healthy in 60s. Check $LOG_DIR/skas-algo.err.log."
echo "  You can revert with: ./scripts/uninstall-supervisor.sh"
exit 1
