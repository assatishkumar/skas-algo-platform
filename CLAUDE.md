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
  is the ONLY code that places real orders (LIMIT-at-touch → 10s → PROTECTED-LIMIT escalation
  — re-priced 3% through the fresh touch, `SKAS_LIVE_ORDER_PROTECT_PCT`; NEVER a naked MARKET
  modify: Zerodha's API rejects those on options ("market orders without market protection"),
  which cancelled a straddle square-off leg and halted run 10 on 2026-07-27 — via
  `ZerodhaAdapter.place_order/modify/status/cancel`, all behind `_ensure_armed`). It is injected by
  `live/manager._maybe_inject_live_broker` ONLY when ALL of: mode=="LIVE" ∧ account.armed ∧
  `SKAS_LIVE_TRADING_ENABLED` ∧ adapter has the full order surface — every other cell keeps
  PaperBroker (matrix test in tests/test_live_broker.py). Never bypass or widen this gate.
- **OWNER DIRECTIVE: Claude never initiates live orders.** Never arm an account, never set the flag,
  never deploy/activate a LIVE run with an armed account, never "verify" with a real order — the
  pilot and every real order is the owner's hand only. Order-path verification = fake-adapter tests.
- **`touch_fn` is what makes an order a LIMIT — and it was blind to equities until 2026-08-24.**
  `manager`'s `touch()` reads `market._bid_ask`, which exists ONLY on `LiveOptionsMarketView`
  and is CHAIN-based, so an equity run (or an equity leg on an options view) got `None`. Two
  silent consequences: `execute()` sent a naked **MARKET** order, and it gates the ENTIRE
  escalation ladder on `req.order_type is OrderType.LIMIT` — so an unfilled equity order got
  **no escalation at all**, was cancelled, and halted the run; and `_check_rails` applies the
  per-order notional cap only `if ref_price`, so the order ALSO skipped
  `SKAS_LIVE_MAX_ORDER_NOTIONAL`. `touch()` now falls back to the last live print
  (`last_close`/`close`) when there is no two-sided book; a real bid/ask still wins. This is
  what killed the Dhan ITC smoke test (run 20): NSE converted our MARKET to a protected LIMIT
  at 267.30, it did not fill in 10s, **we** cancelled it — Dhan support had to correct our
  reading. Coverage: `test_touch_falls_back_*` in tests/test_live_broker.py.
- **The order path emits a structured trace — read it before reading the code.** Every
  lifecycle event logs one greppable INFO line `ORDER <event> cid=<id> k=v …`
  (place/accepted/rejected · status on CHANGE only · timeout · escalate · noescal · modifyerr ·
  cancel · filled/partial/failed), all sharing one correlation id, with `elapsed` on the
  terminal line. Before this the whole path had FOUR log statements, three inside the
  escalation branch — so an order that skipped the ladder left a 10-second hole, and the
  post-mortem was done by inferring which branch ran (and got it wrong). **Absence of a log
  line is not evidence.** Grep one order with its `cid=`, or a day's real orders with
  `"ORDER "`. Pinned end-to-end by
  `test_the_order_trace_reconstructs_a_cancel_without_reading_the_code`.
- **The escalation crossing is SEGMENT-AWARE (owner call, 2026-08-24): options 3%, equity 1%.**
  One constant cannot serve both — option spreads are percents, equity spreads are basis
  points (3% of a ₹14 premium is 43 paise; 3% of a ₹2,300 stock is ₹69). `protect_pct` (3.0,
  `SKAS_LIVE_ORDER_PROTECT_PCT`) drives the OPTIONS ladder, `protect_pct_equity` (1.0,
  `SKAS_LIVE_ORDER_PROTECT_PCT_EQUITY`) the EQUITY one; same 1x/2.7x/6.7x rung shape, chosen
  per ORDER via `is_option_symbol` (the `_check_rails` pattern). An explicit `protect_ladder`
  still overrides BOTH. Unchanged: an ENTRY takes one rung (`[:1]`) and an EXIT walks all
  three. The crossing is a CEILING, not a price — a marketable limit fills at the ask, so 1%
  binds only when the book ran away since the decision, which is an entry the owner would
  rather MISS than chase. **An unfilled entry still HALTS the run** (owner call: intervention
  is wanted; do not quietly retry). Coverage: `test_an_equity_entry_crosses_one_percent_*`,
  `test_an_option_entry_still_crosses_three_percent` in tests/test_live_broker.py.
- **A refused entry must say why (`SkipReasonMixin`, 2026-09-04).** Every option
  strategy's entry path is a chain of silent `return []`s — entry day, entry window, a
  chain that did not price, a credit outside its window, a premium hunt that missed. All
  correct, all invisible: two paper ratio runs sat flat 08-20 → 09-04 with nothing in the
  log or on the tile, and the owner read it as "didn't take off" (they were waiting for the
  last-Tuesday entry day, and had been refused by a gate in their August window). Use
  `self._skip("reason", today)` in place of a bare `return []` on ANY entry refusal and
  `self._entered()` when legs land; the snapshot carries `entry_skip` and the Live tile
  renders "Cycle skipped · reason · day" while the run is flat. Wired into the ratio base
  (batman/call/put/hni), the delta base, fair_value_calendar and monthly_butterfly; a
  new option strategy inherits the mixin and follows the same rule. In-memory only.
  Related: a FRESH monthly_butterfly run waits for the current expiry to pass (it marks
  it spent on first sight) — un-forced it used to open a three-week fly at 09:30 the next
  morning; force (deploy toggle / tile button) is the deliberate way in, and **force
  respects the entry WINDOW** (it skips the expiry wait, never the time of day).
- **A PAPER options run measures spread cost — the strategy's P&L does not see it (2026-09-04).**
  `PaperBroker` in a live session fills SELL@bid / BUY@ask off the live chain, while a
  strategy's `legs[*]["entry"]` and its target/stop marks are chain LTPs. Paper run 30
  (monthly_butterfly, 10 sets, forced at 09:15:03 — the first tick, when BANKNIFTY monthly
  spreads are ₹20-60) booked the legs at those spreads, saw LTP-basis P&L cross +3%
  (₹21,000) two minutes later, exited on "target", and the book realised **−₹9,765**: a
  ~₹30,000 round trip of spread, ₹25 a unit. Two lessons. A paper cycle IS a spread
  measurement (queue/impact excepted) — read book P&L against `strategy_pnl`. And a %
  target on LTPs is only honest when the round-trip spread is small next to it: enter
  inside the window (09:30, never the open), and treat `strategy_pnl − book` as the
  live number that decides whether the target is real.
  **The spread is the one execution cost a strategy CAN see before it trades** —
  `EntrySpreadGateMixin` (`_options_common`, generic): `max_spread_pct` refuses a FRESH
  entry when any leg's (ask−bid)/mid exceeds it, naming the leg and the numbers via
  `_skip`. Live chain cells carry `bid`/`ask` (Zerodha depth[0], Dhan top_bid/ask); the
  backtest chain has only close/oi, so the gate is FAIL-OPEN there — a live-only refusal
  rail, never a different trade. Ctor default 0 = off (§1); butterfly deploy default 1.5
  (09:30 BANKNIFTY monthly ATM ≈0.3%, the 09:15 print was 3-7%). Wired into
  monthly_butterfly + fair_value_calendar (`_spread_refusal({label: cell})` after the OI
  check); the ratio family reads a different chain shape and has not adopted it.
- **NSE's per-ORDER quantity freeze is an exchange control, and LiveBroker splits for it
  (2026-09-04).** BANKNIFTY 600 units / NIFTY 1,800 from the 2026-09-01 circular; it is
  re-derived several times a year and the Kite instruments dump does NOT carry it, so it
  lives in `settings.live_freeze_qty` (`SKAS_FREEZE_QTY="NIFTY:1800,BANKNIFTY:600"`, like
  `NSE_HOLIDAYS_ADD`). `LiveBroker.execute` places an over-cap option order as consecutive
  children ≤ cap (each through the full rails/touch/ladder/trace path, `ORDER split`
  line) and returns ONE combined fill; a child that fails after earlier children filled is
  a PARTIAL (real fills are never raised away), only a first child failing raises. Equity
  never splits; a broker built without the table never splits (every older path).
  Note `kiteconnect` 5.2.x has no `auto_slice`; the split is ours. **A strategy may declare
  its own crossing**: `order_protect_pct` on the strategy is read ONCE at injection
  (`_maybe_inject_live_broker`) and overrides `SKAS_LIVE_ORDER_PROTECT_PCT` for that run —
  the platform's 3% is a square-off setting (₹9 on a ₹300 body); an entry that can wait a
  day must not chase it. Edit-blocklisted (the ladder is derived in the broker's ctor).
  **BANKNIFTY lot is 30, not 35**: the contract table's 2026-01-01 → 35 row was unsourced;
  the live dump reads 30 for every listed series (verified 2026-09-04). Backtests dated
  2026 that ran before the fix sized BANKNIFTY 17% too large.
- Safety rails live in LiveBroker pre-flight: per-order notional cap, per-run daily order cap,
  market-hours check, account-level rate governor (settings SKAS_LIVE_MAX_ORDER_NOTIONAL /
  _MAX_ORDERS_PER_DAY / _ORDER_TIMEOUT_S). An `OrderExecutionError` (reject/unfillable) or hourly
  book-mismatch reconciliation sets `LiveRun.order_error` → decisions HALT until the owner
  acknowledges (POST /live/{id}/ack-order-error; banner + tile chip). Reconciliation compares the
  broker's NET book against the AGGREGATE of all live-order runs on the account (broker nets per
  contract across runs; manual trades in the same account will trip it — dedicate an account).
  Reconciliation runs **market-hours only** (`is_market_open` gate in `_maybe_reconcile`), and a
  failed broker-book *read* (expired overnight token / API blip) raises `ReconcileUnavailable` →
  treated as **transient** (stay reconcile-pending, retry next tick), NOT a halt — only a genuine
  quantity mismatch halts. (Pre-2026-07 a token-expiry read failure off-hours fired a false
  ~4:50 AM "BOOK MISMATCH" halt.)
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
- New strategies onboard via `strategies/registry.py`, **not** engine edits — and they ship
  with docs: a `docs/FEATURES.md` §3 entry AND a `/docs` card in
  `web/src/pages/StrategiesPage.tsx` (STRATEGIES + META). Both are pinned by
  `tests/test_strategy_docs_coverage.py`, which also checks §3's ID/file counts — eight
  strategies had drifted out of the docs before it existed (2026-08-20).
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
  channel breakout/re-entry/whipsaw stats + live-rule flip simulation), the BS-vs-live
  calibration (session-gated, strictly read-only), and the **batman loss-reduction study**
  (`services/loss_study.py`, `POST /research/loss-study`). The last replays batman ONCE over
  the 1-min store, reconstructs each cycle's per-minute MTM from the leg bars, then evaluates
  loss-cutting rules (trailing / VIX / trend / entry-filter) POST-HOC as early-exit overlays —
  no re-replay per rule (exit-only + entry-skip are exact over the marked path; mid-cycle
  rolling is out of scope). Ranked by resulting net with an **in-sample/OOS split** (a single
  window overfits: trailing looked +20k on #224's 19 cycles but −79k OOS over 42; only VIX
  exits + an entry vol-premium filter survived). Single-flight background job (~100s replay
  then instant eval). Backend: `api/routes/research.py`, `services/donchian_study.py`,
  `services/bs_calibration.py`, `services/loss_study.py`.
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
- **Vol-premium entry filter** (`EntryVolFilterMixin`, `_options_common.py` — GENERIC, any
  option SELLER can inherit it): skip a new entry when the vol risk premium (ATM-IV − HV`hv_window`,
  vol points) < `vol_premium_min`. The /research loss-study's one OOS-robust finding (batman
  ≈2). Wired into the ratio base (batman/call/put ratio + hni): a once-per-entry gate in
  `_maybe_enter` (market-wide, not per-wing; applies to forced entries like `min_vix`). Implied
  leg = the strategy's own `_atm_iv` off the chain it's about to trade (same source BT+live →
  no parity gap); realized leg = the underlying's annualized HV via `set_realized_vol_fn`
  (`data.options_provider.make_realized_vol_fn`, reuses `engine.options.realized_vol`), wired
  cache-fed in backtest/replay and **broker-first in live** (the live daily-data invariant),
  probed by `getattr` like `set_daily_bars_fn`. Ctor default `vol_premium_min=0` = OFF (§1:
  recovered deploys byte-identical); v2 FORM default also 0 (opt-in). FAIL-OPEN on missing data.
  To add to another seller: inherit the mixin, add the two ctor params (default off), call
  `_vol_premium_ok(u, today, atm_iv_pct)` at entry. Coverage: `tests/test_entry_vol_filter.py`.
