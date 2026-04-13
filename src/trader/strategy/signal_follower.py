from __future__ import annotations

from datetime import date, datetime, time as dt_time, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from loguru import logger

from trader.config import Settings
from trader.gateways.base import GatewayBase
from trader.models import PerformanceTracker, Signal
from trader.strategy.base import StrategyBase

_ET = ZoneInfo("America/New_York")

_PRICE_COLLECT_START = dt_time(9, 15)
_TRADING_START = dt_time(9, 45)
_TRADING_END = dt_time(15, 30)
_CLOSE_START = dt_time(15, 55)
_CLOSE_END = dt_time(16, 0)
_DECISION_INTERVAL = 5  # minutes
_TOTAL_TRADING_MINUTES = 345  # 9:45 → 15:30


class SignalFollowerStrategy(StrategyBase):
    """Intraday strategy that accumulates positions following QS signals and
    flattens everything before the close.

    **Price collection** — 9:15 AM to 9:45 AM ET
        Collects (delayed) market prices for each symbol.  With 15-min delayed
        data this captures the ~9:00–9:30 open range.  The average becomes
        ``day_open_price`` and drives position sizing for the day.

    **Trading window** — 9:45 AM to 3:30 PM ET
        At every 5-minute boundary the latest QS signal sets the *active
        decision* (UP → long, DOWN → short).  Each minute, ``per_minute_qty``
        is added to a fractional accumulator; when it reaches ≥ 1 contract an
        order is placed.

    **Close window** — 3:55 PM to 4:00 PM ET
        All accumulated positions are flattened.

    **Pre-market** — Before 9:45 AM ET (or on day transition)
        Any stale broker positions are logged and closed.
    """

    def __init__(
        self,
        gw: GatewayBase,
        perf: PerformanceTracker,
        settings: Settings,
    ) -> None:
        self.gw = gw
        self.perf = perf
        self._settings = settings

        self._allowed: set[str] = set(settings.allowed_symbols) if settings.allowed_symbols else set()
        self._multipliers = settings.contract_multipliers

        self._active_decision: dict[str, Signal] = {}

        # Net signed quantity and weighted cost accumulated during the day.
        self._net_qty: dict[str, int] = {}
        self._total_cost: dict[str, float] = {}

        self._current_date: date | None = None
        self._pre_market_done: bool = False
        self._daily_pnl: float = 0.0

        # Dynamic position sizing (computed once per day at 9:45)
        self._collected_prices: dict[str, list[float]] = {}
        self._day_open_price: dict[str, float] = {}
        self._per_minute_qty: dict[str, float] = {}
        self._qty_accumulator: dict[str, float] = {}
        self._sizing_ready: bool = False

    # ------------------------------------------------------------------
    # Position sync — close stale positions at startup
    # ------------------------------------------------------------------
    def sync_positions(self) -> None:
        self._close_stale_positions("startup")

    # ------------------------------------------------------------------
    # Main entry — called once per QS signal
    # ------------------------------------------------------------------
    def handle_signal(self, signal: object) -> None:
        sym: str = signal.symbol
        sig: int = signal.signal

        if signal.is_null_signal():
            logger.debug(f"Signal  {sym}  model={signal.model}  -- {signal.code_message}")
            return

        if self._allowed and sym not in self._allowed:
            return

        et_dt = _to_eastern(signal.for_minute_utc)
        et_date = et_dt.date()
        et_time = et_dt.time()

        if self._current_date != et_date:
            self._reset_day(et_date)

        if not self._pre_market_done:
            self._close_stale_positions("pre-market")
            self._pre_market_done = True

        self.perf.signal_counts[sig] += 1
        logger.info(
            f"Signal  {sym}  {_sig_label(sig)}  model={signal.model}  "
            f"et={et_dt.strftime('%H:%M')}"
        )

        # ---- close window ----
        if _CLOSE_START <= et_time <= _CLOSE_END:
            self._flatten(sym)
            return

        # ---- price collection: 9:15 – 9:45 ----
        if _PRICE_COLLECT_START <= et_time < _TRADING_START:
            self._collect_price(sym)
            return

        # ---- outside trading hours ----
        if et_time < _TRADING_START or et_time > _TRADING_END:
            logger.debug(f"  -> Outside trading window ({et_time}) — skipping")
            return

        # ---- compute sizing once per day at first trading signal ----
        if not self._sizing_ready:
            self._compute_sizing()

        # ---- 5-minute decision point ----
        if et_dt.minute % _DECISION_INTERVAL == 0:
            if sig == Signal.UP:
                self._active_decision[sym] = Signal.UP
                logger.info(f"  -> Decision: {sym} = LONG for next {_DECISION_INTERVAL}min")
            elif sig == Signal.DOWN:
                self._active_decision[sym] = Signal.DOWN
                logger.info(f"  -> Decision: {sym} = SHORT for next {_DECISION_INTERVAL}min")
            else:
                self._active_decision.pop(sym, None)
                logger.info(f"  -> Decision: {sym} = STABLE — pause for next {_DECISION_INTERVAL}min")
                return

        # ---- execute active decision ----
        decision = self._active_decision.get(sym)
        if decision is None:
            logger.debug(f"  -> No active decision for {sym} — skipping")
            return

        self._open_incremental(sym, decision)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _reset_day(self, new_date: date) -> None:
        if self._current_date is not None and self._daily_pnl != 0.0:
            cap = self._settings.daily_start_capital
            self._log_daily_summary(self._current_date, cap)

        self._active_decision.clear()
        self._net_qty.clear()
        self._total_cost.clear()
        self._daily_pnl = 0.0
        self._pre_market_done = False
        self._current_date = new_date

        self._collected_prices.clear()
        self._day_open_price.clear()
        self._per_minute_qty.clear()
        self._qty_accumulator.clear()
        self._sizing_ready = False

        logger.info(
            f"New trading day: {new_date}  "
            f"(starting capital: ${self._settings.daily_start_capital:,.0f})"
        )

    def _log_daily_summary(self, day: date, capital: float) -> None:
        ret = self._daily_pnl / capital if capital else 0.0
        logger.info("=" * 60)
        logger.info(
            f"DAY SUMMARY  {day}  |  "
            f"Start=${capital:,.0f}  End=${capital + self._daily_pnl:,.0f}  "
            f"PnL=${self._daily_pnl:+,.2f}  Return={ret:+.2%}"
        )
        logger.info("=" * 60)

    # ------------------------------------------------------------------
    # Price collection & sizing
    # ------------------------------------------------------------------
    def _collect_price(self, sym: str) -> None:
        """Fetch the current (delayed) price and store it for open-price averaging."""
        contract = self.gw.resolve_contract(sym)
        if contract is None:
            return
        price = self.gw.get_last_price(contract)
        if price is not None:
            self._collected_prices.setdefault(sym, []).append(price)
            logger.info(f"  -> Collected price for {sym}: {price:.2f}  (n={len(self._collected_prices[sym])})")
        else:
            logger.warning(f"  -> Could not get price for {sym} during collection window")

    def _compute_sizing(self) -> None:
        """Derive per-minute contract quantities from collected open prices."""
        cap = self._settings.daily_start_capital
        symbols = self._allowed or set(self._settings.symbol_map.keys())

        logger.info("-" * 60)
        logger.info("POSITION SIZING")

        for sym in symbols:
            prices = self._collected_prices.get(sym, [])

            if prices:
                avg = sum(prices) / len(prices)
            else:
                logger.warning(f"  {sym}: no prices collected — requesting now")
                contract = self.gw.resolve_contract(sym)
                if contract is None:
                    continue
                p = self.gw.get_last_price(contract)
                if p is None:
                    logger.error(f"  {sym}: still no price — cannot size, skipping symbol")
                    continue
                avg = p

            self._day_open_price[sym] = avg
            total = cap / avg
            per_min = total / _TOTAL_TRADING_MINUTES
            self._per_minute_qty[sym] = per_min

            logger.info(
                f"  {sym}:  open_price={avg:,.2f}  "
                f"total_contracts={total:,.2f}  "
                f"per_minute={per_min:.4f}  "
                f"(~1 contract every {1 / per_min:.0f} min)"
            )

        logger.info("-" * 60)
        self._sizing_ready = True

    # ------------------------------------------------------------------
    # Pre-market / stale-position cleanup
    # ------------------------------------------------------------------
    def _close_stale_positions(self, context: str) -> None:
        """Query the broker for existing positions and close them."""
        pos_list = self.gw.get_positions_for_sync()
        if not pos_list:
            logger.info(f"[{context}] No existing positions at broker — clean slate")
            return

        logger.warning(f"[{context}] Found {len(pos_list)} existing position(s) — closing:")
        for p in pos_list:
            sym = p["symbol"]
            qty = int(p["qty"])
            avg_price = p["avg_price"]
            label = "LONG" if qty > 0 else "SHORT"
            logger.warning(f"  {label} {abs(qty)}x {sym} @ {avg_price:.2f}")

            contract = self.gw.resolve_contract(sym)
            if contract is None:
                logger.error(f"  -> Cannot resolve contract for {sym} — manual close required")
                continue

            action = "SELL" if qty > 0 else "BUY"
            self.gw.place_order(contract, action, abs(qty))
            logger.info(f"  -> Closed stale {label} {abs(qty)}x {sym}")

    # ------------------------------------------------------------------
    # Order placement with accumulator-based sizing
    # ------------------------------------------------------------------
    def _open_incremental(self, sym: str, direction: Signal) -> None:
        """Accumulate fractional qty; trade when it reaches a whole contract."""
        per_min = self._per_minute_qty.get(sym)
        if per_min is None or per_min <= 0:
            logger.debug(f"  -> No sizing for {sym} — skipping")
            return

        self._qty_accumulator[sym] = self._qty_accumulator.get(sym, 0.0) + per_min
        trade_qty = int(self._qty_accumulator[sym])

        if trade_qty < 1:
            logger.debug(
                f"  -> {sym} accumulator={self._qty_accumulator[sym]:.3f} "
                f"(< 1 contract) — waiting"
            )
            return

        self._qty_accumulator[sym] -= trade_qty

        contract = self.gw.resolve_contract(sym)
        if contract is None:
            logger.warning(f"  -> No tradable contract for {sym} — skipping")
            self._qty_accumulator[sym] += trade_qty
            return

        action = "BUY" if direction == Signal.UP else "SELL"

        trade = self.gw.place_order(contract, action, trade_qty)
        if trade is None:
            self._qty_accumulator[sym] += trade_qty
            return

        fill_price = _extract_fill_price(trade)
        if fill_price is None:
            fill_price = self.gw.get_last_price(contract)
        if fill_price is None:
            logger.warning(f"  -> No fill price for {action} {trade_qty}x {sym}")
            return

        signed = trade_qty if action == "BUY" else -trade_qty
        self._net_qty[sym] = self._net_qty.get(sym, 0) + signed
        self._total_cost[sym] = self._total_cost.get(sym, 0.0) + signed * fill_price

        logger.info(
            f"  -> {action} {trade_qty}x {sym} @ {fill_price:.2f}  "
            f"(net: {self._net_qty[sym]:+d}  accum: {self._qty_accumulator[sym]:.3f})"
        )

    def _flatten(self, sym: str) -> None:
        """Close the entire net position for *sym*."""
        net = self._net_qty.get(sym, 0)
        if net == 0:
            return

        contract = self.gw.resolve_contract(sym)
        if contract is None:
            logger.warning(f"  -> Cannot close {sym} — no tradable contract")
            return

        action = "SELL" if net > 0 else "BUY"
        abs_qty = abs(net)

        trade = self.gw.place_order(contract, action, abs_qty)
        if trade is None:
            return

        fill_price = _extract_fill_price(trade)
        if fill_price is None:
            fill_price = self.gw.get_last_price(contract)
        if fill_price is None:
            logger.warning(f"  -> No fill price closing {sym}")
            return

        total_cost = self._total_cost.get(sym, 0.0)
        point_pnl = net * fill_price - total_cost
        multiplier = self._multipliers.get(sym, 1.0)
        dollar_pnl = point_pnl * multiplier

        direction = Signal.UP if net > 0 else Signal.DOWN
        self.perf.record_close(sym, dollar_pnl, direction)
        self._daily_pnl += dollar_pnl

        logger.info(
            f"  -> FLATTEN {'LONG' if net > 0 else 'SHORT'} {abs_qty}x {sym} "
            f"@ {fill_price:.2f}  point_pnl={point_pnl:+.2f}  "
            f"x{multiplier:.0f}  $PnL={dollar_pnl:+,.2f}"
        )
        cap = self._settings.daily_start_capital
        logger.info(
            f"  -> Daily: ${self._daily_pnl:+,.2f} "
            f"({self._daily_pnl / cap:+.2%} on ${cap:,.0f})  |  "
            f"{self.perf.summary()}"
        )

        self._net_qty[sym] = 0
        self._total_cost[sym] = 0.0
        self._active_decision.pop(sym, None)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _extract_fill_price(trade_ref: object) -> Optional[float]:
    """Get the actual execution price from a trade/order result.

    Works with both ib_async Trade objects (orderStatus.avgFillPrice)
    and SimulatedGateway dicts ({"price": ...}).
    """
    if isinstance(trade_ref, dict):
        p = trade_ref.get("price")
        if p is not None and p > 0:
            return float(p)
        return None

    status = getattr(trade_ref, "orderStatus", None)
    if status is not None:
        avg = getattr(status, "avgFillPrice", 0.0)
        if avg and avg > 0:
            return float(avg)

    fills = getattr(trade_ref, "fills", None)
    if fills:
        prices = [f.execution.avgPrice for f in fills if getattr(f, "execution", None)]
        if prices:
            return sum(prices) / len(prices)

    return None


def _to_eastern(for_minute_utc: object) -> datetime:
    """Convert the signal's UTC timestamp to Eastern Time."""
    if isinstance(for_minute_utc, datetime):
        dt_utc = (
            for_minute_utc
            if for_minute_utc.tzinfo
            else for_minute_utc.replace(tzinfo=timezone.utc)
        )
    elif isinstance(for_minute_utc, (int, float)):
        dt_utc = datetime.fromtimestamp(for_minute_utc, tz=timezone.utc)
    else:
        raw = str(for_minute_utc).replace("Z", "+00:00")
        dt_utc = datetime.fromisoformat(raw)
    return dt_utc.astimezone(_ET)


def _sig_label(sig: int | None) -> str:
    if sig is None:
        return "NONE --"
    return {
        Signal.UP: "UP ^",
        Signal.STABLE: "STABLE ->",
        Signal.DOWN: "DOWN v",
    }.get(sig, f"?({sig})")
