#!/usr/bin/env python3
"""
Mimic Alpaca's fractional dust + sell_to_close / sell_to_open rules to test
the flat→short fix without waiting for a live session.

Reproduces the June 22 2026 QQQ failure:
  - strategy net ≈ +0.0001, reserve ≈ -0.0886
  - broker holds 0.000099994 long QQQ
  - OLD behaviour: one sell_to_close for ~1.0001 shares → rejected (40310000)
  - FIXED behaviour: close dust, then sell_to_open 1 share → succeeds

Run:
    uv run python scripts/test_flat_to_short.py
    uv run python scripts/test_flat_to_short.py -v
"""

from __future__ import annotations

import argparse
import os
import sys

from loguru import logger

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from trader.config import Settings
from trader.gateways.simulated import SimContract, SimulatedGateway
from trader.models import PerformanceTracker, Signal
from trader.strategy.momentum import MomentumStrategy


class AlpacaLikeGateway(SimulatedGateway):
    """Simulated broker that rejects orders the way Alpaca does for dust longs."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.orders: list[dict] = []

    def place_order(
        self,
        contract: SimContract,
        action: str,
        qty: float,
        price: float | None = None,
        position_intent: str | None = None,
    ) -> dict | None:
        symbol = contract.symbol
        pos = self._sim_positions.get(symbol, {"pos": 0.0})["pos"]
        long_available = max(0.0, pos)

        if action.upper() == "SELL":
            intent = position_intent or "inferred"
            if position_intent == "close" and qty > long_available + 1e-9:
                logger.error(
                    f"[Alpaca-mock] REJECTED {symbol}: insufficient qty available "
                    f"(requested: {qty}, available: {long_available}, intent=close)"
                )
                return None
            if position_intent is None and long_available > 1e-9 and qty > long_available + 1e-9:
                logger.error(
                    f"[Alpaca-mock] REJECTED {symbol}: insufficient qty available "
                    f"(requested: {qty}, available: {long_available}, intent=inferred-close)"
                )
                return None

        fill_price = price or self.get_last_price(contract) or 0.0
        order = {
            "symbol": symbol,
            "action": action.upper(),
            "qty": qty,
            "price": fill_price,
            "position_intent": position_intent,
            "status": "FILLED",
        }
        self.orders.append(order)

        cur = self._sim_positions.get(symbol, {"pos": 0.0, "avg_cost": fill_price})
        if action.upper() == "BUY":
            cur["pos"] += qty
        else:
            cur["pos"] -= qty
        cur["avg_cost"] = fill_price

        if abs(cur["pos"]) < 1e-12:
            self._sim_positions.pop(symbol, None)
        else:
            self._sim_positions[symbol] = cur

        intent_label = position_intent or "none"
        logger.info(
            f"[Alpaca-mock] FILLED {action.upper()} {qty}x {symbol} "
            f"@ {fill_price:.2f}  intent={intent_label}  broker_pos={cur.get('pos', 0.0)}"
        )
        return order


def _make_strategy(gw: AlpacaLikeGateway) -> MomentumStrategy:
    settings = Settings(
        qs_api_key="test",
        broker="alpaca",
        dry_run=False,
        allowed_symbols=["Nasdaq100"],
        daily_start_capital=100_000,
        execution_interval_minutes=5,
    )
    gw.connect()
    return MomentumStrategy(gw, PerformanceTracker(), settings)


def _broker_qty(gw: AlpacaLikeGateway, symbol: str) -> float:
    for p in gw.get_positions_for_sync():
        if p["symbol"] == symbol:
            return float(p["qty"])
    return 0.0


def demo_old_broken_order(gw: AlpacaLikeGateway) -> bool:
    """Single sell_to_close for dust+short qty — mimics pre-fix failure."""
    logger.info("--- Scenario A: OLD broken order (should REJECT) ---")
    gw._sim_positions = {
        "QQQ": {"pos": 0.000099994, "avg_cost": 742.85},
    }
    contract = SimContract(symbol="QQQ", sec_type="STK", exchange="SIM", currency="USD")
    bad_qty = 1.000099999
    result = gw.place_order(contract, "SELL", bad_qty, 738.0, position_intent="close")
    rejected = result is None
    if rejected:
        logger.success("Scenario A: correctly rejected (this was the June 22 bug)")
    else:
        logger.error("Scenario A: unexpected fill — mock misconfigured")
    return rejected


def demo_june22_fix(gw: AlpacaLikeGateway, verbose: bool) -> bool:
    """Exact post-unwind state from June 22 log; strategy should split close+open."""
    logger.info("--- Scenario B: June 22 dust state + strategy DOWN (should PASS) ---")
    gw._sim_positions = {
        "QQQ": {"pos": 0.000099994, "avg_cost": 742.854},
    }
    gw.orders.clear()

    strategy = _make_strategy(gw)
    strategy._net_qty["Nasdaq100"] = 0.0001
    strategy._total_cost["Nasdaq100"] = 0.0001 * 742.854
    strategy._position_reserve["Nasdaq100"] = -0.0886

    if verbose:
        logger.info(
            f"  seed: net={strategy._net_qty['Nasdaq100']}  "
            f"reserve={strategy._position_reserve['Nasdaq100']}  "
            f"broker={_broker_qty(gw, 'QQQ')}"
        )

    strategy._execute("Nasdaq100", Signal.DOWN, 0.0)

    broker_pos = _broker_qty(gw, "QQQ")
    intents = [o.get("position_intent") for o in gw.orders]
    has_open = "open" in intents
    went_short = broker_pos < -0.5
    no_rejects = all(o.get("status") == "FILLED" for o in gw.orders)

    if verbose:
        for i, o in enumerate(gw.orders, 1):
            logger.info(
                f"  order {i}: {o['action']} {o['qty']}x {o['symbol']} intent={o['position_intent']}"
            )

    # Success = short opened without the old single rejected close order.
    # Tiny dust may be closed in a separate leg or left for EOD flatten.
    ok = went_short and has_open and no_rejects and len(gw.orders) >= 1
    if ok:
        close_note = " (+ close dust)" if "close" in intents else " (dust skipped, open only)"
        logger.success(
            f"Scenario B: PASS — broker_pos={broker_pos}, "
            f"orders={len(gw.orders)}{close_note}"
        )
    else:
        logger.error(
            f"Scenario B: FAIL — broker_pos={broker_pos}, intents={intents}, "
            f"orders={len(gw.orders)}"
        )
    return ok


def demo_dust_close_then_open(gw: AlpacaLikeGateway) -> bool:
    """Dust above _DUST_QTY threshold — must emit close then open legs."""
    logger.info("--- Scenario D: larger dust → close + open (should PASS) ---")
    gw._sim_positions = {
        "QQQ": {"pos": 0.0005, "avg_cost": 742.85},
    }
    gw.orders.clear()

    strategy = _make_strategy(gw)
    strategy._net_qty["Nasdaq100"] = 0.0005
    strategy._total_cost["Nasdaq100"] = 0.0005 * 742.85
    strategy._position_reserve["Nasdaq100"] = -0.5

    strategy._execute("Nasdaq100", Signal.DOWN, 0.0)

    intents = [o.get("position_intent") for o in gw.orders]
    ok = "close" in intents and "open" in intents and _broker_qty(gw, "QQQ") < -0.5
    if ok:
        logger.success(f"Scenario D: PASS — intents={intents}")
    else:
        logger.error(f"Scenario D: FAIL — intents={intents}, broker={_broker_qty(gw, 'QQQ')}")
    return ok


def demo_full_long_to_short(gw: AlpacaLikeGateway) -> bool:
    """Build a real long, unwind to short through the strategy (integration path)."""
    logger.info("--- Scenario C: build long → unwind → short (should PASS) ---")
    gw._sim_positions = {}
    gw.orders.clear()
    strategy = _make_strategy(gw)

    for _ in range(3):
        strategy._execute("Nasdaq100", Signal.UP, 0.0)
    assert _broker_qty(gw, "QQQ") > 0, "expected long after UP signals"

    gw.orders.clear()
    for _ in range(5):
        strategy._execute("Nasdaq100", Signal.DOWN, 0.0)

    broker_pos = _broker_qty(gw, "QQQ")
    ok = broker_pos < -0.5
    if ok:
        logger.success(f"Scenario C: PASS — broker_pos={broker_pos}")
    else:
        logger.error(f"Scenario C: FAIL — broker_pos={broker_pos} (still long/flat)")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    settings = Settings(qs_api_key="test", broker="alpaca", dry_run=False)
    gw = AlpacaLikeGateway(settings)

    results = [
        demo_old_broken_order(gw),
        demo_june22_fix(gw, args.verbose),
        demo_dust_close_then_open(gw),
        demo_full_long_to_short(gw),
    ]

    logger.info("--- Summary ---")
    labels = [
        "A old order rejects",
        "B June 22 dust fix",
        "D close+open legs",
        "C long→short path",
    ]
    for label, passed in zip(labels, results):
        status = "PASS" if passed else "FAIL"
        logger.info(f"  {label}: {status}")

    if all(results):
        logger.success("All flat→short checks passed.")
        return 0
    logger.error("One or more checks failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
