from __future__ import annotations

import math
import time
from typing import Optional

from loguru import logger

from trader.config import Settings
from trader.gateways.base import GatewayBase


class IBGateway(GatewayBase):
    """Thin, synchronous wrapper around ib_async for order management."""

    def __init__(self, settings: Settings) -> None:
        from ib_async import IB

        self._settings = settings
        self.ib = IB()
        self._contract_cache: dict[str, object] = {}

    def connect(self) -> None:
        from ib_async import util

        util.startLoop()

        def _on_ib_error(reqId: int, errorCode: int, errorString: str, contract: object) -> None:
            if errorCode in (2104, 2106, 2107, 2108, 2158, 2119, 300, 354, 10167):
                logger.debug(f"IB notice {errorCode}: {errorString}")
            else:
                logger.warning(f"IB error {errorCode} reqId={reqId}: {errorString} contract={contract}")

        self.ib.errorEvent += _on_ib_error

        self.ib.connect(
            host=self._settings.ib_host,
            port=self._settings.ib_port,
            clientId=self._settings.ib_client_id,
            timeout=20,
            readonly=False,
        )
        mdt = self._settings.ib_market_data_type
        self.ib.reqMarketDataType(mdt)
        mdt_labels = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed-frozen"}
        logger.success(
            f"Connected to IB Gateway  {self._settings.ib_host}:{self._settings.ib_port}  "
            f"(clientId={self._settings.ib_client_id}, marketDataType={mdt_labels.get(mdt, mdt)})"
        )

    def disconnect(self) -> None:
        self.ib.disconnect()
        logger.info("Disconnected from IB Gateway")

    def resolve_contract(self, qs_symbol: str) -> Optional[object]:
        from ib_async import Contract

        if qs_symbol in self._contract_cache:
            return self._contract_cache[qs_symbol]

        spec = self._settings.symbol_map.get(qs_symbol)
        if spec is None:
            logger.warning(f"Symbol '{qs_symbol}' not in symbol map — skipping")
            return None

        contract = Contract(**spec)
        logger.info(f"IB qualifyContracts for {qs_symbol} (month={spec.get('lastTradeDateOrContractMonth', 'n/a')})…")
        t0 = time.perf_counter()
        details = self.ib.qualifyContracts(contract)
        elapsed = time.perf_counter() - t0
        logger.info(f"IB qualifyContracts for {qs_symbol} finished in {elapsed:.2f}s ({len(details)} leg(s))")

        if not details:
            logger.error(
                f"Could not qualify contract for {qs_symbol} — check expiry month, exchange, "
                f"and futures permissions on this account"
            )
            return None

        self._contract_cache[qs_symbol] = details[0]
        return details[0]

    @staticmethod
    def _finite(v: object) -> Optional[float]:
        """Return v as float if it's a finite positive number, else None."""
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if math.isfinite(f) and f > 0 else None

    def _best_price(self, ticker: object) -> Optional[float]:
        """Extract the best available price from a Ticker, handling NaN correctly."""
        return (
            self._finite(ticker.last)
            or self._finite(ticker.close)
            or self._finite(ticker.bid)
            or self._finite(ticker.ask)
            or self._finite(ticker.marketPrice())
        )

    def get_last_price(self, contract: object) -> Optional[float]:
        sym = getattr(contract, "localSymbol", None) or getattr(contract, "symbol", "?")
        logger.info(f"IB reqMktData for {sym}…")
        t0 = time.perf_counter()

        ticker = self.ib.reqMktData(contract)

        # Invalidate cached fields — ib_async reuses the Ticker object and old
        # values would satisfy the poll instantly, giving a stale price.
        _nan = float("nan")
        ticker.last = _nan
        ticker.close = _nan
        ticker.bid = _nan
        ticker.ask = _nan

        price = None
        deadline = time.perf_counter() + 4.0
        while time.perf_counter() < deadline:
            self.ib.sleep(0.2)
            price = self._best_price(ticker)
            if price is not None:
                break

        self.ib.cancelMktData(contract)
        elapsed = time.perf_counter() - t0
        logger.info(f"IB market data for {sym} done in {elapsed:.2f}s (price={price!r})")
        if price is None:
            logger.warning(f"No usable price for {sym} — check market data subscription or market hours")
        return price

    def place_order(
        self,
        contract: object,
        action: str,
        qty: int,
        price: Optional[float] = None,
    ) -> Optional[object]:
        from ib_async import LimitOrder, MarketOrder

        if self._settings.use_market_orders or price is None:
            order = MarketOrder(action=action, totalQuantity=qty)
        else:
            order = LimitOrder(action=action, totalQuantity=qty, lmtPrice=price)

        sym = getattr(contract, "localSymbol", None) or getattr(contract, "symbol", "?")
        logger.info(f"IB placeOrder {action} {qty}x {sym}…")
        trade = self.ib.placeOrder(contract, order)
        logger.info(
            f"Order placed  {action} {qty}x {contract.symbol}  "
            f"{'MKT' if self._settings.use_market_orders else f'LMT@{price}'}"
        )

        _DONE = frozenset({"Filled", "Cancelled", "Inactive", "ApiCancelled", "ValidationError"})
        deadline = time.perf_counter() + 5.0
        while time.perf_counter() < deadline:
            self.ib.sleep(0.25)
            if trade.orderStatus.status in _DONE or trade.fills:
                break

        return trade

    def get_account_summary(self) -> dict:
        summary = {s.tag: s.value for s in self.ib.accountSummary()}
        return {
            "net_liquidation": float(summary.get("NetLiquidation", 0)),
            "unrealized_pnl": float(summary.get("UnrealizedPnL", 0)),
            "realized_pnl": float(summary.get("RealizedPnL", 0)),
            "buying_power": float(summary.get("BuyingPower", 0)),
        }

    def get_open_positions(self) -> list[dict]:
        return [
            {
                "symbol": p.contract.symbol,
                "pos": p.position,
                "avg_cost": p.averageCost,
                "unreal_pnl": p.unrealizedPNL,
            }
            for p in self.ib.portfolio()
        ]

    def get_positions_for_sync(self) -> list[dict]:
        result = []
        for p in self.ib.portfolio():
            sym = p.contract.symbol
            qty = p.position
            if qty == 0:
                continue
            try:
                mult = float(p.contract.multiplier) if p.contract.multiplier else 1.0
            except (TypeError, ValueError):
                mult = 1.0
            avg_price = p.averageCost / mult if mult else p.averageCost
            result.append({"symbol": sym, "qty": qty, "avg_price": avg_price})
        return result
