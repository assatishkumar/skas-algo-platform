# skas-algo-platform

Single-user algorithmic trading platform: **backtest, forward-test (paper), and live** trading from
**one engine / one strategy codebase** across stocks and derivatives. Browser PWA UI (installable on
iOS), per-position rule overrides + live intervention, TOTP-automated broker login (Zerodha first,
broker-abstracted), and push + Telegram alerts.

The first strategy is **SST / SST-LIFO** (ported from `skas-trading`). Market data and historical
caching are provided by the sibling `skas-data` package.

> Core design rule: backtest = forward-test = live. Only the **Clock**, **DataFeed**, and
> **BrokerAdapter** swap by mode — everything else (strategy, overrides, risk, reporting) is shared.

See the full implementation plan in [`docs/PLAN.md`](docs/PLAN.md).

## Status

Phase 0 — scaffolding. Not yet functional.
