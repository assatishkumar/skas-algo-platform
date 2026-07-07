# ARCHITECTURE — skas-algo-platform

> The system as-built, the invariants that keep it safe, how to change it without breaking
> live trading, and a prioritized hardening roadmap. `CLAUDE.md` is the terse "how to work
> here" layer; this is the "how it fits together and why" layer. Code is truth — every
> claim below carries a file reference; when they disagree, trust the code and fix the doc.

Written after the 2026-07 architecture review (security + robustness + performance sweeps).

---

## 1. System overview

One FastAPI process (`api/app.py`, uvicorn on `:8080`) serves the REST/WebSocket API and
hosts the live trading engine in-process. A Vite dev server (`web/`, `:5173`) proxies
`/api` to it.

- **One engine, three modes.** `engine/execution.py::SliceExecutor` is the shared core.
  `engine/runner.py` drives it for backtests; `engine/live.py::LiveSession` drives it for
  paper/live. Only the Clock, DataFeed, and BrokerAdapter differ between modes — this is
  the **parity invariant** (§6), and it is what makes strategy development safe: a strategy
  proven in backtest behaves identically in paper and live.
- **In-memory singleton.** `live/manager.py::manager` (a `LiveRunManager`) holds every
  running deployment (`LiveRun`) in memory, each with its own `LiveSession`, portfolio,
  quote source, and per-run asyncio loop. There is no shared mutable state between runs.
- **Persistence = SQLite (WAL).** `skas_algo.db` holds platform state: accounts,
  deployments, the persisted `state` per run, and the Order/Fill audit trail. Market data
  is NOT here — it lives in the sibling `skas-data` DuckDB cache (`../skas-data`).
- **Recovery.** On restart, `live/recovery.py` rebuilds every still-running deployment from
  its persisted `state` + `params_snapshot`. Kite tokens self-heal.
- **Single user, no auth.** By design (§5). The process binds `127.0.0.1`.

```
  Browser (web/ :5173) ──proxy──▶ FastAPI (:8080) ──┬─ REST/WS routes (api/routes/*)
                                                     ├─ LiveRunManager (in-memory)
                                                     │    └─ per-run asyncio loop ─▶ tick pool
                                                     │         └─ LiveSession ─▶ SliceExecutor
                                                     ├─ SQLite WAL (state + audit + backups/)
                                                     └─ skas-data DuckDB (market data, RO here)
       KiteTicker WS ──push──▶ PriceFeed cache ──▶ quote sources ──▶ runs
```

---

## 2. Price / data flow

**Common abstraction, not per-strategy.** Every run reads marks through the `QuoteSource`
protocol (`live/quotes.py`): `get_quotes(symbols) -> {symbol: price}`. Strategies never
call a broker directly. Three implementations:
- `CacheQuoteSource` — latest cached close from skas-data (offline / degraded).
- `ZerodhaQuoteSource` — batched `kite.ltp()` (one REST call for N symbols); also wraps Dhan.
- `FeedQuoteSource` (`live/pricefeed.py`) — the WebSocket feed with a REST fallback (below).

**WebSocket feed (`live/pricefeed.py`).** One `KiteTickerFeed` per broker account (a
registry like the order governor) streams LTPs into a thread-safe last-tick cache; every
run on that account reads the cache. `FeedQuoteSource` serves a mark from the cache when
it's fresh (`ws_feed_stale_s`, default 10s in-market) and **falls back to batched REST on
any miss/staleness** — so a dead socket, an auth failure after a token refresh, or an
un-ticked symbol degrades to REST rather than serving a wrong/missing price. It exposes
`.adapter`, so the ~7 `getattr(quote_source, "adapter")` call sites (order gate, reconcile,
margin, chain wiring) are unchanged. Gated behind `settings.ws_feed_enabled` (default True)
+ zerodha; Dhan/cache paths are untouched and a Dhan feed can slot into the same surface.

> **Prices push; decisions stay loop-driven.** There are deliberately NO raw tick callbacks
> into strategy code — that would make live behave differently from backtest (which has no
> ticks) and break parity. The cache just makes each loop's `get_quotes` instant.

**Option chains stay REST.** LTP-mode WS carries no OI/depth, and chains are selection-time
(not per-tick mark-time). Chain snapshots (`live_chain`) remain REST + TTL cache (15–20s in
`engine/live_options_market.py` / `live_chain.py`). Margins: `basket_order_margins()` ~1/min
per run (`manager._maybe_refresh_margin`, throttled). Reconciliation: `positions()` hourly
for LIVE runs only.

**Call budget.** Before the feed, ~20 runs cost ~80–240 `kite.ltp` calls/min for marks
alone. With the feed, steady-state marks are ~0 REST (push); REST remains for chains,
margins, and fallback. Kite REST cap is ~10/s and WS supports ≤3000 instruments/connection.

---

## 3. Order path & safety chain

**`brokers/live_broker.py::LiveBroker` is the ONLY code that places real orders.** It
satisfies the engine's `execute(BrokerOrder) -> Fill` contract, so the entire shared
decision path is untouched — a LIVE session gets a LiveBroker injected instead of the
default PaperBroker.

