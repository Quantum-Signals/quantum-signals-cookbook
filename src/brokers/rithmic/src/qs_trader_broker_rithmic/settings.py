from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class RithmicSettings(BaseSettings):
    """Rithmic broker configuration, loaded from ``RITHMIC_*`` environment vars.

    Stay on ``Rithmic Test`` until conformance passes. Paper / live URLs come
    from Rithmic after that review — do not point this at Rithmic 01 yet.
    """

    model_config = SettingsConfigDict(env_prefix="RITHMIC_", extra="ignore")

    user: str = ""
    password: str = ""
    system_name: str = "Rithmic Test"
    gateway_url: str = "wss://rituz00100.rithmic.com:443"
    # SampleOrder.py is already accepted on Test from the smoke-test login.
    app_name: str = "SampleOrder.py"
    app_version: str = "0.3.0.0"
    ssl_cert: str = ""
    fcm: str = ""
    ib: str = ""
    account: str = ""
    use_market_orders: bool = True
