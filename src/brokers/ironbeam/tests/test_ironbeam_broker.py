from __future__ import annotations

from datetime import date, datetime

import pytest
import requests

from qs_trader_broker_ironbeam import IronbeamBroker, IronbeamContract, IronbeamSettings
from qs_trader_broker_ironbeam.broker import IronbeamError, _symbol_map_for, candidate_symbols, root_of

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
        allowed_symbols="MES,MNQ",
        use_market_orders=True,
        roll_days=5,
        fill_timeout_s=2.0,
        http_timeout_s=5.0,
        diagnostics=False,
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
        self.timeouts: dict[str, float] = {}  # by path
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
        self.raise_next: list[Exception] = []
        self.margin_o: dict = {}
        self.per_contract = 2_400.0
        self.state = "ENABLED"
        self.start_as_of = NOW
        # Reason on a REJECTED leg: one for every read, or one per read.
        self.reject_msg: str | None | list = "Insufficient margin"
        # Working order ids, and how many reads a cancelled one stays listed.
        self.working: list[str] = []
        self.cancel_reads = 1
        self.cancel_error: int | None = None
        self.cancelled: list[list[str]] = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        assert url.startswith(f"{BASE}/v3/"), url
        path = url[len(BASE) + 3 :]
        self.calls.append((method, path, params, json))
        self.timeouts[path] = timeout
        if self.raise_next:
            raise self.raise_next.pop(0)
        if self.next_status:
            return self.next_status.pop(0)
        if path != "/auth/login":
            assert headers["Authorization"].startswith("Bearer tok"), headers
        handler = getattr(self, f"_{method.lower()}_{path.strip('/').split('/')[0]}_{path.strip('/').split('/')[1] if '/' in path.strip('/') else ''}", None)
        if path == "/order/updates":
            assert params == {"accountId": "ACC1", "orderState": "ACTIVE"}
            if self.cancelled and not self.cancel_error:
                if self.cancel_reads == 0:
                    self.working = []
                self.cancel_reads -= 1
            return FakeResponse(200, {"status": "OK", "orders": [{"orderId": i, "legs": []} for i in self.working]})
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
        if params.get("type") == "START_OF_DAY":
            margin = {"marginO": {"asOfTime": self.start_as_of * 1000}}
            row = {"currencyCode": "USD", "cashBalance": 50_000.0, "marginInfo": margin}
            return FakeResponse(200, {"status": "OK", "balances": [row]})
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
                        "marginInfo": {"marginO": self.margin_o, "marginOW": {"initialTotalMargin": 2_500.0}},
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
            bare = bare.split(":")[-1]
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

    def _get_reference_securityStatus(self, params, body):
        rows = [{"exchSym": s, "status": "OPEN"} for s in params["symbols"].split(",")]
        return FakeResponse(200, {"status": "OK", "securityStatuses": rows})

    def _get_reference_securityMargin(self, params, body):
        rows = [
            {"exchSym": s, "maintMarginLong": self.per_contract, "maintMarginShort": self.per_contract}
            for s in params["symbols"].split(",")
        ]
        return FakeResponse(200, {"status": "OK", "securityMarginAndValues": rows})

    def _get_risk_riskInfo(self, params, body):
        return FakeResponse(200, {"status": "OK", "risks": [{"tradingState": self.state}]})

    def _get_market_depth(self, params, body):
        rows = [
            {
                "exchSym": s,
                "bids": [{"price": 6800.0, "size": 3}, {"price": 6000.0, "size": 50}],
                "asks": [{"price": 6800.5, "size": 1}, {"price": 6801.0, "size": 2}, {"price": 7500.0, "size": 50}],
            }
            for s in params["symbols"].split(",")
        ]
        return FakeResponse(200, {"status": "OK", "depths": rows})

    def _get_market_quotes(self, params, body):
        quotes = [self.quotes[s] for s in params["symbols"].split(",") if s in self.quotes]
        return FakeResponse(200, {"status": "OK", "quotes": quotes})

    def _post_order_place(self, params, body):
        self.placed.append(body)
        if self.place_response is not None:
            return self.place_response
        return FakeResponse(201, {"status": "OK", "result": "ERROR_NONE", "orderId": "987"})

    def _post_order_cancel(self, params, body):
        self.cancelled.append([c["orderId"] for c in body])
        if self.cancel_error:
            return FakeResponse(self.cancel_error, {"status": "ERROR", "message": "no"})
        return FakeResponse(200, {"orders": [{"status": "OK"} for _ in body]})

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
            msg = self.reject_msg.pop(0) if isinstance(self.reject_msg, list) else self.reject_msg
            if msg:
                out["statusMsg"] = msg
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


