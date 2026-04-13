from __future__ import annotations

import threading
import time

from loguru import logger

from trader.gateways.base import GatewayBase
from trader.models import PerformanceTracker, Signal


def report_once(gw: GatewayBase, perf: PerformanceTracker) -> None:
    """Log account summary, open positions, and bot performance (single snapshot)."""
    try:
        acct = gw.get_account_summary()
        pos_list = gw.get_open_positions()

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


def reporting_loop(
    gw: GatewayBase,
    perf: PerformanceTracker,
    stop_event: threading.Event,
) -> None:
    """Log account summary and bot performance every 60 seconds (background thread).

    Prefer calling :func:`report_once` from the main thread when using ib_async
    with the same thread as ``connect()``.
    """
    while not stop_event.is_set():
        time.sleep(60)
        if stop_event.is_set():
            break
        report_once(gw, perf)