- **Two brokers.** `BrokerAccount.broker` ∈ {zerodha, dhan}; `services/broker.make_adapter`
  dispatches. **Dhan** (`brokers/dhan.py`): no api key/secret — client id + a portal-generated
  JWT the user PASTES (its `exp` claim is the session expiry); instruments resolve via the
  public scrip-master CSV (module-cached daily; underlying recovered with `rsplit("-", 3)` —
  hyphenated names like BAJAJ-AUTO); `basket_margin` = Σ per-SHORT-leg margins (Dhan has no
  basket API → overstates, conservative); its option-chain endpoint is throttled (~1/3s) so
  the 50-name screeners STAY on Zerodha, as does the skas-data cache refresh (Kite-coupled —
  `make_data_session` rejects dhan accounts). **Dhan live quotes/chains need the paid "Data
  APIs" subscription** (error 806 without it; expirylist/funds/orders don't) — the owner
  SUBSCRIBED on 2026-08-21, so quotes and chains are available on the Dhan account now; the
  ~1/3s chain throttle above is a separate, still-standing limit (it is what keeps the
  50-name screeners on Zerodha, not the subscription). quote_source ∈ {cache, zerodha, dhan} — the
  broker sources are gated by `live/quotes.is_broker_source`, and the source must match
  `account.broker`. **Dhan PHASE B — real orders (2026-08-21):** `DhanAdapter` now has
  place/modify/status/cancel + `positions()`, behind the SAME `_ensure_armed` double gate,
  so `adapter_can_execute` passes and LiveBroker can be injected on a Dhan account. Raw REST
  (`api.dhan.co/v2`), NOT the `dhanhq` SDK — the file was already raw REST for quotes/chain/
  master and the order path is four endpoints; no new dependency. Dhan specifics that are
  load-bearing: **order APIs only accept calls from a STATIC IP whitelisted on the DhanHQ
  portal** (the VPS, 13.205.157.28, is — quotes work without it, so an unwhitelisted box
  looks healthy until the first order is rejected); **statuses are normalised in the adapter**
  (TRADED→COMPLETE, EXPIRED→CANCELLED, PART_TRADED left NON-terminal so the escalation still
  cancels-and-books the remainder) because LiveBroker's `_TERMINAL` is Kite vocabulary;
  **modify RESTATES the order** (Dhan's PUT rejects a partial body, so the live order is read
  first and the changed fields overlaid); options route NSE_FNO+MARGIN, equity NSE_EQ+CNC, and
  an unresolvable symbol RAISES rather than falling through to a wrong `securityId`.
  **`_error_message`: THE STATUS DECIDES, NOT THE CODE** (2026-08-27). Dhan sends
  `omsErrorCode "0"` for BOTH a healthy fill ("TRADE CONFIRMED") AND a genuine rejection whose
  description IS the reason — so a gate keyed on the code either leaks "CONFIRMED" into a halt
  banner (08-24) or swallows "RMS:…insufficient funds. Please add Rs.83.48 to trade" (08-27,
  the owner had to ask Dhan support for a reason our own API response already held). A
  REJECTED order's description is ALWAYS surfaced; benign confirmation text is suppressed by
  VALUE (`_BENIGN_OMS`), never by code alone.
  `positions()` reshapes tradingSymbol/netQty into the reconciler's tradingsymbol/quantity and
  `_option_tradingsymbol` comes from the scrip master (never reconstructed).
  `BrokerAccountOut.can_place_orders` reports `adapter_can_execute` so the Brokers page's
  smoke-test picker cannot promise what the engine would refuse; `POST /trade/smoke-test/deploy`
  still 422s a LIVE deploy on an order-less broker (no broker fails it today — it stands for the
  next one). That guard keys off the ORDER SURFACE, never off `armed`: a LIVE run on a DISARMED
  account is a documented, useful negative test. Coverage: `tests/test_dhan_orders.py` (fake
  http — §1: never a real order).
  **Dhan rate limits: DEDUPLICATE demand, don't shape traffic (2026-09-02).** Three rounds of
  429 symptoms taught the shape of the problem: N runs on one account are N adapters
  (`make_adapter` never memoizes; adapters churn on every quote-source rebuild), each polling
  on its own clock — a gate at the HTTP layer only chooses who waits, and every new consumer
  adds demand against a Quote-API budget that is a flat ~1/s per ACCOUNT. So quotes are a
  shared per-account cache at MODULE level (`dhan._QUOTES`, the scrip-master precedent):
  single-flight — ONE fetcher per account refreshes the UNION of every recently-wanted symbol
  via `/marketfeed/quote` (one endpoint answers `get_quote` AND `day_quotes`), everyone else
  reads; TTL 5s (`SKAS_DHAN_QUOTE_TTL`, 0 = off). A tenth run costs ~nothing, and the order
  path never touches the cache (LiveBroker re-reads its touch each rung). `_rate_gate` stays
  as the safety net BEHIND it (quote floor 1.2s — 1.0s sits ON the 1/s boundary and jitter
  makes same-second pairs — plus the 1.1s account-wide floor across kinds), `_DhanHttp._call`
  retries a gateway 5xx once but NEVER a 429, and reconcile failures back off 60s→15m
  (`manager._maybe_reconcile`) so one bad read cannot feed itself into a storm. The data
  family (holdings/positions/funds) is deliberately UNCACHED — ~3/min vs a 5/s budget, and
  reconciliation must read fresh or a stale positions row becomes a false BOOK-MISMATCH halt.
  **F&O order parity with Kite is structural** — the ladder lives in LiveBroker ABOVE the
  adapters with zero broker branching, and Dhan's chain carries top_bid/top_ask so a DERIV
  run gets a genuine limit-at-touch. Two restatement rules in `modify_order` ARE the parity
  fixes (2026-09-02 audit): a MARKET restatement carries price 0 (the copy-forward used to
  keep the stale LIMIT price on the book-vanished path), and quantity is rebuilt from
  remainingQuantity+filledQty when the gateway omits it — 0 is NEVER sent, raise instead
  (LiveBroker logs a modifyerr and keeps polling). End-to-end pin:
  `test_the_live_broker_ladder_runs_end_to_end_on_the_dhan_adapter`. Known limitations, by
  design: SENSEX/BANKEX F&O unreachable (the master filters to NSE — those strategies run on
  Zerodha); no broker history (`daily_bars`) so weekly_intraday_straddle fails closed on a
  Dhan account; `basket_margin` overstates (no hedge benefit). A Dhan DERIV order has never
  been exercised for real (only the ITC stock leg, runs 20/21) — the DERIV smoke test is
  runnable on Dhan today and is the owner's hand, never Claude's (§1).
- **gap_reversal** (`strategies/gap_reversal.py`, Nifty 500 equity, 2026-07-28): daily LONG-only
  mean-reversion — gap-up open (≥`min_gap_pct`) + close above EMA(21) + RSI(10) **of the EMA
  series** < 10 (`rsi_source="ema"` default — the owner's TradingView "RSI 10 EMA:EMA": the
  smoothed RSI SATURATES near 0/100 through trends, so the band fires on the first gap-up
  recovery; `"close"` = classic Wilder, rare at <10); exit on close < EMA. The spec's short leg is DEFERRED (cash equity can't hold overnight
  shorts) to a future sell-CE/sell-PE options variant. It rides the NEW **generic indicator
  precompute** (`needs_indicators`/`indicator_config()` → `MarketView(indicators=...)` computes
  per-date ema/rsi/gap_pct from the RAW OHLC frame — the ONLY reader of the day's OPEN, which
  the equity ctx otherwise never sees; mirror of the SuperTrend precompute, default None =
  byte-identical). LIVE: `LiveMarketView.set_indicators` is EMPTY until the Phase-2 manager
  seeding lands → deploys FAIL CLOSED (no entries, no blind exits). Backtest-first.
  Coverage: `tests/test_gap_reversal.py`.
