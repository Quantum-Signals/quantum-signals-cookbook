from __future__ import annotations

import asyncio
import ssl
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from loguru import logger

from qs_trader_broker_core.base import BrokerBase, OrderResult
from qs_trader_symbols import rithmic_root_specs, rithmic_symbol_map

from .front_month import cme_front_month_symbol
from .settings import RithmicSettings

_CONTRACT_MONTH_CODES = "FGHJKMNQUVXZ"

# ---------------------------------------------------------------------------
# Rithmic / OMNE attribution (required for commercial conformance)
# ---------------------------------------------------------------------------
_RITHMIC_ATTRIBUTION = """
================================================================================
                      ╔═══════════════════════════════════════╗
                      ║   TRADING PLATFORM BY RITHMIC™        ║
                      ╚═══════════════════════════════════════╝

                           ┌─────────────────────────┐
                           │    POWERED BY OMNE™     │
                           └─────────────────────────┘

  The R | Protocol API™ software is Copyright © 2026 by Rithmic, LLC.
  All rights reserved.

  Trading Platform by Rithmic™ is a trademark of Rithmic, LLC.
  All rights reserved.

  The OMNE™ software is Copyright © 2026 by Omnesys, LLC and
  Omnesys Technologies, Inc. All rights reserved.

  Powered by OMNE™ is a trademark of Omnesys, LLC and
  Omnesys Technologies, Inc. All rights reserved.
================================================================================
"""


def _log_rithmic_attribution() -> None:
    """Print the required Rithmic/OMNE logos and notices after connect."""
    for line in _RITHMIC_ATTRIBUTION.strip().splitlines():
        logger.info(line)


@dataclass(frozen=True)
class RithmicContract:
    """Opaque contract: *trading_symbol* is what Rithmic wants (e.g. ``ESU6``).

    ``root`` is the product code (``ES`` / ``NQ``) used as the position dict
    ``symbol`` so EOD multipliers and strategy sync stay on QS names, not
    the ETF tickers the strategy still passes into ``resolve_contract``.
    """

    trading_symbol: str
    exchange: str
    root: str

    @property
    def position_key(self) -> str:
        return self.root


def _root_from_trading_symbol(symbol: str) -> Optional[str]:
    """``ESU6`` / ``NQU26`` → ``ES`` / ``NQ``; otherwise ``None``."""
    if len(symbol) >= 3 and symbol[-1].isdigit():
        i = len(symbol) - 1
        while i >= 0 and symbol[i].isdigit():
            i -= 1
        if i >= 0 and symbol[i] in _CONTRACT_MONTH_CODES:
            root = symbol[:i]
            if root:
                return root
    return None


def _as_float(value: object, default: float = 0.0) -> float:
    parsed = _try_float(value)
    return default if parsed is None else parsed


def _try_float(value: object) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _row_get(row: object, name: str, default: object = None) -> object:
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def _row_int(row: object, name: str) -> int:
    raw = _row_get(row, name, 0) or 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _first_number(row: object, *names: str) -> float:
    """First field that is actually present (empty string skipped; 0 kept)."""
    for name in names:
        parsed = _try_float(_row_get(row, name, None))
        if parsed is not None:
            return parsed
    return 0.0


def _root_from_row(row: object) -> str:
    trading = str(_row_get(row, "symbol", "") or "")
    product = str(_row_get(row, "product_code", "") or "")
    return product or _root_from_trading_symbol(trading) or ""


def _signed_qty(row: object) -> int:
    """Rithmic Test often leaves ``net_quantity`` at 0 and fills other fields."""
    net = _row_int(row, "net_quantity")
    if net:
        return net
    buy = _row_int(row, "fill_buy_qty")
    sell = _row_int(row, "fill_sell_qty")
    if buy or sell:
        return buy - sell
    buy = _row_int(row, "buy_qty")
    sell = _row_int(row, "sell_qty")
    if buy or sell:
        return buy - sell
    # ``open_position_quantity`` is typically unsigned; use as last resort.
    return _row_int(row, "open_position_quantity")


