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

Strategies register in `strategies/registry.py` (33 IDs across 30 files) and onboard there,
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

- **`gap_reversal` — gap-up oversold reversal (long-only).** A rare, high-conviction daily
  setup: the stock GAPS UP over the previous close (min-gap % knob), still closes above its
  21 EMA, and the RSI(10) **of the EMA series** is under 10 — through a downtrend the
  smoothed RSI pins near 0, so the first gap-up recovery that retakes the EMA is the entry
  (rsi_source knob: ema = chart spec / close = classic). Hold until the close falls back
  below the EMA. Capital-parts sizing (fixed or equity-scaled), Nifty 500. FULL backtest via
  the new generic indicator precompute (the first equity-engine consumer of the day's OPEN
  price); live deploys fail closed until the live indicator seeding lands. The spec's short
  leg (gap-down + RSI>90) awaits a sell-CE options variant — cash equity can't hold
  overnight shorts.

- **`value_investing` — a daily rupee drip into a watchlist, funded by an ETF.** The platform's
  only pure **accumulation** system: it buys and never sells. Each trading day it sorts the
  watchlist by **today's change %, biggest faller first**, and walks that list from the top buying
  **one share of each** while the configured **daily budget** lasts — a name the remaining budget
  can't afford is skipped and the walk continues to cheaper ones below, there is no wrap-around,
  and whatever is left over evaporates so the drip stays a predictable size. The cash comes from
  selling exactly enough of a **fund source ETF** (LIQUIDCASE / GOLDBEES / LIQUIDBEES) in the same
  decision — idle cash is spent first, since selling an asset while cash sits idle would be
  strictly worse. Funding is **all-or-nothing**: if the ETF can't cover the day's list, it buys
  **nothing** and raises the alert rather than half-filling the day or quietly falling back to
  cash. It warns (Live banner + Telegram/bell, once a day) when the fund source is down to
  ~`warn_days_left` of runway. An all-green day still buys — the least-up name tops the list; the
  drip is the point. The **watchlist is a hot-editable parameter** (the run's symbol list is
  edit-blocklisted, so the list of names to *buy* lives in the strategy and can be changed on a
  running deploy from the tile's Edit params); a name outside the run's symbol list can't be
  priced and is named in the alert. FULL backtest, and — unusually for an equity strategy —
  **live-capable from day one**: ranking by daily change reads the new `ctx.prev_close`, which both
  market views answer natively, instead of the `indicators=` precompute that leaves `gap_reversal`
  and `happy_twins` failing closed live. **Two things to know before deploying:** it needs a
  **broker quote source** (on the cache source the "live" price *is* yesterday's close, so every
  change reads 0.00% — the strategy detects that and refuses the day rather than ranking noise);
  and read the backtest's **equity curve**, not its trade table, because the only SELLs in the log
  are ETF funding sales, so "win rate" and "net realized P&L" describe a cash sweep.
- **`happy_twins` — weekly dual-SuperTrend trend follower (long-only).** One indicator family,
  two speeds: **ENTER** when the FAST SuperTrend (ATR 2, factor 1, weekly) *turns* green — a
  transition, not a state, so a freshly deployed/recovered run never chases an old signal — and
  **EXIT** while the SLOW SuperTrend (ATR 3, factor 1) is red — a state, so a flip missed across
  a restart still closes on the next bar. Fast in, slow out: the trigger gets you in early while
  the exit stops the whipsaw a single fast ST inflicts. Three overlays, each off by default:
  **`park_symbol`** parks idle capital in an ETF (e.g. GOLDBEES) whenever every trading name is
  flat and unparks in the same slice as the entry; **`capital_parts`** switches to portfolio mode
  (equity split into parts, one part per green flip, at most N names held — when more names flip
  than there are free parts the STRONGEST win, ranked by close vs the 20-day mean);
  **`regime_symbol`** is the exposure brake — while that index's own slow SuperTrend is red no
  NEW part is deployed, so a bad regime bleeds the book out through its own exits instead of
  liquidating it. Rides the generic indicator precompute (weekly resampling, flips visible the
  next trading day, no lookahead). FULL backtest; LIVE deploys **fail closed** until live
  indicator seeding lands (same posture as `gap_reversal`).

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
  **2026-07 additions (whole ratio family incl. hni/batman/put):** an optional **trailing stop**
  — `trail_trigger_pct`/`trail_step_pct`/`trail_mode` (`ratchet` lifts the stop one step per
  trigger-of-peak-profit; `below_peak` trails peak − step), off by default, sits ABOVE the fixed
  stop; `pnl_basis="total"` (form default) measures target/stop/trail on realized+unrealized;
  an **entry vol-premium filter** (`vol_premium_min`: skip entries when ATM-IV − HV is thin —
  the /research loss-study's one OOS-robust gate, off by default); **manual Build entry** (Trade
  → Build deploys the exact legs into the native strategy with its native exits); and the
  positional family **replays on the 1-min intraday store** (data basis `Intraday`).
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

### 3.3 Options strategies — deploy-only, 1-min-store replay, or a dedicated backtest

These don't ride the shared EOD engine (its slice is one trading day, which can't model an
intraday stop or a strike picked off the live chain). Most are now backtested by replaying
the self-captured 1-min option store through the same class the deploy runs (§4); the rest
are validated paper-first.

