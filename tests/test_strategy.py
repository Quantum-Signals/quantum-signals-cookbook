from dataclasses import dataclass

from trader.config import Settings
from qs_trader_broker_core import SimulatedBroker, SimulatedSettings
from trader.models import PerformanceTracker, Signal
from trader.strategy.momentum import MomentumStrategy


def _make_settings(**overrides) -> Settings:
    # _env_file=None isolates tests from a developer's local .env so ambient
    # TRADER_* values (e.g. ALLOWED_SYMBOLS) can't leak into fixtures.
    defaults = dict(qs_api_key="test-key", dry_run=True)
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


@dataclass
class FakeSignal:
    """Mimics the QuantumSignals signal object for testing."""
    symbol: str
    signal: int
    model: str = "test-model"
    signal_time_utc: str = "2026-01-01T00:00:00Z"
    code_message: str = ""
    _null: bool = False

    def is_null_signal(self) -> bool:
        return self._null


def _fire(strategy: MomentumStrategy, symbol: str, signal_value: int) -> None:
    """Call _execute directly, bypassing the 5-minute cooldown timer.

    Passes signal_ts=0.0 (epoch 1970) so the stored last-exec time is far
    enough in the past that any subsequent handle_signal call will pass the
    cooldown check regardless of its signal_time_utc.
    """
    strategy._execute(symbol, signal_value, 0.0)


