"""Trading strategies — one module per strategy, all implementing ``StrategyBase``.

Register new strategies in ``app._STRATEGIES`` and select via ``TRADER_STRATEGY``.
"""

from trader.strategy.base import StrategyBase
from trader.strategy.momentum import MomentumStrategy

__all__ = ["StrategyBase", "MomentumStrategy"]
