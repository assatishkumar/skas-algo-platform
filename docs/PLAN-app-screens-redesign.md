# Plan: Backtest / Live / Trade screen redesign (Calm-Hero language)

## Context
A design handoff (`/Users/satish/git/temp/design_handoff_app_screens`) re-skins three **functional**
screens — Backtest, Live, Trade — into the same calm/teal "Calm Hero" language already shipped on Home.
Goal: match the mocks' look (tokens, typography, layout, expandable groups, leaderboard, sparklines,
chain builder, screener modal) **while preserving all existing API wiring and behavior** (deploy,
controls, real data). The handoff says recreate in our stack (React + Tailwind, class-based dark mode),
not port its runtime, and map its tokens onto ours.

## Shared foundation (do first)
1. **Design tokens as CSS variables** — add the handoff's token set to `web/src/index.css` under `:root`
   (light) and `.dark` (dark): `--page, --card, --border, --strong, --muted, --faint, --chip,
   --chip-text, --stat, --seg, --divider, --field, --field-border, --pos, --danger, --accent,
   --accent-deep, --ft, --golive, --ok-bg/--ok-text, --warn-bg/--warn-text, --opt-bg/--opt-text, --atm`.
   Use via Tailwind arbitrary values: `bg-[var(--card)]`, `text-[var(--strong)]`, `border-[var(--border)]`.
   This is the clean mapping (our slate ramp is bluish; the design is teal-neutral) and keeps the 3
   screens consistent without per-element `dark:` duplication. Keep `.dark` mechanism (already in place).
2. **Shared UI primitives** (new, small, in `web/src/components/ui.tsx` or a `redesign/` folder):
   `Segmented` (segmented control), `Chevron`, `KebabMenu` (⋯ dropdown), `Pill`/`Tag`, `Sparkline`
   (smoothed line+area SVG, reuse the Home equity-path math). Reuse across all three screens.
3. Fonts (Space Grotesk + Manrope) already loaded; nav already has the logo square + Home item.

## Backtest (`BacktestPage` + `RunsPage`)
Convert the Runs view to a **leaderboard** (biggest change):
- Header: `Backtest` + segmented [Runs | New backtest] + ghost **Compare** (right).
- Filters: segmented [Active | Archived] + Type [All | Batch | Individual] + Sort pill + search.
- Per **strategy group** (expandable): rank that strategy's runs by `Total Return %` desc.
  - **Winner card** (rank 1): teal gradient, badges (★ BEST RUN, ★ template, type), meta, metric
    cluster (Final equity, Max DD, Sharpe, Win rate, Return) + Open / Forward-test.
  - **Ranked rows** (2…n): chip rank tile, name + type pill + `#id · Nd ago · N trades`, metric
    cluster, Open / Forward-test / ⋯ (Clone/Archive/Edit/Delete).
  - Type filter re-ranks + renumbers within the group.
- Data: `RunSummary.metrics` → `Total Return %`, `Final Equity`, `Max Drawdown %`, `Win Rate %`,
  `Total Trades`; `batch_id != null` ⇒ Batch else Individual; template from the templates map.
  **Sharpe is not in our metrics** → show "—" (or omit the column). Reuse existing `api.runs`,
  forward-test/clone/archive/delete actions and templates from the current `RunsPage`.
- New-backtest view: re-skin `NewBacktestPage` into the single config card (Strategy, Universe
  segmented, dates, capital, Run-type radio cards, Run + Save-as-template). Keep all existing form logic.

## Live (`LivePage`)
Already grouped by strategy (shipped). Re-skin + add:
- Header: segmented [Paper | Live ₹] mode toggle + green "Market data · live" pill + **+ Deploy** (`--ft`).
  (Paper/Live mode = filter by `dep.mode`.)
- Summary bar: 6 KPIs incl. **Realized P&L** (add to `PortfolioBar`).
- Group header per mock (OPT tag, count, Unrealized ±₹, N open) — already close.
- **Deployment card** re-skin (`DeploymentTile`): status/live/OPT/strategy/broker pills, subline, 3 stat
  tiles, **Overall-P&L sparkline** (from `api.liveGreeksHistory` points' `pnl`, or equity curve),
  actions (Open/Pause/Refresh/Start) + **⋯ menu** (Go LIVE, Exit, Stop, Archive/Delete, Edit). Keep all
  existing `act()` wiring, Go-LIVE panel, RunCard expansion.

## Trade (`TradePage` + `OptionTradeBuilder` + `FibRetPage`)
- Builder: re-skin the option chain card (3 equal controls; chain grid `CE OI · CE prem · STRIKE · PE
  prem · PE OI`, ATM row `--atm`, click premium → add SELL leg), Position/Target/Stop/Deploy cards.
  Keep `OptionTradeBuilder`/`EquityTradeBuilder` logic; restyle.
- Screener: add the **screener-selector pills** (FibRet active; others → "coming soon" card), then the
  existing `FibRetPage` config + results table + deploy modal, re-skinned to the tokens. (Modal already
  has strike picker, hedge, bid/ask, margin, target, spot, cushion from the last change.)

## Sequencing (incremental; commit per screen)
1. Shared tokens + primitives. 2. **Live** (smallest delta — re-skin + sparkline + ⋯). 3. **Trade**
(re-skin builder + screener selector). 4. **Backtest** (leaderboard — largest). Verify + screenshot
each against the mock (light + dark) before moving on.

## Fidelity gaps / decisions to confirm
- **Sharpe** not available per-run → "—". OK?
- Backtest **leaderboard replaces** the current status-tab Runs list (Active/Archived only, ranked by
  return). Confirm replacing the existing view (vs adding a "Leaderboard" sub-view).
- Token CSS-vars approach (vs per-element `dark:` like Home) — recommended for these 3 screens; Home can
  stay as-is or be migrated later.

## Verification
Per screen: `tsc --noEmit` clean · `npm run build` · headless-Chrome screenshot (light + dark) vs the
`shots/` reference · click-through that existing actions still work (deploy, forward-test, controls,
chain leg add, screener deploy). Full backend `pytest` stays green (no backend changes expected).
Commit per screen on `feat/options-platform`.
