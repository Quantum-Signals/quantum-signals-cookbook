"""Closing positions on shutdown: honest results, safe retries, Ctrl-C guard."""

import os
import signal
import threading
from datetime import date

import pytest

import trader.app as app
from qs_trader_broker_core import OrderResult, SimulatedBroker, SimulatedSettings
from trader.config import Settings
from trader.models import PerformanceTracker


class ScriptedBroker(SimulatedBroker):
    """Simulated broker whose orders and position reads can be made to fail."""

    def __init__(self, positions: dict[str, float]) -> None:
        super().__init__(SimulatedSettings(price_jitter=0.0))
        self._sim_positions = {s: {"pos": q, "avg_cost": 100.0} for s, q in positions.items()}
        self.orders: list[tuple[str, str, float]] = []
        self.script: list[str] = []  # per order: ok / reject / lost / working
        self.reads: list[bool] = []  # per read: True = ok, False = raise

    def place_order(self, contract, action, qty, price=None, position_intent=None):
        self.orders.append((contract.symbol, action, qty))
        step = self.script.pop(0) if self.script else "ok"
        if step == "reject":
            return None
        if step == "working":
            return OrderResult(
                id="1", symbol=contract.symbol, action=action, qty=qty, price=None, status="new", raw=None
            )
        result = super().place_order(contract, action, qty, price, position_intent)
        return None if step == "lost" else result

    def get_positions_for_sync(self):
        if self.reads and not self.reads.pop(0):
            raise TimeoutError("slow")
        return super().get_positions_for_sync()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(app.time, "sleep", lambda s: None)


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, qs_api_key="k", **kw)


def _close(broker):
    perf = PerformanceTracker()
    pnl, left = app._close_all_positions(broker, _settings(), perf)
    return pnl, left, perf


def test_closes_long_and_short():
    b = ScriptedBroker({"MGC": -21, "SIL": 15})
    _, left, perf = _close(b)
    assert left == {} and perf.total_trades == 2
    assert sorted(b.orders) == [("MGC", "BUY", 21.0), ("SIL", "SELL", 15.0)]


def test_rejected_close_is_retried_then_reported_open_without_pnl():
    b = ScriptedBroker({"SIL": 15})
    b.script = ["reject"] * 3
    pnl, left, perf = _close(b)
    assert (pnl, left, perf.total_trades, len(b.orders)) == (0.0, {"SIL": 15.0}, 0, 3)


def test_rejected_close_succeeds_on_retry():
    b = ScriptedBroker({"SIL": 15})
    b.script = ["reject", "ok"]
    _, left, perf = _close(b)
    assert left == {} and perf.total_trades == 1


def test_order_filled_with_lost_response_is_not_sent_again():
    b = ScriptedBroker({"SIL": 15})
    b.script = ["lost"]
    _, left, perf = _close(b)
    assert left == {} and len(b.orders) == 1 and perf.total_trades == 1


def test_working_order_is_not_sent_again():
    b = ScriptedBroker({"SIL": 15})
    b.script = ["working"]
    _, left, perf = _close(b)
    assert left == {"SIL": 15.0} and len(b.orders) == 1 and perf.total_trades == 0


def test_read_is_retried():
    b = ScriptedBroker({"SIL": 15})
    b.reads = [False, False, True]
    assert _close(b)[1] == {}


def test_unreadable_positions_send_nothing_and_report_unknown():
    b = ScriptedBroker({"SIL": 15})
    b.reads = [False] * 10
    pnl, left, perf = _close(b)
    assert left is None and b.orders == []


def test_failed_recheck_does_not_resend_a_rejected_order():
    b = ScriptedBroker({"SIL": 15})
    b.script = ["reject"]
    b.reads = [True] + [False] * 10
    _, left, _ = _close(b)
    assert left == {"SIL": 15.0} and len(b.orders) == 1


def _summary(broker, **kw) -> str:
    lines: list[str] = []
    sink = app.logger.add(lambda m: lines.append(m.record["message"]), level="INFO")
    try:
        app._flatten_and_summarize(date(2026, 11, 20), broker, PerformanceTracker(), _settings(**kw))
    finally:
        app.logger.remove(sink)
    return "\n".join(lines)


def test_summary_is_flat_only_when_confirmed():
    assert "flat — we close everything" in _summary(ScriptedBroker({"SIL": 15}))

    b = ScriptedBroker({"SIL": 15})
    b.script = ["reject"] * 3
    text = _summary(b)
    assert "STILL OPEN at the broker: SIL +15" in text and "flat — we close" not in text

    b = ScriptedBroker({"SIL": 15})
    b.reads = [False] * 10
    text = _summary(b)
    assert "could not confirm the account is flat" in text and "already flat" not in text


def test_summary_without_close_on_exit_lists_open_positions():
    assert "still open  (SIL +15)" in _summary(ScriptedBroker({"SIL": 15}), close_on_exit=False)


def _ctrl_c():
    os.kill(os.getpid(), signal.SIGINT)
    for _ in range(1000):  # let the interpreter run the pending handler
        pass


@pytest.fixture
def guard():
    g = app._InterruptGuard(threading.Event())
    before = signal.getsignal(signal.SIGINT)
    g.install()
    yield g
    g.uninstall()
    assert signal.getsignal(signal.SIGINT) is before


def test_second_ctrl_c_does_not_interrupt(guard):
    with pytest.raises(KeyboardInterrupt):
        _ctrl_c()
    for _ in range(guard.FORCE_QUIT_PRESSES - 1):
        _ctrl_c()
    with pytest.raises(KeyboardInterrupt):
        _ctrl_c()


def test_ctrl_c_during_end_of_day_close_stops_afterwards(guard):
    with guard.closing():
        _ctrl_c()
    assert guard._stop_event.is_set()
    with pytest.raises(KeyboardInterrupt):
        _ctrl_c()