- **4-key injection gate** (`manager._maybe_inject_live_broker`): a LiveBroker is injected
  ONLY when mode==LIVE **and** `settings.live_trading_enabled` **and** the account is
  **armed** **and** the adapter exposes the full order surface (`adapter_can_execute`).
  Every other combination keeps PaperBroker (matrix-tested in `tests/test_live_broker.py`).
- **Execution style:** LIMIT at touch (SELL@bid / BUY@ask) → poll ~2s → `order_timeout_s`
  (10s) → modify to MARKET → poll to terminal. Partial fills ≥1 unit are booked at actual
  qty. Reject/cancel/unfillable → `OrderExecutionError`.
- **Rails (pre-flight, before the broker sees anything):** market-open + holiday check,
  per-order notional cap, per-run daily order cap, account-level rate governor (shared
  across runs; sleeps outside its lock).
- **Halt/ack:** an `OrderExecutionError` or the hourly book-reconciliation mismatch sets
  `LiveRun.order_error` → decisions HALT until the owner acknowledges
  (`POST /live/{id}/ack-order-error`). Reconciliation compares the broker's NET book vs the
  AGGREGATE of all live-order runs on the account.

> **Owner directive (permanent): Claude never initiates live orders.** No arming, no setting
> the flag, no deploying a LIVE+armed run, no "verify with a real order." Order-path
> verification = fake-adapter tests only. See `CLAUDE.md` §1.

**KNOWN GAP → double-fill window (P1).** Real fills are booked into the in-memory portfolio
inside `run_decision` *before* the tick's `record_trades` + `_persist_state` commit. If the
process dies in that window, recovery restores the pre-fill book and the strategy may
re-enter → a second real order. `Order.client_order_id` is unique but is minted *after* the
fill, so it doesn't currently dedup. Hourly reconciliation only *surfaces* the divergence
later. **P1 fix:** on LIVE-run recovery, reconcile the broker book BEFORE the first decision
and dedup by a pre-placement client_order_id/positions check. Until then, mitigations: the
paper twin, the hourly halt, and the dedicated-account recommendation.

---

## 4. Failure modes & recovery

- **Restart / recovery.** `live/recovery.py` rebuilds runs from persisted `state` +
  `params_snapshot`; it never replays past decisions (no replay double-fire). A running
  deploy's params come from its snapshot — so redefining a param's *meaning* changes a live
  position's risk on the next recovery (§6 rule 2).
- **State-loss window.** Between a fill and its persist (see §3 gap). Everything else
  persists at the end of each `refresh`/`run_decision`.
- **Process supervision (P1).** The documented daily driver is a bare `nohup venv/bin/skas-algo`.
  Stops are **engine-side only** (evaluated in `run_decision`); there is no broker GTT/OCO.
  A box reboot with open positions and no restart = unmanaged exposure. Add supervision
  (launchd/systemd) so the process always comes back — this is the top operational P1.
- **Watchdog.** The manager maintenance task (5-min) restarts any AUTO run whose loop task
  died silently and Telegram-alerts it (`manager._watchdog_scan`).
- **Holidays.** `live/holidays.py` makes `is_market_open` treat NSE holidays like weekends
  (marks re-price read-only; no decisions/orders). Festival dates are PROVISIONAL —
  env-correctable via `NSE_HOLIDAYS_ADD` / `NSE_HOLIDAYS_REMOVE` (VERIFY against the NSE
  circular).
- **Backups.** `services/backup.py` VACUUM-INTO snapshots to `backups/` (retain 7): one per
  startup (pre-recovery) + one daily ~16:30 IST. **On-box only** — off-box durability is the
  owner's job (copy `backups/` off the machine periodically).

### Operational runbook
- **Start (repo root!):** `venv/bin/skas-algo`. Relative SQLite path means the CWD must be
  the repo root or you open a different, empty DB.
- **Restart cleanly:** `pkill -9 -f "venv/bin/skas-algo"` (exact pattern — a bare
  `skas-algo` also matches vite/esbuild via the repo path), then check
  `lsof -nP -iTCP:8080 -sTCP:LISTEN` and kill any `--reload` orphan by PID, then launch ONE
  backend from the repo root.
- **Before any restart/deploy:** run `./scripts/preflight.sh` (§7).
- **"Looks stuck" is usually a dead backend or no broker session, not a hang** — check
  `GET /api/v1/brokers` (`has_session`) first.
- **UI can't reach the backend after the localhost-bind change?** Confirm the backend is up
  and, if running in a container/remote, that `SKAS_API_HOST` is set (default is now
  `127.0.0.1`).

---

## 5. Security model

- **Localhost, single-user, no route auth — by design.** The API binds `127.0.0.1`
  (`config/settings.py`); only the container path sets `SKAS_API_HOST=0.0.0.0` (network
  isolation + firewall are the control there). Do NOT expose `:8080` on a public/LAN
  interface without a reverse proxy + auth — arm/force-entry/flatten/delete are anonymous,
  and CORS does not protect a direct HTTP client.
