"""F&O (options) transaction charges — Zerodha / NSE schedule.

Mirrors Zerodha's brokerage calculator for NSE option trades (current 2024-25 rates):
  * Brokerage  : flat ₹20 per executed order (₹0 on expiry settlement).
  * STT        : 0.1% on the SELL-side premium (sell-to-open / sell-to-close).
  * Exchange   : 0.03503% of premium turnover (every option leg).
  * SEBI       : ₹10 per crore (0.0001%) of turnover.
  * Stamp duty : 0.003% on the BUY-side premium (buy-to-open / buy-to-close).
  * GST        : 18% on (brokerage + exchange + SEBI).

Applied to each option trade event (``amount`` = units × price). Equities are never
charged here (the engine only attaches a ``charge_model`` for options runs). Income tax
is intentionally NOT modelled for options — F&O is business income taxed at slab rates
(~0 for the small trader), so options backtests run with ``tax_rate=0`` and these
transaction charges are the real cost.
"""

from __future__ import annotations

from dataclasses import dataclass

_BUY_SIDE = {"BUY", "AVG_BUY", "COVER"}   # buy-to-open / buy-to-close
_SELL_SIDE = {"SHORT", "SELL"}            # sell-to-open / sell-to-close
_ORDER_ACTIONS = _BUY_SIDE | _SELL_SIDE   # everything except expiry SETTLE pays brokerage


@dataclass(frozen=True)
class ChargeRates:
    brokerage_per_order: float = 20.0
    stt_sell_pct: float = 0.001          # 0.1% on sell-side premium
    exchange_txn_pct: float = 0.0003503  # 0.03503% of premium turnover (NSE)
    sebi_pct: float = 0.000001           # ₹10 / crore
    stamp_buy_pct: float = 0.00003       # 0.003% on buy-side premium
    gst_pct: float = 0.18


DEFAULT_RATES = ChargeRates()


def charges_for_txn(ev: dict, rates: ChargeRates = DEFAULT_RATES) -> dict:
    """Charge breakdown for one option trade event ({action, amount=units*price})."""
    amount = abs(float(ev.get("amount", 0.0)))
    act = ev.get("action", "")
    brokerage = rates.brokerage_per_order if act in _ORDER_ACTIONS else 0.0
    stt = rates.stt_sell_pct * amount if act in _SELL_SIDE else 0.0
    exchange = rates.exchange_txn_pct * amount
    sebi = rates.sebi_pct * amount
    stamp = rates.stamp_buy_pct * amount if act in _BUY_SIDE else 0.0
    gst = rates.gst_pct * (brokerage + exchange + sebi)
    total = brokerage + stt + exchange + sebi + stamp + gst
    return {
        "brokerage": brokerage, "stt": stt, "exchange": exchange,
        "sebi": sebi, "stamp": stamp, "gst": gst, "total": total,
    }


class ChargeModel:
    """Deducts F&O charges from cash at execution (so the equity curve is net of charges)."""

    def __init__(self, rates: ChargeRates | None = None):
        self.rates = rates or DEFAULT_RATES
        self.total_charged = 0.0

    def charge_for(self, ev: dict) -> float:
        c = charges_for_txn(ev, self.rates)["total"]
        self.total_charged += c
        return c
