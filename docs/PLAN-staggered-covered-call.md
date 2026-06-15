# Plan: Staggered Covered Call strategy (GOLDBEES + GOLDM, extensible to NIFTY/BANKNIFTY)

> skas-algo-platform, branch `feat/options-platform`. New strategy per the user's detailed spec.
>
> **Status: IMPLEMENTED (2026-06-12).** `strategies/staggered_covered_call.py` +
> `equity_loader` engine seam; tests `tests/test_staggered_covered_call.py` (11) green;
> BeES history fetched (GOLDBEES/NIFTYBEES/BANKBEES 2020→now); verified backtests:
> GOLD run #94 (2020-26, +45.7%, 186 cycles) and NIFTY run #95 (extension path, +23.4%).
>
> **Refinements (2026-06-13)** from reviewing the report:
> 1. **Equity leg in the report.** The options report previously showed only the CE
>    legs; the ETF tranche buys and the realized P&L booked when called away were
>    invisible. `engine/options/report.py` now pairs the non-option (ETF) BUY/AVG_BUY →
>    SELL into a `equity_legs` round-trip list (with the cc_t1/t2/t3 accumulation
>    history) + an `equity_held` open position (marked to the last close via the new
>    `RunResult.final_marks`). Summary gains `equity_realized_pnl / equity_open_pnl /
>    option_open_pnl / strategy_net_pnl`, the last reconciling EXACTLY to Final Equity −
>    capital. UI: a "Covered leg (equity)" table + combined-net tiles in OptionsReport.
> 2. **Premium-floor strike selection.** `_select_ce_strike` starts at `ce_otm_pct` then
>    walks the strike NEARER (more premium) until the per-unit premium clears
>    `min_premium_pct × spot` (default 0.1%), never nearer than `min_ce_otm_pct` (2%).
>    Entry always establishes coverage; a roll-down DECLINES when no strike clears the
>    floor (stops the short-DTE churn into ~₹0 calls — near-zero-premium cycles 19→1).
>    Re-run #98: +41.2% (selling nearer caps more GOLD upside — the expected
>    covered-call trade-off vs the +45.7% no-floor run; tune `min_premium_pct`/set 0 to
>    recover max upside). Both new params are in the UI.
> 3. **Never sell/roll a call below the ETF cost basis** (`keep_strike_above_cost=True`,
>    default). The old roll-down chased premium as spot fell, rolling the strike BELOW
>    the equity's average cost; a recovery then assigned it and dumped the ETF at a loss
>    (NIFTY run #99: the 2020-01-01 cycle sold NIFTYBEES at ₹104 vs ₹130 cost = −₹1.21L,
>    labelled "called away"). Fix: `_cost_floor_strike` converts the held ETF's avg cost
>    to index points (via the live spot/ETF ratio) and `_select_ce_strike` floors the
>    strike there; a roll-down DECLINES when no strike stays both above cost and below
>    the current strike (it keeps riding the original higher call, which expires OTM, and
>    holds the ETF through the drawdown). Tracks `held_cost` in state; reset on
>    called-away. NIFTY re-run #101: **every called-away leg is now a profit (0 losses)**,
>    the 2020 cycle exits +₹39,391 in Nov-2020 once NIFTY recovers above cost, and total
>    return rises 12.7%→17.9%. UI: a checkbox to toggle the guard. (GOLD has a residual
>    basis gap — GOLDBEES is a real NSE ETF vs the MCX-futures "GOLD" index the strike/
>    cost-floor live in — so the guard is exact on NIFTY/NIFTYBEES but approximate on GOLD.)
>
> **Report revamp (2026-06-13)** — covered-call-specific analytics: the report now groups
> the run into CAMPAIGNS (one per accumulation→called-away, plus the open holding) via
> `_build_campaigns` in `engine/options/report.py` (`report.options.campaigns`), each
> carrying its tranche buys, the calls sold/rolled in its window, and a combined net
> (equity + option). `services/backtest.py` attaches the underlying daily price series
> (`report.options.timeline`). UI: `CoveredCallReport.tsx` renders a global
> accumulation/price/strike timeline + expandable campaign cards (tranche table, calls
> table, per-campaign mini-timeline), shown in place of the straddle/ratio positions
> table for covered-call runs; yearly + monthly tables unchanged.
>
> **Profit levers (2026-06-13, built).** All three roadmap levers added (each a param):
> 1. **Cost-anchored min return** (`min_return_pct`, code default 0 / UI default 2%): the
>    cost floor becomes `avg_cost ×(1+min_return_pct/100)` so an assignment locks in ≥ that
>    gain on the equity.
> 2. **Delta strike when fully covered** (`covered_call_delta`, code 0 / UI 0.30): once all
>    tranches are in and the position is above cost, target a ~0.30Δ (closer) strike for
>    richer premium instead of fixed %-OTM; the cost floor still clamps it. `_delta_strike`
>    backs IV out of each row's close (bs.implied_vol/delta).
> 3. **Wheel / put-selling** (`sell_puts`, default off; `put_otm_pct=5`): accumulation
>    switches from GTT up-buys to selling cash-secured puts (one open at a time) — premium
>    income on the way down, ETF bought (`cc_put_assigned`) when a put finishes ITM
>    (EOD cash-settled-assignment model). New `self.pe` state + `_on_pe_settled`.
>
> NIFTY 2020-26 comparison: baseline +17.9% (DD 9.0%); **delta0.30+minret2% +18.3% (DD
> 6.0% — better risk-adjusted)**; wheel +14.0% (DD 11.5% — underperforms in a bull run,
> as expected; shines in flat/down regimes); all three +16.6%. Code defaults preserve the
> verified baseline; UI defaults turn delta+min-return on, wheel off. **129 backend tests
> green** (delta/min-return/wheel cases added), tsc+build clean.

