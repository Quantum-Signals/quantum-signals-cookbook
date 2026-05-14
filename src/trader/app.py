from __future__ import annotations

import os
import queue
import sys
import threading
import time
from datetime import date, datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from loguru import logger
from quantumsignals.client import Client as QSClient
from trader.config import Settings, load_settings
from trader.gateways import create_gateway
from trader.gateways.base import GatewayBase
from trader.models import PerformanceTracker
from trader.reporting import report_once
from trader.strategy.base import StrategyBase
from trader.strategy.momentum import MomentumStrategy
from trader.strategy.signal_follower import SignalFollowerStrategy, _extract_fill_price
from trader.streaming import stream_signals_thread

_STRATEGIES = {
    "momentum": MomentumStrategy,
    "signal_follower": SignalFollowerStrategy,
}


def _setup_logging() -> str:
    """Set up the stderr sink. File sinks are added per trading day later.

    Returns a run-id string used to label the cumulative RUN SUMMARY at exit.
    """
    run_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    os.makedirs("logs", exist_ok=True)
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
        level="INFO",
    )
    return run_id


def _open_day_log(day: date, run_id: str) -> int:
    """Attach a loguru file sink for one trading day. Returns the handler id.

    Filename pattern: ``logs/session_<YYYY-MM-DD>_<HH-MM-SS>.log`` where the
    ``HH-MM-SS`` suffix is the **process start time** (i.e. the run_id). This
    guarantees a fresh file per process run, so Ctrl-C + restart on the same
    day produces two distinct files instead of appending to one.
    """
    time_suffix = run_id.split("_", 1)[1] if "_" in run_id else run_id
    log_path = f"logs/session_{day.isoformat()}_{time_suffix}.log"
    handler_id = logger.add(
        log_path,
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}",
        encoding="utf-8",
    )
    logger.info(f"Logging this trading day to {log_path}")
    return handler_id


def _close_day_log(handler_id: int | None) -> None:
    if handler_id is None:
        return
    try:
        logger.remove(handler_id)
    except ValueError:
        pass


def _get_tz(settings: Settings) -> ZoneInfo:
    try:
        return ZoneInfo(settings.session_timezone)
    except ZoneInfoNotFoundError:
        logger.warning(
            f"Unknown timezone '{settings.session_timezone}', falling back to UTC"
        )
        return ZoneInfo("UTC")


def _parse_hhmm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def _at_time_on(d: date, hhmm: str, tz: ZoneInfo) -> datetime:
    """Return a tz-aware datetime for ``d`` at ``HH:MM``."""
    h, m = _parse_hhmm(hhmm)
    return datetime.combine(d, dt_time(h, m), tzinfo=tz)


def _next_weekday(d: date) -> date:
    """Return the next Mon–Fri after ``d`` (handles weekends; no holiday calendar)."""
    d = d + timedelta(days=1)
    while d.weekday() >= 5:  # 5=Sat, 6=Sun
        d += timedelta(days=1)
    return d


def _is_weekday(d: date) -> bool:
    return d.weekday() < 5


def _compute_next_session_window(now: datetime, settings: Settings) -> tuple[datetime, datetime]:
    """Decide which trading day we should aim for and return its (start, end).

    Rules:
      - If ``now`` is on a weekday and before today's session_end_time → use today.
      - Otherwise → fast-forward to the next weekday.
    """
    today = now.date()
    today_end = _at_time_on(today, settings.session_end_time, now.tzinfo)
    if _is_weekday(today) and now < today_end:
        target = today
    else:
        target = _next_weekday(today)

    start = _at_time_on(target, settings.session_start_time, now.tzinfo)
    end = _at_time_on(target, settings.session_end_time, now.tzinfo)
    return start, end


def _sleep_until(
    target: datetime,
    stop_event: threading.Event,
    signal_queue: queue.Queue,
    poll_s: float = 5.0,
) -> bool:
    """Sleep (interruptibly) until ``target`` while draining the signal queue.

    Returns True if the target was reached, False if interrupted via stop_event.
    Discarding off-hours signals keeps the queue bounded overnight.
    """
    tz = target.tzinfo
    discarded = 0
    next_status = time.monotonic() + 600.0  # log a heartbeat every 10 min
    while not stop_event.is_set():
        now = datetime.now(tz)
        if now >= target:
            if discarded:
                logger.info(f"Discarded {discarded} off-hours signal(s) while idle")
            return True

        # Drain anything the QS stream pushed while we were asleep
        try:
            while True:
                item = signal_queue.get_nowait()
                if item is None:
                    continue
                discarded += 1
        except queue.Empty:
            pass

        if time.monotonic() >= next_status:
            remaining = target - now
            logger.info(
                f"Idle until {target.strftime('%Y-%m-%d %H:%M %Z')} "
                f"({_humanise_timedelta(remaining)} remaining)"
            )
            next_status = time.monotonic() + 600.0

        time.sleep(poll_s)
    return False


