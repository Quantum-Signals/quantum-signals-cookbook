from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


def normalize_position_side(side: object, *, qty: float = 0.0) -> str:
    """Return ``"long"`` or ``"short"`` from broker position side fields.

    Alpaca-py exposes ``PositionSide`` enums whose ``str()`` is
    ``"PositionSide.SHORT"``, not ``"short"`` — normalize via ``.value``.
    """
    if side is not None and hasattr(side, "value"):
        label = str(side.value).lower()
        if label in ("long", "short"):
            return label
    label = str(side or ("long" if qty >= 0 else "short")).lower()
    if label in ("long", "short"):
        return label
    return "short" if "short" in label else "long"


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
        position_intent: Optional[str] = None,
    ) -> Optional[object]: ...

    @abstractmethod
    def get_account_summary(self) -> dict: ...

    @abstractmethod
    def get_open_positions(self) -> list[dict]: ...

    @abstractmethod
    def get_positions_for_sync(self) -> list[dict]:
        """Return positions with symbol, signed qty, and average entry price."""
        ...
