"""Closing positions on shutdown: honest results, safe retries, Ctrl-C guard."""

import itertools
import os
import queue
import signal
import threading
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import trader.app as app
import trader.closing as closing
from qs_trader_broker_core import OrderResult, SimulatedBroker, SimulatedSettings
from trader.config import Settings
from trader.models import PerformanceTracker


class ScriptedBroker(SimulatedBroker):
    """Simulated broker whose orders and position reads can be made to fail."""

    def __init__(self, positions: dict[str, float]) -> None:
        super().__init__(SimulatedSettings(price_jitter=0.0))
        self._sim_positions = {s: {"pos": q, "avg_cost": 100.0} for s, q in positions.items()}
        self.orders: list[tuple[str, str, float]] = []
        self.script: list[str] = []  # per order: ok / reject / lost / late / working / raise
        self.cancels = True  # whether the broker can cancel working orders
        self.late: list[tuple] = []  # orders that fill during the next wait
        self.reads: list[bool] = []  # per read: True = ok, False = raise

    def place_order(self, contract, action, qty, price=None, position_intent=None):
        self.orders.append((contract.symbol, action, qty))
        step = self.script.pop(0) if self.script else "ok"
        if step == "reject":
            self.last_order_not_placed = True
            return None
        if step == "raise":
            raise ConnectionError("down")
        if step == "late":
            self.late.append((contract, action, qty))
            return None
        if step == "working":
            return OrderResult(
                id="1", symbol=contract.symbol, action=action, qty=qty, price=None, status="new", raw=None
            )
        result = super().place_order(contract, action, qty, price, position_intent)
        return None if step == "lost" else result

    def cancel_working_orders(self) -> bool:
        if self.cancels:
            self.late.clear()
        return self.cancels

    def settle_late(self) -> None:
        while self.late:
            super().place_order(*self.late.pop())

    def get_positions_for_sync(self):
        if self.reads and not self.reads.pop(0):
            raise TimeoutError("slow")
        return super().get_positions_for_sync()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(closing.time, "sleep", lambda s: None)


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, qs_api_key="k", **kw)


def _close(broker):
    perf = PerformanceTracker()
    pnl, left = _closer(broker).close(perf, closing.ONCE)
    return pnl, left, perf


def _closer(broker, **kw):
    return closing.PositionCloser(broker, _settings(**kw), closing.StopGuard(threading.Event()))


def test_closes_long_and_short():
    b = ScriptedBroker({"QQQ": -21, "GLD": 15})
    _, left, perf = _close(b)
    assert left == {} and perf.total_trades == 2
    assert sorted(b.orders) == [("GLD", "SELL", 15.0), ("QQQ", "BUY", 21.0)]


def test_rejected_close_is_retried_then_reported_open_without_pnl():
    b = ScriptedBroker({"GLD": 15})
    b.script = ["reject"] * 3
    pnl, left, perf = _close(b)
    assert (pnl, left, perf.total_trades, len(b.orders)) == (0.0, {"GLD": 15.0}, 0, 3)


def test_rejected_close_succeeds_on_retry():
    b = ScriptedBroker({"GLD": 15})
    b.script = ["reject", "ok"]
    _, left, perf = _close(b)
    assert left == {} and perf.total_trades == 1


def test_fill_that_beats_the_cancel_is_seen_before_resending(monkeypatch):
    b = ScriptedBroker({"GLD": 15})
    b.script = ["late"]
    cancels: list = []

    def cancel():
        cancels.append(1)
        return True  # the order had just filled, but positions lag behind

    b.cancel_working_orders = cancel
    monkeypatch.setattr(closing.time, "sleep", lambda s: b.settle_late() if len(cancels) == 2 else None)
    _, left, perf = _close(b)
    assert left == {} and len(b.orders) == 1 and perf.total_trades == 1


def test_filled_order_is_booked_when_positions_cannot_be_read_again():
    b = ScriptedBroker({"GLD": 15})
    b.reads = [True] + [False] * 99
    _, left, perf = _close(b)
    assert left is None and perf.total_trades == 1


