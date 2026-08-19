from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class SimulatedSettings(BaseSettings):
    """Environment-driven settings for the simulated broker (``SIM_*``).

    The simulated broker needs almost nothing from the environment — its symbol
    map and seed prices are dict overrides passed to the constructor, not env
    vars. This exists so the broker self-loads consistently with the others.
    """

    model_config = SettingsConfigDict(env_prefix="SIM_", extra="ignore")
