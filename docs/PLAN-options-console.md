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

---
---

## Part 2 — What a hand-edit does to a strategy-managed run (2026-09-10)

> Part 1 above is the console build plan (P0–P8 shipped; kept verbatim, also committed as
> `docs/PLAN-options-console.md`). Part 2 is the question the console's Commit button
> raised: **"if I open an algo-managed run, change some legs and commit, what happens to
> the algo?"** Applies to PAPER and LIVE alike — the code path is identical, only the
> broker differs.

### Context — the question, restated

The strategy is a state machine over *its own* leg list and latches. A manual order
changes the *portfolio* (units held). The question is who owns the legs afterwards and
whether the strategy's logic still means anything on a book it did not build.

### What the code does today (verified 2026-09-10, read-only trace)

Console Commit → `LiveRun.manual_order` → `LiveSession.manual_order`
(`engine/live.py:299-353`) → fills through the run's broker (PaperBroker, or LiveBroker
when every §1 key is set) → **`sync_strategy_book(ts)`** (`engine/live.py:389-426`).

`sync_strategy_book` is ONE generic rebuild: `strat.legs = [{symbol, dir, units, entry}]`
from the portfolio (entry re-based to the realised average fill), plus `entry_expiry` /
`entry_date` only if `None`, plus `_flat()` if empty. It touches nothing else (`phase`,
`cycle_expiry`, `margin_base`, `entered_month`, `cycle`, `sides`, per-leg maps …).

Three incompatible leg models meet that rebuild:

| family | leg model | after a manual order |
|---|---|---|
| ratio (call/put/batman/hni), intraday_straddle, weekly_intraday_straddle | `{symbol, dir, units, entry}` | **adopts** — manual legs join the P&L, `_exit_all` closes them. Caveats: frozen margin not re-frozen; a manual leg with no print freezes ALL exits (`all(has_print)`); a manual open on a FLAT run resurrects a finished cycle; weekly straddle's VWAP still watches `cycle["strike"]`, so a hand-roll exits the new legs on the old strike's signal. |
| delta family (delta_neutral, iron_fly, fair_value_calendar, volcano, double_diagonal, monthly_butterfly) | `{symbol, right, dir, units, entry, fill_seen, entry_fill}` | rebuild **drops `right`** → the first slice that reaches a roll / adjust / `_adopt_settled` branch raises `KeyError: 'right'` (`delta_neutral_monthly.py:861`, `:1031`, `:1088` …). The tick loop swallows it (`manager.py:2987`): **no `order_error`, no banner, decisions silently dead — including the stop.** (P&L exits sit above the adjust branch, so a target/stop firing on the SAME slice still exits; hard time exits survive.) |
| custom_options | `list[str]` | rebuild writes dicts into a string list → `TypeError: unhashable type: 'dict'` on every slice, **persisted** by `export_state` so a restart does not heal it. Per-leg maps (`entry_close`, `units`, `leg_index`) are never updated either. |
| donchian_strangle_monthly | `list[str]` + own `sync_to_book` | prunes closed legs; a manual OPEN is invisible forever (by design). |
| intraday_strangle_combo | no `legs` attr (`sides[u][right]`) | sync is a no-op; a manual close drops out via `ctx.lots`; a manual OPEN is entirely unmanaged (no EOD square-off, no MTM stop). |