## Context
A staggered covered call: buy an ETF underlying in 3 tranches (T1 at entry; T2/T3 as
GTT buys that fire as spot rises toward the sold strike), sell 1 full CE lot against the
*intended* full position at entry (33% covered / 67% naked initially), roll the CE down
when ~80% of its premium is captured, cycle monthly. First deployment: **GOLD** —
underlying **GOLDBEES** (NSE ETF), option **GOLDM** (the platform's synthetic Black-76
GOLD chain); must extend to NIFTY/NIFTYBEES and BANKNIFTY/BANKBEES unchanged.

User decisions: **backtest-first** (dashboard outputs come from the existing run report;
live session commands wait for options live-wiring); **GTT fills at the close of the
crossing day** (honest EOD); **bull put spread add-on deferred**; **real BeES history**
fetched via the logged-in Kite session.

## 1 · Engine seam: equity symbols inside a DERIV run (the one engine change)
Today `OptionMarketView` KeyErrors on plain symbols (`engine/options/market.py:66-74`,
loader returns None because `instrument.parse()` → None). Add an optional fallback:

- `OptionMarketView.__init__(..., equity_loader=None)`: in `_ensure(symbol)`, when
  `parse(symbol)` is None and `equity_loader` is set, load the symbol's close series via
  `equity_loader(symbol, lo, hi)` (same `{date, close}` frame shape; forward-fill /
  `has_print` semantics unchanged).
- Thread it through both builders: `data/options_provider.py build_options_run` and
  `data/synthetic_options.py build_synthetic_options_run` get `equity_loader=None`
  param; `services/backtest.py` DERIV branch passes the existing equity `loader`
  (`get_price_loader`) so ANY options run can now price cached NSE symbols.
- Already-correct seams (verified): `ExpirySettler` skips non-option symbols
  (settlement.py:37-41), `ChargeModel` charges only option fills (execution.py:86),
  `build_options_report` excludes equity legs (report.py:42-54) while the main report /
  equity curve includes them. Margin model stays coverage-unaware (reported margin for
  the short CE is overstated for a covered position — documented caveat, fine).
- **Parity guard**: `equity_loader` defaults to None → existing options runs and all
  SST equity paths byte-identical.

## 2 · Strategy: `src/skas_algo/strategies/staggered_covered_call.py`
`strategy_id = "staggered_covered_call"`, registered in `strategies/registry.py`.
Follow the ratio-family conventions (universe/initial_capital/underlying ctor args,
`**_ignored`, `export_state`/`load_state`, entry reasons on signals).

**Params** (defaults per spec §6):
- `underlying="GOLD"`, `etf_symbol=None` → auto-map `{GOLD: GOLDBEES, NIFTY: NIFTYBEES,
  BANKNIFTY: BANKBEES}` (overridable)
- `lots=1` (CE lots; coverage units derived), `ce_otm_pct=6.0` (range 3–12)
- `tranches=3` (fixed per spec), `rolldown_trigger_pct=0.80` (range 0.50–0.95)
- `rolldown_min_dte=5` (don't churn a near-expiry CE; premium-capture near expiry just
  expires worthless instead — sensible guard, configurable)
- `min_dte=18` (monthly expiry selection, same convention as ratio family)
- `lot_overrides`, `risk_free_rate` pass-throughs

**Sizing** (computed each cycle from live data):
`full_units = round(lots × lot_size × opt_spot / etf_close)` where `lot_size` =
`contract_specs.lot_size_for(underlying, expiry)` (GOLD→10 = GOLDM multiplier;
NIFTY→65 era-aware). `tranche_units = full_units // 3` (T1 gets the remainder).
This is notional-matched coverage: e.g. 1 GOLDM lot ≈ ₹14.8L → ~3,300 GOLDBEES/tranche.

**State machine** (`on_slice`, EOD):
1. **Entry (flat)**: pick monthly expiry via max-total-OI-of-nearest-qualifying-month
   (lift `_next_monthly_expiry` from `call_ratio_monthly.py:107-131` into a shared
   helper `strategies/_options_common.py` and reuse in both). Read chain spot S and ETF
   close. Sell `lots` CE at strike K = snap(S × (1 + ce_otm_pct/100)) from OI>0 rows
   (reuse `_snap`; reject bad closes via `_bad`). Buy T1 (`tranche_units` + remainder)
   of the ETF. Record `triggers = [S + i/3·(K−S) for i in (1, 2)]` against the **chain
   spot** (S and K share that unit system; the ETF tracks it).
