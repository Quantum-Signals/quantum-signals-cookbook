"""Entry and exit times that each signal publishes in the QuantumSignals catalog.

A signal's ``trading_spec`` says how it is traded in its published backtest:
decisions are taken inside ``entry_window`` (end excluded), positions are then
held, and everything is closed from ``exit_window.start``. The stream enforces
neither: real signals keep coming after the entry window, and nothing closes a
position at the exit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from loguru import logger
from quantumsignals.client import Client as QSClient

from qs_trader_symbols import canonicalize, parse_allowed_symbols, trading_targets
from trader.config import Settings


@dataclass(frozen=True)
class Window:
    """Times of day in ``tz``; ``None`` means the signal publishes no such limit."""

    tz: ZoneInfo
    entry_start: time | None = None
    entry_end: time | None = None
    exit_start: time | None = None

    def allows(self, when: datetime) -> bool:
        """Whether a decision may be taken at *when*."""
        now = when.astimezone(self.tz).time()
        if self.entry_start is not None and now < self.entry_start:
            return False
        if self.entry_end is not None and now >= self.entry_end:
            return False
        return self.exit_start is None or now < self.exit_start

    def exit_at(self, day: datetime) -> datetime | None:
        """When positions must start closing on *day*'s date."""
        if self.exit_start is None:
            return None
        return datetime.combine(day.astimezone(self.tz).date(), self.exit_start, tzinfo=self.tz)

    def __str__(self) -> str:
        def show(t: time | None) -> str:
            return f"{t:%H:%M}" if t is not None else "any time"

        return f"open {show(self.entry_start)}–{show(self.entry_end)}, exit {show(self.exit_start)} {self.tz.key}"


def _tightest(a: Window, b: Window) -> Window:
    """The limits that satisfy both, for two signals on one instrument."""

    def pick(choose, *times: time | None) -> time | None:
        return choose((t for t in times if t is not None), default=None)

    return Window(
        a.tz,
        pick(max, a.entry_start, b.entry_start),
        pick(min, a.entry_end, b.entry_end),
        pick(min, a.exit_start, b.exit_start),
    )


def _window(spec: object) -> Window | None:
    """The window in a catalog ``trading_spec``, or ``None`` when it has none."""

    def at(window: object, edge: str) -> time | None:
        value = getattr(window, edge, None)
        return time.fromisoformat(value) if value else None

    entry, exit_ = getattr(spec, "entry_window", None), getattr(spec, "exit_window", None)
    if entry is None and exit_ is None:
        return None
    tz = ZoneInfo(getattr(spec, "timezone", None) or "America/New_York")
    return Window(tz, at(entry, "start"), at(entry, "end"), at(exit_, "start"))


class SignalWindows:
    """The published windows of the subscribed signals on the allowed instruments."""

    def __init__(self, settings: Settings) -> None:
        self._api_key = settings.qs_api_key
        self._symbols = trading_targets(parse_allowed_symbols(settings.allowed_symbols))
        self._by_symbol: dict[str, Window] | None = None
        self._exited: set[str] = set()
        self._held_back: set[str] = set()

    @property
    def loaded(self) -> bool:
        return self._by_symbol is not None

    def load(self) -> bool:
        """Read the catalog. On failure the last known windows stay in force."""
        try:
            with QSClient(api_key=self._api_key) as qs:
                catalog = qs.get_signal_catalog()
                subscribed = {s.signal_id for s in qs.get_subscriptions()}
            found: dict[str, Window] = {}
            for signal_id, meta in catalog.items():
                symbol = canonicalize(meta.model_output_symbol or "") or meta.model_output_symbol
                if meta.retired or symbol not in self._symbols or signal_id not in subscribed:
                    continue
                window = _window(meta.trading_spec)
                if window is None:
                    continue
                if symbol in found and found[symbol].tz == window.tz:
                    window = _tightest(found[symbol], window)
                found[symbol] = window
        except Exception as exc:
            kept = "keeping the previous ones" if self.loaded else "trading the whole session without them"
            logger.warning(f"[windows] Could not read the signals' trading times ({exc}); {kept}")
            return False
        self._by_symbol = found
        return True

    def start_day(self, session_start: datetime) -> None:
        """Forget yesterday's exits, read the windows again and log them."""
        self._exited.clear()
        self._held_back.clear()
        if not self.load():
            return
        for symbol in self._symbols:
            window = self._by_symbol.get(symbol)
            logger.info(f"[windows] {symbol}: {window or 'no published times, session hours apply'}")
            if window and window.entry_end and window.entry_end <= session_start.astimezone(window.tz).time():
                logger.warning(
                    f"[windows] {symbol} is never opened: its entry window ends before the session starts "
                    "(TRADER_SESSION_START_TIME)"
                )

    def allows(self, symbol: str, when: datetime) -> bool:
        """Whether *symbol* may be traded on a signal stamped *when*.

        Instruments without known times, or before any could be read, are not limited.
        """
        window = (self._by_symbol or {}).get(symbol)
        if window is None or window.allows(when):
            self._held_back.discard(symbol)
            return True
        if symbol not in self._held_back:
            self._held_back.add(symbol)
            logger.info(f"  -> {symbol}: outside its trading times ({window}) — holding, no new trades")
        return False

    def due_exits(self, now: datetime, session_end: datetime) -> set[str]:
        """Instruments whose exit time has come and that were not closed yet today.

        An exit at or after the session end is left to the end-of-day close.
        """
        return {
            symbol
            for symbol, window in (self._by_symbol or {}).items()
            if symbol not in self._exited
            and (exit_at := window.exit_at(now)) is not None
            and exit_at <= now
            and exit_at < session_end
        }

    def mark_exited(self, symbols: set[str]) -> None:
        self._exited |= symbols
