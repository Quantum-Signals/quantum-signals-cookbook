from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Optional

from loguru import logger

from qs_trader_broker_core.base import BrokerBase, OrderResult
from qs_trader_symbols import canonicalize, tmgm_symbol_map

from .constants import (
    ACCEPTED_RETCODES,
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,
    ACCOUNT_MARGIN_MODE_RETAIL_NETTING,
    ACCOUNT_TRADE_MODE_REAL,
    FILLED_RETCODES,
    ORDER_FILLING_FOK,
    ORDER_FILLING_IOC,
    ORDER_FILLING_RETURN,
    ORDER_TIME_GTC,
    ORDER_TYPE_BUY,
    ORDER_TYPE_BUY_LIMIT,
    ORDER_TYPE_SELL,
    ORDER_TYPE_SELL_LIMIT,
    POSITION_TYPE_BUY,
    POSITION_TYPE_SELL,
    RETRY_RETCODES,
    SYMBOL_FILLING_FOK,
    SYMBOL_FILLING_IOC,
    SYMBOL_TRADE_MODE_DISABLED,
    TRADE_ACTION_DEAL,
    TRADE_ACTION_PENDING,
)
from .settings import TmgmSettings


def align_volume(qty: float, step: float, minimum: float) -> Optional[float]:
    """Floor *qty* to the symbol's volume step, or ``None`` when it is below the minimum."""
    if qty <= 0 or step <= 0 or minimum < 0:
        return None
    quantum = Decimal(str(round(float(step), 8)))
    steps = (Decimal(str(qty)) / quantum).to_integral_value(rounding=ROUND_DOWN)
    aligned = steps * quantum
    if aligned < Decimal(str(round(float(minimum), 8))):
        return None
    return float(aligned)


def _order_filling(symbol_filling_mode: int) -> int:
    """Map ``symbol_info.filling_mode`` onto ``ORDER_FILLING_*``.

    TradeMaxGlobal-Demo reports ``2`` (IOC) for the mapped CFDs, and that filling
    mode is what a live demo deal accepted.
    """
    if symbol_filling_mode & SYMBOL_FILLING_IOC:
        return ORDER_FILLING_IOC
    if symbol_filling_mode & SYMBOL_FILLING_FOK:
        return ORDER_FILLING_FOK
    return ORDER_FILLING_RETURN


