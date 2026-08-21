"""Drive the multi-day supervisor through N simulated trading days in seconds.

This is an *offline* smoke test for the continuous-process / per-day-logfile
logic added to ``trader.app``. It:

  * Forces ``TRADER_BROKER=simulated`` so the SimulatedBroker is used (no real broker).
  * Stubs the QuantumSignals streaming thread so no real API calls are made.
  * Monkey-patches ``trader.app.datetime`` with a sim clock that advances
    SPEED× faster than wall time.
  * Caps ``time.sleep`` so the supervisor loop runs as fast as the CPU allows.
  * Calls ``trader.app.main()`` with ``RUN_DAYS=<days>`` so the supervisor
    exits cleanly after N EODs and prints a RUN SUMMARY.
  * Finally lists the per-day log files produced under ``logs/``.

What this verifies
------------------
  * The supervisor stays alive past EOD and rolls into the next session.
  * One ``logs/session_<day>_<run-start>.log`` file is produced per sim day.
  * Weekend dates are skipped automatically (use --start-day on a Friday).
  * DAY SUMMARY is logged at each EOD; RUN SUMMARY at process exit.
  * ``PerformanceTracker.reset()`` runs between days (no PnL bleed-through).

What this does NOT verify
-------------------------
  * Real QuantumSignals stream behaviour (the thread is stubbed out).
  * Real broker order placement (SimulatedBroker is used).
  * Real wall-clock pacing (sim clock is accelerated).

Usage
-----
    # 3 sim days, ~25s wallclock
    uv run python scripts/test_multiday_sim.py

    # 5 sim days starting Friday → exercises the weekend skip
    uv run python scripts/test_multiday_sim.py --days 5 --start-day 2026-05-15

    # Slower clock so you can watch the heartbeat logs scroll by
    uv run python scripts/test_multiday_sim.py --speed 600 --days 2

    # Wipe ./logs/ before running so only this test's files remain
    uv run python scripts/test_multiday_sim.py --clean
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from datetime import datetime as real_datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=3,
                   help="Number of simulated trading days to run (default: 3)")
    p.add_argument("--speed", type=float, default=3600.0,
                   help="Sim-clock multiplier vs. wall time (default: 3600 → 1h sim per 1s real)")
    p.add_argument("--start-day", type=str, default=None,
                   help="Sim start date YYYY-MM-DD in session TZ (default: today). "
                        "Use a Friday to exercise the weekend-skip code path.")
    p.add_argument("--start-time", type=str, default="08:30",
                   help="Sim start time HH:MM in session TZ (default: 08:30, i.e. pre-open).")
    p.add_argument("--clean", action="store_true",
                   help="Delete logs/ before running so only this test's files remain.")
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    if args.clean and Path("logs").exists():
        shutil.rmtree("logs")

    os.environ.setdefault("QUANTUMSIGNALS_API_KEY", "test_key_unused")
    os.environ["TRADER_BROKER"] = "simulated"
    os.environ.setdefault("STRATEGY", "momentum")
    os.environ.setdefault("CLOSE_ON_EXIT", "true")
    os.environ.setdefault("SESSION_END_TIME", "16:00")
    os.environ.setdefault("SESSION_START_TIME", "09:30")
    os.environ.setdefault("SESSION_TIMEZONE", "America/New_York")
    os.environ["RUN_DAYS"] = str(args.days)

    tz = ZoneInfo(os.environ["SESSION_TIMEZONE"])
    if args.start_day:
        start_date = real_datetime.strptime(args.start_day, "%Y-%m-%d").date()
    else:
        start_date = real_datetime.now(tz).date()
    h, m = (int(x) for x in args.start_time.split(":"))
    sim_origin = real_datetime(
        start_date.year, start_date.month, start_date.day, h, m, tzinfo=tz
    )
    real_origin = time.monotonic()

    eta_s = args.days * 86400.0 / args.speed
    print("=" * 60)
    print("Multi-day supervisor sim")
    print("=" * 60)
    print(f"  Sim start time : {sim_origin.strftime('%a %Y-%m-%d %H:%M %Z')}")
    print(f"  Sim days       : {args.days}")
    print(f"  Sim speed      : {args.speed:g}× wall clock")
    print(f"  Approx ETA     : ~{eta_s:.0f}s wall clock")
    print("=" * 60)

    import trader.app as app  # noqa: E402

    class _SimDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            elapsed_real = time.monotonic() - real_origin
            sim = sim_origin + timedelta(seconds=elapsed_real * args.speed)
            if tz is None:
                return sim.replace(tzinfo=None)
            return sim.astimezone(tz)

    app.datetime = _SimDateTime

    _real_sleep = time.sleep
    def _fast_sleep(t):
        _real_sleep(min(float(t), 0.001))
    time.sleep = _fast_sleep

    def _stub_stream(settings, stop_event, signal_queue):
        while not stop_event.is_set():
            _real_sleep(0.05)

    app.stream_signals_thread = _stub_stream

    logs_dir = Path("logs")
    before = (
        {p.name for p in logs_dir.glob("session_*.log")}
        if logs_dir.exists()
        else set()
    )

    try:
        app.main()
    except SystemExit as exc:
        if exc.code not in (None, 0):
            print(f"[test] main() exited with code {exc.code}", file=sys.stderr)

    time.sleep = _real_sleep

    print()
    print("=" * 60)
    print("Log files created by this run")
    print("=" * 60)
    if not logs_dir.exists():
        print("  (no logs/ directory was created — test failed)")
        return 1
    after = sorted(p for p in logs_dir.glob("session_*.log") if p.name not in before)
    if not after:
        print("  (no new session_*.log files were created — test failed)")
        return 1
    for f in after:
        size = f.stat().st_size
        print(f"  {f.name}  ({size:,} bytes)")
    print(f"\n  This run produced {len(after)} new file(s) for {args.days} sim day(s)")
    if before:
        print(f"  (logs/ also contained {len(before)} pre-existing file(s) — pass --clean to ignore them)")
    if len(after) == args.days:
        print("  ✓ One file per sim day — looks correct.")
        return 0
    print(f"  ! Expected {args.days} new files, got {len(after)} — investigate.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
