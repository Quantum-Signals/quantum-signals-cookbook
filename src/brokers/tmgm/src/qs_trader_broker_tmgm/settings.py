from __future__ import annotations

from pydantic import field_validator
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

    @field_validator("login", mode="before")
    @classmethod
    def _blank_login_means_current_session(cls, value: object) -> object:
        # An empty TMGM_LOGIN in .env means "attach to the terminal already open".
        if value is None or (isinstance(value, str) and value.strip() == ""):
            return 0
        return value
    terminal_path: str = ""
    allow_live: bool = False
    deviation: int = 100
    magic: int = 260007
    use_market_orders: bool = True
