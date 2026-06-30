# skas-algo-platform

Single-user algorithmic trading platform for Indian markets (Zerodha / Kite): **backtest,
forward-test (paper), and live** trading from **one engine / one strategy codebase** across stocks
and derivatives. React PWA UI (installable on iOS), per-position rule overrides + live intervention,
TOTP-assisted broker login (Zerodha first, broker-abstracted), and Telegram alerts.

> **Core design rule: backtest = forward-test = live.** Only the **Clock**, **DataFeed**, and
> **BrokerAdapter** swap by mode — everything else (strategy, overrides, risk, P&L accounting,
> reporting) is shared. What you backtest is literally what you trade.

Market data and historical caching come from the sibling **`skas-data`** package (DuckDB cache +
Kite). Strategy logic was originally ported from `skas-trading`.

## Status

**Actively used.** Well past the original scaffolding phase — the engine runs all three modes, ~15
strategies are registered, live options deployment works end to end, and the React PWA is the daily
driver. Current focus is the **Donchian basket short-strangle** live deployment flow (screener →
basket entry → per-name breach/flip governance → live monitoring).

Roadmap docs live in [`docs/`](docs/). A native iOS app is sketched in
[`docs/PLAN-ios-app.md`](docs/PLAN-ios-app.md) but **app development is paused** for now.

## Architecture

A modular monolith (easy local dev) with clear seams so pieces can split into services later.

```
   iOS PWA / Browser ──▶  React PWA (web/, installable, live WebSocket)
                              │  REST + WebSocket
                          FastAPI backend (src/skas_algo/)
                              │
        ┌─────────────────────┼──────────────────────────┐
   Algo Engine            BrokerAdapter               Notifier
   (mode-agnostic)        • BacktestBroker            • Telegram
   • Clock      ◀─ swap   • PaperBroker
   • DataFeed   ◀─ swap   • LiveBroker (Zerodha TOTP)
   • Strategy
   • Override resolver        │ orders / quotes / ticks
   • Risk / portfolio         │
        │              skas-data (dep): hist replay + live quotes, DuckDB cache
        │
   Platform DB (SQLite dev / Postgres prod):
   broker_account, algo, algo_run, position, order, fill, override, alert, …
```

### Three modes, one engine

| Component | Backtest | Forward-test (paper) | Live |
|-----------|----------|----------------------|------|
| **Clock** | simulated (bar→bar) | wall-clock | wall-clock |
| **DataFeed** | historical replay (`skas-data`) | live quotes / ticks | live quotes / ticks |
| **BrokerAdapter** | `BacktestBroker` (sim fills on history) | `PaperBroker` (sim fills on live prices) | `LiveBroker` (real Zerodha orders) |

The shared core is `engine/execution.py` (`SliceExecutor`: stops → strategy decisions → fills),
used by both `BacktestRunner` and the live `LiveSession`. Parity tests
(`tests/test_sst_parity.py`, `tests/test_mode_equivalence.py`) guard the equivalence.

## Layout

```
src/skas_algo/
  api/          FastAPI app + routes (health, backtest, brokers, data, live, trade)
  engine/       runner, execution, portfolio, overrides, sim_fill, stops, report, metrics
    options/    Black-Scholes, margin, charges, settlement, live chain, contract specs
    indicators/ supertrend, …
  strategies/   ~15 strategies + registry (see below)
  live/         manager (running paper/live sessions), recovery, persistence, quotes, seed
  brokers/      BrokerAdapter base, sim_broker, zerodha (TOTP login)
  data/         market-data provider + options provider (wraps skas-data), universes
  services/     backtest, screeners (donchian_strangle, fibret), vault export, broker, runs
  notify/       Telegram alerts
  security/     Fernet encryption for broker creds at rest
  db/           SQLAlchemy models, enums, base
web/            React + Vite + TS PWA (TanStack Query, Recharts, Tailwind)
alembic/        DB migrations
docs/           PLAN.md + feature/roadmap plans
tests/          ~40 test files (engine, strategies, options, live, API, parity)
```

