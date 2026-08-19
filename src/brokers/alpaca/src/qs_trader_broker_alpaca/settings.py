from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class AlpacaSettings(BaseSettings):
    """Alpaca broker configuration, loaded from ``ALPACA_*`` environment vars.

    Owned entirely by the Alpaca broker package — the host app neither defines
    nor passes these. Field ``api_key`` reads ``ALPACA_API_KEY``, etc.
    """

    model_config = SettingsConfigDict(env_prefix="ALPACA_", extra="ignore")

    api_key: str = ""
    secret_key: str = ""
    paper: bool = True
    use_market_orders: bool = True
