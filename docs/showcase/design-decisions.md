# Design decisions

The rules that shaped the platform, each with the reason it exists. Most were paid for.

## 1. Backtest = paper = live, one engine
Only three things swap by mode: the clock, the data feed and the broker adapter. The
strategy, the override resolver, the portfolio, the charges and the reports are shared. Two
parity suites pin it: the same strategy over the same days must produce a byte-identical book
whether driven by the backtester or the paper replay. Every options-only branch is gated on
"a settler exists" so the equity backtest never changes under it.

**Why:** a backtest that runs through different code than the live loop is a story, not a
test. Parity is what lets a new rule ship to live the day its replay looks right.

## 2. Ctor defaults never change meaning; forms carry policy
A running deployment is rebuilt from its persisted parameters on every restart. Redefining
a parameter's meaning would silently change a live position's risk. So a constructor default
always reproduces the old behaviour, and new behaviour goes behind a new flag that the
backtest form and the deploy card default to.

**The two deliberate exceptions**, both owner decisions: every price-driven exit is held
until 09:20 (the first five minutes of the session print spreads of 3 to 7%), and every
target and stop reads exit prices against real fills (a "target" booked on a stale last
trade realised −₹27,280 on one run).

## 3. A refused entry says why
Every entry path used to be a chain of silent returns. Two paper runs sat flat for two
weeks and the operator read it as "did not take off". Now every refusal records a reason and
a day, and the tile shows "Cycle skipped · reason".

## 4. The order path is one class, and it explains itself
`LiveBroker` is the only code that places real orders. It places a LIMIT at the touch,
waits, escalates through a protected limit crossed by a fixed percentage (3% for options,
1% for equity), snaps to the instrument's tick, tries one cancel-and-replace on a
broker-confirmed empty cancel, and then halts. Every lifecycle event is one greppable log
line sharing a correlation id. Absence of a line is not evidence: the trace is designed to be
read before the code.

## 5. Real orders need four keys
Mode LIVE, account armed, a process-level flag, and an adapter with the full order surface.
Any missing key leaves the paper broker in place and the tile wears an "orders PAPER"
chip. A restart demotes to paper by default and re-promotes only after reconciling the
broker's book.

## 6. Thresholds anchor to a stated base
"Two percent of margin" is only meaningful if the margin is stable. Broker basket margin is
not: the same legs priced ₹70k at entry and ₹4.5L on expiry day. So percentage rules read a
frozen base: the broker margin at entry, or a manual anchor the operator measured once, and
the tile names which.

## 7. Marks are honest or absent
A replay marks an option at the last trade, floored at discounted intrinsic, and a print
older than five minutes is not a print: the strategy defers rather than acts. Live, longs
mark at the bid and shorts at the ask. Paper fills at the live spread so a paper cycle
measures execution cost.

## 8. The data is self-owned
Daily 1-minute capture of every in-range index option contract into parquet, mirrored
off-box, with a purchased history import for the years before capture began. The store is
the reason intraday strategies have a backtest at all.

## 9. The manual hand pauses the machine
When a human changes a running strategy's book, the strategy is paused and a small rail
takes over: an optional stop and target on the cycle's real P&L, the hard time exit, expiry
settlement. A strategy's rule on a book it did not build is a different rule; it is not
applied at all. The strategy resumes only on a flat book.

## 10. Every surface shows one number
The tile's P&L, the console's P&L, the cycle table and the report are computed by the same
functions over the same transaction log. When two screens disagreed by ₹8,606, the fix was
to remove the second computation, not to reconcile it.

## 11. The assistant never trades
The AI pair that works on this codebase is under a standing directive: never arm an
account, never set the trading flag, never deploy a live run on an armed account, never
"verify" with a real order. Order-path verification is fake-adapter tests. Every real order
is the operator's hand.
