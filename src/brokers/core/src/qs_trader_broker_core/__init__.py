"""Shared broker interface for the QuantumSignals live trader.

Every broker package depends on this package for the ``BrokerBase`` ABI and
returns ``OrderResult`` from ``place_order``. The always-available
``SimulatedBroker`` (used for ``DRY_RUN``) ships here too, since it has no
external broker dependency.
"""

from __future__ import annotations

from qs_trader_broker_core.base import (
    BrokerBase,
    OrderResult,
    normalize_position_side,
)
from qs_trader_broker_core.settings import SimulatedSettings
from qs_trader_broker_core.simulated import SimContract, SimulatedBroker

__all__ = [
    "BrokerBase",
    "OrderResult",
    "normalize_position_side",
    "SimContract",
    "SimulatedBroker",
    "SimulatedSettings",
]
