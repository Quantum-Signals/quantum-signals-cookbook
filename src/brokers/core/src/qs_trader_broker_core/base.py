from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class OrderResult:
    """Broker-agnostic result of a ``place_order`` call.

    Every broker returns this shape so callers never inspect broker-native
    objects. ``price`` is the actual fill price when known (the broker extracts
    it from the underlying order/trade), or ``None`` if the order did not fill in
    time. ``raw`` holds the native result (an ``ib_async.Trade``, an Alpaca order
    dict, etc.) as an escape hatch for broker-specific follow-up.
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


class BrokerBase(ABC):
    """Common interface for live and simulated brokers.

    Each subclass wraps one broker's API — Alpaca, IBKR, or the simulator — and
    exposes it to the strategy layer through this interface. The strategy never
    inspects broker-native objects: it calls ``resolve_contract`` to turn a
    QuantumSignals symbol into an opaque *contract*, then threads that contract
    back through ``get_last_price`` and ``place_order``. Implementers are free
    to make ``contract`` whatever their broker needs (a ticker string, an
    ``ib_async.Contract``, a dataclass); callers only pass it around.

    To add a broker, subclass this and implement all abstract methods. Follow
    the per-method contracts below — especially the ``None`` conventions, which
    the strategy layer relies on to skip untradable symbols and failed orders.
    """

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def resolve_contract(self, qs_symbol: str) -> Optional[object]:
        """Resolve a QuantumSignals symbol to an opaque broker contract.

        Returns an object the strategy passes back to ``get_last_price`` and
        ``place_order`` untouched, or ``None`` when the symbol is **not tradable
        on this broker** — the strategy treats ``None`` as "skip this symbol".

        Behaviour for symbols **not** in ``settings.symbol_map`` is
        broker-specific and intentionally so; document your choice:
          - ``IBBroker`` returns ``None`` (unmapped symbols are skipped), and
            also returns ``None`` for a mapped symbol that fails to qualify.
          - ``AlpacaBroker`` treats an unmapped symbol as a direct ticker, and
            returns ``None`` for mapped non-equity (``secType != "STK"``) specs,
            since Alpaca trades equities/ETFs only.
          - ``SimulatedBroker`` auto-creates a placeholder contract so DRY_RUN
            can exercise any symbol.

        Implementations may cache resolved contracts.
        """
        ...

    @abstractmethod
    def get_last_price(self, contract: object) -> Optional[float]:
        """Latest/last-trade price for *contract*, or ``None`` if unavailable.

        *contract* is whatever ``resolve_contract`` returned. Return ``None``
        (rather than raising) on transient data errors so callers can fall back;
        connection-level failures may raise for the caller's retry logic.
        """
        ...

    @abstractmethod
    def place_order(
        self,
        contract: object,
        action: str,
        qty: int,
        price: Optional[float] = None,
        position_intent: Optional[str] = None,
    ) -> Optional[OrderResult]:
        """Submit an order and return an :class:`OrderResult`, or ``None``.

        Args:
            contract: the opaque object from ``resolve_contract``.
            action: ``"BUY"`` or ``"SELL"`` (case-insensitive).
            qty: order quantity; may be fractional where the broker allows it.
            price: limit price. When ``None`` (or ``settings.use_market_orders``)
                the order is placed at market.
            position_intent: ``"open"`` or ``"close"``, or ``None`` if the
                broker does not need it. Brokers map this to broker-native
                intents (e.g. Alpaca's BUY_TO_OPEN / SELL_TO_CLOSE).

        Returns ``None`` when the order is rejected or otherwise not submitted —
        the strategy treats ``None`` as a failed leg and re-plans on the next
        interval. On success, ``OrderResult.price`` is the fill price when known
        (the broker extracts it from its native result) or ``None`` if the
        order had not filled before the poll timeout.
        """
        ...

    @abstractmethod
    def get_account_summary(self) -> dict: ...

    @abstractmethod
    def get_open_positions(self) -> list[dict]: ...

    @abstractmethod
    def get_positions_for_sync(self) -> list[dict]:
        """Return positions with symbol, signed qty, and average entry price."""
        ...
