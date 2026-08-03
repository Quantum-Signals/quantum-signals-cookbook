from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum


class Signal(IntEnum):
    DOWN = 0
    STABLE = 1
    UP = 2


@dataclass
class PerformanceTracker:
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    gross_pnl: float = 0.0
    _trade_log: list = field(default_factory=list)
    signal_counts: dict[int, int] = field(
        default_factory=lambda: {Signal.UP: 0, Signal.STABLE: 0, Signal.DOWN: 0}
    )

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

    def summary(self) -> str:
        return (
            f"Trades={self.total_trades} | "
            f"Win%={self.win_rate:.1%} | "
            f"Gross PnL=${self.gross_pnl:+,.2f} | "
            f"W={self.winning_trades} L={self.losing_trades}"
        )
