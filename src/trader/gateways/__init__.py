from __future__ import annotations

from trader.config import Settings
from trader.gateways.alpaca import AlpacaGateway
from trader.gateways.base import GatewayBase
from trader.gateways.ibkr import IBGateway
from trader.gateways.simulated import SimulatedGateway

__all__ = ["GatewayBase", "SimulatedGateway", "IBGateway", "AlpacaGateway", "create_gateway"]


def create_gateway(settings: Settings) -> GatewayBase:
    """Factory: return the appropriate gateway based on settings.

    dry_run=True always wins → SimulatedGateway (no real broker needed).
    broker="alpaca"         → AlpacaGateway (REST, equity/ETF only).
    broker="ibkr" (default) → IBGateway (ib_async, futures + equities).
    """
    if settings.dry_run:
        return SimulatedGateway(settings)
    if settings.broker == "alpaca":
        return AlpacaGateway(settings)
    return IBGateway(settings)
