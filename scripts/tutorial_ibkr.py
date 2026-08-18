"""
╔══════════════════════════════════════════════════════════════════════════════╗
║            QUANTUMSIGNALS x INTERACTIVE BROKERS — TRADING TUTORIAL          ║
╚══════════════════════════════════════════════════════════════════════════════╝

This script walks you through every step of the live-trading pipeline end-to-end,
from connecting to Interactive Brokers all the way to following QuantumSignals
and placing real (or simulated) orders.

Run it directly:

    uv run python scripts/tutorial.py

Or with DRY_RUN=false in your .env file to route orders through IB Gateway.

──────────────────────────────────────────────────────────────────────────────
PREREQUISITES
──────────────────────────────────────────────────────────────────────────────

1. IB Gateway (or TWS) running on localhost:
     - Download at https://www.interactivebrokers.com/en/trading/ibgateway.php
     - Log in with your IB paper-trading or live account credentials.
     - In Settings → API → Settings:
         ✓  Enable ActiveX and Socket Clients
         ✓  Socket port: 4002  (paper) / 4001 (live)
         ✓  Allow connections from localhost (127.0.0.1)

2. A QuantumSignals API key stored in .env:
     QUANTUMSIGNALS_API_KEY=your_key_here

3. Optional overrides in .env (sensible defaults shown):
     DRY_RUN=true          # set to false for real IB orders
     IB_HOST=127.0.0.1
     IB_PORT=4002          # 4002 = paper gateway, 7497 = TWS paper
     IB_CLIENT_ID=1
     IB_MARKET_DATA_TYPE=4 # 1=live, 3=delayed, 4=delayed-frozen (free)
     ALLOWED_SYMBOLS=ES,NQ # comma-separated; leave blank for all symbols
     EXECUTION_INTERVAL_MINUTES=5  # trading frequency in minutes (default: 5)

4. CLI override for trading frequency:
     uv run python scripts/tutorial.py --execution-interval 10
"""

from __future__ import annotations

# ──────────────────────────────────────────────────────────────────────────────
# STEP 0 — Standard library & third-party imports
# ──────────────────────────────────────────────────────────────────────────────

import os
import queue
import sys
import threading
import time

from dotenv import load_dotenv
from loguru import logger
from quantumsignals.client import Client as QSClient, APIError, APIKeyError

# ──────────────────────────────────────────────────────────────────────────────
# Add the src/ directory to sys.path so we can import the trader package
# without installing it in editable mode every time.
# ──────────────────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from trader.config import Settings, load_settings
from trader.gateways import create_gateway
from trader.models import PerformanceTracker, Signal


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — Configuration
# ══════════════════════════════════════════════════════════════════════════════
#
# All settings come from environment variables (loaded from .env by load_dotenv)
# and an optional --execution-interval CLI flag.
#
# Key variables:
#   QUANTUMSIGNALS_API_KEY  (required)
#   DRY_RUN                 true  → use SimulatedGateway (no real orders)
#                           false → use IBGateway (real orders via IB Gateway)
#   IB_HOST / IB_PORT / IB_CLIENT_ID
#   IB_MARKET_DATA_TYPE     4 = delayed-frozen (works without live data sub)
#   STRATEGY                "momentum" or "signal_follower"
#   ALLOWED_SYMBOLS         comma-separated list, e.g. "ES,NQ"
#   CLOSE_ON_EXIT           true → flatten all positions on Ctrl-C
#   DAILY_START_CAPITAL     used only for return-% calculation, default 100000
# ──────────────────────────────────────────────────────────────────────────────

