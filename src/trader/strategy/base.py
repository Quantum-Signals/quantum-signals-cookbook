from __future__ import annotations

from abc import ABC, abstractmethod


class StrategyBase(ABC):
    """Interface that all trading strategies must implement."""

    @abstractmethod
    def handle_signal(self, signal: object) -> None:
        """React to a single QuantumSignals signal object."""
        ...

    def tick(self) -> None:
        """Called periodically from the main loop for time-driven work."""

    def reset_for_new_day(self) -> None:
        """Clear per-day state before a fresh trading session.

        Called by the app's multi-day supervisor *after* positions have been
        flattened and the daily summary has been logged. The default
        implementation is a no-op; strategies override to clear their own
        accumulators (net qty, decisions, last-exec timestamps, etc.).
        """
