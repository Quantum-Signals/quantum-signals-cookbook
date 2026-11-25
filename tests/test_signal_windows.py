"""Entry and exit times published in the signal catalog."""

import queue
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import trader.app as app
import trader.closing as closing
import trader.signal_windows as sw
from qs_trader_broker_core import SimulatedBroker, SimulatedSettings
from trader.config import Settings
from trader.models import PerformanceTracker
from trader.strategy.momentum import MomentumStrategy

NY = ZoneInfo("America/New_York")


def ny(hour, minute=0, day=19):
    return datetime(2026, 11, day, hour, minute, tzinfo=NY)


def meta(symbol, entry=("09:30", "09:50"), exit_="13:25", retired=False, spec=True):
    window = lambda start, end: SimpleNamespace(start=start, end=end)  # noqa: E731
    trading_spec = SimpleNamespace(
        timezone="America/New_York",
        entry_window=window(*entry) if entry else None,
        exit_window=window(exit_, "16:00") if exit_ else None,
    )
    return SimpleNamespace(
        model_output_symbol=symbol, retired=retired, trading_spec=trading_spec if spec else None
    )


@pytest.fixture
def catalog(monkeypatch):
    state = {"catalog": {"GOLD_DTC": meta("GC"), "SP500_DTC": meta("ES", ("09:45", "15:30"), "15:55")}, "subs": None}

    class FakeClient:
        def __init__(self, api_key):
            if state["catalog"] is None:
                raise ConnectionError("down")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_signal_catalog(self):
            return state["catalog"]

        def get_subscriptions(self):
            ids = state["subs"] if state["subs"] is not None else state["catalog"]
            return [SimpleNamespace(signal_id=i) for i in ids]

    monkeypatch.setattr(sw, "QSClient", FakeClient)
    monkeypatch.setattr(closing.time, "sleep", lambda s: None)
    return state


def _settings(**kw):
    return Settings(_env_file=None, qs_api_key="k", allowed_symbols="GC,ES", **kw)


def _windows(catalog):
    w = sw.SignalWindows(_settings())
    assert w.load()
    return w


@pytest.mark.parametrize(
    "at, allowed",
    [((9, 29), False), ((9, 30), True), ((9, 49), True), ((9, 50), False), ((12, 35), False), ((13, 25), False)],
)
def test_entry_window_end_is_excluded(catalog, at, allowed):
    assert _windows(catalog).allows("GC", ny(*at)) is allowed


def test_times_are_converted_from_the_signals_timezone(catalog):
    w = _windows(catalog)
    assert w.allows("GC", datetime(2026, 11, 19, 14, 35, tzinfo=timezone.utc))
    # New York is on summer time, so 09:35 there is 13:35 UTC.
    assert w.allows("GC", datetime(2026, 7, 8, 13, 35, tzinfo=timezone.utc))
    assert not w.allows("GC", datetime(2026, 7, 8, 14, 35, tzinfo=timezone.utc))


def test_unreadable_times_do_not_limit_trading_and_the_last_known_ones_are_kept(catalog):
    w = sw.SignalWindows(_settings())
    known, catalog["catalog"] = catalog["catalog"], None
    assert not w.load() and w.allows("GC", ny(12)) and w.due_exits(ny(15), ny(15, 55)) == set()
    catalog["catalog"] = known
    assert w.load() and w.allows("GC", ny(9, 35))
    catalog["catalog"] = None
    assert not w.load() and w.allows("GC", ny(9, 35)) and not w.allows("GC", ny(10))


def test_signal_without_published_times_is_not_limited(catalog):
    catalog["catalog"] = {"GOLD_DTC": meta("GC", spec=False)}
    w = _windows(catalog)
    assert w.allows("GC", ny(3)) and w.due_exits(ny(15), ny(15, 55)) == set()


def test_exit_alone_still_stops_trading(catalog):
    catalog["catalog"] = {"GOLD_DTC": meta("GC", entry=None)}
    w = _windows(catalog)
    assert w.allows("GC", ny(13, 24)) and not w.allows("GC", ny(13, 25))


def test_retired_and_unsubscribed_signals_are_ignored(catalog):
    catalog["catalog"]["OLD"] = meta("GC", ("09:30", "09:31"), retired=True)
    catalog["catalog"]["OTHER"] = meta("ES", ("09:30", "09:31"))
    catalog["subs"] = ["GOLD_DTC", "SP500_DTC", "OLD"]
    w = _windows(catalog)
    assert w.allows("GC", ny(9, 49)) and w.allows("ES", ny(15))


def test_two_signals_on_one_instrument_use_the_tightest_times(catalog):
    catalog["catalog"]["GOLD_2"] = meta("GC", ("09:40", "11:00"), "12:00")
    w = _windows(catalog)
    assert [w.allows("GC", ny(9, m)) for m in (35, 45, 55)] == [False, True, False]
    assert w.due_exits(ny(12), ny(15, 55)) == {"GC"}