def step1_load_config() -> Settings:
    banner("STEP 1 — Load configuration")

    load_dotenv()  # read .env into os.environ

    settings = load_settings()  # builds the frozen Settings dataclass

    log_step("Dry-run mode  : " + ("YES — orders go to SimulatedGateway" if settings.dry_run
                                   else "NO  — orders go to IB Gateway"))
    log_step(f"IB Gateway    : {settings.ib_host}:{settings.ib_port}  clientId={settings.ib_client_id}")
    log_step(f"Market data   : type {settings.ib_market_data_type}"
             " (4=delayed-frozen, 1=live)")
    log_step(f"Strategy      : {settings.strategy}")
    log_step(f"Exec interval : {settings.execution_interval_minutes} minutes")
    log_step(f"Allowed syms  : {', '.join(settings.allowed_symbols) or '(all)'}")

    return settings


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — Connect to Interactive Brokers
# ══════════════════════════════════════════════════════════════════════════════
#
# create_gateway() returns:
#   IBGateway         when DRY_RUN=false
#   SimulatedGateway  when DRY_RUN=true
#
# IBGateway.connect() does three things:
#   1. util.startLoop()   — starts ib_async's asyncio event loop on this thread.
#                           ALL subsequent IB API calls (qualify, place_order,
#                           get_last_price…) MUST run on this same thread.
#   2. self.ib.connect()  — TCP handshake with IB Gateway / TWS.
#   3. reqMarketDataType  — tells IB which market-data tier to use.
#
# IMPORTANT threading rule
# ─────────────────────────
# ib_async ties its asyncio loop to the thread that called connect().
# Never call IB API methods from background threads — they will deadlock or
# return empty results. Only the signal-queue thread (step 4) is safe to run
# in a background thread, because it never touches the IB API.
# ──────────────────────────────────────────────────────────────────────────────

def step2_connect_ibkr(settings: Settings):
    banner("STEP 2 — Connect to Interactive Brokers")

    gw = create_gateway(settings)  # IBGateway or SimulatedGateway

    log_step(f"Gateway type  : {type(gw).__name__}")

    try:
        gw.connect()
        log_step("IB Gateway connection successful")
    except Exception as exc:
        logger.critical(f"Cannot connect to IB Gateway: {exc}")
        logger.critical("Make sure IB Gateway or TWS is running and API connections are enabled.")
        sys.exit(1)

    # Quick account snapshot to confirm we are wired in.
    account = gw.get_account_summary()
    log_step(f"Net liquidation  : ${account['net_liquidation']:>12,.2f}")
    log_step(f"Buying power     : ${account['buying_power']:>12,.2f}")
    log_step(f"Unrealised PnL   : ${account['unrealized_pnl']:>12,.2f}")

    return gw


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — Connect to QuantumSignals and browse the model catalog
# ══════════════════════════════════════════════════════════════════════════════
#
# QSClient is a context-manager HTTP + SSE client.
# get_model_catalog() does a single REST request and returns a dict of
# {model_id: ModelInfo}, where ModelInfo has:
#   .trained_on_symbols   list[str]  — e.g. ["ES", "NQ"]
#   .trained_date_range   (str, str) — ISO date strings
#
# This is purely informational — it tells you which symbols each model
# was trained on so you can decide which ones to follow.
# ──────────────────────────────────────────────────────────────────────────────