def test_ctrl_c_during_a_close_does_not_interrupt_it(guard):
    b = ScriptedBroker({"GLD": 15, "QQQ": -2})
    real = b.place_order

    def place_order(*a, **kw):
        _ctrl_c()
        return real(*a, **kw)

    b.place_order = place_order
    closer = closing.PositionCloser(b, _settings(), guard)
    assert closer.close(PerformanceTracker())[1] == {}
    assert guard.stop_event.is_set()


def test_working_order_is_cancelled_and_replaced():
    b = ScriptedBroker({"GLD": 15})
    b.script = ["working"]
    _, left, _ = _close(b)
    assert left == {} and len(b.orders) == 2


def test_order_filled_with_lost_response_is_not_sent_again():
    b = ScriptedBroker({"GLD": 15})
    b.script = ["lost"]
    _, left, perf = _close(b)
    assert left == {} and len(b.orders) == 1 and perf.total_trades == 1


def test_order_filling_late_after_a_lost_response_is_not_sent_again(monkeypatch):
    b = ScriptedBroker({"GLD": 15})
    b.script = ["late"]
    monkeypatch.setattr(closing.time, "sleep", lambda s: b.settle_late())
    _, left, perf = _close(b)
    assert left == {} and len(b.orders) == 1 and perf.total_trades == 1


@pytest.mark.parametrize("step", ["late", "raise"])
def test_order_with_unknown_outcome_is_not_resent_unless_cancelled(step):
    b = ScriptedBroker({"GLD": 15, "QQQ": -2})
    b.cancels, b.script = False, [step]
    _, left, _ = _close(b)  # it never fills; the other position still closes
    assert left == {"GLD": 15.0} and [o[0] for o in b.orders] == ["GLD", "QQQ"]


def test_refusal_of_an_earlier_order_does_not_allow_a_resend():
    b = ScriptedBroker({"GLD": 15})
    b.last_order_not_placed = True  # left over from an intraday rejection
    b.cancels, b.script = False, ["late"]
    _, left, _ = _close(b)
    assert left == {"GLD": 15.0} and len(b.orders) == 1


def test_rejected_close_is_retried_only_for_what_is_still_open(monkeypatch):
    b = ScriptedBroker({"GLD": 15})
    b.script = ["reject"]
    # 10 of the 15 get closed elsewhere (e.g. by hand) while the bot waits.
    elsewhere = iter([{"pos": 5, "avg_cost": 100.0}])
    monkeypatch.setattr(closing.time, "sleep", lambda s: [b._sim_positions.update(GLD=p) for p in elsewhere])
    _, left, _ = _close(b)
    assert left == {} and [o[2] for o in b.orders] == [15, 5]


def test_rejected_close_is_not_retried_once_flat(monkeypatch):
    b = ScriptedBroker({"GLD": 15})
    b.script = ["reject"]
    monkeypatch.setattr(closing.time, "sleep", lambda s: b._sim_positions.update(GLD={"pos": 0, "avg_cost": 0.0}))
    _, left, _ = _close(b)
    assert left == {} and len(b.orders) == 1


def test_order_filling_during_a_later_attempt_is_not_reported_open(monkeypatch):
    b = ScriptedBroker({"GLD": 15, "QQQ": -2})
    b.cancels, b.script = False, ["late", "reject"]
    waits = iter(range(100))
    # Longer than the first attempt waits, so it fills while QQQ is being retried.
    monkeypatch.setattr(closing.time, "sleep", lambda s: b.settle_late() if next(waits) == 6 else None)
    _, left, perf = _close(b)
    assert left == {} and perf.total_trades == 2
    assert [o[0] for o in b.orders] == ["GLD", "QQQ", "QQQ"]


def test_positions_that_become_unreadable_are_reported_unknown():
    b = ScriptedBroker({"GLD": 15, "QQQ": -2})
    b.script = ["ok", "reject"]
    b.reads = [True, True] + [False] * 99
    _, left, perf = _close(b)
    assert left is None and perf.total_trades == 1 and len(b.orders) == 2


