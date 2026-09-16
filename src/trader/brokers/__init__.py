from __future__ import annotations

from importlib.metadata import entry_points
from typing import Callable

from qs_trader_broker_core.base import BrokerBase

from trader.config import Settings

__all__ = ["BrokerBase", "discover_brokers", "create_broker"]

# Entry-point group under which broker packages advertise their BrokerBase
# subclass. Built-in brokers (alpaca, simulated) register here in this
# package's pyproject.toml; a third-party package can register its own broker
# under the same group and it becomes selectable via TRADER_BROKER=<name> with no
# change to this module. Discovery uses Python's native entry-point mechanism.
BROKER_ENTRY_POINT_GROUP = "qs_trader_brokers"


def discover_brokers() -> dict[str, Callable[[], BrokerBase]]:
    """Return ``{name: broker_factory}`` for every registered broker.

    Each entry point loads to a ``BrokerBase`` subclass (or any no-arg callable
    returning one — brokers self-load their own config). Discovery is dynamic:
    installing a package that registers a ``qs_trader_brokers`` entry point makes
    its broker appear here.
    """
    registry: dict[str, Callable[[], BrokerBase]] = {}
    for ep in entry_points(group=BROKER_ENTRY_POINT_GROUP):
        registry[ep.name] = ep.load()
    return registry


def create_broker(settings: Settings) -> BrokerBase:
    """Factory: select and construct the configured broker.

    ``settings`` is used **only** to choose which broker (``broker``); the app
    passes no configuration into the broker itself. Each broker self-loads its
    own settings from the environment (e.g. ``ALPACA_*``), so the factory
    constructs it with no arguments.

    The ``broker`` setting selects a broker discovered from the
    ``qs_trader_brokers`` entry-point group — ``TRADER_BROKER=simulated`` (the
    default) for no real orders, or ``TRADER_BROKER=alpaca`` etc. to trade for real.
    An unknown broker raises ValueError listing the brokers that are actually
    installed, so a typo in ``TRADER_BROKER`` — or a missing broker package — fails
    loudly at startup.
    """
    registry = discover_brokers()

    name = settings.broker
    try:
        factory = registry[name]
    except KeyError:
        available = ", ".join(sorted(registry)) or "(none installed)"
        raise ValueError(
            f"Unknown TRADER_BROKER={settings.broker!r}. Installed brokers: {available}. "
            "Install the package that provides it, or check for a typo."
        ) from None
    return factory()