def step3_explore_quantum_signals(settings: Settings) -> None:
    banner("STEP 3 — Connect to QuantumSignals & browse model catalog")

    try:
        with QSClient(api_key=settings.qs_api_key) as qs:
            log_step("QuantumSignals API connection successful")

            models = qs.get_model_catalog()
            log_step(f"Available models: {len(models)}")

            for model_id, model in models.items():
                symbols = ", ".join(model.trained_on_symbols)
                date_range = f"{model.trained_date_range[0]} → {model.trained_date_range[1]}"
                logger.info(f"  Model {model_id:>4s} | symbols: {symbols:<18s} | trained: {date_range}")

    except APIKeyError as exc:
        logger.critical(f"Invalid API key: {exc}")
        sys.exit(1)
    except Exception as exc:
        logger.warning(f"Could not fetch model catalog: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 — Stream QuantumSignals signals in a background thread
# ══════════════════════════════════════════════════════════════════════════════
#
# qs.stream_signals() is a blocking SSE (Server-Sent Events) iterator.
# Each yielded object has:
#   .symbol          str   — QS symbol, e.g. "ES", "NQ"
#   .signal          int   — 0=DOWN, 1=STABLE, 2=UP  (matches Signal enum)
#   .model           str   — which model produced this signal
#   .for_minute_utc  datetime — the 1-minute bar this signal is for
#   .is_null_signal() bool — True for keepalive / out-of-hours heartbeats
#   .code_message    str   — human-readable reason when is_null_signal()
#
# Because ib_async owns the main thread, we push signals onto a thread-safe
# queue.Queue and consume them on the main thread loop.
#
# Retry logic:
#   APIKeyError → stop immediately (bad key)
#   APIError    → exponential back-off, up to max_stream_retries attempts
#   other       → same back-off
# ──────────────────────────────────────────────────────────────────────────────

def _signal_producer(
    settings: Settings,
    stop_event: threading.Event,
    signal_queue: "queue.Queue[object | None]",
) -> None:
    """Background thread: streams QS signals into signal_queue."""
    retries = 0
    max_retries = settings.max_stream_retries

    try:
        while not stop_event.is_set() and retries < max_retries:
            try:
                with QSClient(api_key=settings.qs_api_key) as qs:
                    logger.success("QuantumSignals SSE stream connected")
                    retries = 0  # reset on successful connection

                    for signal in qs.stream_signals():
                        if stop_event.is_set():
                            break
                        signal_queue.put(signal)

            except APIKeyError as exc:
                logger.critical(f"Invalid QuantumSignals API key: {exc}")
                stop_event.set()
                return

            except (APIError, Exception) as exc:
                retries += 1
                wait = 2 ** retries
                logger.warning(f"QS stream error ({exc!r}) — retry {retries}/{max_retries} in {wait}s")
                time.sleep(wait)

        if retries >= max_retries:
            logger.critical("Max retries reached — signal stream stopped")
            stop_event.set()

    finally:
        signal_queue.put_nowait(None)  # sentinel: tell main loop to stop draining


def step4_start_signal_stream(
    settings: Settings,
    stop_event: threading.Event,
    signal_queue: "queue.Queue[object | None]",
) -> threading.Thread:
    banner("STEP 4 — Start QuantumSignals signal stream")

    thread = threading.Thread(
        target=_signal_producer,
        args=(settings, stop_event, signal_queue),
        name="qs-stream",
        daemon=True,  # thread is killed automatically when main exits
    )
    thread.start()
    log_step("Signal stream thread started (daemon=True)")
    log_step("Signals will arrive as SSE events and be placed on the queue")
    return thread


# ══════════════════════════════════════════════════════════════════════════════
# STEP 5 — Resolve and inspect contracts
# ══════════════════════════════════════════════════════════════════════════════
#
# Before placing any order IB needs a fully qualified Contract object.
# gw.resolve_contract(symbol) looks up the symbol in settings.symbol_map,
# constructs an ib_async.Contract, then calls ib.qualifyContracts() to fill
# in the conId (IB's unique contract identifier).
#
# Results are cached — the first call per symbol does a round-trip to IB;
# subsequent calls are instant.
#
# settings.symbol_map defaults support:
#   Futures : ES, NQ, YM, RTY, CL, GC, MES, MNQ  (CME/CBOT/NYMEX/COMEX)
#   Equities: AAPL, SPY, QQQ  (SMART exchange)
# ──────────────────────────────────────────────────────────────────────────────

def step5_resolve_contracts(gw, settings: Settings) -> dict:
    banner("STEP 5 — Resolve IB contracts")

    resolved = {}
    symbols_to_check = list(settings.allowed_symbols) or ["ES", "NQ"]

    for sym in symbols_to_check:
        log_step(f"Resolving {sym}…")
        contract = gw.resolve_contract(sym)
        if contract is not None:
            resolved[sym] = contract
            log_step(f"  {sym} → {contract}")
        else:
            logger.warning(f"  {sym} → could not qualify (check expiry month in config.py)")

    return resolved


# ══════════════════════════════════════════════════════════════════════════════
# STEP 6 — Inspect open positions (sync state with broker)
# ══════════════════════════════════════════════════════════════════════════════
#
# gw.get_positions_for_sync() returns a list of dicts:
#   {"symbol": str, "qty": int, "avg_price": float}
#
# Strategies call this at startup to either:
#   - Momentum: re-hydrate in-memory position state.
#   - SignalFollower: close any leftover positions from a previous session.
# ──────────────────────────────────────────────────────────────────────────────

def step6_inspect_positions(gw) -> None:
    banner("STEP 6 — Inspect open positions at broker")

    positions = gw.get_positions_for_sync()

    if not positions:
        log_step("No open positions — clean slate, ready to trade")
        return

    log_step(f"Found {len(positions)} open position(s):")
    for p in positions:
        label = "LONG " if p["qty"] > 0 else "SHORT"
        logger.info(f"  {label} {abs(p['qty']):>4d}x {p['symbol']:<8s} avg @ {p['avg_price']:.2f}")


# ══════════════════════════════════════════════════════════════════════════════
# STEP 7 — Follow signals: the main event loop
# ══════════════════════════════════════════════════════════════════════════════
#
# This is the heart of the bot.  The loop runs on the main thread (required
# by ib_async) and does three things on every iteration:
#
#   A. Drain the signal queue — consume every signal that the background
#      thread has pushed since the last poll, and act on each one.
#
#   B. Tick the strategy every second — the signal_follower strategy uses
#      tick() to detect the 3:55–4:00 PM close window even when signals
#      stop arriving.
#
#   C. Print a status report every 60 seconds.
#
# Signal handling (simplified — see strategy/signal_follower.py for full logic):
#   UP   signal → BUY  1 share of the mapped ETF  (ES→SPY, NQ→QQQ)
#   DOWN signal → SELL 1 share of the mapped ETF
#   STABLE / null → no order placed
#
# Order execution via gw.place_order(contract, action, qty, price=None):
#   - By default (use_market_orders=True) sends an IB MarketOrder.
#   - Polls up to 5 seconds for a fill, then returns the Trade object.
# ──────────────────────────────────────────────────────────────────────────────

# Map QS futures symbols to the ETF we actually trade (same as signal_follower)
_SIGNAL_TO_TRADE = {"ES": "SPY", "NQ": "QQQ"}

# Human-readable signal labels
_SIG_LABEL = {0: "DOWN  v", 1: "STABLE ->", 2: "UP    ^"}


def _handle_one_signal(signal: object, gw, resolved_contracts: dict, settings: Settings) -> None:
    """Demonstrate acting on a single QS signal."""
    sym: str = signal.symbol

    # Null signals are keepalives — ignore them
    if signal.is_null_signal():
        logger.debug(f"  [null] {sym}  model={signal.model}  reason={signal.code_message}")
        return

    # Skip symbols not in the allowed list (if a filter is set)
    if settings.allowed_symbols and sym not in settings.allowed_symbols:
        return

    sig_val: int = signal.signal
    label = _SIG_LABEL.get(sig_val, f"?({sig_val})")
    logger.info(f"Signal  {sym:<4s}  {label}  model={signal.model}  ts={signal.for_minute_utc}")

    if sig_val not in (Signal.UP, Signal.DOWN):
        return  # STABLE → no trade in this tutorial

    # Find the tradeable instrument for this signal
    trade_sym = _SIGNAL_TO_TRADE.get(sym, sym)
    contract = resolved_contracts.get(trade_sym)
    if contract is None:
        # Try to resolve on-the-fly (result is cached)
        contract = gw.resolve_contract(trade_sym)
        if contract is None:
            logger.warning(f"  No contract for {trade_sym} — skipping")
            return
        resolved_contracts[trade_sym] = contract

    action = "BUY" if sig_val == Signal.UP else "SELL"
    qty = 1

    logger.info(f"  -> Placing {action} {qty}x {trade_sym}")
    trade = gw.place_order(contract, action, qty)

    if trade is not None:
        # Fill price from IB Trade object or SimulatedGateway dict
        if isinstance(trade, dict):
            fill = trade.get("price")
        else:
            fill = getattr(getattr(trade, "orderStatus", None), "avgFillPrice", None)
        fill_str = f"@ {fill:.2f}" if fill else "(price pending)"
        logger.success(f"  -> Order complete: {action} {qty}x {trade_sym} {fill_str}")


def step7_run_event_loop(
    gw,
    settings: Settings,
    stop_event: threading.Event,
    signal_queue: "queue.Queue[object | None]",
    resolved_contracts: dict,
    perf: PerformanceTracker,
    max_signals: int = 5,  # tutorial limit — set to 0 for unlimited
) -> None:
    banner("STEP 7 — Main event loop: receiving and acting on signals")

    if max_signals > 0:
        log_step(f"Tutorial mode: will process up to {max_signals} actionable signals then stop")
        log_step("Change max_signals=0 in this script to run indefinitely")
    log_step("Press Ctrl-C at any time to stop gracefully")

    poll_s = 0.05          # 50 ms poll — keeps the IB event loop responsive
    tick_interval_s = 1.0
    report_interval_s = 60.0
    next_tick_at = time.monotonic()
    next_report_at = time.monotonic() + report_interval_s
    signals_acted_on = 0

    try:
        while not stop_event.is_set():

            # ── A. Drain the signal queue ────────────────────────────────────
            try:
                while True:
                    item = signal_queue.get_nowait()
                    if item is None:
                        # Sentinel from the producer thread (stream ended)
                        logger.warning("Signal stream ended — stopping loop")
                        stop_event.set()
                        break
                    try:
                        was_actionable = (
                            not item.is_null_signal()
                            and (not settings.allowed_symbols or item.symbol in settings.allowed_symbols)
                            and item.signal in (Signal.UP, Signal.DOWN)
                        )
                        _handle_one_signal(item, gw, resolved_contracts, settings)
                        if was_actionable:
                            signals_acted_on += 1
                    except Exception as exc:
                        logger.exception(f"Error handling signal: {exc}")

            except queue.Empty:
                pass

            # Tutorial exit condition
            if max_signals > 0 and signals_acted_on >= max_signals:
                log_step(f"Processed {signals_acted_on} actionable signals — tutorial complete")
                break

            # ── B. Periodic tick (strategy time-based logic) ─────────────────
            now = time.monotonic()
            if now >= next_tick_at:
                # In a full bot this is where strategy.tick() runs to catch
                # the 3:55 PM close window even when no signal arrives.
                next_tick_at = now + tick_interval_s

            # ── C. Periodic report ────────────────────────────────────────────
            if now >= next_report_at:
                _print_status(gw, perf)
                next_report_at = now + report_interval_s

            time.sleep(poll_s)

    except KeyboardInterrupt:
        log_step("Ctrl-C received — stopping")
    finally:
        stop_event.set()


def _print_status(gw, perf: PerformanceTracker) -> None:
    """Print a brief account + PnL snapshot."""
    logger.info("─" * 60)
    logger.info("STATUS REPORT")
    try:
        account = gw.get_account_summary()
        logger.info(f"  Net liquidation : ${account['net_liquidation']:>12,.2f}")
        logger.info(f"  Unrealised PnL  : ${account['unrealized_pnl']:>12,.2f}")
    except Exception:
        pass
    if perf.total_trades:
        logger.info(f"  {perf.summary()}")
    logger.info("─" * 60)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 8 — Close positions and disconnect
# ══════════════════════════════════════════════════════════════════════════════
#
# If CLOSE_ON_EXIT=true (default) the bot flattens all open positions before
# disconnecting so you never carry overnight exposure unintentionally.
#
# gw.get_positions_for_sync() → list of {"symbol", "qty", "avg_price"}
# gw.resolve_contract(sym)    → qualified Contract (cached)
# gw.place_order(contract, action, abs(qty))  → flatten trade
#
# Finally gw.disconnect() cleanly closes the IB TCP connection and stops
# the ib_async event loop.
# ──────────────────────────────────────────────────────────────────────────────

def step8_close_and_disconnect(
    gw,
    settings: Settings,
    perf: PerformanceTracker,
) -> None:
    banner("STEP 8 — Close positions & disconnect")

    if settings.close_on_exit:
        positions = gw.get_positions_for_sync()
        if positions:
            log_step(f"Closing {len(positions)} open position(s) before disconnect…")
            for p in positions:
                sym, qty = p["symbol"], int(p["qty"])
                if qty == 0:
                    continue
                contract = gw.resolve_contract(sym)
                if contract is None:
                    logger.error(f"  Cannot resolve {sym} — manual close required")
                    continue
                action = "SELL" if qty > 0 else "BUY"
                gw.place_order(contract, action, abs(qty))
                logger.info(f"  Closed {'LONG' if qty > 0 else 'SHORT'} {abs(qty)}x {sym}")
        else:
            log_step("No open positions — nothing to close")
    else:
        log_step("CLOSE_ON_EXIT=false — leaving positions open")

    # Final session summary
    cap = settings.daily_start_capital
    logger.info("=" * 60)
    logger.info("SESSION SUMMARY")
    logger.info(f"  Total PnL  : ${perf.gross_pnl:+,.2f}")
    logger.info(f"  Return on ${cap:,.0f}: {perf.gross_pnl / cap:+.2%}")
    if perf.total_trades:
        logger.info(f"  {perf.summary()}")
    logger.info("=" * 60)

    gw.disconnect()
    log_step("Disconnected from IB Gateway — goodbye!")


# ══════════════════════════════════════════════════════════════════════════════
# UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def banner(title: str) -> None:
    logger.info("")
    logger.info("╔" + "═" * 74 + "╗")
    logger.info("║  " + title.upper().ljust(72) + "║")
    logger.info("╚" + "═" * 74 + "╝")


def log_step(msg: str) -> None:
    logger.info("  ▸ " + msg)


def _setup_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
        level="DEBUG",
        colorize=True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# MAIN — wire all steps together
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    _setup_logging()

    logger.info("╔══════════════════════════════════════════════════════════════╗")
    logger.info("║  QuantumSignals x Interactive Brokers — Tutorial             ║")
    logger.info("╚══════════════════════════════════════════════════════════════╝")
    logger.info("Read the source of this file for detailed step-by-step explanations.")
    logger.info("")

    # ── Step 1: load config ───────────────────────────────────────────────────
    settings = step1_load_config()

    # ── Step 2: connect to IB ────────────────────────────────────────────────
    gw = step2_connect_ibkr(settings)

    # ── Step 3: explore QuantumSignals models ─────────────────────────────────
    step3_explore_quantum_signals(settings)

    # ── Step 4: start signal stream in background thread ──────────────────────
    stop_event = threading.Event()
    signal_queue: queue.Queue = queue.Queue()
    step4_start_signal_stream(settings, stop_event, signal_queue)

    # ── Step 5: pre-resolve contracts for the symbols we will trade ───────────
    resolved_contracts = step5_resolve_contracts(gw, settings)

    # ── Step 6: check existing broker positions ───────────────────────────────
    step6_inspect_positions(gw)

    # ── Step 7: run the main event loop ───────────────────────────────────────
    perf = PerformanceTracker()
    step7_run_event_loop(
        gw,
        settings,
        stop_event,
        signal_queue,
        resolved_contracts,
        perf,
        max_signals=5,   # process 5 actionable UP/DOWN signals then stop
                         # set to 0 to run indefinitely until Ctrl-C
    )

    # ── Step 8: close positions and disconnect ────────────────────────────────
    step8_close_and_disconnect(gw, settings, perf)


if __name__ == "__main__":
    main()