- **`call_put_ratio_expiry` — expiry-day 1:3 premium seller.** Only on each index's weekly
  expiry day (NIFTY Tue / SENSEX Thu), 09:20–09:27: BUY an ATM straddle, SELL 3 lots/side at
  the strikes trading nearest ⅓ of each ATM premium (live-chain lookup; >30% miss → skip the
  day). Exit ±1.1%/−1% of the **broker** basket margin, or 15:20; never carries (0DTE).
  Net short 2 lots/side beyond the ⅓ strikes → open-ended risk, the stop is the only guard.
  Deploy-only for trading (broker quote source required) — there is no EOD backtest, because
  flat-vol Black-Scholes would misplace the smile-driven ⅓ strikes; the **1-min store replay**
  backtests it on real captured premiums instead.
- **`intraday_straddle` — daily intraday short straddle (NIFTY / BANKNIFTY).** SELL the ATM CE+PE
  on the nearest weekly at `entry_time` (09:18; 0DTE on expiry day) and exit at `exit_time` —
  **15:20** for new deploys/backtests, because Zerodha auto-squares intraday F&O at 15:26 and a
  15:25 exit leaves no room to retry a rejected leg (the 2026-08-11 failure). `strike_delta`
  (e.g. 0.6) sells slightly-ITM legs picked by per-share BS delta instead of the ATM. Risk is
  two stops off the **frozen broker margin**: a fixed `stop_loss_pct` (2%) and a trailing stop
  that only ever ratchets UP (`ratchet` — each +`trail_trigger_pct` of PEAK profit lifts the stop
  a step; `below_peak` — peak − step); there is no fixed profit target, the trail is the upside.
  `leg_book_pct` books a **melted leg on its own** once it has given up that % of its entry
  premium (the remaining reward is pennies against its gamma); the banked profit stays in the
  day's P&L so the stop/trail keep guarding the whole day, not just the surviving leg. One entry
  per day (a stopped-out day does not re-enter); the time exit never waits on margin. Deploy-only
  + broker source (the live chain picks the strike); **backtested on the 1-min store**.
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
  run). Deploy-only for trading, broker source required; **backtested on the 1-min store**
  (the purchased GFD history + the platform's own daily capture is exactly what seeded it).
- **`delta_neutral_monthly` — 18Δ BANKNIFTY strangle → iron fly.** Enter 2 trading days after
  the prior monthly expiry ~11:00: SELL the ~18-delta PE and CE (delta solved from the live
  chain). When |CE−PE| > 40% of the combined premium, roll the *cheap* side to the strike whose
  LTP matches the rich side (capped at the other strike → straddle max); the straddle then buys
  long wings at **K ± (CE+PE premium)** — its two breakevens, snapped to the strike grid, same
  lots as the shorts → **iron fly**. Exit at 2.5% of the **entry** broker margin by default — the
  two-margin scheme (2026-07-27): thresholds freeze ONCE at cycle entry (absolute ₹ from day
  one; an adjustment's margin jump never moves them) while the dynamic margin keeps feeding
  margin-used/sizing/reports; `exit_margin_basis="current"` restores the historical
  re-freeze-per-change (iron fly targets 2.5% of its *reduced* margin; optional stop default off);
  recurring monthly. Deploy-only for trading (the delta solve needs a live chain) with a
  **1-min store replay** for backtesting — but the form warns that a full monthly cycle needs
  ~2 months of captured BANKNIFTY chain. `force_entry` deploy flag skips the entry-day wait.
  **Exit/adjust controls (2026-07, from run #233):** adjustments sample on their own cadence
  `adjust_check` (form default 5min; unset → rides `profit_check`) and never fire in the first
  `adjust_after_open_min` minutes after 09:15 (form default 5 — deep-OTM wing strikes don't
  print at the open; iron-fly wings also resolve to the nearest strike with a real premium, so
  a phantom ₹0 hedge can never enter the book); optional **trailing stop** (ratchet /
  below-peak, % of frozen margin); `pnl_basis="total"` (form default) folds roll-banked
  realized P&L into the target/stop/trail measure. Manual Build entry supported. An optional
  **post-iron-fly adjustment** (off by default here, on for `iron_fly_monthly`) is togglable on a
  running deploy — see below.
- **`iron_fly_monthly` — NIFTY / BANKNIFTY / SENSEX monthly iron fly + active repair.** Enters the iron fly directly
  (SELL ATM straddle, BUY wings at ATM ± (CE+PE premium)) on the same cadence. Its **adjustment**
  (default on): when spot breaches a breakeven (K ± net credit), SELL a naked ~15-20Δ short on the
  UNTESTED side and roll it (close at ≤10Δ or ≤¼ of its sold premium, re-sell); it exits ALL only
  when the expiry payoff can no longer be positive (max payoff < 0). The naked short adds an
  uncapped tail → an optional hard MTM stop is the backstop. Togglable live via
  `POST /live/{id}/ironfly-adjust` (persisted; survives restart). Deploy-only for trading, broker
  source required; **1-min store replay** for backtesting (same ~2-months-per-cycle caveat).
- **`double_diagonal_calendar` — NIFTY double diagonal (the first two-expiry position).**
  SELL a near-weekly ~20-25Δ strangle, BUY a farther-expiry ~15-20Δ strangle as the calendar
  hedge (its residual time value rounds the payoff tent). VIX regime (High ≥20 / Med 13-19 /
  Low 9-12) labels the *expected* net-premium band — display only, selection is delta-first;
  a manual **bias** knob (up/neutral/down) skews the short/hedge deltas asymmetrically. ONE
  cycle per deploy (manual; no auto re-entry): exit at ±1.5% of the frozen broker basket
  margin; when the UNTESTED near short decays (≤10Δ or ≤¼ of its sold premium) it rolls back
  toward delta-neutral and drags its far hedge along, never crossing the other short; no
  adjustments inside 3 DTE; at the near expiry the WHOLE structure squares — far hedges are
  never left naked. Subclasses `delta_neutral_monthly` (margin freeze / cadences / exits /
  persistence spine). Deploy-only, broker source required, no backtest.
- **`fair_value_calendar` — premium-matched ratio calendar with a fair-value side pick (NIFTY).**
  The owner's video spec (ref video: https://www.youtube.com/watch?v=tn-73I63yBw&t=2162s),
  two halves:
  - **The mean-reversion side pick.** A "fair value" line = a post-crash reference high (the
    Jan-2020 COVID high, **12,430**) compounded at a long-run **11.7%/yr**. Spot significantly
    ABOVE the line → a **PUT** calendar (a reversion down pays); significantly BELOW → a **CALL**
    calendar; inside the ±`fv_band_pct` band → CALLs (better rollover carry). `side_mode` makes
    the selector optional: `fair_value` (auto) / `both` (both sides every month) / `pe` / `ce`.
  - **The structure**, hunted by PREMIUM rather than by strike distance (per lot-set): SELL 1 lot
    of the near weekly (≥ `min_sold_dte`, 4 DTE) at ~₹150, SELL 1 lot of the same weekly at ~₹450
    (deliberately ITM), BUY **3 lots** of the MONTHLY after it at ~₹200. Sold ≈ 150+450 ≈ bought
    ≈ 3×200 — **premium-matched**, so the net debit is ~0 and the % target anchors to margin.
  **The 900-point gap rule** is the risk cap: each sold leg earns ~70-80 pts per weekly roll →
  ~150/week → ~450 over a month's three rolls, so only enter when the dead zone (|buy strike −
  furthest sell strike|) is ≤ 2× that expected income. Premium hunting makes the gap *volatility's*
  choice, so the rule doubles as a vol filter; a failed check retries the NEXT day and never burns
  the month. **The cycle:** enter at the start of a calendar month (retry daily until a valid setup
  lands); target **+5% of the frozen broker margin** on the WHOLE cycle (`pnl_basis="total"` — the
  rolls ARE the income); not hit by the sold weekly's expiry → roll both sold legs to the next
  weekly at the SAME strikes, banking the decay — **one trading day before that expiry**, not on
  it, because a short weekly's margin requirement spikes into settlement (`roll_days_before`,
  form/deploy default 1; the constructor keeps the old expiry-day roll so a recovered deploy is
  unchanged). **The buy leg is never rolled** (owner rule): when
  the sells would land on the buy expiry the CYCLE ENDS — close everything, and a fresh cycle (new
  FV read, new hunt) opens the next session, which structurally bounds a losing cycle at ~one
  buy-expiry month. Premiums are absolute ₹ from `premium_scale_before` (2024-08-01) and scale by
  spot before that (₹150 at 25k was not ₹150 at 16k); the gap cap scales identically.
  **1-min-store backtest** (the first two-expiry strategy with one) + a deploy route (broker source
  required) for the paper forward test. **5-year store replay, 1 lot-set, ₹1.45L margin/set:** CE-only
  **+₹147k** (65 cycles, 77% win, max DD ₹52k, t=2.4) is the only positive mode — `fair_value` −₹64k,
  `both` −₹154k, PE-only −₹196k. Monthly re-centering rides the index's upward drift on calls but
  chases crashes down on puts.
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
- **`intraday_strangle_combo` — two-index OTM strangle/straddle with per-leg re-entry
  (owner deck, 2026-08).** Sell 1 lot CE + PE on the current weekly at 09:16, flat by 15:25;
  the index rotates by weekday (Mon NIFTY · Tue both · Wed/Thu SENSEX · Fri NIFTY). The two
  legs are managed **independently**: 40% stop / 70% target on each leg's OWN entry premium,
  and an exiting leg re-enters at a freshly recomputed strike (separate SL and target re-entry
  budgets). `otm_steps` sets the structure — 3 = the deck's OTM3 on the **listing** grid
  (NIFTY 50s: the one documented exemption from the 100s rule, `needs_listing_grid`), 0 = an
  ATM **straddle**; `wing_steps` > 0 buys a protective long that many grid steps beyond each
  short — the intraday **iron fly / condor**, which caps the worst possible day by construction
  instead of by a stop working through a gap (the wing is protection, not a managed leg: no
  40/70 exits of its own, it rolls with its side and is sold when that side is done for the day).
  `same_strike_action` (reenter/skip/hold) governs the case where the
  recomputed strike hasn't moved. Rupee MTM stop per index (`mtm_stop_per_lot`, day-cumulative).
  **Backtest finding (5y NIFTY): `max_sl_reentries=0` + the straddle structure was the best
  risk-adjusted config tested (~₹181k net, ~₹16k max DD at 1 lot); SL re-entries lost money
  in every window.** Deploy-capable (paper-tested since 2026-08-13) + 1-min-store backtest.
- **`put_condor` — monthly LONG put condor with switchable adjustments.** First trading day of
  each month, on the monthly expiry: buy high PE / sell two spaced PEs / buy low PE — a NET
  DEBIT, so max loss = the debit (~₹2k/lot): **defined-risk by construction**. Exits are % of
  MAX LOSS (not margin — the model margin is ~200× the real risk). Both adjustment rules are
  independently switchable (`down_breach_action`: roll the top long / recenter / none;
  `loss_repair`: roll the lower short up / none) — the 5y sweep favoured target-only exits with
  NO adjustments (best OOS). Backtest-only for now (deploy route deliberately deferred).
- **`straddle_btst` — BTST long ATM straddle.** BUY the ATM CE+PE at 15:20, SELL both at the
  next session's 09:20 — a debit position betting overnight gaps outrun overnight theta.
  **Tested and negative** (2y NIFTY: −₹66.5k); kept for study, not recommended.
- **`asymmetric_premium_intra` — current-week call + next-week put (owner sheet, 2026-08).**
  Short ATM CE on this week, short ATM PE on next week, 09:30→15:15; relative-premium
  adjustment (cheap leg rolls to match the rich leg); 100-point combined day stop.
  **Tested and shelved**: both distinguishing ideas cost money over 2y, and with them removed
  it is statistically the ATM straddle above (t=0.83) — kept as the controlled experiment that
  proved it (`put_expiry_offset` 0 collapses it to the straddle).
- **`broker_smoke_test` — the end-to-end REAL-order probe (Brokers page card).** BUY 1 lot of a
  cheap OTM weekly option (premium ₹5-20, OI floor) or 1 share of a stock (default ITC), hold
  ~60s, SELL, then the run **stops itself** (only ever on a FLAT book). One cycle exercises the
  full order path: place → poll → LIMIT-at-touch→protected-limit escalation (the wide OTM spread triggers
  it naturally) → fill → book-sync → reconcile (+ Telegram) → exit. Sizes hard-coded (1 lot /
  1 share, not params); the UI gates a LIVE deploy behind a typed "REAL" confirm; on a disarmed
  account it paper-fills and wears the "orders PAPER" chip — a useful negative test. Deploy-only,
  no backtest (paper fills always "work").

### 3.4 Shared strategy machinery

`base.py::Strategy` (the `on_slice(ctx) → [Signal]` protocol) · `_options_common.py`
(premium-sanity guard, nearest-strike snap, most-liquid monthly-expiry picker, plus three
mixins: `ExitCadenceMixin` — every options strategy samples its profit/adjust decision on
`profit_check` and its stop/exit on `stop_check`, each tick/1..60min/eod, hard time exits never
gated; `TrailingStopMixin` — the shared ratchet/below-peak trail; `EntryVolFilterMixin` — the
ATM-IV−HV vol-premium entry gate any option seller can inherit) ·
`call_ratio_monthly.py` (the ratio-family base: strike-mode resolution in points/percent/delta/
SD, the credit-gated leg-shift search, margin-auto sizing, intraday exit cadences, force-entry,
BS delta/IV helpers) · engine services: `black_scholes.py` (IV/delta), `contract_specs.py`
(lot sizes, expiry calendars), `margin.py` (short-option margin), `instrument.make` (builds the
`UNDERLYING|YYYY-MM-DD|STRIKE|RIGHT` symbol), `indicators/supertrend.py`.

---

## 4. Backtesting

- **One backtest page, two data bases.** The New-backtest form starts with a basis selector:
  **EOD (daily cache)** runs the engine below; **Intraday (1-min store)** replays the
  self-captured option store minute-by-minute through the ACTUAL strategy classes
  (`services/intraday_replay.py`) — 15 IDs and counting: the intraday family (intraday straddle,
  strangle combo, weekly VWAP straddle, CP ratio expiry, straddle BTST, asymmetric premium), the
  positional family (call / put / batman ratio, HNI weekly, 21-EMA momentum, put condor), the
  monthly cycles (delta-neutral, iron fly — these warn that a full cycle needs ~2 months of
  capture) and the two-expiry fair-value calendar, plus momentum-theta via its BS service. Real premiums, volume-weighted signals, F&O charges
  per fill, parity-derived spot, expiry settlement. Both bases produce ordinary runs — same
  Runs list, run detail, and Compare; intraday runs carry `data_basis: intraday` in params.
  The intraday window grows automatically with every day the store captures.
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
- **Point-in-time universes** (the survivorship-bias fix): the **Nifty500 Momentum 50** universe
  can run in `pit_universe` mode — the run trades the UNION of every name that was EVER a member
  and the strategy receives the semi-annual rebalance table, so its daily scan follows the index
  *of that date*. Membership gates ENTRIES only, never exits (a name dropped at a rebalance is
  still managed out by its own rules). Official NSE lists where a capture exists (Wayback 2024-09
  + 2025-02, live 2026-08), methodology replication elsewhere — validated 38-39/50 at the official
  checkpoints, with each rebalance's source labelled in `data/mom50_membership.json`. Consumed by
  `happy_twins`, `supertrend_momentum` and `nifty_shop`.
- **Clone & template**: a finished run can be cloned into a new backtest form with its params
  prefilled (including its start/end dates).
- **Parameter sweep on BOTH bases**: tick "Sweep a parameter", pick any numeric knob and 2-5
  values — each runs and saves, then Compare opens with them side by side. On the intraday
  basis the values run sequentially through the single-flight replay job (minutes each;
  closing the page stops the queue after the value in flight). Single underlying only.

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

Real orders are the platform's most guarded path. **The path is LIVE** (first real order
2026-07-10); every gate below still applies to each deployment individually — a LIVE run on a
disarmed account paper-fills.

