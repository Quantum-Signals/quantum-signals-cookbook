from qs_trader_broker_core.base import OrderResult
from trader.app import _close_all_positions
from trader.config import Settings
from trader.models import PerformanceTracker


def _settings() -> Settings:
    return Settings(_env_file=None, qs_api_key="test-key")


class _FakeBroker:
    def __init__(self, positions: list[dict], orders: list[OrderResult | None]) -> None:
        self._positions = positions
        self._orders = list(orders)
        self.place_calls = 0

    def get_positions_for_sync(self) -> list[dict]:
        return list(self._positions)

    def resolve_contract(self, qs_symbol: str) -> str:
        return qs_symbol

    def place_order(self, contract, action, qty, price=None, position_intent=None):
        self.place_calls += 1
        if not self._orders:
            return None
        return self._orders.pop(0)

    def get_last_price(self, contract) -> float:
        return 7640.75


def _long_es() -> list[dict]:
    return [{"symbol": "ES", "qty": 25.0, "avg_price": 7650.60}]


def _fill(price: float = 7640.75) -> OrderResult:
    return OrderResult(
        id="1",
        symbol="ES",
        action="SELL",
        qty=25.0,
        price=price,
        status="filled",
    )


class TestCloseAllPositions:
    def test_failed_order_does_not_book_close(self, monkeypatch):
        monkeypatch.setattr("trader.app._FLATTEN_RETRY_S", 0.0)
        broker = _FakeBroker(_long_es(), [None, None, None])
        perf = PerformanceTracker()

        pnl = _close_all_positions(broker, _settings(), perf)

        assert broker.place_calls == 3
        assert pnl == 0.0
        assert perf.total_trades == 0
        assert perf.gross_pnl == 0.0

    def test_successful_flatten_records_pnl(self):
        broker = _FakeBroker(_long_es(), [_fill(7640.75)])
        perf = PerformanceTracker()

        pnl = _close_all_positions(broker, _settings(), perf)

        assert broker.place_calls == 1
        assert perf.total_trades == 1
        # (7640.75 - 7650.60) * 25 * 50
        assert pnl == (7640.75 - 7650.60) * 25.0 * 50.0
        assert perf.gross_pnl == pnl

    def test_retries_then_succeeds(self, monkeypatch):
        monkeypatch.setattr("trader.app._FLATTEN_RETRY_S", 0.0)
        broker = _FakeBroker(_long_es(), [None, _fill(7640.75)])
        perf = PerformanceTracker()

        pnl = _close_all_positions(broker, _settings(), perf)

        assert broker.place_calls == 2
        assert perf.total_trades == 1
        assert pnl != 0.0

    def test_none_trade_does_not_use_last_price_as_fill(self, monkeypatch):
        monkeypatch.setattr("trader.app._FLATTEN_RETRY_S", 0.0)
        broker = _FakeBroker(_long_es(), [None, None, None])
        perf = PerformanceTracker()

        _close_all_positions(broker, _settings(), perf)

        # Last price is 7640.75; old bug would book that as a close.
        assert perf.total_trades == 0
