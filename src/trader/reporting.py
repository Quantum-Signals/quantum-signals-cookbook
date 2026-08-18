from __future__ import annotations

from loguru import logger

from trader.brokers.base import BrokerBase
from trader.models import PerformanceTracker, Signal


def report_once(broker: BrokerBase, perf: PerformanceTracker) -> None:
    """Log account summary, open positions, and bot performance (single snapshot)."""
    try:
        acct = broker.get_account_summary()
        pos_list = broker.get_open_positions()

        logger.info("=" * 60)
        logger.info(
            f"ACCOUNT  NLV=${acct['net_liquidation']:,.0f}  "
            f"UnrealPnL=${acct['unrealized_pnl']:+,.2f}  "
            f"RealPnL=${acct['realized_pnl']:+,.2f}  "
            f"BuyPow=${acct['buying_power']:,.0f}"
        )
        if pos_list:
            logger.info("POSITIONS:")
            for p in pos_list:
                logger.info(
                    f"  {p['symbol']:8s} qty={p['pos']:+.0f}  "
                    f"avgCost={p['avg_cost']:.4f}  "
                    f"UnrealPnL=${p['unreal_pnl']:+,.2f}"
                )
        logger.info(f"BOT PERF  {perf.summary()}")
        logger.info(
            f"SIGNALS   UP={perf.signal_counts[Signal.UP]}  "
            f"STABLE={perf.signal_counts[Signal.STABLE]}  "
            f"DOWN={perf.signal_counts[Signal.DOWN]}"
        )
        logger.info("=" * 60)
    except Exception as exc:
        logger.warning(f"Reporting error: {exc}")
