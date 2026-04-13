from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class GatewayBase(ABC):
    """Common interface for live and simulated brokers."""

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def resolve_contract(self, qs_symbol: str) -> Optional[object]: ...

    @abstractmethod
    def get_last_price(self, contract: object) -> Optional[float]: ...

    @abstractmethod
    def place_order(
        self,
        contract: object,
        action: str,
        qty: int,
        price: Optional[float] = None,
    ) -> Optional[object]: ...

    @abstractmethod
    def get_account_summary(self) -> dict: ...

    @abstractmethod
    def get_open_positions(self) -> list[dict]: ...

    @abstractmethod
    def get_positions_for_sync(self) -> list[dict]:
        """Return positions with symbol, signed qty, and average entry price."""
        ...
