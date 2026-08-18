from __future__ import annotations

from trader.config import Settings
from trader.brokers.alpaca import AlpacaBroker
from trader.brokers.base import BrokerBase
from trader.brokers.ibkr import IBBroker
from trader.brokers.simulated import SimulatedBroker

__all__ = ["BrokerBase", "SimulatedBroker", "IBBroker", "AlpacaBroker", "create_broker"]


def create_broker(settings: Settings) -> BrokerBase:
    """Factory: return the appropriate broker based on settings.

    dry_run=True always wins → SimulatedBroker (no real broker needed).
    broker="alpaca"         → AlpacaBroker (REST, equity/ETF only).
    broker="ibkr" (default) → IBBroker (ib_async, futures + equities).
    """
    if settings.dry_run:
        return SimulatedBroker(settings)
    if settings.broker == "alpaca":
        return AlpacaBroker(settings)
    return IBBroker(settings)
