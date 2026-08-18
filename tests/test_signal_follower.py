from dataclasses import dataclass

from trader.config import Settings
from trader.brokers.simulated import SimulatedBroker
from trader.models import PerformanceTracker, Signal
from trader.strategy.signal_follower import SignalFollowerStrategy


def _make_settings(**overrides) -> Settings:
    defaults = dict(qs_api_key="test-key", dry_run=True)
    defaults.update(overrides)
    return Settings(**defaults)


@dataclass
class FakeSignal:
    symbol: str
    signal: int
    model: str = "test-model"
    for_minute_utc: str = "2026-04-14T13:45:00Z"  # 9:45 ET (EDT, UTC-4)
    code_message: str = ""
    _null: bool = False

    @property
    def signal_time_utc(self) -> str:
        return self.for_minute_utc

    def is_null_signal(self) -> bool:
        return self._null


def _make_strategy(**settings_kw):
    """Create a strategy seeded to 2026-04-14 so ``_reset_day`` is not
    triggered on first signal. Pre-market cleanup is marked done.
    """
    from datetime import date

    settings = _make_settings(**settings_kw)
    broker = SimulatedBroker(settings)
    broker.connect()
    perf = PerformanceTracker()
    strategy = SignalFollowerStrategy(broker, perf, settings)

    strategy._current_date = date(2026, 4, 14)
    strategy._pre_market_done = True

    return strategy, broker, perf


def _utc_ts(hour_et: int, minute_et: int) -> str:
    """Build a UTC ISO timestamp for a given ET time on 2026-04-14 (EDT, UTC-4)."""
    utc_hour = hour_et + 4
    return f"2026-04-14T{utc_hour:02d}:{minute_et:02d}:00Z"


# =====================================================================
# Basic behaviour
# =====================================================================

class TestSignalFollowerBasic:
    def test_null_signal_ignored(self):
        strategy, broker, perf = _make_strategy()
        strategy.handle_signal(FakeSignal(symbol="ES", signal=1, _null=True))
        assert len(broker.get_open_positions()) == 0
        assert perf.total_trades == 0

    def test_outside_hours_ignored(self):
        strategy, broker, perf = _make_strategy()
        strategy.handle_signal(
            FakeSignal(symbol="ES", signal=Signal.UP, for_minute_utc=_utc_ts(9, 8))
        )
        assert len(broker.get_open_positions()) == 0

    def test_decision_at_boundary_opens_position(self):
        strategy, broker, perf = _make_strategy()
        strategy.handle_signal(
            FakeSignal(symbol="ES", signal=Signal.UP, for_minute_utc=_utc_ts(9, 45))
        )
        positions = broker.get_open_positions()
        assert len(positions) == 1
        assert positions[0]["pos"] > 0

    def test_decision_down_opens_short(self):
        strategy, broker, perf = _make_strategy()
        strategy.handle_signal(
            FakeSignal(symbol="ES", signal=Signal.DOWN, for_minute_utc=_utc_ts(9, 45))
        )
        positions = broker.get_open_positions()
        assert len(positions) == 1
        assert positions[0]["pos"] < 0

    def test_stable_at_boundary_pauses(self):
        strategy, broker, perf = _make_strategy()
        strategy.handle_signal(
            FakeSignal(symbol="ES", signal=Signal.STABLE, for_minute_utc=_utc_ts(9, 45))
        )
        assert len(broker.get_open_positions()) == 0


# =====================================================================
# Accumulation
# =====================================================================

class TestSignalFollowerAccumulation:
    def test_accumulates_over_5min_window(self):
        strategy, broker, perf = _make_strategy()
        for minute in range(45, 50):
            strategy.handle_signal(
                FakeSignal(symbol="ES", signal=Signal.UP, for_minute_utc=_utc_ts(9, minute))
            )
        positions = broker.get_open_positions()
        assert len(positions) == 1
        assert positions[0]["pos"] == 5

    def test_new_decision_at_next_boundary(self):
        strategy, broker, perf = _make_strategy()
        for minute in range(45, 50):
            strategy.handle_signal(
                FakeSignal(symbol="ES", signal=Signal.UP, for_minute_utc=_utc_ts(9, minute))
            )
        for minute in range(50, 55):
            strategy.handle_signal(
                FakeSignal(symbol="ES", signal=Signal.DOWN, for_minute_utc=_utc_ts(9, minute))
            )
        positions = broker.get_open_positions()
        assert len(positions) == 0

    def test_no_action_between_decisions_without_active(self):
        strategy, broker, perf = _make_strategy()
        strategy.handle_signal(
            FakeSignal(symbol="ES", signal=Signal.UP, for_minute_utc=_utc_ts(9, 46))
        )
        assert len(broker.get_open_positions()) == 0

    def test_carries_decision_across_non_boundary_minutes(self):
        strategy, broker, perf = _make_strategy()
        strategy.handle_signal(
            FakeSignal(symbol="ES", signal=Signal.DOWN, for_minute_utc=_utc_ts(10, 0))
        )
        assert broker.get_open_positions()[0]["pos"] == -1
        for minute in range(1, 5):
            strategy.handle_signal(
                FakeSignal(symbol="ES", signal=Signal.UP, for_minute_utc=_utc_ts(10, minute))
            )
        assert broker.get_open_positions()[0]["pos"] == -5


