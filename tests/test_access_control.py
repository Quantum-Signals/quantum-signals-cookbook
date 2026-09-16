from unittest.mock import MagicMock, patch

from qs_trader_broker_core import SimulatedBroker, SimulatedSettings
from quantumsignals.client.models import SignalMetadata, Subscription

from trader.access_control import qs_entitlement_warnings, target_canonical_symbols
from trader.config import Settings


def _settings(**overrides) -> Settings:
    defaults = dict(qs_api_key="test-key")
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def test_target_canonical_symbols_deduplicates_aliases():
    s = _settings(allowed_symbols=["ES", "S&P500"])
    assert target_canonical_symbols(s) == ["ES"]


def test_target_accepts_etf_names_from_user():
    s = _settings(allowed_symbols=["SPY", "QQQ"])
    assert target_canonical_symbols(s) == ["ES", "NQ"]


@patch("trader.access_control.QSClient")
def test_qs_warns_when_not_subscribed_and_not_in_catalog(mock_client_cls):
    mock_qs = MagicMock()
    mock_client_cls.return_value.__enter__.return_value = mock_qs
    mock_qs.get_signal_catalog.return_value = {}
    mock_qs.get_subscriptions.return_value = []

    warnings = qs_entitlement_warnings(_settings(), ["ES"])

    assert len(warnings) == 1
    assert "ES" in warnings[0]
    assert "no active subscription" in warnings[0]


@patch("trader.access_control.QSClient")
def test_qs_ok_when_subscribed(mock_client_cls):
    mock_qs = MagicMock()
    mock_client_cls.return_value.__enter__.return_value = mock_qs
    mock_qs.get_signal_catalog.return_value = {
        "5min_sp500": SignalMetadata(
            uuid="u1",
            retired=False,
            model_output_symbol="S&P500",
        ),
    }
    mock_qs.get_subscriptions.return_value = [
        Subscription(signal_id="5min_sp500", uuid="u1", tier="pro"),
    ]

    warnings = qs_entitlement_warnings(_settings(), ["ES"])

    assert warnings == []


@patch("trader.access_control.QSClient")
def test_qs_warns_catalog_only_no_subscription(mock_client_cls):
    mock_qs = MagicMock()
    mock_client_cls.return_value.__enter__.return_value = mock_qs
    mock_qs.get_signal_catalog.return_value = {
        "5min_nq": SignalMetadata(
            uuid="u2",
            retired=False,
            model_output_symbol="NQ",
        ),
    }
    mock_qs.get_subscriptions.return_value = []

    warnings = qs_entitlement_warnings(_settings(), ["NQ"])

    assert len(warnings) == 1
    assert "in signal catalog but no active commerce subscription" in warnings[0]


@patch("trader.access_control.QSClient")
def test_qs_warns_subscribed_but_missing_catalog_metadata(mock_client_cls):
    mock_qs = MagicMock()
    mock_client_cls.return_value.__enter__.return_value = mock_qs
    mock_qs.get_signal_catalog.return_value = {}
    mock_qs.get_subscriptions.return_value = [
        Subscription(signal_id="5min_nq", uuid="u2", tier="pro"),
    ]

    warnings = qs_entitlement_warnings(_settings(), ["NQ"])

    assert len(warnings) == 1
    assert "subscribed but symbol metadata missing from catalog" in warnings[0]


def test_simulated_broker_no_startup_warnings():
    broker = SimulatedBroker(SimulatedSettings())
    broker.connect()
    assert broker.startup_entitlement_warnings(["ES", "NQ"]) == []
