from __future__ import annotations

from datetime import date, datetime

import pytest

from qs_trader_broker_ironbeam import IronbeamBroker, IronbeamContract, IronbeamSettings
from qs_trader_broker_ironbeam.broker import candidate_symbols, root_of

BASE = "https://ib.test"
# 2026-11-18 12:00 local; MES.Z26 expires 2026-12-18.
NOW = datetime(2026, 11, 18, 12, 0).timestamp()
DAY_MS = 86_400_000


def _settings(**overrides) -> IronbeamSettings:
    values = dict(
        api_key="key",
        username="TRADER1",
        password="pw",
        account_id="",
        base_url=BASE,
        use_micros=True,
        use_market_orders=True,
        roll_days=5,
        fill_timeout_s=2.0,
        http_timeout_s=5.0,
    )
    values.update(overrides)
    return IronbeamSettings(**values)


class FakeResponse:
    def __init__(self, status: int, body: object, headers: dict | None = None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = str(body)

    def json(self):
        return self._body


class FakeIronbeam:
    """Minimal stand-in for the Ironbeam v3 REST API."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None, dict | None]] = []
        self.accounts = ["ACC1"]
        self.positions: list[dict] = []
        self.quotes = {"XCME:MES.Z26": {"exchSym": "XCME:MES.Z26", "last": 6800.25, "bid": 6800.0, "ask": 6800.5}}
        self.expiries = {
            "XCME:MES.Z26": datetime(2026, 12, 18, 9, 30).timestamp() * 1000,
            "XCME:MES.H27": datetime(2027, 3, 19, 9, 30).timestamp() * 1000,
            "XCME:MNQ.Z26": datetime(2026, 12, 18, 9, 30).timestamp() * 1000,
        }
        self.order_status = ["NEW", "FILLED"]
        self.place_response: FakeResponse | None = None
        self.next_status: list[FakeResponse] = []
        self.placed: list[dict] = []
        self.logins = 0

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        assert url.startswith(f"{BASE}/v3/"), url
        path = url[len(BASE) + 3 :]
        self.calls.append((method, path, params, json))
        if self.next_status:
            return self.next_status.pop(0)
        if path != "/auth/login":
            assert headers["Authorization"].startswith("Bearer tok"), headers
        handler = getattr(self, f"_{method.lower()}_{path.strip('/').split('/')[0]}_{path.strip('/').split('/')[1] if '/' in path.strip('/') else ''}", None)
        if path.startswith("/order/") and method == "GET":
            return self._get_order(path.rsplit("/", 1)[-1])
        assert handler is not None, (method, path)
        return handler(params or {}, json)

    def _post_auth_login(self, params, body):
        self.logins += 1
        assert body["apikey"] == "key"
        return FakeResponse(200, {"status": "OK", "token": f"tok{self.logins}"})

    def _get_reference_userInfo(self, params, body):
        return FakeResponse(200, {"status": "OK", "traderId": "TRADER1", "accounts": self.accounts})

    def _get_account_balances(self, params, body):
        return FakeResponse(
            200,
            {
                "status": "OK",
                "balances": [
                    {
                        "accountId": params["accountId"],
                        "currencyCode": "USD",
                        "cashBalanceAvailable": 50_000.0,
                        "openTradeEquity": -125.0,
                        "netLiquidity": 49_875.0,
                        "netLiquidityAvailable": 49_875.0,
                        "marginInfo": {"marginOW": {"initialTotalMargin": 2_500.0}},
                    }
                ],
            },
        )

    def _get_account_positions(self, params, body):
        assert params["consolidate"] == "true"
        return FakeResponse(200, {"status": "OK", "accountId": params["accountId"], "positions": self.positions})

    def _get_reference_securityDefinitions(self, params, body):
        defs = []
        for bare in params["symbols"].split(","):
            exch_sym = f"XCME:{bare}"
            if exch_sym in self.expiries:
                defs.append(
                    {
                        "exchSym": exch_sym,
                        "securityType": "FUT",
                        "marketGroup": root_of(bare),
                        "maturityYear": 2000 + int(bare[-2:]),
                        "maturityMonth": "FGHJKMNQUVXZ".index(bare.split(".")[1][0]) + 1,
                        "expirationTime": self.expiries[exch_sym],
                    }
                )
        return FakeResponse(200, {"status": "OK", "securityDefinitions": defs})

    def _get_market_quotes(self, params, body):
        q = self.quotes.get(params["symbols"])
        return FakeResponse(200, {"status": "OK", "quotes": [q] if q else []})

    def _post_order_place(self, params, body):
        self.placed.append(body)
        if self.place_response is not None:
            return self.place_response
        return FakeResponse(201, {"status": "OK", "result": "ERROR_NONE", "orderId": "987"})

    def _get_order(self, order_id):
        status = self.order_status.pop(0) if len(self.order_status) > 1 else self.order_status[0]
        if status == "404":
            return FakeResponse(404, {"status": "ERROR", "message": "Order not found"})
        leg = self.placed[-1]["legs"][0]
        filled = leg["quantity"] if status == "FILLED" else 0
        out = {
            "role": "MAIN",
            "exchSym": leg["exchSym"],
            "side": leg["side"],
            "quantity": float(leg["quantity"]),
            "status": status,
            "filledQuantity": filled,
        }
        if filled:
            out["avgFillPrice"] = 6800.5
        if status == "REJECTED":
            out["statusMsg"] = "Insufficient margin"
        return FakeResponse(200, {"status": "OK", "order": {"orderId": order_id, "legs": [out]}})


class Clock:
    def __init__(self, t: float = NOW):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


@pytest.fixture
def api():
    return FakeIronbeam()


def _broker(api, **overrides) -> IronbeamBroker:
    clock = Clock()
    broker = IronbeamBroker(_settings(**overrides), session=api, clock=clock, sleep=clock.sleep)
    broker.connect()
    return broker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_root_of_strips_exchange_and_month():
    assert root_of("XCME:MES.Z26") == "MES"
    assert root_of("MNQ.H27") == "MNQ"


def test_candidate_symbols_wrap_the_year():
    out = candidate_symbols("MES", date(2026, 11, 1), months=3)
    assert out == ["MES.X26", "MES.Z26", "MES.F27"]


# ---------------------------------------------------------------------------
# Connect / auth
# ---------------------------------------------------------------------------


def test_connect_logs_in_with_trader_credentials_and_picks_single_account(api):
    broker = _broker(api)
    login = api.calls[0]
    assert login[:2] == ("POST", "/auth/login")
    assert login[3] == {"apikey": "key", "username": "TRADER1", "password": "pw"}
    assert broker._account_id == "ACC1"


def test_connect_requires_account_id_when_login_sees_several(api):
    api.accounts = ["ACC1", "ACC2"]
    with pytest.raises(RuntimeError, match="IRONBEAM_ACCOUNT_ID"):
        _broker(api)
    broker = _broker(api, account_id="ACC2")
    assert broker._account_id == "ACC2"


def test_connect_rejects_account_not_on_login(api):
    with pytest.raises(RuntimeError, match="not one of"):
        _broker(api, account_id="OTHER")


def test_base_url_with_v3_suffix_is_not_doubled(api):
    broker = _broker(api, base_url=f"{BASE}/v3/")
    assert broker._base == BASE


def test_expired_token_logs_in_again_and_retries(api):
    broker = _broker(api)
    api.next_status = [FakeResponse(401, {"status": "ERROR", "message": "expired"})]
    summary = broker.get_account_summary()
    assert api.logins == 2
    assert summary["net_liquidation"] == 49_875.0


def test_refreshed_token_header_is_adopted(api):
    broker = _broker(api)
    api.next_status = [
        FakeResponse(200, {"status": "OK", "quotes": []}, headers={"X-Auth-Token": "tok-fresh"})
    ]
    broker.get_last_price(IronbeamContract("XCME:MES.Z26", "MES"))
    assert broker._token == "tok-fresh"


def test_rate_limit_waits_retry_after_once(api):
    broker = _broker(api)
    api.next_status = [FakeResponse(429, {"status": "ERROR", "message": "slow"}, headers={"Retry-After": "2"})]
    assert broker.get_last_price(IronbeamContract("XCME:MES.Z26", "MES")) == 6800.25


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


def test_resolve_maps_qs_aliases_to_micro_front_month(api):
    broker = _broker(api)
    for alias in ("ES", "SPY", "S&P500"):
        c = broker.resolve_contract(alias)
        assert c == IronbeamContract("XCME:MES.Z26", "MES")
    # Cached: one lookup for the root, not one per alias.
    lookups = [c for c in api.calls if c[1] == "/reference/securityDefinitions"]
    assert len(lookups) == 2  # 13 candidate months → two batches of ≤10


def test_resolve_full_size_roots_when_micros_off(api):
    api.expiries["XCME:ES.Z26"] = api.expiries["XCME:MES.Z26"]
    broker = _broker(api, use_micros=False)
    assert broker.resolve_contract("ES") == IronbeamContract("XCME:ES.Z26", "ES")


def test_resolve_rolls_inside_roll_window(api):
    broker = _broker(api, roll_days=40)
    assert broker.resolve_contract("ES") == IronbeamContract("XCME:MES.H27", "MES")


def test_resolve_skips_non_futures_and_passes_explicit_contracts(api):
    broker = _broker(api)
    assert broker.resolve_contract("AAPL") is None
    assert broker.resolve_contract("XCME:ES.H27") == IronbeamContract("XCME:ES.H27", "ES")
    assert broker.resolve_contract("TSLA") is None


def test_resolve_prefers_held_contract(api):
    api.positions = [{"exchSym": "XCME:MES.H27", "quantity": 2.0, "side": "LONG", "price": 6900.0}]
    broker = _broker(api)
    broker.get_positions_for_sync()
    assert broker.resolve_contract("ES").exch_sym == "XCME:MES.H27"
    # The EOD flatten resolves the position symbol itself.
    assert broker.resolve_contract("MES").exch_sym == "XCME:MES.H27"


def test_startup_warnings_name_unsupported_symbols(api):
    broker = _broker(api)
    warnings = broker.startup_entitlement_warnings(["ES", "AAPL", "GC"])
    assert any("AAPL" in w and "not a futures" in w for w in warnings)
    assert any(w.startswith("GC:") and "MGC" in w for w in warnings)
    assert not any(w.startswith("ES") for w in warnings)


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


def test_last_price_falls_back_to_mid(api):
    api.quotes["XCME:MES.Z26"] = {"exchSym": "XCME:MES.Z26", "bid": 10.0, "ask": 12.0}
    broker = _broker(api)
    assert broker.get_last_price(IronbeamContract("XCME:MES.Z26", "MES")) == 11.0


def test_last_price_none_when_no_quote(api):
    broker = _broker(api)
    assert broker.get_last_price(IronbeamContract("XCME:MNQ.Z26", "MNQ")) is None


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


def test_market_order_body_and_fill(api):
    broker = _broker(api)
    contract = IronbeamContract("XCME:MES.Z26", "MES")
    result = broker.place_order(contract, "buy", 2.0, price=6800.0, position_intent="open")
    body = api.placed[-1]
    assert body["orderType"] == "FAST_ORDER"
    assert body["clOrdId"].startswith("qs-")
    assert body["legs"] == [
        {
            "role": "MAIN",
            "accountId": "ACC1",
            "exchSym": "XCME:MES.Z26",
            "side": "BUY",
            "quantity": 2,
            "kind": "MARKET",
            "duration": "DAY",
        }
    ]
    assert result is not None
    assert (result.id, result.qty, result.price, result.status) == ("987", 2.0, 6800.5, "filled")


def test_limit_order_when_market_orders_off(api):
    broker = _broker(api, use_market_orders=False)
    broker.place_order(IronbeamContract("XCME:MES.Z26", "MES"), "SELL", 1, price=6801.25)
    leg = api.placed[-1]["legs"][0]
    assert leg["kind"] == "LIMIT" and leg["limitPrice"] == 6801.25


def test_order_404_right_after_place_is_polled_again(api):
    api.order_status = ["404", "NEW", "FILLED"]
    broker = _broker(api)
    result = broker.place_order(IronbeamContract("XCME:MES.Z26", "MES"), "SELL", 1)
    assert result is not None and result.status == "filled"


def test_rejected_order_returns_none(api):
    api.order_status = ["REJECTED"]
    broker = _broker(api)
    assert broker.place_order(IronbeamContract("XCME:MES.Z26", "MES"), "BUY", 1) is None


def test_unfilled_order_returns_working_result_after_timeout(api):
    api.order_status = ["NEW"]
    broker = _broker(api)
    result = broker.place_order(IronbeamContract("XCME:MES.Z26", "MES"), "BUY", 1)
    assert result is not None
    assert result.price is None and result.status == "new"


def test_place_error_returns_none_and_is_not_retried(api):
    api.place_response = FakeResponse(504, {"status": "ERROR", "message": "timeout"})
    broker = _broker(api)
    assert broker.place_order(IronbeamContract("XCME:MES.Z26", "MES"), "BUY", 1) is None
    assert len(api.placed) == 1


def test_fractional_quantity_rounds_and_sub_contract_is_skipped(api):
    broker = _broker(api)
    contract = IronbeamContract("XCME:MES.Z26", "MES")
    assert broker.place_order(contract, "BUY", 0.4) is None
    assert api.placed == []
    broker.place_order(contract, "BUY", 1.6)
    assert api.placed[-1]["legs"][0]["quantity"] == 2


# ---------------------------------------------------------------------------
# Account & positions
# ---------------------------------------------------------------------------


def test_account_summary_fields(api):
    broker = _broker(api)
    s = broker.get_account_summary()
    assert s == {
        "net_liquidation": 49_875.0,
        "unrealized_pnl": -125.0,
        "realized_pnl": 0.0,
        "buying_power": 47_375.0,
    }


def test_positions_are_signed_and_keyed_by_root(api):
    api.positions = [
        {"exchSym": "XCME:MES.Z26", "quantity": 3.0, "side": "SHORT", "price": 6810.0, "unrealizedPL": 15.0},
        {"exchSym": "XCME:MNQ.Z26", "quantity": 1.0, "side": "LONG", "price": 24000.0, "unrealizedPL": -4.0},
    ]
    broker = _broker(api)
    sync = {p["symbol"]: p for p in broker.get_positions_for_sync()}
    assert sync["MES"]["qty"] == -3.0 and sync["MES"]["side"] == "short"
    assert sync["MES"]["qty_available"] == 3.0 and sync["MES"]["avg_price"] == 6810.0
    assert sync["MNQ"]["qty"] == 1.0
    open_rows = {p["symbol"]: p for p in broker.get_open_positions()}
    assert open_rows["MES"] == {"symbol": "MES", "pos": -3.0, "avg_cost": 6810.0, "unreal_pnl": 15.0}


def test_two_contracts_of_one_root_report_net_and_trade_larger(api):
    api.positions = [
        {"exchSym": "XCME:MES.Z26", "quantity": 1.0, "side": "LONG", "price": 6800.0},
        {"exchSym": "XCME:MES.H27", "quantity": 3.0, "side": "LONG", "price": 6900.0},
    ]
    broker = _broker(api)
    (row,) = broker.get_positions_for_sync()
    assert row["qty"] == 4.0 and row["avg_price"] == 6900.0
    assert broker.resolve_contract("ES").exch_sym == "XCME:MES.H27"


def test_disconnect_does_not_log_out(api):
    broker = _broker(api)
    broker.disconnect()
    assert not any(c[1] == "/auth/logout" for c in api.calls)


def test_registered_as_entry_point():
    from importlib.metadata import entry_points

    names = {ep.name for ep in entry_points(group="qs_trader_brokers")}
    assert "ironbeam" in names
