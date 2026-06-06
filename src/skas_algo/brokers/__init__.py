"""Broker abstraction (see docs/PLAN.md → Broker abstraction).

Three implementations land in later phases behind one interface:
  - BacktestBroker  (Phase 1) — simulated fills on historical bars
  - PaperBroker     (Phase 1) — simulated fills on live prices
  - LiveBroker/Zerodha (Phase 4) — real orders, TOTP-automated login
"""

from .base import BrokerAdapter, BrokerOrder, Funds, Session

__all__ = ["BrokerAdapter", "BrokerOrder", "Funds", "Session"]
