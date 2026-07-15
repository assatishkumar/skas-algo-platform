# FEATURES — what skas-algo-platform does, in detail

A complete, plain-language catalog of everything the platform implements and how each piece
works. Companion to `docs/ARCHITECTURE.md` (the how-it-fits-together layer) and `CLAUDE.md`
(the how-to-work-here layer). Where this doc and the code disagree, the **code is truth**.

Contents:
1. What it is · 2. The one-engine principle · 3. Strategy catalog · 4. Backtesting ·
5. Live & paper deployment · 6. Real-order execution & safety · 7. Brokers & sessions ·
8. Market data & the WebSocket price feed · 9. Options analytics · 10. Research & calibration ·
11. Notifications · 12. Operations & safety infrastructure · 13. Web application ·
14. API reference · 15. Data & repo topology · 16. Configuration reference

---

## 1. What it is

A single-user platform to **research, backtest, forward-test (paper), and live-trade** Indian
equity and F&O strategies from one engine. It targets the NSE/BSE: NIFTY, BANKNIFTY, SENSEX,
the Nifty-50 universe, F&O lot sizes and monthly/weekly expiries, IST market hours, ₹/INR,
and Zerodha (Kite) / Dhan brokers. It models STCG tax and withdrawals. It is built for one
operator running real money — not a multi-tenant SaaS.

The system is a FastAPI backend (Python) hosting an in-process trading engine, a React/Vite
web UI, and a sibling market-data cache (`../skas-data`, DuckDB).

---

## 2. The one-engine principle (parity)

The founding rule: **backtest = forward-test = live**, one engine. A strategy is written once
and behaves identically in all three modes — only three things swap:
- the **Clock** (historical dates vs the real IST clock),
- the **DataFeed** (cached history vs live quotes),
- the **BrokerAdapter** (a simulated fill vs a real order).

The shared core is `engine/execution.py::SliceExecutor`, driven by `engine/runner.py` for
backtests and `engine/live.py::LiveSession` for paper/live. Strategies emit `Signal`s against
an `AlgoContext`; they never touch a broker, cash, or lot accounting — the engine owns all of
that. This is why developing a new strategy is safe: prove it in backtest and it runs the same
live. The invariant is guarded by golden tests (`tests/test_sst_parity.py`,
`test_sst_fifo_parity.py`, `test_mode_equivalence.py`) that must stay byte-identical.

---

## 3. Strategy catalog

Strategies register in `strategies/registry.py` (20 IDs across 15 files) and onboard there,
never by editing the engine. `intraday=True` means "decide every tick"; otherwise the run
decides once per day at a set time. "Backtest" notes whether a strategy runs the FULL shared
engine, a dedicated Black-Scholes service, or is deploy-only.

### 3.1 Equity strategies (long-only, positional)

- **`sst_lifo` — SST Donchian accumulator (LIFO exits).** Track a name when its close prints a
  20-day low; buy when it breaks the 20-day high; pyramid on repeats. Each lot exits
  *independently* once up ≥ `profit_target` (6%) from its own entry. Nifty-50 basket,
  once/day. FULL backtest. Params: `capital_parts`, `profit_target`, `max_lots`,
  `allocation_mode` (fixed vs equity-scaled/compounding).
- **`sst_fifo` — SST, pooled tiered exit.** Same entry; the *whole* position exits at an
  average-cost target that tightens with lot count (1→10% / 2→8% / 3+→6%). FULL backtest.
- **`sst_weekly` / `sst_weekly_fifo` — SST on a weekly timeframe.** Builds weekly closes from
  the daily stream; 20-week Donchian; wider targets (15% LIFO; 20/15/12% tiered) because
  weekly trends run further. FULL backtest.
- **`supertrend_momentum` — SuperTrend trend rider.** Buy on a SuperTrend green flip (optional
  pullback entry); at `profit_target` (5%) book part of the position (`partial_book_pct`, 50%)
  and let the rest ride to the red flip. Daily/weekly/monthly cadence. FULL backtest. Params:
  `timeframe`, `supertrend_period` (10), `supertrend_multiplier` (3), `entry_mode`,
  `pullback_pct`.
- **`nifty_shop` — dip-averaging "shop".** Rank the universe by how far each close sits below
  its N-DMA; buy the most-beaten-down not-held names (2/day), or average into the worst holder
  that fell > `avg_down_pct` (3%). Whole position sells at +`profit_target` (5%). Each buy is
  `allocation_pct` (4%) of *current equity* (built-in compounding). FULL backtest.
- **`custom_equity` — user single-stock trade (Trade UI).** Immediate or GTT-trigger entry;
  managed exit via `target_pct` / hard `stop_pct` / trailing (`trail_pct`). One-shot, CNC
  long-only, `intraday=True` (reacts to trigger/stop/trailing each tick). FULL backtest.

### 3.2 Options strategies with a FULL backtest (real cached chain)

- **`short_premium` — index premium seller.** Near `dte_target` (2), sell an ATM straddle or an
  OTM strangle (fixed step or `strangle_delta`≈0.20). Book at `profit_target_pct` (50% decay),
  stop at `stop_loss_pct` (50% rise), survivors settle at expiry; optional re-entry. NIFTY,
  fixed lots. FULL backtest.
