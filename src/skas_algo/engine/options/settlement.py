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


class ExpirySettler:
    def __init__(self, spot_provider: SpotProvider, lot_overrides: dict | None = None):
        self.spot_provider = spot_provider
        self.lot_overrides = lot_overrides

    def settle(self, ts: date | datetime, portfolio) -> list[dict]:
        """Settle every held option lot whose expiry is on/before ``ts``.

        Realizes each lot at intrinsic value (short → buy_to_close, long → close_lot)
        and returns ``SETTLE`` trade events. Non-option symbols are ignored, so an
        equity portfolio produces no events.
        """
        today = ts.date() if isinstance(ts, datetime) else ts
        events: list[dict] = []
        for symbol in list(portfolio.lot_symbols()):
            inst = parse(symbol, lot_overrides=self.lot_overrides)
            if inst is None or inst.expiry > today:
                continue
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