def _parse_instrument_position(row: object) -> Optional[dict]:
    trading = str(_row_get(row, "symbol", "") or "")
    root = _root_from_row(row)
    qty = _signed_qty(row)
    if qty == 0 or not root:
        return None
    return {
        "symbol": root,
        "qty": float(qty),
        "avg_price": _as_float(_row_get(row, "avg_open_fill_price", None)),
        "unreal_pnl": _first_number(row, "open_position_pnl", "day_open_pnl"),
        "trading_symbol": trading,
        "account_id": str(_row_get(row, "account_id", "") or ""),
        "side": "long" if qty > 0 else "short",
    }


def _account_from_row(row: object) -> dict:
    return {
        "net_liquidation": _first_number(
            row, "account_balance", "mtm_account", "margin_balance", "cash_on_hand"
        ),
        "unrealized_pnl": _first_number(row, "day_open_pnl", "open_position_pnl"),
        "realized_pnl": _first_number(row, "day_closed_pnl", "closed_position_pnl"),
        "buying_power": _first_number(row, "available_buying_power"),
    }


def _market_data_bits() -> int:
    from async_rithmic import DataType

    return int(DataType.LAST_TRADE | DataType.BBO)


def _price_from_tick(data: dict) -> Optional[float]:
    """Last trade, else BBO mid (or a single side). Test often has quotes, few prints."""
    trade = _try_float(data.get("trade_price"))
    if trade and trade > 0:
        return trade
    bid = _try_float(data.get("bid_price"))
    ask = _try_float(data.get("ask_price")) or _try_float(data.get("offer_price"))
    if bid and bid > 0 and ask and ask > 0:
        return (bid + ask) / 2.0
    if ask and ask > 0:
        return ask
    if bid and bid > 0:
        return bid
    return None