- **`call_ratio_monthly` — 1:2 call-ratio spread + hedge (the ratio-family base).** BUY 1 near
  spot, SELL 2 further OTM, BUY 1 outer hedge (optional far tail) → **zero downside risk** (all
  calls), capped upside risk; the edge is harvesting the credit when NIFTY doesn't rip. Entry
  is **credit-gated** — the net credit must land in a band or the legs shift further OTM
  (`shift_step`×`max_shifts`), else the month is skipped. Exit: `profit_target_pct` (2.5%) /
  `stop_loss_pct` (3%) of the risk base / `max_holding_days` (20). NIFTY monthly, one entry/
  month, zero adjustments. FULL backtest. **Sizing** — `fixed` (default) or `margin`: refit
  `lots = ⌊equity × capital_utilization_pct ÷ model-margin-per-lot-set⌋` at each entry (model
  margin ≈2× broker, so util 95 ≈ ~50% broker margin — the live calibration knob).
- **`put_ratio_monthly` — downside mirror.** 1:2 put ratio + far put hedge → zero *upside* risk;
  same timing/gate/exits/sizing. FULL backtest.
- **`batman_ratio_monthly` — both wings ("Batman").** A 6-leg position: call-ratio above +
  put-ratio below, each hedged; both wings must qualify their credit gate or the month is
  skipped; combined target/stop/time on the 6-leg MTM. Profit zone is the whole band between
  the shorts (theta both sides); a half-size put tail hedge cushions gap-crashes. FULL backtest.
- **`hni_weekly` — NIFTY 1-3-2 net-zero call "tent".** BUY 1 @+200, SELL 3 @+400, BUY 2 @+600
  → net-zero contracts → limited risk both sides, so *not* credit-gated. Enter Monday into the
  next-Tuesday weekly, force-exit Friday — or the week's LAST trading day if Friday is an NSE
  holiday (strictly no weekend/holiday carry); ±1% of *deployed margin* as target/stop. FULL
  backtest (weekly expiries cached from 2025-09).
- **`staggered_covered_call` — covered-call wheel on an ETF.** SELL an OTM CE against an ETF
  accumulated in tranches (GOLD→GOLDBEES etc.); GTT tranche buys fire as spot falls; roll down
  when ≥80% of premium is captured; at expiry ITM → liquidate (called away) & restart, OTM →
  keep and sell next month. Optional put-selling wheel. FULL backtest.
