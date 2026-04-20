from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


# Roll `lastTradeDateOrContractMonth` to the active front month or IB may fail/hang on qualify.
_DEFAULT_SYMBOL_MAP: dict[str, dict] = {
    "ES":   {"symbol": "ES",   "secType": "FUT", "exchange": "CME",   "currency": "USD", "lastTradeDateOrContractMonth": "202606"},
    "NQ":   {"symbol": "NQ",   "secType": "FUT", "exchange": "CME",   "currency": "USD", "lastTradeDateOrContractMonth": "202606"},
    "YM":   {"symbol": "YM",   "secType": "FUT", "exchange": "CBOT",  "currency": "USD", "lastTradeDateOrContractMonth": "202606"},
    "RTY":  {"symbol": "RTY",  "secType": "FUT", "exchange": "CME",   "currency": "USD", "lastTradeDateOrContractMonth": "202606"},
    "CL":   {"symbol": "CL",   "secType": "FUT", "exchange": "NYMEX", "currency": "USD", "lastTradeDateOrContractMonth": "202606"},
    "GC":   {"symbol": "GC",   "secType": "FUT", "exchange": "COMEX", "currency": "USD", "lastTradeDateOrContractMonth": "202606"},
    "MES":  {"symbol": "MES",  "secType": "FUT", "exchange": "CME",   "currency": "USD", "lastTradeDateOrContractMonth": "202606"},
    "MNQ":  {"symbol": "MNQ",  "secType": "FUT", "exchange": "CME",   "currency": "USD", "lastTradeDateOrContractMonth": "202606"},
    "AAPL": {"symbol": "AAPL", "secType": "STK", "exchange": "SMART", "currency": "USD"},
    "SPY":  {"symbol": "SPY",  "secType": "STK", "exchange": "SMART", "currency": "USD"},
    "QQQ":  {"symbol": "QQQ",  "secType": "STK", "exchange": "SMART", "currency": "USD"},
}

_DEFAULT_POSITION_SIZES: dict[str, int] = {
    "ES": 1, "NQ": 1, "YM": 1, "RTY": 1,
    "MES": 1, "MNQ": 1, "CL": 1, "GC": 1, "AAPL": 10, "SPY": 5, "QQQ": 5,
}

_DEFAULT_SIM_PRICES: dict[str, float] = {
    "ES": 5450.00, "NQ": 19200.00, "YM": 42500.00, "RTY": 2050.00,
    "MES": 5450.00, "MNQ": 19200.00,
    "CL": 68.50, "GC": 2350.00, "AAPL": 218.50, "SPY": 545.00, "QQQ": 470.00,
}

_DEFAULT_CONTRACT_MULTIPLIERS: dict[str, float] = {
    "ES": 50.0, "NQ": 20.0, "YM": 5.0, "RTY": 50.0,
    "MES": 5.0, "MNQ": 2.0,
    "CL": 1000.0, "GC": 100.0, "AAPL": 1.0, "SPY": 1.0, "QQQ": 1.0,
}


@dataclass(frozen=True)
class Settings:
    qs_api_key: str
    dry_run: bool = True
    ib_host: str = "127.0.0.1"
    ib_port: int = 4002
    ib_client_id: int = 1
    ib_market_data_type: int = 4  # 1=live, 3=delayed, 4=delayed-frozen
    trade_cooldown_seconds: int = 60
    use_market_orders: bool = True
    max_stream_retries: int = 5
    strategy: str = "momentum"  # "momentum" or "signal_follower"
    allowed_symbols: tuple[str, ...] = ()  # empty = allow all
    close_on_exit: bool = True
    daily_start_capital: float = 100_000.0
    signal_queue_maxsize: int = 0  # 0 = unlimited (Queue default)
    symbol_map: dict[str, dict] = field(default_factory=lambda: dict(_DEFAULT_SYMBOL_MAP))
    position_sizes: dict[str, int] = field(default_factory=lambda: dict(_DEFAULT_POSITION_SIZES))
    sim_prices: dict[str, float] = field(default_factory=lambda: dict(_DEFAULT_SIM_PRICES))
    contract_multipliers: dict[str, float] = field(default_factory=lambda: dict(_DEFAULT_CONTRACT_MULTIPLIERS))


def load_settings() -> Settings:
    """Load settings from environment variables (and .env file)."""
    load_dotenv()

    raw_symbols = os.getenv("ALLOWED_SYMBOLS", "").strip()
    allowed: tuple[str, ...] = ()
    if raw_symbols:
        allowed = tuple(s.strip().upper() for s in raw_symbols.split(",") if s.strip())

    return Settings(
        qs_api_key=os.environ["QUANTUMSIGNALS_API_KEY"],
        dry_run=os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes"),
        ib_host=os.getenv("IB_HOST", "127.0.0.1"),
        ib_port=int(os.getenv("IB_PORT", "4002")),
        ib_client_id=int(os.getenv("IB_CLIENT_ID", "1")),
        ib_market_data_type=int(os.getenv("IB_MARKET_DATA_TYPE", "4")),
        strategy=os.getenv("STRATEGY", "momentum").lower(),
        allowed_symbols=allowed,
        close_on_exit=os.getenv("CLOSE_ON_EXIT", "true").lower() in ("true", "1", "yes"),
        daily_start_capital=float(os.getenv("DAILY_START_CAPITAL", "100000")),
        signal_queue_maxsize=int(os.getenv("SIGNAL_QUEUE_MAXSIZE", "0")),
    )
