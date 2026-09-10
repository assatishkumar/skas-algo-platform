# Plan: Options Console — manual trading + minute replay

> skas-algo-platform, branch `feat/options-console` (off `feat/options-platform`).
> Design handoff: `../temp/design_handoff_options_console` (README + 13 artboards, hi-fi;
> its decisions on colour, type, spacing and copy are final).
>
> **Status: P0 IMPLEMENTED (2026-09-09).** The replay market is extracted and pinned;
> nothing user-facing yet.

## Goal

A single screen where the owner **clicks an option chain to build a position** and **steps
a historical session minute by minute**, trading a past day by hand. Today neither exists:
manual option positions are typed into a form (`/trade?tab=build` → `custom_options`) with
no chain, no payoff staging and no clock, and replay only runs a *strategy* over a range as
a batch job. Nothing lets a human sit inside a past session and make the decisions.

The gap that matters: **a strategy idea can currently only be validated by writing the
strategy.** The console lets the owner rehearse a structure — including the adjustment they
would actually have made at 11:40 — before any of it becomes code.

## Decisions taken with the owner (2026-09-09)

| | |
|---|---|
| Milestone 1 | **Replay only** (desktop D1/D2/D3/D8). No broker, no orders. |
| Underlyings | **NIFTY · BANKNIFTY · SENSEX** — what the 1-min store holds. The design lists FINNIFTY; it has never been captured and cannot be replayed. |
| Live orders | Eventually yes, but **only through the existing gates** — live mode drives an existing `LiveRun` via `manual-order`/`LiveBroker`. Never a second order path. Claude builds and tests with fake adapters and paper mode; every real order is the owner's hand (§1). |
| Placement | New top-level page `/console`. `/trade?tab=build` untouched. |

## Architecture: two sources, one state shape

```
        ┌──────────── ConsoleState (one DTO) ────────────┐
        │ session · market · chain · legs · staged ·      │
        │ risk · alerts · track · pricing · notes         │
        └───────────▲───────────────────────▲─────────────┘
                    │                       │
   REPLAY (M1)  services/options_console  LIVE (P5)  existing LiveRun
   store-fed ReplayMarket, own book,      live chain + manual_order +
   model/manual margin, no broker         LiveBroker, all §1 gates
```

The UI renders `ConsoleState` and never learns which backend produced it. In live mode the
console holds **no book at all** — it is a view and a remote control over a deployment that
already exists. That is what keeps it from becoming a second order path.

`services/options_console/` sits deliberately outside `live/` (the `/portfolio` precedent,
§8a): a non-order-path feature must not import the order path, and a test pins it.

## P0 — the replay market becomes a shared reference (DONE)

`_Market` / `_ChainRow` / `_Chain` / `_Ctx` moved **verbatim** from
`services/intraday_replay.py` into **`services/replay_market.py`**, which re-exports them as
`ReplayMarket` / `ReplayChainRow` / `ReplayChain` / `ReplayCtx`. `intraday_replay` re-imports
the underscore names, so the replay driver reads exactly as before — the diff is 4 added
lines at the import block and 292 deleted, with **zero changed lines inside
`run_intraday_backtest`**.

Why a move rather than a copy: a fork is precisely how two answers to "what was this leg
worth at 09:30" come to disagree, and the batch replay is the reference the console has to
be honest against.

Two pins in `tests/test_intraday_replay.py` guard it **forever**, not just at move time:
- `test_the_replay_report_is_byte_stable` — sha256 of the whole report over a fixed
  synthetic store. Catches any behaviour drift in those classes from either consumer.
- `test_the_console_and_the_replay_read_one_market` — identity assertions, so a future
  re-fork fails loudly.

## Measured facts the design rests on

Taken on the fixture day (NIFTY, 2026-04-01), not estimated:

| Operation | Cost |
|---|---|
| `load_day(day, underlying="NIFTY")` — 158,809 rows | 0.31 s |
| One minute step incl. `index_spot()` + `live_chain()` | 0.18 ms |
| 44 strikes × 2 sides: IV solve + Δ | 0.16 ms |
| A whole 376-minute session, stepped | 0.07 s |

Two consequences, both load-bearing:

- **Seek = rewind to the open and replay forward. No checkpoints.** `seek()` rebuilds the
  market from the day's open and re-applies a **fill journal**. It is the only construction
  that preserves forward-fill and the stale window exactly, it makes the backward jog chips
  correct by construction, and determinism becomes an identity a test can assert. Stated
  plainly: **rewinding past a fill unwinds it**, and alerts re-arm.
