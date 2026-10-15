from types import SimpleNamespace

import pytest

from qs_trader_broker_tmgm import TmgmBroker, TmgmSettings
from qs_trader_broker_tmgm.constants import (
    ORDER_FILLING_IOC,
    ORDER_TYPE_BUY,
    ORDER_TYPE_BUY_LIMIT,
    ORDER_TYPE_SELL,
    POSITION_TYPE_BUY,
    TRADE_ACTION_DEAL,
    TRADE_ACTION_PENDING,
    TRADE_RETCODE_DONE,
    TRADE_RETCODE_REQUOTE,
)


def _settings(**overrides) -> TmgmSettings:
    values = dict(
        login=0,
        password="",
        server="",
        terminal_path="",
        allow_live=False,
        deviation=100,
        magic=260007,
        use_market_orders=True,
    )
    values.update(overrides)
    return TmgmSettings(**values)


def _symbol(name="US500", *, volume_min=0.01, volume_step=0.01, filling_mode=2, trade_mode=4):
    return SimpleNamespace(
        name=name,
        volume_min=volume_min,
        volume_step=volume_step,
        filling_mode=filling_mode,
        trade_mode=trade_mode,
    )


class FakeMT5:
    def __init__(
        self,
        *,
        trade_mode=0,
        margin_mode=2,
        trade_allowed=True,
        server="TradeMaxGlobal-Demo",
    ):
        self.trade_mode = trade_mode
        self.margin_mode = margin_mode
        self.trade_allowed = trade_allowed
        self.server = server
        self.positions: list = []
        self.orders: list[dict] = []
        self.symbols = {"US500": _symbol()}
        self.quotes = {"US500": (7780.0, 7780.3)}
        self.initialized = False
        self.shutdown_calls = 0
        self.init_kwargs: dict = {}
        self._scripted: list = []

    def initialize(self, **kwargs):
        self.initialized = True
        self.init_kwargs = kwargs
        return True

    def shutdown(self):
        self.shutdown_calls += 1
        self.initialized = False

    def last_error(self):
        return (1, "Success")

    def account_info(self):
        return SimpleNamespace(
            login=60138941,
            server=self.server,
            trade_mode=self.trade_mode,
            margin_mode=self.margin_mode,
            currency="USD",
            balance=2_000_000.0,
            equity=2_000_000.0,
            profit=0.0,
            margin_free=1_900_000.0,
            leverage=500,
        )

    def terminal_info(self):
        return SimpleNamespace(trade_allowed=self.trade_allowed, connected=True)

    def symbol_info(self, name):
        return self.symbols.get(name)

    def symbol_select(self, name, enable):
        return name in self.symbols

    def symbol_info_tick(self, name):
        quote = self.quotes.get(name)
        if quote is None:
            return None
        return SimpleNamespace(bid=quote[0], ask=quote[1], last=0.0, time=1)

    def positions_get(self, symbol=None):
        rows = self.positions
        if symbol is not None:
            rows = [p for p in rows if p.symbol == symbol]
        return tuple(rows)

    def order_send(self, request):
        self.orders.append(dict(request))
        if self._scripted:
            return self._scripted.pop(0)
        return SimpleNamespace(
            retcode=TRADE_RETCODE_DONE,
            comment="Request executed",
            order=len(self.orders),
            deal=len(self.orders),
            volume=request["volume"],
            price=request["price"],
        )


def _position(ticket, *, volume=0.01, price_open=7780.0, time=1, pos_type=POSITION_TYPE_BUY):
    return SimpleNamespace(
        ticket=ticket,
        symbol="US500",
        type=pos_type,
        volume=volume,
        price_open=price_open,
        profit=-0.03,
        time=time,
        magic=260007,
    )


def _broker(client: FakeMT5, **settings) -> TmgmBroker:
    broker = TmgmBroker(_settings(**settings), client=client)
    broker.connect()
    return broker


def test_resolve_maps_qs_aliases_and_skips_unlisted_gas():
    broker = TmgmBroker(_settings())
    assert broker.resolve_contract("ES") == "US500"
    assert broker.resolve_contract("SPY") == "US500"
    assert broker.resolve_contract("S&P500") == "US500"
    assert broker.resolve_contract("NQ") == "NAS100"
    assert broker.resolve_contract("AAPL") == "AAPL.NAS"
    assert broker.resolve_contract("NG") is None
    assert broker.resolve_contract("Natural Gas") is None
    assert broker.resolve_contract("EURUSD") == "EURUSD"


def test_connect_refuses_live_account_unless_allowed():
    client = FakeMT5(trade_mode=2, server="TradeMaxGlobal-Live2")
    broker = TmgmBroker(_settings(), client=client)
    with pytest.raises(RuntimeError, match="live account"):
        broker.connect()
    assert client.shutdown_calls == 1

    allowed = FakeMT5(trade_mode=2, server="TradeMaxGlobal-Live2")
    TmgmBroker(_settings(allow_live=True), client=allowed).connect()
    assert allowed.initialized


def test_connect_refuses_when_algo_trading_is_off():
    client = FakeMT5(trade_allowed=False)
    broker = TmgmBroker(_settings(), client=client)
    with pytest.raises(RuntimeError, match="Algo Trading"):
        broker.connect()
    assert client.shutdown_calls == 1


