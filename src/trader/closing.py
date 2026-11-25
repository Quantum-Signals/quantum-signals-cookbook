"""Closing positions until the broker confirms the account is flat."""

from __future__ import annotations

import os
import signal as os_signal
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from loguru import logger

from qs_trader_broker_core import BrokerBase, OrderResult
from qs_trader_symbols import position_symbol_to_canonical
from trader.config import Settings
from trader.models import PerformanceTracker, Signal
from trader.reporting import describe_open

_READ_TRIES = 3  # per position read
_SETTLE_POLLS = 5  # re-reads while waiting for closing fills to show
_SETTLE_WAIT_S = 2.0
_FLAT_QTY = 1e-9

# Seconds between attempts. At end of day the bot keeps running, so it can keep
# trying; a stopping bot has to finish.
ONCE: Sequence[float] = (2, 2)
STOPPING: Sequence[float] = (2, 2, 10)
END_OF_DAY: Sequence[float] = (2, 2, *[60] * 10)


class StopGuard:
    """Turn every stop request into one orderly close of the positions.

    Ctrl-C (SIGINT), ``kill`` and ``docker stop`` (SIGTERM) and a closed terminal
    (SIGHUP) all raise ``KeyboardInterrupt`` the first time. From then on (or
    while ``closing()`` is active) further ones do not interrupt, because that
    would leave positions open. ``FORCE_QUIT_PRESSES`` further Ctrl-C presses
    still abort, as an escape hatch.

    The switch happens inside the handler, so there is no gap between the first
    Ctrl-C and the shutdown code in which a second one could get through.
    """

    FORCE_QUIT_PRESSES = 5
    SIGNALS = tuple(
        getattr(os_signal, name) for name in ("SIGINT", "SIGTERM", "SIGHUP") if hasattr(os_signal, name)
    )

    def __init__(self, stop_event: threading.Event) -> None:
        self.stop_event = stop_event
        self.stopped_by: str | None = None  # the first stop request received
        self._closing = False
        self._extra = 0
        self._previous: dict[int, object] = {}

    def install(self) -> None:
        # Python only allows signal handlers in the main thread.
        if threading.current_thread() is threading.main_thread():
            for signum in self.SIGNALS:
                self._previous[signum] = os_signal.signal(signum, self._handle)

    def uninstall(self) -> None:
        for signum, previous in self._previous.items():
            os_signal.signal(signum, previous)
        self._previous.clear()

    @contextmanager
    def closing(self) -> Iterator[None]:
        """Hold back stop requests during the block; they stop the run afterwards."""
        before, self._closing = self._closing, True
        try:
            yield
        finally:
            self._closing = before

    def _handle(self, signum: int, frame: object) -> None:
        if self.stopped_by is None:
            self.stopped_by = "Ctrl-C" if signum == os_signal.SIGINT else os_signal.Signals(signum).name
        if not self._closing:
            self._closing = True
            raise KeyboardInterrupt
        self.stop_event.set()
        if signum != os_signal.SIGINT:
            return
        self._extra += 1
        if self._extra >= self.FORCE_QUIT_PRESSES:
            raise KeyboardInterrupt
        left = self.FORCE_QUIT_PRESSES - self._extra
        notice = (
            f"\nClosing positions, please wait. Press Ctrl-C {left} more time(s) to "
            f"force quit and LEAVE POSITIONS OPEN.\n"
        )
        try:
            os.write(2, notice.encode())  # not the logger: its lock may be held right now
        except OSError:  # the terminal is gone; an error here would abort the close
            pass


@dataclass
class _Order:
    """A closing order, and the position it was sent for."""

    position: dict
    contract: object = None
    result: OrderResult | None = None
    # Accepted, or of unknown outcome. Sending another one could fill twice and
    # flip the position. False when it was refused or never sent.
    may_fill: bool = False


