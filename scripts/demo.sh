#!/usr/bin/env bash
# A broker-free demo box on port 8090 — its OWN database, option store and console dir
# under ./demo/, so nothing here can touch the real skas_algo.db or the real 1-min capture.
#
#   scripts/demo.sh            # seed 5 synthetic NIFTY days, build the UI if needed, serve
#   open http://localhost:8090/console      (replay any seeded day; click the chain)
#   open http://localhost:8090/backtest?tab=new   (Intraday basis → any options strategy)
#
# Needs the venv (pip install -e ".[dev]" + the sibling skas-data) and node for the UI build.
# Stop with Ctrl-C. Delete ./demo to start over.
set -euo pipefail
cd "$(dirname "$0")/.."

DEMO_DIR="$PWD/demo"
mkdir -p "$DEMO_DIR/option_intraday" "$DEMO_DIR/console"

export SKAS_DATABASE_URL="sqlite:///$DEMO_DIR/skas_demo.db"
export SKAS_OPTION_INTRADAY_DIR="$DEMO_DIR/option_intraday"
export SKAS_CONSOLE_DIR="$DEMO_DIR/console"
export SKAS_BACKUP_OFFBOX_DIR=""
export SKAS_BACKUP_REMOTE_CMD=""
export SKAS_OPTION_BARS_BACKUP_DIR=""
export SKAS_WS_FEED_ENABLED=0
export SKAS_LIVE_TRADING_ENABLED=0
export SKAS_API_PORT="${SKAS_API_PORT:-8090}"
export SKAS_SERVE_WEBAPP=1          # serve web/dist from the same port (opt-in in settings)
unset SKAS_AUTH_PASSWORD_HASH SKAS_AUTH_JWT_SECRET SKAS_PEER_API_URL SKAS_PEER_API_TOKEN

if [ ! -d venv ]; then
  echo "no venv — run: python -m venv venv && venv/bin/pip install -e '.[dev]' && venv/bin/pip install -e ../skas-data" >&2
  exit 1
fi

if [ -z "$(ls -A "$DEMO_DIR/option_intraday" 2>/dev/null)" ]; then
  echo "▶ seeding synthetic option days…"
  venv/bin/skas-algo demo-seed --days "${DEMO_DAYS:-5}"
fi

if [ ! -f web/dist/index.html ]; then
  echo "▶ building the UI once (web/dist)…"
  (cd web && npm ci --silent && npm run build --silent)
fi

echo "▶ demo backend on http://localhost:$SKAS_API_PORT  (db + store under ./demo, real data untouched)"
exec venv/bin/skas-algo