Two structural facts underneath: **`Lot` has no provenance** (`engine/portfolio.py:30`;
the `MANUAL` tag lives only on the trade event and is display-only — no `tag ==` anywhere
in engine/strategies/reconciler/snapshot), and **EXIT_ALL resolves by SYMBOL to every lot
record** (`overrides.py:149-158`), so a manual leg on a strategy's contract is merged by
construction — the broker nets per contract anyway. Reconciliation counts manual legs
like any other (correct: the platform book must equal the broker's). No snapshot field or
tile says "this run was hand-edited"; only the trade row's tag badge and a server journal
line record it. Tests cover manual orders ONLY on `hni_weekly` (the one family the
rebuild fits): `tests/test_live_options.py`, `tests/test_console_live.py`.

### First-principles floor (what is true regardless of design)

1. LAW — the broker's book is per contract; two lots on one contract are one position.
   "Coexist on the same symbol" is impossible; only different-symbol coexistence is.
2. LAW — a strategy's exits are functions of ITS leg list and entry prices; a threshold on
   a book it did not build is a different rule, not the same rule on more legs.
3. MEASURED — 4 of 6 strategy families cannot represent a hand-edited book (table above);
   two of them fail silently today, on LIVE as on paper.
4. MEASURED — the only whole-book exits that exist outside a strategy are `flatten` and
   `adopt_broker_close`; there is no generic stop for a book nobody manages.
5. RULE (owner, CLAUDE.md §1) — an unmanaged live position with no stop is the failure
   mode the platform exists to prevent.

**Load-bearing assumption to settle with the owner:** what the algo should DO after an
edit — adopt the merged book, or step aside. The answer is different per family, so the
honest design is a per-strategy DECLARED contract, never one generic rebuild.

### The contract (owner decision, 2026-09-10): **ALWAYS HAND OVER**

A manual order that leaves a run holding positions ends algorithmic management of that
run. The strategy is PAUSED, the book goes onto a generic **manual rail**, and every
screen says so. No family adopts, no family guesses. A manual order that flattens the
book keeps today's flat semantics (the strategy sees flat; its own latches decide
whether it may re-enter — the smoke-test rule in CLAUDE.md), because a flat book has no
structure to disagree about.

### 1. The handover itself (`engine/live.py`)
- `LiveSession.manual_order` (and `adopt_broker_close`; `flatten` only when it leaves lots)
  ends with `self._hand_over(ts, reason)` instead of `sync_strategy_book` whenever the
  portfolio is non-empty afterwards:
  - `self.paused_strategy = self.strategy`, `self.strategy = ManualBookStrategy.from_book(
    portfolio, underlying, rail_params)`, `self.managed_by = "manual"`,
    `self.handover = {"at": ts, "reason": "manual_order", "strategy_id": …}`.
  - `sync_strategy_book` is no longer called on a non-flat book (it stays for the flat
    branch: `legs = []` + `_flat()`), so the delta/custom_options corruption cannot happen.
- `export_state` gains `managed_by`, `handover`, `paused_strategy` (the paused strategy's
  own `export_state()`); `load_state` rebuilds the rail from the portfolio and re-hydrates
  the paused strategy from `paused_strategy` (recovery.py `_rebuild` builds the ORIGINAL
  strategy from `params_snapshot` as today, then `load_state` swaps it into
  `paused_strategy` and installs the rail — no recovery-code change beyond that).
- `_rewire` hooks the manager applies to `session.strategy` (`set_broker_margin`,
  `set_index_spot`, quote-source wiring in `_wire_quote_source`, `_tag_underlying_spot`)
  must reach the rail: it implements the same duck-typed surface (`legs`, `underlying`,
  `set_broker_margin`, `margin_base`/`margin_source`, `export_state`, `basket_status`,
  `exit_rules`, `strategy_alert`). `update_params` refuses on a handed-over run except for
  the rail's own knobs (`stop_pct`, `target_pct`, `time_exit`).

### 2. `ManualBookStrategy` (`strategies/manual_book.py`) — the rail
Built from `custom_options`' MANAGEMENT half (its `_manage` is already leg target/stop +
basket target/stop + spot bands; only its entry half and string/dict maps are replaced):
- state from the book: `legs` = symbols held, `entry` = the lots' average price, `units`,
  side from `direction`; all re-derived from the portfolio on every slice so a later
  manual edit needs no sync at all (the rail never "owns" anything).
- **stop**: `stop_pct` of the margin anchor (`margin_base` = last broker push, or the
  paused strategy's frozen `margin_base`/`margin_per_set` where it has one — same
  `_threshold_anchor` probe the tile uses); **target**: `target_pct`, off by default;
  **time exit**: inherited from the paused strategy when it is an intraday deck
  (`exit_time` / `eod_exit` / `cycle_exit_time` probed by `getattr`), else none;
  expiry settlement is the engine's (`settle_expiries`) and needs nothing.
