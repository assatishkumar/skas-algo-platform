# CLAUDE.md — working notes for agents

Operational nuances + invariants for this repo. The README orients you; `docs/` holds design intent;
**this file is the "how to work here safely" layer.** Keep it tight and high-signal.

> **Maintain this file.** As the platform matures, update CLAUDE.md when a new invariant, footgun, or
> convention emerges — not after-the-fact docs, but the things a fresh session would otherwise have to
> rediscover. (Standing request from the owner.)

## 1. This is a real, live trading system with real money
- Real orders go through `brokers/zerodha.py` and are **double-gated**: `SKAS_LIVE_TRADING_ENABLED=true`
  **and** the broker account `armed`. Never bypass either.
- Do **not** run ad-hoc scripts that could place/modify/cancel orders. Be deliberate around anything in
  `live/` and order placement. When in doubt, ask.
- Tests use simulated brokers and an isolated DB — they never touch the broker or dev data.

## 2. Docs are intent; code + comments + git log are truth
- `docs/PLAN.md` is aspirational (mentions things not built / not built that way). Trust the code when
  they disagree. The README is kept current; deep design lives in `docs/`.
- **Inline comments are the design memory** — unusually rich "why" comments encode invariants and
  footguns. Read them, trust them, and match their density/voice when adding code. Commit messages share
  that voice (concise: *what* + *the guard/why*).

## 3. The parity / mode-equivalence invariant is sacred
- Founding rule: **backtest = forward-test = live**, one engine; only Clock / DataFeed / BrokerAdapter
  swap. The shared core is `engine/execution.py` (`SliceExecutor`), used by `engine/runner.py`
  (backtest) and `live/` (paper/live).
- Much of the shared path is **deliberately gated** to keep the equity backtest byte-identical and
  backtest == paper-replay. Options-only logic is gated on `settler is not None` / `margin_model`.
  Comments flag the sensitive lines ("byte-identical", "mode-equivalence holds").
- Guardrail tests: `tests/test_sst_parity.py`, `test_sst_fifo_parity.py`, `test_mode_equivalence.py`.
  If you touch the shared path, these must stay green.

## 4. Local dev data is real and irreplaceable
- `skas_algo.db` (~75 MB, gitignored) holds actual deployments + run history. **Do not reset/delete it.**
- Tests are fully isolated: `tests/conftest.py` spins a temp SQLite DB + throwaway Fernet key before any
  import, so `pytest` is always safe to run.
- Secrets: broker creds are Fernet-encrypted at rest; `.env` and tokens are gitignored — never commit them.

## 5. Repo topology — market data is NOT in this repo
- `../skas-data` — installed editable (`pip install -e ../skas-data`); the data/cache layer (DuckDB +
  Kite). All market data + backtest history comes from here.
- `../skas-trading` — original strategy reference (SST, etc.).
- `../skas-options` — old options code, **explicitly not reused**.
- Known security debt: Kite secrets were committed in sibling repos (see `docs/PLAN.md` Phase 0) — not
  this repo's scope, but don't propagate the pattern.

## 6. Indian-market assumptions are implicit everywhere
- IST timezone, NSE hours, Nifty 50 universe, F&O lot sizes + monthly/weekly expiries, STCG-tax &
  withdrawal modeling, ₹/INR, Zerodha/Kite.
- Decision cadence: **equity** decides once/day (~15:20 IST); **options/intraday** strategies decide
  every tick. Off-hours the live loop re-prices marks but never trades.

## 7. Single-user, in-process state
- Process-wide singleton `manager = LiveRunManager()` (`live/manager.py`) holds running sessions in
  memory. On restart, `live/recovery.py` rebuilds them from DB-persisted state; Zerodha token expiry
  self-heals. No auth / multi-tenancy (the iOS app would add a token — **paused**).

## 8. Conventions
- New strategies onboard via `strategies/registry.py`, **not** engine edits.
- Feature branches; `main` is default. Commit/push only when asked.
- ruff + black + mypy, line-length 100. `pytest` runs with coverage (see `pyproject.toml`).
- Active frontier: the **Donchian basket strangle** (`donchian_strangle_monthly`) — note it has **no
  backtest path**; it's only deployed live/paper from the screener.

## 9. Frontend (`web/`) gotchas
- **Router state vs legacy redirects:** several old paths are `<Navigate to=... replace />` redirects in
  `App.tsx` (e.g. `/new` → `/backtest?tab=new`). `<Navigate>` **drops `location.state`**, so navigating
  to a redirect path with state (e.g. `clonePrefill`) silently loses it — land on the real route
  directly. The Backtest "tabs" (Runs / New backtest) are the same page selected by `?tab=`.
- Pages prefill forms from `location.state` via one-shot effects (`useRef` guards) and let the
  template/clone values land **after** the per-strategy default-reset effects — order matters; read the
  comments before reordering effects.

## 10. Running locally
```bash
# Backend (FastAPI + uvicorn on :8080)
venv/bin/skas-algo
# Web (Vite on :5173, proxies /api → :8080 incl. WebSocket)
cd web && npm run dev
```
Health check: `curl http://localhost:8080/api/v1/health`. The DB schema is created on startup
(idempotent); Alembic migrations are in `alembic/` for evolving an existing DB.
</content>
