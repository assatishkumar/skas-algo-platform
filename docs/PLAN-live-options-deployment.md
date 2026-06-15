# Plan: Live/paper deployment for options strategies + configurable intraday exit cadence

> skas-algo-platform, branch `feat/options-platform`. Deploy **HNI Weekly** and **Batman
> Ratio Monthly** for live forward-testing, and add per-exit-type check cadences (e.g.
> profit-book every 15 min, stop-loss at EOD 15:15).
>
> **Status: IMPLEMENTED + LIVE-VALIDATED (2026-06-15), PAPER.** All 5 stages built;
> **138 backend tests green**, tsc+build clean.
>
> **Live validation against the real logged-in Zerodha session (2026-06-15):**
> - NFO option-quote mapping resolves a real contract to a live LTP
>   (`NIFTY|2026-06-23|24000|CE` → ₹96); live index ₹23,622.
> - Entry-time gate confirmed live (08:38 IST → no entry; forced 09:50 → enters).
> - Full HNI entry off the LIVE spot: strikes 23800/24000/24200 (1-3-2, 65/195/130) on the
>   8-DTE 06-23 weekly, entry filled at LIVE premiums, net credit ₹1,186, no fake P&L.
>
> **Three live-correctness fixes made during validation:** (a) strike selection now uses
> the LIVE index spot (manager feeds the index LTP; settlement keeps the official cached
> close); (b) the live chain provider falls back to the most recent cached bhavcopy for
> strike/expiry listing (today's isn't cached intraday); (c) a contract's entry fills at
> its LIVE price via a lazy quote fetch (else it filled at a days-stale close → fake P&L).
>
> **Lot-sets** are now selectable in the deploy form (`lots` param; "Lot-sets (× 1-3-2)").
>
> Real-money LIVE order routing remains gated/out-of-scope.

## Goal
1. Make the live/paper engine run **options** strategies (today it's equity-only — the
   live market view has no option chain, no expiry settler, no charges/margin, so
   `ctx.option_chain()` is None and HNI/Batman never trade live).
2. Add **configurable per-exit-type cadence**: independently choose how often each exit is
   evaluated — e.g. **profit target every 15 min**, **stop-loss at EOD (15:15)**, time-exit
   at EOD. Entries fire at the strategy's spec time (HNI Mon 09:45, Batman last-Tue 15:16).

## Current state (verified)
- `engine/live.py LiveSession` + `engine/live_market.py LiveMarketView` are equity-only:
  the view exposes closes + Donchian levels but **no `chain`, no `current_date`/datetime,
  no `has_print`**; the session wires **no settler / charge / margin model**.
- `live/manager.py` `_loop` ticks every `refresh_seconds` and fires **one decision/day** at
  `decision_time` (15:20). `quotes.py`: `CacheQuoteSource` (EOD close, offline) and
  `ZerodhaQuoteSource` (live LTP). `recovery.py` rebuilds running runs on startup.
- `brokers/zerodha.py get_quote` maps only **`NSE:<symbol>`** (equities). Option contracts
  (`NIFTY|2026-01-13|25400|CE`) need NFO tradingsymbol mapping — not implemented.
- Backtest options stack (chain/lazy market/settler/charges/margin) already exists
  (`data/options_provider.build_options_run`) and is solid — we mirror it for live.

## Key constraint (drives the design)
Intraday option prices come **only** from a logged-in **Zerodha** session during market
hours. The skas-data cache is **EOD bhavcopy** — it cannot supply 15-min option marks.
So: **the 15-min cadence is real only with `quote_source=zerodha` + a live login**; in
`cache` mode it degrades to a single EOD evaluation. Backtest stays EOD (the cadence is a
live-only refinement; all existing backtests/tests are unchanged).

## Design

### 1 · `ctx.now()` — a real timestamp (small, shared)
`AlgoContext.now()` returns the current **datetime** (live: the market view's cursor; back-
test: the bar date at 15:30 IST). `ctx.today()` stays. Backtest collapses every cadence to
the daily bar, so EOD behaviour is byte-identical. Strategies read `ctx.now()` for cadence.

### 2 · Live options market view (`engine/live_options_market.py`)
A real-time analogue of the backtest `OptionMarketView`, satisfying the same protocol:
- Holds an **`OptionChainView`** (chain + spot providers). Live: chain/spot from the cache
  (last bhavcopy) for strike selection; contract **marks** from live quotes. Cache mode:
  marks = last cached close (EOD).
- `current_datetime` cursor; `today()` from it. `update_quotes({symbol: ltp})`,
  `close(symbol)` (live quote → last mark), `has_print`, `mark_prices`, `set_now(ts)`.
