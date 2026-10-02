from trader.models import PerformanceTracker, Signal


class TestPerformanceTracker:
    def test_initial_state(self):
        pt = PerformanceTracker()
        assert pt.total_trades == 0
        assert pt.winning_trades == 0
        assert pt.losing_trades == 0
        assert pt.gross_pnl == 0.0
        assert pt.win_rate == 0.0

    def test_record_winning_trade(self):
        pt = PerformanceTracker()
        pt.record_close("ES", 250.0, Signal.UP)

        assert pt.total_trades == 1
        assert pt.winning_trades == 1
        assert pt.losing_trades == 0
        assert pt.gross_pnl == 250.0
        assert pt.win_rate == 1.0

    def test_record_losing_trade(self):
        pt = PerformanceTracker()
        pt.record_close("NQ", -100.0, Signal.DOWN)

        assert pt.total_trades == 1
        assert pt.winning_trades == 0
        assert pt.losing_trades == 1
        assert pt.gross_pnl == -100.0
        assert pt.win_rate == 0.0

    def test_mixed_trades_win_rate(self):
        pt = PerformanceTracker()
        pt.record_close("ES", 300.0, Signal.UP)
        pt.record_close("NQ", -50.0, Signal.DOWN)
        pt.record_close("YM", 100.0, Signal.UP)

        assert pt.total_trades == 3
        assert pt.winning_trades == 2
        assert pt.losing_trades == 1
        assert pt.gross_pnl == 350.0
        assert pt.win_rate == pytest.approx(2 / 3)

    def test_trade_log_populated(self):
        pt = PerformanceTracker()
        pt.record_close("ES", 100.0, Signal.UP)

        assert len(pt._trade_log) == 1
        entry = pt._trade_log[0]
        assert entry["symbol"] == "ES"
        assert entry["pnl"] == 100.0
        assert entry["direction"] == Signal.UP
        assert "closed_at" in entry

    def test_summary_format(self):
        pt = PerformanceTracker()
        pt.record_close("ES", 500.0, Signal.UP)
        s = pt.summary()
        assert "Closed=1" in s
        assert "Win%=100.0%" in s
        assert "W=1" in s
        assert "L=0" in s

    def test_order_counts_sum_across_symbols(self):
        pt = PerformanceTracker()
        pt.record_trade("ES", "BUY", 2)
        pt.record_trade("NQ", "SHORT", 1)
        pt.record_trade("ES", "SELL", 0.5)
        assert pt.order_counts() == {"BUY": 2, "SELL": 1, "SHORT": 1, "COVER": 0}

    def test_zero_pnl_counts_as_loss(self):
        pt = PerformanceTracker()
        pt.record_close("ES", 0.0, Signal.UP)
        assert pt.losing_trades == 1
        assert pt.winning_trades == 0


import pytest
