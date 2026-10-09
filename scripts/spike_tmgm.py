"""Spike the TMGM demo through the local MetaTrader 5 terminal.

Connects with ``TmgmBroker``, prints the account, and requests a tick for each
mapped symbol. ``--order`` sends one minimum-lot demo buy on US500 and flattens
it. A live account is refused unless ``TMGM_ALLOW_LIVE=true``.
"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv
from loguru import logger

from qs_trader_broker_tmgm import TmgmBroker
from qs_trader_symbols import tmgm_symbol_map


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--order",
        action="store_true",
        help="Place one 0.01-lot US500 demo buy, then flatten it",
    )
    args = parser.parse_args()

    load_dotenv()
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    broker = TmgmBroker()
    broker.connect()
    try:
        summary = broker.get_account_summary()
        logger.info(
            "Account equity={net_liquidation:,.2f}  free_margin={buying_power:,.2f}  "
            "unrealized={unrealized_pnl:,.2f}".format(**summary)
        )
        seen: set[str] = set()
        for symbol in tmgm_symbol_map().values():
            if symbol in seen:
                continue
            seen.add(symbol)
            price = broker.get_last_price(symbol)
            logger.info(f"{symbol}: {price}")

        if not args.order:
            return 0

        opened = broker.place_order("US500", "BUY", 0.01, position_intent="open")
        if opened is None:
            logger.error("Demo order was not filled")
            return 1
        logger.info(f"Opened {opened.qty} {opened.symbol} @ {opened.price} id={opened.id}")
        closed = broker.place_order("US500", "SELL", opened.qty, position_intent="close")
        if closed is None:
            logger.error("Flatten was not filled; a demo position may still be open")
            return 1
        logger.info(f"Flattened {closed.qty} {closed.symbol} @ {closed.price} id={closed.id}")
        return 0
    finally:
        broker.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
