from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from trader.config import Settings
from trader.gateways.base import GatewayBase


@dataclass
class SimContract:
    symbol: str
    sec_type: str
    exchange: str
    currency: str


class SimulatedGateway(GatewayBase):
    """
    In-memory broker that logs trades without connecting to anything.
    Prices jitter around configured sim prices so the PnL logic gets exercised.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._prices: dict[str, float] = dict(settings.sim_prices)
        self._orders: list[dict] = []
        self._sim_positions: dict[str, dict] = {}
        self._sim_account = {
            "net_liquidation": 100_000.0,
            "unrealized_pnl": 0.0,
            "realized_pnl": 0.0,
            "buying_power": 50_000.0,
        }

    def connect(self) -> None:
        logger.success("[SIM] Simulated gateway connected (DRY-RUN mode)")

    def disconnect(self) -> None:
        logger.info("[SIM] Simulated gateway disconnected")

    def resolve_contract(self, qs_symbol: str) -> Optional[SimContract]:
        spec = self._settings.symbol_map.get(qs_symbol)
        if spec is None:
            logger.info(f"[SIM] Symbol '{qs_symbol}' not in symbol map — auto-creating simulated contract")
            return SimContract(symbol=qs_symbol, sec_type="FUT", exchange="SIM", currency="USD")
        return SimContract(
            symbol=spec["symbol"],
            sec_type=spec["secType"],
            exchange=spec["exchange"],
            currency=spec["currency"],
        )

    def get_last_price(self, contract: SimContract) -> Optional[float]:
        base = self._prices.get(contract.symbol)
        if base is None:
            base = 1000.0
            self._prices[contract.symbol] = base
        jitter = base * random.uniform(-0.001, 0.001)
        self._prices[contract.symbol] = base + jitter
        return round(self._prices[contract.symbol], 2)

    def place_order(
        self,
        contract: SimContract,
        action: str,
        qty: int,
        price: Optional[float] = None,
        position_intent: Optional[str] = None,
    ) -> Optional[dict]:
        order_id = len(self._orders) + 1
        fill_price = price or self.get_last_price(contract) or 0.0
        order = {
            "id": order_id,
            "symbol": contract.symbol,
            "action": action,
            "qty": qty,
            "price": fill_price,
            "status": "FILLED",
            "time": datetime.now(timezone.utc).isoformat(),
        }
        self._orders.append(order)

        cur = self._sim_positions.get(contract.symbol, {"pos": 0, "avg_cost": 0})
        if action == "BUY":
            cur["pos"] += qty
        elif action == "SELL":
            cur["pos"] -= qty
        cur["avg_cost"] = fill_price

        if cur["pos"] == 0:
            self._sim_positions.pop(contract.symbol, None)
        else:
            self._sim_positions[contract.symbol] = cur

        logger.info(
            f"[SIM] Order #{order_id} FILLED  {action} {qty}x {contract.symbol} @ {fill_price:.2f}"
        )
        return order

    def get_account_summary(self) -> dict:
        return dict(self._sim_account)

    def get_open_positions(self) -> list[dict]:
        return [
            {"symbol": sym, "pos": d["pos"], "avg_cost": d["avg_cost"], "unreal_pnl": 0.0}
            for sym, d in self._sim_positions.items()
        ]

    def get_positions_for_sync(self) -> list[dict]:
        return [
            {"symbol": sym, "qty": d["pos"], "avg_price": d["avg_cost"]}
            for sym, d in self._sim_positions.items()
            if d["pos"] != 0
        ]