## Strategies

Registered in `src/skas_algo/strategies/registry.py` — new algos onboard by registering, not by
touching the engine:

- **Equity:** SST-LIFO, SST-FIFO, SST-Weekly (+ FIFO variant), SuperTrend Momentum, Nifty Shop,
  Custom Equity.
- **Options:** Short Premium, Call / Put / Batman Ratio Monthly, HNI Weekly, Staggered Covered Call,
  Custom Options, **Donchian Strangle Monthly** (multi-underlying basket short-strangle with
  per-name breach→flip governance and a portfolio-level stop/target).

## Notable features

- **Live options deployment** from screeners (Donchian strangle, Fibonacci retracement): basket
  entry, manual leg overrides, live payoff charts, greeks/margin tracking, dead-leg tolerance.
- **Override engine** — every exit/sizing decision passes through a resolver, so rules can be set
  pre-trade (config) or injected live mid-session (intervention).
- **Crash recovery** — running paper/live sessions are rebuilt from the DB on restart
  (`live/recovery.py`); Zerodha token expiry self-heals.
- **Safety gates** — `SKAS_LIVE_TRADING_ENABLED` kill-switch **and** a per-account `armed` flag;
  no real order fires unless both are set. Broker creds are Fernet-encrypted at rest.
- **Trading Brain** — exports run-cards + a journal to an Obsidian vault for offline review with
  Claude Desktop (`skas-algo export-vault`; see [`docs/trading-brain.md`](docs/trading-brain.md)).

## Getting started

### Backend

```bash
python -m venv venv && source venv/bin/activate
pip install -e ".[dev]"          # also install skas-data: pip install -e ../skas-data
cp .env.example .env             # then fill in the required values

# Generate the encryption key (required to connect a broker):
python -c "from skas_algo.security import generate_key; print(generate_key())"
# → set SKAS_SECRET_ENCRYPTION_KEY in .env

skas-algo                        # runs the FastAPI server (uvicorn) on :8080
```

The schema is created on startup (idempotent `create_all`); Alembic migrations live in `alembic/`
for evolving an existing DB. Local dev uses SQLite (`skas_algo.db`); production uses Postgres.

### Web UI

```bash
cd web
npm install
npm run dev      # Vite dev server on :5173, proxies /api → :8080 (WebSocket included)
```

### Connecting Zerodha

Add an account in the **Brokers** tab (label, user_id, api_key, api_secret). The api_secret is
stored encrypted. You log in to Kite yourself, paste the `request_token`, and it's exchanged for the
daily access token. Real orders additionally require `SKAS_LIVE_TRADING_ENABLED=true` and arming the
account.

### Docker (VPS)

`docker-compose.yaml` brings up the FastAPI backend + Postgres. Set `SKAS_SECRET_ENCRYPTION_KEY`
(and other secrets) in the environment.

## Testing

```bash
pytest                  # full suite with coverage (configured in pyproject.toml)
ruff check .            # lint
mypy src                # type-check
```

## Docs

- [`docs/PLAN.md`](docs/PLAN.md) — the foundational implementation plan and design rationale.
- [`docs/PLAN-live-options-deployment.md`](docs/PLAN-live-options-deployment.md),
  [`docs/PLAN-staggered-covered-call.md`](docs/PLAN-staggered-covered-call.md),
  [`docs/PLAN-hni-weekly.md`](docs/PLAN-hni-weekly.md) — feature plans.
- [`docs/PLAN-app-screens-redesign.md`](docs/PLAN-app-screens-redesign.md),
  [`docs/PLAN-ios-app.md`](docs/PLAN-ios-app.md) — UI / app plans (**app dev paused**).
- [`docs/trading-brain.md`](docs/trading-brain.md) — Obsidian vault export + Claude Desktop review.
</content>
</invoke>
