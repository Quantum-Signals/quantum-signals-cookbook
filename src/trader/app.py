from __future__ import annotations

import queue
import sys
import threading
import time
from loguru import logger
from quantumsignals.client import Client as QSClient
from trader.config import Settings, load_settings
from trader.gateways import create_gateway
from trader.gateways.base import GatewayBase
from trader.models import PerformanceTracker
from trader.reporting import report_once
from trader.strategy.momentum import MomentumStrategy
from trader.strategy.signal_follower import SignalFollowerStrategy, _extract_fill_price
from trader.streaming import stream_signals_thread

_STRATEGIES = {
    "momentum": MomentumStrategy,
    "signal_follower": SignalFollowerStrategy,
}


def _setup_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
        level="DEBUG",
    )
    logger.add(
        "logs/trader_{time:YYYY-MM-DD}.log",
        rotation="1 day",
        retention="30 days",
        level="DEBUG",
    )


def _log_model_catalog(settings: Settings) -> None:
    try:
        with QSClient(api_key=settings.qs_api_key) as qs:
            models = qs.get_model_catalog()
            logger.info(f"Available models ({len(models)}):")
            for model_id, model in models.items():
                symbols = ", ".join(model.trained_on_symbols)
                logger.info(f"  {model_id}: {symbols}")
    except Exception as exc:
        logger.warning(f"Could not fetch model catalog: {exc}")


def _close_all_positions(
    gw: GatewayBase,
    settings: Settings,
    perf: PerformanceTracker,
) -> float:
    """Flatten every open position at the broker before disconnecting.

    Each close is recorded in *perf* so the final summary is complete.
    Returns the total dollar PnL realised by these exit trades.
    """
    from trader.models import Signal

    exit_pnl = 0.0
    try:
        pos_list = gw.get_positions_for_sync()
        if not pos_list:
            logger.info("No open positions to close on exit")
            return 0.0

        logger.warning(f"CLOSE_ON_EXIT: closing {len(pos_list)} open position(s)…")
        for p in pos_list:
            sym = p["symbol"]
            qty = int(p["qty"])
            avg_price = p["avg_price"]
            if qty == 0:
                continue

            label = "LONG" if qty > 0 else "SHORT"
            logger.warning(f"  {label} {abs(qty)}x {sym} @ {avg_price:.2f}")

            contract = gw.resolve_contract(sym)
            if contract is None:
                logger.error(f"  -> Cannot resolve contract for {sym} — manual close required")
                continue

            action = "SELL" if qty > 0 else "BUY"
            trade = gw.place_order(contract, action, abs(qty))

            fill_price = _extract_fill_price(trade) if trade else None
            if fill_price is None:
                fill_price = gw.get_last_price(contract)

            if fill_price is not None:
                multiplier = settings.contract_multipliers.get(sym, 1.0)
                pnl = (fill_price - avg_price) * qty * multiplier
                exit_pnl += pnl

                direction = Signal.UP if qty > 0 else Signal.DOWN
                perf.record_close(sym, pnl, direction)

                logger.info(
                    f"  -> Closed {label} {abs(qty)}x {sym} @ {fill_price:.2f}  "
                    f"PnL=${pnl:+,.2f}"
                )
            else:
                logger.info(f"  -> Closed {label} {abs(qty)}x {sym} (fill price unavailable)")

    except Exception as exc:
        logger.error(f"Error closing positions on exit: {exc}")

    return exit_pnl


def main() -> None:
    _setup_logging()

    settings = load_settings()

    logger.info("=" * 60)
    logger.info("  QuantumSignals x IB Gateway Trader  ")
    logger.info(f"  Mode: {'DRY-RUN (simulated)' if settings.dry_run else 'LIVE (IB Gateway)'}")
    logger.info(f"  Strategy: {settings.strategy}")
    if settings.strategy == "momentum":
        logger.info(f"  Execution interval: {settings.execution_interval_minutes} min")
    logger.info("=" * 60)

    perf = PerformanceTracker()
    stop_event = threading.Event()
    gw = create_gateway(settings)

    maxsize = settings.signal_queue_maxsize if settings.signal_queue_maxsize > 0 else 0
    signal_queue: queue.Queue = queue.Queue(maxsize=maxsize)

    try:
        gw.connect()
    except Exception as exc:
        logger.critical(f"Cannot connect to broker: {exc}")
        sys.exit(1)

    _log_model_catalog(settings)

    strategy_cls = _STRATEGIES.get(settings.strategy)
    if strategy_cls is None:
        logger.critical(
            f"Unknown strategy '{settings.strategy}'. "
            f"Choose from: {', '.join(_STRATEGIES)}"
        )
        sys.exit(1)
    strategy = strategy_cls(gw, perf, settings)
    strategy.sync_positions()

    qs_thread = threading.Thread(
        target=stream_signals_thread,
        args=(settings, stop_event, signal_queue),
        name="qs-stream",
        daemon=True,
    )
    qs_thread.start()
    logger.info("QuantumSignals stream thread started")

    # ib_async: same thread as gw.connect() must run qualifyContracts / orders / account.
    report_interval_s = 60.0
    tick_interval_s = 1.0
    poll_s = 0.05
    next_report_at = time.monotonic() + report_interval_s
    next_tick_at = time.monotonic()

    logger.info("Bot running. Press Ctrl-C to stop.")
    try:
        while not stop_event.is_set():
            # Drain all pending signals (main thread = IB event loop thread)
            try:
                while True:
                    item = signal_queue.get_nowait()
                    if item is None:
                        break
                    try:
                        strategy.handle_signal(item)
                    except Exception as exc:
                        logger.exception(
                            f"Strategy error for signal {getattr(item, 'symbol', '?')}: {exc}"
                        )
            except queue.Empty:
                pass

            now = time.monotonic()

            if now >= next_tick_at:
                try:
                    strategy.tick()
                except Exception as exc:
                    logger.exception(f"Strategy tick error: {exc}")
                next_tick_at = now + tick_interval_s

            if now >= next_report_at:
                report_once(gw, perf)
                next_report_at = now + report_interval_s

            time.sleep(poll_s)

    except KeyboardInterrupt:
        logger.info("Ctrl-C received — shutting down…")
    finally:
        stop_event.set()

        pnl_before_exit = perf.gross_pnl
        exit_pnl = 0.0
        if settings.close_on_exit:
            exit_pnl = _close_all_positions(gw, settings, perf)

        cap = settings.daily_start_capital

        logger.info("=" * 60)
        logger.info("SESSION SUMMARY")
        logger.info(f"  In-session PnL (flattened) : ${pnl_before_exit:+,.2f}")
        logger.info(f"  Exit close PnL             : ${exit_pnl:+,.2f}")
        logger.info(f"  Total session PnL          : ${perf.gross_pnl:+,.2f}")
        logger.info(f"  Return on ${cap:,.0f}        : {perf.gross_pnl / cap:+.2%}")
        logger.info(f"  {perf.summary()}")
        logger.info("=" * 60)

        gw.disconnect()
        logger.info("Goodbye.")