def test_due_exits(catalog):
    w = _windows(catalog)
    assert w.due_exits(ny(13, 24), ny(15, 55)) == set()
    assert w.due_exits(ny(13, 25), ny(15, 55)) == {"GC"}
    # ES exits at the session end, which closes everything anyway.
    assert w.due_exits(ny(15, 56), ny(15, 55)) == {"GC"}
    # Half day: the session ends before gold's exit.
    assert w.due_exits(ny(13, 30), ny(12, 55)) == set()
    w.mark_exited({"GC"})
    assert w.due_exits(ny(14), ny(15, 55)) == set()
    w.start_day(ny(9, 30, day=20))
    assert w.due_exits(ny(14, day=20), ny(15, 55, day=20)) == {"GC"}


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


def _signal(symbol, at, value=2):
    return SimpleNamespace(
        symbol=symbol, signal=value, signal_time_utc=at.astimezone(timezone.utc), model="m",
        is_null_signal=lambda: False,
    )


def _strategy(catalog, interval=30):
    broker = SimulatedBroker(SimulatedSettings(price_jitter=0.0))
    broker.connect()
    settings = _settings(execution_interval_minutes=interval, daily_start_capital=40_000)
    return broker, MomentumStrategy(broker, PerformanceTracker(), settings, windows=_windows(catalog))


def _orders(broker):
    return [(o["symbol"], o["action"]) for o in broker._orders]


def test_signals_after_the_entry_window_do_not_trade(catalog):
    broker, strategy = _strategy(catalog)
    for i in range(0, 200):  # 09:35 to 12:54
        strategy.handle_signal(_signal("GC", ny(9, 35) + timedelta(minutes=i)))
    assert len(_orders(broker)) == 1


def test_opposite_signal_after_the_entry_window_holds_the_position(catalog):
    broker, strategy = _strategy(catalog)
    strategy.handle_signal(_signal("GC", ny(9, 35)))
    strategy.handle_signal(_signal("GC", ny(11), value=0))
    assert len(_orders(broker)) == 1


def test_signals_before_the_window_do_not_start_the_interval(catalog):
    broker, strategy = _strategy(catalog, interval=60)
    strategy.handle_signal(_signal("ES", ny(9, 31)))
    strategy.handle_signal(_signal("ES", ny(9, 45)))
    assert len(_orders(broker)) == 1


def test_without_windows_nothing_changes(catalog):
    broker = SimulatedBroker(SimulatedSettings(price_jitter=0.0))
    strategy = MomentumStrategy(broker, PerformanceTracker(), _settings(execution_interval_minutes=30))
    strategy.handle_signal(_signal("GC", ny(12)))
    assert len(_orders(broker)) == 1


# ---------------------------------------------------------------------------
# Exits
# ---------------------------------------------------------------------------


class Broker(SimulatedBroker):
    def __init__(self, **positions):
        super().__init__(SimulatedSettings(price_jitter=0.0))
        self._sim_positions = {s: {"pos": q, "avg_cost": 100.0} for s, q in positions.items()}
        self.refuse: set[str] = set()
        self.unreadable = 0
        self.sent: list[str] = []

    def place_order(self, contract, action, qty, price=None, position_intent=None):
        self.sent.append(contract.symbol)
        if contract.symbol in self.refuse:
            self.last_order_not_placed = True
            return None
        return super().place_order(contract, action, qty, price, position_intent)

    def get_positions_for_sync(self):
        if self.unreadable:
            self.unreadable -= 1
            raise TimeoutError
        return super().get_positions_for_sync()

    def held(self):
        return {p["symbol"] for p in super().get_positions_for_sync()}


def _closer(broker):
    return closing.PositionCloser(broker, _settings(), closing.StopGuard(threading.Event()))


def test_exit_closes_only_that_instrument_under_any_contract_name(catalog):
    b = Broker(GLD=7, SPY=3)
    perf = PerformanceTracker()
    assert _closer(b).close_instruments({"GC"}, perf) == {"GC"}
    assert b.held() == {"SPY"} and perf.total_trades == 1


def test_exit_with_nothing_open_sends_nothing(catalog):
    b = Broker(SPY=3)
    assert _closer(b).close_instruments({"GC"}, PerformanceTracker()) == {"GC"} and b.sent == []


def test_failed_exit_is_not_confirmed_and_can_be_retried(catalog):
    b = Broker(GLD=7, SPY=3)
    b.refuse = {"GLD"}
    closer = _closer(b)
    assert closer.close_instruments({"GC"}, PerformanceTracker()) == set()
    b.refuse = set()
    assert closer.close_instruments({"GC"}, PerformanceTracker()) == {"GC"}
    assert b.held() == {"SPY"}


