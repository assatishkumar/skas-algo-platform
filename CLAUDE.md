# CLAUDE.md — working notes for agents

Operational nuances + invariants for this repo. The README orients you; `docs/` holds design intent;
**this file is the "how to work here safely" layer.** Keep it tight and high-signal.

> **Maintain this file.** As the platform matures, update CLAUDE.md when a new invariant, footgun, or
> convention emerges — not after-the-fact docs, but the things a fresh session would otherwise have to
> rediscover. (Standing request from the owner.)

> **Full system map:** `docs/ARCHITECTURE.md` — the as-built architecture (price/data flow,
> order path, failure modes, security model), the guidelines "constitution", how to develop
> without breaking live, and the P0/P1/P2 hardening roadmap. Read it before large changes.
> **Feature catalog:** `docs/FEATURES.md` — a detailed, plain-language catalog of everything
> implemented (all strategies, backtest, live, order path, brokers, feed, research, UI, API,
> config). Start here to learn WHAT the platform does.

## 1. This is a real, live trading system with real money
- **The real-order path is LIVE-CAPABLE (Phase B, 2026-07).** `brokers/live_broker.py::LiveBroker`
  is the ONLY code that places real orders (LIMIT-at-touch → 10s → MARKET escalation, via
  `ZerodhaAdapter.place_order/modify/status/cancel`, all behind `_ensure_armed`). It is injected by
  `live/manager._maybe_inject_live_broker` ONLY when ALL of: mode=="LIVE" ∧ account.armed ∧
  `SKAS_LIVE_TRADING_ENABLED` ∧ adapter has the full order surface — every other cell keeps
  PaperBroker (matrix test in tests/test_live_broker.py). Never bypass or widen this gate.
- **OWNER DIRECTIVE: Claude never initiates live orders.** Never arm an account, never set the flag,
  never deploy/activate a LIVE run with an armed account, never "verify" with a real order — the
  pilot and every real order is the owner's hand only. Order-path verification = fake-adapter tests.
- Safety rails live in LiveBroker pre-flight: per-order notional cap, per-run daily order cap,
  market-hours check, account-level rate governor (settings SKAS_LIVE_MAX_ORDER_NOTIONAL /
  _MAX_ORDERS_PER_DAY / _ORDER_TIMEOUT_S). An `OrderExecutionError` (reject/unfillable) or hourly
  book-mismatch reconciliation sets `LiveRun.order_error` → decisions HALT until the owner
  acknowledges (POST /live/{id}/ack-order-error; banner + tile chip). Reconciliation compares the
  broker's NET book against the AGGREGATE of all live-order runs on the account (broker nets per
  contract across runs; manual trades in the same account will trip it — dedicate an account).
- Do **not** run ad-hoc scripts that could place/modify/cancel orders. Be deliberate around anything
  in `live/` and order placement. When in doubt, ask.