- **`LiveBroker` (`brokers/live_broker.py`) is the ONLY code that places real orders.** It
  satisfies the engine's `execute(BrokerOrder) → Fill` contract, so the entire shared decision
  path is untouched — a LIVE session gets a LiveBroker *injected* in place of the paper broker.
- **The 4-key injection gate** (`manager._maybe_inject_live_broker`): a LiveBroker is used only
  when **mode == LIVE** AND **`SKAS_LIVE_TRADING_ENABLED`** AND the **account is armed** AND the
  adapter exposes the full order surface. Any other combination keeps simulated fills. Matrix-
  tested.
- **Execution style**: LIMIT at the touch (SELL@bid / BUY@ask) → poll ~2s → after
  `live_order_timeout_s` (10s) escalate to a **protected LIMIT** re-priced through the fresh
  touch — never a naked MARKET modify (Zerodha's API rejects those on options; the 2026-07-27
  halt). **Exits walk a LADDER** of escalations (3% → ~8% → ~20% through the touch, touch
  re-read per rung, later rungs half-timeout) — being flat matters more than the last rupee;
  entries keep the single 3% rung (a bad fill is worse than no fill). Partial fills ≥1 unit
  are booked at the actual quantity, and legs that DID fill before a mid-basket failure are
  booked into the session's own trade log before the halt (the 2026-08-11 lesson).