def test_contract_size_follows_the_symbol_list():
    m = _symbol_map_for("MES,NQ,GC")
    assert (m["ES"], m["SPY"], m["MES"]) == ("MES", "MES", "MES")
    assert (m["NQ"], m["MNQ"], m["GC"]) == ("NQ", "NQ", "GC")
    assert _symbol_map_for("")["ES"] == "ES"


def test_symbol_list_naming_both_sizes_is_refused():
    with pytest.raises(RuntimeError, match="both ES and MES"):
        _symbol_map_for("ES,MES")


def test_settings_read_the_trader_symbol_list(monkeypatch):
    monkeypatch.setenv("TRADER_ALLOWED_SYMBOLS", "MES,GC")
    assert IronbeamSettings().allowed_symbols == "MES,GC"


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


def _silver(api, nov_oi, dec_oi):
    api.expiries["XCME:SI.X26"] = datetime(2026, 11, 25, 12, 0).timestamp() * 1000
    api.expiries["XCME:SI.Z26"] = datetime(2026, 12, 29, 12, 0).timestamp() * 1000
    for sym, oi in (("XCME:SI.X26", nov_oi), ("XCME:SI.Z26", dec_oi)):
        if oi is not None:
            api.quotes[sym] = {"exchSym": sym, "last": 45.0, "openInterest": oi}


def test_resolve_picks_most_open_interest_over_thin_nearby_month(api):
    _silver(api, nov_oi=522, dec_oi=21_831)
    broker = _broker(api)
    assert broker.resolve_contract("SI") == IronbeamContract("XCME:SI.Z26", "SI")


def test_resolve_keeps_nearby_month_while_it_has_more_open_interest(api):
    _silver(api, nov_oi=30_000, dec_oi=5_000)
    broker = _broker(api)
    assert broker.resolve_contract("SI").exch_sym == "XCME:SI.X26"


def test_resolve_falls_back_to_earliest_without_open_interest(api):
    _silver(api, nov_oi=None, dec_oi=None)
    broker = _broker(api)
    assert broker.resolve_contract("SI").exch_sym == "XCME:SI.X26"


def test_resolve_full_size_roots_when_named(api):
    api.expiries["XCME:ES.Z26"] = api.expiries["XCME:MES.Z26"]
    broker = _broker(api, allowed_symbols="ES")
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
    assert any(w.startswith("GC:") and "GC contract" in w for w in warnings)
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


def test_rejected_order_logs_reason_and_margin(api):
    from loguru import logger

    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="ERROR")
    try:
        api.order_status = ["REJECTED"]
        broker = _broker(api)
        assert broker.place_order(IronbeamContract("XCME:MES.Z26", "MES"), "BUY", 1) is None
    finally:
        logger.remove(sink)
    text = "".join(lines)
    assert "REJECTED: Insufficient margin" in text
    assert "Account at rejection" in text
    assert "initialMargin=$2,500" in text and "buyingPower=$47,375" in text


def test_read_timeout_is_retried_once(api):
    broker = _broker(api)
    api.raise_next = [requests.ReadTimeout("slow")]
    assert broker.get_account_summary()["net_liquidation"] == 49_875.0
    assert [c[1] for c in api.calls[-2:]] == ["/account/balances", "/account/balances"]


