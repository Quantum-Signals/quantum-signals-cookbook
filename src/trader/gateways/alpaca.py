from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Optional

from loguru import logger

from trader.config import Settings
from trader.gateways.base import GatewayBase

# Alpaca's RESTClient uses ``requests.Session`` without per-call timeouts, so a
# stalled TCP connection can block the main trading loop forever (e.g. during
# ``report_once`` every 300s). Default (connect, read) seconds for all requests.
_DEFAULT_ALPACA_HTTP_TIMEOUT: tuple[float, float] = (10.0, 60.0)


def _install_default_request_timeout(rest_client: object, timeout: tuple[float, float]) -> None:
    """Ensure ``session.request(..., timeout=...)`` so hung sockets cannot block indefinitely."""
    session = getattr(rest_client, "_session", None)
    if session is None:
        return
    orig_request: Callable[..., Any] = session.request

    def request_with_default_timeout(method: str, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", timeout)
        return orig_request(method, url, **kwargs)

    session.request = request_with_default_timeout  # type: ignore[method-assign]


class AlpacaGateway(GatewayBase):
    """Broker gateway backed by the Alpaca REST API (alpaca-py).

    Supports equity / ETF trading only.  Futures symbols in the symbol map
    (secType != "STK") are rejected with a warning from resolve_contract().

    The ``contract`` object used throughout this gateway is a plain ticker
    string (e.g. ``"SPY"``), which is sufficient for all Alpaca REST calls.
    This is compatible with the dict-branch of ``_extract_fill_price()`` in
    the strategy layer, which reads ``trade_result["price"]``.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._trading_client: object = None
        self._data_client: object = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        self._trading_client = TradingClient(
            self._settings.alpaca_api_key,
            self._settings.alpaca_secret_key,
            paper=self._settings.alpaca_paper,
        )
        self._data_client = StockHistoricalDataClient(
            self._settings.alpaca_api_key,
            self._settings.alpaca_secret_key,
        )
        _install_default_request_timeout(self._trading_client, _DEFAULT_ALPACA_HTTP_TIMEOUT)
        _install_default_request_timeout(self._data_client, _DEFAULT_ALPACA_HTTP_TIMEOUT)

        account = self._trading_client.get_account()
        mode = "PAPER" if self._settings.alpaca_paper else "LIVE"
        logger.success(
            f"Connected to Alpaca ({mode})  "
            f"equity=${float(account.equity):,.2f}  "
            f"buying_power=${float(account.buying_power):,.2f}"
        )

    def disconnect(self) -> None:
        # Alpaca is REST-based; no persistent socket to close.
        logger.info("Alpaca gateway disconnected (REST — nothing to tear down)")

    # ------------------------------------------------------------------
    # Contract resolution
    # ------------------------------------------------------------------

    def resolve_contract(self, qs_symbol: str) -> Optional[str]:
        """Return the Alpaca ticker string for *qs_symbol*, or None.

        Only STK entries in the symbol map are tradeable on Alpaca.
        If the symbol is not in the map at all it is treated as a direct
        Alpaca ticker (useful when positions are already held as ETF tickers).
        """
        spec = self._settings.symbol_map.get(qs_symbol)
        if spec is None:
            # Not in map — assume the caller already passed an Alpaca ticker.
            logger.debug(f"[Alpaca] '{qs_symbol}' not in symbol map — treating as direct ticker")
            return qs_symbol

        sec_type = spec.get("secType", "STK")
        if sec_type != "STK":
            logger.warning(
                f"[Alpaca] '{qs_symbol}' is secType={sec_type} — "
                "Alpaca only supports equities/ETFs; skipping"
            )
            return None

        ticker = spec["symbol"]
        logger.debug(f"[Alpaca] Resolved '{qs_symbol}' → '{ticker}'")
        return ticker

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def get_last_price(self, contract: str) -> Optional[float]:
        from alpaca.data.requests import StockLatestTradeRequest

        ticker = contract
        logger.debug(f"[Alpaca] Fetching latest trade price for {ticker}…")
        t0 = time.perf_counter()
        try:
            req = StockLatestTradeRequest(symbol_or_symbols=ticker)
            trade = self._data_client.get_stock_latest_trade(req)
            price = float(trade[ticker].price)
            elapsed = time.perf_counter() - t0
            logger.debug(f"[Alpaca] Latest price for {ticker}: ${price:,.2f} ({elapsed:.2f}s)")
            return price
        except Exception as exc:
            logger.warning(f"[Alpaca] Could not fetch price for {ticker}: {exc}")
            return None

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    def place_order(
        self,
        contract: str,
        action: str,
        qty: int | float,
        price: Optional[float] = None,
    ) -> Optional[dict]:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        ticker = contract
        side = OrderSide.BUY if action.upper() == "BUY" else OrderSide.SELL

        if self._settings.use_market_orders or price is None:
            req = MarketOrderRequest(
                symbol=ticker,
                qty=qty,
                side=side,
                time_in_force=TimeInForce.DAY,
            )
            order_type = "MKT"
        else:
            req = LimitOrderRequest(
                symbol=ticker,
                qty=qty,
                side=side,
                time_in_force=TimeInForce.DAY,
                limit_price=price,
            )
            order_type = f"LMT@{price}"

        logger.debug(f"[Alpaca] Submit {action.upper()} {qty}x {ticker} ({order_type})…")
        try:
            order = self._trading_client.submit_order(req)
        except Exception as exc:
            msg = str(exc)
            logger.error(f"[Alpaca] Order submission failed for {ticker}: {exc}")
            # Code 42210000 means the resulting position would be a fractional
            # short. Almost always indicates the strategy's net_qty has drifted
            # from the broker's actual position — the next sync_positions()
            # call (start of next trading day) will reconcile.
            if "42210000" in msg or "fractional orders cannot be sold short" in msg:
                logger.warning(
                    f"[Alpaca] Hint: this typically means strategy state for "
                    f"{ticker} is out of sync with the broker. The bot will "
                    f"re-sync at the next session start; manually flatten any "
                    f"stray positions in the Alpaca dashboard if needed."
                )
            return None

        logger.debug(
            f"[Alpaca] Order submitted  id={order.id}  status={order.status}"
        )

        # Poll briefly for a fill so we can return the actual fill price.
        filled_avg_price: Optional[float] = None
        deadline = time.perf_counter() + 5.0
        while time.perf_counter() < deadline:
            time.sleep(0.25)
            try:
                refreshed = self._trading_client.get_order_by_id(str(order.id))
                if refreshed.filled_avg_price is not None:
                    filled_avg_price = float(refreshed.filled_avg_price)
                    break
                if str(refreshed.status) in {"filled", "partially_filled"}:
                    filled_avg_price = (
                        float(refreshed.filled_avg_price)
                        if refreshed.filled_avg_price is not None
                        else None
                    )
                    break
            except Exception:
                break

        if filled_avg_price is None:
            filled_avg_price = self.get_last_price(ticker)

        logger.info(
            f"[Alpaca] {action.upper()} {qty}x {ticker}  "
            f"fill_price={filled_avg_price}  id={order.id}"
        )

        return {
            "id": str(order.id),
            "symbol": ticker,
            "action": action.upper(),
            "qty": qty,
            "price": filled_avg_price,
            "status": str(order.status),
        }

    # ------------------------------------------------------------------
    # Account & positions
    # ------------------------------------------------------------------

    def get_account_summary(self) -> dict:
        account = self._trading_client.get_account()
        equity = float(account.equity)
        last_equity = float(account.last_equity)
        _raw_upl = getattr(account, "unrealized_pl", None)
        unrealized_pnl = float(_raw_upl) if _raw_upl is not None else 0.0
        # Approximate realized P&L for the session as day-over-day change minus unrealized.
        realized_pnl = (equity - last_equity) - unrealized_pnl
        return {
            "net_liquidation": equity,
            "unrealized_pnl": unrealized_pnl,
            "realized_pnl": realized_pnl,
            "buying_power": float(account.buying_power),
        }

    def get_open_positions(self) -> list[dict]:
        positions = self._trading_client.get_all_positions()
        return [
            {
                "symbol": p.symbol,
                "pos": float(p.qty),
                "avg_cost": float(p.avg_entry_price),
                "unreal_pnl": float(p.unrealized_pl) if getattr(p, "unrealized_pl", None) is not None else 0.0,
            }
            for p in positions
        ]

    def get_positions_for_sync(self) -> list[dict]:
        positions = self._trading_client.get_all_positions()
        result = []
        for p in positions:
            qty = float(p.qty)
            if qty == 0:
                continue
            result.append(
                {
                    "symbol": p.symbol,
                    "qty": qty,
                    "avg_price": float(p.avg_entry_price),
                }
            )
        return result
