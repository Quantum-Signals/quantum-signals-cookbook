from datetime import date

from trader.models import PerformanceTracker, Signal
from trader.reporting import format_day_summary


def test_day_summary_explains_zero_unrealized_and_zero_closes():
    """Boss case: leftover/scaling day — fills exist, nothing went flat."""
    perf = PerformanceTracker()
    perf.record_signal("GC", Signal.UP)
    perf.record_signal("GC", Signal.DOWN)
    perf.record_signal("RTY", Signal.UP)
    perf.record_trade("SLV", "BUY", 0.58)

    lines = format_day_summary(
        date(2026, 10, 1),
        perf,
        intraday_pnl=0.0,
        exit_pnl=0.0,
        daily_start_capital=10_000,
        snapshot={
            "unrealized_pnl": 12.5,
            "net_liquidation": 10_012.5,
            "positions": [
                {"symbol": "GLD", "qty": -8, "avg_price": 381.81},
                {"symbol": "SLV", "qty": 0.58, "avg_price": 55.12},
            ],
        },
        intraday_closes=0,
    )
    text = "\n".join(lines)

    assert "DAY SUMMARY  2026-10-01" in text
    assert "UP=2" in text and "DOWN=1" in text
    assert "BUY=1" in text
    assert "During session : 0" in text
    assert "EOD flatten    : 0" in text
    assert "orders filled but no position went flat" in text
    assert "Unrealized before flatten : $+12.50" in text
    assert "Unrealized now            : $0.00" in text
    assert "SHORT        8 GLD" in text
    assert "LONG    0.5800 SLV" in text
    assert "Return on $10,000" in text


def test_day_summary_splits_intraday_and_flatten_closes():
    perf = PerformanceTracker()
    perf.record_close("ES", 20.0, Signal.UP)
    perf.record_close("GLD", -5.0, Signal.DOWN)

    lines = format_day_summary(
        date(2026, 10, 1),
        perf,
        intraday_pnl=20.0,
        exit_pnl=-5.0,
        daily_start_capital=10_000,
        snapshot={"unrealized_pnl": 8.0, "positions": []},
        intraday_closes=1,
    )
    text = "\n".join(lines)

    assert "During session : 1" in text
    assert "EOD flatten    : 1" in text
    assert "Total          : 2" in text
    assert "ES       LONG   PnL=$+20.00" in text
    assert "GLD      SHORT  PnL=$-5.00" in text
    assert "already flat" in text
