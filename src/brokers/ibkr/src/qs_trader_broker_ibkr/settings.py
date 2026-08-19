from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class IBSettings(BaseSettings):
    """IBKR broker configuration, loaded from ``IB_*`` environment vars.

    Owned entirely by the IBKR broker package — the host app neither defines
    nor passes these. Field ``host`` reads ``IB_HOST``, ``port`` reads
    ``IB_PORT``, etc.
    """

    model_config = SettingsConfigDict(env_prefix="IB_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 1
    market_data_type: int = 4  # 1=live, 3=delayed, 4=delayed-frozen
    use_market_orders: bool = True