- **Credentials at rest.** `api_secret` + `session_token` are Fernet-encrypted
  (`security/crypto.py`, key from `SKAS_SECRET_ENCRYPTION_KEY`). No secrets appear in API
  responses, logs, snapshots, WS broadcasts, or the repo. `.env` and tokens are gitignored.
- **Gate chain.** The live-order path is 4-key gated (§3) + `_ensure_armed` in the adapter +
  pre-flight rails. Sound; the exposure is at transport (localhost bind closes it).
- **Non-goals (current):** multi-user auth, RBAC. The paused iOS app would add token auth.
- **P1 items:** encrypt `api_key` too (currently plaintext in the DB — can't trade alone,
  but it's a live credential); a CSRF token / bearer token if the surface ever widens beyond
  localhost; `MultiFernet` key rotation; pin dependencies.

---

## 6. Guidelines — the constitution

The rules that keep changes safe. Violate one and you risk breaking live silently.

1. **Parity is sacred.** `tests/test_sst_parity.py`, `test_sst_fifo_parity.py`,
   `test_mode_equivalence.py` are the engine's golden tests. A shared-path change must keep
   them byte-identical. Never weaken an assertion to fit a feature — add a *gated branch*
   that defaults to the old behavior.
2. **Never change the meaning of a persisted param.** Running deploys rebuild from
   `params_snapshot` on every restart. New behavior gates behind a new flag that defaults
   to the OLD behavior; only new deploys opt in.
3. **Event dicts grow by additive, conditional keys only.** A new key in a `trade_event`
   must not appear in backtest/paper dicts that parity compares (see the `broker_order_id` /
   `commission` pattern in `engine/execution.py`).
4. **Strategies never touch a broker or the engine internals.** They read marks via
   `QuoteSource` and the chain view; they onboard via `strategies/registry.py`, never by
   editing `engine/`. New strategies ship with their own test file.
5. **One order path.** `LiveBroker` is the only place real orders may ever be added. Never
   widen the 4-key gate. Claude never initiates a live order (§3).
6. **Every displayed date carries the time (HH:MM) on live paths** — trades, banners,
   alerts, tooltips. Carry the full datetime end-to-end; backtest daily events may be
   date-only.
7. **Broker-margin-only for new option strategies** where risk is margin-based (the DNM/CPRE
   pattern): freeze `margin_base` from the broker push, not the model estimate.
8. **Schema changes:** new *tables* may rely on `create_all` at startup; new *columns*
   require an Alembic migration (`create_all` silently skips them — drift risk).
9. **Test isolation must cover DB, Fernet, notifiers, AND the WS feed.** `tests/conftest.py`
   blanks Telegram creds and forces `SKAS_WS_FEED_ENABLED=false` so tests never page the
   owner or open a real socket. Any new external side-effect needs the same treatment.
10. **The paper twin + /research BS-calibration are the live regression harness** for fill
    quality — keep a paper deployment alongside a new live one and read the slippage.

---

## 7. Developing without breaking live

The workflow that lets you build/upgrade continuously without silently breaking a live
strategy or the engine:

1. **`./scripts/preflight.sh` is the gate before ANY restart/deploy.** It runs ruff
   (advisory), the FULL test suite including the parity/mode-equivalence suites, and the web
   typecheck. Green = the change didn't alter the engine or a live path. It auto-deselects
   the two DuckDB-cache parity suites while a backend is live (they need exclusive cache
   access) and tells you to re-run those once against a stopped/reloaded backend.
2. **Add a test with every strategy and every fix.** The suite only protects you if it
   covers the path. A regression test that reproduces a bug before the fix is the standard
   here (see the settlement / timestamp / force-entry fixes).
3. **Respect the constitution (§6)** — especially rules 1–3 for anything under `engine/` or
   `live/`, and rule 2 for anything a running deploy persists.
4. **Restart discipline:** preflight green → single clean restart (runbook §4) → verify the
   recovered-run count and one live snapshot before walking away.
5. **When unsure whether a change touches the shared path,** it does if a parity test moves.
   Run preflight and let the tests answer.

---

## 8. Prioritized roadmap

**P0 — done (2026-07 review):** localhost bind; NSE holiday calendar; DB backups +
retention; dead-loop watchdog; dedicated tick executor; governor lock fix; in-memory history
cap; WebSocket price feed; `preflight.sh`; this doc.

**P1 — do next (each has a clear trigger):**
- **Process supervision** (launchd/systemd) — *before* the first real-money pilot runs
  unattended. Highest operational priority.
- **Close the double-fill window** (§3) — before scaling live size past the 1-lot pilot.
- **`greeks_snapshot` retention job** — the table grows ~375 rows/run/day unbounded; add a
  pruning job when the DB crosses a size you care about.
- **Encrypt `api_key`; `MultiFernet` key rotation; pin dependencies; CSRF/bearer token** if
  the API surface ever leaves localhost.

**P2 — at scale (50+ runs / multi-year):**
- Serialize/guard concurrent DuckDB cache reads across tick threads (single-connection
  hazard at high fan-out).
- Move platform state from SQLite to Postgres (the docker-compose path already exists).
- Backpressure/adaptive sizing on the tick pool; consider `MODE_FULL` on the feed if depth
  is ever needed for marks.