- **Chain IV/Δ are solved server-side, per step.** Cheaper than the step itself, and it
  keeps one solver: the chain's Δ column and the payoff's Δ come from the same numbers.

## Truths the data cannot supply (and what we show instead)

- **Spot.** The design's "NIFTY 22,929.75" is the *un-de-carried near-expiry parity
  forward*; the store has no index series. We emit `spot = index_spot()` (de-carried, so the
  console's ATM matches what every strategy sees), `fut = _parity(expiry)` raw,
  `basis = fut − spot`, and say so in `notes`. **The fixture's ATM anchor moves from the
  artboard's 22,900 to 23,000; the screen will not pixel-match, and that is correct.**
- **DAY range and VIX are lookahead traps.** A replayed day's settled bar contains the
  future. Use `ReplayMarket.spot_open/high/low` (the forming bar `note_spot()` already
  builds) and change against the *prior* settled close. India VIX is cached daily only, so
  ship prior close → today's open, labelled and frozen. Never today's close.
- **IVR** needs a year of ATM-IV (~250 `load_day` calls). Ship **VIX rank**, labelled
  `ivr_basis: "vix_rank_1y"`.
- **No bid/ask in replay** (trades-only store): `LMT` is live-mode only; replay fills at the
  minute close, and the control is disabled with a reason.
- **The illiquid row is the normal case.** At 09:30 on 2026-04-01, 177 of 239 NIFTY 28-Apr
  contracts had printed at all. Mark them `quoted: false` — **never BS-interpolate a missing
  quote**, pinned by a test.
- **Fixtures come from the store, never the artboard.** The artboard's ladder is the
  handoff's own synthetic BS-with-skew generator; the store has 24000 CE at 211.95 and
  24100 CE at 186.65 against the design's 215.00 / 188.50.

## Margin — the one number the platform cannot honestly produce in replay

Every percentage on the risk rail is "of margin". For the design's own fixture:

| Bear call spread, 10 lots | Margin |
|---|---|
| `short_option_margin` (span 10% + exposure 3% of notional, **shorts only, hedge-blind**) | ₹19,37,564 |
| Kite basket (the design's number) | ₹3,63,826 |

**5.3× wrong**, in the direction that makes a hedged structure look unaffordable. So:

1. **A manual anchor is the accurate path** — `margin_per_lot_set` on the session, exactly
   the `margin_per_set` precedent from `fair_value_calendar` / `volcano_calendar`.
2. **The default is the model, labelled** — `margin_source: model | manual | broker` is in
   the DTO and on the rail, so a 5×-off number can never read as a broker figure.
3. **A basis toggle**: percentages of margin or of capital. Capital is exact in replay.

Explicitly **not** doing: a SPAN replica. A hedged-pair approximation still came out 1.4×
off Kite on the fixture, and a plausible-looking wrong number is worse than a labelled rough
one.

## Two platform gaps the live phase must fix first

Both additive, both P5, both would otherwise surface as a broken button.

1. **`custom_options` cannot accept a manual leg open at all today.**
   `LiveSession._build_manual_leg` resolves the expiry from `strategy.entry_expiry` or
   `_default_expiry()`; `custom_options` has neither, so the call raises. The only existing
   coverage uses `hni_weekly`, which does have it. Fix: `expiry` on `ManualLegOpen`, honoured
   in `_build_manual_leg`, `entry_expiry` set on `custom_options` at entry. Also required for
   the design's expiry-chip row.
2. **Partial exit of a SHORT leg has no live representation.** `CloseShort` carries no
   `units`, `manual_order`'s `lots` counts lot *records*, and `custom_options` opens one
   record for all lots — so "exit 4 of 10" closes everything. The design's `− 4 ＋ · Exit`
   stepper is the core interaction. Fix: `Portfolio.reduce_short_lot` mirroring the existing
   `reduce_lot`, `CloseShort.units: int | None = None`, honoured in `_buy_to_close`. Shared
   engine, so **additive with `None` = today's behaviour**, pinned by `test_mode_equivalence`.
   Replay has no such constraint (the console owns its book), which is why partial exits ship
   in **P3 for replay and P5 for live**.

Also: **`payoff.ts` and the backend disagree on `t`** — the TS uses whole days floored at
`1/365`, `_enrich_greeks` uses seconds to 15:30 floored at 120 s. Up to a full day apart on
expiry morning, with the chain's Δ and the payoff's Δ on one screen. Fix: `LiveLeg` gains
`t?: number`, backend-supplied. The DTO also ships `pricing: {r, q, t_floor_s, expiry_time}`
so the frontend never hardcodes a second `r`.

## NIFTY strikes on a manual surface

§8 governs *automated* selection and leaves the manual builder unfiltered — but this
console's purpose is rehearsing what the automated strategies would do, and a 22,950 row
invites a trade the live strategies can never place. **Coarsening ON by default**, with an
explicit `Show 50s` toggle in the chain settings row (off by default), echoed as
`chain.listing_grid` in the DTO. `ReplayMarket.allow_fifty_strikes` already exists.

## Reuse

| Need | Reuse |
|---|---|
| Minute-clock market | `services/replay_market.ReplayMarket` (P0) |
| Charges | `engine/options/charges.charges_for_txn` — the same call the batch replay's `_fill` makes |
| Greeks / IV | `engine/options/black_scholes` (backend), `web/src/lib/payoff.ts` (frontend) |
| Payoff, max P/L, breakevens, POP, R:R | `payoff.ts::computeMetrics` — the staged before→after is the same function called twice |
| Δ-anchored preset resolution | `pages/trade/BuildView.tsx::pickDeltaStrike`, extracted to `lib/console/presets.ts` |
| Chain ladder | Harvest two ideas from the **orphaned** `components/trade/OptionTradeBuilder.tsx` (ATM auto-centre keyed on `[atm, rows.length]`, mirrored `colgroup`), then delete the file — it is imported by nobody |
| Payoff chart | **Write `PayoffSvg.tsx` fresh.** `LivePayoffChart` is recharts with a fixed height and a bbox gradient, and it is live on two other pages; the design wants a reflowing SVG with fills split at the breakeven. Harvest only its ResizeObserver width pattern. |

## Phases

| Phase | Scope | Done when |
|---|---|---|
| **P0** ✅ | Extract the replay market; byte-stability + identity pins | Preflight green; zero changed lines inside `run_intraday_backtest` |
| **P1** | Headless session service: book, staging, commit/discard, charges, settlement, alerts, save/load, `ConsoleState` incl. chain IV/Δ | A pytest drives a whole session — open → stage a bear call spread → commit → step → partial exit → settle — with no HTTP and no frontend |
| **P2** | `routes/console.py`, `types.ts`, `ConsolePage` with session bar / scrubber / market strip / chain (D1 + D8 states + 36px rail); `--oc-*` tokens; IBM Plex; `/console` route + nav; delete `OptionTradeBuilder.tsx` | `/console` opens 2026-04-01 09:16 locally; jog chips move the clock; the chain shows real store LTP/OI/IV/Δ and collapses to the rail |
| **P3** | `PayoffSvg`, net-greeks bar, positions table, staged bar, scenario tiles, the reducer, the keyboard map, the rail | D1 reproduces from store data: staging draws a dotted curve and a before→after bar; Enter commits; Esc discards |
| **P8 (DONE 2026-09-10)** | margin from KITE: `services/console_margin.py` prices a replayed book as today's equivalent structure on Kite's basket API (moneyness-scaled strikes, nearest-DTE listed expiries, today's lot size), labelled "zerodha" with the mapped legs; `manual > zerodha > model`; the manual anchor is a click on the Margin tile; a fresh day opens on its month's expiry chip | verified in Chrome against Priya Kite (iron fly ₹71,155 mapped to 2026-09-22) |
| **P7 (DONE 2026-09-10)** | the rest of the replay track (D6): a 30-minute open-P&L sparkline beside the track (the tape regrouped per symbol once, then bisects — never rebuilds), and an `alert ›` jump that scans the tape forward to the minute an armed target/stop/spot alert would fire (delta alerts excluded — no cheap series) | — |
| **P6 (DONE 2026-09-10)** | the order ticket (D5): Review & commit opens the orders the basket becomes, row by row at the run's mark (BUY/SELL to open/close, lots, qty, MKT, price, cash), net credit/debit, margin impact vs capital, risk before → after, the typed REAL for a real run lives there; LMT shown struck through with the reason (the executor builds the broker order without a price and LiveBroker works its own ladder) | verified on paper run 111, reverted |
| **P5b (DONE 2026-09-10)** | on a running deployment the STAGED book is the one shown everywhere (Positions rows with PENDING badges, payoff, margin, MTM, greeks) until Commit / Revert; an edit to an uncommitted leg edits the staged item; leaving the run with uncommitted changes asks first; the date picker opens at 09:30 | verified on paper run 111, reverted |
| **P5 (DONE 2026-09-09)** | paper/live mode: `console_live.LiveConsole` over a running DERIV deployment — same DTO, live chain + portfolio, staged basket → `LiveRun.manual_order`; source selector on the page, live polling, transport hidden, Apply to paper / typed-REAL Send to broker; engine: `ManualLegOpen.expiry`, partial short cover (`CloseShort.units`, `reduce_short_lot`) | paper run 111 opened in Chrome: legs, chain, margin, staged bar; NOT applied |
| **P4c (DONE 2026-09-09)** | steps roll into the next captured session (a finished cycle pins at its close); cycle bar in NSE sessions with 'data to <last capture>' when the expiry is beyond the store; END OF DATA status; hidden-chain layout = MTM strip + Positions + Alerts left, chart right; the session day loud in the strip; drag-to-zoom; whole-book multiplier (gcd-derived, one undoable action) | — |
| **P4b (DONE 2026-09-09)** | owner review: SPAN-shaped margin (`margin.py`), preset cards reduced to glyphs, leg expiry column + settlement note, MTM as % of margin, payoff at a fixed height with Positions under it, chain-hidden layout = risk+positions left / chart right, wheel zoom | — |
| **P4 (DONE 2026-09-09)** | Δ-anchored presets (gallery = the D3 empty state and a `presets` toggle; 8 cards, refused by name when unbuildable), replay track (fill/alert/bookmark markers, click-to-seek, jump chips ‹fill fill› 1%› ‹◇ ◇›, keys B/J/K), bookmarks, ⤓ save / ⤒ load (`~/.skas_data/console`), session restore after a backend restart/eviction (journal in state), chain collapse ⟨⟨/⟩⟩ (H), D7 breach state (stop fired → red stripe + banner), expiry kept in the URL | verified in Chrome |
| **P3b (DONE 2026-09-09)** | autoplay + labelled speeds (Space; pauses on hidden tab, EOD, error, a fired alert), alerts (target/stop/|Δ|/spot above/below — fire once at the cursor, amber on the chart, re-arm on rewind), per-leg + net greeks in the Live page's convention, T+0 scenario tiles at ±1% | armed → play → paused at the fire minute, verified in Chrome |
| **P4** | Presets (D4), order ticket (D5), replay track (D6), alerts on chart + rail, D7 rail states, autoplay | A preset applies at any spot/expiry; jump-to-event lands on the right minutes; a stop alert fires once and re-arms on rewind |
| **P5** | LIVE/PAPER over an existing `LiveRun`, plus the two platform gaps above | A PAPER `custom_options` run drives the console end to end under fake-adapter tests; the LIVE cell is exercised only by fakes |
| **P6** | D9 1280 reflow; mobile M1–M4 in `web-mobile/` | Both render off the same `ConsoleState` |

**Deferred:** FINNIFTY (needs capture first), true IVR, multi-day carry (v1 is one day at a
time), DB-backed session recovery (save/load is a hand action to `~/.skas_data/console/`),
"as fast as it renders" autoplay, and any SPAN-accurate margin model.

## Testing

`tests/test_options_console.py` (synthetic store in `tmp_path`, reusing
`test_intraday_replay.py`'s fixtures): seek determinism as an identity; a rewind unwinds a
later fill; a staged change moves nothing; commit charges equal the batch replay's; partial
exit books pro rata; a leg past expiry settles to parity intrinsic; alerts fire once and
re-arm; every quoted chain cell reprices its own LTP through BS; an illiquid strike is
marked, never faked; the registry evicts; save/load round-trips; live mode owns no book and
refuses to commit without a run.

`tests/test_options_console_contract.py`: parse `web/src/types.ts` and assert `ConsoleState`
agrees key-for-key with the Python model both ways, plus `pricing.r == RISK_FREE` in
`payoff.ts`. This DTO is the entire coupling surface between a stateful service and a large
page, and a silently-added backend field renders as `undefined` — §9's "the page doesn't
load" failure.

Plus the isolation pin (`services/options_console/**` imports no order path) and
`./scripts/preflight.sh` as the gate.

## Out of scope
- Any change to `live/`, `brokers/` or the order path before P5.
- Touching `skas_algo.db` or the VPS.
- Reworking `/trade?tab=build`; it stays until the console demonstrably replaces it.