def test_read_timeout_twice_raises(api):
    broker = _broker(api)
    api.raise_next = [requests.ReadTimeout("slow"), requests.ReadTimeout("slow")]
    with pytest.raises(requests.ReadTimeout):
        broker.get_account_summary()


def test_order_timeout_is_not_retried(api):
    broker = _broker(api)
    api.raise_next = [requests.ReadTimeout("slow")]
    assert broker.place_order(IronbeamContract("XCME:MES.Z26", "MES"), "BUY", 1) is None
    assert sum(1 for c in api.calls if c[1] == "/order/place") == 1


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


def _lot(symbol, quantity, side="LONG", price=6800.0):
    return {"exchSym": f"XCME:{symbol}", "quantity": quantity, "side": side, "price": price}


def _held(broker):
    return {p["symbol"]: p["qty"] for p in broker.get_positions_for_sync()}


def test_each_month_of_a_product_is_reported_on_its_own(api):
    api.positions = [_lot("MES.Z26", 1.0), _lot("MES.H27", 3.0, price=6900.0)]
    broker = _broker(api)
    assert _held(broker) == {"MES": 3.0, "XCME:MES.Z26": 1.0}
    assert broker.resolve_contract("ES").exch_sym == "XCME:MES.H27"  # the larger one is traded
    assert broker.resolve_contract("MES").exch_sym == "XCME:MES.H27"
    assert broker.resolve_contract("XCME:MES.Z26").exch_sym == "XCME:MES.Z26"
    assert {p["symbol"]: p["pos"] for p in broker.get_open_positions()} == _held(broker)


def test_opposite_months_do_not_look_flat(api):
    api.positions = [_lot("MES.Z26", 3.0), _lot("MES.H27", 3.0, "SHORT")]
    assert _held(_broker(api)) == {"MES": 3.0, "XCME:MES.H27": -3.0}


def test_traded_month_does_not_change_when_a_larger_one_appears(api):
    api.positions = [_lot("MES.Z26", 1.0)]
    broker = _broker(api)
    assert _held(broker) == {"MES": 1.0}
    api.positions.append(_lot("MES.H27", 5.0))  # e.g. bought by hand
    assert _held(broker) == {"MES": 1.0, "XCME:MES.H27": 5.0}


def test_month_keeps_its_name_while_the_others_close(api):
    api.positions = [_lot("MES.Z26", 7.0), _lot("MES.H27", 2.0)]
    broker = _broker(api)
    assert _held(broker) == {"MES": 7.0, "XCME:MES.H27": 2.0}
    api.positions = [_lot("MES.Z26", 1.0), _lot("MES.H27", 2.0)]
    assert _held(broker) == {"MES": 1.0, "XCME:MES.H27": 2.0}
    api.positions = [_lot("MES.H27", 2.0)]
    assert _held(broker) == {"XCME:MES.H27": 2.0}
    api.positions = []
    assert _held(broker) == {}
    api.positions = [_lot("MES.H27", 2.0)]  # opened again later
    assert _held(broker) == {"MES": 2.0}


def test_disconnect_does_not_log_out(api):
    broker = _broker(api)
    broker.disconnect()
    assert not any(c[1] == "/auth/logout" for c in api.calls)


def test_registered_as_entry_point():
    from importlib.metadata import entry_points

    names = {ep.name for ep in entry_points(group="qs_trader_brokers")}
    assert "ironbeam" in names


# ---------------------------------------------------------------------------
# Empty position list while margin is in use
# ---------------------------------------------------------------------------

LONG_MES = {"exchSym": "XCME:MES.Z26", "quantity": 1.0, "price": 6800.0, "side": "LONG", "unrealizedPL": 0.0}


def test_empty_positions_with_margin_in_use_are_not_trusted(api):
    api.margin_o = {"initialTotalMargin": 3960.0, "maintenanceTotalMargin": 3600.0}
    broker = _broker(api)
    with pytest.raises(IronbeamError, match="cannot be trusted"):
        broker.get_positions_for_sync()