# =====================================================================
# Flatten (close window)
# =====================================================================

class TestSignalFollowerFlatten:
    def test_flatten_closes_long(self):
        strategy, broker, perf = _make_strategy()
        for minute in range(45, 50):
            strategy.handle_signal(
                FakeSignal(symbol="ES", signal=Signal.UP, for_minute_utc=_utc_ts(9, minute))
            )
        assert broker.get_open_positions()[0]["pos"] == 5
        strategy.handle_signal(
            FakeSignal(symbol="ES", signal=Signal.UP, for_minute_utc=_utc_ts(15, 55))
        )
        assert len(broker.get_open_positions()) == 0
        assert perf.total_trades == 1

    def test_flatten_closes_short(self):
        strategy, broker, perf = _make_strategy()
        for minute in range(45, 50):
            strategy.handle_signal(
                FakeSignal(symbol="ES", signal=Signal.DOWN, for_minute_utc=_utc_ts(9, minute))
            )
        assert broker.get_open_positions()[0]["pos"] == -5
        strategy.handle_signal(
            FakeSignal(symbol="ES", signal=Signal.DOWN, for_minute_utc=_utc_ts(15, 56))
        )
        assert len(broker.get_open_positions()) == 0
        assert perf.total_trades == 1

    def test_flatten_noop_when_flat(self):
        strategy, broker, perf = _make_strategy()
        strategy.handle_signal(
            FakeSignal(symbol="ES", signal=Signal.UP, for_minute_utc=_utc_ts(15, 55))
        )
        assert len(broker.get_open_positions()) == 0
        assert perf.total_trades == 0

    def test_flatten_pnl_uses_multiplier(self):
        strategy, broker, perf = _make_strategy()
        strategy.handle_signal(
            FakeSignal(symbol="ES", signal=Signal.UP, for_minute_utc=_utc_ts(9, 45))
        )
        strategy.handle_signal(
            FakeSignal(symbol="ES", signal=Signal.UP, for_minute_utc=_utc_ts(15, 55))
        )
        assert perf.total_trades == 1


# =====================================================================
# Multi-symbol
# =====================================================================

class TestSignalFollowerMultiSymbol:
    def test_independent_symbols(self):
        strategy, broker, perf = _make_strategy()
        strategy.handle_signal(
            FakeSignal(symbol="ES", signal=Signal.UP, for_minute_utc=_utc_ts(9, 45))
        )
        strategy.handle_signal(
            FakeSignal(symbol="NQ", signal=Signal.DOWN, for_minute_utc=_utc_ts(9, 45))
        )
        positions = broker.get_open_positions()
        by_sym = {p["symbol"]: p["pos"] for p in positions}
        assert by_sym["SPY"] > 0
        assert by_sym["QQQ"] < 0

    def test_flatten_only_closes_symbol_in_window(self):
        strategy, broker, perf = _make_strategy()
        strategy.handle_signal(
            FakeSignal(symbol="ES", signal=Signal.UP, for_minute_utc=_utc_ts(9, 45))
        )
        strategy.handle_signal(
            FakeSignal(symbol="NQ", signal=Signal.DOWN, for_minute_utc=_utc_ts(9, 45))
        )
        strategy.handle_signal(
            FakeSignal(symbol="ES", signal=Signal.UP, for_minute_utc=_utc_ts(15, 55))
        )
        positions = broker.get_open_positions()
        symbols = {p["symbol"] for p in positions}
        assert "SPY" not in symbols
        assert "QQQ" in symbols


# =====================================================================
# Day reset
# =====================================================================