- Dynamic contract handling like the lazy backtest view (a contract is priced on first
  reference; equity-symbol fallback retained for the covered call later).

### 3 · Options-capable `LiveSession`
Thread an optional **chain / settler / charge_model / margin_model** into `LiveSession`
(all default None → equity path byte-identical). For a DERIV deployment the manager builds
the options market view + `ExpirySettler` + `ChargeModel` + `MarginModel` and the session
runs expiry settlement before each decision (mirrors `BacktestRunner`). Snapshot/watchlist
gain option-aware fields (legs, premium, margin).

### 4 · Manager: build the options stack + intraday ticking
- `LiveConfig` gains `instrument_class` ("STOCK"|"DERIV"), `underlying`, and the cadence
  config (below). `manager.start` for DERIV builds the options session (via a new
  `build_live_options_run` in `data/options_provider.py`, parallel to `build_options_run`).
- `_loop`: during market hours, `refresh()` every `refresh_seconds`, and call
  `run_decision(now)` **every tick** — the strategy's cadence filter (below) decides what
  actually fires. Entry-time + per-exit cadence gate the rest. (Equity runs keep the
  once-daily decision via a "eod"-only cadence default → unchanged.)

### 5 · Per-exit cadence (the headline feature)
Add to the ratio family + short-premium (options strategies) — params, with the user's
example as the default:
- `entry_time = "09:45"` (HNI) / `"15:16"` (Batman) — entries fire at/after this on the
  entry day; before it, skip.
- `profit_check = "15min"`, `stop_check = "eod"`, `time_check = "eod"` — each ∈
  `{"tick","1min","5min","15min","30min","60min","eod"}`. `eod` = at/after `eod_time`
  (default "15:15").
In `_manage`, evaluate each exit type only when its cadence is **due** at `ctx.now()`
(track last-eval timestamps per exit type in strategy state; "due" = a new boundary has
elapsed, or now ≥ eod_time for "eod"). Backtest: `ctx.now()` is the EOD bar → every cadence
is due once/day → unchanged. This keeps the exit logic in the strategy (self-contained) and
needs no new engine decision-phase concept.

### 6 · Zerodha option quotes + (gated) orders
- `ZerodhaAdapter`: map `UNDERLYING|EXPIRY|STRIKE|RIGHT` → NFO tradingsymbol +
  `NFO:` LTP keys; `get_quote` handles both equity and option symbols.
- Order placement for options stays **gated** behind the existing `armed` + master-switch;
  PAPER uses `PaperBroker` (sim fills on the live LTP). **Default deployment = PAPER**
  (forward-test) — see open decision #1.

### 7 · UI
`LivePage` start form: instrument class DERIV → underlying (NIFTY), strategy (HNI Weekly /
Batman), capital, quote source, and a **cadence block** (entry time, profit interval, stop
interval, time-exit, EOD time). Running-deployment cards show option legs + per-exit
cadence. `params.ts` labels.

### 8 · Recovery + tests
- `recovery.py` rebuilds DERIV runs (options stack + cadence) like equity runs.
- Tests: a scripted intraday harness driving a LiveSession with an options view —
  profit fires at a 15-min boundary, stop only at/after 15:15, entry only at/after the
  entry time; mode-equivalence (EOD replay == backtest) stays green; recovery round-trip.
  **End-to-end live (real Zerodha intraday) can't be auto-tested here** — verified manually
  by the user during a session (documented as the live acceptance step).

## Stages
1. `ctx.now()` + cadence-aware exits in the ratio/short-premium families (backtest-safe,
   fully unit-testable). ← the feature, testable offline.
2. Live options market view + options `LiveSession` + `build_live_options_run`.
3. Manager DERIV wiring + intraday loop + recovery.
4. Zerodha option quote/order mapping (needs a live session to validate).
5. UI + params + labels.

## Decisions (resolved 2026-06-14)
1. **PAPER forward-test** — sim fills on real Zerodha LTP; real-money LIVE routing stays
   gated (not wired now).
2. **Per-exit cadence intervals** — each exit ∈ `{tick,1,5,15,30,60min,eod}`; defaults
   profit=15min, stop=eod, time=eod, eod_time=15:15; entries at entry_time (HNI 09:45,
   Batman 15:16).
3. **Intraday needs a live Zerodha login** — accepted; cache mode degrades to one EOD eval.

## Out of scope (for now)
Real-money LIVE order routing beyond the existing gate (unless decision #1 says so) ·
intraday option *history* (no source → backtest stays EOD) · BANKNIFTY weeklies (uncached).
