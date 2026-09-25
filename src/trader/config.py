from __future__ import annotations

import argparse
import os
import sys

from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


# QS symbol aliases and canonical names live in qs_trader_symbols.
# Each broker builds its tradable map from that catalog. Contract multipliers
# are broker-neutral PnL math (used by the app's EOD flatten) and stay here.

# Point-value per contract, used for end-of-day PnL in app.py. Futures carry a
# contract multiplier; equities/ETFs are 1:1.
_DEFAULT_CONTRACT_MULTIPLIERS: dict[str, float] = {
    "ES": 50.0, "NQ": 20.0, "YM": 5.0, "RTY": 50.0,
    "MES": 5.0, "MNQ": 2.0, "MYM": 0.5, "M2K": 5.0,
    "CL": 1000.0, "GC": 100.0, "SI": 5000.0, "NG": 10000.0,
    "AAPL": 1.0, "SPY": 1.0, "QQQ": 1.0,
    "DIA": 1.0, "IWM": 1.0, "USO": 1.0, "GLD": 1.0, "SLV": 1.0, "UNG": 1.0,
}


def _parse_symbol_capital(value: str | dict[str, float] | None) -> dict[str, float]:
    """Parse per-symbol capital allocation from env var.

    Accepts formats:
      - ``"RTY:1000000,YM:500000"`` (comma-separated pairs)
      - ``{"RTY": 1000000, "YM": 500000}`` (dict, e.g. from JSON)
      - ``None`` or empty → empty dict (use equal split from daily_start_capital)
    """
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return {k: float(v) for k, v in value.items()}
    result: dict[str, float] = {}
    for pair in str(value).split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            raise ValueError(
                f"Invalid symbol_capital format: '{pair}'. "
                f"Expected 'SYMBOL:AMOUNT' (e.g. 'RTY:1000000')"
            )
        sym, amt = pair.split(":", 1)
        result[sym.strip()] = float(amt.strip())
    return result


class Settings(BaseSettings):
    """App-level (orchestration) settings, loaded from ``TRADER_*`` env vars.

    Broker connection settings and credentials are **not** here — each broker
    package owns and self-loads its own config from its own namespaced env vars
    (e.g. ``ALPACA_*``). This object only decides *which* broker to run
    (``broker``) and drives the session/strategy layer.

    Env var names carry a ``TRADER_`` prefix (e.g. ``TRADER_BROKER``), except
    ``qs_api_key`` which reads the QuantumSignals client's own
    ``QUANTUMSIGNALS_API_KEY`` convention (unprefixed).
    """

    model_config = SettingsConfigDict(
        env_prefix="TRADER_",
        env_file=".env",
        extra="ignore",
        frozen=True,
        populate_by_name=True,
    )

    # QuantumSignals API key — unprefixed, matching the QS client library's
    # own env var. Required.
    qs_api_key: str = Field(validation_alias="QUANTUMSIGNALS_API_KEY")
    # Broker selection: "simulated" (default, no real orders). Set to
    # "alpaca" (or any other installed broker) to trade for real. The
    # broker's own connection settings live in its package (e.g. AlpacaSettings).
    broker: str = "simulated"
    max_stream_retries: int = 5
    strategy: str = "momentum"
    # Comma-separated in ``TRADER_ALLOWED_SYMBOLS``. Futures tickers (``GC``),
    # full QS names (``Gold``), or ETF tickers (``GLD``) all map to the same
    # — all equivalent forms map to the same instrument and route to the broker
    # (e.g. GC→GLD on Alpaca). Empty = allow all catalog symbols.
    allowed_symbols: Annotated[list[str], NoDecode] = []
    close_on_exit: bool = True
    daily_start_capital: float = 1_000_000.0
    # Per-symbol capital allocation. Format: ``"RTY:1000000,YM:500000"``.
    # If set, overrides equal-split of ``daily_start_capital``. Symbols not
    # listed here but present in ``allowed_symbols`` get
    # ``daily_start_capital / n_symbols`` as fallback.
    symbol_capital: Annotated[dict[str, float], NoDecode] = {}
    signal_queue_maxsize: int = 0  # 0 = unlimited (Queue default)
    execution_interval_minutes: int = 5
    # How often to print the account info panel. Independent of decision interval.
    #   0 or negative = print after each BUY/SELL action (default, recommended)
    #   positive N    = print every N minutes
    account_info_interval_minutes: int = 0
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
    # Broker-neutral PnL math; not env-driven.
    contract_multipliers: dict[str, float] = Field(
        default_factory=lambda: dict(_DEFAULT_CONTRACT_MULTIPLIERS),
        exclude=True,
    )

    @field_validator("broker", "strategy", mode="before")
    @classmethod
    def _lowercase(cls, v: object) -> object:
        return v.lower() if isinstance(v, str) else v

    @field_validator("allowed_symbols", mode="before")
    @classmethod
    def _split_symbols(cls, v: object) -> object:
        """Accept a comma-separated string (``"ES,NQ"``) or a list."""
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @field_validator("symbol_capital", mode="before")
    @classmethod
    def _parse_symbol_capital(cls, v: object) -> dict[str, float]:
        """Parse per-symbol capital: ``"RTY:1000000,YM:500000"`` or dict."""
        if v is None or v == "":
            return {}
        return _parse_symbol_capital(v)  # type: ignore[arg-type]

    @field_validator("execution_interval_minutes")
    @classmethod
    def _validate_interval(cls, v: int) -> int:
        """Execution interval must be positive to avoid division by zero."""
        if v <= 0:
            raise ValueError(
                f"execution_interval_minutes must be positive (got {v}). "
                "Use at least 1 minute."
            )
        return v

    @field_validator("daily_start_capital")
    @classmethod
    def _validate_capital(cls, v: float) -> float:
        """Daily start capital must be positive for meaningful order sizing."""
        if v <= 0:
            raise ValueError(
                f"daily_start_capital must be positive (got {v}). "
                "Set to your desired daily notional exposure."
            )
        return v


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
    """Load settings from ``TRADER_*`` env vars, the ``.env`` file, and CLI args.

    The ``--execution-interval`` CLI flag, when given, overrides
    ``TRADER_EXECUTION_INTERVAL_MINUTES`` — constructor args take precedence over
    env values in pydantic-settings.
    """
    cli = _parse_cli_args()
    overrides: dict[str, object] = {}
    if cli.execution_interval is not None:
        overrides["execution_interval_minutes"] = cli.execution_interval
    return Settings(**overrides)