- Defaults at handover: `stop_pct` = the paused strategy's own stop where it declares one
  (`stop_loss_pct` in %-of-margin families), else **OFF with a loud amber "NO STOP" chip**
  — the platform must never place an order the owner did not ask for on a book they just
  took by hand; the console and tile make the absence unmissable and one click sets it.
- Exits are ordinary `EXIT_ALL` signals per symbol through `SliceExecutor`, so charges,
  trade events (`reason="rail_stop"` etc., tag `RAIL`), reconcile and the §1 order gates
  are byte-identical to a strategy exit. `OpenSettleGuard` applies (no exit before 09:20).
- Hot-editable via the tile's Edit params and settable from the console's Alerts card in
  live mode (a ₹ stop/target armed on a handed-over run writes the rail, not a console
  alert).

### 3. The strategy exception is a visible halt (independent, ship first)
`LiveRun.run_decision`: an exception from `strategy.on_slice` while the book is non-flat
sets `self.strategy_error = f"{type}: {msg}"` with `order_error` semantics (decisions halt,
banner + tile chip + one push, `POST /live/{id}/ack-strategy-error`), logged as one
`STRATEGY error run=… exc=…` line. Today it is swallowed at `manager.py:2987` and a run can
tick for weeks with a dead stop. Pinned by `test_a_strategy_exception_halts_visibly`.

### 4. Resume
"Resume strategy" (`POST /live/{id}/resume-strategy`, tile menu + console) is offered ONLY
when the book is FLAT: the paused strategy is reinstalled, its `legs` cleared, its latches
untouched (so a monthly does not re-enter the same cycle, an intraday does not re-enter
the same day). On a non-flat book the button explains why it is disabled. No family
re-derives state from a foreign book — that is the whole point of the decision.

### 5. Provenance (small, additive)
`Lot.tag` (default `"STRATEGY"`; every existing constructor path unchanged, parity suites
pin it). Manual lots carry `"MANUAL"`, rail exits `"RAIL"`. The snapshot's `positions[]`
gains `tag`, the Live tile and the console's Positions badge foreign lots. Reconciliation
is untouched (it must count everything).

### 6. Surfaces
- Snapshot: `managed_by`, `handover{at, reason, strategy_id}`, `rail{stop_pct, target_pct,
  time_exit, anchor, no_stop}`, `strategy_error`. Persisted; survives restart.
- Live tile: chip **"MANUAL · <strategy> paused 11:04"** (amber), red **"NO STOP"** when the
  rail has none; KPI band prints the rail's rules under "exit"; menu gains Resume strategy.
- Console (live mode): the ticket's first line before Commit — *"Committing hands this book
  over: volcano_calendar pauses, the manual rail takes it (stop 2% of ₹1,34,612 · exit
  15:15). Resume is possible once flat."* Typed REAL unchanged. Positions badges MANUAL lots.
- Mobile: `managed_by` + `strategy_error` in the snapshot it already reads; chip only.
- `/live/summary` + alerts: one WARNING alert at handover, one at resume.

### Files (representative)
- `engine/live.py` — `_hand_over`, `manual_order`/`adopt_broker_close`/`flatten` tail,
  `export_state`/`load_state`, snapshot `managed_by`
- `strategies/manual_book.py` (new) + registry entry (`_DEPLOYS_ELSEWHERE`: never deployed
  directly — installed only by a handover); `strategies/custom_options.py` (factor the
  management half into a shared mixin, byte-identical behaviour)
- `live/manager.py` — `strategy_error` (mirror the `order_error` plumbing at :486/:1346/
  :1665/:2933), `update_params` guard, rewire hooks reach the rail, resume route
- `api/routes/live.py` — `ack-strategy-error`, `resume-strategy`; `api/models.py`
- `engine/portfolio.py::Lot.tag`; `engine/execution.py` carries the tag into events
- `services/console_live.py` (ticket consequence line, rail in `risk`, alerts → rail),
  `web/src/pages/ConsolePage.tsx`, `LivePage.tsx` tile chips + menu, `types.ts`,
  `web-mobile` snapshot fields
- Docs: CLAUDE.md §1 (the handover rule — a fresh session must not "fix" the delta family
  to adopt), §8d console; FEATURES §3 entry for the rail; the console guide.