@pytest.mark.parametrize(
    "status, refused", [(403, True), (422, True), (429, True), (408, False), (409, False), (500, False), (504, False)]
)
def test_alpaca_calls_only_a_4xx_answer_a_refusal(status, refused):
    from alpaca.common.exceptions import APIError
    from qs_trader_broker_alpaca.broker import _is_refusal

    http_error = SimpleNamespace(response=SimpleNamespace(status_code=status))
    assert _is_refusal(APIError("{}", http_error)) is refused


def test_alpaca_timeout_is_not_a_refusal():
    from qs_trader_broker_alpaca.broker import _is_refusal

    assert not _is_refusal(TimeoutError("slow"))


def test_working_order_is_not_sent_again():
    b = ScriptedBroker({"GLD": 15})
    b.cancels, b.script = False, ["working"]
    _, left, perf = _close(b)
    assert left == {"GLD": 15.0} and len(b.orders) == 1 and perf.total_trades == 0


def test_read_is_retried():
    b = ScriptedBroker({"GLD": 15})
    b.reads = [False, False, True]
    assert _close(b)[1] == {}


def test_unreadable_positions_send_nothing_and_report_unknown():
    b = ScriptedBroker({"GLD": 15})
    b.reads = [False] * 99
    pnl, left, perf = _close(b)
    assert left is None and b.orders == []


def test_failed_recheck_does_not_resend_a_rejected_order():
    b = ScriptedBroker({"GLD": 15})
    b.script = ["reject"]
    b.reads = [True] + [False] * 99
    _, left, _ = _close(b)
    assert left is None and len(b.orders) == 1


def _summary(broker, **kw) -> str:
    lines: list[str] = []
    sink = app.logger.add(lambda m: lines.append(m.record["message"]), level="INFO")
    try:
        settings = _settings(**kw)
        app._flatten_and_summarize(
            date(2026, 11, 20), broker, PerformanceTracker(), settings, _closer(broker)
        )
    finally:
        app.logger.remove(sink)
    return "\n".join(lines)


def test_summary_is_flat_only_when_confirmed():
    assert "flat — we close everything" in _summary(ScriptedBroker({"GLD": 15}))

    b = ScriptedBroker({"GLD": 15})
    b.script = ["reject"] * 99
    text = _summary(b)
    assert "STILL OPEN at the broker: GLD +15" in text and "flat — we close" not in text

    b = ScriptedBroker({"GLD": 15})
    b.reads = [False] * 99
    text = _summary(b)
    assert "could not confirm the account is flat" in text and "already flat" not in text


def test_summary_without_close_on_exit_lists_open_positions():
    assert "still open  (GLD +15)" in _summary(ScriptedBroker({"GLD": 15}), close_on_exit=False)


def _ctrl_c(signum=signal.SIGINT):
    os.kill(os.getpid(), signum)
    for _ in range(1000):  # let the interpreter run the pending handler
        pass


@pytest.fixture
def guard():
    g = closing.StopGuard(threading.Event())
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
    assert guard.stop_event.is_set()
    with pytest.raises(KeyboardInterrupt):
        _ctrl_c()


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_forced_stop_closes_like_ctrl_c_and_cannot_be_repeated(guard, signum):
    with pytest.raises(KeyboardInterrupt):
        _ctrl_c(signum)
    assert guard.stopped_by == signal.Signals(signum).name
    for _ in range(20):  # e.g. uv and the terminal both send it
        _ctrl_c(signum)


def test_notice_to_a_closed_terminal_does_not_abort_the_close(guard, monkeypatch):
    def gone(*_):
        raise OSError("terminal closed")

    monkeypatch.setattr(closing.os, "write", gone)
    with guard.closing():
        _ctrl_c()


# ---------------------------------------------------------------------------
# Closing until flat
# ---------------------------------------------------------------------------


def test_unknown_outcome_is_resent_only_after_the_broker_cancelled_it():
    for cancels, expected, orders in ((True, {}, 2), (False, {"GLD": 15.0}, 1)):
        b = ScriptedBroker({"GLD": 15})
        b.cancels, b.script = cancels, ["late"]
        _, left = _closer(b).close(PerformanceTracker())
        assert (left, len(b.orders)) == (expected, orders)