- **`21_ema_momentum` — EMA(21) breakout credit spread.** A fresh daily close above the
  EMA(21)-of-highs → BULL PUT spread; below the EMA(21)-of-lows → BEAR CALL spread. 100-pt
  strikes, 300–500 wide, ₹80–140 credit (ideal 90–130; miss → skip and retry at 15:20). Hold
  to the opposite signal (close+reverse in one decision); roll 5 days pre-expiry. Self-gates to
  once/day at 15:20 (bands include today's forming bar). NIFTY, fixed lots. FULL backtest.
- **`custom_options` — user multi-leg position (Trade UI).** Enter the exact user legs for one
  expiry; exit on any of per-leg targets/stops, combined P&L `target_pct`/`stop_pct`, or spot
  bands; survivors settle at expiry. Builds symbols directly (so any listed contract, incl.
  stock F&O, can fill). `intraday=True`, one-shot. FULL backtest. **This is the pilot vehicle**
  for the first real order (1-lot single leg).

### 3.3 Options strategies — deploy-only or dedicated backtest

- **`call_put_ratio_expiry` — expiry-day 1:3 premium seller.** Only on each index's weekly
  expiry day (NIFTY Tue / SENSEX Thu), 09:20–09:27: BUY an ATM straddle, SELL 3 lots/side at
  the strikes trading nearest ⅓ of each ATM premium (live-chain lookup; >30% miss → skip the
  day). Exit ±1.1%/−1% of the **broker** basket margin, or 15:20; never carries (0DTE).
  Net short 2 lots/side beyond the ⅓ strikes → open-ended risk, the stop is the only guard.
  **Deploy-only, no backtest** (smile-driven strike selection needs the live chain; flat-vol BS
  would misplace the ⅓ strikes). Broker quote source required.
- **`weekly_intraday_straddle` — weekly-cycle intraday short straddle (NIFTY).** A cycle spans one
  weekly expiry; the ATM strike (nearest 100) is **locked once at 09:20 on the first trading day
  after the prior weekly expiry** and traded every day of the week on that fixed strike (a mid-cycle
  deploy force-starts at the current ATM). Each day, on the last **closed** 5-min bar of the combined
  premium: **SELL** the CE+PE when `x` (=CE.close+PE.close) is below **both** the prior day's intraday
  combined-premium **low** and the session **VWAP** (sum of per-leg volume-weighted VWAPs); **exit**
  when `x` closes back above VWAP, or at 15:25 (hard, never waits on margin); an optional MTM stop
  (% of broker margin) is **off by default** — a naked short straddle has uncapped tails. Up to 3
  entries/day; intraday only. This is the "short straddle with a VWAP stop" (video ref:
  https://www.youtube.com/watch?v=kYahbSjbubQ). It required a **new capability** — Kite intraday bars
  for an *option contract* with **volume** (`ZerodhaAdapter.option_intraday_bars`; the spot-only
  `intraday_bars` dropped volume) — for its VWAP and prior-day low. If those bars can't be fetched
  (no historical-data subscription / broker outage) the run shows an **amber data error** on the
  Live page and refuses **all** entries — even a forced one — until bars flow again (exits still
  run). **Deploy-only, broker source required, no backtest yet** (Global Financial Feeds
  intraday-option data will seed one later).
- **`delta_neutral_monthly` — 18Δ BANKNIFTY strangle → iron fly.** Enter 2 trading days after
  the prior monthly expiry ~11:00: SELL the ~18-delta PE and CE (delta solved from the live
  chain). When |CE−PE| > 40% of the combined premium, roll the *cheap* side to the strike whose
  LTP matches the rich side (capped at the other strike → straddle max); the straddle then buys
  long wings at **K ± (CE+PE premium)** — its two breakevens, snapped to the strike grid, same
  lots as the shorts → **iron fly**. Exit at 2.5% of the **broker** basket margin (re-frozen after
  each roll/hedge, so the iron fly targets 2.5% of its *reduced* margin; optional stop default off);
  recurring monthly. **Deploy-only, no backtest** (live-chain delta solve; only ~2 months of
  BANKNIFTY chain history cached). `force_entry` deploy flag skips the entry-day wait. An optional
  **post-iron-fly adjustment** (off by default here, on for `iron_fly_monthly`) is togglable on a
  running deploy — see below.
- **`iron_fly_monthly` — NIFTY / BANKNIFTY / SENSEX monthly iron fly + active repair.** Enters the iron fly directly
  (SELL ATM straddle, BUY wings at ATM ± (CE+PE premium)) on the same cadence. Its **adjustment**
  (default on): when spot breaches a breakeven (K ± net credit), SELL a naked ~15-20Δ short on the
  UNTESTED side and roll it (close at ≤10Δ or ≤¼ of its sold premium, re-sell); it exits ALL only
  when the expiry payoff can no longer be positive (max payoff < 0). The naked short adds an
  uncapped tail → an optional hard MTM stop is the backstop. Togglable live via
  `POST /live/{id}/ironfly-adjust` (persisted; survives restart). Deploy-only, broker source
  required, no backtest.
- **`momentum_theta_gainer_intra` — 15-min SuperTrend + pivot ATM seller.** Builds its OWN
  15-min candles from live spot ticks; on a closed candle, close > SuperTrend(7,3) AND > pivot
  R1 → SELL the ATM PUT of the nearest weekly (0DTE allowed); the mirror → SELL the ATM CALL.
  **Pivots are broker-sourced in live** (`ZerodhaAdapter.daily_bars`, fresh Kite daily — a stale
  cache can't corrupt them) with a stale-guard that gates entries + alerts rather than trade off an
  out-of-date prior day. Max 3 entries/day/underlying; exit on a SuperTrend flip (never re-enters on the flip candle)
  or 15:20. NIFTY + SENSEX. **Dedicated BS backtest** (`services/momentum_theta_bt` replays real
  15-min NIFTY bars through the actual strategy class; SENSEX is live-only — no BSE history).
- **`donchian_strangle_monthly` — Nifty-50 basket short-strangle (the active frontier).** For
  each screener-selected name, SELL a CE at last month's Donchian high and a PE at the low, plus
  a long OTM NIFTY hedge — governed as ONE portfolio: a combined portfolio stop/target (basis
  `margin` for new deploys), optional per-leg profit-taking, and a per-name **breach → roll**
  (spot crossing a short strike closes that name and sells a fresh ATM/30Δ short on the opposite
  side, up to `max_flips`). New deploys default to intraday `touch` breaches, one flip/name/day,
  max 3 flips. **Deploy-only** (from the live screener). Its **backtest sibling**
  `donchian_strangle_bt` re-enters expiry-anchored cycles from a schedule and prices stock
  options with **synthetic Black-Scholes** (σ = HV20 × `vol_multiplier`, calibrated on /research
  ~1.1; NIFTY uses the real chain), adding backtest-only VIX rules + notional-per-name sizing.

### 3.4 Shared strategy machinery

`base.py::Strategy` (the `on_slice(ctx) → [Signal]` protocol) · `_options_common.py`
(premium-sanity guard, nearest-strike snap, most-liquid monthly-expiry picker) ·
`call_ratio_monthly.py` (the ratio-family base: strike-mode resolution in points/percent/delta/
SD, the credit-gated leg-shift search, margin-auto sizing, intraday exit cadences, force-entry,
BS delta/IV helpers) · engine services: `black_scholes.py` (IV/delta), `contract_specs.py`
(lot sizes, expiry calendars), `margin.py` (short-option margin), `instrument.make` (builds the
`UNDERLYING|YYYY-MM-DD|STRIKE|RIGHT` symbol), `indicators/supertrend.py`.

---

## 4. Backtesting

- **Engine backtest** (`engine/runner.py` + `services/backtest.py`): replays cached daily data
  through the shared `SliceExecutor` — one slice per trading day. Options runs
  (`build_options_run`) attach the real cached option chain, a margin model, and an expiry
  settler; equity runs read closes. Produces a full report (below).
- **Dedicated intraday backtest** (`services/momentum_theta_bt.py`): the shared engine's slice
  is one day, which breaks true-intraday strategies; this replays real 15-min bars as
  o→h→l→c ticks through the actual strategy class (signal parity by construction), pricing
  premiums with Black-Scholes. Bars live in a csv.gz store (`data/intraday_bars.py`).
- **Report** (`engine/report.py`): total return, **CAGR**, **Sharpe**, **win rate**, max
  drawdown, an **equity curve** (with a 30-day sparkline on the Home page), monthly and yearly
  breakdowns, capital-utilization, and STCG-tax + withdrawal modeling. The Runs page shows CAGR
  (with total return as a sub-line).
- **Clone & template**: a finished run can be cloned into a new backtest form with its params
  prefilled.

---

## 5. Live & paper deployment

- **Deploy** a strategy from the UI (or API) with a mode: **PAPER** (simulated fills, real or
  cached quotes) or **LIVE** (real orders — gated, see §6). Options deploys pick a
  broker-backed quote source; equity/testing can use the cache.
- **The loop** (`live/manager.py`): each deployment runs its own async loop. Tick-driven
  strategies (options + intraday) decide every `refresh_seconds` (15–30s); plain equity decides
  once/day at `decision_time` (~15:20). Each tick pulls marks, re-prices unrealized P&L, and —
  only during market hours — runs the strategy's decision. Off-hours it re-prices slowly
  (read-only) and never trades. The whole tick runs on a dedicated worker pool so 20+ runs
  never starve the API.
- **Recovery** (`live/recovery.py`): the manager holds runs in memory; on restart it rebuilds
  every still-running deployment from its persisted `state` + `params_snapshot` and resumes.
  Kite tokens self-heal. (A recovered *live* run keeps paper fills unless
  `live_resume_orders_on_recovery` is on — §6.)
- **Seeding**: a paper run can be *warmed* from a past date — the engine replays history and
  carries the resulting open book + realized P&L forward as the live starting position.
- **Per-run controls** (Live page + API): **Pause** the decision loop (marks keep updating);
  **Run decision** now; **Force entry** (schedule-gated options strategies — enter on the next
  tick, bypassing entry-day/window gates, structural credit gates still apply); **Flatten**
  (exit-all at live prices); **Manual order** (close/open specific legs); **Acknowledge** an
  order-error/book-mismatch halt; **Reconnect** quotes; toggle **cache vs broker** quotes.
- **Monitoring**: live tiles show realized P&L, win rate, greeks; a payoff chart (with zoom)
  renders the expiry tent + current-value curve; a basket monitor page covers the multi-name
  Donchian deployment; a per-run analyze page shows round-trips and charts.

---

## 6. Real-order execution & safety

Real orders are the platform's most guarded path. **No real order has been placed yet** — even
LIVE mode fills via the simulated broker until every gate is deliberately turned on.

- **`LiveBroker` (`brokers/live_broker.py`) is the ONLY code that places real orders.** It
  satisfies the engine's `execute(BrokerOrder) → Fill` contract, so the entire shared decision
  path is untouched — a LIVE session gets a LiveBroker *injected* in place of the paper broker.
- **The 4-key injection gate** (`manager._maybe_inject_live_broker`): a LiveBroker is used only
  when **mode == LIVE** AND **`SKAS_LIVE_TRADING_ENABLED`** AND the **account is armed** AND the
  adapter exposes the full order surface. Any other combination keeps simulated fills. Matrix-
  tested.
- **Execution style**: LIMIT at the touch (SELL@bid / BUY@ask) → poll ~2s → after
  `live_order_timeout_s` (10s) escalate to MARKET → poll to terminal. Partial fills ≥1 unit are
  booked at the actual quantity.
- **Pre-flight rails**: market-open + **holiday** check, per-order notional cap
  (`live_max_order_notional`, ₹5L), per-run daily order cap (`live_max_orders_per_day`, 20), and
  an account-level rate governor shared across runs (paces simultaneous entries).
- **Halts & acknowledgement**: a rejected/unfillable order, or the hourly book-reconciliation
  finding a mismatch, sets the run's `order_error` → all decisions HALT until the owner
  acknowledges (a UI banner + tile chip + `POST /live/{id}/ack-order-error`). Telegram alerts
  fire on halt.
- **Reconciliation**: compares the broker's NET book against the AGGREGATE of all live-order
  runs on the account (the broker nets per contract across runs). Manual trades in the same
  account trip it — so dedicate an account to the platform.
- **Reconcile-before-first-decision** (the double-fill safety net): a run that gets a LiveBroker
  starts `reconcile_pending` and will not decide until it has verified its broker book — so a
  fill a crash left unpersisted is detected and halts instead of being re-traded.
- **Recovery behavior**: by default a restart makes a live run fall back to paper fills (it
  pauses real orders until re-activation). `live_resume_orders_on_recovery=true` makes recovery
  resume real-order management — still behind the 4-key gate, and it reconciles before its first
  decision.
- **Owner directive**: the operator places every real order by hand; the platform never arms an
  account, sets the flag, or initiates a real order on its own (`CLAUDE.md` §1).

---

## 7. Brokers & sessions

- **Two brokers** (`BrokerAccount.broker` ∈ {zerodha, dhan}; `services/broker.make_adapter`
  dispatches). Credentials (`api_secret`, `session_token`) are Fernet-encrypted at rest.
- **Zerodha (Kite)**: the primary. You enter Kite Connect app credentials, click Login to open
  Kite's site, sign in, and paste the `request_token` from the redirect — the platform exchanges
  it for the daily access token (valid until ~06:00 IST next day; a restart doesn't lose a still-
  valid session). Provides quotes, chains, margins, historical bars, and (Phase B) real orders.
- **Dhan**: no api key/secret — you paste a portal-generated JWT (its `exp` claim is the session
  expiry); instruments resolve from the public scrip-master CSV. Read-only today (no order
  methods); live quotes/chains need Dhan's paid "Data APIs" subscription. The 50-name screeners
  and cache refresh stay on Zerodha.
- **Arming**: an account is armed/disarmed from the Brokers page. Arming is a prerequisite for
  real orders (one of the 4 keys) and is always the owner's action.
- **Session handling** (`redesign.tsx::SessionBanner`): screeners/deploys that need a live
  session distinguish "backend down" from "no session" and disable actions with a reason.

---

## 8. Market data & the WebSocket price feed

- **Cache** (`../skas-data`, DuckDB): all historical daily bars and the cached option chain
  (bhavcopy) come from here; it also backs offline/degraded marks (`CacheQuoteSource`).
- **Live quotes**: every run reads marks through the `QuoteSource` contract — so strategies are
  quote-source-agnostic. `ZerodhaQuoteSource` batches N symbols into one `kite.ltp()` call.
- **WebSocket price feed** (`live/pricefeed.py`): one KiteTicker WebSocket per account streams
  LTPs into a shared last-tick cache; every run on that account reads the cache instantly
  (`FeedQuoteSource`), cutting steady-state REST calls from ~80–240/min (20 runs) to near-zero.
  Every read is staleness-checked, so a dead socket / token refresh / un-ticked symbol
  transparently falls back to batched REST — the feed can only make marks faster, never wrong or
  missing. Prices *push*; decisions stay loop-driven (no raw tick callbacks into strategies —
  that would break parity). Gated by `SKAS_WS_FEED_ENABLED` (default on) + a zerodha account.
- **Option chains** stay REST + a 15–20s TTL cache (LTP-mode WS carries no depth/OI; chains are
  selection-time, not per-tick). Broker basket margins refresh ~1/min per run.
- **Intraday bars store** (`data/intraday_bars.py`, `~/.skas_data/intraday/`): Kite-fetched
  15-min bars in csv.gz, used to warm up and backtest the intraday strategies.
- **Option intraday-bar store — the self-built GFD replacement**
  (`data/option_intraday_store.py`, `~/.skas_data/option_intraday/1min/`): the platform builds
  its OWN 1-minute option dataset. Every trading day after close (~15:45), a background task
  fetches each in-universe contract's full-day 1-min bars **with volume and open interest**
  from Kite historical (NIFTY + BANKNIFTY + SENSEX; expiries within ~40 days; strikes within
  ±10% of spot — all configurable via `SKAS_OPTION_BARS_*`, default off) and writes one
  Parquet file per day (~1-2 MB, via DuckDB). Purchased GlobalDataFeeds 1-min CSVs import
  into the same store with `skas-algo import-gfd`, so bought history and self-captured days
  form one continuous dataset. Readers serve any timeframe (5-min etc.) by aggregation —
  this is the future backtest feed for the intraday options strategies (weekly straddle).
  One hard rule: capture must run ON the trading day — an expired weekly's contracts vanish
  from the broker's instrument list, so a missed expiry day cannot be backfilled. The store
  auto-mirrors into a Google Drive folder after every capture/import (`SKAS_OPTION_BARS_BACKUP_DIR`;
  copy-only, never deletes), and the Data → Options page shows the store's freshness + per-day
  history (rows, contracts per underlying, bar window, size) with a stale-capture warning badge.
- **Contract specs** (`contract_specs.py`): F&O lot sizes (a flat 2026-07 Kite snapshot for
  stocks), monthly/weekly expiry calendars, index-vs-exchange routing (SENSEX→BSE/BFO).

---

## 9. Options analytics

- **Black-Scholes** (`engine/options/black_scholes.py`): implied vol (from an LTP), delta, and
  pricing — used for delta-targeted strike selection, greeks, and synthetic premiums.
- **Greeks**: per-leg IV/delta and net position delta are computed from live quotes each tick
  and shown on live tiles + recorded to a `greeks_snapshot` history.
- **Margin models** (`engine/options/margin.py`): a deterministic model margin (span+exposure)
  for sizing/backtests, and the **broker basket margin** (Zerodha `basket_order_margins`) for
  live risk. New option strategies freeze their target/stop base from the *broker* margin.
- **Expiry settlement** (`engine/options/settlement.py`): at/after expiry, held option lots are
  cash-settled to intrinsic value vs the underlying's settlement price. Live runs settle only
  at/after 15:30 on expiry day (0DTE legs live until then) and price intrinsic off the *live*
  index spot; the backtest keeps its daily-slice semantics byte-identically.
- **Payoff chart** (`web`, `lib/payoff.ts`): the expiry P&L "tent" (green/red split at zero) +
  a current-value (T+0) curve, spot and strike markers, and a zoom control (auto → ±8/5/3/1.5%).
- **Symbol formatting** (`lib/symbol.ts`): renders `UNDERLYING|YYYY-MM-DD|STRIKE|RIGHT` as
  `NIFTY 24500 CE · 7 Jul '26` (never the raw pipe form).

---

## 10. Research & calibration (the /research page)

- **Donchian breakout study** (`services/donchian_study.py`): cache-only daily bars,
  expiry-anchored cycles, channel breakout/re-entry/whipsaw stats and a live-rule flip
  simulation — the analysis behind the Donchian basket strategy.
- **Black-Scholes vs live calibration** (`services/bs_calibration.py`): a session-gated,
  strictly read-only panel comparing synthetic BS premiums to live premiums, used to calibrate
  the stock-option `vol_multiplier` (~1.1) that the Donchian backtest relies on.
- **Momentum-theta backtest panel**: runs the dedicated 15-min BS replay and shows the
  EOD-vs-flip-buyback P&L split.
- **Donchian backtest schedule builder** (`services/donchian_bt.py`): the "backtest screener"
  that injects the expiry-anchored cycle schedule the backtest sibling replays.
- **Fibonacci-retracement screener** (`services/fibret.py`): a cache-backed study/screener.

---

## 11. Notifications

Telegram alerts (`notify/`): configure `SKAS_TELEGRAM_BOT_TOKEN` + `SKAS_TELEGRAM_CHAT_ID`.
Alerts fire on broker connect/session events, order fills/rejections, order-error/book-mismatch
halts, and the loop watchdog restarting a dead run. Sends are fire-and-forget (a slow Telegram
call can never stall a request or hold a DB transaction). Tests never page the owner (creds are
blanked in the test bootstrap).

---

## 12. Operations & safety infrastructure

- **Authentication**: an opt-in single-operator login — a password (bcrypt hash in
  `SKAS_AUTH_PASSWORD_HASH`) exchanged at `/auth/login` for a signed JWT bearer token that every
  API route + the WebSocket require; the web app has a login page + logout, stores the token,
  and redirects to `/login` on a 401. **Fail-open**: enforced only when both the hash and
  `SKAS_AUTH_JWT_SECRET` are set — off on localhost, required on a networked host. Generate the
  hash with `skas-algo hash-password`. The same token scheme covers the future iOS app.
- **Localhost bind**: the API binds `127.0.0.1` by default (a single-user, real-money
  API must not be on the LAN; the container path sets `0.0.0.0` behind its own isolation).
- **NSE holiday calendar** (`live/holidays.py`): holidays close the market like weekends (no
  decisions/orders; marks re-price read-only). Festival dates are provisional and env-correctable
  (`NSE_HOLIDAYS_ADD` / `NSE_HOLIDAYS_REMOVE`).
- **DB backups** (`services/backup.py`): crash-consistent `VACUUM INTO` snapshots to `backups/`
  (retain 7) — one on every startup (pre-recovery) + one daily ~16:30 IST. Set
  `SKAS_BACKUP_REMOTE_CMD` (an rsync/rclone/`aws s3` command with `{path}` = the snapshot) and
  the nightly backup also ships it off-box for disk-failure protection (best-effort; alerts on
  failure). Unset → on-box only.
- **Loop watchdog**: a 5-min maintenance task restarts any auto run whose loop died silently and
  Telegram-alerts it.
- **Daily cache refresh**: the same task refreshes the index + running-equity daily cache once per
  trading day, in the background, as soon as a valid Zerodha session exists (historical/read-only —
  never orders/arming). So live keeps working without a manual Data-page refresh: the index
  strategies (momentum_theta pivots, 21-EMA bands) read daily bars broker-first anyway, and the
  cache-backed equity strategies stay fresh via this task. A quiet **"Data ✓ HH:MM"** chip in the
  web header shows the last refresh (from `GET /live/summary` + a `cache_refreshed` WS event).
- **Process supervision** (`scripts/install-supervisor.sh`): a launchd LaunchAgent auto-starts
  the backend at login and auto-restarts it within 15s of any exit (single-process, reload off);
  `uninstall-supervisor.sh` reverts.
- **Preflight gate** (`scripts/preflight.sh`): ruff (advisory) + the full test suite incl. the
  parity/mode-equivalence golden tests + web typecheck — run before any restart/deploy; green
  means the engine and live paths are unchanged.
- **SQLite durability**: WAL mode + a busy timeout so many writer threads coexist without
  "database is locked". Postgres is available for scale (docker-compose path).

---

## 13. Web application (`web/`)

React + React Router + React Query + Recharts + Tailwind. Desktop top-nav (Home / Backtest /
Trade / Live / Docs / Research / Data / Brokers) and a mobile PWA bottom tab bar; light/dark
theme. All data goes through `api/client.ts` (`/api/v1`) plus a live WebSocket feed.

- **Home** (`/`) — dashboard: a paper-equity sparkline, headline KPI tiles (win rate, Sharpe,
  equity), and a workspace grid. Metrics are real, from active paper deployments.
- **Backtest** (`/backtest`) — a shell with two tabs:
  - **Runs** — a per-strategy leaderboard of saved backtests ranked by CAGR (total return as a
    sub-line); Open, Forward-test (→ deploy prefill), Clone, Archive/Delete, Compare-top-3.
  - **New backtest** — the master config form with a **param builder per strategy** (all equity
    + backtestable options strategies), an exit-override builder, and a **parameter sweep** that
    batches variations into the compare view. Renders the universal report.
- **Trade** (`/trade`) — **Builders** (Option, Equity, Momentum-Theta, CP-Ratio-Expiry,
  Delta-Neutral — each constructs and deploys a position) and **Screeners**:
  - **FibRet** — a Fibonacci-retracement single-stock option-selling screener (IVP CSV upload,
    live-chain strike picker + margin, deploy as `custom_options`).
  - **Donchian strangle** — the Nifty-50 basket short-strangle screener: per-row strike
    overrides, a portfolio panel (notional/premium/margin/stop + notional-matched NIFTY hedge),
    and one-click deploy of the basket + hedge.
- **Live** (`/live`) — the live/paper dashboard over the WebSocket feed: a hero KPI strip, an
  Active/Stopped/Archived filter, tiles grouped by category → strategy, and expandable run
  cards with every control (Pause/Resume, Refresh, Run-decision, **Force-entry**, Intervene
  (override / manual-order), Exit-all, **Go LIVE**, **Acknowledge** an order halt,
  Reconnect-quotes, quote-source switch, Stop/Archive/Delete). Panels: option metrics
  (Sensibull-style max P/L, breakevens, POP, target/stop), the live payoff chart, greeks with
  history, and executed-trades tables.
- **Basket monitor** (`/live/:id`) — the rich Donchian deployment view: hero KPIs, an aggregate
  expiry payoff, the index hedge, a portfolio-stop gauge, and per-name cards → a drawer with
  each leg's ITM/OTM/flip state, the flip timeline, and a per-name payoff.
- **Deploy** (`/live/new`) — the generic deploy / forward-test form (equity vs options branches,
  quote source + account, auto-loop, optional warm-from-date seed).
- **Docs** (`/docs`) — static, searchable reference cards for ~19 strategies (structure / entry /
  exit / risk + a deploy CTA). No API calls.
- **Analyze** (`/analyze?run=`) — the trade analyzer for any run; options runs reconstruct
  cycles with per-leg detail, equity runs show per-stock candlesticks + round-trip markers.
- **Run detail** (`/runs/:id`) — the full backtest report + lifecycle (Analyze, Set-template,
  Clone, Forward-test, Delete).
- **Compare** (`/compare?ids=`) — 2–5 runs: a rebased growth chart + benchmark, options cycle
  comparison, a metrics table, and a parameter diff.
- **Research** (`/research`) — the three research tools (§10).
- **Data** (`/data`) — the cache manager (stocks / options / futures): coverage, chunked
  refresh, per-symbol charts, and an option-chain / futures viewer.
- **Brokers** (`/brokers`) — connect / list / login / arm broker accounts, cache + GOLD refresh,
  with the two-key live-order gate banner and arm/disarm danger treatment.

## 14. API reference (`/api/v1`)

Grouped by router. (M = state-changing.)

**Health** — `GET /health` (liveness + DB check).

**Backtest & runs** (`backtest.py`) — `GET /strategies` (backtestable IDs), `GET /benchmarks`,
`GET /universes` + `/{name}/symbols`; **M** `POST /backtest` (preview), **M** `POST
/backtest/save`; `GET /runs`, `GET /runs/compare?ids=`, `GET /runs/{id}`, `GET /runs/{id}/analysis`,
`GET /runs/{id}/trades.csv`, `GET /runs/{id}/benchmark?index=`, `GET /analysis/runs`; **M** `PATCH
/runs/{id}`, `POST /runs/{id}/archive|unarchive`, `DELETE /runs/{id}`; strategy templates (get /
set-from-run / delete).

**Brokers** (`/brokers`) — `GET ""`, **M** `POST ""` (connect), **M** `DELETE /{id}`; `GET
/{id}/login-url`, **M** `POST /{id}/login` (exchange request_token; then background-promote
degraded runs); **M** `POST /{id}/refresh-cache`, `POST /{id}/refresh-gold`; **M** `POST
/{id}/arm|disarm`.

**Data** (`/data`, mostly read-only) — `GET /summary`, `/coverage`, `/symbols` + `/{symbol}`,
`/stocks/{symbol}/series`; cached options `GET /options/underlyings|/{u}/coverage|/{u}/expiries|
/{u}/chain`; **live** options `GET /options/live/...` (session needed); **M** `POST
/options/refresh` (bhavcopy); futures `GET /futures/...` + **M** `POST /futures/refresh`.

**Live** (`/live`) — **M** `POST /start`; `GET ""`, `GET /deployments?status=`, `GET /summary`
(Home aggregates), `GET /{id}`, `GET /{id}/watchlist`, `GET /{id}/greeks-history`, `GET
/{id}/trades`; **M** control endpoints: `POST /{id}/quote-source`, `/reconnect-quotes`,
`/refresh`, `/run-decision` (blocked while reconcile-pending), `/controls`, `/flatten`,
`/manual-order`, `/overrides`, `/stop`, `/activate`, **`/go-live`** (armed + flag + session
required), **`/force-entry`**, **`/ack-order-error`**, `/archive|unarchive`, `PATCH /{id}`,
`DELETE /{id}`; `WS /ws` (snapshot/trade broadcast).

**Research** (`/research`, read-only) — **M** `POST /donchian-study`, `POST /momentum-theta-bt`,
`POST /bs-calibration`.

**Trade** (`/trade`) — **M** `POST /options/margin` (basket margin preview); deploys that reuse
the shared deployment path: **M** `POST /options/deploy` (custom_options), `/options/fibret/analyze`,
`/options/donchian/analyze|portfolio|deploy`, `/options/delta-neutral/deploy`,
`/options/cp-ratio-expiry/deploy`, `/options/momentum-theta/deploy`, `/equity/deploy`.

---

## 15. Data & repo topology

- **`skas-algo-platform`** (this repo): the engine, strategies, API, and web UI.
- **`../skas-data`** (installed editable): the market-data cache (DuckDB + Kite) — all daily
  bars and cached chains. Opened read-write by one process at a time (parity tests need
  exclusive access — see the preflight caveat). **Backtest-focused + a live fallback**: LIVE runs
  now source daily/historical data broker-first (fresh Kite `daily_bars`), so a stale cache no
  longer affects live; the daily cache-refresh task keeps it current for the cache-backed equity
  strategies.
- **`../skas-trading`**: original strategy reference (SST etc.).
- **`../skas-options`**: old options code, explicitly not reused.
- **`skas_algo.db`** (~200 MB, gitignored): all platform state — accounts, deployments, per-run
  persisted state, and the Order/Fill audit trail. The only copy of live state; `backups/`
  holds rolling snapshots.

---

## 16. Configuration reference (env vars, `SKAS_` prefix; `.env` at the repo root)

| Setting | Default | Purpose |
|---|---|---|
| `SKAS_API_HOST` | `127.0.0.1` | API bind address (localhost by default; container sets `0.0.0.0`). |
| `SKAS_DATABASE_URL` | `sqlite:///./skas_algo.db` | Platform DB (relative → start from repo root). |
| `SKAS_SECRET_ENCRYPTION_KEY` | — | Fernet key for encrypting broker credentials. |
| `SKAS_AUTH_PASSWORD_HASH` | — | bcrypt hash of the operator password (`skas-algo hash-password`). |
| `SKAS_AUTH_JWT_SECRET` | — | HS256 signing key for login tokens. Auth is on only when both this + the hash are set. |
| `SKAS_AUTH_TOKEN_TTL_HOURS` | `24` | Login token lifetime. |
| `SKAS_DEBUG` | `true` | Dev auto-reload; the supervisor sets `false` (single process). |
| `SKAS_LIVE_TRADING_ENABLED` | `false` | Master switch for real orders (one of the 4 keys). |
| `SKAS_LIVE_MAX_ORDER_NOTIONAL` | `500000` | Per-order notional cap (rail). |
| `SKAS_LIVE_MAX_ORDERS_PER_DAY` | `20` | Per-run daily order cap (rail). |
| `SKAS_LIVE_ORDER_TIMEOUT_S` | `10` | LIMIT→MARKET escalation timeout. |
| `SKAS_LIVE_RESUME_ORDERS_ON_RECOVERY` | `false` | Resume real orders after a restart (else paper). |
| `SKAS_WS_FEED_ENABLED` | `true` | Use the KiteTicker WebSocket price feed (REST fallback). |
| `SKAS_WS_FEED_STALE_S` | `10` | In-market staleness before a mark falls back to REST. |
| `SKAS_DB_BACKUP_KEEP` | `7` | Rolling on-box DB snapshots to retain. |
| `SKAS_BACKUP_REMOTE_CMD` | — | Command the nightly backup runs to ship the snapshot off-box (`{path}`/`{name}`). |
| `SKAS_TELEGRAM_BOT_TOKEN` / `_CHAT_ID` | — | Telegram alerts (unset → alerts are no-ops). |
| `NSE_HOLIDAYS_ADD` / `NSE_HOLIDAYS_REMOVE` | — | Correct the holiday calendar without a redeploy. |