def test_unreadable_positions_confirm_nothing(catalog):
    b = Broker(GLD=7)
    b.unreadable = 99
    assert _closer(b).close_instruments({"GC"}, PerformanceTracker()) == set()


def test_pending_order_of_another_instrument_is_remembered(catalog):
    """An exit must not forget an order of unknown outcome on something else."""
    b = Broker(GLD=7, SPY=3)
    b.cancel_working_orders = lambda: False
    closer = _closer(b)
    order = closing._Order({"symbol": "SPY", "qty": 3.0, "avg_price": 100.0}, "contract", None, may_fill=True)
    closer._orders["SPY"] = order
    closer.close_instruments({"GC"}, PerformanceTracker())
    assert closer._orders["SPY"] is order and "SPY" not in b.sent


def _main(monkeypatch, tmp_path, broker, strategy_day, **kw):
    """Run ``main()`` with the real trading loop, ending the session shortly."""
    monkeypatch.chdir(tmp_path)
    start = datetime.now(timezone.utc)
    settings = Settings(
        _env_file=None, qs_api_key="k", allowed_symbols="GC,ES", session_timezone="UTC", run_days=1,
        close_on_start=False, **kw,
    )
    monkeypatch.setattr(app, "load_settings", lambda: settings)
    monkeypatch.setattr(
        app, "_compute_next_session_window",
        lambda n, s: (start - timedelta(hours=1), start + timedelta(seconds=1.5)),
    )
    monkeypatch.setattr(app, "create_broker", lambda s: broker)
    for name in ("_log_signal_catalog", "run_startup_access_checks", "stream_signals_thread"):
        monkeypatch.setattr(app, name, lambda *a: None)
    held_during: list = []
    real = sw.SignalWindows.mark_exited

    def record(self, symbols):
        real(self, symbols)
        held_during.append((set(symbols), broker.held()))

    monkeypatch.setattr(sw.SignalWindows, "mark_exited", record)
    strategy_day(start)
    app.main()
    return held_during


def test_main_closes_gold_at_its_exit_and_the_rest_at_session_end(monkeypatch, tmp_path, catalog):
    b = Broker(GLD=7, SPY=3)

    def times(start):
        past = (start.astimezone(NY) - timedelta(minutes=1)).strftime("%H:%M")
        catalog["catalog"] = {"GOLD_DTC": meta("GC", None, past), "SP500_DTC": meta("ES", None, None, spec=False)}

    events = _main(monkeypatch, tmp_path, b, times)
    assert events[0] == ({"GC"}, {"SPY"})  # gold closed first, ES still open
    assert b.held() == set()


def test_unfinished_exit_is_tried_again(monkeypatch, tmp_path, catalog):
    b = Broker(GLD=7, SPY=3)
    b.refuse = {"GLD"}
    real = b.place_order

    def place_order(*a, **kw):
        if b.sent.count("GLD") == 1 + len(closing.ONCE):  # the first exit has failed
            b.refuse = set()
        return real(*a, **kw)

    b.place_order = place_order
    monkeypatch.setattr(app, "_RETRY_S", 0.2)

    def times(start):
        past = (start.astimezone(NY) - timedelta(minutes=1)).strftime("%H:%M")
        catalog["catalog"] = {"GOLD_DTC": meta("GC", None, past)}

    assert _main(monkeypatch, tmp_path, b, times)[:2] == [(set(), {"GLD", "SPY"}), ({"GC"}, {"SPY"})]


def test_instruments_the_bot_does_not_trade_are_left_alone(catalog):
    catalog["catalog"]["SILVER_DTC"] = meta("SI", exit_="12:00")
    assert _windows(catalog).due_exits(ny(14), ny(15, 55)) == {"GC"}


@pytest.mark.parametrize("kw", [{"follow_signal_windows": False}, {"close_on_exit": False}])
def test_exits_can_be_turned_off(monkeypatch, tmp_path, catalog, kw):
    b = Broker(GLD=7)

    def times(start):
        past = (start.astimezone(NY) - timedelta(minutes=1)).strftime("%H:%M")
        catalog["catalog"] = {"GOLD_DTC": meta("GC", None, past)}

    assert _main(monkeypatch, tmp_path, b, times, **kw) == []


def test_times_are_read_again_during_the_session(monkeypatch, tmp_path, catalog):
    b = Broker(GLD=7, SPY=3)
    down = {"left": 2}

    def times(start):
        past = (start.astimezone(NY) - timedelta(minutes=1)).strftime("%H:%M")
        catalog["catalog"] = {"GOLD_DTC": meta("GC", None, past)}

    real = sw.SignalWindows.load

    def load(self):
        if down["left"]:
            down["left"] -= 1
            return False
        return real(self)

    monkeypatch.setattr(sw.SignalWindows, "load", load)
    monkeypatch.setattr(app, "_RETRY_S", 0.2)
    assert _main(monkeypatch, tmp_path, b, times)[0] == ({"GC"}, {"SPY"})
