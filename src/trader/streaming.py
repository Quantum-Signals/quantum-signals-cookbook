from __future__ import annotations

import queue
import threading
import time

from loguru import logger
from quantumsignals.client import Client as QSClient, APIError, APIKeyError, APIVersionRemovedError

from trader.config import Settings


def stream_signals_thread(
    settings: Settings,
    stop_event: threading.Event,
    signal_queue: queue.Queue,
) -> None:
    """Read QuantumSignals SSE events and enqueue them for the main thread.

    This thread only performs I/O to the QS stream and enqueues ``signal``
    objects; the main thread drains ``signal_queue`` and runs the strategy. All
    broker calls stay on the main thread — a broker may require thread affinity
    (e.g. an SDK bound to the event loop of the thread that connected it).

    Retries with exponential backoff on transient errors.
    """
    retries = 0
    max_retries = settings.max_stream_retries
    maxsize = settings.signal_queue_maxsize if settings.signal_queue_maxsize > 0 else 0

    try:
        while not stop_event.is_set() and retries < max_retries:
            try:
                with QSClient(api_key=settings.qs_api_key) as qs:
                    logger.success("QuantumSignals stream connected")
                    retries = 0
                    for signal in qs.stream_signals():
                        if stop_event.is_set():
                            break
                        try:
                            if maxsize > 0:
                                signal_queue.put(signal, timeout=30.0)
                            else:
                                signal_queue.put(signal)
                        except queue.Full:
                            logger.error(
                                "Signal queue full — increase SIGNAL_QUEUE_MAXSIZE or speed up processing"
                            )
                            raise

            except APIKeyError as exc:
                logger.critical(f"Invalid QuantumSignals API key: {exc}")
                stop_event.set()
                return

            except APIVersionRemovedError as exc:
                logger.critical(
                    f"QS client is too old — API version removed (upgrade quantumsignals-client): {exc}"
                )
                stop_event.set()
                return

            except APIError as exc:
                retries += 1
                wait = 2**retries
                logger.warning(f"QS stream error ({exc}). Retry {retries}/{max_retries} in {wait}s…")
                time.sleep(wait)

            except Exception as exc:
                retries += 1
                wait = 2**retries
                logger.error(f"Unexpected error in QS stream: {exc}. Retry in {wait}s…")
                time.sleep(wait)

        if retries >= max_retries:
            logger.critical("Max retries reached — stopping signal stream")
            stop_event.set()
    finally:
        try:
            signal_queue.put_nowait(None)
        except queue.Full:
            pass
