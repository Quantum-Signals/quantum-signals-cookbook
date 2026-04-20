from __future__ import annotations

import time

from loguru import logger

from trader.config import Settings
from trader.gateways.base import GatewayBase
from trader.models import PerformanceTracker, Signal
from trader.strategy.base import StrategyBase

_TRADING_MINUTES_PER_DAY = 390  # 6.5 hours for US equities
_TARGET_DAILY_NOTIONAL = 100_000.0

_SIGNAL_TO_TRADE: dict[str, str] = {
    "ES": "SPY",
    "NQ": "QQQ",
}


def _compute_order_qty(interval_minutes: int, price: float) -> int:
    """Return the per-execution order size so daily notional ≈ $100k.

    With a 5-min interval there are 78 executions/day.  At price $545
    (SPY) that gives qty=round(100_000 / (78*545)) ≈ 2.  If the interval
    doubles to 10 min (39 execs), qty doubles to keep the daily spend
    constant.
    """
    executions_per_day = _TRADING_MINUTES_PER_DAY // interval_minutes
    if executions_per_day <= 0 or price <= 0:
        return 1
    raw = _TARGET_DAILY_NOTIONAL / (executions_per_day * price)
    return max(1, round(raw))


class MomentumStrategy(StrategyBase):
    """
    Incremental momentum strategy that decides and executes at a
    user-configurable interval (default 5 minutes).

    At each interval the latest signal adjusts the net position by
    *qty* units (scaled so daily notional ≈ $100 000):

        UP     → BUY  qty  (net position += qty)
        DOWN   → SELL qty  (net position -= qty)
        STABLE → hold      (no trade, timer still resets)

    Positions accumulate over time.
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

        self._interval_minutes = settings.execution_interval_minutes
        self._interval_s = self._interval_minutes * 60

        self._allowed: set[str] = set(settings.allowed_symbols) if settings.allowed_symbols else set()

        self._net_qty: dict[str, int] = {}
        self._total_cost: dict[str, float] = {}
        self._last_exec_time: dict[str, float] = {}

        logger.info(
            f"MomentumStrategy: interval={self._interval_minutes}min  "
            f"execs/day={_TRADING_MINUTES_PER_DAY // self._interval_minutes}  "
            f"target_daily_notional=${_TARGET_DAILY_NOTIONAL:,.0f}"
        )

    # ------------------------------------------------------------------
    # Position sync
    # ------------------------------------------------------------------
    def sync_positions(self) -> None:
        """Seed the bot's position state from existing broker positions."""
        pos_list = self.gw.get_positions_for_sync()
        for p in pos_list:
            sym = p["symbol"]
            qty = int(p["qty"])
            avg_price = p["avg_price"]
            if qty == 0:
                continue
            self._net_qty[sym] = qty
            self._total_cost[sym] = qty * avg_price
            label = "LONG" if qty > 0 else "SHORT"
            logger.info(f"  Synced {label} {abs(qty)}x {sym} @ {avg_price:.2f} from broker")
        if pos_list:
            logger.info(f"Synced {len(pos_list)} existing position(s) from broker")
        else:
            logger.info("No existing positions at broker")

    # ------------------------------------------------------------------
    # Signal handler — gated to one execution per 5 minutes per symbol
    # ------------------------------------------------------------------
    def handle_signal(self, signal: object) -> None:
        sym = signal.symbol
        sig = signal.signal
        ts = signal.for_minute_utc

        if signal.is_null_signal():
            logger.debug(f"Signal  {sym}  model={signal.model}  -- {signal.code_message}")
            return

        if self._allowed and sym not in self._allowed:
            logger.debug(f"  -> {sym} not in ALLOWED_SYMBOLS — skipping")
            return

        if sym not in _SIGNAL_TO_TRADE:
            logger.debug(f"  -> {sym} has no ETF mapping — skipping")
            return

        self.perf.signal_counts[sig] += 1
        logger.info(f"Signal  {sym}  {_sig_label(sig)}  model={signal.model}  ts={ts}")

        now = time.monotonic()
        elapsed = now - self._last_exec_time.get(sym, 0)
        if elapsed < self._interval_s:
            remaining = self._interval_s - elapsed
            logger.debug(
                f"  -> Next execution for {sym} in {remaining:.0f}s — skipping"
            )
            return

        if sig == Signal.STABLE:
            logger.info(f"  -> STABLE — holding {sym} (net: {self._net_qty.get(sym, 0):+d})")
            self._last_exec_time[sym] = now
            return

        self._execute(sym, sig, now)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def _execute(self, sym: str, sig: int, now: float) -> None:
        trade_sym = _SIGNAL_TO_TRADE[sym]
        contract = self.gw.resolve_contract(trade_sym)
        if contract is None:
            logger.warning(
                f"  -> No tradable contract for {trade_sym} — skipping "
                f"(check symbol map / contract month / IB permissions)"
            )
            return

        price = self.gw.get_last_price(contract)
        if price is None:
            logger.warning(f"  -> No market data for {trade_sym} — skipping")
            return

        qty = _compute_order_qty(self._interval_minutes, price)
        action = "BUY" if sig == Signal.UP else "SELL"
        signed_qty = qty if sig == Signal.UP else -qty

        prev_net = self._net_qty.get(sym, 0)
        new_net = prev_net + signed_qty

        crosses_zero = prev_net != 0 and (prev_net > 0) != (new_net > 0) and new_net != 0
        goes_flat = prev_net != 0 and new_net == 0

        trade = self.gw.place_order(contract, action, qty, price)
        if trade is None:
            return

        self._last_exec_time[sym] = now

        if goes_flat or crosses_zero:
            self._record_pnl(sym, price, prev_net, close_qty=prev_net)
            if crosses_zero:
                self._net_qty[sym] = new_net
                self._total_cost[sym] = new_net * price
            else:
                self._net_qty[sym] = 0
                self._total_cost[sym] = 0.0
        else:
            self._net_qty[sym] = new_net
            self._total_cost[sym] = self._total_cost.get(sym, 0.0) + signed_qty * price

        logger.info(
            f"  -> {action} {qty}x {trade_sym} @ {price:.2f}  "
            f"[signal={sym}]  (net: {prev_net:+d} -> {self._net_qty[sym]:+d})"
        )

    def _record_pnl(
        self, sym: str, exit_price: float, prev_net: int, close_qty: int
    ) -> None:
        total_cost = self._total_cost.get(sym, 0.0)
        if prev_net == 0:
            return
        avg_entry = total_cost / prev_net
        pnl = (exit_price - avg_entry) * close_qty
        direction = Signal.UP if prev_net > 0 else Signal.DOWN
        self.perf.record_close(sym, pnl, direction)
        logger.info(
            f"  -> Closed {'LONG' if prev_net > 0 else 'SHORT'} "
            f"{abs(close_qty)}x {sym}  est.PnL={pnl:+.2f}"
        )
        logger.info(f"  -> {self.perf.summary()}")


def _sig_label(sig: int | None) -> str:
    if sig is None:
        return "NONE --"
    return {
        Signal.UP: "UP ^",
        Signal.STABLE: "STABLE ->",
        Signal.DOWN: "DOWN v",
    }.get(sig, f"?({sig})")
