from trader.config import Settings
from trader.gateways.simulated import SimContract, SimulatedGateway


def _make_settings(**overrides) -> Settings:
    defaults = dict(qs_api_key="test-key", dry_run=True)
    defaults.update(overrides)
    return Settings(**defaults)


class TestSimulatedGateway:
    def test_connect_disconnect(self):
        gw = SimulatedGateway(_make_settings())
        gw.connect()
        gw.disconnect()

    def test_resolve_known_symbol(self):
        gw = SimulatedGateway(_make_settings())
        contract = gw.resolve_contract("ES")
        assert isinstance(contract, SimContract)
        assert contract.symbol == "ES"
        assert contract.sec_type == "FUT"
        assert contract.exchange == "CME"

    def test_resolve_unknown_symbol_auto_creates(self):
        gw = SimulatedGateway(_make_settings())
        contract = gw.resolve_contract("UNKNOWN")
        assert isinstance(contract, SimContract)
        assert contract.symbol == "UNKNOWN"
        assert contract.exchange == "SIM"

    def test_get_last_price_returns_float(self):
        gw = SimulatedGateway(_make_settings())
        contract = gw.resolve_contract("ES")
        price = gw.get_last_price(contract)
        assert isinstance(price, float)
        assert price > 0

    def test_get_last_price_unknown_symbol_defaults(self):
        gw = SimulatedGateway(_make_settings())
        contract = SimContract(symbol="XYZ", sec_type="FUT", exchange="SIM", currency="USD")
        price = gw.get_last_price(contract)
        assert isinstance(price, float)
        assert price > 0

    def test_place_buy_order(self):
        gw = SimulatedGateway(_make_settings())
        contract = gw.resolve_contract("ES")
        order = gw.place_order(contract, "BUY", 1, 5450.0)

        assert order is not None
        assert order["action"] == "BUY"
        assert order["qty"] == 1
        assert order["status"] == "FILLED"

        positions = gw.get_open_positions()
        assert len(positions) == 1
        assert positions[0]["symbol"] == "ES"
        assert positions[0]["pos"] == 1

    def test_place_sell_closes_position(self):
        gw = SimulatedGateway(_make_settings())
        contract = gw.resolve_contract("ES")
        gw.place_order(contract, "BUY", 1, 5450.0)
        gw.place_order(contract, "SELL", 1, 5460.0)

        positions = gw.get_open_positions()
        assert len(positions) == 0

    def test_account_summary(self):
        gw = SimulatedGateway(_make_settings())
        summary = gw.get_account_summary()
        assert "net_liquidation" in summary
        assert "buying_power" in summary
        assert summary["net_liquidation"] == 100_000.0
