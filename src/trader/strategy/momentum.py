from __future__ import annotations

import time
from datetime import datetime, timezone

from loguru import logger

from trader.config import Settings
from trader.gateways.base import GatewayBase
from trader.models import PerformanceTracker, Position, Signal
from trader.strategy.base import StrategyBase


class MomentumStrategy(StrategyBase):
    """
    Converts a QuantumSignals signal into a broker order action.

    State machine per symbol:
        FLAT  -> long  on UP   signal
        FLAT  -> short on DOWN signal
        LONG  -> close on DOWN / STABLE signal  (then open short if DOWN)
        SHORT -> close on UP   / STABLE signal  (then open long  if UP)
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
        self._positions: dict[str, Position] = {}
        self._last_trade_time: dict[str, float] = {}

    def sync_positions(self) -> None:
        """Seed the bot's position state from existing broker positions."""
        pos_list = self.gw.get_positions_for_sync()
        for p in pos_list:
            sym = p["symbol"]
            qty = int(p["qty"])
            avg_price = p["avg_price"]
            if qty == 0:
                continue
            direction = Signal.UP if qty > 0 else Signal.DOWN
            abs_qty = abs(qty)
            self._positions[sym] = Position(
                symbol=sym,
                direction=direction,
                qty=abs_qty,
                entry_price=avg_price,
                entry_time=datetime.now(timezone.utc),
            )
            label = "LONG" if direction == Signal.UP else "SHORT"
            logger.info(f"  Synced {label} {abs_qty}x {sym} @ {avg_price:.2f} from broker")
        if pos_list:
            logger.info(f"Synced {len(pos_list)} existing position(s) from broker")
        else:
            logger.info("No existing positions at broker")

    def handle_signal(self, signal: object) -> None:
        sym = signal.symbol
        sig = signal.signal
        ts = signal.for_minute_utc

        if signal.is_null_signal():
            logger.debug(f"Signal  {sym}  model={signal.model}  -- {signal.code_message}")
            return

        self.perf.signal_counts[sig] += 1
        logger.info(f"Signal  {sym}  {_sig_label(sig)}  model={signal.model}  ts={ts}")

        now = time.monotonic()
        if now - self._last_trade_time.get(sym, 0) < self._settings.trade_cooldown_seconds:
            logger.debug(f"  -> cooldown active for {sym} — skipping")
            return

        contract = self.gw.resolve_contract(sym)
        if contract is None:
            logger.warning(f"  -> No tradable contract for {sym} — skipping (check symbol map / contract month / IB permissions)")
            return

        open_pos = self._positions.get(sym)

        if open_pos is None:
            if sig == Signal.UP:
                self._open_position(contract, sym, Signal.UP)
            elif sig == Signal.DOWN:
                self._open_position(contract, sym, Signal.DOWN)
        else:
            if open_pos.direction == Signal.UP and sig in (Signal.DOWN, Signal.STABLE):
                self._close_position(contract, open_pos)
                if sig == Signal.DOWN:
                    self._open_position(contract, sym, Signal.DOWN)

            elif open_pos.direction == Signal.DOWN and sig in (Signal.UP, Signal.STABLE):
                self._close_position(contract, open_pos)
                if sig == Signal.UP:
                    self._open_position(contract, sym, Signal.UP)

    def _open_position(self, contract: object, sym: str, direction: Signal) -> None:
        qty = self._settings.position_sizes.get(sym, 1)
        action = "BUY" if direction == Signal.UP else "SELL"
        price = self.gw.get_last_price(contract)

        if price is None:
            logger.warning(f"  -> Skipping {action} {qty}x {sym} — no market data available")
            return

        trade = self.gw.place_order(contract, action, qty, price)
        if trade is None:
            return

        self._positions[sym] = Position(
            symbol=sym,
            direction=direction,
            qty=qty,
            entry_price=price,
            entry_time=datetime.now(timezone.utc),
            trade_ref=trade,
        )
        self._last_trade_time[sym] = time.monotonic()
        logger.info(
            f"  -> Opened {'LONG' if direction == Signal.UP else 'SHORT'} "
            f"{qty}x {sym} @ {price}"
        )

    def _close_position(self, contract: object, pos: Position) -> None:
        close_action = "SELL" if pos.direction == Signal.UP else "BUY"
        price = self.gw.get_last_price(contract)
        self.gw.place_order(contract, close_action, pos.qty, price)
        self._last_trade_time[pos.symbol] = time.monotonic()

        if price and pos.entry_price:
            raw_pnl = (price - pos.entry_price) * pos.qty
            if pos.direction == Signal.DOWN:
                raw_pnl = -raw_pnl
        else:
            raw_pnl = 0.0

        self.perf.record_close(pos.symbol, raw_pnl, pos.direction)
        del self._positions[pos.symbol]

        logger.info(
            f"  -> Closed {'LONG' if pos.direction == Signal.UP else 'SHORT'} "
            f"{pos.qty}x {pos.symbol} @ {price}  est.PnL={raw_pnl:+.2f}"
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
