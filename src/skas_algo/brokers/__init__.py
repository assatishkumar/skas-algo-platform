"""Broker abstraction (see docs/PLAN.md → Broker abstraction).

Three implementations land in later phases behind one interface:
  - BacktestBroker  (Phase 1) — simulated fills on historical bars
  - PaperBroker     (Phase 1) — simulated fills on live prices
  - LiveBroker/Zerodha (Phase 4) — real orders, TOTP-automated login
"""

from .base import BrokerAdapter, BrokerOrder, Fill, Funds, Session
from .dhan import DhanAdapter, DhanCredentials
from .sim_broker import BacktestBroker, PaperBroker, SimBroker
from .zerodha import BrokerLoginError, NotArmedError, ZerodhaAdapter, ZerodhaCredentials

__all__ = [
    "BrokerAdapter",
    "BrokerOrder",
    "Fill",
    "Funds",
    "Session",
    "SimBroker",
    "BacktestBroker",
    "PaperBroker",
    "DhanAdapter",
    "DhanCredentials",
    "ZerodhaAdapter",
    "ZerodhaCredentials",
    "BrokerLoginError",
    "NotArmedError",
]