def test_empty_positions_are_reread_until_they_appear(api):
    api.margin_o = {"initialTotalMargin": 3960.0}
    broker = _broker(api)
    original, reads = api._get_account_positions, []

    def second_time(params, body):
        reads.append(1)
        if len(reads) == 2:
            api.positions = [LONG_MES]
        return original(params, body)

    api._get_account_positions = second_time
    assert broker.get_positions_for_sync()[0]["qty"] == 1.0


def test_empty_positions_without_margin_mean_flat(api):
    assert _broker(api).get_positions_for_sync() == []


def test_empty_positions_trusted_after_our_own_close_even_if_margin_lags(api):
    api.positions = [LONG_MES]
    api.margin_o = {"initialTotalMargin": 3960.0}
    broker = _broker(api)
    broker.get_positions_for_sync()
    assert broker.place_order(IronbeamContract("XCME:MES.Z26", "MES"), "SELL", 1).status == "filled"
    api.positions = []
    assert broker.get_positions_for_sync() == []


def test_unclear_order_failure_forgets_known_positions(api):
    broker = _broker(api)
    broker.get_positions_for_sync()  # known flat
    api.place_response = FakeResponse(504, {"status": "ERROR", "message": "timeout"})
    broker.place_order(IronbeamContract("XCME:MES.Z26", "MES"), "BUY", 1)
    api.margin_o = {"initialTotalMargin": 3960.0}
    with pytest.raises(IronbeamError):
        broker.get_positions_for_sync()


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


@pytest.fixture
def logs():
    from loguru import logger

    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="DEBUG", format="{level} {message}")
    yield lines
    logger.remove(sink)


def _line(logs: list[str], text: str) -> str:
    found = [x for x in logs if text in x]
    assert found, f"{text!r} not in:\n" + "".join(logs)
    return found[-1]


@pytest.mark.parametrize(
    "maint, predicted",
    # Friday's two closest calls: 15:57 filled, 15:52 was rejected.
    [(49_736 - 2 * 3600, "accept"), (50_400 - 2 * 3600, "reject")],
)
def test_order_prediction_uses_start_of_day_cash(api, logs, maint, predicted):
    api.positions = [dict(LONG_MES, quantity=12.0)]
    api.margin_o = {"maintenanceTotalMargin": maint, "initialTotalMargin": maint * 1.1}
    api.per_contract = 3600.0
    broker = _broker(api, diagnostics=True)
    broker.get_positions_for_sync()
    broker.place_order(IronbeamContract("XCME:MES.Z26", "MES"), "BUY", 2)
    assert f"expect={predicted}" in _line(logs, "PRE-ORDER")


def test_rejection_logs_matching_causes_and_count(api, logs):
    api.positions = [dict(LONG_MES, quantity=12.0)]
    api.margin_o = {"maintenanceTotalMargin": 48_000.0, "initialTotalMargin": 52_800.0}
    api.per_contract = 3600.0
    api.order_status = ["REJECTED"]
    api.reject_msg = None
    api.state = "DISABLED_FOR_DAY"
    broker = _broker(api, diagnostics=True)
    broker.get_positions_for_sync()
    contract = IronbeamContract("XCME:MES.Z26", "MES")
    assert broker.place_order(contract, "BUY", 5) is None
    assert broker.place_order(contract, "BUY", 5) is None
    line = _line(logs, "ORDER RESULT")
    assert "rejection 2 in a row" in line and "prediction correct=yes" in line
    assert "DISABLED_FOR_DAY" in line and "margin: about $66,000" in line and "only 3 contract(s)" in line


def test_late_reject_reason_is_picked_up(api, logs):
    api.order_status = ["REJECTED"]
    api.reject_msg = [None, "Exceeds buying power"]
    broker = _broker(api, diagnostics=True)
    assert broker.place_order(IronbeamContract("XCME:MES.Z26", "MES"), "BUY", 1) is None
    assert "REJECTED: Exceeds buying power" in "".join(logs)