### Tests
- A manual order against EVERY family on a fake broker: after commit `managed_by ==
  "manual"`, the paused strategy is intact (its `export_state` unchanged), the next slice
  raises nothing, the rail's stop fires on a marked-down book and closes every lot
  (including a manual one on the strategy's own strike).
- Flatten by hand → strategy stays installed, `legs == []`, no re-entry that cycle (the
  existing smoke-test pin).
- Recovery round-trip: `export_state` → `load_state` reinstalls rail + paused strategy.
- `test_a_strategy_exception_halts_visibly`; `test_resume_is_refused_on_a_held_book`.
- Parity/mode-equivalence suites green (Lot.tag default, custom_options refactor).

### Verification
- `./scripts/preflight.sh` green.
- Paper run in Chrome (never a LIVE armed run, §1): open volcano run 282 in the console,
  resize one leg, Commit → tile reads "MANUAL · volcano_calendar paused", NO STOP chip until
  a stop is set, next slice logs no exception; flatten from the tile → Resume strategy
  enabled → strategy reinstalled, tile normal.
- Kill-switch check: `order_error` / ack still work on a handed-over run.

### Out of scope here
- Adopt-style re-derivation for any family (explicitly rejected by the owner).
- Per-leg rules richer than custom_options' (leg target/stop, basket target/stop, bands).


**Status (2026-09-10): SHIPPED** — see CLAUDE.md §1 "always hand over" and `tests/test_handover.py`.


---
---

# Part 3 — The Simulator, the record and the coach (2026-09-11 → 2026-09-15, shipped)

- **Simulator** (`/simulator`, `services/simulator.py`): a backtest whose decisions are the
  owner's — an `Algo(strategy_id="manual_sim")` with ONE `AlgoRun` whose trade_log grows as
  cycles are banked and whose metrics is the standard report; the ledger in
  `AlgoRun.state["sim"]`. Owner decisions: flat-based cycles, compounding, hidden from
  Runs, the Bank sheet prompts automatically. Console `?sim=<id>` autosaves the tape.
- **The record** (owner: "log each and every detail… so Claude can analyze every
  adjustment"): a decision context stamped on every action group (spot, DTE, VIX, ATM IV,
  IV30 + rank, greeks, MTM, margin, payoff shape from `options_console/payoff.py`), the
  undo trace (`session.discarded`), the cycle path (`cycle_path`: daily MTM, MAE/MFE, exit
  vs best), actions labelled by shape with the owner's post-hoc `why`, alerts, and a
  Markdown dossier (`GET /simulator/{id}/dossier`, `skas-algo sim-dossier`).
- **The coach** (owner chose all of): the what-if panel (`options_console/whatif.py` —
  candidates priced at the cursor: close / half / roll the TESTED short out, roll the
  UNTESTED short in, match the tested side's premium, a wing on every naked short,
  flatten; ranked by max loss with a tested-side buffer on every row), post-cycle
  counterfactuals (stops / targets / trail / entry-only / each adjustment removed, over
  the cycle's own tape), cross-cycle patterns (≥5 cycles), and a Claude review by hand
  from the dossier. Deterministic arithmetic, never advice.
- Also shipped in the same window: "payoff on" for multi-expiry books, opposite-side clicks
  NET the contract, replay sessions written through to disk and rebuilt under the same id,
  the broker's available margin + a hedge-exit warning on the live ticket (2026-09-18).
- Invariants: CLAUDE.md §8d (console) and §8e (Simulator). Coverage:
  `tests/test_options_console.py`, `tests/test_simulator.py`, `tests/test_console_live.py`.

**Deferred by owner decision:** the mobile console screens, FINNIFTY (never captured), a
SPAN replica (Kite's figure instead), re-centre / add-a-short what-if candidates, a rank
toggle. **Closed 2026-09-18:** the `why` box on the OPEN cycle in the console (an
"actions this cycle" list under Positions in replay/SIM, each group with its box, saved through
the session's annotate verb and carried into the bank) and the console guide artifact.
Nothing on the original plan remains open; what is left is deferred by owner decision.
