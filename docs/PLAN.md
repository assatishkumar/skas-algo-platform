# SKAS Algo Platform — Implementation Plan

## Context

You've backtested the **SST** strategy (and 7 others) in `skas-trading` and want to take it
**live / forward-test in a real platform** with a browser UI and an iOS app. Today everything is
CLI-only backtesting; there is no live execution, no UI, no persistent state, and daily Zerodha
login requires manually copying a request token.

This plan builds a single **single-user** platform (`skas-algo-platform`) that runs multiple algos in
**backtest**, **forward-test (paper)**, and **LIVE** modes — all from the **same codebase, same strategy
code, same engine** — across **stocks and derivatives**, with extensive reports, a PWA web UI (installable
on iOS), per-position rule overrides + live intervention, simplified TOTP-automated broker login behind a
multi-broker abstraction, and push + Telegram alerts.

### Decisions locked in
- **One engine, three modes (the core constraint):** backtest, forward-test (paper), and live all run the
  **identical strategy + engine + override + risk + reporting code**. Only three pluggable pieces swap —
  the **clock**, the **data feed**, and the **broker adapter**. What you backtest is literally what you trade.
- **Users:** single-user (you). No multi-tenancy/billing.
- **Frontend:** PWA web-first (React) — installable on iOS, push notifications; native app later.
- **Broker:** Zerodha first, **TOTP-automated login** (auto-generate 2FA code → no daily request-token copy). Built behind a `BrokerAdapter` interface so Angel One / Fyers / Upstox / Dhan plug in later.
- **Go-live:** paper/forward-test first, then flip per-algo to LIVE.
- **Hosting:** local now (Mac), design for a cloud VPS move before real money.
- **Overrides:** both pre-trade config rules AND mid-session live intervention from the app.
- **Alerts:** PWA push + Telegram bot.
- **Codebase:** build **fresh** (`skas-algo-platform`). **Reuse `skas-data`** as the data/caching layer (it's clean: 67 tests, coverage, lint/type config, `DataProvider` ABC). Reference `skas-trading` for SST/strategy logic. Do **not** carry over `skas-options` code.

### Reuse decisions
- **`skas-data` → REUSE as a dependency.** It cleanly abstracts historical stock+options caching (DuckDB) and Kite market data via a `DataProvider` ABC (`fetch_historical`, `get_quote`, `search_instruments`). The new platform consumes it for all market data + backtest data. We do **not** fork it.
- **`skas-data` does NOT abstract order execution** (data-only). The platform adds a new `BrokerAdapter` interface for orders/positions/funds/login — this is net-new.
- **`skas-trading` → PORT strategy logic.** SST / SST-LIFO rules (20-day Donchian breakout entry, per-lot/tiered profit-target exits, capital-in-parts sizing, STCG/withdrawal modeling) are reimplemented against the new Strategy interface. Reference: `skas-trading/strategies/sst_lifo/strategy.py`, `strategies/sst/strategy.py`, `strategies.md`.

---

## Architecture

A modular monolith (easy local dev) with clear seams so pieces can split into services on the VPS.

```
                         ┌─────────────────────────────────────────┐
   iOS PWA / Browser ───▶│  React PWA (web UI + push, installable)  │
                         └───────────────┬─────────────────────────┘
                                         │  REST + WebSocket (live updates)
                         ┌───────────────▼─────────────────────────┐
                         │  FastAPI Backend (skas-algo-platform)    │
                         │  • Auth (single-user)                    │
                         │  • Algo control (start/stop, mode)       │
                         │  • Overrides API (config + live)         │
                         │  • Reports API                           │
                         │  • WebSocket hub (positions/PnL/alerts)  │
                         └───┬───────────────┬─────────────┬────────┘
                             │               │             │
              ┌──────────────▼───┐   ┌───────▼─────────┐  ┌──▼────────────┐
              │  Algo Engine     │   │ BrokerAdapter   │  │ Notifier      │
              │  (mode-agnostic) │   │ • BacktestBroker│  │ • PWA push    │
              │  • Clock ◄─swap  │   │ • PaperBroker   │  │ • Telegram    │
              │  • DataFeed ◄swap│   │ • LiveBroker    │  └───────────────┘
              │  • Strategy API  │   │   (Zerodha TOTP)│
              │  • Override eval  │  └───────┬─────────┘
              │  • Risk manager  │           │
              └────────┬─────────┘           │ orders / positions / funds
                       │                      │
       DataFeed swaps ─┤              ┌───────▼──────────┐
        by mode:       │              │  Live ticks      │
              ┌────────▼─────────┐   │  (Kite WebSocket)│
              │  skas-data (dep) │   └──────────────────┘
              │  hist replay +   │   (forward-test + live)
              │  live quotes     │
              │  DuckDB cache    │
                       │
              ┌────────▼─────────────────────────────┐
              │  Platform DB (Postgres / SQLite-dev) │
              │  algos, positions, orders, fills,    │
              │  overrides, runs, reports, secrets   │
              └──────────────────────────────────────┘
```

### Tech stack
- **Backend:** Python 3.11, **FastAPI** + Uvicorn (matches `skas-data`), **SQLAlchemy** + Alembic.
- **DB:** SQLite for local dev → **Postgres** on VPS. (Market data stays in `skas-data`'s DuckDB.)
- **Async runtime:** asyncio event loop; one supervised task per running algo. Optional **APScheduler** for market-open/close jobs and pre-open broker login.
- **Realtime:** FastAPI **WebSocket** to the UI; Kite **KiteTicker** WebSocket for live ticks.
- **Frontend:** **React + Vite + TypeScript**, PWA (service worker, Web Push via VAPID), TailwindCSS, TanStack Query, lightweight charts (Recharts / TradingView lightweight-charts).
- **Secrets:** `.env` + OS keyring locally; design for cloud secret manager later. Encrypt broker creds/TOTP at rest.
- **Packaging:** Docker Compose (backend + db + web) mirroring `skas-data`'s setup.

---

## Execution modes — one engine (backtest = forward-test = live)

The engine never knows which mode it's in. A strategy emits signals against an `AlgoContext`; the context
is wired with three swappable components. Switching mode = swapping these three, nothing else.

| Component | Backtest | Forward-test (paper) | Live |
|-----------|----------|----------------------|------|
| **Clock** | `SimulatedClock` — jumps bar→bar, runs as fast as CPU allows | `RealClock` — wall-clock, market hours | `RealClock` |
| **DataFeed** | `HistoricalReplayFeed` — replays cached OHLC/ticks from `skas-data` | `LiveFeed` — real-time quotes/ticks (`skas-data` quotes + KiteTicker) | `LiveFeed` (same) |
| **BrokerAdapter** | `BacktestBroker` — simulated fills on historical bars (slippage/commission model) | `PaperBroker` — simulated fills on **live** prices | `LiveBroker` — real Zerodha orders |
| **Speed** | as-fast-as-CPU (years in seconds) | real-time | real-time |

Everything else is shared and identical across all three modes:
- The **Strategy** code (SST, etc.) — written once.
- The **override resolver**, **risk manager**, **position/PnL accounting**, **persistence**, and **reports**.
- The event flow: `DataFeed → Strategy → Signal → override resolver → risk → BrokerAdapter → Fill → portfolio`.

**Why this matters:** backtest results, forward-test results, and live behaviour come from the *same lines of
code*. No "backtest engine vs live engine" drift — the classic failure mode of trading systems. The only
differences are data source, time, and whether fills are simulated or real. `BacktestBroker` and `PaperBroker`
share the same simulated-fill core (slippage, commission, Black-Scholes for options); they differ only in
whether prices come from history or the live feed.

**Migration of existing work:** `skas-trading`'s CLI backtest becomes `Mode=BACKTEST` on this engine. The SST
backtest run is reproduced by wiring `SimulatedClock + HistoricalReplayFeed + BacktestBroker` and replaying the
same date range — this is also the primary correctness gate (must match the old numbers).

---

## Core domain model (Platform DB)

- `broker_account` — broker, api_key, encrypted api_secret, **encrypted TOTP secret**, user_id, session token + expiry.
- `algo` — name (e.g. SST-LIFO), strategy_id, instrument_class (STOCK/DERIV), mode (PAPER/LIVE), status, capital, params JSON, broker_account_id.
- `algo_run` — per-day/session run with start/stop, mode, snapshotted params.
- `position` — algo_id, symbol, qty, avg_price, lots, opened_at, status, realized/unrealized PnL.
- `order` / `fill` — full audit trail (mirrors `skas-options` trade_log richness, persisted).
- `override` — scope (algo | symbol | position), rule JSON (e.g. `{exit: [{at_pct:6, action:"book", qty_pct:50},{action:"trail_sl", trail_pct:2}]}`), source (CONFIG | LIVE), active flag.
- `alert` — type, payload, channel, delivered_at.

---

## The Strategy & Override interfaces (the heart of it)

**Strategy interface** (port SST logic to this):
```python
class Strategy(Protocol):
    def on_bar(self, ctx: AlgoContext, bar: Bar) -> list[Signal]: ...
    def on_tick(self, ctx: AlgoContext, tick: Tick) -> list[Signal]: ...
    def initial_state(self, params: dict) -> dict: ...
```
- `AlgoContext` exposes positions, funds, data access (`skas-data`), and the **override resolver**.
- SST-LIFO ported from `skas-trading/strategies/sst_lifo/strategy.py`: track 20-day low/high, breakout entry, per-lot exit at target %, capital/parts sizing.

**Override engine** — every exit/sizing decision passes through a resolver:
1. Strategy proposes a default action (e.g. "exit 100% at 6%").
2. Resolver checks active `override` rows for that position/symbol/algo.
3. Applies the override (e.g. "book 50% at 6%, convert remainder to trailing SL @2%").
4. Live intervention = inserting/updating an `override` row from the app mid-session; the running algo re-reads on its next decision tick.

This single seam satisfies feature #4 for both **config** and **live intervention**.

---

## Broker abstraction + simplified login (feature #5)

```python
class BrokerAdapter(Protocol):
    def login(self) -> Session: ...          # TOTP-automated, no manual token
    def place_order(self, o: Order) -> str: ...
    def modify_order(...); def cancel_order(...)
    def positions(self) -> list[Position]: ...
    def funds(self) -> Funds: ...
    def subscribe_ticks(self, symbols, cb): ...  # KiteTicker
```
Three implementations behind this one interface (see Execution modes above):
- **`BacktestBroker`** — simulated fills on historical bars; no login. Used in Mode=BACKTEST.
- **`PaperBroker`** — same simulated-fill core, but priced off the **live** feed; no real orders. Used in forward-test.
- **`LiveBroker` (ZerodhaAdapter)** — TOTP-automated login: stored `user_id` + `password` + **TOTP secret** → programmatically complete the Kite login flow and exchange for the daily `access_token`, cached with expiry. Run automatically pre-open via scheduler. Places real orders. Market data still flows through `skas-data`'s `KiteProvider`.

`BacktestBroker` and `PaperBroker` share one simulated-fill module (slippage, commission, Black-Scholes for
options; reference `skas-trading` fill logic) — they differ only in price source.

⚠️ **ToS note:** automating Zerodha username/password login is against Kite ToS. Angel One **SmartAPI** supports TOTP login officially — recommended as the second live adapter when you expand. The interface makes that a drop-in.

---

## Reports (feature #2)

Persist every order/fill/position to the DB, then expose:
- **Live dashboard:** open positions, live PnL, funds, per-algo status, today's fills.
- **Performance:** CAGR, total/realized/unrealized PnL, max drawdown, Sharpe, win rate, capital utilization — reuse the metric formulas already in `skas-trading` (`core/utils.py`: `xirr`, `format_inr`) and SST runners.
- **Breakdowns:** monthly/yearly PnL, per-symbol, per-algo, override-impact (what overrides changed vs default).
- **Tax/withdrawal:** STCG modeling already in SST — carry forward.
- **Export:** CSV/PDF; reuse trade-log schema from `skas-trading`.

---

## Phased roadmap

**Phase 0 — Security + scaffold (do first)**
- Rotate the leaked Kite API secret/request token (committed in `skas-data/config/config.yaml`, `skas-options/.env`, `access_token.txt`). Move all secrets to `.env`/keyring, add `.gitignore`, encrypt broker creds at rest.
- Create `skas-algo-platform` repo: FastAPI app, SQLAlchemy models + Alembic, settings, `skas-data` as a dependency, Docker Compose, pytest + lint/type config mirroring `skas-data`.

**Phase 1 — Unified engine + backtest + paper SST (no UI yet)**
- Algo Engine with the **Clock / DataFeed / BrokerAdapter** seams; Strategy interface; port **SST-LIFO** + **SST**.
- Implement `SimulatedClock + HistoricalReplayFeed + BacktestBroker` (BACKTEST) **and** `RealClock + LiveFeed + PaperBroker` (forward-test) — same engine, both modes.
- Persist positions/orders/fills; basic risk manager (max loss, position caps).
- **Correctness gate:** BACKTEST mode reproduces `skas-trading`'s SST numbers on the same date range. Then forward-test runs the identical strategy code live-paper.

**Phase 2 — Override engine + reports API**
- Override model + resolver wired into every exit/sizing decision (config + live).
- Reports endpoints (live + performance + breakdowns + export).

**Phase 3 — Web PWA**
- React PWA: dashboard, algo control (start/stop, PAPER/LIVE toggle), positions w/ live PnL via WebSocket, override panel (config + "intervene on this position"), reports/charts. Installable on iOS; Web Push.

**Phase 4 — Live broker + alerts**
- **ZerodhaAdapter** with TOTP-automated login + pre-open scheduler; KiteTicker live ticks.
- Telegram bot (alerts + approve/override via chat) + PWA push.
- Go live on **one** algo with small capital after paper validation.

**Phase 5 — Derivatives + hardening + VPS**
- Options/derivatives support in engine (lot sizes, expiry, margins; reference `skas-data` options cache + `skas-options` option pricing logic conceptually).
- Reconciliation (broker positions vs platform), kill-switch, audit, deploy to VPS with Postgres + always-on uptime + monitoring.

---

## Ways to make it better (recommendations)

1. **Backtest, paper, and LIVE share one code path** (only clock + data feed + broker adapter swap) — guarantees that what you backtest is what you forward-test is what you trade. This is the foundational design rule.
2. **Reconciliation loop** every N seconds: compare broker positions/funds vs platform state; alert + halt on drift. Critical safety net.
3. **Global kill-switch** + per-algo max-daily-loss auto-halt; square-off-all button in UI.
4. **Idempotent orders** (client order IDs) + crash recovery: on restart, rebuild state from DB + broker, never double-fire.
5. **Pre-open health check:** broker login OK, data fresh, funds sufficient, instrument master loaded — block start otherwise.
6. **Override audit / "what-if":** report shows PnL impact of each override vs the strategy default, so you learn whether your interventions help.
7. **Angel One SmartAPI** as broker #2 — officially supports TOTP login (cleaner than Zerodha automation) and de-risks ToS.
8. **Strategy registry + param schema** so new algos (your other 7 strategies) onboard via config, not code.
9. **Time-synced scheduler** for market open/close, expiry handling, and auto square-off before expiry.
10. **Observability:** structured logs + a `/health` endpoint + Telegram error alerts from day one (you can't watch a terminal during market hours).

---

## Critical files / references
- Reuse: `skas-data/src/skas_data/data_manager.py` (`SkasData`), `providers/base.py` (`DataProvider`), `providers/kite_provider.py`.
- Port logic from: `skas-trading/strategies/sst_lifo/strategy.py`, `strategies/sst/strategy.py`, `core/utils.py`, `strategies.md`.
- New code lives in a fresh `skas-algo-platform/` (engine, adapters, api, models, web).

## Verification
- **Backtest parity (primary gate):** run SST in **BACKTEST mode** over the exact date range used in `skas-trading` and assert trades / PnL / metrics match the existing backtest. This proves the unified engine is faithful.
- **Mode equivalence:** feed the *same* historical day through BACKTEST (replay) and through forward-test (replay piped as a fake "live" feed into `PaperBroker`) and assert identical fills — proves the three modes share one path.
- **Unit:** Strategy (SST entry/exit), override resolver (each rule type), simulated-fill module (slippage/commission/BS), risk manager.
- **Integration:** run SST in forward-test (paper) mode; exercise an override mid-run and confirm the booked-50%-trail-rest behavior.
- **E2E (manual):** start algo from the PWA, watch live positions/PnL over WebSocket, trigger a live override from the app, confirm Telegram + push alerts fire.
- **Live readiness:** dry-run ZerodhaAdapter login (TOTP) + a single 1-share order in LIVE before scaling; reconciliation shows zero drift.
- **Security:** confirm no secrets in git; broker creds encrypted at rest; rotated keys.
