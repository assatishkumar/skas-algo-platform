#!/usr/bin/env bash
# VPS one-shot updater (BACKEND only): stop → pull → pip → start → health.
#
# The web/mobile UI is NOT built here — the Vite/tsc build OOM-thrashes on the small Lightsail
# box (the bundle outgrew ~1 GB) and would hang before the restart, leaving the box dead. Build
# the UI on the Mac and rsync the dist instead:  scripts/deploy-web.sh <vps-host>  (a dist-only
# update needs no restart — the backend serves web/dist live). `web*/dist` are gitignored, so
# `git pull` here never clobbers the dist the Mac shipped.
#
# Run OFF-HOURS: the live loop is down only for the brief pip + restart. Whatever happens after
# the stop, the backend is ALWAYS started again on exit — a failure must never leave the box dead
# (real-money runs recover on start; a recovered LIVE run needs SKAS_LIVE_RESUME_ORDERS_ON_RECOVERY
# + the morning login to re-arm real orders).
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

echo "▶ stopping backend for a clean code reload…"
sudo systemctl stop skas-algo

start_backend() {
  echo "▶ daemon-reload + start backend…"
  sudo systemctl daemon-reload   # harmless if the unit didn't change; required if it did
  sudo systemctl start skas-algo
}
trap start_backend EXIT

git pull --ff-only
# The skas-data sibling (editable install) must move WITH the platform — a platform that
# calls a newer skas-data API against a stale clone 500s at runtime (options_coverage,
# 2026-07-17: the Data→Options page broke on the VPS while everything else looked green).
if [ -d ../skas-data/.git ]; then
  echo "▶ updating ../skas-data …"
  git -C ../skas-data pull --ff-only
  venv/bin/pip install -e ../skas-data --quiet
fi
echo "▶ pip install -e . …"
venv/bin/pip install -e . --quiet

# NB: the web/mobile UI is built on the Mac + rsync'd (scripts/deploy-web.sh) — NOT here.

trap - EXIT
start_backend

printf "▶ waiting for health"
for _ in $(seq 1 15); do
  if curl -fsS http://localhost:8080/api/v1/health >/dev/null 2>&1; then
    echo
    echo "✓ backend healthy — now at: $(git log --oneline -1)"
    echo "  UI: build + ship it from the Mac →  scripts/deploy-web.sh <this-vps-host>"
    echo "      (then hard-refresh the browser — Cmd/Ctrl+Shift+R — for the PWA cache)."
    exit 0
  fi
  printf "."
  sleep 2
done
echo
echo "✗ health check failed after 30s — inspect: journalctl -u skas-algo -n 50"
exit 1
