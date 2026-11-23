from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class IronbeamSettings(BaseSettings):
    """Ironbeam configuration, loaded from ``IRONBEAM_*`` environment variables.

    Owned entirely by this package. ``api_key`` is always required.
    ``username`` (the trader id) and ``password`` are only required for
    tenant-owner keys; a standard key ignores the password and only checks
    that ``username``, when sent, matches the key's owner.

    Live and simulated trading are separate Ironbeam deployments with their own
    keys and hosts. ``base_url`` must be the host named for your key's
    environment in the onboarding material.
    """

    model_config = SettingsConfigDict(env_prefix="IRONBEAM_", extra="ignore")

    api_key: str = ""
    username: str = ""
    password: str = ""
    # Account to trade. Empty = the single account under the login (or the
    # login itself when it is an account). Required when the login sees several.
    account_id: str = ""
    # Host only, no /v3 suffix: the broker adds the version prefix itself.
    base_url: str = "https://api.ironbeamapi.com"
    # Trade micro contracts (MES, MNQ, …) instead of full-size ones (ES, NQ, …).
    use_micros: bool = True
    use_market_orders: bool = True
    # Skip a contract once it is this close to expiry and use the next one.
    roll_days: int = Field(default=5, ge=0)
    # How long place_order polls GET /order/{id} for a fill before returning.
    fill_timeout_s: float = Field(default=10.0, ge=0)
    http_timeout_s: float = Field(default=15.0, gt=0)
    # Log account, margin and order book checks at startup and around each order
    # (read-only; adds two to four quick reads before an order).
    diagnostics: bool = True