2. **Tranche fires**: any day chain-spot close ≥ trigger_i and tranche i unfired →
   `ENTER_LONG etf tranche_units` (fills at that day's close per decision). Tag reasons
   `cc_t1/cc_t2/cc_t3` so the trade log shows the accumulation stages.
3. **Roll-down**: when CE close ≤ (1 − rolldown_trigger_pct) × sold premium AND
   DTE ≥ rolldown_min_dte → EXIT the CE (buy back), sell new CE at
   K_new = snap(S_now × (1 + ce_otm_pct/100)) **same expiry**, recompute the unfired
   triggers from (S_now, K_new) (Rules 3.2–3.4). Reason tags `cc_rolldown_close` /
   `cc_rolldown_open`.
4. **Expiry** (engine settles the CE to intrinsic automatically): next slice with no CE
   lot → if it expired ITM (last spot > K): sell ALL ETF units (called-away equivalence
   under cash settlement: keep cash ≈ strike value + collected premium, Rule 4.2) and
   re-enter fresh. If OTM: **keep tranches**, start a new cycle — sell next month's CE
   at fresh OTM strike, re-baseline `full_units` at current prices, and set GTT triggers
   only for the still-missing tranche units (held units count as fired tranches).
5. **Hard guardrails honored in-model**: naked fraction = 1 − held_units/full_units is
   recomputed every slice and stamped into signal meta / state (never claim covered);
   triggers always recomputed after roll-down; stale-mark guard on the CE like the ratio
   family (`market.has_print`). Binary-event pause (Rules 2.4/3.5) and legacy holdings
   (5.3) are live-trading concerns — documented as out of scope for the EOD backtest.

## 3 · Data: BeES history (no code)
At implementation start, fetch via the existing endpoint (needs the user's Kite session):
`POST /api/v1/brokers/1/refresh-cache {"symbols": ["GOLDBEES","NIFTYBEES","BANKBEES"],
"start_date": "2020-01-01"}` — `services/market_data.py:15-43` already auto-fetches
uncached symbols full-range through `SkasData.get_prices(use_cache=True)`. If no valid
session, build/test on the fake harness and ask the user to log in before the real run.

## 4 · UI
- `web/src/pages/NewBacktestPage.tsx`: new param block for `staggered_covered_call`
  (isOptions=true): Underlying (GOLD/NIFTY/BANKNIFTY), ETF symbol (auto-filled,
  editable), CE lots, CE OTM % (3–12), Roll-down trigger % (50–95), Roll-down min DTE,
  Min DTE, Capital. Info note explaining tranche/GTT mechanics + the EOD-close GTT
  approximation + naked-% disclosure (Rule 5.4 wording).
- `web/src/lib/params.ts`: labels/PCT/order for the new params.
- `web/src/pages/RunsPage.tsx`: STRATEGY_LABELS entry.
- Templates/compare/payoff chart work automatically (cycles with one short CE leg).

## 5 · Tests (`tests/test_staggered_covered_call.py`)
Fake-SD harness like `FakeCRSD` (test_call_ratio_monthly.py:53-79) extended with an ETF
close series + a scripted spot path; plus one engine test in `tests/test_options_engine.py`
for the `equity_loader` fallback (OptionMarketView serves a plain symbol; still raises
without the fallback). Strategy cases:
1. Entry: short CE at ~OTM% strike + T1 ETF buy ≈ ⅓ of notional-matched units; naked
   fraction ≈ 67% in state.
2. Rising path: T2 then T3 fire at the trigger crossings; fully covered at K.
3. Falling path: CE decays to ≤20% of sold premium → roll-down: CE bought back, new
   lower strike sold, triggers recomputed (assert new trigger values).
4. ITM expiry: settler cash-settles, strategy liquidates ETF and re-enters fresh.
5. OTM expiry: tranches kept, new CE sold next cycle, remaining-tranche triggers only.
6. Parity: `pytest tests/test_sst_parity.py tests/test_mode_equivalence.py` green
   (equity_loader=None default; no equity-path changes).

## 6 · Verification (e2e)
1. Full `pytest` green; `tsc --noEmit` + `npm run build` clean.
2. Fetch BeES data (Section 3), then run GOLD backtests via API 2020→2026:
   default config (6% OTM, 80% trigger) — verify report sanity: options report shows
   monthly CE cycles with premium collected; equity curve includes ETF P&L; trade log
   shows cc_t1/t2/t3 staging; payoff chart renders the single-leg cycles.
3. One NIFTY/NIFTYBEES run to prove the extension path.
4. Present results to the user with the usual honest caveats: synthetic GOLDM premiums
   (Black-76, no smile), EOD GTT approximation, coverage-unaware margin reporting.

## Out of scope (explicitly deferred)
Bull put spread add-on (§4.3) · binary-event pause (live) · legacy-holding offset (5.3,
live) · Section 7 dashboard page & Section 9 session commands (live wiring pending) ·
limit-order/GTT price-level fills (EOD engine).
