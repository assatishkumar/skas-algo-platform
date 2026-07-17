#!/usr/bin/env bash
# VPS one-shot updater: stop backend → pull → pip → build web + mobile web → start → health.
#
# The Vite/tsc builds are safe on the small Lightsail box ONLY while the backend is stopped
# (they OOM-fight the live loop for RAM otherwise — owner-verified 2026-07-18). Run this
# OFF-HOURS: the live loop is down for the duration of the build (~1-3 min).
#
# Whatever happens after the stop, the backend is ALWAYS started again on exit — a broken
# build must never leave the box dead (real-money runs recover on start; a recovered LIVE
# run needs SKAS_LIVE_RESUME_ORDERS_ON_RECOVERY + the morning login to re-arm real orders).
set -euo pipefail
cd "$(dirname "$0")/.."

if ! systemctl list-unit-files skas-algo.service --no-legend 2>/dev/null | grep -q skas-algo; then
  echo "✗ no skas-algo systemd unit — this script is for the VPS (the Mac uses scripts/start.sh)"
  exit 1
fi

branch=$(git branch --show-current)
git fetch origin --quiet
behind=$(git rev-list --count "HEAD..origin/${branch}")
echo "▶ branch ${branch} — ${behind} commit(s) to pull"
if [ "${behind}" != "0" ]; then
  git log --oneline "HEAD..origin/${branch}" | head -10
fi

echo "▶ stopping backend (builds must not fight the live loop for RAM)…"
sudo systemctl stop skas-algo

start_backend() {
  echo "▶ daemon-reload + start backend…"
  sudo systemctl daemon-reload   # harmless if the unit didn't change; required if it did
  sudo systemctl start skas-algo
}
trap start_backend EXIT

git pull --ff-only
echo "▶ pip install -e . …"
venv/bin/pip install -e . --quiet

build_web() {  # $1 = workspace dir (web | web-mobile)
  echo "▶ building $1 …"
  (
    cd "$1"
    # npm ci only when deps could have changed — it's minutes on this box otherwise.
    if [ ! -d node_modules ] || [ package-lock.json -nt node_modules ]; then
      npm ci --no-audit --prefer-offline
    fi
    npm run build
  )
}
build_web web
# The mobile companion webapp — served by the backend at /mobile/ (same Tailscale origin).
if [ -f web-mobile/package.json ]; then
  build_web web-mobile
fi

trap - EXIT
start_backend

printf "▶ waiting for health"
for _ in $(seq 1 15); do
  if curl -fsS http://localhost:8080/api/v1/health >/dev/null 2>&1; then
    echo
    echo "✓ backend healthy — now at: $(git log --oneline -1)"
    echo "  desktop UI: hard-refresh the browser (Cmd/Ctrl+Shift+R — PWA cache)."
    echo "  mobile web: https://<vps>.<tailnet>.ts.net/mobile/ (leave Backend URL blank there)."
    exit 0
  fi
  printf "."
  sleep 2
done
echo
echo "✗ health check failed after 30s — inspect: journalctl -u skas-algo -n 50"
exit 1
