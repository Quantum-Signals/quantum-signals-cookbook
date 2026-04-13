from __future__ import annotations

from abc import ABC, abstractmethod


class StrategyBase(ABC):
    """Interface that all trading strategies must implement."""

    @abstractmethod
    def handle_signal(self, signal: object) -> None:
        """React to a single QuantumSignals signal object."""
        ...