class TestMomentumStrategy:
    def _make_strategy(self, **settings_kw):
        settings = _make_settings(**settings_kw)
        # price_jitter=0.0 → deterministic prices so order sizing is reproducible.
        broker = SimulatedBroker(SimulatedSettings(price_jitter=0.0))
        broker.connect()
        perf = PerformanceTracker()
        strategy = MomentumStrategy(broker, perf, settings)
        return strategy, broker, perf

    def test_null_signal_ignored(self):
        strategy, broker, perf = self._make_strategy()
        strategy.handle_signal(FakeSignal(symbol="ES", signal=1, _null=True))
        assert len(broker.get_open_positions()) == 0
        assert perf.total_trades == 0

    def test_up_signal_opens_long(self):
        strategy, broker, perf = self._make_strategy()
        _fire(strategy, "ES", Signal.UP)
        assert len(broker.get_open_positions()) == 1
        pos = broker.get_open_positions()[0]
        assert pos["pos"] > 0

    def test_down_signal_opens_short(self):
        strategy, broker, perf = self._make_strategy()
        _fire(strategy, "ES", Signal.DOWN)
        assert len(broker.get_open_positions()) == 1
        pos = broker.get_open_positions()[0]
        assert pos["pos"] < 0

    def test_stable_signal_no_position(self):
        strategy, broker, perf = self._make_strategy()
        # STABLE with no position → no trade
        strategy._last_exec_time.clear()
        strategy.handle_signal(FakeSignal(symbol="ES", signal=Signal.STABLE))
        assert len(broker.get_open_positions()) == 0

    def test_long_closed_on_down(self):
        strategy, broker, perf = self._make_strategy()
        _fire(strategy, "ES", Signal.UP)
        assert len(broker.get_open_positions()) == 1

        # Second DOWN closes the long. With deterministic sim prices
        # (price_jitter=0.0) the UP and DOWN slices are the same size, so the
        # net cancels to exactly flat.
        _fire(strategy, "ES", Signal.DOWN)
        spy_pos = next(
            (p["pos"] for p in broker.get_open_positions() if p["symbol"] == "SPY"), 0.0
        )
        assert spy_pos == 0.0, (
            f"Expected flat after UP+DOWN, got pos={spy_pos}"
        )

    def test_long_closed_on_stable(self):
        # STABLE means "hold" — the long is kept, not closed.
        strategy, broker, perf = self._make_strategy()
        _fire(strategy, "ES", Signal.UP)
        # Trigger STABLE via handle_signal (the proper path that enforces "hold")
        strategy._last_exec_time.clear()
        strategy.handle_signal(FakeSignal(symbol="ES", signal=Signal.STABLE))
        spy_pos = next(
            (p["pos"] for p in broker.get_open_positions() if p["symbol"] == "SPY"), 0.0
        )
        assert spy_pos > 0, "Long should still be open after STABLE"
        assert perf.total_trades == 0  # no close yet

    def test_short_closed_on_up(self):
        strategy, broker, perf = self._make_strategy()
        _fire(strategy, "ES", Signal.DOWN)
        _fire(strategy, "ES", Signal.UP)
        # After DOWN then UP the short closes. With deterministic sim prices the
        # equal slices cancel to exactly flat.
        spy_pos = next(
            (p["pos"] for p in broker.get_open_positions() if p["symbol"] == "SPY"), 0.0
        )
        assert spy_pos == 0.0, f"Expected flat after DOWN+UP, got {spy_pos}"
        assert perf.total_trades == 1  # short trade was closed

    def test_short_closed_on_stable(self):
        # STABLE means "hold" — the short stays open.
        strategy, broker, perf = self._make_strategy()
        _fire(strategy, "ES", Signal.DOWN)
        strategy._last_exec_time.clear()
        strategy.handle_signal(FakeSignal(symbol="ES", signal=Signal.STABLE))
        spy_pos = next(
            (p["pos"] for p in broker.get_open_positions() if p["symbol"] == "SPY"), 0.0
        )
        assert spy_pos < 0, "Short should still be open after STABLE"
        assert perf.total_trades == 0  # no close yet

    def test_signal_ts_stamps_same_epoch(self):
        """Both symbols are stamped with the same epoch when signal_time_utc matches."""
        strategy, broker, perf = self._make_strategy()

        ts = "2026-01-01T19:08:00Z"
        strategy.handle_signal(FakeSignal(symbol="ES", signal=Signal.UP, signal_time_utc=ts))
        strategy.handle_signal(FakeSignal(symbol="NQ", signal=Signal.UP, signal_time_utc=ts))

        assert strategy._last_exec_time.get("ES") == strategy._last_exec_time.get("NQ"), (
            "Both symbols must share the same last-exec timestamp when their "
            "signal_time_utc is identical"
        )

    def test_signal_ts_prevents_nq_skip(self):
        """NQ must fire in cycle 2 even when its SSE event arrives seconds after ES.

        Regression for: QS stream delivers S&P500 and Nasdaq100 as separate
        SSE events ~1s apart.  With wall-clock gating, the 1-second offset
        accumulated in _last_exec_time caused one symbol to appear ~1s shy of
        the 300-second cooldown in the next cycle, permanently desynchronising
        the two instruments into alternating solo trades.

        The fix: key the cooldown on signal_time_utc (same for both symbols in
        the same QS bar) so the elapsed check is always identical.
        """
        strategy, broker, perf = self._make_strategy()

        ts1 = "2026-01-01T19:08:00Z"
        strategy.handle_signal(FakeSignal(symbol="ES", signal=Signal.UP, signal_time_utc=ts1))
        strategy.handle_signal(FakeSignal(symbol="NQ", signal=Signal.UP, signal_time_utc=ts1))

        # Both timers must be identical (same epoch) after cycle 1
        assert strategy._last_exec_time["ES"] == strategy._last_exec_time["NQ"]

        # Cycle 2: signal_time_utc is exactly 300s later → both cooldowns just expired
        ts2 = "2026-01-01T19:13:00Z"
        strategy.handle_signal(FakeSignal(symbol="ES", signal=Signal.UP, signal_time_utc=ts2))
        strategy.handle_signal(FakeSignal(symbol="NQ", signal=Signal.UP, signal_time_utc=ts2))

        symbols = {p["symbol"] for p in broker.get_open_positions()}
        assert "SPY" in symbols, "ES/SPY must trade in cycle 2"
        assert "QQQ" in symbols, "NQ/QQQ must trade in cycle 2"

    def test_cooldown_blocks_rapid_trades(self):
        """The 5-minute execution interval prevents back-to-back handle_signal calls."""
        strategy, broker, perf = self._make_strategy()
        strategy.handle_signal(FakeSignal(symbol="ES", signal=Signal.UP))
        # A second handle_signal arriving immediately is within the 5-min window
        strategy.handle_signal(FakeSignal(symbol="ES", signal=Signal.DOWN))
        positions = broker.get_open_positions()
        assert len(positions) == 1
        assert positions[0]["pos"] > 0  # still long; DOWN was blocked by cooldown

    def test_multiple_symbols_independent(self):
        strategy, broker, perf = self._make_strategy()
        _fire(strategy, "ES", Signal.UP)
        _fire(strategy, "NQ", Signal.DOWN)
        positions = broker.get_open_positions()
        symbols = {p["symbol"] for p in positions}
        assert symbols == {"SPY", "QQQ"}

    # ------------------------------------------------------------------
    # Regression test: strategy must continue shorting after all longs
    # are exhausted, not stop at flat (the sell_to_short bug).
    # ------------------------------------------------------------------
    def test_continues_shorting_after_longs_exhausted(self):
        """
        After closing all accumulated long slices, the next DOWN signal must
        open a short position — not silently stop trading.

        Scenario:
          - Send N UP signals → builds a long position of N slices
          - Send N+2 DOWN signals → first N close the longs (flat),
            next 2 must open and grow a short
        """
        strategy, broker, perf = self._make_strategy()

        n_up = 5
        for _ in range(n_up):
            _fire(strategy, "S&P500", Signal.UP)

        # Confirm we have a long position
        positions = broker.get_open_positions()
        assert any(p["pos"] > 0 for p in positions), "Expected a long after UP signals"
        long_qty = next(p["pos"] for p in positions if p["symbol"] == "SPY")
        assert long_qty > 0

        # Send more DOWNs than UPs so we clear all longs AND open a short:
        # with deterministic prices the first n_up DOWN slices cancel the longs
        # exactly, and the extra slices build the short.
        n_down = n_up + 3  # clear longs + build a short
        for _ in range(n_down):
            _fire(strategy, "S&P500", Signal.DOWN)

        positions_final = broker.get_open_positions()
        spy_pos_final = next(
            (p["pos"] for p in positions_final if p["symbol"] == "SPY"), 0.0
        )
        assert spy_pos_final < 0, (
            f"BUG: strategy stopped trading after closing longs. "
            f"Expected a short position, got pos={spy_pos_final}"
        )

    def test_dust_long_to_short(self):
        """Regression: fractional dust at broker must not block sell_to_open."""
        strategy, broker, perf = self._make_strategy(broker="alpaca")
        broker._sim_positions["SPY"] = {"pos": 0.0001, "avg_cost": 745.0}
        strategy._net_qty["S&P500"] = 0.0001
        strategy._total_cost["S&P500"] = 0.0001 * 745.0
        strategy._position_reserve["S&P500"] = -0.5

        _fire(strategy, "S&P500", Signal.DOWN)

        spy_pos = next(
            (p["pos"] for p in broker.get_open_positions() if p["symbol"] == "SPY"), 0.0
        )
        assert spy_pos < 0, (
            f"Expected short after dust long + DOWN, got pos={spy_pos}"
        )

    def test_long_to_short_crosses_zero(self):
        """Regression: long → short must use close then open, not one close order."""
        strategy, broker, perf = self._make_strategy(broker="alpaca")

        for _ in range(3):
            _fire(strategy, "S&P500", Signal.UP)

        long_qty = next(p["pos"] for p in broker.get_open_positions() if p["symbol"] == "SPY")
        assert long_qty > 0

        for _ in range(4):
            _fire(strategy, "S&P500", Signal.DOWN)

        spy_pos = next(
            (p["pos"] for p in broker.get_open_positions() if p["symbol"] == "SPY"), 0.0
        )
        assert spy_pos < 0, (
            f"Expected short after long unwound, got pos={spy_pos}"
        )

    def test_preserves_dust_after_unwind_to_flat(self):
        """Regression: broker dust after an unwind must not block the next short."""
        strategy, broker, perf = self._make_strategy(broker="alpaca")
        broker._sim_positions["SPY"] = {"pos": 0.0001, "avg_cost": 745.0}
        strategy._net_qty["S&P500"] = 0.0001
        strategy._total_cost["S&P500"] = 0.0001 * 745.0
        strategy._position_reserve["S&P500"] = -0.5

        _fire(strategy, "S&P500", Signal.DOWN)

        spy_pos = next(
            (p["pos"] for p in broker.get_open_positions() if p["symbol"] == "SPY"), 0.0
        )
        assert spy_pos < 0, (
            f"Expected short when opening from preserved dust, got pos={spy_pos}"
        )

    def test_stale_fractional_long_flat_broker_opens_whole_short(self):
        """Regression (July 8 2026): strategy carries a fractional long the broker
        no longer holds. A DOWN flip must open a *whole-share* short, not a
        fractional sell_to_open (Alpaca 42210000 "fractional orders cannot be
        sold short").
        """
        strategy, broker, perf = self._make_strategy(
            broker="alpaca",
            allowed_symbols=["ES"],
            daily_start_capital=50_000,
        )
        # Broker is flat for SPY, but strategy state drifted to a fractional long.
        assert "SPY" not in broker._sim_positions
        strategy._net_qty["ES"] = 0.5
        strategy._total_cost["ES"] = 0.5 * 600.0
        strategy._position_reserve["ES"] = 0.0

        _fire(strategy, "ES", Signal.DOWN)

        sell_orders = [o for o in broker._orders if o["action"] == "SELL"]
        assert len(sell_orders) == 1, (
            f"Expected a single whole-share sell_to_open, got {broker._orders}"
        )
        assert sell_orders[0]["position_intent"] == "open"
        qty = float(sell_orders[0]["qty"])
        assert abs(qty - round(qty)) < 1e-9, (
            f"Short open leg must be whole shares, got qty={qty}"
        )
        spy_pos = next(
            (p["pos"] for p in broker.get_open_positions() if p["symbol"] == "SPY"), 0.0
        )
        assert spy_pos < 0, f"Expected a short after DOWN flip, got pos={spy_pos}"
        assert abs(spy_pos - round(spy_pos)) < 1e-9, (
            f"Broker short must be a whole share, got pos={spy_pos}"
        )

    def test_june29_flat_strategy_broker_dust_to_short(self):
        """Regression: strategy flat + broker dust must close before sell_to_open."""
        strategy, broker, perf = self._make_strategy(broker="alpaca")
        broker._sim_positions["QQQ"] = {"pos": 0.000099994, "avg_cost": 716.0}
        strategy._net_qty["NQ"] = 0.0
        strategy._total_cost["NQ"] = 0.0
        strategy._position_reserve["NQ"] = -0.0489

        _fire(strategy, "NQ", Signal.DOWN)

        qqq_pos = next(
            (p["pos"] for p in broker.get_open_positions() if p["symbol"] == "QQQ"), 0.0
        )
        assert qqq_pos < 0, (
            f"Expected short after flat strategy + broker dust, got pos={qqq_pos}"
        )

    def test_synced_short_up_covers_then_opens_long(self):
        """Regression: synced short + UP must buy_to_close then buy_to_open (July 2026)."""
        strategy, broker, perf = self._make_strategy(
            broker="alpaca",
            allowed_symbols=["ES"],
            daily_start_capital=1_000_000,
        )
        broker._sim_positions["SPY"] = {"pos": -3.809, "avg_cost": 732.09}
        strategy.sync_positions()

        assert strategy._net_qty["ES"] == -3.809
        _fire(strategy, "ES", Signal.UP)

        buy_orders = [o for o in broker._orders if o["action"] == "BUY"]
        assert len(buy_orders) == 2, (
            f"Expected cover + open legs, got orders={broker._orders}"
        )
        assert buy_orders[0]["position_intent"] == "close"
        assert buy_orders[1]["position_intent"] == "open"
        spy_pos = next(
            (p["pos"] for p in broker.get_open_positions() if p["symbol"] == "SPY"), 0.0
        )
        assert spy_pos > 0, f"Expected long after covering short, got pos={spy_pos}"

    def test_sell_capped_to_broker_long_when_strategy_ahead(self):
        """Regression: do not send uncapped sell when broker holds less long than bot."""
        strategy, broker, perf = self._make_strategy(
            broker="alpaca",
            allowed_symbols=["ES"],
            daily_start_capital=50_000,
        )
        broker._sim_positions["SPY"] = {"pos": 3.809, "avg_cost": 732.09}
        strategy.sync_positions()
        # Drift: bot thinks it holds more long than the broker.
        strategy._net_qty["ES"] = 5.0
        strategy._total_cost["ES"] = 5.0 * 732.09

        _fire(strategy, "ES", Signal.DOWN)

        sell_orders = [o for o in broker._orders if o["action"] == "SELL"]
        assert len(sell_orders) >= 1
        assert sell_orders[0]["position_intent"] == "close"
        assert sell_orders[0]["qty"] <= 3.809 + 1e-4

    def test_sync_failure_preserves_state(self):
        """Regression: failed broker fetch must not wipe in-memory positions."""
        strategy, broker, perf = self._make_strategy(broker="alpaca")
        strategy._net_qty["ES"] = 5.0
        strategy._total_cost["ES"] = 500.0

        def _fail_sync():
            raise ConnectionError("network down")

        broker.get_positions_for_sync = _fail_sync  # type: ignore[method-assign]

        try:
            strategy.sync_positions()
        except ConnectionError:
            pass

        assert strategy._net_qty.get("ES") == 5.0
        assert strategy._total_cost.get("ES") == 500.0

    def test_normalize_position_side_alpaca_enum(self):
        """Regression: PositionSide str() is 'PositionSide.SHORT', not 'short'."""
        from alpaca.trading.enums import PositionSide

        from qs_trader_broker_core import normalize_position_side

        assert normalize_position_side(PositionSide.SHORT) == "short"
        assert normalize_position_side(PositionSide.LONG) == "long"
        assert normalize_position_side("PositionSide.SHORT") == "short"
        assert normalize_position_side("short") == "short"

    def test_open_leg_not_oversized_cover_after_stale_short(self):
        """Regression: do not flip open leg to buy_to_close on stale short API."""
        strategy, broker, perf = self._make_strategy(
            broker="alpaca",
            allowed_symbols=["ES"],
            daily_start_capital=1_000_000,
        )
        broker._sim_positions["SPY"] = {"pos": -3.809, "avg_cost": 732.09}
        strategy.sync_positions()

        orig_sync = broker.get_positions_for_sync
        stale_calls = {"n": 0}

        def _stale_then_live():
            stale_calls["n"] += 1
            if stale_calls["n"] <= 4:
                return [{
                    "symbol": "SPY",
                    "qty": -3.809,
                    "qty_available": 3.809,
                    "side": "short",
                    "avg_price": 732.09,
                }]
            return orig_sync()

        broker.get_positions_for_sync = _stale_then_live  # type: ignore[method-assign]

        _fire(strategy, "ES", Signal.UP)

        buy_orders = [o for o in broker._orders if o["action"] == "BUY"]
        assert len(buy_orders) == 2
        assert buy_orders[0]["position_intent"] == "close"
        assert buy_orders[0]["qty"] <= 3.809 + 1e-4
        assert buy_orders[1]["position_intent"] == "open"
        assert buy_orders[1]["qty"] > 4.0

    def test_broker_long_qty_zero_when_short(self):
        """Regression: qty_available on a short must not count as long to close."""
        from trader.strategy.momentum import _plan_legs

        # Extending a short: the short's qty_available must not produce a
        # sell_to_close leg — only a delta-sized sell_to_open.
        legs = _plan_legs(current=-36.0, target=-54.0, avail_close=36.0, use_fractional=True)
        assert [(l.action, l.qty, l.intent) for l in legs] == [("SELL", 18.0, "open")]

    def test_plan_legs_state_space(self):
        """Table test for the reconciliation core across the position state space."""
        from trader.strategy.momentum import _plan_legs

        cases = [
            # (current, target, avail, expected [(action, qty, intent), ...])
            # flat → long / short
            (0.0, 5.5, 0.0, [("BUY", 5.5, "open")]),
            (0.0, -5.0, 0.0, [("SELL", 5.0, "open")]),
            # extend / reduce a long
            (5.0, 6.5, 5.0, [("BUY", 1.5, "open")]),
            (5.0, 3.5, 5.0, [("SELL", 1.5, "close")]),
            # extend / reduce a short — deltas only (July 8 2026 doubling bug)
            (-36.0, -54.0, 36.0, [("SELL", 18.0, "open")]),
            (-36.0, -27.0, 36.0, [("BUY", 9.0, "close")]),
            # cross zero in both directions
            (3.5, -2.0, 3.5, [("SELL", 3.5, "close"), ("SELL", 2.0, "open")]),
            (-3.0, 2.5, 3.0, [("BUY", 3.0, "close"), ("BUY", 2.5, "open")]),
            # long dust → short
            (0.000099994, -1.0, 0.000099994,
             [("SELL", 0.000099994, "close"), ("SELL", 1.0, "open")]),
            # short dust → long
            (-1e-8, 5.0, 1e-8, [("BUY", 1e-8, "close"), ("BUY", 5.0, "open")]),
            # short dust → more short
            (-0.0001, -2.0, 0.0001,
             [("BUY", 0.0001, "close"), ("SELL", 2.0, "open")]),
            # cover absorbs leftover dust
            (-2.0, -0.0001, 2.0, [("BUY", 2.0, "close")]),
            # close capped to what the broker has available
            (5.0, 0.0, 3.809, [("SELL", 3.809, "close")]),
            # opposite side not clearable → close-only progress, no open leg
            (5.0, -2.0, 3.809, [("SELL", 3.809, "close")]),
            # no-op
            (5.0, 5.0, 5.0, []),
        ]
        for current, target, avail, expected in cases:
            legs = _plan_legs(current, target, avail, use_fractional=True)
            got = [(l.action, l.qty, l.intent) for l in legs]
            assert len(got) == len(expected), (
                f"current={current} target={target} avail={avail}: "
                f"expected {expected}, got {got}"
            )
            for (ga, gq, gi), (ea, eq, ei) in zip(got, expected):
                assert ga == ea and gi == ei and abs(gq - eq) < 1e-9, (
                    f"current={current} target={target} avail={avail}: "
                    f"expected {expected}, got {got}"
                )

    def test_extend_short_single_sell_to_open(self):
        """Regression: adding to an existing short must not send sell_to_close."""
        strategy, broker, perf = self._make_strategy(
            broker="alpaca",
            allowed_symbols=["NQ"],
            daily_start_capital=1_000_000,
        )
        broker._sim_positions["QQQ"] = {"pos": -36.0, "avg_cost": 710.0}
        strategy.sync_positions()
        assert strategy._net_qty["NQ"] == -36.0

        _fire(strategy, "NQ", Signal.DOWN)

        sell_orders = [o for o in broker._orders if o["action"] == "SELL"]
        assert len(sell_orders) == 1, (
            f"Expected one sell_to_open leg, got orders={broker._orders}"
        )
        assert sell_orders[0]["position_intent"] == "open"
        # Regression (July 8 2026): the open leg must be the *delta* only.
        # Sending abs(target_net) re-shorts the whole existing position on
        # top of itself, so the broker ends ~2x more short than the strategy
        # believes and buying power is eventually exhausted (code 40310000).
        sold = float(sell_orders[0]["qty"])
        assert sold < 36.0, (
            f"Extend-short leg must be the delta, not the full target: qty={sold}"
        )
        qqq_pos = next(
            p["pos"] for p in broker.get_open_positions() if p["symbol"] == "QQQ"
        )
        assert abs(qqq_pos - strategy._net_qty["NQ"]) < 1e-6, (
            f"Broker ({qqq_pos}) and strategy ({strategy._net_qty['NQ']}) "
            f"must agree after extending a short"
        )
        assert qqq_pos == -36.0 - sold

    def test_short_dust_down_covers_then_opens_whole_short(self):
        """Cover fractional short residue before opening a whole-share short."""
        strategy, broker, _perf = self._make_strategy(
            broker="alpaca",
            allowed_symbols=["NQ"],
            daily_start_capital=100_000,
        )
        broker._sim_positions["QQQ"] = {"pos": -0.0001, "avg_cost": 470.98}
        strategy.sync_positions()

        _fire(strategy, "NQ", Signal.DOWN)

        buy_orders = [o for o in broker._orders if o["action"] == "BUY"]
        sell_orders = [o for o in broker._orders if o["action"] == "SELL"]
        assert len(buy_orders) == 1 and buy_orders[0]["position_intent"] == "close"
        assert abs(float(buy_orders[0]["qty"]) - 0.0001) < 1e-9
        assert len(sell_orders) == 1 and sell_orders[0]["position_intent"] == "open"
        sold = float(sell_orders[0]["qty"])
        assert abs(sold - round(sold)) < 1e-9
        qqq_pos = next(
            p["pos"] for p in broker.get_open_positions() if p["symbol"] == "QQQ"
        )
        assert qqq_pos < 0
        assert abs(qqq_pos - round(qqq_pos)) < 1e-6

    def test_short_dust_up_covers_then_opens_long(self):
        """Regression: sub-lot short dust must cover before buy_to_open (July 2026)."""
        strategy, broker, perf = self._make_strategy(
            broker="alpaca",
            allowed_symbols=["ES"],
            daily_start_capital=1_000_000,
        )
        broker._sim_positions["SPY"] = {"pos": -1e-8, "avg_cost": 732.09}
        strategy.sync_positions()
        assert "ES" not in strategy._net_qty

        _fire(strategy, "ES", Signal.UP)

        buy_orders = [o for o in broker._orders if o["action"] == "BUY"]
        assert len(buy_orders) == 2, f"Expected cover + open, got {buy_orders}"
        assert buy_orders[0]["position_intent"] == "close"
        assert buy_orders[0]["qty"] <= 1e-7
        assert buy_orders[1]["position_intent"] == "open"
        assert buy_orders[1]["qty"] > 4.0
