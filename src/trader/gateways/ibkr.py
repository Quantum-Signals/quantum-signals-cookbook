from __future__ import annotations

import math
import threading
import time
from typing import Optional

from loguru import logger

from trader.config import Settings
from trader.gateways.base import GatewayBase, OrderResult


class IBGateway(GatewayBase):
    """Thin, synchronous wrapper around ib_async for order management.

    Owns its own symbol map: QuantumSignals symbol → IBKR contract spec
    (native futures + equities). ``settings.symbol_map`` overrides it when
    non-empty. Roll ``lastTradeDateOrContractMonth`` to the active front month
    or IB may fail/hang on qualify.
    """

    DEFAULT_SYMBOL_MAP: dict[str, dict] = {
        "ES":   {"symbol": "ES",   "secType": "FUT", "exchange": "CME",   "currency": "USD", "lastTradeDateOrContractMonth": "202606"},
        "NQ":   {"symbol": "NQ",   "secType": "FUT", "exchange": "CME",   "currency": "USD", "lastTradeDateOrContractMonth": "202606"},
        "YM":   {"symbol": "YM",   "secType": "FUT", "exchange": "CBOT",  "currency": "USD", "lastTradeDateOrContractMonth": "202606"},
        "RTY":  {"symbol": "RTY",  "secType": "FUT", "exchange": "CME",   "currency": "USD", "lastTradeDateOrContractMonth": "202606"},
        "CL":   {"symbol": "CL",   "secType": "FUT", "exchange": "NYMEX", "currency": "USD", "lastTradeDateOrContractMonth": "202606"},
        "GC":   {"symbol": "GC",   "secType": "FUT", "exchange": "COMEX", "currency": "USD", "lastTradeDateOrContractMonth": "202606"},
        "MES":  {"symbol": "MES",  "secType": "FUT", "exchange": "CME",   "currency": "USD", "lastTradeDateOrContractMonth": "202606"},
        "MNQ":  {"symbol": "MNQ",  "secType": "FUT", "exchange": "CME",   "currency": "USD", "lastTradeDateOrContractMonth": "202606"},
        "AAPL": {"symbol": "AAPL", "secType": "STK", "exchange": "SMART", "currency": "USD"},
        "SPY":  {"symbol": "SPY",  "secType": "STK", "exchange": "SMART", "currency": "USD"},
        "QQQ":  {"symbol": "QQQ",  "secType": "STK", "exchange": "SMART", "currency": "USD"},
    }

    def __init__(self, settings: Settings) -> None:
        from ib_async import IB

        self._settings = settings
        self._symbol_map = settings.symbol_map or self.DEFAULT_SYMBOL_MAP
        self.ib = IB()
        self._contract_cache: dict[str, object] = {}
        # Set when IBKR reports a competing live session (error 10197); cleared
        # at the start of each get_last_price call so recovery is automatic once
        # the competing session is closed.
        self._mkt_abort = threading.Event()

    def connect(self) -> None:
        from ib_async import util

        util.startLoop()

        def _on_ib_error(reqId: int, errorCode: int, errorString: str, contract: object) -> None:
            if errorCode in (2104, 2106, 2107, 2108, 2158, 2119, 300, 354, 10167):
                logger.debug(f"IB notice {errorCode}: {errorString}")
            elif errorCode == 10197:
                self._mkt_abort.set()
                logger.error(
                    "IB error 10197: Competing TWS/IB Gateway session detected — "
                    "market data is blocked for this API connection. "
                    "Close the other TWS or IB Gateway session on this account "
                    "(check other machines/IPs), then restart the bot."
                )
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

        spec = self._symbol_map.get(qs_symbol)
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

        # snapshot=True requests a one-shot quote; IBKR auto-cancels the
        # subscription once the packet arrives, which avoids the stale-cache
        # problem that plagued the old streaming approach.
        self._mkt_abort.clear()
        ticker = self.ib.reqMktData(contract, snapshot=True)

        price = None
        deadline = time.perf_counter() + 8.0
        while time.perf_counter() < deadline:
            self.ib.sleep(0.2)
            if self._mkt_abort.is_set():
                logger.debug(f"Aborting market data wait for {sym} (competing session)")
                break
            price = self._best_price(ticker)
            if price is not None:
                break

        elapsed = time.perf_counter() - t0
        logger.info(f"IB market data for {sym} done in {elapsed:.2f}s (price={price!r})")

        if price is None:
            logger.warning(f"No usable price for {sym} — trying historical data fallback")
            price = self._price_from_history(contract, sym)
            if price is None:
                logger.warning(f"No usable price for {sym} — check market data subscription or market hours")

        return price

    def _price_from_history(self, contract: object, sym: str) -> Optional[float]:
        """Fallback: pull the last 1-min bar close via reqHistoricalData.

        Works during and outside regular trading hours, and does not require
        a streaming market-data subscription.
        """
        try:
            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr="1 D",
                barSizeSetting="1 min",
                whatToShow="TRADES",
                useRTH=False,
                formatDate=1,
            )
            if bars:
                price = self._finite(bars[-1].close)
                if price is not None:
                    logger.info(f"Historical fallback price for {sym}: {price}")
                    return price
        except Exception as exc:
            logger.debug(f"Historical data fallback failed for {sym}: {exc}")
        return None

    def place_order(
        self,
        contract: object,
        action: str,
        qty: int,
        price: Optional[float] = None,
        position_intent: Optional[str] = None,
    ) -> Optional[OrderResult]:
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

        return OrderResult(
            id=str(getattr(order, "orderId", "") or getattr(order, "permId", "")),
            symbol=str(getattr(contract, "localSymbol", None) or contract.symbol),
            action=action.upper(),
            qty=qty,
            price=_ib_fill_price(trade),
            status=str(trade.orderStatus.status),
            raw=trade,
        )

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


def _ib_fill_price(trade: object) -> Optional[float]:
    """Actual execution price from an ib_async Trade, or None if not filled.

    Prefers ``orderStatus.avgFillPrice``; falls back to averaging the per-fill
    execution prices.
    """
    status = getattr(trade, "orderStatus", None)
    if status is not None:
        avg = getattr(status, "avgFillPrice", 0.0)
        if avg and avg > 0:
            return float(avg)

    fills = getattr(trade, "fills", None)
    if fills:
        prices = [f.execution.avgPrice for f in fills if getattr(f, "execution", None)]
        if prices:
            return sum(prices) / len(prices)

    return None