def test_startup_check_logs_state_margin_and_positions(api, logs):
    api.positions = [dict(LONG_MES, dateOpened="20261117")]
    api.margin_o = {"maintenanceTotalMargin": 60_000.0, "initialTotalMargin": 66_000.0}
    api.start_as_of = NOW - 5 * 86_400
    broker = _broker(api, diagnostics=True)
    broker.startup_entitlement_warnings(["ES"])
    assert "ENABLED" in _line(logs, "CHECK account state")
    assert "startOfDayCash=$50,000" in _line(logs, "CHECK margin: cash")
    assert "no room left" in "".join(logs) and "below the $60,000" in "".join(logs)
    assert "restored an older copy" in "".join(logs)
    assert "+1" in _line(logs, "CHECK position XCME:MES.Z26") and "20261117" in "".join(logs)
    assert "x3" in _line(logs, "CHECK contract XCME:MES.Z26")


def test_startup_warns_about_held_contract_that_is_thin_and_expiring(api):
    api.expiries["XCME:MES.X26"] = (NOW + 2 * 86_400) * 1000
    api.positions = [dict(LONG_MES, exchSym="XCME:MES.X26")]
    broker = _broker(api, diagnostics=True)
    warnings = broker.startup_entitlement_warnings(["ES"])  # before any positions read, as in the app
    assert any("XCME:MES.X26 expires in 2.0" in w for w in warnings)
    assert any("XCME:MES.X26 is thin" in w for w in warnings)


def test_failing_checks_never_stop_an_order(api, logs):
    broker = _broker(api, diagnostics=True)
    original = api.request

    def only_orders_work(method, url, **kwargs):
        if "/order/" not in url:
            raise requests.ReadTimeout("slow")
        return original(method, url, **kwargs)

    api.request = only_orders_work
    result = broker.place_order(IronbeamContract("XCME:MES.Z26", "MES"), "BUY", 1)
    assert result is not None and result.status == "filled"
    assert "PRE-ORDER check incomplete" in "".join(logs)
    assert len(api.placed) == 1


def test_closing_orders_are_never_predicted_to_be_rejected():
    from qs_trader_broker_ironbeam.diagnostics import OrderCheck, margin_after

    assert margin_after(48_000, 3600, +12, "SELL", 2) == (40_800, 0)
    assert margin_after(48_000, 3600, +1, "SELL", 3) == (51_600, 2)
    check = OrderCheck("X", "SELL", 2, opening=0, margin_after=60_000, start_cash=50_000)
    assert check.predicted == "accept"


# ---------------------------------------------------------------------------
# What the app needs to close positions safely
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["REJECTED", "CANCELED", "EXPIRED"])
def test_refused_order_is_reported_as_not_placed(api, status):
    broker = _broker(api)
    api.order_status = [status]
    assert broker.place_order(broker.resolve_contract("ES"), "SELL", 1) is None
    assert broker.last_order_not_placed


@pytest.mark.parametrize(
    "http, refused", [(400, True), (403, True), (429, True), (408, False), (409, False), (500, False), (503, False)]
)
def test_only_a_4xx_answer_to_an_order_is_a_refusal(api, http, refused):
    broker = _broker(api)
    api.place_response = FakeResponse(http, {"status": "ERROR", "message": "no"})
    assert broker.place_order(broker.resolve_contract("ES"), "SELL", 1) is None
    assert broker.last_order_not_placed is refused


def test_order_with_unknown_outcome_is_not_a_refusal(api):
    broker = _broker(api)
    contract = broker.resolve_contract("ES")
    broker.last_order_not_placed = True  # left over from an earlier order
    api.raise_next = [requests.ReadTimeout("slow")]
    assert broker.place_order(contract, "SELL", 1) is None
    assert not broker.last_order_not_placed
    api.order_status = ["STOPPED"]  # may still execute
    assert broker.place_order(contract, "SELL", 1) is None
    assert not broker.last_order_not_placed


def test_order_never_sent_is_not_placed(api):
    broker = _broker(api)
    assert broker.place_order(broker.resolve_contract("ES"), "SELL", 0.2) is None
    assert broker.last_order_not_placed and api.placed == []


def test_no_working_orders_needs_no_cancel(api):
    assert _broker(api).cancel_working_orders() and api.cancelled == []


