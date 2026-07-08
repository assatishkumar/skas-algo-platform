#!/usr/bin/env bash
#
# status.sh — show whether the backend + web are up (read-only; changes nothing).
set -uo pipefail
cd "$(dirname "$0")/.."
LABEL="com.skas.algo"

echo "── skas-algo status ─────────────────────────────"

# Backend supervisor (launchd)
if launchctl list 2>/dev/null | grep -q "$LABEL"; then
  line=$(launchctl list | grep "$LABEL")
  pid=$(echo "$line" | awk '{print $1}')
  echo "Supervisor : loaded (launchd $LABEL, pid ${pid})"
else
  echo "Supervisor : not loaded"
fi

# Backend health
if curl -s -m 3 http://127.0.0.1:8080/api/v1/health >/dev/null 2>&1; then
  runs=$(curl -s -m 5 http://127.0.0.1:8080/api/v1/live 2>/dev/null \
    | grep -o '"run_id"' | wc -l | tr -d ' ')
  echo "Backend    : UP   http://127.0.0.1:8080   (${runs} runs recovered)"
else
  echo "Backend    : DOWN (nothing answering on :8080)"
fi

# Web
if lsof -nP -iTCP:5173 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Web UI     : UP   http://localhost:5173"
else
  echo "Web UI     : DOWN"
fi
echo "─────────────────────────────────────────────────"