- **happy_twins** (`strategies/happy_twins.py`, equity, 2026-08-18): weekly dual-SuperTrend —
  ENTER on the FAST ST (2,1) TURNING green (a TRANSITION, so a recovered/fresh run never chases
  an old signal), EXIT while the SLOW ST (3,1) is red (a STATE, so a flip missed across a restart
  still closes on the next bar). Rides the same generic indicator precompute as gap_reversal
  (`needs_indicators`, weekly resample) → **LIVE fails closed** until the Phase-2 manager seeding
  lands. Three overlays, all default-off: `park_symbol` (park idle capital in e.g. GOLDBEES while
  EVERY trading name is flat; the sale funds the entry in the SAME slice — the park symbol must be
  in the run's symbol list and is excluded from the twins logic), `capital_parts` (portfolio mode:
  one part per flip, strongest-first by close-vs-20DMA, at most N names), `regime_symbol` (the
  exposure BRAKE — no NEW part while that index's slow ST is red; open names still exit normally).
  The route appends `regime_symbol` to the run's symbols for data. **Point-in-time membership**
  (`membership={effective_iso: [syms]}`, `_membership.MembershipGate`) gates ENTRIES ONLY, never
  exits — shared with supertrend_momentum/nifty_shop and fed by the backtest route's
  `pit_universe` flag on the `nifty500mom50` universe (trades the UNION of ever-members;
  `data/mom50_membership.json` labels each rebalance's source — official capture vs replication).
- **value_investing** (`strategies/value_investing.py`, equity, 2026-08-20): the platform's
  only BUY-AND-NEVER-SELL strategy — a fixed `daily_budget` spread across the watchlist (sorted by
  today's change %, biggest faller first), funded by selling a `fund_source` ETF in the same
  decision. Owner rules: an unaffordable name is SKIPPED (the walk
  continues), NO wrap-around, leftover evaporates, funding is ALL-OR-NOTHING (dry ⇒ buy nothing +
  alert), and an all-green day still buys. Idle cash is spent BEFORE the ETF.
  **Three load-bearing facts.** (1) `SignalAction.EXIT` with a quantity but NO `lot_id` is a
  SILENT no-op and the quantity is clamped to ONE lot (`overrides.py`) — there is no cross-lot
  partial sell, so funding emits one `EXIT(lot_id, qty)` per lot, FIFO. Pinned by
  `test_every_fund_exit_carries_a_lot_id`. (2) Ranking needs yesterday's close, and the
  `indicators=` precompute is DEAD IN LIVE — so this reads the NEW **`ctx.prev_close`**
  (`MarketView._prev_close`, a `close.shift(1)` precompute, write-only for every existing path;
  `LiveMarketView._hist[-1]`, history ONLY — `last_close()` prefers the live quote and would make
  every change read 0.00%). That is why this deploys live while gap_reversal/happy_twins fail
  closed. (3) On `quote_source="cache"` the live price IS yesterday's close, so every change reads
  0.00% — `_rank` detects an all-flat book (>1 name) and refuses the day. **Broker source
  required live.** The `watchlist` is a comma-separated STRING, not a list: the Live Edit-params
  modal only renders scalars, and hot-editing it is the point (the run's `symbols` is
  edit-blocklisted). `universes.with_helper_symbols` unions `fund_source` + `watchlist` into the
  run's symbols at both launch paths; a name added LATER by a params edit can't be priced and the
  strategy names it in `strategy_alert`. `fund_seed` ctor default `"never"` (§1 — a LIVE deploy
  must never place a surprise ETF buy); the backtest FORM sends `"if_empty"`. The exit-override
  builder is HIDDEN for this id — an override intercepts the strategy's partial EXIT and would
  halve the funding sale, after which the buys overdraw silently. Backtest caveat: the only SELLs
  are ETF funding sales, so read the equity curve, not the trade table (form defaults `tax_rate`
  0 and `lookback` 1) — or better, the **accumulation panel** (`services/holdings.py`,
  `report_holdings = True` → "Portfolio — what you own": per-name units/cost/value/weight, sleeve
  XIRR, and the SAME-rupees-same-days index SIP as the yardstick; marks come from the run's END
  DATE, never each name's last fill — that bug understated #278 by ₹3.4L and read TCS at 29.0%
  weight instead of 18.9%).
  **T+1 SETTLEMENT (`settlement_days`, owner 2026-08-27) — the funding model was a backtest
  fiction.** The strategy used to sell the ETF and spend the proceeds in the SAME decision.
  Correct in a backtest (the engine credits a sale synchronously), impossible live: an equity
  CNC sale settles T+1 (Dhan confirmed in writing). Live run 23 showed both halves of the
  failure — capital was ₹1,00,00,000 so "idle cash is spent before the ETF" never fired the
  sale at all, while the real Dhan balance was ₹146.03; MANAPPURAM filled and KTKBANK was
  rejected for want of ₹83.48, halting the run. Now: **buy from SETTLED cash, then pre-sell
  for tomorrow.** The strategy owns a settlement ledger (`settled_cash` + `pending_credits`,
  both persisted) and ages credits on `live/holidays.next_trading_day` (NEW — the mirror of
  `previous_trading_day`), so a Friday sale funds Monday and a holiday shifts again.
  **The float target covers the POTS' claims** (`max(daily_budget, sum(pot)) × (1+buffer)`),
  not just one day's budget — a ₹2,900 name saving six days needs ₹2,900 on the day it fires,
  and capping at the daily budget would price it out forever (caught by an existing test).
  **SIGNAL ORDER FLIPPED — and the old comment was actively wrong to keep.** It used to read
  "the sale funds the same tick, so every EXIT must precede every buy". The sale no longer
  funds today at all, yet must STILL go first: a rejected BUY halts the run, and a sale queued
  behind the buys would then never fire, starving TOMORROW too.
  **LIVE reconciles against the broker**: `manager._maybe_refresh_funds` (mirrors
  `_maybe_refresh_margin` — STOCK + broker source, market hours, ~1/min, `getattr`-probed)
  pushes `adapter.funds().available`, and the ledger is capped at `min(ledger, broker)` —
  `Funds` already existed in `brokers/base.py`, `ZerodhaAdapter.funds()` was implemented and
  called by NOBODY, `DhanAdapter.funds()` is new (`/fundlimit`).
  Ctor default `settlement_days=0` = the historical same-day path, kept verbatim in
  `_legacy_fund` (§1); FORM/deploy default 1, buffer 10%.
  **Fixed in the same change:** `_equal_value_plan` DEBITED a name's pot while planning, but
  the caller could stop emitting when cash ran out — so money vanished from pots for buys that
  never happened. Same-day funding hid it (the sale always covered the plan); settled-cash
  gating would have hit it daily. The planner now takes the spendable `cap` so plan ==
  emission (`cap=None` preserves the historical unbounded walk).
  **LIVE OPERATING MODEL (owner 2026-08-27): capital is tiny, the ETF is refilled OUTSIDE
  the strategy, and the ETF is treated as an effectively infinite source.** Two consequences
  are load-bearing. (1) The platform only knows lots the RUN created and an EXIT without a
  `lot_id` is a silent no-op — so ETF units bought directly in the broker were UNSELLABLE,
  and a run deployed with capital ≈ one day's budget would report FUND DRY on day one while
  the account held lakhs. `manager._maybe_adopt_fund_holding` now adopts the shortfall via
  the new `LiveSession.adopt_broker_holding` (the mirror of `adopt_broker_close`: books an
  entry that already happened, no order, no charges, and NO cash movement — the units were
  paid for at the broker; `Portfolio.buy` always debits cash and is shared engine code the
  parity suites pin, so the balance is restored explicitly rather than adding a param there).
  It only ever ADDS: a broker showing FEWER units is a real divergence and reconciliation's
  job. Needs `adapter.holdings()` — NEW on both adapters, and distinct from `positions()`,
  which is the day's book and would not show an ETF bought weeks ago. (2) A dry fund source
  WARNS LOUDLY AND KEEPS RUNNING — banner + one push a day — and buying continues on whatever
  has settled. Never say "the drip stops"; it degrades.
  **`sizing` (2026-08-24) is the allocation decision, and the one to get right.** All three modes
  share the faller-first ranking and differ only in rupees per name. `one_share` (ctor default,
  §1) is the spec and silently weights by SHARE PRICE — one share of a ₹2,299 name is 51× the
  money of one ₹45 share: 2020-26 on the owner's list it spread ₹1,383…₹2,85,902 per name (207×)
  and ran ~7 XIRR points BEHIND a plain index SIP. `balanced` skips a name already `max_skew_pct`
  above the pack average (corrective; can't undo an existing gap; still whole shares).
  **`equal_value` (FORM default) separates allocation from timing**: each name is credited
  `daily_budget/N` into its own POT every day — regardless of rank, and even if it did not print —
  then buys `floor(pot/price)` from that pot alone, banking the remainder for tomorrow. Rank now
  decides only WHO SPENDS FIRST on a tight day. Divergence between names is bounded by one share's
  price → ₹75,380…₹77,584 (1.0×), XIRR 18.87%, +3.14 pts vs the index. No epoch logic needed (the
  split is over the CURRENT watchlist, so an added name takes 1/N from today with no catch-up, and
  a removed name stops being credited AND ranked); `_roll_epoch` is `balanced`-only. An unknown
  mode **RAISES** — it used to fall through to one_share in silence, so #279 asked for
  `equal_value` against a stale backend, traded price-weighted and looked healthy. Coverage:
  `tests/test_value_investing.py`, `tests/test_holdings.py`.
  **Decision time 15:05, declared by the STRATEGY** (`default_decision_time`, 2026-08-24).
  The platform default is 15:20, which since CAS is PAST the end of continuous cash trading
  for **F&O-listed** stocks (`live/quotes.FNO_CASH_CONTINUOUS_CLOSE` = 15:15; auction to
  ~15:35) — an order would rest unfilled and, because an unfilled entry HALTS, stop the run
  daily. Harmless until now because no equity strategy was live-capable; this one is. The
  deploy route resolves it (`_resolve_decision_time`: explicit → strategy → 15:20) so API
  callers get it too, `GET /strategies` publishes `decision_times` so the deploy form shows
  the same number instead of keeping its own copy, and an equity deploy landing in the
  auction raises a WARNING alert (never a 422 — a non-F&O-only watchlist is fine at 15:20).
  `decision_time` is DEPLOY-level and edit-blocklisted: stop + redeploy to change it, and
  `recovery.py`'s literal "15:20" fallback is untouched so existing deploys are unchanged (§1).
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
  no EOD backtest by design — flat-vol BS would misplace the smile-driven ⅓ strikes; the
  1-min store replay covers it on real premiums).
- **intraday_straddle** (`strategies/intraday_straddle.py`, NIFTY / BANKNIFTY): a DAILY
  intraday short straddle on the nearest weekly. Sell ATM CE+PE (or ~0.6Δ ITM via
  `strike_delta`, which relaxes the OTM filter) at `entry_time` (default 09:18, once/day
  `entered_day` latch), exit `exit_time` (checked FIRST — never waits on margin). **The
  intraday family's FORM/deploy exits are 15:20** (2026-08-12): Zerodha auto-squares intraday
  F&O at **15:26**, so a 15:25 exit leaves no room to retry — the 2026-08-11 failure was at
  15:25:34. Applies to `intraday_straddle.exit_time` and `weekly_intraday_straddle.eod_exit`
  (+ its `entry_cutoff` → 15:15). CONSTRUCTOR defaults stay 15:25 (§1 — a param-less recovery
  is byte-identical); RUNNING deploys keep their stored value until you move it with the tile's
  "Edit params". Allowlisted in `tests/test_backtest_v2_registry.py::_INTENTIONAL`.
  Two configurable stops off the FROZEN broker `margin_base` (pending → waits for the manager's
  `set_broker_margin` push, never the model): a fixed `-stop_loss_pct` (2%) AND a trailing stop
  that only ratchets UP (`_stop_level`) — `trail_mode="ratchet"` (each `trail_trigger_pct` of
  PEAK profit lifts the stop `trail_step_pct`) or `"below_peak"` (peak − `trail_step_pct`);
  trailing off when a trail pct is 0. No fixed profit target (the trail is the upside). Uncapped
  short-straddle tails → the stop is the only guard. Deploy-only + broker source required (live
  chain for ATM/delta); no EOD backtest (the daily slice can't model an intraday SL/trail) —
  REPLAYABLE on the 1-min store instead. `peak_pct`
  persists in export_state for the trail; one entry/day (a stopped-out day doesn't re-enter).
- **intraday_strangle_combo** (`strategies/intraday_strangle_combo.py`, NIFTY + SENSEX,
  owner deck 2026-08-12): sell 1 lot OTM3 CE + PE on the current weekly at 09:16, flat 15:25.
  Index rotates by weekday (`day_schedule`, default Mon NIFTY · Tue BOTH · Wed/Thu SENSEX ·
  Fri NIFTY — expiry-anchored). **The two legs are managed INDEPENDENTLY** (the deck's one
  "non-negotiable"): 40%/70% stop/target on each leg's OWN entry premium, and an exiting leg
  re-enters at a **freshly recomputed** OTM3 — `max_sl_reentries` AND `max_target_reentries`
  are SEPARATE budgets (2+2 → a side can trade 5×/day). Nothing may be read or written across
  sides. **ORDER IS LOAD-BEARING** on a re-entry: the EXIT_ALL signal must precede the
  ENTER_SHORT, because `overrides.py` resolves EXIT_ALL against the PRE-action book — with an
  unmoved spot the re-entry lands on the same symbol and entry-first would have the close
  swallow the reopened lot (the run-#203 merge bug in miniature). Pinned by test.
  **OTM3 counts the exchange's LISTING grid** (`_LISTING_STEP`: NIFTY 50, SENSEX 100 → spot
  25000 gives 24850 PE / 25150 CE), deliberately NOT `selection_step` — the ONE place the
  platform's NIFTY-100s rule (§8) is set aside, which is why v1 is **BACKTEST-ONLY**: the live
  chain coarsens NIFTY at `_coarsen_chain`, so a deploy would silently place 100-step strikes.
  The harness force-enables `allow_fifty_strikes` for this id (`_NEEDS_LISTING_GRID`) — never
  a form checkbox, since a coarsened run places different strikes and still looks healthy.
  **LIVE carve-out (2026-08-13, owner-approved after the paper forward test):** the strategy
  class declares `needs_listing_grid = True`; `_build_session` then sets
  `LiveOptionsMarketView.allow_listing_grid`, which skips `_coarsen_chain`. Default False, so
  §8's round-strikes rule still holds for every other deployment — this is the ONE documented
  exemption, pinned in `tests/test_nifty_strike_rule.py`. Without it the run asked for 24850,
  never found it in the coarsened chain, and SILENTLY never entered.
  **A DERIV deploy passes its index as `universe=[config.underlying]`, NOT `underlyings`** —
  the ctor reads universe when underlyings is absent. Before that fix, deploying a NIFTY run
  and a SENSEX run gave BOTH the whole weekday schedule, so on a shared day each would have
  doubled the other's book. The replay harness pins `underlyings`, which is why no test caught
  it; there is one now.
  **Deploy surface (2026-08-27):** `POST /trade/options/strangle-combo/deploy` + the Trade
  page's "Strangle combo" card — until then the Aug-13 live carve-out was ENGINE-only and
  the only way in was a raw `/live/start` call (the owner hit this on the VPS). One INDEX
  per deploy (the ctor's universe precedence); `mtm_stop_per_lot` is a SCALAR there (one
  index — the ctor spreads it); deploy exit default 15:20 per the intraday-family rule,
  ctor stays 15:25 (§1). Model→route→ctor contract pinned in its test file.
  **`same_strike_action`** (2026-08-14) decides what happens when a leg's exit fires but the
  recomputed OTM3 is the strike it is ALREADY on — spot must move half a grid step (25 pts
  NIFTY / 50 SENSEX) before the strike shifts, so until then the deck's re-entry repositions
  nothing and merely re-bases the stop wider: `reenter` (deck, ctor default per §1) ·
  `skip` (book it, stay flat and armed) · `hold` (don't exit; carry the leg until it can roll
  away — this DEFERS the leg stop, so only the MTM stop backstops it, and the deck turns that
  off on SENSEX). Same-strike is ~10% of NIFTY re-entries (50-pt grid) but was 4 of 6 on
  SENSEX 2026-08-13. The side lifecycle runs on a persisted `pending` re-entry so the budget
  is charged when one LANDS, not when it is owed (a restart that dropped it would leave the
  counters spent but the side looking untraded → free re-entries).
  **`wing_steps`** (>0) buys a protective long that many listing-grid steps beyond each short —
  the intraday IRON FLY/condor: defined risk BY CONSTRUCTION (a stop can't work through a gap)
  and far less broker margin. The wing is protection, NOT a managed leg — no 40/70 exits of its
  own, it rolls with its side's re-entry and is sold the moment that side is done for the day.
  Risk is a **rupee** MTM stop per index (`mtm_stop_per_lot`, NIFTY ₹1,500/lot, SENSEX 0 = off),
  day-cumulative (realized + open) and evaluated PER underlying; on breach the index's book
  closes and stops for the day. It **outranks a leg stop in the same tick** — and with the
  deck's numbers they collide constantly (a 40% stop on a ₹100 premium is −₹3,000, 2× the
  ₹1,500 budget), so the overall stop usually binds first. A rupee threshold is also the one
  case where backtest and live thresholds are identical (everything else anchors to broker
  margin, which reads ~1.5-2× the model margin in replay). SENSEX is **unvalidated** — the
  1-min store holds 23 SENSEX days vs 1,249 NIFTY. Coverage: `tests/test_intraday_strangle_combo.py`.
- **weekly_intraday_straddle** (`strategies/weekly_intraday_straddle.py`, NIFTY; ref video
  https://www.youtube.com/watch?v=kYahbSjbubQ): a WEEKLY-CYCLE intraday SHORT straddle. A cycle
  spans one weekly expiry; the ATM strike (nearest 100 — the live chain coarsens it) is LOCKED
  once at 09:20 on the first trading day after the prior weekly expiry (a mid-cycle deploy
  auto force-starts at the current ATM; re-anchors at each expiry+1, detected off the chain's
  nearest weekly), then traded EVERY day of the week on that FIXED strike. Daily, on the last
  CLOSED 5-min bar of the combined premium: SELL when `x` (=CE.close+PE.close) < `y` (the prior
  day's intraday combined-premium LOW) AND `x` < VWAP (sum of per-leg volume-weighted VWAPs);
  exit when `x` closes back ABOVE VWAP, or 15:25 (hard, never waits on margin); optional
  `stop_loss_pct` (% broker margin, default 0=OFF — uncapped short tails). Up to
  `max_entries_per_day` (3) re-entries; intraday only (squared off daily). **The NEW capability
  it needs**: option-contract 5-min bars WITH volume — the platform previously fetched Kite
  historical for the index SPOT only (volume dropped). `ZerodhaAdapter.option_intraday_bars`
  resolves the option instrument_token from the cached NFO/BFO dump (`_nfo_token`, no extra
  `ltp()`) and keeps volume; wired into the strategy by `manager._wire_quote_source` via
  `set_option_bars_fn` (None on a cache source → entries gate off, safe). `on_slice` runs the
  15:25 exit + optional stop BEFORE the (try/except-wrapped) bar fetch so a broker hiccup can't
  swallow the square-off. **Unfetchable bars = a surfaced ERROR, not a silent no-op** (owner
  rule 2026-07-15): the strategy sets `strategy_alert` (no source / no today-bars past the
  first close / no prior-day bars / fetch raised), the manager snapshot + `/live/summary` tile
  carry it (amber banner + "data ⚠" chip), and ALL entries — **including the force button** —
  stay disabled while set (a forced book would have no working VWAP exit); exits still run.
  Deploy-only + broker source required; no EOD backtest — REPLAYABLE on the 1-min store (the
  GFD import + daily capture that seeded it are the data this was waiting for). Coverage: `tests/test_weekly_intraday_straddle.py`.
- **delta_neutral_monthly** (18Δ BANKNIFTY monthly strangle): entry expiry+2 TRADING days
  ~11:00 (force_entry deploy flag skips the wait); adjustment rule is the spec's EXAMPLE,
  not its prose — when |CE−PE| > 40% of (CE+PE), the CHEAP side rolls to the strike whose
  LTP matches the rich side, hard-capped at the other strike (straddle max, never
  crossing); straddle → breakeven hedges (K ± combined) in the SAME decision → ironfly.
  margin_base tracks the BROKER basket margin ONLY (manager `set_broker_margin` push;
  thresholds WAIT while "pending"). **Two-margin scheme (owner 2026-07-27):**
  `exit_margin_basis` picks the ₹-threshold anchor — ctor default `"current"` (§1: re-frozen
  after every roll/hedge/naked-add, so an iron fly targets 2.5% of the fly's SMALLER margin and
  the ₹1L→₹2.5L naked-add SPAN jump re-bases the stop) vs `"entry"` (FORM+deploy default:
  frozen ONCE at cycle entry → absolute ₹ target/stop fixed from day one; cleared in `_exit_all`
  so the next cycle freezes its own; the dynamic push still feeds margin_used/sizing/reports —
  the "strategy margin"). `"entry"` + `margin_per_lot` keyed to the real broker entry margin
  makes replay exit ₹ == live exit ₹ exactly (the replay's model margin is shorts-only,
  wings-blind, linear-in-short-count — fine for reporting, wrong for thresholds). Stop param
  default OFF; recurring monthly (done_expiry gates same-month re-entry). Deploy-only + broker source
  required (live-chain delta solve); no EOD backtest — BANKNIFTY chain history ≈ 2 months in
  cache — but REPLAYABLE on the 1-min store (a full monthly cycle needs ~2 months captured).
- **Post-iron-fly adjustment (shared, 2026-07)** — on the BASE class, GATED by `ironfly_adjust`
  (**default False in delta_neutral_monthly** per §1 — a running deploy is unchanged on recovery;
  runtime-togglable via `set_ironfly_adjust` → `POST /live/{id}/ironfly-adjust`, persisted in
  export_state, so it survives a restart). When enabled and phase=="ironfly", `_adjust_ironfly`
  replaces the old terminal ride: on a breakeven breach (K ± net_credit) it sells a naked
  ~15-20Δ short on the UNTESTED side (`adjust_target_delta`, reuses `_pick_delta_strike` with
  `exclude`=the fly's held same-side strikes — the naked short must NEVER land on a strike the
  fly already holds, or it MERGES into that leg's position and a later roll's EXIT_ALL closes the
  STRADDLE short too, leaving a naked call: the run-#203 blow-up, 2026-07), rolls
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
  (`POST /trade/options/iron-fly/deploy`, broker source required, `_DEPLOY_ONLY`); no EOD
  backtest — REPLAYABLE on the 1-min store (same ~2-months-per-cycle caveat).
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
- **Unified backtest page (2026-07):** the New-backtest form has a **data basis** —
  `EOD (daily cache)` = the engine path, untouched; `Intraday (1-min store)` = minute-replays
  over the self-captured option store via `services/intraday_replay.py` (`POST
  /backtest/intraday`, `GET /strategies?basis=intraday`). The harness replays the ACTUAL
  strategy classes (all six deploy-only options strategies; momentum_theta dispatches to its
  BS service, labeled `premium_source=black_scholes`) with parity spot (F≈K+CE−PE), the
  NIFTY-100 coarsening, model-margin pushes (~1.5-2× broker — %-of-margin stops are wider in
  ₹ than live), F&O charges per fill, and expiry settlement to intrinsic. **Runs are ordinary
  `AlgoRun`s** — the replay emits the standard report contract (`metrics` keys +
  `equity_curve` + Trade-shaped `trade_log`; exit rows SELL/COVER/SETTLE) so Runs/RunDetail/
  Compare render unchanged; tagged `params["data_basis"]="intraday"` (no schema change).
  FOOTGUN: never set `report["options"]` unless the FULL options sub-report is built —
  its mere presence flips ReportView into a layout that dereferences
  `options.summary.total_charges`. Coverage: `tests/test_intraday_replay.py`.
  **Intraday replays run as a BACKGROUND JOB (2026-07-17):** POST /backtest/intraday
  returns `{job_id}` (409 while one runs — single-flight, `services/replay_jobs.py`);
  GET /backtest/intraday/progress carries {done,total,day} + the full result when done
  (the form polls it — a page revisit re-attaches). Sizing params (default-off, harness-
  only — zero strategy-class edits): `margin_per_lot` = TODAY'S broker margin ₹ for one
  lot-set of the strategy's structure, converted to a %-of-notional against the latest
  store day and applied ERA-TRUE (old years scale down with spot × era lot size; the era
  key is the nearest EXPIRY — revisions bind to contracts, not trade dates);
  `sizing="capital"` + `sizing_buffer_pct` refit lots per FLAT day from CURRENT equity
  (floor(equity/(era-margin×(1+buffer))); 0 lots ⇒ the day's entries are skipped, never
  0-unit orders). `_LOT_SIZES` now carries BANKNIFTY eras (25→15 2023-07→30 2024-11-20→35
  2026-01-01) alongside NIFTY's; `params["contract_specs"]` overrides thread into the
  replay's lot lookups. **Replay spot is DE-CARRIED to cash** (`_Market._decarry`: parity
  F/(1+0.065·t)) — raw parity is the FUTURES level, and its ~20-pt carry bias picked the
  24200 straddle on 2026-07-16 while live (cash index spot) picked 24100, flipping that
  day's P&L sign; no-op on expiry day so settlement intrinsic stays exact. `_to_report`
  also emits `yearly`/`monthly_profit`/`monthly_equity` from the equity curve (the EOD
  contract keys → ReportView's existing tables just render; runs saved before 2026-07-17
  stored reports without them — `get_run` now derives the missing tables from the stored
  curve at READ time, `_backfill_period_tables` in `api/routes/backtest.py`, never
  mutating the row).
- **Two-cadence model + ALL index options on the store (2026-07-18, owner design):**
  every options strategy samples its PROFIT/ADJUST decision on `profit_check` and its
  STOP/EXIT on `stop_check` (tick/1..60min/eod@`eod_time`) via `ExitCadenceMixin`
  (`_options_common` — extracted from the ratio family, where it originated). RULES:
  `_due` CONSUMES its window → sample once per kind per slice AFTER all readiness guards
  (margin frozen/prints/pnl) or a stop slot is silently eaten; hard time exits are NEVER
  gated; multi-book strategies key per book (`f"stop:{u}"` — cpre). Ctor defaults stay
  "tick" (§1 — recovered deploys byte-identical; verified no stale cadence keys in any
  running snapshot); FORM/deploy defaults carry the policy: profit 1min everywhere, stop
  1min (intraday family) / eod 15:20 (positional family). weekly_intraday_straddle has
  NO profit_check by design (VWAP exits — no profit decision exists). **The positional
  family (call/put/batman ratio, hni, ema21) is REPLAYABLE on the 1-min store** via
  `_Chain`'s cached-chain adapter (chain()/spot()/expiry_for_dte emitting STORE-format
  symbols — load-bearing: `_fill` splits on "|"); the EOD options basis left the UI
  (backend strategy lists unchanged — DeployPage depends on them; `build_options_run`
  stays for stock-option strategies [donchian_bt, covered_call, short_premium — no stock
  1-min data exists] and old runs). Harness sizing is THE sizing on replay (the "sizing"
  name collision resolves by construction: the harness pops it first, so ratio
  strategies always build sizing="fixed"). ema21's bands: cache daily bars for PRIOR
  days + a FORMING today-bar from the replay's running parity spot (owner vetoed the
  settled-bar lookahead). Store replays are NOT byte-comparable with old EOD options
  runs (real minute fills, per-fill charges, frozen pushed margin, de-carried parity
  spot). Coverage: `tests/test_exit_cadence.py`, replay round-trips in
  `tests/test_intraday_replay.py`, registry pins in `tests/test_backtest_v2_registry.py`.
- **fair_value_calendar** (`strategies/fair_value_calendar.py`, NIFTY, 2026-08-19; ref video
  https://www.youtube.com/watch?v=tn-73I63yBw&t=2162s): monthly
  premium-matched ratio calendar — sell a ~₹150 and a ~₹450 near-weekly (≥`min_sold_dte`=4 DTE)
  + buy 3× a ~₹200 monthly, SAME side; the side comes from the **fair-value line** (Jan-2020
  high 12,430 × 1.117^yrs): above by >`fv_band_pct` → PE, below/inside → CE (`side_mode` also
  offers both/pe/ce). **900-pt gap rule**: |buy K − furthest sell K| > `max_gap_points` → skip
  the DAY, retry tomorrow (entry retries daily all month — `entered_month` latch, one cycle per
  calendar month). Target 5% of FROZEN broker margin (₹1.43L/set measured 2026-08-19) on the
  WHOLE cycle (`pnl_basis="total"` — the rolls ARE the income); not hit by the sold expiry →
  roll both sells to the next weekly at the SAME strikes (roll_time 15:00, before the replay's
  15:30 settle), banking into `realized_rolls`. **`roll_days_before`** (owner 2026-08-20) moves
  the roll OFF expiry day — a short weekly's margin spikes into settlement — firing N TRADING
  days earlier (`_roll_date` walks `previous_trading_day`, never a weekend/NSE holiday). Ctor
  default **0** = the historical expiry-day roll (§1: recovered deploys unchanged); FORM +
  deploy default **1**, pinned in `test_backtest_v2_registry._INTENTIONAL`. The due test is
  `today > roll_date`, so a roll that cannot fill on its day still catches up on expiry day.
  Next weekly == buy expiry → **the CYCLE ends**
  (`fvc_cycle_end`, owner rule 2026-08-19: the buy leg is NEVER rolled) — exit all, clear the
  month latch, and a FRESH cycle (new FV read + hunt) opens next session, so a losing cycle is
  structurally bounded at ~one buy-expiry month. A window that ends mid-cycle emits the open
  episode as a `live: true` cycles row (closed-only aggregates) — before that, run #269's tiles
  counted the open cycle's banked rolls while the table couldn't show it (a phantom ₹-88k gap).
  CAPTURE-ERA CAVEAT: the store lists only ≤40d expiries, so late-month entries can pick a far
  WEEKLY as the "monthly" buy (max-of-month on a truncated ladder) — live chains are immune. Rolls are ALL-OR-NOTHING (every new leg must price or
  the whole roll defers a tick) and a settled-short backstop re-sells from the persisted
  `sold_specs` (strikes are frozen at entry). Premiums are ABSOLUTE ₹ from
  `premium_scale_before` (2024-08-01) and scale by spot/`premium_ref_spot` before (owner: ₹150
  at 25k wasn't ₹150 at 16k); the gap cap scales identically. Subclasses DeltaNeutralMonthly
  (ddc precedent) with phase="calendar" so the base delta-adjust machinery never engages.
  Deploy-only + broker source (`POST /trade/options/fair-value-calendar/deploy` + the Deploy
  page's "FV calendar" builder, 2026-08-19 — deploy default `side_mode="ce"`, the only mode the
  backtest likes); REPLAYABLE on the 1-min store (the first two-expiry strategy WITH a backtest).
  **`margin_per_set` — a MANUAL margin anchor (owner 2026-08-25).** The % target/stop is
  measured against `margin_base`, which is normally the frozen BROKER basket margin — and that
  is not a stable anchor: run 15 froze ~₹70k/set at entry, while the SAME legs priced ₹4.5L/set
  on expiry day with a deep-ITM short. A "5% of margin" target therefore meant wildly different
  rupees over one cycle. Set `margin_per_set` (₹ for ONE lot-set) and it OUTRANKS the broker
  push, scales by `_size_multiple()` (`sets` here, `lots` on the base class), and needs no wait
  — thresholds are live from the first tick instead of sitting `margin_source="pending"`.
  `margin_source` reads `"manual"`. Ctor default **0** = derive from the broker exactly as
  before (§1); FORM + deploy default **134612** (the owner's measured Kite figure), allowlisted
  in `test_backtest_v2_registry._INTENTIONAL`. Hot-editable on a running tile.
  **The Live tile reads the SAME anchor (2026-09-02).** `exit_amounts` (the snapshot's
  `profit_target_amt`/`stop_loss_amt`) used to answer for a broker-frozen base only, so every
  manual-anchor deploy showed "Margin used ₹3,38,918 · Zerodha basket" with NO target while
  the strategy worked towards 5% of ₹4,03,836 — and the callout said "of entry margin".
  Now the snapshot also carries `threshold_base`/`threshold_source` (`LiveSession.
  _threshold_anchor`, generic `margin_base`+`margin_source` probe), the KPI band prints the
  anchor row when it differs from the live basket figure and computes the % off it (÷
  margin_used read 5.96% for a 5% rule), and `_margin_label()` names the anchor with its
  arithmetic in `exit_rules` for the whole delta family. The three margin numbers on that
  card are DIFFERENT things: margin_used = the broker's live basket (moves every tick),
  threshold anchor = what the %-rules are measured against (frozen), deploy capital = neither.
  **The two margin numbers differ by CONVENTION**: Kite's basket API returns `final.total` NET
  of premium receivable (cash actually blocked) while the web calculator's headline "Total
  margin" is span+exposure BEFORE that credit — ₹1,15,288 vs ₹1,34,612 on the same 2026-08-25
  basket. Enter whichever you mean to size on; the value is used verbatim. Inherited by
  delta_neutral_monthly / iron_fly_monthly / double_diagonal_calendar — and note each subclass
  with an explicit signature must FORWARD it, or `**_ignored` swallows it silently (pinned by
  `test_margin_per_set_is_forwarded_through_every_explicit_subclass`).
  FOOTGUN: the ctor knob is `sets`, but the backtest form's Sizing **LOTS** is the lot-SET count —
  the harness maps `lots`→`sets` (`intraday_replay`), because a `lots` kwarg falls into
  `**_ignored` and silently traded 1 set under a 5-set capital plan until 2026-08-19. Never
  re-add a separate "lot sets" field to the form. **5y store replay, 1 set, ₹1.45L margin/set:**
  ce +₹147k (65 cycles, 77% win, DD ₹52k, t=2.4) · fair_value −₹64k · both −₹154k · pe −₹196k —
  monthly re-centering rides the drift on calls, chases crashes down on puts. Coverage:
  `tests/test_fair_value_calendar.py`.
- **volcano_calendar** (`strategies/volcano_calendar.py`, NIFTY, 2026-08-25; owner video
  deck): monthly PE butterfly (ATM / −400 ×2 / −800) on the NEXT month's monthly + a CE
  calendar at ATM+200 (short that expiry, long the month after, ONE shared strike — the legs
  move together). Entry the **last Friday of the month at 15:16** via module helper
  `last_trading_friday` (walks back over weekends/`is_nse_holiday`; validated against the
  deck's own Jun-2026 example, which entered THURSDAY the 25th because Friday was a holiday).
  The gate is `today >= last_friday` + an entered_month latch — **stamped at ENTRY ONLY**: a
  cycle entered in April closes on the MAY expiry, and stamping May at close would silently
  skip the May-29 entry (six cycles a year instead of twelve; caught by test before it
  shipped). **The 4% center-credit rule** (owner's reading, settled 2026-08-25): payoff at
  the entry spot evaluated ON the near expiry (the valley between the peaks) as % of margin;
  while > `max_credit_pct` the CE strike steps out 100 pts (≤ `max_ce_shifts`).
  `_payoff_on_near_expiry` prices near legs at `bs.intrinsic` and the far CE at `bs.price`
  with `t_resid` + an IV solved from its own LTP — NOT the base `_payoff_at`, which intrinsics
  every leg and would misprice the calendar's whole point; an unsolvable IV DEFERS the entry
  (all-or-nothing, no latch). **`margin_per_set` is effectively required**: the rule needs a
  denominator BEFORE the order exists (broker margin is pushed only after a fill) — with 0
  the walk is skipped + `strategy_alert`, base strike stands. It also anchors the ±2%
  target/stop (margin_source="manual", live from the first tick). Exits: ±2%, else close ALL
  five legs (far CE included) at `cycle_exit_time` (15:15) on the near expiry — checked FIRST
  in `_manage`, never cadence-gated — with a past-expiry `volcano_cycle_end_late` backstop.
  Subclasses DeltaNeutralMonthly, `phase="volcano"` (base adjustments inert). Deploy-only
  (`POST /trade/options/volcano-calendar/deploy` + Trade-page card, broker source required);
  **no backtest** — the 1-min store captures ≤40-DTE expiries and the far CE is ~60 DTE at
  entry. Also fixed while wiring: the fvc deploy route never forwarded `roll_days_before`
  (model default 1 silently unused — run 15's expiry-day roll). Coverage:
  `tests/test_volcano_calendar.py`.
- **broker_smoke_test** (Brokers-page card, 2026-07-18): the end-to-end REAL-order probe —
  BUY 1 lot of a cheap OTM weekly (premium band ₹5–20, nearest ₹10, OI floor) or 1 share of
  a stock (default ITC), hold ~60s, SELL, then the run **stops itself** (`stop_requested` →
  `manager._maybe_self_stop`, honored on the EVENT LOOP side only — `manager.stop` cancels
  the loop task — and hard-guarded on a FLAT book: a run holding positions never self-stops).
  One cycle exercises place → poll → LIMIT-at-touch→protected-limit escalation (the wide OTM spread
  triggers it naturally) → fill → book-sync → reconcile (+ the per-recon Telegram) → exit.
  **A MANUAL flatten must not restart the cycle (2026-08-25).** `flatten` closes the book and
  then calls `sync_strategy_book`, which rebuilds `legs` from the now-EMPTY portfolio — so
  `legs` is `[]`, the "book flat → test complete" guard is unreachable, and `exited` is still
  False (the strategy never ordered that exit). The run then looked untraded and placed a
  SECOND REAL BUY four seconds after the owner's exit filled (run 21: BUY 267.90 → manual SELL
  269.15 → BUY 269.20). The flat-book branch now also stops on `entry_at is not None` — set
  once at entry, never cleared, persisted. Generalise the lesson: **`sync_strategy_book` wipes
  a strategy's `legs`, so any "have I traded?" test that reads `legs` is wrong after a manual
  intervention.**
  Sizes are hard-coded 1 lot / 1 share (not params). Deploy-only, no backtest (paper fills
  always "work"); `POST /trade/smoke-test/deploy`; one class, two deploy modes (DERIV option
  leg / STOCK share leg — `intraday=True` keeps the STOCK run tick-driven). UI gates LIVE
  behind a typed "REAL" confirm; all §1 keys still apply — a LIVE deploy on a disarmed
  account paper-fills and wears the "orders PAPER" chip (a useful negative test). Per §1
  Claude never deploys it LIVE. Coverage: `tests/test_broker_smoke_test.py`.
- **Donchian flip default:** new deploys roll a breached name **intraday** (`breach_basis="touch"`),
  **once per name per day** (`last_flip_day` guard), up to `max_flips=3` (two rolls, then close the
  name on the next breach). Defaults live in the deploy layer (`api/models.py:DonchianDeploy`); the
  strategy **constructor** stays `close`/2 as the conservative backstop for a param-less recovery
  (CLAUDE.md §1). To change a **running** deploy's config: `POST /live/{id}/params` (the Live
  tile's "Edit params" menu, 2026-07-29) hot-edits strategy knobs in place —
  `LiveRun.update_params` rebuilds the strategy **recovery-style** (ctor logic re-runs, so
  derived fields recompute; `export_state`→`load_state` carries positions/latches/peaks), re-runs
  `_wire_quote_source`, and **MERGES** the changes into `Algo.params` + `AlgoRun.params_snapshot`
  (never replaces — a recovered run's `config.params` is stripped of infra keys, and overwriting
  the snapshot with it would break the next recovery). Infra keys
  (`LiveRun._PARAM_EDIT_BLOCKLIST`: mode/capital/symbols/quote_source/…) are rejected → stop +
  redeploy; unknown ctor kwargs are rejected too (strategy_kwargs would silently drop them —
  a typo'd knob must not report "applied"). Coverage: `test_update_params_*` in tests/test_live.py.
  The old edit-`params_snapshot`-in-DB-and-restart route still works for stopped backends.
- **Adopt-broker-close** (`POST /live/{id}/adopt-broker-close`, the Live tile's "Mark closed at
  broker", 2026-08-12) books legs the BROKER has ALREADY closed — you squared off in Kite, or an
  intraday auto-square-off fired — at the price the caller states, **placing no order**.
  `flatten` is the wrong tool there: it PLACES orders and there is nothing left to trade, so the
  platform keeps a phantom leg and every reconciliation halts the run. Before this the only
  repair was to stop the backend and hand-edit `algo_run.state` (run 10, 2026-08-11).
  Implementation deliberately reuses the ORDINARY exit path — `LiveSession.adopt_broker_close`
  builds the same CloseShort/ClosePosition actions as `flatten` and swaps
  `executor.broker` for a `_SettledPriceBroker` (restored in a `finally`) — so portfolio
  bookkeeping, F&O charges, trade events (`exit_reason="broker_closed"`, tag MANUAL) and
  strategy book-adoption are identical to a real exit. Prices default to the last mark in the UI
  but MUST stay editable: the settled price is usually not the mark (2026-08-11: mark 28.25,
  actual fill 33.90). Not routed through `_manual_guarded` — no order can fail here. It does
  NOT clear an `order_error` halt; that stays a separate, explicit ack.
  Coverage: `test_adopt_broker_close_*` in tests/test_live_options.py.

## 8a. The /portfolio tracker is NOT part of the trading system
**The VPS is the authoritative portfolio.** It holds the owner's real book (56 holdings)
and is the only box with both broker sessions and an always-on maintenance loop for the
daily snapshot — a laptop that sleeps leaves a hole in a history nothing back-fills. The
Mac's portfolio tables are DEV-ONLY and deliberately empty; never treat local numbers as
real, and never edit the two in parallel.
A personal net-worth screen (`/portfolio`, 2026-09) that happens to live in this repo. It places
no orders, reaches no order path, and its only broker calls are `holdings()` and quotes. Keep it
that way — nothing in `services/portfolio*.py` or `api/routes/portfolio.py` may import from
`live/` beyond read-only adapter use.
- **The transaction ledger is the source of truth when it exists.** `PortfolioTransaction` rows
  drive FIFO lots → cost basis, real money-weighted XIRR, per-LOT tax and realized gains. A
  holding with no rows falls back to typed `invested`/`units`/`value` (PPF, EPF and property will
  never have a trade history) and `basis` says which path produced the record. **Once a ledger
  exists, `value` is units × `last_price`** — units are then a FACT and a stale typed value must
  never contradict them.
- **Tax is PER LOT, not per holding.** A position built over years straddles the 12-month LTCG
  line, so one holding legitimately sits in two regimes at once. Never collapse it to a single
  rate; the per-holding row says "2 regimes" and links to the By-lot view. The ₹1.25 L equity
  exemption is applied ONCE across the portfolio (`apply_equity_exemption`) — subtracting it per
  holding understates a bill by lakhs.
- **A sync NEVER invents and never overwrites the ledger.** A missing quote, an unlisted ISIN, or
  a broker book that disagrees with the ledger → the holding is left untouched and an `issues`
  entry is raised. A units mismatch is REPORTED, because only the owner knows which side is
  missing a row; silently taking the broker's number destroys the only evidence. Untracked broker
  positions are `discovered`, never auto-added.
- **Mutual funds price off AMFI by ISIN** (`data/amfi.py`, free daily `NAVAll.txt`). Both ISIN
  columns are indexed (the owner may hold either plan); a NAV is a DAY BEHIND during market hours
  by design, so every price carries its own `as_of` and day change needs yesterday's cached file
  (no prior file ⇒ change is unknown, not zero). `day_quotes()` is the NEW optional adapter seam
  carrying `prev_close` — `get_quote` returns LTP alone, which cannot express a day change.
- **US equities and crypto price off `data/global_quotes.py`** (`sync_source="global"`,
  `sync_ref` = a Yahoo symbol) — no Indian broker quotes them, so they sat frozen otherwise.
  The multi-symbol endpoint 401s, so it is one request per symbol, throttled; fine once a day
  and NEVER on an order path. **A USD quote is converted at BOTH ends of the day change** —
  today's price at today's rate, the previous close at the RATE'S own previous close — because
  an INR investor's position moved by the currency as well as the instrument, and on a 1% rupee
  day that is the larger half. An INR-quoted pair (Yahoo carries `BTC-INR`) must NOT be
  converted at all: doing so reports a bitcoin at a hundred times its price. Pinned by
  `test_an_inr_quoted_pair_is_not_converted_twice`.
- **The previous close comes from the CANDLE SERIES, never `chartPreviousClose`.** That field
  is the close before the requested WINDOW, so on a 5-day range it is a week old. Trusting it
  reported MSFT at **+5.65% on a day it fell 1.22%** — the wrong SIGN, not just the wrong size —
  and the USD/INR rate was a week stale on top of it, compounding every US figure. Pinned by
  `test_the_previous_close_is_the_prior_SESSION_not_the_prior_window`.
- **A non-INR holding stores its OWN currency, price and cost** (`native_currency` /
  `native_price` / `native_invested`). The dollar cost basis CANNOT be recovered by dividing the
  rupee one by today's rate — those rupees were spent at the rates of the day. The screen shows
  both, INR as the primary figure.
- **AMFI ships THREE column layouts and they disagree on where the ISIN is** — index 1-2 in the
  daily NAVAll, index 4-5 in the dated historical report. `parse_navall` locates columns from
  the HEADER, falling back to reading from the ends (code first, date last, NAV second-to-last)
  when there is none. A positional parser reads a scheme NAME as an ISIN and indexes nothing.
  `amfi.backfill(day)` seeds a PRIOR day from the historical report so a fund day change works
  on the first sync instead of waiting a night for a second file to exist. The prior NAV is
  assembled PER ISIN across every older cached file, newest first — AMFI publishes on
  weekends for overnight/liquid schemes ONLY (~630 of 14,137), so taking the newest older
  file wholesale leaves every equity fund with no comparison and a day change of zero.
- **Zerodha's symbol suffixes are not all its own.** `-E`/`-F` are its ETF/liquid markers and
  must be STRIPPED (the exchange has no `GOLDBEES-E`); `-RR`/`-IV` are the NSE SERIES for a
  REIT and an InvIT and are PART of the tradingsymbol (`EMBASSY-RR` prices, `EMBASSY` does
  not). Strip the wrong set and those holdings silently never get a price.
- **Growth history is recorded FORWARD and never back-filled.** `PortfolioSnapshot`, one row per
  trading day from `manager._maybe_daily_portfolio_snapshot` (≥16:00 IST). A holding reads
  **null**, not zero, before it was tracked — zero draws a line rising off the floor. Do not add
  a "reconstruct from today's units at old prices" path: that is a chart of a portfolio that
  never existed.
- **The WIDE sheet seeder** (`parse_ledger_paste` → `POST /portfolio/seed`) loads the owner's
  multi-symbol tracking spreadsheet, where buys and sells are different COLUMNS of one row.
  **Tabs are required** — they alone preserve the empty cells that tell the two apart, so a
  tab-less paste is refused, never guessed at (a wrong guess imports the position inverted and
  looks fine). A **sell with no price is an error** (the real sheet has one: 15,161 NIFTYBEES
  units with the price left in the notes → a ~₹38.7 L phantom loss). `Invested Amount` and
  `Booked Profit` are DERIVED columns (the latter is gross proceeds, not profit), so they
  cross-check for typos and warn without blocking.
- **A zero-price BUY is a CORPORATE ACTION (`kind="bonus"`), and it RE-BASES the open lots.**
  Never append it as an ordinary lot: the older lots then keep their PRE-split per-unit cost
  while every later sale is of POST-split units, so FIFO matches a ₹972 sale against ₹7,192.
  Bajaj Finance's June-2025 1:10 (4:1 bonus + 1:2 split) invented **₹10.3 L of losses** that
  way; re-basing gives **+₹2.19 L** over the same 62 trades. `build_ledger` derives the ratio
  from units held at the time ((held+added)/held), multiplies every open lot's units and
  divides its cost/unit — total cost, FIFO order and lot dates all preserved — and records it
  in `Ledger.actions`. A bonus against NOTHING held is recorded with `ratio: None` and NOT
  applied (a buy row is missing; inventing a lot would fabricate a cost basis). Simplification
  worth knowing: bonus shares strictly carry nil cost and their own allotment date for Indian
  CGT, so this can differ only on the holding period of the bonus portion, only within 12
  months of the action.
- **`units_locked` = the row is a CROSS-BROKER AGGREGATE.** The same stock legitimately sits
  in several accounts (GOLDBEES spans four), so the holding's units are a total no single
  broker's book equals. A sync then refreshes PRICE only: it must never adopt one account's
  count (which would replace the total with a fraction) and must never raise a book mismatch
  (which would fire on every pass, forever). Pinned by
  `test_an_aggregate_holding_is_repriced_but_its_units_are_never_rewritten`.
- **A holding's TAG (`kind`) is the level rebalancing happens at**: equity / debt / gold /
  realestate / crypto. Coarser than asset class on purpose — real risk is "how much equity",
  not "how many mid-cap funds". It drives the TAX REGIME as well as allocation
  (`regime_for` reads `kind == "debt"`), so retagging a fund changes both and they can never
  disagree. An unrecognised override falls back to the class default rather than creating a
  sixth tag no target covers.
- **Two different things label a holding, and they must not be merged.** `kind_override` is
  the ASSET CLASS: one per holding, drives rebalancing and the tax regime, so each rupee is
  counted exactly once. `PortfolioTag` (many-to-many, `portfolio_holding_tag`) is a free
  user-defined LABEL — "child's education", "long term" — and a holding may carry any number.
  Folding tags into the class would double-count a holding in two of them, or drop it; folding
  the class into tags would leave rebalancing with no denominator.
- **A GOAL is a stream of outflows, not a number at a date** (`schedule` = `[{year, amount}]`
  in TODAY's rupees + `inflation_pct`). Three things the projection gets right that a single
  target cannot: amounts are inflated to the year they are NEEDED (₹42 L of school fees is
  ₹65 L by 2036 at 6% — planning against the raw figure understates by half); a goal can be
  fully funded in TOTAL and still fail in one year, so the walk reports
  `first_shortfall_year`; and a shortfall is FLOORED AT ZERO rather than left to compound —
  a negative corpus growing at the equity return turned a ₹15 L gap into ₹1.48 Cr of fiction.
  The current year earns only the months left of it.
- **Expected income is DERIVED (value × yield), never stored.** A typed forecast ages into
  fiction the moment the position changes size. Only RECEIVED distributions are recorded
  (`portfolio_dividend`), bucketed by the INDIAN financial year. A holding with no yield set
  is EXCLUDED from the estimate and its value reported separately — "unknown" and "pays
  nothing" lead to different decisions.
- **`broker_units` is what makes a LIVE account safe inside an aggregate.** `units_locked`
  freezes units entirely — right for a statement-loaded total, wrong when one of its sources
  trades daily (value_investing buys at Dhan every day). The per-source breakdown
  (`{"static": 7769, "account:3": 12}`) lets a sync rewrite ONLY the slice for the account it
  just read and re-total; a successful book read that omits the symbol sets that slice to ZERO
  (a real sell-out), while the statement-loaded slices — which no sync can see — hold. Empty
  dict = the whole row is static. Coverage:
  `test_a_live_account_slice_updates_while_the_other_brokers_hold`.
- **Broker statements: `Quantity Available` is NOT the holding.** Zerodha reports pledged
  stock in its own columns, and `Quantity Long Term` is a TAX SUBSET, never another parcel to
  add — but since it IS a subset, the total can never be less than it. One real row broke that
  invariant (AN8911 GOLDBEES: long-term 7,361 against available+pledged 1,525) and taking the
  smaller figure left the account out by 1.9%; `max(available+pledged, long_term)` closes it to
  0.15% and is a no-op on every other row. ISIN is the merge key across brokers — symbols
  don't travel (`GOLDBEES-E` vs `GOLDBEES`), and cost is summed as RUPEES, never as an average
  of averages.
- **Seed errors are SYMBOL-SCOPED.** One unreadable row skips ITS symbol and lets the rest
  seed — a global block made the button permanently dead on a 396-trade paste with one bad
  line. A symbol with a bad row cannot be seeded even if requested: a partial history inside
  one holding is the dangerous case, since its cost basis looks reasonable and is wrong
  forever.
- **The paste importer parses on the SERVER** (`services/portfolio_import.py`) so the preview the
  owner approves is the code that imports. An unreadable line BLOCKS the import rather than
  landing a partial history that looks complete; an ambiguous `d/m/y` is DAY-first and a
  month-first date is refused, not swapped (guessing moves a trade across the LTCG line).
- Aggregates (allocation, drift, buckets, goals) are derived in the BROWSER from the server's
  per-holding records, so a keystroke in a target box updates every tile with no round trip.
  `rebalanceMoves` is shared by the class and bucket rebalancers — one algorithm, so the two
  screens cannot suggest contradictory transfers.

## 8b. Mobile app (`web-mobile/` + its `ios/` Capacitor shell)
A dedicated 7-screen iPhone companion (see `docs/MOBILE.md`) that monitors + acts on the
VPS's runs over Tailscale HTTPS. Key invariants: it imports `web/src/{api,lib,types}` via
the `@shared` alias — **keep those layers pure** (no window/DOM coupling beyond client.ts's
guarded seam). `client.ts` grew `setApiOrigin()`/origin-aware `liveWsUrl()`/
`setUnauthorizedHandler()` — all default to same-origin, so the desktop app is unchanged;
don't regress that. The backend's mobile surface: `GET/POST /alerts*` (fed by
`notify/in_app.InAppNotifier` → the `alert` table; WS `{"type":"alert"}`), and
`snapshot["mode"]` (PAPER/LIVE — the app's paper/real toggle). Real-money actions in the
app are typed-confirmation-gated (ARM, LIVE deploy) on top of the unchanged §1 gates —
the app is the OWNER's hand, never Claude's.

## 8c. Deploying is ONE registry-driven page (`/trade` → Deploy, 2026-08-27)
Every deployable strategy is declared as DATA in `web/src/lib/deploy/registry.ts`; the page
(`pages/trade/DeployView.tsx`) renders the left rail grouped by HOLD LENGTH (intraday /
weekly / monthly / positional), a summary panel, and only that strategy's own knobs.
- **Why:** deploying used to split across two pages on the invisible line "does it have a
  backtest?" — generic form at `/live/new` vs hand-built cards on `/trade`. Options
  strategies lived on BOTH, with no cross-link. It cost real time twice in two days
  (value_investing deployed PAPER by accident; `intraday_strangle_combo` looked unavailable
  when it simply had no card — it had been live-capable and unreachable since 08-13).
- **The generic form was also a CORRECTNESS bug, not just UX.** Ctors end in `**_ignored`,
  so a form field that isn't a real kwarg is silently dropped — `supertrend_momentum` had
  been receiving `max_lots`, which it does not define, for as long as that form existed.
  The registry test caught it on the first run.
- **`tests/test_deploy_registry.py` is what makes the data safe** — it parses the TS and
  asserts, per strategy: the id is registered, any `route` exists on the FastAPI app, every
  field `param` is a real ctor kwarg **walked across the MRO** (subclass params hide behind
  `*args/**kwargs` — the `test_backtest_v2_registry._ctor_defaults` trick), and a /docs card
  exists for the summary panel. Plus `test_every_strategy_has_a_deploy_path`: a strategy is
  either in the registry or in `_DEPLOYS_ELSEWHERE` with the page that owns it (smoke test →
  Brokers, custom_options → Build, donchian → Screener). A new strategy with no deploy path
  now FAILS rather than being quietly unreachable.
- **Deploy defaults live in the registry, ctor defaults stay put** (§1) — intraday exits
  15:20 vs ctor 15:25, cadences 1min vs tick, fvc side "ce" vs "fair_value".
- **The summary panel reads `web/src/lib/strategyDocs.ts`** — the STRATEGIES/META tables
  EXTRACTED from StrategiesPage so `/docs` and the deploy page share ONE copy.
  `test_strategy_docs_coverage` now parses that file, not the page.
- `custom_equity` is the ONE non-declarative entry (`custom: "equity"`) — a manual position,
  not a parameterized strategy; it keeps its bespoke form and has no docs card.
- `/live/new` renders the SAME component so old links and the forward-test router state keep
  working — a `<Navigate>` redirect would have dropped `state.prefill` (§9's documented
  footgun). Prefill only applies knobs that exist on the selected strategy.

## 9. Frontend (`web/`) gotchas
- **A page renders BEFORE its query resolves.** Anything derived from `data?.x ?? []`
  runs on that first pass with an EMPTY list, so a `useMemo` that builds a keyed object
  from rows returns `{}` — and one dot-access into it (`mix.equity.toFixed()`) throws
  before a single pixel paints. That reads to the user as "the page doesn't load",
  not as an error, and no amount of testing the API will surface it. Guard every read
  out of a derived map, and OPEN THE PAGE after changing one (2026-09-01: /portfolio
  shipped in exactly this state).
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
- **The Analyze page (`/analyze`, 2026-07-28)** is the backtest-analytics workbench
  (design_handoff_analyze): a per-run **analytics bundle** (`services/run_analytics.py`,
  background job on the `analytics` slot of `replay_jobs` — never the replay slot; cached
  `~/.skas_data/analytics/run_<id>.v<V>.json.gz`, bump BUNDLE_VERSION on shape changes) feeds
  KPI tiles, the Stop/Target Simulator (margin-basis default + ratchet/below-peak trail —
  mirrors intraday_straddle._stop_level), the Conditioner Explorer, the Premium-Melt hero and
  6 drill-in sections + a Trade-ledger. INVARIANTS: full-run headline KPIs are COPIED from the
  run's report (Analyze == Run Detail to the rupee); melt/MAE/MFE come from
  `loss_study.reconstruct_path` over the 1-min store (intraday basis only — other bases fail
  closed with amber notices, never fake); the melt MTM overlay is SURVIVOR-tilted by
  construction (help text says so). STOCK runs keep `EquityTradeAnalysis`.
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
+ daily ~16:30 backup). The nightly also ships **off-box** (`SKAS_BACKUP_OFFBOX_DIR` — a Google
Drive for Desktop folder — and/or `SKAS_BACKUP_REMOTE_CMD`): Drive copies land **gzipped, written
straight to their final name** — NEVER stage a tmp+rename inside a Drive-synced folder (Drive
starts uploading the .tmp within seconds and the rename orphans it into lost_and_found — daily
complaint until 2026-07-30) — pruned per-series to `SKAS_BACKUP_OFFBOX_KEEP` (30). The nightly
latch is disk-derived on the first pass after boot (`last_backup_at`, stamp ≥16:30 today), so an
evening restart no longer re-ships ~270MB per boot. Tests must never touch real offbox
destinations — `tests/conftest.py` blanks both env keys (before that, every preflight copied
junk `s-*.db` snapshots into the owner's real Drive).

**NEVER build the UI on the VPS — and never tell the owner to.** The box is a ~911 MB
swapless Lightsail instance; `npm run build` (Vite + tsc) outgrew it, OOM-thrashes, and hangs
BEFORE the restart, which leaves the backend dead and the live loop down. `web*/dist` are
gitignored, so `git pull` never ships a bundle and never clobbers the one that is there.
Deploying is TWO steps, from two different machines (full runbook: `docs/DEPLOY.md`):
```bash
# 1. BACKEND — on the VPS, off-hours. stop → pull (incl. ../skas-data) → pip → start → health.
#    Always restarts on exit even if a step fails: a failed update must never leave the box dead.
~/git/skas-algo-platform/scripts/vps-update.sh
# 2. UI — on the MAC. Builds web/ (+ web-mobile/) and rsyncs dist up. No restart needed:
#    the backend serves web/dist live, so a UI change never interrupts the live loop.
scripts/deploy-web.sh <vps-ssh-host>     # then hard-refresh — the PWA SW caches the old bundle
```
A bare `git pull` + `systemctl restart` on the box updates the BACKEND ONLY and silently leaves
the old frontend serving — which reads as "my fix didn't deploy" (2026-08-24: instructions were
given to `npm run build` on the box, contradicting docs/DEPLOY.md).

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

**The session close is PER-SEGMENT since CAS (2026-08-12).** SEBI's Closing Auction Session
(effective 2026-08-03) extended index F&O past 15:30 while equity cash stayed put, so there is
no single close: `live/quotes.py::session_close(segment)` gives **15:40 for DERIV, 15:30 for
EQUITY**, both env-overridable (`SKAS_SESSION_CLOSE_DERIV` / `_EQUITY`, malformed → the literal
default, never a wider window). `is_market_open(now, *, segment="EQUITY")` defaults to the
NARROW window, so any caller that doesn't opt in keeps pre-CAS behaviour; DERIV callers pass
`live.config.segment`. **There is exactly ONE definition** — `LiveBroker._check_rails` used to
keep its own inline copy and that is precisely how it went stale: it refused every real order
after 15:30, so an exit or manual flatten between 15:30 and 15:40 raised `OrderExecutionError`
→ the run halted holding an open position. The rail picks its segment per ORDER
(`is_option_symbol`), because an option leg and an equity leg can share an account. If you add
another market-hours check, call `is_market_open` — do not re-derive the window.
The 15:40 is **measured, not quoted** (vendor pages disagree): our own 1-min store's last
minute-bar is 15:29 pre-CAS and 15:39 after. Consequence for equity, flagged not fixed: for
F&O-LISTED stocks the cash continuous session now ends **15:15** with an auction to ~15:35, so
an equity strategy deciding at 15:20 lands in the auction — no equity strategy is deployed
live today.

**Option intraday-bar store (the self-built GFD replacement, 2026-07):**
`data/option_intraday_store.py` → one **Parquet** file per trading day under
`~/.skas_data/option_intraday/1min/` (written/read via **duckdb** — the venv has NO pyarrow;
in-memory connections, no locking) with `symbol,start,open,high,low,close,volume,oi` (internal
option symbols, minute-START, sparse = traded minutes only). **Store filter window is
09:15–15:45** (`_SESSION_CLOSE`) — deliberately WIDER than the real session, which the store
itself measures at **15:40** for index F&O since the CAS extension 2026-08-03 (last minute-bar
`start` = 15:29 through 2026-07-31, 15:39 every day from 08-03; the old 15:30 cap silently
truncated four days at 15:29 before the owner caught it 2026-08-06 — tails re-fetched and
merged, expired-weekly tails lost). Keep the cap generous: nothing trades after 15:39, and an
over-tight bound is exactly what caused that truncation. The **live** session close is a
different, per-segment number — see below.
Filled two ways: (1)
`manager._maybe_daily_option_capture` — once/trading-day ≥16:00 IST (`SKAS_OPTION_BARS_*`,
**default OFF; enabled on ONE box only** — the Mac data box), one Kite `historical_data(...,
oi=True)` per in-universe contract (NIFTY/BANKNIFTY/SENSEX, expiries ≤40d, strikes ±10% of spot,
~0.35s throttle), read-only/arm-independent, + a `days_back=3` sweep for missed days; (2)
`skas-algo import-gfd <csv…>` — purchased GlobalDataFeeds 1-min files (13MB CSV → ~2MB parquet;
futures `NIFTY-I` rows skipped; existing captured rows win merges). **Footguns:** an expired
weekly VANISHES from the instruments dump — expiry-day bars are unrecoverable if the capture
misses that day (the sweep can't help); an all-errors day (dead historical subscription) writes
NO file so the sweep retries, but the day-latch still stops same-day hammering. The store keeps
ALL listed strikes (incl. NIFTY 50s) — the 100s rule is a TRADING rule, not a data rule. Readers:
`load_contract_bars(u, expiry, strike, right, d1, d2, minutes=1|5)` aggregates on the fly.
**Off-box backup:** `SKAS_OPTION_BARS_BACKUP_DIR` (a Google Drive for Desktop folder) →
`mirror_store` runs after every capture + `import-gfd` — COPY new/changed day-files only,
NEVER deletes (a local mistake can't propagate to the backup). Data-page panel:
`GET /data/options/intraday-store` → the Data → Options "Intraday 1-min bars" card
(freshness, per-day history, stale-⚠ badge). Coverage: `tests/test_option_intraday_store.py`,
`tests/test_gfd_import.py`.

**Supervision (optional but recommended for real-money):** `./scripts/install-supervisor.sh`
puts the backend under a launchd LaunchAgent (auto-start + auto-restart on any exit);
`uninstall-supervisor.sh` reverts. NOTE: while supervised, launchd re-spawns the backend
within ~15s of any kill — so a manual restart during dev means `launchctl kickstart -k
gui/$(id -u)/com.skas.algo`, not a bare `pkill` (which just triggers a respawn). A recovered
LIVE-order run keeps PaperBroker by default (recovery re-injects LiveBroker ONLY when
`live_resume_orders_on_recovery` is on — still behind the 4-key gate); a LiveBroker run —
fresh deploy or resumed — reconciles its broker book before its first decision
(`reconcile_pending`, ARCHITECTURE §3). **The demotion is surfaced (2026-07-17):** snapshot
`order_broker` ("live"/"paper") → a red "orders PAPER" chip on LIVE runs (web tile + mobile)
+ a WARNING alert on recovery — that morning a manual flatten filled on PAPER while the real
Zerodha book stayed open, with "live" on the tile. With the resume flag on but NO session at
restart (the daily VPS pattern: restart ~08:30, Kite login ~08:35) recovery can't inject —
the run is marked `resume_orders_pending` and the login promotion (`_maybe_resume_orders`)
finishes the injection through the same gate, reconcile-first. **Order-adapter rebind
(2026-07-24):** a LiveBroker freezes the adapter it was injected with, and every
`make_adapter` call mints a NEW adapter holding a COPY of the DB token — so when the daily
~06:00 Kite rollover + morning login rebuilt the quote source, READS rode the fresh token
while ORDERS kept the dead one (reconcile green all day, the 15:15 calendar exit rejected
"Incorrect api_key or access_token" → halt). Now every quote-source rebuild
(`promote_quote_source` / `_retry_quotes`) calls `_rebind_order_adapter` — repoints the
LiveBroker at the current quote adapter through the SAME armed+order-surface keys,
reconcile-first — and a 5-min maintenance sweep (`_rebind_order_sweep`) converges any run a
future path misses AND detects DB-token drift directly (`_maybe_remint_order_adapter`:
order-adapter token ≠ current DB token → re-mint both adapters — covers the WS-masked case
where the KiteTicker keeps serving marks so no quote_error ever fires and the quote adapter
itself stays stale). If you add a new place that swaps `live.quote_source`, call the rebind. Options snapshots also carry
`strategy_pnl` — the DECISION-entry-basis MTM the exit checks actually compare (live it sits
fill-slippage away from the book P&L; the UI shows both), and `exit_rules` now state each
check's sampling cadence ("checked every 15 min"); the deploy form defaults `profit_check`
to **1min** (constructor defaults unchanged — §1).

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