def test_close_that_fills_between_passes_is_booked_once(monkeypatch):
    b = ScriptedBroker({"GLD": 15})
    b.cancels, b.script = False, ["late"]
    waits = iter(range(1000))
    monkeypatch.setattr(closing.time, "sleep", lambda s: b.settle_late() if next(waits) == 6 else None)
    perf = PerformanceTracker()
    _, left = _closer(b).close(perf)
    assert left == {} and len(b.orders) == 1 and perf.total_trades == 1


def test_entry_filling_after_the_close_is_closed_too(monkeypatch):
    b = ScriptedBroker({"GLD": 15})
    b.cancels = False
    b.late = [(b.resolve_contract("ES"), "BUY", 2)]  # sent just before Ctrl-C
    monkeypatch.setattr(closing.time, "sleep", lambda s: b.settle_late())
    _, left = _closer(b).close(PerformanceTracker())
    assert left == {} and len(b.orders) == 2


def test_working_entry_is_cancelled_before_closing():
    b = ScriptedBroker({"GLD": 15})
    b.late = [(b.resolve_contract("ES"), "BUY", 2)]
    _, left = _closer(b).close(PerformanceTracker())
    b.settle_late()
    assert left == {} and b.get_positions_for_sync() == []


def test_failing_cancel_does_not_stop_the_close():
    b = ScriptedBroker({"GLD": 15})
    b.cancel_working_orders = lambda: 1 / 0
    assert _closer(b).close(PerformanceTracker())[1] == {}


def test_end_of_day_keeps_trying_until_flat():
    b = ScriptedBroker({"GLD": 15})
    b.script = ["reject"] * 10  # e.g. the broker is down for a few minutes
    _, left = _closer(b).close(PerformanceTracker(), closing.END_OF_DAY)
    assert left == {}


def test_stop_request_shortens_the_end_of_day_retries():
    b = ScriptedBroker({"GLD": 15})
    b.script = ["reject"] * 999
    closer = _closer(b)
    closer._guard.stop_event.set()
    closer.close(PerformanceTracker(), closing.END_OF_DAY)
    assert len(b.orders) == 1 + len(closing.STOPPING)


def test_no_signal_is_traded_after_the_session_end():
    traded: list = []
    strategy = SimpleNamespace(handle_signal=traded.append, tick=lambda: None)
    q: queue.Queue = queue.Queue()
    q.put("late signal")
    ended = datetime.now(timezone.utc) - timedelta(hours=8)  # the computer slept
    reached = app._run_trading_day(
        ended, timezone.utc, ScriptedBroker({}), strategy, PerformanceTracker(), _settings(), threading.Event(), q
    )
    assert reached and traded == []


def test_idle_task_runs_until_it_succeeds(monkeypatch):
    clock = itertools.count(0, 100)
    monkeypatch.setattr(app.time, "monotonic", lambda: next(clock))
    results = [False, True]
    target = datetime.now(timezone.utc) + timedelta(seconds=0.2)
    app._sleep_until(target, threading.Event(), queue.Queue(), idle_task=lambda: results.pop(0))
    assert results == []


# ---------------------------------------------------------------------------
# The whole program
# ---------------------------------------------------------------------------


def _run_main(monkeypatch, tmp_path, broker, day, starts_in=timedelta(hours=-1), **kw):
    """Run ``main()`` for one session; *day* replaces the trading loop."""
    monkeypatch.chdir(tmp_path)
    now = datetime.now(timezone.utc)
    kw.setdefault("follow_signal_windows", False)  # no network
    settings = _settings(allowed_symbols="ES", session_timezone="UTC", run_days=1, **kw)
    monkeypatch.setattr(app, "load_settings", lambda: settings)
    monkeypatch.setattr(
        app, "_compute_next_session_window", lambda n, s: (now + starts_in, now + timedelta(hours=1))
    )
    monkeypatch.setattr(app, "create_broker", lambda s: broker)
    monkeypatch.setattr(app, "_log_signal_catalog", lambda s: None)
    monkeypatch.setattr(app, "run_startup_access_checks", lambda *a: None)
    monkeypatch.setattr(app, "stream_signals_thread", lambda *a: None)
    monkeypatch.setattr(app, "_run_trading_day", day)
    before = signal.getsignal(signal.SIGTERM)
    try:
        app.main()
    finally:
        assert signal.getsignal(signal.SIGTERM) is before


