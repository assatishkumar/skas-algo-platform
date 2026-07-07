#!/usr/bin/env bash
#
# preflight.sh — THE gate to run before restarting/deploying the LIVE backend.
#
# Hard gate (blocks the restart if it fails): the full test suite — including the parity /
# mode-equivalence suites that guard the shared engine — and the web typecheck. Ruff is
# advisory (the repo carries known pre-existing lint debt; style must not block a fix).
#
# Why this exists: this is a real-money system where "one engine, backtest == paper == live"
# is the load-bearing invariant. A green preflight is the evidence that a code change did NOT
# silently alter the engine, a persisted strategy's meaning, or a live code path. Run it,
# read it, and only restart on green. See docs/ARCHITECTURE.md → "Developing without
# breaking live".
#
# Usage:  ./scripts/preflight.sh
set -uo pipefail
cd "$(dirname "$0")/.."
FAIL=0

echo "▶ ruff (advisory — not blocking)…"
venv/bin/ruff check src tests || echo "  ⚠ ruff findings above (advisory)."

# The parity suites (test_sst_parity / test_sst_fifo_parity) open the REAL skas-data DuckDB
# cache read-write — exclusive to ONE process. If the live backend is still running on :8080
# it holds that lock, so those two would fail with an ENVIRONMENTAL "Could not set lock"
# (not a code defect). Deselect them while a backend is up and flag that they must be
# validated once, against a stopped/reloaded backend. Everything else uses fakes and is safe.
DESELECT=()
if lsof -nP -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "  ⚠ backend is live on :8080 — deselecting the 2 DuckDB-cache parity suites."
  echo "    Re-run them once against a stopped/reloaded backend:"
  echo "      venv/bin/python -m pytest tests/test_sst_parity.py tests/test_sst_fifo_parity.py"
  DESELECT=(--ignore=tests/test_sst_parity.py --ignore=tests/test_sst_fifo_parity.py)
fi

echo "▶ pytest (full suite incl. parity/mode-equivalence)…"
# ${arr[@]+"${arr[@]}"} — safe expansion of a possibly-empty array under `set -u` on the
# macOS system bash (3.2).
if ! venv/bin/python -m pytest tests/ ${DESELECT[@]+"${DESELECT[@]}"} -q --no-cov; then
  echo "  ✗ tests failed — see output above."
  FAIL=1
fi

echo "▶ web typecheck (tsc --noEmit)…"
if ! ( cd web && npm run --silent lint ); then
  echo "  ✗ web typecheck failed."
  FAIL=1
fi

echo
if [ "$FAIL" -ne 0 ]; then
  echo "✗ PREFLIGHT FAILED — do NOT restart the live backend."
  exit 1
fi
echo "✓ PREFLIGHT PASSED — safe to restart the backend."
