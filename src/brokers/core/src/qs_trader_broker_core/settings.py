from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class SimulatedSettings(BaseSettings):
    """Environment-driven settings for the simulated broker (``SIM_*``).

    The simulated broker's symbol map and seed prices are dict overrides passed
    to the constructor, not env vars. This exists so the broker self-loads
    consistently with the others.
    """

    model_config = SettingsConfigDict(env_prefix="SIM_", extra="ignore")

    # Fractional per-call price jitter (± this fraction of the seed price) so
    # simulated trading exercises the PnL path. Set to 0.0 for deterministic prices —
    # tests use this so order sizing is reproducible.
    price_jitter: float = 0.001