class _LoopThread:
    """Run asyncio on a dedicated thread so the trader's sync loop can block on it."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._main, name="rithmic-loop", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("Rithmic asyncio loop failed to start")

    def _main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def run(self, coro: object, timeout: float = 30.0) -> object:
        if self._loop is None:
            raise RuntimeError("Rithmic asyncio loop is not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)  # type: ignore[arg-type]
        return future.result(timeout=timeout)

    def stop(self) -> None:
        loop = self._loop
        thread = self._thread
        if loop is None or thread is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=8.0)
        self._loop = None
        self._thread = None
        self._ready.clear()


class RithmicBroker(BrokerBase):
    """Broker backed by Rithmic's Protocol API (async_rithmic over WebSocket).

    The strategy still maps QS symbols to SPY/QQQ; this broker remaps those
    (and ES/NQ) to the CME front-month future. Positions are keyed by product
    root (``ES`` / ``NQ``) so ``contract_multipliers`` and strategy sync work.

    Configuration is loaded from ``RITHMIC_*`` env vars via ``RithmicSettings``.
    """

    supports_fractional: bool = False

    DEFAULT_SYMBOL_MAP: dict[str, dict[str, str]] = rithmic_symbol_map()

    def __init__(
        self,
        settings: Optional[RithmicSettings] = None,
        *,
        symbol_map: Optional[dict[str, dict[str, str]]] = None,
    ) -> None:
        self._settings = settings if settings is not None else RithmicSettings()
        self._symbol_map = symbol_map or self.DEFAULT_SYMBOL_MAP
        self._loop = _LoopThread()
        self._client: object | None = None
        self._last_prices: dict[str, float] = {}
        self._subscribed: set[tuple[str, str]] = set()
        self._fills: dict[str, dict] = {}
        self._fill_events: dict[str, threading.Event] = {}
        self._price_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._resolved: dict[str, RithmicContract] = {}
        self._front_month_cache: dict[str, str] = {}
        self._positions: dict[str, dict] = {}
        self._account: dict = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        if not self._settings.user or not self._settings.password:
            raise ValueError(
                "RITHMIC_USER and RITHMIC_PASSWORD are required when TRADER_BROKER=rithmic"
            )

        from async_rithmic import RithmicClient, SysInfraType

        self._loop.start()

        async def _connect() -> None:
            client = RithmicClient(
                user=self._settings.user,
                password=self._settings.password,
                system_name=self._settings.system_name,
                app_name=self._settings.app_name,
                app_version=self._settings.app_version,
                url=self._settings.gateway_url,
            )
            cert = self._settings.ssl_cert
            if cert:
                path = Path(cert)
                if not path.is_file():
                    raise FileNotFoundError(f"RITHMIC_SSL_CERT not found: {cert}")
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ctx.load_verify_locations(path)
                client.ssl_context = ctx

            client.on_tick += self._on_tick
            client.on_rithmic_order_notification += self._on_order_notification
            client.on_exchange_order_notification += self._on_order_notification
            client.on_instrument_pnl_update += self._on_instrument_pnl
            client.on_account_pnl_update += self._on_account_pnl

            await client.connect(
                plants=[
                    SysInfraType.TICKER_PLANT,
                    SysInfraType.ORDER_PLANT,
                    SysInfraType.PNL_PLANT,
                ]
            )
            self._client = client
            self._resolved.clear()
            self._front_month_cache.clear()
            with self._state_lock:
                self._positions.clear()
                self._account.clear()
            try:
                await client.subscribe_to_pnl_updates()
            except Exception as exc:
                logger.warning(f"[Rithmic] PnL subscribe failed (positions may still work): {exc}")

            # Pre-subscribe front months for every catalog instrument on Rithmic.
            for root, exchange in rithmic_root_specs():
                trading = None
                try:
                    trading = await client.get_front_month_contract(root, exchange)
                except Exception as exc:
                    if exchange in ("CME", "CBOT"):
                        trading = cme_front_month_symbol(root)
                        logger.warning(
                            f"[Rithmic] Front-month lookup failed for {root}/{exchange}: {exc} "
                            f"— subscribing calendar contract {trading}"
                        )
                    else:
                        logger.warning(
                            f"[Rithmic] Front-month lookup failed for {root}/{exchange}: {exc} "
                            "— will subscribe on first quote request"
                        )
                if not trading and exchange in ("CME", "CBOT"):
                    trading = cme_front_month_symbol(root)
                if not trading:
                    continue
                try:
                    await client.subscribe_to_market_data(
                        trading, exchange, _market_data_bits()
                    )
                    self._subscribed.add((trading, exchange))
                    self._remember_front_month(root, exchange, trading)
                    logger.info(f"[Rithmic] Subscribed last-trade+BBO {trading} {exchange}")
                except Exception as exc:
                    logger.warning(
                        f"[Rithmic] Could not subscribe {root} ({trading}): {exc}"
                    )

        self._loop.run(_connect(), timeout=45.0)
        client = self._client
        accounts = getattr(client, "accounts", None) or []
        account_ids = [getattr(a, "account_id", str(a)) for a in accounts]
        logger.success(
            f"Connected to Rithmic ({self._settings.system_name})  "
            f"fcm={getattr(client, 'fcm_id', '')}  "
            f"ib={getattr(client, 'ib_id', '')}  "
            f"accounts={account_ids or ['(none)']}"
        )
        _log_rithmic_attribution()

    def disconnect(self) -> None:
        client = self._client
        if client is not None:
            try:
                self._loop.run(client.disconnect(), timeout=10.0)
            except Exception as exc:
                logger.warning(f"[Rithmic] Disconnect error: {exc}")
            self._client = None
        self._loop.stop()
        logger.info("Rithmic broker disconnected")

    def startup_entitlement_warnings(self, canonical_symbols: list[str]) -> list[str]:
        warnings = super().startup_entitlement_warnings(canonical_symbols)
        if self._client is None:
            warnings.append("Rithmic client not connected — entitlement checks incomplete")
        if not self._settings.user or not self._settings.password:
            warnings.append("Rithmic credentials not configured")
        if self._settings.account:
            accounts = getattr(self._client, "accounts", None) or []
            account_ids = {getattr(a, "account_id", str(a)) for a in accounts}
            if account_ids and self._settings.account not in account_ids:
                warnings.append(
                    f"RITHMIC_ACCOUNT={self._settings.account} not in connected "
                    f"accounts {sorted(account_ids)}"
                )
        return warnings

    # ------------------------------------------------------------------
    # Contract resolution
    # ------------------------------------------------------------------

    def resolve_contract(self, qs_symbol: str) -> Optional[RithmicContract]:
        cached = self._resolved.get(qs_symbol)
        if cached is not None:
            return cached

        spec = self._symbol_map.get(qs_symbol)
        if spec is None:
            root = _root_from_trading_symbol(qs_symbol)
            if root is None:
                logger.debug(f"[Rithmic] '{qs_symbol}' not in symbol map — skipping")
                return None
            exchange = "CME"
            trading = qs_symbol
            contract = RithmicContract(trading_symbol=trading, exchange=exchange, root=root)
            self._resolved[qs_symbol] = contract
            logger.debug(f"[Rithmic] Resolved '{qs_symbol}' → {contract.trading_symbol}")
            return contract

        root = spec["root"]
        exchange = spec["exchange"]
        trading = self._front_month(root, exchange)
        contract = RithmicContract(trading_symbol=trading, exchange=exchange, root=root)
        self._remember_front_month(root, exchange, trading)
        logger.debug(f"[Rithmic] Resolved '{qs_symbol}' → {trading} ({exchange})")
        return contract

    def _remember_front_month(self, root: str, exchange: str, trading: str) -> None:
        self._front_month_cache[root] = trading
        contract = RithmicContract(trading_symbol=trading, exchange=exchange, root=root)
        for key, spec in self._symbol_map.items():
            if spec.get("root") == root:
                self._resolved[key] = contract

    def _front_month(self, root: str, exchange: str) -> str:
        cached = self._front_month_cache.get(root)
        if cached:
            return cached
        client = self._client
        if client is not None:
            try:
                trading = self._loop.run(
                    client.get_front_month_contract(root, exchange),
                    timeout=15.0,
                )
                if trading:
                    self._front_month_cache[root] = str(trading)
                    return str(trading)
            except Exception as exc:
                logger.warning(
                    f"[Rithmic] Front-month lookup failed for {root}/{exchange}: {exc} "
                    "— using calendar fallback"
                )
        fallback = cme_front_month_symbol(root)
        self._front_month_cache[root] = fallback
        return fallback

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def get_last_price(self, contract: RithmicContract) -> Optional[float]:
        self._ensure_subscribed(contract)
        key = contract.trading_symbol
        with self._price_lock:
            cached = self._last_prices.get(key)
        if cached is not None:
            return cached

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            time.sleep(0.1)
            with self._price_lock:
                cached = self._last_prices.get(key)
            if cached is not None:
                return cached
        logger.warning(
            f"[Rithmic] No last trade or BBO yet for {key} "
            "(Rithmic Test often has a quiet tape — will retry on the next signal)"
        )
        return None

    def _ensure_subscribed(self, contract: RithmicContract) -> None:
        key = (contract.trading_symbol, contract.exchange)
        if key in self._subscribed or self._client is None:
            return
        try:
            self._loop.run(
                self._client.subscribe_to_market_data(
                    contract.trading_symbol, contract.exchange, _market_data_bits()
                ),
                timeout=15.0,
            )
            self._subscribed.add(key)
        except Exception as exc:
            logger.warning(f"[Rithmic] Subscribe failed for {contract.trading_symbol}: {exc}")

    def _on_tick(self, data: dict) -> None:
        symbol = data.get("symbol") or data.get("trading_symbol")
        if not symbol:
            return
        price = _price_from_tick(data)
        if price is None:
            return
        with self._price_lock:
            self._last_prices[str(symbol)] = price

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    def place_order(
        self,
        contract: RithmicContract,
        action: str,
        qty: int | float,
        price: Optional[float] = None,
        position_intent: Optional[str] = None,
    ) -> Optional[OrderResult]:
        from async_rithmic import OrderType, TransactionType

        if self._client is None:
            logger.error("[Rithmic] place_order called before connect")
            return None

        order_qty = int(round(float(qty)))
        if order_qty < 1:
            logger.warning(f"[Rithmic] Skipping sub-contract qty={qty} for {contract.trading_symbol}")
            return None

        side = TransactionType.BUY if action.upper() == "BUY" else TransactionType.SELL
        order_type = (
            OrderType.MARKET
            if self._settings.use_market_orders or price is None
            else OrderType.LIMIT
        )
        order_id = uuid.uuid4().hex[:12]
        event = threading.Event()
        self._fill_events[order_id] = event

        kwargs: dict = {}
        if order_type == OrderType.LIMIT and price is not None:
            kwargs["price"] = float(price)
        if self._settings.account:
            kwargs["account_id"] = self._settings.account

        logger.debug(
            f"[Rithmic] Submit {action.upper()} {order_qty}x {contract.trading_symbol} "
            f"({order_type}) id={order_id} intent={position_intent}"
        )
        try:
            self._loop.run(
                self._client.submit_order(
                    order_id,
                    contract.trading_symbol,
                    contract.exchange,
                    order_qty,
                    side,
                    order_type,
                    **kwargs,
                ),
                timeout=30.0,
            )
        except Exception as exc:
            logger.error(f"[Rithmic] Order submission failed for {contract.trading_symbol}: {exc}")
            self._fill_events.pop(order_id, None)
            return None

        event.wait(timeout=8.0)
        fill = self._fills.pop(order_id, {})
        self._fill_events.pop(order_id, None)
        fill_price = _as_float(fill.get("price"), 0.0) or None
        if fill_price is None:
            fill_price = self.get_last_price(contract)
        status = str(fill.get("status") or "submitted")
        native_id = str(fill.get("basket_id") or order_id)

        logger.info(
            f"[Rithmic] {action.upper()} {order_qty}x {contract.trading_symbol}  "
            f"fill_price={fill_price}  id={native_id}"
        )
        self._apply_local_fill(contract, action, order_qty, fill_price)
        return OrderResult(
            id=native_id,
            symbol=contract.root,
            action=action.upper(),
            qty=order_qty,
            price=fill_price,
            status=status,
            raw=fill or None,
        )

    def _on_order_notification(self, note: object) -> None:
        tag = str(getattr(note, "user_tag", "") or "")
        if not tag or tag not in self._fill_events:
            return
        price = _as_float(getattr(note, "price", 0), 0.0)
        status = str(getattr(note, "status", "") or "")
        basket_id = str(getattr(note, "basket_id", "") or "")
        prev = self._fills.get(tag, {})
        merged = {
            "price": price or prev.get("price") or 0.0,
            "status": status or prev.get("status") or "",
            "basket_id": basket_id or prev.get("basket_id") or "",
            "raw": note,
        }
        self._fills[tag] = merged
        # COMPLETE / complete: the sample used this as "order is done".
        notify = getattr(note, "notify_type", None)
        notify_name = str(getattr(notify, "name", notify) or "").upper()
        if price or "COMPLETE" in notify_name or status.lower() == "complete":
            self._fill_events[tag].set()

    # ------------------------------------------------------------------
    # Account & positions
    # ------------------------------------------------------------------

    def get_account_summary(self) -> dict:
        self._refresh_account_snapshot()
        with self._state_lock:
            if self._account:
                return dict(self._account)
        return {
            "net_liquidation": 0.0,
            "unrealized_pnl": 0.0,
            "realized_pnl": 0.0,
            "buying_power": 0.0,
        }

    def get_open_positions(self) -> list[dict]:
        return [
            {
                "symbol": p["symbol"],
                "pos": p["qty"],
                "avg_cost": p["avg_price"],
                "unreal_pnl": p.get("unreal_pnl", 0.0),
            }
            for p in self._position_rows()
        ]

    def get_positions_for_sync(self) -> list[dict]:
        return [
            {
                "symbol": p["symbol"],
                "qty": p["qty"],
                "qty_available": abs(p["qty"]),
                "side": "long" if p["qty"] > 0 else "short",
                "avg_price": p["avg_price"],
            }
            for p in self._position_rows()
        ]

    def _account_kwargs(self) -> dict:
        kwargs: dict = {}
        if self._settings.account:
            kwargs["account_id"] = self._settings.account
        if self._settings.fcm:
            kwargs["fcm_id"] = self._settings.fcm
        if self._settings.ib:
            kwargs["ib_id"] = self._settings.ib
        return kwargs

    def _on_instrument_pnl(self, row: object) -> None:
        self._ingest_instrument(row)

    def _on_account_pnl(self, row: object) -> None:
        parsed = _account_from_row(row)
        with self._state_lock:
            self._account = parsed

    def _ingest_instrument(self, row: object) -> None:
        if self._settings.account:
            acct = str(_row_get(row, "account_id", "") or "")
            if acct and acct != self._settings.account:
                return
        parsed = _parse_instrument_position(row)
        root = parsed["symbol"] if parsed else _root_from_row(row)
        if not root:
            return
        with self._state_lock:
            if parsed is None:
                self._positions.pop(root, None)
            else:
                self._positions[root] = parsed

    def _apply_local_fill(
        self,
        contract: RithmicContract,
        action: str,
        qty: int,
        price: Optional[float],
    ) -> None:
        signed = qty if action.upper() == "BUY" else -qty
        root = contract.root
        with self._state_lock:
            prev = self._positions.get(root)
            old_qty = float(prev["qty"]) if prev else 0.0
            new_qty = old_qty + signed
            if abs(new_qty) < 1e-9:
                self._positions.pop(root, None)
                return
            old_avg = float(prev["avg_price"]) if prev else 0.0
            if price and ((old_qty >= 0 and signed > 0) or (old_qty <= 0 and signed < 0)):
                avg = (old_avg * abs(old_qty) + price * abs(signed)) / abs(new_qty)
            elif price and abs(old_qty) < 1e-9:
                avg = price
            else:
                avg = old_avg or (price or 0.0)
            self._positions[root] = {
                "symbol": root,
                "qty": new_qty,
                "avg_price": avg,
                "unreal_pnl": float(prev["unreal_pnl"]) if prev else 0.0,
                "trading_symbol": contract.trading_symbol,
                "account_id": self._settings.account,
                "side": "long" if new_qty > 0 else "short",
            }

    def _refresh_account_snapshot(self) -> None:
        if self._client is None:
            return
        try:
            rows = self._loop.run(
                self._client.list_account_summary(**self._account_kwargs()),
                timeout=20.0,
            ) or []
        except Exception as exc:
            logger.warning(f"[Rithmic] Account snapshot failed: {exc}")
            return
        if not rows:
            return
        parsed = _account_from_row(rows[-1])
        with self._state_lock:
            self._account = parsed

    def _refresh_position_snapshot(self) -> None:
        if self._client is None:
            return
        try:
            rows = self._loop.run(
                self._client.list_positions(**self._account_kwargs()),
                timeout=20.0,
            ) or []
        except Exception as exc:
            logger.warning(f"[Rithmic] Position snapshot failed: {exc}")
            return
        if not rows:
            with self._state_lock:
                cached = list(self._positions)
            if cached:
                logger.debug(
                    f"[Rithmic] Position snapshot empty — keeping cached {cached}"
                )
            return
        for row in rows:
            self._ingest_instrument(row)

    def _position_rows(self) -> list[dict]:
        self._refresh_position_snapshot()
        with self._state_lock:
            return [dict(p) for p in self._positions.values()]
