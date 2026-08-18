from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class BrokerSettings(Protocol):
    """The configuration surface a broker reads from the host application.

    This is the *only* contract between a broker package and whatever supplies
    its settings. The host app's ``Settings`` dataclass satisfies it structurally
    (no inheritance needed), so broker packages depend on this protocol instead
    of the app — keeping the package graph a clean DAG with no import cycle.

    Only fields actually consumed by a broker belong here. A broker uses the
    subset relevant to it (e.g. ``AlpacaBroker`` never reads the ``ib_*`` fields);
    they are grouped together so one protocol covers every built-in broker.
    """

    # Symbol resolution / pricing (all brokers)
    symbol_map: dict[str, dict]
    sim_prices: dict[str, float]
    use_market_orders: bool

    # Alpaca
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_paper: bool

    # Interactive Brokers
    ib_host: str
    ib_port: int
    ib_client_id: int
    ib_market_data_type: int