class TmgmBroker(BrokerBase):
    """Broker that sends orders through a local MetaTrader 5 terminal logged into TMGM.

    TMGM has no REST API. This process talks to the official ``MetaTrader5`` Python
    package, which only runs on Windows and only while the desktop terminal is open.
    The ``contract`` object is the MT5 symbol string (``US500``, ``XAUUSD``, …).

    A hedging account (the TradeMaxGlobal demo is one) does not net an opposite
    deal into the open position. Reducing orders therefore close the position
    ticket. An order with no ``position_intent`` closes the opposite side first
    and only then opens the remainder, so end-of-day flatten still works.

    Sizes are lots. ``supports_fractional`` is true because the minimum on the
    index and metal CFDs is 0.01. The app's futures point-value multipliers are
    not these CFD contract sizes; ``connect`` logs that once.
    """

    supports_fractional: bool = True

    DEFAULT_SYMBOL_MAP: dict[str, str] = tmgm_symbol_map()

    def __init__(
        self,
        settings: Optional[TmgmSettings] = None,
        *,
        symbol_map: Optional[dict[str, str]] = None,
        client: object = None,
    ) -> None:
        self._settings = settings if settings is not None else TmgmSettings()
        self._symbol_map = symbol_map if symbol_map is not None else self.DEFAULT_SYMBOL_MAP
        self._client = client

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        if self._client is None:
            import MetaTrader5 as mt5

            self._client = mt5

        client = self._client
        kwargs: dict[str, object] = {}
        if self._settings.terminal_path:
            kwargs["path"] = self._settings.terminal_path
        if self._settings.login:
            kwargs["login"] = self._settings.login
            kwargs["password"] = self._settings.password
            kwargs["server"] = self._settings.server
        if not client.initialize(**kwargs):
            raise RuntimeError(f"MT5 initialize() failed: {client.last_error()}")

        account = client.account_info()
        if account is None:
            client.shutdown()
            raise RuntimeError(f"MT5 returned no account: {client.last_error()}")

        if account.trade_mode == ACCOUNT_TRADE_MODE_REAL and not self._settings.allow_live:
            client.shutdown()
            raise RuntimeError(
                f"TMGM account {account.login} on {account.server} is a live account. "
                "Refusing to trade. Set TMGM_ALLOW_LIVE=true only when that is intentional."
            )

        terminal = client.terminal_info()
        if terminal is not None and not getattr(terminal, "trade_allowed", True):
            client.shutdown()
            raise RuntimeError(
                "MetaTrader 5 algorithmic trading is off. "
                "Turn on the Algo Trading button in the terminal toolbar."
            )

        mode = {0: "DEMO", 1: "CONTEST", 2: "LIVE"}.get(account.trade_mode, str(account.trade_mode))
        margin = {
            ACCOUNT_MARGIN_MODE_RETAIL_NETTING: "netting",
            ACCOUNT_MARGIN_MODE_RETAIL_HEDGING: "hedging",
        }.get(account.margin_mode, str(account.margin_mode))
        logger.success(
            f"Connected to TMGM via MetaTrader 5 ({mode}, {margin})  "
            f"login={account.login}  server={account.server}  "
            f"equity={float(account.equity):,.2f} {account.currency}"
        )
        if account.margin_mode == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
            logger.info(
                "[TMGM] Hedging account — reducing orders close the position ticket "
                "so a net position can flatten."
            )
        logger.warning(
            "[TMGM] Orders are in lots. The app contract multipliers are futures "
            "point values and do not match these CFD contract sizes."
        )

    def disconnect(self) -> None:
        if self._client is not None:
            self._client.shutdown()
        logger.info("[TMGM] Disconnected from MetaTrader 5")

    def startup_entitlement_warnings(self, canonical_symbols: list[str]) -> list[str]:
        warnings = super().startup_entitlement_warnings(canonical_symbols)
        client = self._client
        if client is None:
            warnings.append("TMGM client not connected — symbol checks skipped")
            return warnings
        for canon in canonical_symbols:
            symbol = self.resolve_contract(canon)
            if not symbol:
                continue
            info = client.symbol_info(symbol)
            if info is None:
                warnings.append(f"TMGM has no symbol {symbol} ({canon})")
                continue
            if getattr(info, "trade_mode", SYMBOL_TRADE_MODE_DISABLED) == SYMBOL_TRADE_MODE_DISABLED:
                warnings.append(f"TMGM symbol {symbol} ({canon}) is disabled for trading")
        return warnings

    # ------------------------------------------------------------------
    # Contract resolution
    # ------------------------------------------------------------------

    def resolve_contract(self, qs_symbol: str) -> Optional[str]:
        """Return the MT5 symbol for *qs_symbol*, or ``None`` if QS lists it but TMGM does not.

        A name that is not a QS alias is returned unchanged so a passthrough
        Market Watch symbol can still be traded. Natural gas is a known QS name
        with no TMGM symbol, so it returns ``None`` and the strategy skips it.
        """
        mapped = self._symbol_map.get(qs_symbol)
        if mapped:
            logger.debug(f"[TMGM] Resolved '{qs_symbol}' → '{mapped}'")
            return mapped
        if canonicalize(qs_symbol) is not None:
            logger.debug(f"[TMGM] '{qs_symbol}' has no TMGM symbol — skipping")
            return None
        logger.debug(f"[TMGM] '{qs_symbol}' not in symbol map — treating as a direct MT5 symbol")
        return qs_symbol

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def get_last_price(self, contract: str) -> Optional[float]:
        client = self._require_client()
        symbol = str(contract)
        client.symbol_select(symbol, True)
        tick = client.symbol_info_tick(symbol)
        if tick is None:
            logger.warning(f"[TMGM] No tick for {symbol}: {client.last_error()}")
            return None
        bid = float(tick.bid)
        ask = float(tick.ask)
        if bid <= 0 or ask <= 0:
            logger.warning(f"[TMGM] {symbol} has no quote (bid={bid}, ask={ask})")
            return None
        return (bid + ask) / 2.0

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
        client = self._client
        if client is None:
            logger.error("[TMGM] place_order called before connect()")
            return None

        side = action.upper()
        if side not in {"BUY", "SELL"}:
            logger.error(f"[TMGM] Unknown order action {action!r}")
            return None

        symbol = str(contract)
        info = client.symbol_info(symbol)
        if info is None:
            logger.error(f"[TMGM] No symbol info for {symbol}: {client.last_error()}")
            return None
        if getattr(info, "trade_mode", SYMBOL_TRADE_MODE_DISABLED) == SYMBOL_TRADE_MODE_DISABLED:
            logger.error(f"[TMGM] {symbol} is disabled for trading")
            return None

        step = float(info.volume_step)
        minimum = float(info.volume_min)
        aligned = align_volume(float(qty), step, minimum)
        if aligned is None:
            logger.warning(
                f"[TMGM] {side} {qty} {symbol} is below the minimum lot "
                f"(min={minimum}, step={step}) — not sent"
            )
            return None
        if abs(aligned - float(qty)) > 1e-9:
            logger.info(
                f"[TMGM] Aligned {side} {qty} → {aligned} {symbol} "
                f"(step {step})"
            )

        client.symbol_select(symbol, True)
        positions = list(client.positions_get(symbol=symbol) or [])
        close_pool = [
            p
            for p in positions
            if int(p.type) == (POSITION_TYPE_BUY if side == "SELL" else POSITION_TYPE_SELL)
        ]
        close_pool.sort(key=lambda p: (int(getattr(p, "time", 0)), int(p.ticket)))

        if position_intent == "open":
            close_qty = 0.0
            open_qty = aligned
        elif position_intent == "close":
            close_qty = aligned
            open_qty = 0.0
        else:
            available = sum(float(p.volume) for p in close_pool)
            close_qty = align_volume(min(aligned, available), step, minimum) or 0.0
            open_qty = align_volume(aligned - close_qty, step, minimum) or 0.0

        fills: list[tuple[str, float, float, str]] = []
        if close_qty > 0:
            closed = self._close_tickets(
                symbol, side, close_qty, close_pool, step, minimum, price, info
            )
            if closed is None or closed[1] + 1e-9 < close_qty:
                if closed is None:
                    return None
                logger.warning(
                    f"[TMGM] Only closed {closed[1]} of {close_qty} {symbol} — "
                    "not opening a remainder"
                )
                return _result(symbol, side, closed)
            fills.append(closed)
        if open_qty > 0:
            opened = self._send_deal(symbol, side, open_qty, price, info, position_ticket=None)
            if opened is None:
                if fills:
                    logger.warning(
                        f"[TMGM] Close of {symbol} filled but the opening remainder failed"
                    )
                    return _result(symbol, side, fills[0])
                return None
            fills.append(opened)

        if not fills:
            logger.warning(f"[TMGM] {side} {aligned} {symbol} had nothing to trade")
            return None
        return _combine(symbol, side, fills)

    def _close_tickets(
        self,
        symbol: str,
        side: str,
        qty: float,
        tickets: list,
        step: float,
        minimum: float,
        price: Optional[float],
        info: object,
    ) -> Optional[tuple[str, float, float, str]]:
        remaining = qty
        filled_qty = 0.0
        notional = 0.0
        last_id = ""
        status = "filled"
        for pos in tickets:
            if remaining + 1e-12 < minimum:
                break
            chunk = align_volume(min(float(pos.volume), remaining), step, minimum)
            if chunk is None:
                continue
            sent = self._send_deal(
                symbol, side, chunk, price, info, position_ticket=int(pos.ticket)
            )
            if sent is None:
                break
            last_id, vol, fill_price, status = sent
            filled_qty += vol
            notional += vol * fill_price
            remaining -= vol
        if filled_qty <= 0:
            logger.warning(f"[TMGM] No {symbol} position ticket to {side} {qty} lots")
            return None
        avg = notional / filled_qty
        return last_id, filled_qty, avg, status

    def _send_deal(
        self,
        symbol: str,
        side: str,
        volume: float,
        price: Optional[float],
        info: object,
        *,
        position_ticket: Optional[int],
    ) -> Optional[tuple[str, float, float, str]]:
        client = self._require_client()
        market = self._settings.use_market_orders or price is None
        buy = side == "BUY"
        for attempt in range(2):
            tick = client.symbol_info_tick(symbol)
            if tick is None or float(tick.bid) <= 0 or float(tick.ask) <= 0:
                logger.error(f"[TMGM] No quote for {symbol}; order not sent")
                return None
            if market:
                action_code = TRADE_ACTION_DEAL
                order_type = ORDER_TYPE_BUY if buy else ORDER_TYPE_SELL
                order_price = float(tick.ask if buy else tick.bid)
            else:
                action_code = TRADE_ACTION_PENDING
                order_type = ORDER_TYPE_BUY_LIMIT if buy else ORDER_TYPE_SELL_LIMIT
                order_price = float(price)
            request: dict[str, object] = {
                "action": action_code,
                "symbol": symbol,
                "volume": volume,
                "type": order_type,
                "price": order_price,
                "deviation": self._settings.deviation,
                "magic": self._settings.magic,
                "comment": "qs-tmgm-close" if position_ticket is not None else "qs-tmgm",
                "type_time": ORDER_TIME_GTC,
                "type_filling": _order_filling(int(getattr(info, "filling_mode", 0))),
            }
            if position_ticket is not None:
                request["position"] = position_ticket
            result = client.order_send(request)
            retcode = None if result is None else int(result.retcode)
            if retcode in RETRY_RETCODES and attempt == 0:
                logger.warning(
                    f"[TMGM] {side} {volume} {symbol} retcode {retcode} "
                    f"({getattr(result, 'comment', '')}) — retrying"
                )
                continue
            if result is None or retcode not in ACCEPTED_RETCODES:
                comment = None if result is None else result.comment
                logger.error(
                    f"[TMGM] {side} {volume} {symbol} rejected: "
                    f"retcode={retcode} comment={comment} error={client.last_error()}"
                )
                return None
            filled = retcode in FILLED_RETCODES
            fill_price = float(getattr(result, "price", 0.0) or 0.0)
            if filled and fill_price <= 0:
                fill_price = order_price
            status = "filled" if filled else "placed"
            order_id = str(getattr(result, "order", "") or getattr(result, "deal", ""))
            logger.info(
                f"[TMGM] {side} {volume}x {symbol} {status} @ {fill_price}  id={order_id}"
                + (f"  position={position_ticket}" if position_ticket is not None else "")
            )
            return order_id, volume if filled else volume, fill_price if filled else order_price, status
        return None

    # ------------------------------------------------------------------
    # Account & positions
    # ------------------------------------------------------------------

    def get_account_summary(self) -> dict:
        account = self._require_client().account_info()
        if account is None:
            raise RuntimeError("MT5 returned no account")
        equity = float(account.equity)
        unrealized = float(account.profit)
        return {
            "net_liquidation": equity,
            "unrealized_pnl": unrealized,
            "realized_pnl": 0.0,
            "buying_power": float(account.margin_free),
        }

    def get_open_positions(self) -> list[dict]:
        rows = []
        for symbol, net in self._net_positions().items():
            rows.append(
                {
                    "symbol": symbol,
                    "pos": net["qty"],
                    "avg_cost": net["avg_price"],
                    "unreal_pnl": net["unreal_pnl"],
                }
            )
        return rows

    def get_positions_for_sync(self) -> list[dict]:
        rows = []
        for symbol, net in self._net_positions().items():
            qty = net["qty"]
            if abs(qty) <= 1e-12:
                continue
            side = "long" if qty > 0 else "short"
            rows.append(
                {
                    "symbol": symbol,
                    "qty": qty,
                    "qty_available": abs(qty),
                    "side": side,
                    "avg_price": net["avg_price"],
                }
            )
        return rows

    def _net_positions(self) -> dict[str, dict]:
        client = self._require_client()
        grouped: dict[str, list] = {}
        for pos in client.positions_get() or []:
            grouped.setdefault(str(pos.symbol), []).append(pos)

        nets: dict[str, dict] = {}
        for symbol, positions in grouped.items():
            longs = [p for p in positions if int(p.type) == POSITION_TYPE_BUY]
            shorts = [p for p in positions if int(p.type) == POSITION_TYPE_SELL]
            long_vol = sum(float(p.volume) for p in longs)
            short_vol = sum(float(p.volume) for p in shorts)
            net_qty = long_vol - short_vol
            if long_vol > 0 and short_vol > 0:
                logger.warning(
                    f"[TMGM] {symbol} has both long {long_vol} and short {short_vol} "
                    "tickets. Reporting the net; flatten both sides before relying on it."
                )
            if abs(net_qty) <= 1e-12:
                continue
            book = longs if net_qty > 0 else shorts
            volume = sum(float(p.volume) for p in book)
            if volume <= 0:
                continue
            avg = sum(float(p.volume) * float(p.price_open) for p in book) / volume
            nets[symbol] = {
                "qty": net_qty,
                "avg_price": avg,
                "unreal_pnl": sum(float(getattr(p, "profit", 0.0) or 0.0) for p in positions),
            }
        return nets

    def _require_client(self):
        if self._client is None:
            raise RuntimeError("TMGM broker is not connected")
        return self._client


def _result(symbol: str, side: str, fill: tuple[str, float, float, str]) -> OrderResult:
    order_id, qty, price, status = fill
    return OrderResult(
        id=order_id,
        symbol=symbol,
        action=side,
        qty=qty,
        price=price if status == "filled" else None,
        status=status,
        raw=fill,
    )


def _combine(symbol: str, side: str, fills: list[tuple[str, float, float, str]]) -> OrderResult:
    qty = sum(item[1] for item in fills)
    notional = sum(item[1] * item[2] for item in fills if item[3] == "filled")
    filled_qty = sum(item[1] for item in fills if item[3] == "filled")
    status = fills[-1][3]
    price = (notional / filled_qty) if filled_qty > 0 else None
    return OrderResult(
        id=fills[-1][0],
        symbol=symbol,
        action=side,
        qty=qty,
        price=price,
        status=status,
        raw=fills,
    )
