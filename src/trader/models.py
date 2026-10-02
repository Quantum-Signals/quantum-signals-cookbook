from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Literal


class Signal(IntEnum):
    DOWN = 0
    STABLE = 1
    UP = 2


def sig_label(sig: int | None) -> str:
    """Human-readable label for a signal value."""
    if sig is None:
        return "NONE --"
    return {
        Signal.UP: "UP ^",
        Signal.STABLE: "STABLE ->",
        Signal.DOWN: "DOWN v",
    }.get(sig, f"?({sig})")


TradeAction = Literal["BUY", "SELL", "SHORT", "COVER"]


@dataclass
class SymbolStats:
    """Per-symbol tracking for signals and trades."""

    signal_counts: dict[int, int] = field(
        default_factory=lambda: {Signal.UP: 0, Signal.STABLE: 0, Signal.DOWN: 0}
    )
    trade_counts: dict[str, int] = field(
        default_factory=lambda: {"BUY": 0, "SELL": 0, "SHORT": 0, "COVER": 0}
    )
    realized_pnl: float = 0.0

    def record_signal(self, sig: int) -> None:
        self.signal_counts[sig] += 1

    def record_trade(self, action: TradeAction, qty: float) -> None:
        """Record a trade action. qty is always positive (count of shares)."""
        self.trade_counts[action] += int(qty) if qty == int(qty) else 1

    def total_signals(self) -> int:
        return sum(self.signal_counts.values())

    def reset(self) -> None:
        for k in self.signal_counts:
            self.signal_counts[k] = 0
        for k in self.trade_counts:
            self.trade_counts[k] = 0
        self.realized_pnl = 0.0


@dataclass
class PerformanceTracker:
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    gross_pnl: float = 0.0
    _trade_log: list = field(default_factory=list)
    # Global signal counts (backward compatible)
    signal_counts: dict[int, int] = field(
        default_factory=lambda: {Signal.UP: 0, Signal.STABLE: 0, Signal.DOWN: 0}
    )
    # Per-symbol stats for detailed reporting
    symbol_stats: dict[str, SymbolStats] = field(default_factory=dict)
    # Count of decision points executed (signals processed, whether or not a trade resulted)
    decisions_executed: int = 0

    def _get_symbol_stats(self, symbol: str) -> SymbolStats:
        """Get or create per-symbol stats tracker."""
        if symbol not in self.symbol_stats:
            self.symbol_stats[symbol] = SymbolStats()
        return self.symbol_stats[symbol]

    def record_signal(self, symbol: str, sig: int) -> None:
        """Record a signal for a symbol. Called at each decision point."""
        self.signal_counts[sig] += 1
        self._get_symbol_stats(symbol).record_signal(sig)
        self.decisions_executed += 1

    def record_trade(self, symbol: str, action: TradeAction, qty: float) -> None:
        """Record a trade action (BUY, SELL, SHORT, COVER) for a symbol."""
        self._get_symbol_stats(symbol).record_trade(action, qty)

    def record_close(self, symbol: str, pnl: float, direction: Signal) -> None:
        self.total_trades += 1
        self.gross_pnl += pnl
        if pnl > 0:
            self.winning_trades += 1
        else:
            self.losing_trades += 1
        self._trade_log.append({
            "symbol": symbol,
            "pnl": pnl,
            "direction": direction,
            "closed_at": datetime.now(timezone.utc).isoformat(),
        })
        # Also track per-symbol realized PnL
        self._get_symbol_stats(symbol).realized_pnl += pnl

    @property
    def win_rate(self) -> float:
        return self.winning_trades / self.total_trades if self.total_trades else 0.0

    def reset(self) -> None:
        """Zero all daily counters. Used between trading days in multi-day mode.

        The same instance is reset in-place so strategies holding a reference
        keep seeing the live tracker.
        """
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.gross_pnl = 0.0
        self._trade_log.clear()
        for k in self.signal_counts:
            self.signal_counts[k] = 0
        for stats in self.symbol_stats.values():
            stats.reset()
        self.decisions_executed = 0

    def order_counts(self) -> dict[str, int]:
        """Sum BUY/SELL/SHORT/COVER counts across symbols."""
        totals = {"BUY": 0, "SELL": 0, "SHORT": 0, "COVER": 0}
        for stats in self.symbol_stats.values():
            for action, count in stats.trade_counts.items():
                totals[action] = totals.get(action, 0) + count
        return totals

    def summary(self) -> str:
        return (
            f"Closed={self.total_trades} | "
            f"Win%={self.win_rate:.1%} | "
            f"Gross PnL=${self.gross_pnl:+,.2f} | "
            f"W={self.winning_trades} L={self.losing_trades}"
        )
