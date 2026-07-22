#!/usr/bin/env bash
#
# deploy-web.sh — build the web UI ON THE MAC and rsync the dist to the VPS.
#
# The small Lightsail trading box OOM-thrashes on the Vite/tsc build (the bundle grew past what
# 1 GB can build), so building there hangs vps-update.sh before it can restart the backend. Build
# HERE (roomy Mac) and ship only the static dist. A dist-only update needs NO restart — the backend
# serves web/dist live, so the live loop is never interrupted for a UI change (unlike a Python
# update, which goes via scripts/vps-update.sh on the VPS: pull + pip + restart).
#
# Usage:  scripts/deploy-web.sh <vps-ssh-host> [remote-repo-path]
#   e.g.  scripts/deploy-web.sh ubuntu@myvps.tailnet.ts.net
#   remote-repo-path defaults to git/skas-algo-platform (relative to the remote login's ~).
#
# After it runs: hard-refresh the browser (Cmd/Ctrl+Shift+R) — the PWA service worker caches the
# old bundle.
set -euo pipefail
cd "$(dirname "$0")/.."

HOST="${1:?usage: deploy-web.sh <vps-ssh-host> [remote-repo-path]}"
REMOTE="${2:-git/skas-algo-platform}"   # relative to the remote login home (~)

build() {  # $1 = workspace dir (web | web-mobile)
  echo "▶ building $1 …"
  (
    cd "$1"
    # npm ci only when deps could have changed (it's slow); otherwise the cached node_modules.
    if [ ! -d node_modules ] || [ package-lock.json -nt node_modules ]; then
      npm ci --no-audit --prefer-offline
    fi
    npm run build
  )
}

ship() {  # $1 = workspace dir; rsync its dist → the VPS (delete stale hashed assets)
  echo "▶ rsync $1/dist → ${HOST}:${REMOTE}/$1/dist …"
  rsync -az --delete "$1/dist/" "${HOST}:${REMOTE}/$1/dist/"
}

build web
ship web

# The mobile companion webapp (served at /mobile/ on the same Tailscale origin), if present.
if [ -f web-mobile/package.json ]; then
  build web-mobile
  ship web-mobile
fi

echo
echo "✓ UI shipped to ${HOST}. The backend serves web/dist live — no restart needed."
echo "  Now hard-refresh the browser (Cmd/Ctrl+Shift+R — the PWA service worker caches the old bundle)."