- **Session close is per-segment** since SEBI's CAS (2026-08-03): index F&O trades to
  **15:40**, equity cash to 15:30 (`SKAS_SESSION_CLOSE_DERIV`/`_EQUITY`). One definition
  (`live/quotes.is_market_open(segment=)`) gates decisions, reconciliation AND the order rail —
  the rail picks its segment per order.
- **"Mark closed at broker"** (`POST /live/{id}/adopt-broker-close`, Live-tile menu): books
  legs the broker has ALREADY closed (manual square-off in Kite, a broker auto-close) at the
  price the owner states — **placing no orders**. Flatten places real orders, so it is the
  wrong tool once the position is gone; without this the run carries a phantom leg and every
  reconciliation halts it.
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

- **Analyze workbench (`/analyze`, 2026-07)**: per-run backtest analytics for options runs —
  KPI tiles (report-exact), a Stop/Target Simulator replaying every trade's stored 5-min MTM
  path (margin or credit basis, fixed stop/target + ratchet/below-peak trail), a Conditioner
  Explorer (11 entry-condition slices with CI-aware bucket stats), a Premium-Melt × Position
  hero (time-of-day decay from the traded straddles), and drill-ins: conditioning cross-tabs,
  skew, melt, trade lifecycle (MAE/MFE, spaghetti), execution quality (breakeven slippage,
  cost decomposition, phantom-fill check vs store volumes) and risk & robustness (underwater,
  streaks, rolling Sharpe, autocorrelation, bootstrap CIs, regime boundaries) + the trade
  ledger. Bundle computed on first open (background job, disk-cached); equity runs keep the
  candle-chart trade analysis.

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
- **Trade** (`/trade`) — **Builders** (Option, Equity, Delta-Neutral, Iron Fly, Intraday
  Straddle, Weekly Straddle, Intraday Theta, CP-Ratio-Expiry, FV Calendar — each constructs
  and deploys a position) and **Screeners**:
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
- **Docs** (`/docs`) — static, searchable reference cards for every deployable strategy
  (structure / entry / exit / risk, a 3×2 fact grid and a deploy CTA), grouped by family and
  searchable. No API calls. `tests/test_strategy_docs_coverage.py` fails if a registered
  strategy has no card (the Trade builders' custom positions and the broker smoke-test probe
  are the documented exclusions — they're tools, not strategies).
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
`GET /runs/{id}/trades.csv`, `GET /runs/{id}/benchmark?index=`, `GET /analysis/runs`;
**M** `POST /backtest/intraday` (the 1-min-store replay — returns a job id, 409 while one runs)
+ `GET /backtest/intraday/progress`; **M** `PATCH
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
required), **`/force-entry`**, **`/ack-order-error`**, `/params` (hot-edit a running deploy's
strategy knobs), `/adopt-broker-close` (book legs the broker already closed — places no order),
`/ironfly-adjust`, `/archive|unarchive`, `PATCH /{id}`,
`DELETE /{id}`; `WS /ws` (snapshot/trade broadcast).

**Research** (`/research`, read-only) — **M** `POST /donchian-study`, `POST /momentum-theta-bt`,
`POST /bs-calibration`, `POST /loss-study` + `GET /loss-study/progress` (single-flight job).

**Trade** (`/trade`) — **M** `POST /options/margin` (basket margin preview); deploys that reuse
the shared deployment path: **M** `POST /options/deploy` (custom_options), `/options/fibret/analyze`,
`/options/donchian/analyze|portfolio|deploy`, `/options/delta-neutral/deploy`,
`/options/iron-fly/deploy`, `/options/double-diagonal/deploy`,
`/options/fair-value-calendar/deploy`, `/options/ratio/deploy`,
`/options/intraday-straddle/deploy`, `/options/weekly-intraday-straddle/deploy`,
`/options/cp-ratio-expiry/deploy`, `/options/momentum-theta/deploy`, `/equity/deploy`,
`/smoke-test/deploy` (the real-order probe).

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