- Tests use simulated brokers and an isolated DB — they never touch the broker or dev data.
- **Don't silently change the *meaning* of a persisted strategy param.** Running deploys are rebuilt
  from their persisted `params_snapshot` on restart (`live/recovery.py`), so redefining a param (e.g.
  Donchian's `portfolio_sl_pct` from %-of-notional to %-of-margin) would change a live position's risk
  on the next recovery. Gate the new behavior behind a new flag that **defaults to the old behavior**
  (e.g. `portfolio_basis="notional"`), and have only new deploys opt in.

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
  self-heals. **Opt-in single-operator auth** (password → JWT bearer, fail-open when unconfigured —
  `security/auth.py`, `SKAS_AUTH_PASSWORD_HASH`+`SKAS_AUTH_JWT_SECRET`); no multi-tenancy. The
  same bearer scheme covers the (paused) iOS app.

## 8. Conventions
- New strategies onboard via `strategies/registry.py`, **not** engine edits.
- **NIFTY strikes = 100-multiples only** (owner rule, 2026-07): NIFTY lists 50s but automated
  strategies must never SELECT one. Enforced centrally via `contract_specs.selection_step` /
  `eligible_strikes` (`_SELECTION_STEP={"NIFTY":100}`, extensible) at THREE candidate choke points —
  `OptionChainView.chain()` (cached/backtest), `LiveChainView._build_live_chain()` (live-adapter),
  and `LiveOptionsMarketView.live_chain()` (Path B — the deploy-only intraday strategies read
  `ctx.market.live_chain()`, NOT the chain view; it also RECOMPUTES `atm_strike` to the nearest
  surviving strike or `call_put_ratio_expiry`'s `rows.get(atm)` no-ops) — plus the two computed-step
  strategies route `_STRIKE_STEP["NIFTY"]` through `selection_step` (→100) and the donchian NIFTY
  hedge filters via `eligible_strikes`. Same filter on backtest + live keeps parity. The MANUAL
  Option builder (`custom_options`, data-route chain) is deliberately UNFILTERED. `ema21_momentum`
  predates this (its own `strike_step=100`). Coverage: `tests/test_nifty_strike_rule.py`.
- Feature branches; `main` is default. Commit/push only when asked.
- ruff + black + mypy, line-length 100. `pytest` runs with coverage (see `pyproject.toml`).
- Active frontier: the **Donchian basket strangle** (`donchian_strangle_monthly`) — note it has **no
  backtest path**; it's only deployed live/paper from the screener. Its **backtest-only sibling** is
  `donchian_strangle_bt`: a subclass that re-enters expiry-anchored cycles from a schedule injected
  by `services/donchian_bt.build_cycle_schedule` (the "backtest screener") — the live class stays
  byte-identical (its live-market calls are `getattr`-guarded and fall back to `ctx.close`). Stock
  option premiums are **synthetic Black-Scholes** (σ = HV20 × `vol_multiplier`; no stock-chain
  history exists — `data/basket_options.py` routes NIFTY contracts to the real cached chain, stocks
  to BS); calibrate the multiplier on the **/research** page (BS-vs-live panel, ~1.1 as of Jul 2026).
  Stock lot sizes in `contract_specs._STOCK_LOT_SIZES` are a FLAT 2026-07 Kite snapshot.
- The **/research** page: Donchian breakout study (cache-only daily bars; expiry-anchored cycles;
  channel breakout/re-entry/whipsaw stats + live-rule flip simulation) and the BS-vs-live
  calibration (session-gated, strictly read-only). Backend: `api/routes/research.py`,
  `services/donchian_study.py`, `services/bs_calibration.py`.
- **Donchian entry gates** (from the run-186 loss study; danger = vol COMPRESSION + tight channel,
  NOT rising vol): `min_hv_ratio` (HV20/HV60, ~0.85) and `min_channel_width_pct` (~8) exist in BOTH
  the backtest schedule builder and the live screener (`DonchianParams`; default 0 = off; excluded
  rows keep their legs). The backtest additionally has VIX half/skip rules + notional-per-name
  sizing (₹7.5L default — the flat lot table is split-unsafe, see KOTAKBANK); live, VIX is an
  ADVISORY banner only (lots are the owner's call). Beware: the live screener's auto range window
  is cycle-TO-DATE, so early in a cycle the width gate excludes ~everything (correctly — the
  strikes really would hug spot); it reads like the backtest only late-cycle or with a range
  override.
- **Ratio-family auto sizing** (`call_ratio_monthly` + put/batman/HNI): `sizing="margin"`
  refits lot-sets to CURRENT equity at every entry — divisor = era-true model margin
  ((span+exposure)% × spot × short-body units; deterministic in both modes — never
  `ctx.position_margin()`, which is model-in-BT vs broker-live ≈2× apart) — and scales the
  rupee credit gates with the same equity. Constructor default stays `sizing="fixed"` (§1);
  the backtest FORM defaults to auto. Model margin ignores the long hedges (≈2× broker
  SPAN), so `capital_utilization_pct=95` ≈ ~50% broker margin — the knob is the live
  calibration point. Manual `LiveControlsInput.lots` only bites in fixed mode.
- **Two brokers.** `BrokerAccount.broker` ∈ {zerodha, dhan}; `services/broker.make_adapter`
  dispatches. **Dhan** (`brokers/dhan.py`): no api key/secret — client id + a portal-generated
  JWT the user PASTES (its `exp` claim is the session expiry); instruments resolve via the
  public scrip-master CSV (module-cached daily; underlying recovered with `rsplit("-", 3)` —
  hyphenated names like BAJAJ-AUTO); `basket_margin` = Σ per-SHORT-leg margins (Dhan has no
  basket API → overstates, conservative); its option-chain endpoint is throttled (~1/3s) so
  the 50-name screeners STAY on Zerodha, as does the skas-data cache refresh (Kite-coupled —
  `make_data_session` rejects dhan accounts). **Dhan live quotes/chains need the paid "Data
  APIs" subscription** (error 806 without it; expirylist/funds/orders don't) — verified
  2026-07-03 on the owner's account. quote_source ∈ {cache, zerodha, dhan} — the
  broker sources are gated by `live/quotes.is_broker_source`, and the source must match
  `account.broker`. **No broker places real orders yet** — even LIVE mode fills via
  PaperBroker; the real order path (LiveBroker, LIMIT-at-touch→market, double-gated) is the
  planned Phase B and the only place order code may ever be added.
- **21_ema_momentum** (`strategies/ema21_momentum.py`, NIFTY): daily EMA(21)-on-high/low
  channel; fresh close beyond the band at 15:20 → OTM 100-pt credit spread (bull put /
  bear call), width 300-500, credit ₹80-140 (ideal 90-130 preferred; miss → SKIP and
  retry while the direction stays armed); hold till the opposite signal (close+reverse in
  one slice); roll `roll_days_before`(5) days pre-expiry; expiry = current month before
  the 15th else next. FULL engine backtest (real cached chain — the first new-strategy
  since the ratio family to ride `build_options_run` untouched). Daily H/L comes via a
  strategy-side `set_daily_bars_fn` hook (options views are close-only): backtest wires
  the cache in `services/backtest.py`; live wires **broker-first daily bars**
  (`manager._broker_daily_df`, fresh Kite `daily_bars`) + today's-intraday-bar in
  `_wire_quote_source`, cache as fallback (bands INCLUDE today's forming bar — chart-at-15:20
  semantics; no broker session → today degrades to H=L=C=LTP). DERIV live ticks have no engine time
  gate — the strategy self-gates (15:20 + once-a-day latch that only engages AFTER bands
  computed, so a data hiccup doesn't burn the day). Margin model reads ≈2× real broker
  for the spread (no long-leg offset — ratio-family caveat).
- **call_put_ratio_expiry** (expiry-day-only 1:3 premium-ratio, NIFTY Tue / SENSEX Thu):
  buy ATM straddle 09:20-09:27, sell 3 lots/side at the strikes trading nearest ⅓ of each
  ATM premium (LIVE-chain lookup; >30% tolerance miss → skip the day, `traded_day` guard);
  exits +1.1% / −1% of `margin_base` or 15:20. `margin_base` is FROZEN at entry (broker
  basket margin if available else model Σ shorts — source recorded; model reads ~2×
  broker). Net short 2 lots/side beyond the ⅓ strikes — open-ended risk, stop is the only
  guard. Deploy-only + broker quote source REQUIRED (strike selection needs live premiums;
  no backtest by design — flat-vol BS would misplace the smile-driven ⅓ strikes).
- **intraday_straddle** (`strategies/intraday_straddle.py`, NIFTY / BANKNIFTY): a DAILY
  intraday short straddle on the nearest weekly. Sell ATM CE+PE (or ~0.6Δ ITM via
  `strike_delta`, which relaxes the OTM filter) at `entry_time` (default 09:18, once/day
  `entered_day` latch), exit `exit_time` (15:25, checked FIRST — never waits on margin).
  Two configurable stops off the FROZEN broker `margin_base` (pending → waits for the manager's
  `set_broker_margin` push, never the model): a fixed `-stop_loss_pct` (2%) AND a trailing stop
  that only ratchets UP (`_stop_level`) — `trail_mode="ratchet"` (each `trail_trigger_pct` of
  PEAK profit lifts the stop `trail_step_pct`) or `"below_peak"` (peak − `trail_step_pct`);
  trailing off when a trail pct is 0. No fixed profit target (the trail is the upside). Uncapped
  short-straddle tails → the stop is the only guard. Deploy-only + broker source required (live
  chain for ATM/delta); NO backtest (EOD-slice can't model intraday SL/trailing). `peak_pct`
  persists in export_state for the trail; one entry/day (a stopped-out day doesn't re-enter).
- **delta_neutral_monthly** (18Δ BANKNIFTY monthly strangle): entry expiry+2 TRADING days
  ~11:00 (force_entry deploy flag skips the wait); adjustment rule is the spec's EXAMPLE,
  not its prose — when |CE−PE| > 40% of (CE+PE), the CHEAP side rolls to the strike whose
  LTP matches the rich side, hard-capped at the other strike (straddle max, never
  crossing); straddle → breakeven hedges (K ± combined) in the SAME decision → ironfly.
  margin_base tracks the BROKER basket margin ONLY (manager `set_broker_margin` push;
  thresholds WAIT while "pending"; re-frozen after every roll/hedge — so once it's an iron fly
  the 2.5% target is of the fly's much SMALLER margin, not the straddle's); stop param default
  OFF; recurring monthly (done_expiry gates same-month re-entry). Deploy-only + broker source
  required (live-chain delta solve); NO backtest — BANKNIFTY chain history ≈ 2 months in cache.
- **Post-iron-fly adjustment (shared, 2026-07)** — on the BASE class, GATED by `ironfly_adjust`
  (**default False in delta_neutral_monthly** per §1 — a running deploy is unchanged on recovery;
  runtime-togglable via `set_ironfly_adjust` → `POST /live/{id}/ironfly-adjust`, persisted in
  export_state, so it survives a restart). When enabled and phase=="ironfly", `_adjust_ironfly`
  replaces the old terminal ride: on a breakeven breach (K ± net_credit) it sells a naked
  ~15-20Δ short on the UNTESTED side (`adjust_target_delta`, reuses `_pick_delta_strike`), rolls
  it when it decays (≤`adjust_close_delta`=10Δ OR ≤`adjust_close_prem_frac`=¼ of its sold
  premium; banks the credit in `adjust_realized`), and calls `_exit_all("ironfly_payoff_neg")`
  when `_payoff_max(legs) < 0` (the whole expiry payoff is below zero — a backend piecewise-linear
  payoff over `bs.intrinsic`, the only backend payoff util; the frontend one is `web/src/lib/
  payoff.ts`). The naked adjustment adds an UNCAPPED tail → the optional `stop_loss_pct` is the
  hard MTM backstop.
- **iron_fly_monthly** (`strategies/iron_fly_monthly.py`, subclasses DeltaNeutralMonthly): enters
  the iron fly DIRECTLY (override `_try_enter` = SELL ATM straddle + BUY wings at ATM ± (CE+PE
  premium), grid-snapped), `ironfly_adjust` defaults **True**. **NIFTY / BANKNIFTY / SENSEX**
  (the base machinery is underlying-generic — `_STRIKE_STEP`/`lot_size_for` cover all three; SENSEX
  resolves off the broker BFO chain + BSE:SENSEX spot, no cache needed for a single-underlying
  broker-source run). Same monthly cadence + margin/target/exit machinery inherited. Deploy-only
  (`POST /trade/options/iron-fly/deploy`, broker source required, `_DEPLOY_ONLY`); no backtest.
  **SENSEX (and BANKNIFTY beyond its ~2-month cache) needs `force_entry`/the Live force button** —
  `_is_entry_day`'s 45-day expiry lookback is cache-only + `option_expiries` returns only FUTURE
  expiries, so the "2 days after expiry" auto-trigger can't fire without cached expiry history.
- **momentum_theta_gainer_intra** (intraday 15-min SuperTrend(7,3) + daily-pivot ATM weekly
  seller, NIFTY + SENSEX): builds its OWN 15-min candles from live spot ticks (none exist in
  any cache) and carries them in `export_state`; pivots (R1/S1) come from a daily-OHLC provider —
  **broker-first in LIVE** (`ZerodhaAdapter.daily_bars`, Kite `interval="day"`, always fresh so a
  stale/unrefreshed cache can't corrupt a live entry), cache fallback, then own prior-day bars —
  with a **stale-pivot guard**: if the provider's prior day isn't the actual adjacent trading day
  (`live/holidays.previous_trading_day`) it uses current own-bars else GATES entries + alerts once
  (never trades off a stale pivot — this is exactly the bug that mis-timed a live entry 07-08). The
  guard engages ONLY on the live-only `date` field, so the backtest provider (dateless, cache-fed
  `services/momentum_theta_bt._official_daily_ohlc_fn`) stays byte-identical. Entries only on CLOSED
  candles; flip exit never re-enters on the same candle; 3-entries/day cap. Warmup:
  `ZerodhaAdapter.intraday_bars` seeds ~7 days of real bars at deploy/recovery (cache source
  cold-starts: ST after ~2×period candles, entries day 2).
  **SENSEX is live-only** — zero BSE history exists (spot or options), so no backtest, ever;
  its options ride **BFO** (adapter merges the BFO dump into the NFO LUT, `_ts_exchange` keys
  the `NFO:`/`BFO:` prefixes, spot = `BSE:SENSEX`) and the deploy route rejects SENSEX+cache.
  Its backtest is a dedicated BS-priced 15-min service — NOT the shared engine (whose slice
  is one trading day: settlement timing/report cadence break intraday): `services/
  momentum_theta_bt.run_backtest` replays real 15-min NIFTY bars AS o→h→l→c ticks through
  the ACTUAL strategy class (signal parity by construction); bars live in a csv.gz store
  (`data/intraday_bars.py`, `~/.skas_data/intraday/`, Kite-fetched ≤190-day chunks, no
  parquet engine in the venv). Premiums = BS both ways (prior-day HV20 × vol_multiplier,
  same /research calibration). Endpoint: POST /research/momentum-theta-bt + panel on
  /research. First finding (2025-26, defaults): EOD exits profit, ST-flip buybacks lose
  ~2× that — the spec as given is net-negative on NIFTY; tune via the panel before deploying.
- **Donchian flip default:** new deploys roll a breached name **intraday** (`breach_basis="touch"`),
  **once per name per day** (`last_flip_day` guard), up to `max_flips=3` (two rolls, then close the
  name on the next breach). Defaults live in the deploy layer (`api/models.py:DonchianDeploy`); the
  strategy **constructor** stays `close`/2 as the conservative backstop for a param-less recovery
  (CLAUDE.md §1). To change a **running** deploy's config, edit its `params_snapshot` in the DB and
  restart — `live/recovery.py` rebuilds the strategy from it (persisted `flip_count`/`last_flip_day`
  are preserved via `state`). There's no in-place param-edit endpoint.

## 9. Frontend (`web/`) gotchas
- **Router state vs legacy redirects:** several old paths are `<Navigate to=... replace />` redirects in
  `App.tsx` (e.g. `/new` → `/backtest?tab=new`). `<Navigate>` **drops `location.state`**, so navigating
  to a redirect path with state (e.g. `clonePrefill`) silently loses it — land on the real route
  directly. The Backtest "tabs" (Runs / New backtest) are the same page selected by `?tab=`.
- Pages prefill forms from `location.state` via one-shot effects (`useRef` guards) and let the
  template/clone values land **after** the per-strategy default-reset effects — order matters; read the
  comments before reordering effects.
- **"Looks stuck" is usually a dead backend or no broker session, not a hang.** The live-options
  screeners (Donchian / FibRet) gate Refresh + the portfolio panel on `effectiveAccount` (a logged-in
  Zerodha session). If the backend is down *or* there's no session, the dropdown reads "No logged-in
  session", Refresh is silently disabled, and the panel shows "Computing portfolio…" forever — these are
  *not-ready* states, not loading. Check `GET /api/v1/brokers` (`has_session`) before assuming a code bug.
  Tables persist via `localStorage`, so data can show even with the backend down. Use the shared
  `SessionBanner` (`components/redesign.tsx`) on any new broker-session-gated screener/deploy page —
  it distinguishes backend-down from no-session — and give disabled action buttons a `title` reason.
- **Option tickers are `UNDERLYING|YYYY-MM-DD|STRIKE|RIGHT`** — never render the raw form: the `|`
  reads as an `I` (`NIFTYI2026-07-07I24500ICE`). Display option symbols through
  `formatOptionSymbol()` (`lib/symbol.ts`) → `NIFTY 24500 CE · 7 Jul '26`; it passes equity tickers
  (no pipes) through unchanged, so it's safe to wrap any `.symbol` / `.ticker` you print.

## 10. Running locally
**One-command stack:** `./scripts/start.sh` (backend via launchd + Vite web) · `./scripts/stop.sh`
(unloads the agent so the backend stays down — a bare `pkill` would just get respawned) ·
`./scripts/status.sh`. There is only ONE backend process — the live loop, order/broker
reconciliation, watchdog, backups, and WebSocket all run inside it; no separate monitor server.
The manual commands underneath:
```bash
# Backend (FastAPI + uvicorn on :8080) — START FROM THE REPO ROOT (see footgun below)
venv/bin/skas-algo
# Web (Vite on :5173, proxies /api → :8080 incl. WebSocket)
cd web && npm run dev
```
Health check: `curl http://localhost:8080/api/v1/health`. The DB schema is created on startup
(idempotent); Alembic migrations are in `alembic/` for evolving an existing DB. Startup also
takes a pre-recovery DB backup (`services/backup.py` → `backups/`, retain 7) and starts the
manager maintenance task (5-min: loop watchdog + a once-per-trading-day background cache refresh
+ daily ~16:30 backup).

**Preflight before any restart/deploy:** `./scripts/preflight.sh` — ruff (advisory) + the FULL
test suite incl. the parity/mode-equivalence suites + web typecheck. Green = the change didn't
alter the engine or a live path. It auto-deselects the two DuckDB-cache parity suites while a
backend is live and tells you to re-run them against a stopped backend. This is THE gate that
lets you develop continuously without silently breaking a live strategy (see ARCHITECTURE §7).

**Binds `127.0.0.1` by default** (`config/settings.py::api_host`) — a single-user real-money API
must not be on the LAN. If the UI can't reach the backend on a container/remote, set
`SKAS_API_HOST` (the docker path already sets `0.0.0.0`). **Auth** is opt-in and fail-open: set
BOTH `SKAS_AUTH_PASSWORD_HASH` (via `skas-algo hash-password`) + `SKAS_AUTH_JWT_SECRET` to enforce
a login (required on any networked host; off on localhost). Live marks come from a shared
per-account KiteTicker WS feed (`live/pricefeed.py`, `SKAS_WS_FEED_ENABLED`, default on) with a
REST fallback — strategies still read via `QuoteSource` (no tick callbacks, parity intact).
**Live daily/historical data is broker-first too (2026-07)** — the founding invariant is that a
LIVE run should not depend on the (manually-refreshed) skas-data cache: the daily-OHLC hooks
(`manager._prior_day_ohlc` for momentum_theta pivots, `manager._daily_bars_live` for ema21 bands)
prefer `ZerodhaAdapter.daily_bars` (fresh Kite `interval="day"`, `manager._broker_daily_df`), with
the cache as fallback (Dhan has NO broker history → cache; SENSEX has no cache → own bars). The
skas-data cache is now **backtest-focused + a live fallback**, and is kept fresh by a maintenance
task `manager._maybe_daily_cache_refresh` (once/trading-day, background, fires as soon as a valid
Zerodha session exists; equity SuperTrend/Donchian stay cache-backed — a 50-symbol batch beats ~50
broker calls/decision — so THEY rely on this refresh). It is historical/read-only
(`make_data_session`, never `make_adapter`/arm/orders), and on success broadcasts a `cache_refreshed`
WS event + surfaces `manager.last_cache_refresh` on `GET /live/summary` → the web header's quiet
"Data ✓ HH:MM" chip. NSE holidays close the market like weekends (`live/holidays.py`; festival dates
PROVISIONAL, env-correctable via `NSE_HOLIDAYS_ADD`/`NSE_HOLIDAYS_REMOVE`;
`previous_trading_day(d)` is the adjacency helper the pivot stale-guard uses).

**Supervision (optional but recommended for real-money):** `./scripts/install-supervisor.sh`
puts the backend under a launchd LaunchAgent (auto-start + auto-restart on any exit);
`uninstall-supervisor.sh` reverts. NOTE: while supervised, launchd re-spawns the backend
within ~15s of any kill — so a manual restart during dev means `launchctl kickstart -k
gui/$(id -u)/com.skas.algo`, not a bare `pkill` (which just triggers a respawn). A recovered
LIVE-order run keeps PaperBroker by default (recovery re-injects LiveBroker ONLY when
`live_resume_orders_on_recovery` is on — still behind the 4-key gate); a LiveBroker run —
fresh deploy or resumed — reconciles its broker book before its first decision
(`reconcile_pending`, ARCHITECTURE §3).

**Footguns when launching:**
- **Relative SQLite path.** `SKAS_DATABASE_URL=sqlite:///./skas_algo.db` is relative to the CWD —
  start the backend from the **repo root** or it opens/creates a *different, empty* DB (no accounts /
  sessions / runs). The real DB is `./skas_algo.db` at the root.
- **Broker sessions persist in the DB**, not in memory — `make_adapter` resumes the encrypted
  `session_token` on restart, and Kite tokens are valid until ~06:00 IST next day. A restart does **not**
  lose a still-valid session. If the UI shows "no session" right after a restart, suspect a dead/wrong-CWD
  backend before assuming the token was dropped.
- **Detached dev servers (agents):** the harness reaps `run_in_background` tasks and kills child
  processes at turn end. To keep servers up across turns, launch with `nohup … & disown`
  (macOS has **no** `setsid`). The durable option is the user running them in their own terminal.
- **Orphaned uvicorn reload workers (2026-07-07 incident).** Killing the backend can leave the
  `--reload` CHILD alive (a `multiprocessing spawn_main` process, reparented to launchd — its
  cmdline does NOT contain "skas-algo"), silently holding :8080 AND the SQLite write lock →
  "database is locked" spam, hung API, port races on restart. Before any relaunch:
  `pkill -9 -f "venv/bin/skas-algo"` (exact pattern — a bare "skas-algo" also matches vite/esbuild
  via the repo path!), then check `lsof -nP -iTCP:8080 -sTCP:LISTEN` and kill any survivor by PID.
  Launch exactly ONE backend, always from the repo root.
- **Parity tests vs a running backend (DuckDB lock).** `test_sst_parity` / `test_sst_fifo_parity`
  read the REAL skas-data cache, and skas-data opens DuckDB read-write — **one process only**. If
  the running backend has touched the cache since its last `--reload` restart, those tests fail
  with `IOException: Could not set lock … held in <backend pid>`. That's environmental, not a code
  failure: trigger a backend reload (touch any .py) or stop it, then rerun. Everything else in the
  suite uses fakes and is immune.
</content>