def test_positions_from_an_earlier_session_are_closed_before_trading(monkeypatch, tmp_path):
    b = ScriptedBroker({"QQQ": 7, "GLD": 7})
    seen: list = []
    _run_main(monkeypatch, tmp_path, b, lambda *a: seen.append((b.get_positions_for_sync(), a[4].total_trades)) or True)
    assert seen == [([], 0)]  # flat, and the old loss is not in today's numbers


def test_earlier_positions_are_closed_at_once_even_outside_the_session(monkeypatch, tmp_path):
    b = ScriptedBroker({"GLD": 7})
    open_while_waiting: list = []

    def sleep_until(*a, **kw):
        open_while_waiting.extend(b.get_positions_for_sync())
        return False

    monkeypatch.setattr(app, "_sleep_until", sleep_until)
    _run_main(monkeypatch, tmp_path, b, None, starts_in=timedelta(minutes=30))
    assert open_while_waiting == [] and len(b.orders) == 1


def test_no_trading_while_earlier_positions_cannot_be_closed(monkeypatch, tmp_path):
    b = ScriptedBroker({"GLD": 7})
    b.script = ["reject"] * 9999
    traded: list = []
    monkeypatch.setattr(app, "_wait_until_flat", lambda *a: a[0].close_leftovers())
    _run_main(monkeypatch, tmp_path, b, lambda *a: traded.append(1) or True)
    assert traded == []


def test_wait_until_flat_gives_up_at_the_session_end():
    b = ScriptedBroker({"GLD": 7})
    b.script = ["reject"] * 9999
    ended = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert not app._wait_until_flat(_closer(b), ended, threading.Event(), queue.Queue())


@pytest.mark.parametrize("kw", [{"close_on_exit": False}, {"close_on_start": False}])
def test_earlier_positions_can_be_kept(monkeypatch, tmp_path, kw):
    b = ScriptedBroker({"GLD": 7})
    seen: list = []
    _run_main(monkeypatch, tmp_path, b, lambda *a: seen.append(len(b.get_positions_for_sync())) or True, **kw)
    assert seen == [1]


def _open_then(b, action):
    def day(*_):
        b._sim_positions["GLD"] = {"pos": 3, "avg_cost": 100.0}
        return action()

    return day


def test_crash_closes_positions_and_still_fails(monkeypatch, tmp_path):
    b = ScriptedBroker({})
    with pytest.raises(ZeroDivisionError):
        _run_main(monkeypatch, tmp_path, b, _open_then(b, lambda: 1 / 0))
    assert b.get_positions_for_sync() == []


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_every_stop_signal_closes_positions(monkeypatch, tmp_path, signum):
    b = ScriptedBroker({})
    _run_main(monkeypatch, tmp_path, b, _open_then(b, lambda: _ctrl_c(signum)))
    assert b.get_positions_for_sync() == [] and len(b.orders) == 1


def test_lost_signal_stream_closes_positions(monkeypatch, tmp_path):
    b = ScriptedBroker({})
    _run_main(monkeypatch, tmp_path, b, _open_then(b, lambda: False))
    assert b.get_positions_for_sync() == []


def test_failed_end_of_day_close_is_tried_again_on_the_way_out(monkeypatch, tmp_path):
    b = ScriptedBroker({})
    b.script = ["reject"] * (1 + len(closing.END_OF_DAY))
    _run_main(monkeypatch, tmp_path, b, _open_then(b, lambda: True))
    assert b.get_positions_for_sync() == []


# ---------------------------------------------------------------------------
# Alpaca
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("left, confirmed", [([[], []], True), ([["o"], ["o"], []], True), ([["o"]] * 99, False)])
def test_alpaca_confirms_cancelled_orders(monkeypatch, left, confirmed):
    from qs_trader_broker_alpaca import AlpacaBroker

    reads, cancelled = iter(left), []
    b = object.__new__(AlpacaBroker)
    b._trading_client = SimpleNamespace(
        get_orders=lambda request: next(reads), cancel_orders=lambda: cancelled.append(1)
    )
    assert b.cancel_working_orders() is confirmed
    assert bool(cancelled) is bool(left[0])