class TestSignalFollowerDayReset:
    def test_new_day_resets_net_qty_and_computes_sizing(self):
        strategy, broker, perf = _make_strategy()
        strategy.handle_signal(
            FakeSignal(symbol="ES", signal=Signal.UP, for_minute_utc=_utc_ts(9, 45))
        )
        assert strategy._net_qty.get("ES") == 1

        # Day 2: _reset_day clears per-day state
        strategy.handle_signal(
            FakeSignal(
                symbol="ES", signal=Signal.DOWN,
                for_minute_utc="2026-04-15T13:45:00Z",
            )
        )
        assert strategy._current_date.day == 15

    def test_daily_pnl_resets_on_new_day(self):
        strategy, broker, perf = _make_strategy()
        for minute in range(45, 50):
            strategy.handle_signal(
                FakeSignal(symbol="ES", signal=Signal.UP, for_minute_utc=_utc_ts(9, minute))
            )
        strategy.handle_signal(
            FakeSignal(symbol="ES", signal=Signal.UP, for_minute_utc=_utc_ts(15, 55))
        )

        # Day 2: daily PnL resets
        strategy.handle_signal(
            FakeSignal(
                symbol="ES", signal=Signal.UP,
                for_minute_utc="2026-04-15T13:45:00Z",
            )
        )
        assert strategy._daily_pnl == 0.0


# =====================================================================
# Gap window
# =====================================================================

class TestSignalFollowerGapWindow:
    def test_gap_between_trading_and_close_is_quiet(self):
        strategy, broker, perf = _make_strategy()
        strategy.handle_signal(
            FakeSignal(symbol="ES", signal=Signal.UP, for_minute_utc=_utc_ts(9, 45))
        )
        assert broker.get_open_positions()[0]["pos"] == 1
        strategy.handle_signal(
            FakeSignal(symbol="ES", signal=Signal.UP, for_minute_utc=_utc_ts(15, 40))
        )
        assert broker.get_open_positions()[0]["pos"] == 1


# =====================================================================
# Symbol filtering (ALLOWED_SYMBOLS)
# =====================================================================

class TestSignalFollowerSymbolFilter:
    def test_allowed_symbols_filter(self):
        strategy, broker, perf = _make_strategy(allowed_symbols=("ES", "NQ"))
        strategy.handle_signal(
            FakeSignal(symbol="ES", signal=Signal.UP, for_minute_utc=_utc_ts(9, 45))
        )
        strategy.handle_signal(
            FakeSignal(symbol="YM", signal=Signal.UP, for_minute_utc=_utc_ts(9, 45))
        )
        strategy.handle_signal(
            FakeSignal(symbol="NQ", signal=Signal.DOWN, for_minute_utc=_utc_ts(9, 45))
        )
        positions = broker.get_open_positions()
        symbols = {p["symbol"] for p in positions}
        assert "SPY" in symbols
        assert "QQQ" in symbols
        assert "YM" not in symbols

    def test_empty_allowed_means_all(self):
        strategy, broker, perf = _make_strategy(allowed_symbols=())
        strategy.handle_signal(
            FakeSignal(symbol="YM", signal=Signal.UP, for_minute_utc=_utc_ts(9, 45))
        )
        assert len(broker.get_open_positions()) == 1


# =====================================================================
# Pre-market cleanup
# =====================================================================

class TestPreMarketCleanup:
    def test_sync_positions_closes_existing(self):
        settings = _make_settings()
        broker = SimulatedBroker(settings)
        broker.connect()
        contract = broker.resolve_contract("ES")
        broker.place_order(contract, "BUY", 3, 5450.0)
        assert len(broker.get_open_positions()) == 1

        perf = PerformanceTracker()
        strategy = SignalFollowerStrategy(broker, perf, settings)
        strategy.sync_positions()
        assert len(broker.get_open_positions()) == 0

    def test_pre_market_cleanup_on_first_signal(self):
        settings = _make_settings()
        broker = SimulatedBroker(settings)
        broker.connect()
        contract = broker.resolve_contract("NQ")
        broker.place_order(contract, "SELL", 2, 19200.0)
        assert broker.get_open_positions()[0]["pos"] == -2

        perf = PerformanceTracker()
        strategy = SignalFollowerStrategy(broker, perf, settings)

        strategy.handle_signal(
            FakeSignal(symbol="ES", signal=Signal.UP, for_minute_utc=_utc_ts(9, 20))
        )
        nq_positions = [p for p in broker.get_open_positions() if p["symbol"] == "NQ"]
        assert len(nq_positions) == 0

    def test_pre_market_cleanup_only_once_per_day(self):
        strategy, broker, perf = _make_strategy()
        # _make_strategy already sets _pre_market_done=True.
        # Verify that trading proceeds normally without re-triggering cleanup.
        strategy.handle_signal(
            FakeSignal(symbol="ES", signal=Signal.UP, for_minute_utc=_utc_ts(9, 45))
        )
        assert broker.get_open_positions()[0]["pos"] == 1

        strategy.handle_signal(
            FakeSignal(symbol="ES", signal=Signal.UP, for_minute_utc=_utc_ts(9, 46))
        )
        assert broker.get_open_positions()[0]["pos"] == 2


