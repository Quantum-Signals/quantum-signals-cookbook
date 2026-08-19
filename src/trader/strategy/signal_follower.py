from __future__ import annotations

from datetime import date, datetime, time as dt_time, timezone
from zoneinfo import ZoneInfo

from loguru import logger

from trader.config import Settings
from qs_trader_broker_core import BrokerBase
from trader.models import PerformanceTracker, Signal, sig_label
from trader.strategy.base import StrategyBase

_ET = ZoneInfo("America/New_York")

_TRADING_START = dt_time(9, 45)
_TRADING_END = dt_time(15, 30)
_CLOSE_START = dt_time(15, 55)
_CLOSE_END = dt_time(16, 0)

# Signals arrive for futures symbols; we trade the correlated ETF.
_SIGNAL_TO_TRADE: dict[str, str] = {
    # QS client < 0.5.1 used futures tickers; >= 0.5.1 uses full names
    "ES": "SPY",
    "NQ": "QQQ",
    "S&P500": "SPY",
    "Nasdaq100": "QQQ",
}


class SignalFollowerStrategy(StrategyBase):
    """Intraday strategy that trades 1 share of the mapped ETF (SPY, QQQ)
    for every incoming signal and flattens everything before the close.

    Signals arrive for ES / NQ but orders are placed on SPY / QQQ.

    **Trading window** — 9:45 AM to 3:30 PM ET
        At every N-minute boundary (configurable via execution_interval_minutes,
        default 5) the latest QS signal sets the *active decision* (UP → long,
        DOWN → short).  Each signal minute, 1 share is traded in the active
        direction.

    **Close window** — 3:55 PM to 4:00 PM ET
        All accumulated positions are flattened.

    **Pre-market** — Before 9:45 AM ET (or on day transition)
        Any stale broker positions are logged and closed.
    """

    def __init__(
        self,
        broker: BrokerBase,
        perf: PerformanceTracker,
        settings: Settings,
    ) -> None:
        self.broker = broker
        self.perf = perf
        self._settings = settings

        self._allowed: set[str] = set(settings.allowed_symbols) if settings.allowed_symbols else set()
        self._decision_interval = settings.execution_interval_minutes

        self._active_decision: dict[str, Signal] = {}

        # Net signed quantity and weighted cost accumulated during the day
        # (keyed by signal symbol, e.g. "ES", "NQ").
        self._net_qty: dict[str, int] = {}
        self._total_cost: dict[str, float] = {}

        self._current_date: date | None = None
        self._pre_market_done: bool = False
        self._close_window_done: bool = False
        self._daily_pnl: float = 0.0

    # ------------------------------------------------------------------
    # Position sync — close stale positions at startup
    # ------------------------------------------------------------------
    def sync_positions(self) -> None:
        self._close_stale_positions("startup")

    # ------------------------------------------------------------------
    # Multi-day lifecycle — called by app after EOD flatten + summary
    # ------------------------------------------------------------------
    def reset_for_new_day(self) -> None:
        """Drop yesterday's per-day state. The next incoming signal will
        trigger ``_reset_day`` automatically once we see a new ET date."""
        self._active_decision.clear()
        self._net_qty.clear()
        self._total_cost.clear()
        self._daily_pnl = 0.0
        self._pre_market_done = False
        self._close_window_done = False
        self._current_date = None
        logger.debug("SignalFollowerStrategy: per-day state cleared")

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

        et_dt = _to_eastern(signal.signal_time_utc)
        et_date = et_dt.date()
        et_time = et_dt.time()

        if self._current_date != et_date:
            self._reset_day(et_date)

        if not self._pre_market_done:
            self._close_stale_positions("pre-market")
            self._pre_market_done = True

        self.perf.signal_counts[sig] += 1
        logger.info(
            f"Signal  {sym}  {sig_label(sig)}  model={signal.model}  "
            f"et={et_dt.strftime('%H:%M')}"
        )

        # ---- close window ----
        if _CLOSE_START <= et_time <= _CLOSE_END:
            self._flatten(sym)
            return

        # ---- outside trading hours ----
        if et_time < _TRADING_START or et_time > _TRADING_END:
            logger.debug(f"  -> Outside trading window ({et_time}) — skipping")
            return

        # ---- N-minute decision point (configurable interval) ----
        if et_dt.minute % self._decision_interval == 0:
            if sig == Signal.UP:
                self._active_decision[sym] = Signal.UP
                logger.info(f"  -> Decision: {sym} = LONG for next {self._decision_interval}min")
            elif sig == Signal.DOWN:
                self._active_decision[sym] = Signal.DOWN
                logger.info(f"  -> Decision: {sym} = SHORT for next {self._decision_interval}min")
            else:
                self._active_decision.pop(sym, None)
                logger.info(f"  -> Decision: {sym} = STABLE — pause for next {self._decision_interval}min")
                return

        # ---- execute active decision ----
        decision = self._active_decision.get(sym)
        if decision is None:
            logger.debug(f"  -> No active decision for {sym} — skipping")
            return

        self._open_incremental(sym, decision)

    # ------------------------------------------------------------------
    # Time-driven close window — runs every tick, independent of signals
    # ------------------------------------------------------------------
    def tick(self) -> None:
        if self._close_window_done or self._current_date is None:
            return

        now_et = datetime.now(_ET)
        if now_et.date() != self._current_date:
            return

        et_time = now_et.time()
        if not (_CLOSE_START <= et_time <= _CLOSE_END):
            return

        syms = [s for s, q in self._net_qty.items() if q != 0]
        if syms:
            logger.info(f"Close window ({et_time:%H:%M}) — flattening {len(syms)} position(s)")
            for sym in syms:
                self._flatten(sym)
        self._close_window_done = True

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
        self._close_window_done = False
        self._current_date = new_date

        logger.info(f"New trading day: {new_date}")

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
    # Pre-market / stale-position cleanup
    # ------------------------------------------------------------------
    def _close_stale_positions(self, context: str) -> None:
        """Query the broker for existing positions and close them."""
        pos_list = self.broker.get_positions_for_sync()
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

            contract = self.broker.resolve_contract(sym)
            if contract is None:
                logger.error(f"  -> Cannot resolve contract for {sym} — manual close required")
                continue

            action = "SELL" if qty > 0 else "BUY"
            self.broker.place_order(contract, action, abs(qty))
            logger.info(f"  -> Closed stale {label} {abs(qty)}x {sym}")

    # ------------------------------------------------------------------
    # Order placement — 1 share per signal
    # ------------------------------------------------------------------
    def _open_incremental(self, sym: str, direction: Signal) -> None:
        """Trade 1 share on the mapped ETF for each signal."""
        trade_sym = _SIGNAL_TO_TRADE.get(sym, sym)
        contract = self.broker.resolve_contract(trade_sym)
        if contract is None:
            logger.warning(f"  -> No tradable contract for {trade_sym} — skipping")
            return

        action = "BUY" if direction == Signal.UP else "SELL"

        trade = self.broker.place_order(contract, action, 1)
        if trade is None:
            return

        signed = 1 if action == "BUY" else -1
        self._net_qty[sym] = self._net_qty.get(sym, 0) + signed

        fill_price = trade.price
        if fill_price is None:
            fill_price = self.broker.get_last_price(contract)
        if fill_price is None:
            logger.warning(
                f"  -> {action} 1x {trade_sym} — fill price unknown, "
                f"position tracked (net: {self._net_qty[sym]:+d})"
            )
            return

        self._total_cost[sym] = self._total_cost.get(sym, 0.0) + signed * fill_price

        logger.info(
            f"  -> {action} 1x {trade_sym} @ {fill_price:.2f}  "
            f"(net: {self._net_qty[sym]:+d})"
        )

    def _flatten(self, sym: str) -> None:
        """Close the entire net position for *sym*."""
        net = self._net_qty.get(sym, 0)
        if net == 0:
            return

        trade_sym = _SIGNAL_TO_TRADE.get(sym, sym)
        contract = self.broker.resolve_contract(trade_sym)
        if contract is None:
            logger.warning(f"  -> Cannot close {trade_sym} — no tradable contract")
            return

        action = "SELL" if net > 0 else "BUY"
        abs_qty = abs(net)

        trade = self.broker.place_order(contract, action, abs_qty)
        if trade is None:
            return

        total_cost = self._total_cost.get(sym, 0.0)

        self._net_qty[sym] = 0
        self._total_cost[sym] = 0.0
        self._active_decision.pop(sym, None)

        fill_price = trade.price
        if fill_price is None:
            fill_price = self.broker.get_last_price(contract)
        if fill_price is None:
            logger.warning(f"  -> No fill price closing {trade_sym} — PnL not recorded")
            return

        dollar_pnl = net * fill_price - total_cost

        direction = Signal.UP if net > 0 else Signal.DOWN
        self.perf.record_close(sym, dollar_pnl, direction)
        self._daily_pnl += dollar_pnl

        logger.info(
            f"  -> FLATTEN {'LONG' if net > 0 else 'SHORT'} {abs_qty}x {trade_sym} "
            f"@ {fill_price:.2f}  $PnL={dollar_pnl:+,.2f}"
        )
        cap = self._settings.daily_start_capital
        logger.info(
            f"  -> Daily: ${self._daily_pnl:+,.2f} "
            f"({self._daily_pnl / cap:+.2%} on ${cap:,.0f})  |  "
            f"{self.perf.summary()}"
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

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


