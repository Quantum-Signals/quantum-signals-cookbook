from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class TmgmSettings(BaseSettings):
    """TMGM / MetaTrader 5 configuration, loaded from ``TMGM_*`` environment vars.

    Owned entirely by this package. An empty ``login`` attaches to the MetaTrader 5
    terminal that is already running and logged in. ``allow_live`` defaults off so
    a real-money account is refused.
    """

    model_config = SettingsConfigDict(env_prefix="TMGM_", extra="ignore")

    login: int = 0
    password: str = ""
    server: str = ""
    terminal_path: str = ""
    allow_live: bool = False
    deviation: int = 100
    magic: int = 260007
    use_market_orders: bool = True
