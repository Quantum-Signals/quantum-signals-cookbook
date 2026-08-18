from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field

from dotenv import load_dotenv


# Symbol → broker-native contract mapping now lives with each broker that owns
# it (each broker encodes symbols differently): see AlpacaBroker.DEFAULT_SYMBOL_MAP
# (QS → ETF ticker), IBBroker.DEFAULT_SYMBOL_MAP (QS → futures/stock spec), and
# SimulatedBroker.DEFAULT_SYMBOL_MAP / DEFAULT_PRICES. Settings.symbol_map and
# Settings.sim_prices below stay as optional overrides (empty = use the broker's
# own default). Contract multipliers are broker-neutral PnL math and stay here.

# Point-value per contract, used for end-of-day PnL in app.py. Futures carry a
# contract multiplier; equities/ETFs are 1:1.
_DEFAULT_CONTRACT_MULTIPLIERS: dict[str, float] = {
    "ES": 50.0, "NQ": 20.0, "YM": 5.0, "RTY": 50.0,
    "MES": 5.0, "MNQ": 2.0,
    "CL": 1000.0, "GC": 100.0, "AAPL": 1.0, "SPY": 1.0, "QQQ": 1.0,
}


@dataclass(frozen=True)
class Settings:
    qs_api_key: str
    dry_run: bool = True
    # Broker selection: "ibkr" (default) or "alpaca"
    broker: str = "alpaca"
    # IBKR connection settings
    ib_host: str = "127.0.0.1"
    ib_port: int = 4002
    ib_client_id: int = 1
    ib_market_data_type: int = 4  # 1=live, 3=delayed, 4=delayed-frozen
    # Alpaca credentials
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_paper: bool = True
    use_market_orders: bool = True
    max_stream_retries: int = 5
    strategy: str = "momentum"  # "momentum" or "signal_follower"
    allowed_symbols: tuple[str, ...] = ()  # empty = allow all
    close_on_exit: bool = True
    daily_start_capital: float = 1_000_000.0
    signal_queue_maxsize: int = 0  # 0 = unlimited (Queue default)
    execution_interval_minutes: int = 5
    # Session end: trader closes all positions and logs the daily summary at
    # this local time. Format: "HH:MM" (24-hour). Set to 15:55 NY so the EOD
    # flatten executes during regular-hours liquidity (5 min before the close)
    # instead of racing the 16:00 closing auction and getting post-close fills.
    session_end_time: str = "15:55"
    # Session start: when multi-day mode is enabled, the process sleeps until
    # this local time on the next trading day before resuming. Defaults to
    # 9:30 AM NY (cash market open).
    session_start_time: str = "09:30"
    session_timezone: str = "America/New_York"
    # Number of full trading days to run before exiting.
    #   0 = run indefinitely (until Ctrl-C) — recommended for "leave it on for a week".
    #   1 = single-day mode (legacy behaviour: exit after first EOD).
    #   N = exit after N EODs.
    run_days: int = 0
    # Optional overrides for a broker's own symbol map / sim prices.
    # Empty (the default) means "use the broker's DEFAULT_SYMBOL_MAP / DEFAULT_PRICES".
    symbol_map: dict[str, dict] = field(default_factory=dict)
    sim_prices: dict[str, float] = field(default_factory=dict)
    contract_multipliers: dict[str, float] = field(default_factory=lambda: dict(_DEFAULT_CONTRACT_MULTIPLIERS))


def _parse_cli_args() -> argparse.Namespace:
    """Parse our CLI flags. Skip entirely under Jupyter: kernels inject argv (e.g. -f …json)."""
    default = argparse.Namespace(execution_interval=None)
    argv0 = os.path.basename(sys.argv[0]).lower() if sys.argv else ""
    if (
        os.environ.get("JPY_PARENT_PID")
        or "ipykernel" in sys.modules
        or "ipykernel" in argv0
    ):
        return default

    parser = argparse.ArgumentParser(description="QuantumSignals Live Trader")
    parser.add_argument(
        "--execution-interval",
        type=int,
        default=None,
        metavar="MINUTES",
        help="Minutes between momentum execution decisions (default: 5)",
    )
    args, _unknown = parser.parse_known_args(args=sys.argv[1:])
    return args


def load_settings() -> Settings:
    """Load settings from environment variables, .env file, and CLI args."""
    load_dotenv()
    cli = _parse_cli_args()

    raw_symbols = os.getenv("ALLOWED_SYMBOLS", "").strip()
    allowed: tuple[str, ...] = ()
    if raw_symbols:
        allowed = tuple(s.strip() for s in raw_symbols.split(",") if s.strip())

    interval = cli.execution_interval or int(os.getenv("EXECUTION_INTERVAL_MINUTES", "5"))

    return Settings(
        qs_api_key=os.environ["QUANTUMSIGNALS_API_KEY"],
        dry_run=os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes"),
        broker=os.getenv("BROKER", "alpaca").lower(),
        ib_host=os.getenv("IB_HOST", "127.0.0.1"),
        ib_port=int(os.getenv("IB_PORT", "4002")),
        ib_client_id=int(os.getenv("IB_CLIENT_ID", "1")),
        ib_market_data_type=int(os.getenv("IB_MARKET_DATA_TYPE", "4")),
        alpaca_api_key=os.getenv("ALPACA_API_KEY", ""),
        alpaca_secret_key=os.getenv("ALPACA_SECRET_KEY", ""),
        alpaca_paper=os.getenv("ALPACA_PAPER", "true").lower() in ("true", "1", "yes"),
        strategy=os.getenv("STRATEGY", "momentum").lower(),
        allowed_symbols=allowed,
        close_on_exit=os.getenv("CLOSE_ON_EXIT", "true").lower() in ("true", "1", "yes"),
        daily_start_capital=float(os.getenv("DAILY_START_CAPITAL", "100000")),
        signal_queue_maxsize=int(os.getenv("SIGNAL_QUEUE_MAXSIZE", "0")),
        execution_interval_minutes=interval,
        session_end_time=os.getenv("SESSION_END_TIME", "15:55"),
        session_start_time=os.getenv("SESSION_START_TIME", "09:30"),
        session_timezone=os.getenv("SESSION_TIMEZONE", "America/New_York"),
        run_days=int(os.getenv("RUN_DAYS", "0")),
    )