class PositionCloser:
    """Closes positions and keeps at it until the broker stops reporting them.

    A position counts as closed, and is booked, only when the broker no longer
    reports it. An order that may still fill is sent again only once the broker
    confirms that no order is working any more.
    """

    def __init__(self, broker: BrokerBase, settings: Settings, guard: StopGuard) -> None:
        self._broker = broker
        self._settings = settings
        self._guard = guard
        self._orders: dict[str, _Order] = {}
        # False from the start of trading until the account is confirmed flat.
        self.flat = True

    def close(
        self,
        perf: PerformanceTracker,
        waits: Sequence[float] = STOPPING,
        only: Callable[[str], bool] = lambda symbol: True,
    ) -> tuple[float, dict[str, float] | None]:
        """Close every position (*only*: some), trying again after each of *waits*.

        Returns the realised PnL and the signed quantities still open by symbol,
        ``None`` when the positions could not be read. Cannot be interrupted.
        """
        total = 0.0
        waits = list(waits)
        with self._guard.closing():
            while True:
                pnl, left = self._attempt(perf, only)
                total += pnl
                if left == {}:
                    # An order in flight can still fill: look again before believing it.
                    time.sleep(_SETTLE_WAIT_S)
                    held = self._read()
                    left = None if held is None else _quantities(held, only)
                if left == {} or not waits:
                    return total, left
                if self._guard.stop_event.is_set():
                    waits = [min(w, STOPPING[-1]) for w in waits[: len(STOPPING)]]
                wait = waits.pop(0)
                logger.critical(f"NOT FLAT: {describe_open(left)}. Trying again in {wait:g}s")
                self._pause(wait)

    def close_all(self, perf: PerformanceTracker, waits: Sequence[float]) -> tuple[float, dict[str, float] | None]:
        """Close the whole account and remember whether it is flat."""
        pnl, left = self.close(perf, waits)
        self.flat = left == {}
        return pnl, left

    def close_leftovers(self) -> bool:
        """Close what an earlier session left. True when the account is flat.

        That PnL belongs to the earlier session, so it stays out of today's numbers.
        """
        held = self._read()
        if held:
            logger.warning(f"Positions left from an earlier session: {describe_open(_quantities(held))}")
        pnl, _ = self.close_all(PerformanceTracker(), ONCE)
        if held and self.flat:
            logger.info(f"Closed the earlier session's positions  PnL=${pnl:+,.2f} (not counted in today's)")
        return self.flat

    def close_instruments(self, instruments: set[str], perf: PerformanceTracker) -> set[str]:
        """Close these instruments; returns the ones confirmed flat."""

        def chosen(symbol: str) -> bool:
            return _instrument(symbol) in instruments

        held = self._read()
        if held is None:
            return set()
        if any(map(chosen, held)):
            logger.info(f"EXIT TIME — closing {describe_open(_quantities(held, chosen))}")
            _, left = self.close(perf, ONCE, chosen)
            if left is None:
                return set()
            return instruments - {_instrument(symbol) for symbol in left}
        return instruments

    # ------------------------------------------------------------------

    def _attempt(
        self, perf: PerformanceTracker, only: Callable[[str], bool]
    ) -> tuple[float, dict[str, float] | None]:
        """Send one closing order per position and wait for the fills to show."""
        none_working = self._cancel_working_orders()
        if none_working and self._orders:
            time.sleep(_SETTLE_WAIT_S)  # let a fill that beat the cancel show up
        held = self._read()
        if held is None:
            logger.critical(
                "POSITIONS NOT CLOSED: the broker's positions could not be read. "
                "Check the account and close them by hand."
            )
            return 0.0, None
        pnl = self._settle(held, perf)
        self._orders = {s: o for s, o in self._orders.items() if o.may_fill and not none_working}

        chosen = {symbol: p for symbol, p in held.items() if only(symbol)}
        if not chosen:
            logger.info("No open positions to close")
            return pnl, {}
        logger.warning(f"CLOSE: flattening {len(chosen)} open position(s)…")
        for symbol, position in chosen.items():
            if symbol not in self._orders:
                self._orders[symbol] = self._send(symbol, position)

        for poll in range(_SETTLE_POLLS):
            held = self._read()
            if held is None or not any(s in held and self._orders[s].may_fill for s in chosen):
                break
            if poll < _SETTLE_POLLS - 1:
                time.sleep(_SETTLE_WAIT_S)
        if held is not None:
            pnl += self._settle(held, perf)
            return pnl, _quantities(held, only)
        logger.critical("Positions could not be re-read after the closing orders")
        # Cannot check: believe only an order the broker called filled.
        unfilled = {s: p for s, p in chosen.items() if not _is_filled(self._orders[s].result)}
        pnl += self._settle({**{s: o.position for s, o in self._orders.items() if s not in chosen}, **unfilled}, perf)
        return pnl, _quantities(unfilled)

    def _send(self, symbol: str, position: dict) -> _Order:
        order = _Order(position)
        qty = float(position["qty"])
        logger.warning(f"  {_side(qty)} {abs(qty):g}x {symbol} @ {position['avg_price']:.2f}")
        try:
            order.contract = self._broker.resolve_contract(symbol)
        except Exception as exc:
            logger.error(f"  -> Resolving {symbol} failed: {exc}")
            return order
        if order.contract is None:
            logger.error(f"  -> Cannot resolve contract for {symbol} — manual close required")
            return order
        self._broker.last_order_not_placed = False
        try:
            order.result = self._broker.place_order(order.contract, "SELL" if qty > 0 else "BUY", abs(qty))
        except Exception as exc:
            logger.error(f"  -> Close order for {symbol} raised: {exc}")
        order.may_fill = order.result is not None or not self._broker.last_order_not_placed
        if not order.may_fill:
            logger.error(f"  -> {symbol} NOT closed: the order was refused")
        elif order.result is None:
            logger.error(f"  -> {symbol}: the order failed without a clear refusal, it may still fill")
        return order

    def _settle(self, held: dict[str, dict], perf: PerformanceTracker) -> float:
        """Book what the orders closed since the last look, and drop the finished ones."""
        pnl = 0.0
        for symbol, order in list(self._orders.items()):
            before = float(order.position["qty"])
            now = float(held[symbol]["qty"]) if symbol in held else 0.0
            done = now * before <= 0  # flat, or on the other side
            closed = before if done else before - now
            if closed * before > 0 and abs(closed) >= _FLAT_QTY:
                pnl += self._book(symbol, order, closed, perf)
            if now * before < 0:
                logger.critical(f"  -> {symbol} is now {now:+g}, on the other side — check the account")
            if done:
                del self._orders[symbol]
            else:
                order.position = held[symbol]
        return pnl

    def _book(self, symbol: str, order: _Order, qty: float, perf: PerformanceTracker) -> float:
        """Record the PnL of *qty* (signed) that is confirmed closed."""
        price = order.result.price if order.result else None
        if price is None and order.contract is not None:
            try:
                price = self._broker.get_last_price(order.contract)
            except Exception as exc:
                logger.warning(f"  -> No price for {symbol}: {exc}")
        closed = f"  -> Closed {_side(qty)} {abs(qty):g}x {symbol}"
        if price is None:
            logger.info(f"{closed} (fill price unavailable)")
            return 0.0
        multiplier = self._settings.contract_multipliers.get(symbol, 1.0)
        pnl = (price - float(order.position["avg_price"])) * qty * multiplier
        perf.record_close(symbol, pnl, Signal.UP if qty > 0 else Signal.DOWN)
        logger.info(f"{closed} @ {price:.2f}  PnL=${pnl:+,.2f}")
        return pnl

    def _read(self) -> dict[str, dict] | None:
        """Non-flat broker positions by symbol, or ``None`` when they cannot be read."""
        for attempt in range(1, _READ_TRIES + 1):
            try:
                rows = self._broker.get_positions_for_sync()
                return {p["symbol"]: p for p in rows if abs(float(p["qty"])) >= _FLAT_QTY}
            except Exception as exc:
                logger.warning(f"Reading positions failed ({attempt}/{_READ_TRIES}): {exc}")
                if attempt < _READ_TRIES:
                    time.sleep(_SETTLE_WAIT_S)
        return None

    def _cancel_working_orders(self) -> bool:
        """Whether the broker confirms that no order is working any more."""
        try:
            return self._broker.cancel_working_orders()
        except Exception as exc:
            logger.warning(f"Cancelling working orders failed: {exc}")
            return False

    def _pause(self, seconds: float) -> None:
        """Sleep in slices, cut short when a stop is requested meanwhile."""
        stopping = self._guard.stop_event.is_set()
        for _ in range(int(seconds)):
            if self._guard.stop_event.is_set() and not stopping:
                return
            time.sleep(1.0)


def _instrument(symbol: str) -> str:
    return position_symbol_to_canonical(symbol) or symbol


def _side(qty: float) -> str:
    return "LONG" if qty > 0 else "SHORT"


def _is_filled(result: OrderResult | None) -> bool:
    return result is not None and str(result.status).lower() == "filled"


def _quantities(held: dict[str, dict], only: Callable[[str], bool] = lambda symbol: True) -> dict[str, float]:
    return {symbol: float(p["qty"]) for symbol, p in held.items() if only(symbol)}
