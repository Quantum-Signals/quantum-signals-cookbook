from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class OrderResult:
    """Broker-agnostic result of a ``place_order`` call.

    Every gateway returns this shape so callers never inspect broker-native
    objects. ``price`` is the actual fill price when known (the gateway extracts
    it from the underlying order/trade), or ``None`` if the order did not fill in
    time. ``raw`` holds the native result (an ``ib_async.Trade``, an Alpaca order
    dict, etc.) as an escape hatch for gateway-specific follow-up.
    """

    id: str
    symbol: str
    action: str
    qty: float
    price: Optional[float]
    status: str
    raw: object = None


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
    ) -> Optional[OrderResult]: ...

    @abstractmethod
    def get_account_summary(self) -> dict: ...

    @abstractmethod
    def get_open_positions(self) -> list[dict]: ...

    @abstractmethod
    def get_positions_for_sync(self) -> list[dict]:
        """Return positions with symbol, signed qty, and average entry price."""
        ...