def test_connect_passes_login_when_configured():
    client = FakeMT5()
    TmgmBroker(
        _settings(login=60138941, password="secret", server="TradeMaxGlobal-Demo"),
        client=client,
    ).connect()
    assert client.init_kwargs["login"] == 60138941
    assert client.init_kwargs["server"] == "TradeMaxGlobal-Demo"
    assert client.init_kwargs["password"] == "secret"


def test_last_price_is_the_bid_ask_mid():
    broker = _broker(FakeMT5())
    assert broker.get_last_price("US500") == pytest.approx(7780.15)


def test_open_buy_is_a_market_deal_without_a_position_ticket():
    client = FakeMT5()
    broker = _broker(client)
    result = broker.place_order("US500", "BUY", 0.01, 7780.0, position_intent="open")
    assert result is not None
    assert result.status == "filled"
    assert result.qty == pytest.approx(0.01)
    assert result.price == pytest.approx(7780.3)
    request = client.orders[0]
    assert request["action"] == TRADE_ACTION_DEAL
    assert request["type"] == ORDER_TYPE_BUY
    assert request["type_filling"] == ORDER_FILLING_IOC
    assert request["volume"] == pytest.approx(0.01)
    assert "position" not in request


def test_close_sell_targets_the_position_ticket():
    client = FakeMT5()
    client.positions.append(_position(592843210))
    broker = _broker(client)
    result = broker.place_order("US500", "SELL", 0.01, 7780.0, position_intent="close")
    assert result is not None
    assert result.price == pytest.approx(7780.0)
    request = client.orders[0]
    assert request["type"] == ORDER_TYPE_SELL
    assert request["position"] == 592843210
    assert request["comment"] == "qs-tmgm-close"


def test_unspecified_intent_closes_then_opens_the_remainder():
    client = FakeMT5()
    client.positions.append(_position(11, volume=0.01))
    broker = _broker(client)
    result = broker.place_order("US500", "SELL", 0.02, 7780.0)
    assert result is not None
    assert result.qty == pytest.approx(0.02)
    assert client.orders[0]["position"] == 11
    assert client.orders[0]["volume"] == pytest.approx(0.01)
    assert "position" not in client.orders[1]
    assert client.orders[1]["type"] == ORDER_TYPE_SELL
    assert client.orders[1]["volume"] == pytest.approx(0.01)


def test_close_walks_tickets_oldest_first():
    client = FakeMT5()
    client.positions.extend([_position(2, time=20), _position(1, time=10)])
    broker = _broker(client)
    broker.place_order("US500", "SELL", 0.02, position_intent="close")
    assert [order["position"] for order in client.orders] == [1, 2]


def test_below_minimum_lot_is_not_sent():
    client = FakeMT5()
    broker = _broker(client)
    assert broker.place_order("US500", "BUY", 0.004, position_intent="open") is None
    assert client.orders == []


def test_volume_is_floored_to_the_step():
    client = FakeMT5()
    broker = _broker(client)
    result = broker.place_order("US500", "BUY", 0.029, position_intent="open")
    assert result is not None
    assert result.qty == pytest.approx(0.02)
    assert client.orders[0]["volume"] == pytest.approx(0.02)


def test_requote_is_retried_once():
    client = FakeMT5()
    client._scripted = [
        SimpleNamespace(retcode=TRADE_RETCODE_REQUOTE, comment="Requote", order=0, deal=0, volume=0, price=0),
        SimpleNamespace(retcode=TRADE_RETCODE_DONE, comment="Request executed", order=9, deal=9, volume=0.01, price=7780.3),
    ]
    broker = _broker(client)
    result = broker.place_order("US500", "BUY", 0.01, position_intent="open")
    assert result is not None
    assert result.id == "9"
    assert len(client.orders) == 2


def test_limit_order_is_a_pending_request():
    client = FakeMT5()
    broker = _broker(client, use_market_orders=False)
    result = broker.place_order("US500", "BUY", 0.01, 7700.0, position_intent="open")
    assert result is not None
    assert client.orders[0]["action"] == TRADE_ACTION_PENDING
    assert client.orders[0]["type"] == ORDER_TYPE_BUY_LIMIT
    assert client.orders[0]["price"] == pytest.approx(7700.0)


def test_positions_aggregate_same_side_tickets():
    client = FakeMT5()
    client.positions.extend(
        [_position(1, volume=0.01, price_open=100.0), _position(2, volume=0.01, price_open=300.0)]
    )
    broker = _broker(client)
    synced = broker.get_positions_for_sync()
    assert len(synced) == 1
    assert synced[0]["symbol"] == "US500"
    assert synced[0]["side"] == "long"
    assert synced[0]["qty"] == pytest.approx(0.02)
    assert synced[0]["qty_available"] == pytest.approx(0.02)
    assert synced[0]["avg_price"] == pytest.approx(200.0)


def test_startup_warns_when_the_terminal_has_no_symbol():
    client = FakeMT5()
    broker = _broker(client)
    warnings = broker.startup_entitlement_warnings(["ES", "NG"])
    assert any("NG" in warning and "no tradable contract" in warning for warning in warnings)
    assert not any("US500" in warning for warning in warnings)

    client.symbols.clear()
    warnings = broker.startup_entitlement_warnings(["ES"])
    assert any("US500" in warning for warning in warnings)


def test_account_summary_uses_equity_and_free_margin():
    broker = _broker(FakeMT5())
    summary = broker.get_account_summary()
    assert summary["net_liquidation"] == pytest.approx(2_000_000.0)
    assert summary["buying_power"] == pytest.approx(1_900_000.0)
    assert summary["realized_pnl"] == 0.0
