#!/usr/bin/env bash
#
# uninstall-supervisor.sh — remove the launchd supervision installed by install-supervisor.sh.
# Stops the supervised backend and removes the agent so it no longer auto-starts. After this,
# run the backend manually again (venv/bin/skas-algo from the repo root).
set -euo pipefail

LABEL="com.skas.algo"
PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ -f "$PLIST_DEST" ]; then
  echo "▶ Unloading and removing $PLIST_DEST…"
  launchctl unload -w "$PLIST_DEST" 2>/dev/null || true
  rm -f "$PLIST_DEST"
  echo "✓ Supervisor removed. The backend is stopped; start it manually when needed:"
  echo "    cd $(cd "$(dirname "$0")/.." && pwd) && venv/bin/skas-algo"
else
  echo "No supervisor plist found at $PLIST_DEST — nothing to remove."
fi
