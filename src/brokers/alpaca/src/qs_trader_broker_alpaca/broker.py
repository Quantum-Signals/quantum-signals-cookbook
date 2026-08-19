from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Optional

from loguru import logger

from qs_trader_broker_core import (
    BrokerBase,
    OrderResult,
    normalize_position_side,
)

from qs_trader_broker_alpaca.settings import AlpacaSettings

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


class AlpacaBroker(BrokerBase):
    """Broker backed by the Alpaca REST API (alpaca-py).

    Supports equity / ETF trading only, so it owns its own symbol map:
    QuantumSignals symbol → tradable Alpaca ticker. Futures signals (ES, NQ,
    …) map to correlated ETFs; equity/ETF signals map to themselves. Pass a
    ``symbol_map`` override to the constructor to replace ``DEFAULT_SYMBOL_MAP``
    (values are plain tickers).

    Configuration is loaded from ``ALPACA_*`` env vars via ``AlpacaSettings``;
    the broker self-loads unless a settings object is injected.

    The ``contract`` object used throughout this broker is a plain ticker
    string (e.g. ``"SPY"``), which is sufficient for all Alpaca REST calls.
    ``place_order`` returns a broker-agnostic ``OrderResult`` with the fill
    price already resolved.
    """

    # Alpaca fills fractional equity/ETF quantities (long side). The strategy
    # layer reads this to size orders fractionally; fractional shorts are still
    # rejected by Alpaca (code 42210000) and handled per-leg in the strategy.
    supports_fractional: bool = True

    DEFAULT_SYMBOL_MAP: dict[str, str] = {
        "ES": "SPY",
        "NQ": "QQQ",
        "S&P500": "SPY",
        "Nasdaq100": "QQQ",
        "SPY": "SPY",
        "QQQ": "QQQ",
        "AAPL": "AAPL",
    }

    def __init__(
        self,
        settings: Optional[AlpacaSettings] = None,
        *,
        symbol_map: Optional[dict[str, str]] = None,
    ) -> None:
        self._settings = settings if settings is not None else AlpacaSettings()
        self._symbol_map = symbol_map or self.DEFAULT_SYMBOL_MAP
        self._trading_client: object = None
        self._data_client: object = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        self._trading_client = TradingClient(
            self._settings.api_key,
            self._settings.secret_key,
            paper=self._settings.paper,
        )
        self._data_client = StockHistoricalDataClient(
            self._settings.api_key,
            self._settings.secret_key,
        )
        _install_default_request_timeout(self._trading_client, _DEFAULT_ALPACA_HTTP_TIMEOUT)
        _install_default_request_timeout(self._data_client, _DEFAULT_ALPACA_HTTP_TIMEOUT)

        account = self._trading_client.get_account()
        mode = "PAPER" if self._settings.paper else "LIVE"
        logger.success(
            f"Connected to Alpaca ({mode})  "
            f"equity=${float(account.equity):,.2f}  "
            f"buying_power=${float(account.buying_power):,.2f}"
        )

    def disconnect(self) -> None:
        # Alpaca is REST-based; no persistent socket to close.
        logger.info("Alpaca broker disconnected (REST — nothing to tear down)")

    # ------------------------------------------------------------------
    # Contract resolution
    # ------------------------------------------------------------------

    def resolve_contract(self, qs_symbol: str) -> Optional[str]:
        """Return the tradable Alpaca ticker string for *qs_symbol*, or None.

        Looks *qs_symbol* up in the symbol map (QS symbol → ticker). A symbol
        not in the map is treated as a direct Alpaca ticker (useful when
        positions are already held as ETF tickers). Returns ``None`` only if a
        map override yields a non-equity spec, since Alpaca trades
        equities/ETFs only.
        """
        entry = self._symbol_map.get(qs_symbol)
        if entry is None:
            # Not in map — assume the caller already passed an Alpaca ticker.
            logger.debug(f"[Alpaca] '{qs_symbol}' not in symbol map — treating as direct ticker")
            return qs_symbol

        # A plain ticker string is the native shape; a dict-shaped override
        # (a full contract spec) is tolerated by reading its symbol, but only STK.
        if isinstance(entry, dict):
            if entry.get("secType", "STK") != "STK":
                logger.warning(
                    f"[Alpaca] '{qs_symbol}' is secType={entry.get('secType')} — "
                    "Alpaca only supports equities/ETFs; skipping"
                )
                return None
            entry = entry["symbol"]

        logger.debug(f"[Alpaca] Resolved '{qs_symbol}' → '{entry}'")
        return entry

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
        position_intent: Optional[str] = None,
    ) -> Optional[OrderResult]:
        from alpaca.trading.enums import OrderSide, PositionIntent, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        ticker = contract
        side = OrderSide.BUY if action.upper() == "BUY" else OrderSide.SELL

        alpaca_intent: Optional[PositionIntent] = {
            ("open", OrderSide.SELL): PositionIntent.SELL_TO_OPEN,
            ("close", OrderSide.SELL): PositionIntent.SELL_TO_CLOSE,
            ("open", OrderSide.BUY): PositionIntent.BUY_TO_OPEN,
            ("close", OrderSide.BUY): PositionIntent.BUY_TO_CLOSE,
        }.get((position_intent, side))

        def build_request(order_qty: int | float, intent: Optional[PositionIntent]):
            """Build the market/limit request for this order (used on submit and retry)."""
            if self._settings.use_market_orders or price is None:
                return MarketOrderRequest(
                    symbol=ticker,
                    qty=order_qty,
                    side=side,
                    time_in_force=TimeInForce.DAY,
                    position_intent=intent,
                )
            return LimitOrderRequest(
                symbol=ticker,
                qty=order_qty,
                side=side,
                time_in_force=TimeInForce.DAY,
                limit_price=price,
                position_intent=intent,
            )

        req = build_request(qty, alpaca_intent)
        order_type = "MKT" if isinstance(req, MarketOrderRequest) else f"LMT@{price}"

        logger.debug(f"[Alpaca] Submit {action.upper()} {qty}x {ticker} ({order_type})…")
        try:
            order = self._trading_client.submit_order(req)
        except Exception as exc:
            msg = str(exc)
            retry_intent = _intent_mismatch_retry_intent(msg, side)
            if retry_intent is not None and retry_intent != alpaca_intent:
                from alpaca.trading.enums import PositionIntent

                retry_qty = qty
                if retry_intent in (
                    PositionIntent.BUY_TO_CLOSE,
                    PositionIntent.SELL_TO_CLOSE,
                ):
                    cap = self._closeable_qty(ticker, retry_intent)
                    logger.info(
                        f"[Alpaca] Intent mismatch retry: closeable={cap} "
                        f"requested={retry_qty} intent={retry_intent}"
                    )
                    if cap <= 1e-12:
                        logger.warning(
                            f"[Alpaca] Intent mismatch for {ticker} but nothing "
                            f"to close — not retrying {action.upper()} {qty}x"
                        )
                        _log_alpaca_order_hint(msg, ticker)
                        return None
                    if retry_qty > cap + 1e-9:
                        logger.warning(
                            f"[Alpaca] Capping retry {action.upper()} "
                            f"{retry_qty}x → {cap}x {ticker} (closeable at broker)"
                        )
                        retry_qty = cap
                logger.warning(
                    f"[Alpaca] Retrying {action.upper()} {retry_qty}x {ticker} with "
                    f"position_intent={retry_intent.value} "
                    f"(was {alpaca_intent.value if alpaca_intent else 'unset'})"
                )
                req = build_request(retry_qty, retry_intent)
                try:
                    order = self._trading_client.submit_order(req)
                    qty = retry_qty
                except Exception as retry_exc:
                    msg = str(retry_exc)
                    logger.error(
                        f"[Alpaca] Order submission failed for {ticker}: {retry_exc}"
                    )
                    _log_alpaca_order_hint(msg, ticker)
                    return None
            else:
                logger.error(f"[Alpaca] Order submission failed for {ticker}: {exc}")
                _log_alpaca_order_hint(msg, ticker)
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
                    # Filled but no avg price yet — fall through to the last-price
                    # fallback below (filled_avg_price is still None here).
                    break
            except Exception:
                break

        if filled_avg_price is None:
            filled_avg_price = self.get_last_price(ticker)

        logger.info(
            f"[Alpaca] {action.upper()} {qty}x {ticker}  "
            f"fill_price={filled_avg_price}  id={order.id}"
        )

        return OrderResult(
            id=str(order.id),
            symbol=ticker,
            action=action.upper(),
            qty=qty,
            price=filled_avg_price,
            status=str(order.status),
            raw=order,
        )

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
            raw_qty = float(p.qty)
            side = normalize_position_side(getattr(p, "side", None), qty=raw_qty)
            if side == "short":
                qty = -abs(raw_qty)
            else:
                qty = abs(raw_qty)
            if qty == 0:
                continue
            qty_available = float(getattr(p, "qty_available", None) or abs(raw_qty))
            result.append(
                {
                    "symbol": p.symbol,
                    "qty": qty,
                    "qty_available": abs(qty_available),
                    "side": side,
                    "avg_price": float(p.avg_entry_price),
                }
            )
        return result

    def _closeable_qty(self, ticker: str, intent: object) -> float:
        """Shares available to close for *intent* (buy_to_close / sell_to_close)."""
        from alpaca.trading.enums import PositionIntent

        for p in self._trading_client.get_all_positions():
            if p.symbol != ticker:
                continue
            raw_qty = float(p.qty)
            side = normalize_position_side(getattr(p, "side", None), qty=raw_qty)
            qty_avail = float(getattr(p, "qty_available", None) or abs(raw_qty))
            logger.debug(
                f"[Alpaca] _closeable_qty: {ticker} side={side} "
                f"qty={raw_qty} qty_avail={qty_avail} intent={intent}"
            )
            if intent == PositionIntent.BUY_TO_CLOSE and side == "short":
                return qty_avail
            if intent == PositionIntent.SELL_TO_CLOSE and side == "long":
                return qty_avail
        return 0.0


