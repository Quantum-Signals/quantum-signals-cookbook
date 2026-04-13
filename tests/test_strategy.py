from dataclasses import dataclass
from typing import Optional

from trader.config import Settings
from trader.gateways.simulated import SimulatedGateway
from trader.models import PerformanceTracker, Signal
from trader.strategy.momentum import MomentumStrategy


def _make_settings(**overrides) -> Settings:
    defaults = dict(qs_api_key="test-key", dry_run=True, trade_cooldown_seconds=0)
    defaults.update(overrides)
    return Settings(**defaults)


@dataclass
class FakeSignal:
    """Mimics the QuantumSignals signal object for testing."""
    symbol: str
    signal: int
    model: str = "test-model"
    for_minute_utc: str = "2026-01-01T00:00:00Z"
    code_message: str = ""
    _null: bool = False

    def is_null_signal(self) -> bool:
        return self._null


class TestMomentumStrategy:
    def _make_strategy(self, **settings_kw):
        settings = _make_settings(**settings_kw)
        gw = SimulatedGateway(settings)
        gw.connect()
        perf = PerformanceTracker()
        strategy = MomentumStrategy(gw, perf, settings)
        return strategy, gw, perf

    def test_null_signal_ignored(self):
        strategy, gw, perf = self._make_strategy()
        strategy.handle_signal(FakeSignal(symbol="ES", signal=1, _null=True))
        assert len(gw.get_open_positions()) == 0
        assert perf.total_trades == 0

    def test_up_signal_opens_long(self):
        strategy, gw, perf = self._make_strategy()
        strategy.handle_signal(FakeSignal(symbol="ES", signal=Signal.UP))
        assert len(gw.get_open_positions()) == 1
        pos = gw.get_open_positions()[0]
        assert pos["pos"] > 0

    def test_down_signal_opens_short(self):
        strategy, gw, perf = self._make_strategy()
        strategy.handle_signal(FakeSignal(symbol="ES", signal=Signal.DOWN))
        assert len(gw.get_open_positions()) == 1
        pos = gw.get_open_positions()[0]
        assert pos["pos"] < 0

    def test_stable_signal_no_position(self):
        strategy, gw, perf = self._make_strategy()
        strategy.handle_signal(FakeSignal(symbol="ES", signal=Signal.STABLE))
        assert len(gw.get_open_positions()) == 0

    def test_long_closed_on_down(self):
        strategy, gw, perf = self._make_strategy()
        strategy.handle_signal(FakeSignal(symbol="ES", signal=Signal.UP))
        assert len(gw.get_open_positions()) == 1

        strategy.handle_signal(FakeSignal(symbol="ES", signal=Signal.DOWN))
        positions = gw.get_open_positions()
        # Should have reversed: closed long, opened short
        assert len(positions) == 1
        assert positions[0]["pos"] < 0
        assert perf.total_trades == 1

    def test_long_closed_on_stable(self):
        strategy, gw, perf = self._make_strategy()
        strategy.handle_signal(FakeSignal(symbol="ES", signal=Signal.UP))
        strategy.handle_signal(FakeSignal(symbol="ES", signal=Signal.STABLE))
        assert len(gw.get_open_positions()) == 0
        assert perf.total_trades == 1

    def test_short_closed_on_up(self):
        strategy, gw, perf = self._make_strategy()
        strategy.handle_signal(FakeSignal(symbol="ES", signal=Signal.DOWN))
        strategy.handle_signal(FakeSignal(symbol="ES", signal=Signal.UP))
        positions = gw.get_open_positions()
        # Should have reversed: closed short, opened long
        assert len(positions) == 1
        assert positions[0]["pos"] > 0
        assert perf.total_trades == 1

    def test_short_closed_on_stable(self):
        strategy, gw, perf = self._make_strategy()
        strategy.handle_signal(FakeSignal(symbol="ES", signal=Signal.DOWN))
        strategy.handle_signal(FakeSignal(symbol="ES", signal=Signal.STABLE))
        assert len(gw.get_open_positions()) == 0
        assert perf.total_trades == 1

    def test_cooldown_blocks_rapid_trades(self):
        strategy, gw, perf = self._make_strategy(trade_cooldown_seconds=9999)
        strategy.handle_signal(FakeSignal(symbol="ES", signal=Signal.UP))
        # Second signal within cooldown should be skipped
        strategy.handle_signal(FakeSignal(symbol="ES", signal=Signal.DOWN))
        positions = gw.get_open_positions()
        assert len(positions) == 1
        assert positions[0]["pos"] > 0  # still long, DOWN was skipped

    def test_multiple_symbols_independent(self):
        strategy, gw, perf = self._make_strategy()
        strategy.handle_signal(FakeSignal(symbol="ES", signal=Signal.UP))
        strategy.handle_signal(FakeSignal(symbol="NQ", signal=Signal.DOWN))
        positions = gw.get_open_positions()
        symbols = {p["symbol"] for p in positions}
        assert symbols == {"ES", "NQ"}