def test_working_orders_are_cancelled_in_batches_and_confirmed(api):
    broker = _broker(api)
    api.working = [str(i) for i in range(45)]
    api.cancel_reads = 2  # a cancel is only accepted at first
    assert broker.cancel_working_orders()
    assert [len(batch) for batch in api.cancelled] == [20, 20, 5]


def test_cancel_that_never_takes_effect_is_not_confirmed(api):
    broker = _broker(api)
    api.working, api.cancel_reads = ["1"], 10**6
    assert not broker.cancel_working_orders()


def test_order_that_finished_before_its_cancel_is_confirmed_by_the_list(api):
    broker = _broker(api)
    api.working, api.cancel_error = ["1"], 404
    real = api._post_order_cancel

    def finished(params, body):
        api.working = []
        return real(params, body)

    api._post_order_cancel = finished
    assert broker.cancel_working_orders()


def test_refused_cancel_is_not_confirmed(api):
    broker = _broker(api)
    api.working, api.cancel_error = ["1"], 403  # e.g. an order the risk service placed
    assert not broker.cancel_working_orders()


@pytest.mark.parametrize("configured, expected", [(5.0, 35.0), (60.0, 60.0)])
def test_an_order_is_awaited_longer_than_ironbeam_takes_to_answer(api, configured, expected):
    broker = _broker(api, http_timeout_s=configured)
    broker.place_order(broker.resolve_contract("ES"), "SELL", 1)
    assert api.timeouts["/order/place"] == expected  # Ironbeam answers within 30 s
    assert api.timeouts["/order/987"] == configured


# ---------------------------------------------------------------------------
# A contract lookup that fails is tried again
# ---------------------------------------------------------------------------


def _failing(api, name, failure):
    """Make one read fail until the returned function is called."""
    real = getattr(api, name)

    def fail(params, body):
        if isinstance(failure, Exception):
            raise failure
        return FakeResponse(failure, {"status": "ERROR", "message": "boom"})

    setattr(api, name, fail)
    return lambda: setattr(api, name, real)


@pytest.mark.parametrize("failure", [500, requests.ReadTimeout("slow"), requests.ConnectionError("down")])
@pytest.mark.parametrize("read", ["_get_reference_securityDefinitions", "_get_market_quotes"])
def test_failed_lookup_skips_the_symbol_only_until_the_next_try(api, read, failure):
    api.quotes["XCME:MES.Z26"]["openInterest"] = 5
    api.quotes["XCME:MES.H27"] = {"exchSym": "XCME:MES.H27", "last": 6900.0, "openInterest": 9}
    broker = _broker(api)
    recover = _failing(api, read, failure)
    assert broker.resolve_contract("ES") is None
    recover()
    assert broker.resolve_contract("ES").exch_sym == "XCME:MES.H27"


def test_one_failed_batch_does_not_pick_from_the_others(api):
    api.next_status = []
    broker = _broker(api)
    real = api._get_reference_securityDefinitions
    calls: list = []

    def first_batch_fails(params, body):
        calls.append(1)
        return FakeResponse(500, {"status": "ERROR"}) if len(calls) == 1 else real(params, body)

    api._get_reference_securityDefinitions = first_batch_fails
    api.expiries["XCME:MES.U27"] = datetime(2027, 9, 17).timestamp() * 1000
    assert broker.resolve_contract("ES") is None
    assert broker.resolve_contract("ES").exch_sym == "XCME:MES.Z26"


def test_product_that_is_not_listed_is_looked_up_once_a_day(api):
    broker = _broker(api)
    assert broker.resolve_contract("GC") is None
    before = len(api.calls)
    assert broker.resolve_contract("GC") is None and len(api.calls) == before


def test_startup_survives_a_failed_lookup(api):
    broker = _broker(api, diagnostics=True)
    _failing(api, "_get_reference_securityDefinitions", requests.ReadTimeout("slow"))
    assert any("ES" in w for w in broker.startup_entitlement_warnings(["ES"]))
