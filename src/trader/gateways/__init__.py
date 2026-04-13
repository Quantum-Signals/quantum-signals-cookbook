from __future__ import annotations

from trader.config import Settings
from trader.gateways.base import GatewayBase
from trader.gateways.ibkr import IBGateway
from trader.gateways.simulated import SimulatedGateway

__all__ = ["GatewayBase", "SimulatedGateway", "IBGateway", "create_gateway"]


def create_gateway(settings: Settings) -> GatewayBase:
    """Factory: return a SimulatedGateway or IBGateway based on settings."""
    if settings.dry_run:
        return SimulatedGateway(settings)
    return IBGateway(settings)
