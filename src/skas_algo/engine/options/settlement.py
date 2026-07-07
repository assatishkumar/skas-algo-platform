"""Expiry settlement for European, cash-settled index options.

At/after expiry a held option contract is force-settled to its intrinsic value vs the
underlying's settlement (index close on expiry day) — no roll, just realization. The
same ``ExpirySettler`` is invoked by both the backtest runner and the live session
(via ``SliceExecutor.settle_expiries``) so settlement is identical across modes.

``spot_provider(underlying, on_date) -> float | None`` supplies the underlying spot
(the platform backs this with the cached ``NIFTY 50`` / ``NIFTY BANK`` index series).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Callable

from skas_algo.engine.execution import _as_date, trade_event
from skas_algo.engine.options.black_scholes import intrinsic
from skas_algo.engine.options.instrument import parse

SpotProvider = Callable[[str, date], "float | None"]


# NSE options cease trading 15:30 IST on expiry day; settlement realizes after that.
_EXPIRY_CUTOFF_HHMM = (15, 30)


class ExpirySettler:
    def __init__(self, spot_provider: SpotProvider, lot_overrides: dict | None = None,
                 live_spot_fn=None):
        self.spot_provider = spot_provider
        self.lot_overrides = lot_overrides
        # Live runs: fn(underlying) -> live index spot. Settlement on expiry day must
        # price intrinsic off the REAL spot — the cached series only has yesterday's
        # close, which mid-day mis-settled run 200's 0DTE legs at phantom intrinsics
        # (24500 PE "settled" ₹324 vs a true ₹50, 2026-07-07).
        self.live_spot_fn = live_spot_fn

    def settle(self, ts: date | datetime, portfolio) -> list[dict]:
        """Settle every held option lot whose expiry is on/before ``ts``.

        Realizes each lot at intrinsic value (short → buy_to_close, long → close_lot)
        and returns ``SETTLE`` trade events. Non-option symbols are ignored, so an
        equity portfolio produces no events.
        """
        today = ts.date() if isinstance(ts, datetime) else ts
        # LIVE intraday guard: with a real clock (datetime carrying a time-of-day — the
        # backtest's daily slices are midnight timestamps and keep the old settle-on-
        # expiry-day semantics BYTE-IDENTICALLY), a contract expiring TODAY is still
        # alive until 15:30. Settling it at the morning's first decision force-closed
        # run 200's freshly-sold 0DTE legs (2026-07-07).
        intraday = isinstance(ts, datetime) and (ts.hour, ts.minute, ts.second) != (0, 0, 0)
        cutoff_passed = (not intraday) or (ts.hour, ts.minute) >= _EXPIRY_CUTOFF_HHMM
        events: list[dict] = []
        for symbol in list(portfolio.lot_symbols()):
            inst = parse(symbol, lot_overrides=self.lot_overrides)
            if inst is None or inst.expiry > today:
                continue
            if inst.expiry == today and not cutoff_passed:
                continue  # 0DTE, market still open — the strategy owns it until 15:30
            spot = None
            if self.live_spot_fn is not None:
                try:
                    spot = self.live_spot_fn(inst.underlying)
                except Exception:  # pragma: no cover - fall through to the cache
                    spot = None
            if spot is None:
                spot = self.spot_provider(inst.underlying, inst.expiry)
            if spot is None:
                # No settlement spot available — leave the lot; caller can warn/retry.
                continue
            settle_px = intrinsic(inst.right, spot, inst.strike)
            for lot in portfolio.lots(symbol):
                if lot.direction == -1:
                    profit = portfolio.buy_to_close(symbol, lot.id, settle_px)
                else:
                    profit = portfolio.close_lot(symbol, lot.id, settle_px)
                pnl_pct = (
                    (lot.price - settle_px) / lot.price if lot.direction == -1 and lot.price
                    else (settle_px - lot.price) / lot.price if lot.price else 0.0
                )
                events.append(trade_event(
                    ts, symbol, "SETTLE", lot.units, settle_px, profit, pnl_pct,
                    len(portfolio.lots(symbol)), "EXPIRY",
                    exit_reason="expiry",
                    entry_premium=lot.price,
                    holding_days=(_as_date(ts) - _as_date(lot.opened_at)).days,
                ))
        return events
