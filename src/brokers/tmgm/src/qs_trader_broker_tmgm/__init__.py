"""TMGM broker for the QuantumSignals live trader (local MetaTrader 5)."""

from __future__ import annotations

from .broker import TmgmBroker
from .settings import TmgmSettings

__all__ = ["TmgmBroker", "TmgmSettings"]
