from trader.config import Settings
from trader.brokers.simulated import SimContract, SimulatedBroker


def _make_settings(**overrides) -> Settings:
    defaults = dict(qs_api_key="test-key", dry_run=True)
    defaults.update(overrides)
    return Settings(**defaults)


class TestSimulatedBroker:
    def test_connect_disconnect(self):
        broker = SimulatedBroker(_make_settings())
        broker.connect()
        broker.disconnect()

    def test_resolve_known_symbol(self):
        broker = SimulatedBroker(_make_settings())
        contract = broker.resolve_contract("ES")
        assert isinstance(contract, SimContract)
        assert contract.symbol == "ES"
        assert contract.sec_type == "FUT"
        assert contract.exchange == "CME"

    def test_resolve_unknown_symbol_auto_creates(self):
        broker = SimulatedBroker(_make_settings())
        contract = broker.resolve_contract("UNKNOWN")
        assert isinstance(contract, SimContract)
        assert contract.symbol == "UNKNOWN"
        assert contract.exchange == "SIM"

    def test_get_last_price_returns_float(self):
        broker = SimulatedBroker(_make_settings())
        contract = broker.resolve_contract("ES")
        price = broker.get_last_price(contract)
        assert isinstance(price, float)
        assert price > 0

    def test_get_last_price_unknown_symbol_defaults(self):
        broker = SimulatedBroker(_make_settings())
        contract = SimContract(symbol="XYZ", sec_type="FUT", exchange="SIM", currency="USD")
        price = broker.get_last_price(contract)
        assert isinstance(price, float)
        assert price > 0

    def test_place_buy_order(self):
        broker = SimulatedBroker(_make_settings())
        contract = broker.resolve_contract("ES")
        order = broker.place_order(contract, "BUY", 1, 5450.0)

        assert order is not None
        assert order.action == "BUY"
        assert order.qty == 1
        assert order.status == "FILLED"

        positions = broker.get_open_positions()
        assert len(positions) == 1
        assert positions[0]["symbol"] == "ES"
        assert positions[0]["pos"] == 1

    def test_place_sell_closes_position(self):
        broker = SimulatedBroker(_make_settings())
        contract = broker.resolve_contract("ES")
        broker.place_order(contract, "BUY", 1, 5450.0)
        broker.place_order(contract, "SELL", 1, 5460.0)

        positions = broker.get_open_positions()
        assert len(positions) == 0

    def test_account_summary(self):
        broker = SimulatedBroker(_make_settings())
        summary = broker.get_account_summary()
        assert "net_liquidation" in summary
        assert "buying_power" in summary
        assert summary["net_liquidation"] == 100_000.0
