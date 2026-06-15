# Plan: HNI Weekly strategy (NIFTY 1-3-2 net-zero call ratio, weekly Tuesday expiry)

> skas-algo-platform, branch `feat/options-platform`. New strategy from the HNI deck.
>
> **Status: IMPLEMENTED (2026-06-12).** `strategies/hni_weekly.py` + generalized ratio
> base; tests `tests/test_hni_weekly.py` (8) green; verified backtest run #93
> (2025-09-01 → 2026-06-10): 40 weekly cycles, Mon→Fri, era-aware 75/225/150 → 65/195/130.

## Context
A new **HNI Weekly** strategy (NIFTY-only). It's a *net-zero* call-ratio "broken-wing"
tent: **BUY 1× ≈200 OTM, SELL 3× ≈400 OTM, BUY 2× ≈600 OTM** (net contracts
+1−3+2 = 0 → limited risk on **both** sides; no naked tail). Enter **Monday** into the
**next Tuesday's weekly (8 DTE = "bi-weekly")**, exit **Friday** of the same week (no
weekend carry — the deck's "DURATION: 5 DAYS"), with a **±1% of deployed margin**
target/stop. Target R:R ≈ **1:1** (max profit ≈ max loss).

Geometry: for the equidistant net-zero tent, `max profit = (sell−buy offset)·lot + credit`
and `max loss = (sell−buy offset)·lot − credit`, so the **1:1 R:R is intrinsic to the fixed
200/400/600 offsets + net-zero ratio** (a small net credit/debit just nudges it slightly —
e.g. the screenshot's ₹1,235 credit gives 1:1.2). **No net-credit requirement** — we enter
at the deck offsets every week regardless of the small credit/debit sign.

User decisions: target/stop = **1% × lots × ₹1.32L** (deployed margin per 1 lot-set);
keep **fixed offsets** (`max_shifts=0` — the 200/400/600 geometry is what gives ~1:1 R:R;
don't shift strikes); **no credit-sign gate** (enter every week); **backtest from
2025-09-01** (Tuesday-expiry era). EOD engine → entry/exit at the **close** (9:45 AM entry
and intraday ±1% exits are not representable; documented).

## Findings (verified against the cache)
- Weekly NIFTY options are cached; **Tuesday expiries from 2025-09-01** (Thursday before).
  `OptionChainView.expiry_for_dte(monday, 8)` → the next Tuesday at exactly 8 DTE
  (`engine/options/chain.py:65`). No chain plumbing needed.
- **BANKNIFTY options are NOT cached** → HNI is NIFTY-only (the deck is NIFTY-only too).
- NIFTY lot is era-aware via `contract_specs.lot_size_for` (65 now); 1/3/2 × 65 =
  **65 / 195 / 130** — matches the StockMock positions exactly.
- **No engine change needed** (unlike the covered call): HNI is pure CE options; the
  options stack already handles it. `call_ratio_monthly` is itself a net-zero 1:2:1
  spread, so HNI is the **same family** with a 1:3:2 ratio + weekly timing.

## 1 · Generalize the leg ratio in the base (backward-compatible)
File `src/skas_algo/strategies/call_ratio_monthly.py`:
- Add params `buy_lots=1, sell_lots=2, hedge_lots=1` (relative multiples). Defaults
  reproduce today's 1:2:1 **byte-identical** → parity/existing tests stay green.
- `_maybe_enter`: leg units = `units*buy_lots` / `units*sell_lots` (short) /
  `units*hedge_lots` (where `units = lots × lot_size_for(...)`).
- `_build_side` credit: `net = (sell_lots*sell − buy_lots*buy − hedge_lots*hedge)*units`.
- Batman `_wing_credit`: same generalization.
- Extract three subclass seams (base behaviour unchanged):
  - `_select_expiry(chain, today)` → base returns `_next_monthly_expiry(...)`.
  - `_entry_allowed(today)` + `_mark_entered(today)` → base = month-lock +
    last-weekday-of-month (replaces the inline `last_entry_month`/`_last_weekday_of_month`
    checks at the top of `_maybe_enter`).
  - `_risk_base()` → base returns `initial_capital`; `_time_exit(today)` → base returns
    the `max_holding_days` rule. Used in `_manage` (`pnl >= profit_target_pct*_risk_base()`).

## 2 · Strategy: `src/skas_algo/strategies/hni_weekly.py`
`class HniWeeklyStrategy(CallRatioMonthlyStrategy)`, `strategy_id="hni_weekly"`,
`entry_reason="hni_weekly"`; register in `strategies/registry.py`.

**Defaults:** `buy_lots=1, sell_lots=3, hedge_lots=2`; `strike_mode="points"`,
`buy_offset=200, sell_offset=400, hedge_offset=600`; `max_shifts=0` (strict deck offsets —
the fixed 200/400/600 geometry gives the ~1:1 R:R); credit-sign gate **disabled** (wide
`min_credit_pct` floor + generous `credit_debit_limit_pct`) so the deck offsets are taken
**every week** regardless of small credit/debit; `tail_hedge_offset=0` (no tail);
`dte_target=8`; `entry_weekday=0` (Mon);
`exit_weekday=4` (Fri); `margin_per_lotset=132000`; `profit_target_pct=0.01`,
`stop_loss_pct=0.01` (× deployed margin).

**Overrides:**
- `_select_expiry`: `chain.expiry_for_dte(underlying, today, dte_target)`; skip if
  `|dte−dte_target| > 3` (don't enter when no ~8-DTE weekly exists, e.g. holiday weeks).
- `_entry_allowed`: `today.weekday() == entry_weekday` (Monday; or first trading slice of
  the ISO-week if Monday is a holiday) **and** this ISO-week not yet entered.
  `_mark_entered`: store `last_entry_week = today.isocalendar()[:2]`.
- `_risk_base`: `margin_per_lotset × lots` (deployed margin, not account capital).
- `_time_exit`: `today.weekday() >= exit_weekday OR isoweek(today) != isoweek(entry_date)`
  → force-exit Friday (and never carry past the entry week, holiday-robust).
- `export_state`/`load_state`: add `last_entry_week`.
Everything else inherited: strike snap/`_bad`, OI>0 filter, credit gate, stale-mark guard,
EOD MTM target/stop, `EXIT_ALL` per leg, expiry settlement.

## 3 · UI
- `web/src/pages/NewBacktestPage.tsx`: param block for `hni_weekly` (isOptions): Underlying
  (NIFTY default), CE lots, ratio (buy/sell/hedge lots = 1/3/2), offsets (200/400/600 pts),
  DTE target, Target % / Stop %, Margin per lot-set (₹1.32L), Capital. Info note: net-zero
  tent, R:R ~1:1 by construction (no credit-sign gate), Mon→Fri EOD-close approximation,
  intraday ±1% not modeled.
- `web/src/lib/params.ts`: labels/order/PCT for the new params.
- `web/src/pages/RunsPage.tsx`: `STRATEGY_LABELS["hni_weekly"] = "HNI Weekly"`.
- Templates / compare / payoff chart work automatically (3 CE legs → broken-wing tent).

## 4 · Tests
- `tests/test_call_ratio_monthly.py`: add a 1:3:2 unit test of the generalized `_build_side`
  net formula; confirm the existing 18 tests stay green (defaults unchanged).
- `tests/test_hni_weekly.py`: `FakeCRSD`-style harness (weekly Tuesday expiries + scripted
  spot). Cases: (1) Monday entry builds 1:3:2 at ~200/400/600 OTM, net credit ≥ 0, units
  65/195/130, naked-free; (2) expiry = next Tuesday at 8 DTE; (3) Friday force-exit;
  (4) target at +1%×margin / stop at −1%×margin; (5) entry is **not gated on credit sign** —
  a small-net-debit week still enters, and max profit ≈ max loss (R:R ~1:1) at the
  equidistant strikes; (6) one trade per ISO-week.
- Parity: `tests/test_sst_parity.py`, `tests/test_mode_equivalence.py` green.

## 5 · Verification (e2e)
1. Full `pytest` green; `tsc --noEmit` + `npm run build` clean.
2. API backtest: NIFTY DERIV, `hni_weekly` defaults, **2025-09-01 → latest**. Verify the
   trade log shows weekly **Monday entries / Friday exits**, 1-3-2 legs (65/195/130),
   entries every week (no credit-sign skip), premium/debit per cycle, and the report's
   max-profit ≈ max-loss (R:R ~1:1). Spot-check a cycle's payoff chart renders the tent.
3. Present with honest caveats: **EOD ±1% approximation** (the strategy is intrinsically
   intraday — close-to-close marks under-represent how often ±1% fires); short
   Tuesday-era sample (~38 weeks); real NIFTY chain (not synthetic).

## Out of scope (deferred)
Intraday 9:45 entry & intraday ±1% fills (EOD engine) · BANKNIFTY (uncached) · binary-event
pause · live/paper options wiring · pre-Sep-2025 Thursday-era data.
