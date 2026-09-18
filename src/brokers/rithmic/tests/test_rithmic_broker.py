from datetime import date

from qs_trader_broker_rithmic.broker import (
    RithmicBroker,
    RithmicContract,
    _parse_instrument_position,
    _price_from_tick,
    _root_from_trading_symbol,
)
from qs_trader_broker_rithmic.front_month import cme_front_month_symbol
from qs_trader_broker_rithmic.settings import RithmicSettings


def _broker() -> RithmicBroker:
    return RithmicBroker(
        RithmicSettings(
            user="test",
            password="test",
            system_name="Rithmic Test",
            gateway_url="wss://example.invalid",
        )
    )


class TestFrontMonth:
    def test_august_2026_is_september_contract(self):
        assert cme_front_month_symbol("ES", date(2026, 8, 28)) == "ESU6"
        assert cme_front_month_symbol("NQ", date(2026, 8, 28)) == "NQU6"

    def test_after_september_expiry_rolls_to_december(self):
        # Third Friday of Sep 2026 is the 18th.
        assert cme_front_month_symbol("ES", date(2026, 9, 18)) == "ESU6"
        assert cme_front_month_symbol("ES", date(2026, 9, 19)) == "ESZ6"


class TestRootFromTradingSymbol:
    def test_esu6(self):
        assert _root_from_trading_symbol("ESU6") == "ES"

    def test_mesu6(self):
        assert _root_from_trading_symbol("MESU6") == "MES"

    def test_nqu26(self):
        assert _root_from_trading_symbol("NQU26") == "NQ"

    def test_equity_ticker(self):
        assert _root_from_trading_symbol("SPY") is None
        assert _root_from_trading_symbol("ES") is None


class TestResolveContract:
    def test_spy_maps_to_mes_root(self):
        broker = _broker()
        contract = broker.resolve_contract("SPY")
        assert contract is not None
        assert contract.root == "MES"
        assert contract.exchange == "CME"
        assert contract.trading_symbol.startswith("MES")
        assert contract.position_key == "MES"

    def test_es_and_spy_share_root(self):
        broker = _broker()
        es = broker.resolve_contract("ES")
        spy = broker.resolve_contract("SPY")
        assert es is not None and spy is not None
        assert es.root == spy.root == "MES"
        assert es.trading_symbol == spy.trading_symbol

    def test_front_month_cached_across_aliases(self):
        broker = _broker()
        first = broker.resolve_contract("SPY")
        assert first is not None
        assert broker._front_month_cache["MES"] == first.trading_symbol
        broker._client = object()  # would fail if lookup ran again
        second = broker.resolve_contract("ES")
        assert second is not None
        assert second.trading_symbol == first.trading_symbol

    def test_qqq_maps_to_mnq(self):
        broker = _broker()
        contract = broker.resolve_contract("QQQ")
        assert contract is not None
        assert contract.root == "MNQ"

    def test_direct_front_month_symbol(self):
        broker = _broker()
        contract = broker.resolve_contract("ESU6")
        assert contract is not None
        assert contract.trading_symbol == "ESU6"
        assert contract.root == "ES"

    def test_unknown_equity_skipped(self):
        assert _broker().resolve_contract("AAPL") is None


class TestPositionParse:
    def test_net_quantity_and_root(self):
        parsed = _parse_instrument_position(
            {
                "symbol": "ESU6",
                "product_code": "ES",
                "net_quantity": -1,
                "avg_open_fill_price": 7709.25,
                "open_position_pnl": -437.5,
            }
        )
        assert parsed is not None
        assert parsed["symbol"] == "ES"
        assert parsed["qty"] == -1.0
        assert parsed["side"] == "short"
        assert parsed["avg_price"] == 7709.25

    def test_flat_skipped(self):
        assert _parse_instrument_position({"symbol": "ESU6", "net_quantity": 0}) is None

    def test_open_position_quantity_when_net_zero(self):
        parsed = _parse_instrument_position(
            {
                "symbol": "ESU6",
                "product_code": "ES",
                "net_quantity": 0,
                "open_position_quantity": 2,
                "avg_open_fill_price": 7716.75,
            }
        )
        assert parsed is not None
        assert parsed["qty"] == 2.0

    def test_fill_qty_when_net_zero(self):
        parsed = _parse_instrument_position(
            {
                "symbol": "NQU6",
                "net_quantity": 0,
                "fill_buy_qty": 0,
                "fill_sell_qty": 1,
            }
        )
        assert parsed is not None
        assert parsed["symbol"] == "NQ"
        assert parsed["qty"] == -1.0


class TestPositionCache:
    def test_local_fill_visible_without_snapshot(self):
        broker = _broker()
        contract = RithmicContract("ESU6", "CME", "ES")
        broker._apply_local_fill(contract, "BUY", 1, 7716.75)
        rows = broker.get_positions_for_sync()
        assert len(rows) == 1
        assert rows[0]["symbol"] == "ES"
        assert rows[0]["qty"] == 1.0
        assert rows[0]["side"] == "long"

    def test_empty_snapshot_keeps_cached_fill(self):
        broker = _broker()
        contract = RithmicContract("ESU6", "CME", "ES")
        broker._apply_local_fill(contract, "BUY", 1, 7716.75)
        broker._refresh_position_snapshot()  # client is None → no-op
        rows = broker.get_positions_for_sync()
        assert rows[0]["qty"] == 1.0


class TestTickPrice:
    def test_last_trade(self):
        assert _price_from_tick({"trade_price": 7716.75}) == 7716.75

    def test_bbo_mid_uses_ask_not_offer(self):
        assert _price_from_tick({"bid_price": 10.0, "ask_price": 12.0}) == 11.0

    def test_single_sided_quote(self):
        assert _price_from_tick({"ask_price": 29351.5}) == 29351.5

    def test_empty_tick(self):
        assert _price_from_tick({"symbol": "ESU6"}) is None

    def test_on_tick_caches_symbol(self):
        broker = _broker()
        broker._on_tick({"symbol": "ESU6", "bid_price": 7716.0, "ask_price": 7716.5})
        assert broker._last_prices["ESU6"] == 7716.25


class TestCapabilities:
    def test_not_fractional(self):
        assert RithmicBroker.supports_fractional is False

    def test_contract_is_hashable(self):
        a = RithmicContract("ESU6", "CME", "ES")
        b = RithmicContract("ESU6", "CME", "ES")
        assert a == b