def _humanise_timedelta(td: timedelta) -> str:
    total = int(td.total_seconds())
    if total < 0:
        total = 0
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


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
    """Flatten every open position at the broker.

    Each close is recorded in *perf* so the daily summary is complete.
    Returns the total dollar PnL realised by these exit trades.
    """
    from trader.models import Signal

    exit_pnl = 0.0
    try:
        pos_list = gw.get_positions_for_sync()
        if not pos_list:
            logger.info("No open positions to close")
            return 0.0

        logger.warning(f"CLOSE: flattening {len(pos_list)} open position(s)…")
        for p in pos_list:
            sym = p["symbol"]
            qty = float(p["qty"])
            avg_price = p["avg_price"]
            if abs(qty) < 1e-9:
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
        logger.error(f"Error closing positions: {exc}")

    return exit_pnl


def _run_trading_day(
    eod_dt: datetime,
    tz: ZoneInfo,
    gw: GatewayBase,
    strategy: StrategyBase,
    perf: PerformanceTracker,
    settings: Settings,
    stop_event: threading.Event,
    signal_queue: queue.Queue,
) -> bool:
    """Run the intraday event loop until EOD or until stop_event is set.

    Returns True if the loop exited because EOD was reached, False if it was
    interrupted (e.g. Ctrl-C, fatal stream error).
    """
    report_interval_s = 300.0
    tick_interval_s = 1.0
    poll_s = 0.05
    next_report_at = time.monotonic() + report_interval_s
    next_tick_at = time.monotonic()

    while not stop_event.is_set():
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

        now_mono = time.monotonic()

        if now_mono >= next_tick_at:
            try:
                strategy.tick()
            except Exception as exc:
                logger.exception(f"Strategy tick error: {exc}")
            next_tick_at = now_mono + tick_interval_s

        if now_mono >= next_report_at:
            report_once(gw, perf)
            next_report_at = now_mono + report_interval_s

        if datetime.now(tz) >= eod_dt:
            logger.info(
                f"End-of-day reached ({settings.session_end_time} "
                f"{settings.session_timezone}) — closing positions…"
            )
            return True

        time.sleep(poll_s)

    return False


def _log_day_summary(
    day: date,
    perf: PerformanceTracker,
    intraday_pnl_before_close: float,
    exit_pnl: float,
    settings: Settings,
) -> None:
    cap = settings.daily_start_capital
    total = perf.gross_pnl
    logger.info("=" * 60)
    logger.info(f"DAY SUMMARY  {day}")
    logger.info(f"  In-session PnL (pre-close) : ${intraday_pnl_before_close:+,.2f}")
    logger.info(f"  EOD flatten PnL            : ${exit_pnl:+,.2f}")
    logger.info(f"  Total day PnL              : ${total:+,.2f}")
    logger.info(f"  Return on ${cap:,.0f}        : {total / cap:+.2%}" if cap else "")
    logger.info(f"  {perf.summary()}")
    logger.info("=" * 60)


def _log_overall_summary(
    session_id: str,
    days_completed: int,
    cumulative_pnl: float,
    cumulative_trades: int,
    cumulative_wins: int,
    cumulative_losses: int,
    settings: Settings,
    shutdown_reason: str,
) -> None:
    cap = settings.daily_start_capital
    win_rate = (cumulative_wins / cumulative_trades) if cumulative_trades else 0.0
    avg_daily = (cumulative_pnl / days_completed) if days_completed else 0.0
    logger.info("=" * 60)
    logger.info(f"RUN SUMMARY  [{session_id}]")
    logger.info(f"  Shutdown reason        : {shutdown_reason}")
    logger.info(f"  Trading days completed : {days_completed}")
    logger.info(f"  Cumulative PnL         : ${cumulative_pnl:+,.2f}")
    logger.info(f"  Average daily PnL      : ${avg_daily:+,.2f}")
    if cap:
        logger.info(f"  Cumulative return      : {cumulative_pnl / cap:+.2%} (on ${cap:,.0f}/day basis)")
    logger.info(
        f"  Cumulative trades      : {cumulative_trades}  "
        f"(W={cumulative_wins}  L={cumulative_losses}  Win%={win_rate:.1%})"
    )
    logger.info("=" * 60)


