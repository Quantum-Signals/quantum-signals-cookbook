from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from qs_trader_symbols import simulated_symbol_map

from .base import BrokerBase, OrderResult
from .settings import SimulatedSettings


@dataclass
class SimContract:
    symbol: str
    sec_type: str
    exchange: str
    currency: str


class SimulatedBroker(BrokerBase):
    """
    In-memory broker that logs trades without connecting to anything.
    Prices jitter around configured sim prices so the PnL logic gets exercised.

    Owns its own symbol map (contract shapes) and seed prices; both fall back
    to the class defaults when the ``symbol_map`` / ``sim_prices`` constructor
    overrides are omitted. Any symbol not in the map is auto-created so DRY_RUN
    can exercise arbitrary symbols.
    """

    # Simulates fractional fills (and mimics Alpaca's fractional-short
    # rejection, code 42210000), so DRY_RUN exercises the same fractional
    # sizing path as the live equity/ETF broker it stands in for.
    supports_fractional: bool = True

    DEFAULT_SYMBOL_MAP: dict[str, dict] = simulated_symbol_map()

    DEFAULT_PRICES: dict[str, float] = {
        "ES": 5450.00, "NQ": 19200.00, "YM": 42500.00, "RTY": 2050.00,
        "MES": 5450.00, "MNQ": 19200.00,
        "CL": 68.50, "GC": 2350.00, "SI": 29.50, "NG": 3.20,
        "AAPL": 218.50, "SPY": 545.00, "QQQ": 470.00,
        "DIA": 425.00, "IWM": 205.00,
        "USO": 72.00, "GLD": 235.00, "SLV": 28.50, "UNG": 12.00,
    }

    def __init__(
        self,
        settings: Optional[SimulatedSettings] = None,
        *,
        symbol_map: Optional[dict[str, dict]] = None,
        sim_prices: Optional[dict[str, float]] = None,
    ) -> None:
        # Self-load env settings (SIM_*) when not injected.
        self._settings = settings if settings is not None else SimulatedSettings()
        self._symbol_map = symbol_map or self.DEFAULT_SYMBOL_MAP
        self._prices: dict[str, float] = dict(sim_prices or self.DEFAULT_PRICES)
        self._orders: list[dict] = []
        self._sim_positions: dict[str, dict] = {}
        self._sim_account = {
            "net_liquidation": 100_000.0,
            "unrealized_pnl": 0.0,
            "realized_pnl": 0.0,
            "buying_power": 50_000.0,
        }

    def connect(self) -> None:
        logger.success("[SIM] Simulated broker connected (DRY-RUN mode)")

    def disconnect(self) -> None:
        logger.info("[SIM] Simulated broker disconnected")

    def startup_entitlement_warnings(self, canonical_symbols: list[str]) -> list[str]:
        return []  # dry-run accepts every mapped symbol

    def resolve_contract(self, qs_symbol: str) -> Optional[SimContract]:
        spec = self._symbol_map.get(qs_symbol)
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
        j = self._settings.price_jitter
        if j:
            base = base + base * random.uniform(-j, j)
            self._prices[contract.symbol] = base
        return round(base, 2)

    def place_order(
        self,
        contract: SimContract,
        action: str,
        qty: int | float,
        price: Optional[float] = None,
        position_intent: Optional[str] = None,
    ) -> Optional[OrderResult]:
        order_id = len(self._orders) + 1
        fill_price = price or self.get_last_price(contract) or 0.0
        symbol = contract.symbol

        cur_pos = float(self._sim_positions.get(symbol, {"pos": 0}).get("pos", 0))

        if action.upper() == "SELL" and position_intent == "close":
            long_avail = max(0.0, cur_pos)
            if float(qty) > long_avail + 1e-9:
                logger.error(
                    f"[SIM] REJECTED {symbol}: insufficient long qty "
                    f"(requested: {qty}, available: {long_avail})"
                )
                return None

        # Mimic Alpaca error 42210000: fractional orders cannot be sold short.
        # A sell that leaves a *short* position must land on a whole share; a
        # fractional resulting short is rejected outright by the broker.
        if action.upper() == "SELL":
            resulting_pos = cur_pos - float(qty)
            if resulting_pos < -1e-9 and abs(resulting_pos - round(resulting_pos)) > 1e-6:
                logger.error(
                    f"[SIM] REJECTED {symbol}: "
                    '{"code":42210000,"message":"fractional orders cannot be sold short"} '
                    f"(cur={cur_pos}, qty={qty}, resulting={resulting_pos})"
                )
                return None

        order = {
            "id": order_id,
            "symbol": symbol,
            "action": action,
            "qty": qty,
            "price": fill_price,
            "status": "FILLED",
            "time": datetime.now(timezone.utc).isoformat(),
            "position_intent": position_intent,
        }
        self._orders.append(order)

        cur = self._sim_positions.get(symbol, {"pos": 0, "avg_cost": 0})
        if action == "BUY":
            cur["pos"] += qty
        elif action == "SELL":
            cur["pos"] -= qty
        cur["avg_cost"] = fill_price

        if cur["pos"] == 0:
            self._sim_positions.pop(symbol, None)
        else:
            self._sim_positions[symbol] = cur

        logger.info(
            f"[SIM] Order #{order_id} FILLED  {action} {qty}x {symbol} @ {fill_price:.2f}"
        )
        return OrderResult(
            id=str(order_id),
            symbol=symbol,
            action=action.upper(),
            qty=qty,
            price=fill_price,
            status="FILLED",
            raw=order,
        )

    def get_account_summary(self) -> dict:
        return dict(self._sim_account)

    def get_open_positions(self) -> list[dict]:
        return [
            {"symbol": sym, "pos": d["pos"], "avg_cost": d["avg_cost"], "unreal_pnl": 0.0}
            for sym, d in self._sim_positions.items()
        ]

    def get_positions_for_sync(self) -> list[dict]:
        return [
            {
                "symbol": sym,
                "qty": d["pos"],
                "qty_available": abs(d["pos"]),
                "side": "long" if d["pos"] > 0 else "short",
                "avg_price": d["avg_cost"],
            }
            for sym, d in self._sim_positions.items()
            if d["pos"] != 0
        ]