def _intent_mismatch_retry_intent(msg: str, side: object) -> object | None:
    """Return the Alpaca PositionIntent Alpaca inferred, for one retry."""
    from alpaca.trading.enums import OrderSide, PositionIntent

    if "42210000" not in msg or "position intent mismatch" not in msg:
        return None
    inferred = None
    for token in (
        "buy_to_close",
        "buy_to_open",
        "sell_to_close",
        "sell_to_open",
    ):
        if f"inferred: {token}" in msg:
            inferred = token
            break
    if inferred is None:
        return None
    mapping = {
        (OrderSide.BUY, "buy_to_close"): PositionIntent.BUY_TO_CLOSE,
        (OrderSide.BUY, "buy_to_open"): PositionIntent.BUY_TO_OPEN,
        (OrderSide.SELL, "sell_to_close"): PositionIntent.SELL_TO_CLOSE,
        (OrderSide.SELL, "sell_to_open"): PositionIntent.SELL_TO_OPEN,
    }
    return mapping.get((side, inferred))


def _log_alpaca_order_hint(msg: str, ticker: str) -> None:
    if "42210000" in msg or "fractional orders cannot be sold short" in msg:
        logger.warning(
            f"[Alpaca] Hint: strategy state for {ticker} may be out of sync "
            f"with the broker. The bot will re-sync at the next session start."
        )