def main() -> None:
    run_id = _setup_logging()
    settings = load_settings()
    tz = _get_tz(settings)

    if settings.dry_run:
        broker_label = "DRY-RUN (simulated)"
    else:
        broker_label = f"LIVE ({settings.broker.upper()})"

    if settings.run_days == 0:
        days_label = "indefinite (Ctrl-C to stop)"
    elif settings.run_days == 1:
        days_label = "1 day (single-session mode)"
    else:
        days_label = f"{settings.run_days} day(s)"

    # Decide which trading day the startup logs belong to (today if we're
    # before today's EOD on a weekday, otherwise the next weekday) and open
    # that day's log file *before* the banner so it's captured.
    now = datetime.now(tz)
    _, first_eod = _compute_next_session_window(now, settings)
    current_log_day: date | None = first_eod.date()
    day_log_id: int | None = _open_day_log(current_log_day, run_id)

    logger.info("=" * 60)
    logger.info("  QuantumSignals Live Trader  ")
    logger.info(f"  Run ID        : {run_id}")
    logger.info(f"  Log directory : logs/  (one file per trading day)")
    logger.info(f"  Broker        : {broker_label}")
    logger.info(f"  Strategy      : {settings.strategy}")
    logger.info(f"  Exec interval : {settings.execution_interval_minutes} min")
    logger.info(
        f"  Session window: {settings.session_start_time} → "
        f"{settings.session_end_time} {settings.session_timezone}"
    )
    logger.info(f"  Run length    : {days_label}")
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
        _close_day_log(day_log_id)
        sys.exit(1)

    _log_model_catalog(settings)

    strategy_cls = _STRATEGIES.get(settings.strategy)
    if strategy_cls is None:
        logger.critical(
            f"Unknown strategy '{settings.strategy}'. "
            f"Choose from: {', '.join(_STRATEGIES)}"
        )
        _close_day_log(day_log_id)
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

    days_completed = 0
    cumulative_pnl = 0.0
    cumulative_trades = 0
    cumulative_wins = 0
    cumulative_losses = 0
    shutdown_reason = "manual (Ctrl-C)"

    logger.info("Bot running. Press Ctrl-C to stop.")
    try:
        while not stop_event.is_set():
            now = datetime.now(tz)
            session_start, eod_dt = _compute_next_session_window(now, settings)
            trading_day = eod_dt.date()

            # Rotate the log file when we cross into a new trading day so
            # overnight idle and the new day's activity land in the new file.
            if trading_day != current_log_day:
                _close_day_log(day_log_id)
                day_log_id = _open_day_log(trading_day, run_id)
                current_log_day = trading_day

            if now < session_start:
                logger.info(
                    f"Waiting for next session start at "
                    f"{session_start.strftime('%Y-%m-%d %H:%M %Z')} "
                    f"({_humanise_timedelta(session_start - now)} from now)…"
                )
                if not _sleep_until(session_start, stop_event, signal_queue):
                    break

            logger.info("=" * 60)
            logger.info(
                f"TRADING DAY  {trading_day}  "
                f"(session ends at {eod_dt.strftime('%H:%M %Z')})"
            )
            logger.info("=" * 60)

            reached_eod = _run_trading_day(
                eod_dt, tz, gw, strategy, perf, settings, stop_event, signal_queue
            )

            intraday_pnl = perf.gross_pnl
            exit_pnl = 0.0
            if settings.close_on_exit:
                exit_pnl = _close_all_positions(gw, settings, perf)

            _log_day_summary(trading_day, perf, intraday_pnl, exit_pnl, settings)

            cumulative_pnl += perf.gross_pnl
            cumulative_trades += perf.total_trades
            cumulative_wins += perf.winning_trades
            cumulative_losses += perf.losing_trades
            days_completed += 1

            if not reached_eod:
                shutdown_reason = "stream/loop interrupted"
                break

            perf.reset()
            strategy.reset_for_new_day()

            if settings.run_days and days_completed >= settings.run_days:
                shutdown_reason = f"completed configured RUN_DAYS={settings.run_days}"
                logger.info(shutdown_reason)
                break

    except KeyboardInterrupt:
        logger.info("Ctrl-C received — shutting down…")
        shutdown_reason = "manual (Ctrl-C)"
        if perf.gross_pnl != 0.0 or perf.total_trades > 0:
            intraday_pnl = perf.gross_pnl
            exit_pnl = 0.0
            if settings.close_on_exit:
                exit_pnl = _close_all_positions(gw, settings, perf)
            _log_day_summary(
                datetime.now(tz).date(), perf, intraday_pnl, exit_pnl, settings
            )
            cumulative_pnl += perf.gross_pnl
            cumulative_trades += perf.total_trades
            cumulative_wins += perf.winning_trades
            cumulative_losses += perf.losing_trades
            days_completed += 1
    finally:
        stop_event.set()
        _log_overall_summary(
            run_id,
            days_completed,
            cumulative_pnl,
            cumulative_trades,
            cumulative_wins,
            cumulative_losses,
            settings,
            shutdown_reason,
        )
        gw.disconnect()
        logger.info("Goodbye.")
        _close_day_log(day_log_id)
