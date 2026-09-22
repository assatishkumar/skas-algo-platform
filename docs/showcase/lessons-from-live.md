# Lessons from live

Incidents from trading real money, in the order they happened, and what each one changed
in the code. Figures are per-lot and illustrative; the point is the mechanism.

## The exchange rejected a naked market order (July 2026)
A straddle square-off escalated from a resting limit to a MARKET modify. Zerodha's API
rejects market orders on options without market protection; one leg cancelled and the run
halted holding the other. **Change:** escalation re-prices to a protected LIMIT crossed
by a fixed percentage of a fresh touch; never MARKET while a touch exists. Partial fills
are persisted before a halt.

## Orders kept a dead token (July 2026)
The daily broker session rollover rebuilt the quote source with a fresh token while the
order broker kept the old one. Reads worked all day; the 15:15 calendar exit failed
authentication and the run halted. **Change:** every quote-source rebuild rebinds the order
adapter, and a five-minute sweep detects token drift directly.

## Reconciliation fired at 04:50 (July 2026)
An expired overnight token made the broker-book read fail, and a failed read looked like a
mismatch. **Change:** a failed read is transient and retried; only a genuine quantity
mismatch halts. Reconciliation runs in market hours only.

## The margin number was three numbers (August to September 2026)
The tile showed the broker's live basket margin, the strategy measured its target against a
frozen entry margin, and the deploy form asked for capital. The same "5% target" meant
different rupees on different days. **Change:** thresholds read a stated anchor, the tile
prints it beside the live margin, and the exit callout names the arithmetic.

## A paper target that lost money (September 2026)
A butterfly forced at the first tick booked a +3% target on last-traded prices two minutes
later; the book realised −₹9,765 after paying the 09:15 spreads twice. **Changes:** no
price-driven exit before 09:20; entries and marks read the real fills and the exit side of
the book; a spread gate can refuse an entry whose legs are too wide.

## Cash equity settles T+1 (August 2026)
A daily-drip strategy sold an ETF and spent the proceeds in the same decision. Correct in a
backtest, impossible live: the broker balance was ₹146 and the second buy was rejected for
want of ₹83. **Change:** a settlement ledger with pending credits aged on the trading
calendar; buys come from settled cash and the funding sale runs a day ahead.

## An entry that could not be paid halted the run (September 2026)
A momentum strategy sized off its own ledger, which live was whatever capital was typed.
**Change:** an entry the settled cash cannot cover is queued with a banner naming the rupees
to add, and re-sized at the next decision while the signal holds.

## The re-price that never landed (8 and 21 September 2026)
A ₹2,300 stock's +1% re-price was rejected by the exchange, then a ₹1,165 stock's, with
"the order price is not a multiple of the tick size". The ladder snapped every price to
₹0.05; NSE's equity tick is price-banded and those stocks tick in ₹0.10. The first time the
trace could not show it; the second time it could. **Change:** the re-price snaps to the
instrument's tick from the broker's master, else the price band, never finer than the
exchange allows.

## A dev-only import took the live loop down (22 September 2026)
A new module imported a package that was only in the dev extras. The server-side deploy
installs runtime dependencies only, so the API crash-looped 37 minutes before the open
until a hand install. **Change:** the dependency is declared, the import is lazy, and the
rule is written down where the next deploy will read it.

## What the pattern says
Every one of these was invisible in a backtest and cheap in paper. The platform's answer
each time was the same: make the mechanism visible (a trace, a chip, a reason), make the
failure halt rather than continue, and add the case to the test suite so it cannot return.
