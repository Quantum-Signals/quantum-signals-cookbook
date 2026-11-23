from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Optional

import requests
from loguru import logger

from qs_trader_broker_core.base import BrokerBase, OrderResult
from qs_trader_symbols import canonicalize, ironbeam_root_exchanges, ironbeam_symbol_map

from .diagnostics import Diagnostics, OrderCheck
from .settings import IronbeamSettings

# CME Group month codes, January → December.
_MONTH_CODES = "FGHJKMNQUVXZ"
# How many months ahead to look for listed contracts when finding the front month.
_LOOKAHEAD_MONTHS = 13
# /reference/securityDefinitions accepts at most 10 symbols per call.
_DEFS_BATCH = 10
_POLL_INTERVAL_S = 0.5
_MAX_RETRY_AFTER_S = 10.0
# Pause before retrying a read that timed out or lost its connection.
_NETWORK_RETRY_WAIT_S = 2.0
# An empty position list is re-read this often while the account shows margin in use.
_EMPTY_POSITIONS_READS = 3

_FAILED_STATUSES = frozenset({"REJECTED", "CANCELED", "EXPIRED", "STOPPED", "SUSPENDED", "DONE_FOR_DAY"})


class IronbeamError(RuntimeError):
    """An Ironbeam REST call answered with an error envelope or HTTP error status."""

    def __init__(self, status: int, message: str, result: Optional[str] = None) -> None:
        self.status = status
        self.message = message
        self.result = result
        detail = f" [{result}]" if result else ""
        super().__init__(f"Ironbeam HTTP {status}: {message}{detail}")


@dataclass(frozen=True)
class IronbeamContract:
    """Opaque contract: one listed futures contract on Ironbeam.

    ``exch_sym`` is what the API takes (``XCME:MES.Z26``). ``root`` is the
    product (``MES``); it is the ``symbol`` in position dicts so the app's
    ``contract_multipliers`` and the strategy's position lookups key on it.
    """

    exch_sym: str
    root: str

    @property
    def position_key(self) -> str:
        return self.root

    @property
    def trading_symbol(self) -> str:
        return self.exch_sym


def root_of(exch_sym: str) -> str:
    """``XCME:MES.Z26`` / ``MES.Z26`` → ``MES``."""
    bare = exch_sym.split(":", 1)[-1]
    return bare.split(".", 1)[0]


def candidate_symbols(root: str, today: date, months: int = _LOOKAHEAD_MONTHS) -> list[str]:
    """Bare contract symbols for *root* from this month forward (``MES.Z26``, ``MES.F27``, …).

    Not every month is listed (index futures are quarterly); the caller keeps
    whichever ones Ironbeam knows.
    """
    out: list[str] = []
    year, month = today.year, today.month
    for _ in range(months):
        out.append(f"{root}.{_MONTH_CODES[month - 1]}{year % 100:02d}")
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return out


class IronbeamBroker(BrokerBase):
    """Broker backed by the Ironbeam REST API v3 (CME Group futures).

    Each QuantumSignals instrument maps to a futures product root (micro by
    default: ``ES`` → ``MES``). The contract month is resolved at runtime: of
    the listed contracts more than ``IRONBEAM_ROLL_DAYS`` from expiry, the one
    with the most open interest (the earliest if quotes are unavailable),
    cached for the day. While the account holds a position in a root,
    that held contract is traded instead, so a roll never leaves a stray leg in
    the old month (the EOD flatten closes it, and the next day picks the new
    front month).

    Ironbeam accounts net positions, so ``position_intent`` is not needed: a
    SELL against a long reduces it. Quantities are whole contracts
    (``supports_fractional`` is false). Orders go in as ``FAST_ORDER`` MARKET
    (or LIMIT) DAY orders, then ``place_order`` polls ``GET /order/{id}`` for
    the fill.
    """

    supports_fractional: bool = False

    def __init__(
        self,
        settings: Optional[IronbeamSettings] = None,
        *,
        symbol_map: Optional[dict[str, str]] = None,
        session: Any = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings if settings is not None else IronbeamSettings()
        self._symbol_map = (
            symbol_map
            if symbol_map is not None
            else ironbeam_symbol_map(use_micros=self._settings.use_micros)
        )
        self._exchanges = ironbeam_root_exchanges()
        self._session = session
        self._clock = clock
        self._sleep = sleep
        self._base = self._settings.base_url.rstrip("/")
        if self._base.endswith("/v3"):
            self._base = self._base[: -len("/v3")]
        self._token: str = ""
        self._account_id: str = ""
        # root → (local date resolved, contract or None)
        self._front: dict[str, tuple[date, Optional[IronbeamContract]]] = {}
        # root → contract currently held, refreshed on every positions read.
        self._held: dict[str, IronbeamContract] = {}
        # exch_sym → signed contracts: last positions read plus our own fills since.
        self._known_positions: Optional[dict[str, float]] = None
        self._diag: Optional[Diagnostics] = Diagnostics(self) if self._settings.diagnostics else None

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
        auth: bool = True,
        timeout: Optional[float] = None,
        network_retry: bool = True,
        _retried: bool = False,
    ) -> dict:
        """Call ``/v3{path}`` and return the JSON body, raising ``IronbeamError`` on failure.

        Retries once, and only where it is safe: a 401 after logging in again
        (expired token), a 429 / 503 that carries ``Retry-After`` (Ironbeam did
        nothing), and a timeout or dropped connection on a ``GET`` (read-only).
        A 5xx without ``Retry-After``, a 504, or a timeout on a ``POST`` is never
        retried, because an order may have been placed. ``network_retry=False``
        skips the timeout retry, for callers that retry themselves.
        """
        if self._session is None:
            raise RuntimeError("Ironbeam broker is not connected")
        headers = {"Accept": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            resp = self._session.request(
                method,
                f"{self._base}/v3{path}",
                params=params,
                json=body,
                headers=headers,
                timeout=timeout if timeout is not None else self._settings.http_timeout_s,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            if method != "GET" or _retried or not network_retry:
                raise
            logger.warning(f"[Ironbeam] GET {path} failed ({type(exc).__name__}) — retrying once")
            self._sleep(_NETWORK_RETRY_WAIT_S)
            return self._request(
                method, path, params=params, body=body, auth=auth, timeout=timeout, _retried=True
            )
        resp_headers = getattr(resp, "headers", None) or {}
        fresh = resp_headers.get("X-Auth-Token")
        if fresh and auth:
            self._token = fresh

        status = int(resp.status_code)
        if not _retried:
            if status == 401 and auth:
                logger.warning(f"[Ironbeam] {method} {path} returned 401 — logging in again")
                self._login()
                return self._request(
                method, path, params=params, body=body, auth=auth, timeout=timeout, _retried=True
            )
            retry_after = resp_headers.get("Retry-After")
            if status in (429, 503) and retry_after is not None:
                try:
                    wait = min(float(retry_after), _MAX_RETRY_AFTER_S)
                except ValueError:
                    wait = 1.0
                logger.warning(f"[Ironbeam] {method} {path} returned {status} — retrying in {wait:g}s")
                self._sleep(wait)
                return self._request(
                method, path, params=params, body=body, auth=auth, timeout=timeout, _retried=True
            )

        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {"data": payload}
        if status >= 400 or payload.get("status") == "ERROR":
            message = str(payload.get("message") or getattr(resp, "text", "")[:200] or "no message")
            raise IronbeamError(status, message, payload.get("result"))
        return payload

    def _login(self) -> None:
        body: dict[str, str] = {"apikey": self._settings.api_key}
        if self._settings.username:
            body["username"] = self._settings.username
        if self._settings.password:
            body["password"] = self._settings.password
        payload = self._request("POST", "/auth/login", body=body, auth=False, _retried=True)
        token = payload.get("token")
        if not token:
            raise RuntimeError("Ironbeam login returned no token")
        self._token = str(token)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        if not self._settings.api_key:
            raise RuntimeError("IRONBEAM_API_KEY is not set")
        if self._session is None:
            import requests

            self._session = requests.Session()

        self._login()

        info = self._request("GET", "/reference/userInfo", params={"includeAccounts": "true"})
        accounts = [str(a) for a in info.get("accounts") or []]
        trader_id = str(info.get("traderId") or "")
        self._account_id = self._pick_account(accounts, trader_id)

        balance = self._balance()
        equity = float(balance.get("netLiquidity", balance.get("totalEquity", 0.0)) or 0.0)
        logger.success(
            f"Connected to Ironbeam  host={self._base}  trader={trader_id or '?'}  "
            f"account={self._account_id}  "
            f"net_liq={equity:,.2f} {balance.get('currencyCode', '')}"
        )
        logger.info(
            "[Ironbeam] Live and simulated accounts run on separate hosts with separate keys; "
            f"orders on this key go to {self._base}."
        )
        logger.info(
            f"[Ironbeam] Trading {'micro' if self._settings.use_micros else 'full-size'} "
            "futures in whole contracts"
        )
        if self._diag is not None:
            self._diag.account_check()

    def _pick_account(self, accounts: list[str], trader_id: str) -> str:
        wanted = self._settings.account_id.strip()
        if wanted:
            if accounts and wanted.upper() not in {a.upper() for a in accounts}:
                raise RuntimeError(
                    f"IRONBEAM_ACCOUNT_ID={wanted} is not one of this login's accounts: "
                    f"{', '.join(accounts)}"
                )
            return wanted
        if len(accounts) == 1:
            return accounts[0]
        if not accounts and trader_id:
            return trader_id
        if not accounts:
            raise RuntimeError("Ironbeam returned no account for this login; set IRONBEAM_ACCOUNT_ID")
        raise RuntimeError(
            f"This Ironbeam login can trade {len(accounts)} accounts ({', '.join(accounts)}). "
            "Set IRONBEAM_ACCOUNT_ID to the one to trade."
        )

    def disconnect(self) -> None:
        # Deliberately no POST /auth/logout: it revokes every token of the
        # login, including the client's own platform sessions.
        close = getattr(self._session, "close", None)
        if callable(close):
            close()
        self._session = None
        logger.info("[Ironbeam] Disconnected")

    # ------------------------------------------------------------------
    # Contract resolution
    # ------------------------------------------------------------------

    def resolve_contract(self, qs_symbol: str) -> Optional[IronbeamContract]:
        """Return the contract to trade for *qs_symbol*, or ``None`` when Ironbeam has none.

        QS aliases map to a product root (``ES`` → ``MES``); instruments with no
        futures root (``AAPL``) return ``None``. A position symbol the broker
        reported (a root such as ``MES``) returns the held contract, so the EOD
        flatten closes exactly what is open. An explicit contract
        (``XCME:ES.Z26`` or ``ES.Z26``) is passed through.
        """
        held = self._held.get(qs_symbol)
        if held is not None:
            return held

        root = self._symbol_map.get(qs_symbol)
        if root is None:
            if canonicalize(qs_symbol) is not None:
                logger.debug(f"[Ironbeam] '{qs_symbol}' has no Ironbeam futures root — skipping")
                return None
            if "." in qs_symbol:
                return IronbeamContract(exch_sym=qs_symbol, root=root_of(qs_symbol))
            logger.debug(f"[Ironbeam] '{qs_symbol}' is not a futures symbol — skipping")
            return None

        held = self._held.get(root)
        if held is not None:
            return held
        return self._front_month(root)

    def _front_month(self, root: str) -> Optional[IronbeamContract]:
        today = datetime.fromtimestamp(self._clock()).date()
        cached = self._front.get(root)
        if cached is not None and cached[0] == today:
            return cached[1]

        contract = self._lookup_front_month(root, today)
        self._front[root] = (today, contract)
        if contract is not None:
            logger.info(f"[Ironbeam] {root} front month → {contract.exch_sym}")
        else:
            logger.warning(f"[Ironbeam] No listed {root} futures contract found")
        return contract

    def _lookup_front_month(self, root: str, today: date) -> Optional[IronbeamContract]:
        candidates = candidate_symbols(root, today)
        definitions: list[dict] = []
        for i in range(0, len(candidates), _DEFS_BATCH):
            batch = candidates[i : i + _DEFS_BATCH]
            try:
                payload = self._request(
                    "GET", "/reference/securityDefinitions", params={"symbols": ",".join(batch)}
                )
            except IronbeamError as exc:
                logger.warning(f"[Ironbeam] Contract lookup for {root} failed: {exc}")
                continue
            definitions.extend(payload.get("securityDefinitions") or [])

        preferred = self._exchanges.get(root)
        cutoff_ms = (self._clock() + self._settings.roll_days * 86_400) * 1000
        listed: list[tuple[tuple, str]] = []
        for d in definitions:
            exch_sym = str(d.get("exchSym") or "")
            if not exch_sym or d.get("legs") or d.get("securityType", "FUT") != "FUT":
                continue
            if root_of(exch_sym) != root:
                continue
            expiry = d.get("expirationTime")
            if expiry is not None and float(expiry) <= cutoff_ms:
                continue
            exchange = exch_sym.split(":", 1)[0] if ":" in exch_sym else ""
            key = (
                0 if exchange == preferred else 1,
                int(d.get("maturityYear") or 9999),
                int(d.get("maturityMonth") or 99),
                float(expiry) if expiry is not None else float("inf"),
            )
            listed.append((key, exch_sym))
        if not listed:
            return None
        listed.sort()
        # Keep one exchange: the preferred one when it lists the product.
        exchange_rank = listed[0][0][0]
        months = [sym for key, sym in listed if key[0] == exchange_rank][:_DEFS_BATCH]
        return IronbeamContract(exch_sym=self._most_traded(months), root=root)

    def _most_traded(self, months: list[str]) -> str:
        """Pick the contract with the highest open interest from *months* (chronological).

        The earliest listed month is not always the traded one: metals and
        energy list every month, and the nearest can be a thin delivery month
        (silver's November with a few lots a day while December trades tens of
        thousands). Open interest marks the active month. Falls back to the
        earliest month when quotes are unavailable.
        """
        if len(months) == 1:
            return months[0]
        try:
            payload = self._request("GET", "/market/quotes", params={"symbols": ",".join(months)})
        except IronbeamError as exc:
            logger.warning(f"[Ironbeam] Open interest lookup failed ({exc}); using {months[0]}")
            return months[0]
        open_interest = {
            str(q.get("exchSym")): float(q.get("openInterest") or 0)
            for q in payload.get("quotes") or []
        }
        best = max(months, key=lambda sym: (open_interest.get(sym, 0.0), -months.index(sym)))
        if open_interest.get(best, 0.0) <= 0:
            return months[0]
        if best != months[0]:
            logger.info(
                f"[Ironbeam] {best} has the most open interest "
                f"({open_interest[best]:,.0f}); skipping {months[0]} "
                f"({open_interest.get(months[0], 0.0):,.0f})"
            )
        return best

    def startup_entitlement_warnings(self, canonical_symbols: list[str]) -> list[str]:
        warnings: list[str] = []
        for canon in canonical_symbols:
            if self._symbol_map.get(canon) is None:
                warnings.append(f"{canon} is not a futures product on Ironbeam")
            elif self.resolve_contract(canon) is None:
                warnings.append(f"{canon}: no listed {self._symbol_map[canon]} contract on Ironbeam")
        if self._diag is not None:
            contracts = [c for c in map(self.resolve_contract, canonical_symbols) if c is not None]
            warnings.extend(self._diag.contract_check([c.exch_sym for c in contracts]))
        return warnings

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def get_last_price(self, contract: IronbeamContract) -> Optional[float]:
        try:
            payload = self._request("GET", "/market/quotes", params={"symbols": contract.exch_sym})
        except IronbeamError as exc:
            logger.warning(f"[Ironbeam] Quote for {contract.exch_sym} failed: {exc}")
            return None
        quotes = payload.get("quotes") or []
        if not quotes:
            logger.warning(f"[Ironbeam] No quote data for {contract.exch_sym}")
            return None
        q = quotes[0]
        last = q.get("last")
        if last is not None and float(last) > 0:
            return float(last)
        bid, ask = q.get("bid"), q.get("ask")
        if bid is not None and ask is not None and float(bid) > 0 and float(ask) > 0:
            return (float(bid) + float(ask)) / 2.0
        logger.warning(f"[Ironbeam] {contract.exch_sym} has no last trade or two-sided quote")
        return None

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    def place_order(
        self,
        contract: IronbeamContract,
        action: str,
        qty: float,
        price: Optional[float] = None,
        position_intent: Optional[str] = None,
    ) -> Optional[OrderResult]:
        side = action.upper()
        if side not in {"BUY", "SELL"}:
            logger.error(f"[Ironbeam] Unknown order action {action!r}")
            return None
        quantity = int(round(float(qty)))
        if quantity < 1:
            logger.warning(f"[Ironbeam] {side} {qty} {contract.exch_sym} is under one contract — not sent")
            return None
        if abs(quantity - float(qty)) > 1e-6:
            logger.info(f"[Ironbeam] Rounded {side} {qty} → {quantity} contracts")

        market = self._settings.use_market_orders or price is None
        leg: dict[str, object] = {
            "role": "MAIN",
            "accountId": self._account_id,
            "exchSym": contract.exch_sym,
            "side": side,
            "quantity": quantity,
            "kind": "MARKET" if market else "LIMIT",
            "duration": "DAY",
        }
        if not market:
            leg["limitPrice"] = float(price)
        check = self._diag.before_order(contract, side, quantity) if self._diag is not None else None
        cl_ord_id = f"qs-{uuid.uuid4().hex[:16]}"
        body = {"orderType": "FAST_ORDER", "clOrdId": cl_ord_id, "legs": [leg]}

        try:
            payload = self._request("POST", "/order/place", body=body)
        except IronbeamError as exc:
            if exc.status in (500, 502, 503, 504):
                self._known_positions = None  # it may have filled
                logger.error(
                    f"[Ironbeam] {side} {quantity} {contract.exch_sym}: {exc}. The order may still "
                    f"have been placed (clOrdId={cl_ord_id}); the next interval re-plans from positions."
                )
            else:
                logger.error(f"[Ironbeam] {side} {quantity} {contract.exch_sym} rejected: {exc}")
            return None
        except Exception as exc:  # network failure: the order may or may not be in
            self._known_positions = None
            logger.error(
                f"[Ironbeam] {side} {quantity} {contract.exch_sym} request failed ({exc}); "
                f"it may have been placed (clOrdId={cl_ord_id})"
            )
            return None

        order_id = str(payload.get("orderId") or "")
        if not order_id:
            self._known_positions = None
            logger.error(f"[Ironbeam] Place answered without an orderId: {payload}")
            return None
        logger.info(
            f"[Ironbeam] {side} {quantity}x {contract.exch_sym} {leg['kind']} accepted  "
            f"id={order_id}  clOrdId={cl_ord_id}"
        )
        return self._await_fill(order_id, contract, side, quantity, check)

    def _await_fill(
        self,
        order_id: str,
        contract: IronbeamContract,
        side: str,
        quantity: int,
        check: Optional[OrderCheck] = None,
    ) -> Optional[OrderResult]:
        deadline = self._clock() + self._settings.fill_timeout_s
        leg: dict = {}
        order: dict = {}
        while True:
            try:
                payload = self._request("GET", f"/order/{order_id}")
                order = payload.get("order") or {}
                legs = order.get("legs") or []
                leg = next((x for x in legs if x.get("role") == "MAIN"), legs[0] if legs else {})
            except IronbeamError as exc:
                if exc.status != 404:  # 404 right after placing just means "not reported yet"
                    logger.warning(f"[Ironbeam] Order {order_id} lookup failed: {exc}")
            except Exception as exc:
                logger.warning(f"[Ironbeam] Order {order_id} lookup failed: {exc}")

            status = str(leg.get("status") or "")
            filled = float(leg.get("filledQuantity") or 0)
            avg = leg.get("avgFillPrice")
            if status == "FILLED":
                self._book_fill(contract, side, filled or quantity)
                if self._diag is not None and check is not None:
                    self._diag.after_order(check, order_id, True, avg)
                return _result(order_id, contract, side, filled or quantity, avg, "filled", leg)
            if status in _FAILED_STATUSES:
                if status == "REJECTED" and not _given_reason(leg, order):
                    leg, order = self._reread_rejection(order_id, leg, order)
                reason = _reject_reason(leg, order)
                if filled > 0:
                    self._book_fill(contract, side, filled)
                    logger.warning(
                        f"[Ironbeam] Order {order_id} {status} after filling {filled:g} of {quantity}: {reason}"
                    )
                    return _result(order_id, contract, side, filled, avg, "partially_filled", leg)
                logger.error(f"[Ironbeam] Order {order_id} {status}: {reason}")
                self._log_margin_snapshot()
                if self._diag is not None and check is not None and status == "REJECTED":
                    self._diag.after_order(check, order_id, False, reason=_given_reason(leg, order))
                return None
            if self._clock() >= deadline:
                break
            self._sleep(_POLL_INTERVAL_S)

        filled = float(leg.get("filledQuantity") or 0)
        logger.warning(
            f"[Ironbeam] Order {order_id} not filled after {self._settings.fill_timeout_s:g}s "
            f"(status={leg.get('status', 'unknown')}, filled={filled:g}/{quantity}) — still working"
        )
        # The order is still working, so what it will fill is not known.
        self._known_positions = None
        if filled > 0:
            return _result(order_id, contract, side, filled, leg.get("avgFillPrice"), "partially_filled", leg)
        return OrderResult(
            id=order_id,
            symbol=contract.exch_sym,
            action=side,
            qty=float(quantity),
            price=None,
            status=str(leg.get("status") or "submitted").lower(),
            raw=leg or None,
        )

    def _reread_rejection(self, order_id: str, leg: dict, order: dict) -> tuple[dict, dict]:
        """Read a rejected order once more: its ``statusMsg`` can arrive a moment later."""
        self._sleep(1.0)
        try:
            again = self._request("GET", f"/order/{order_id}", timeout=5.0, network_retry=False).get("order") or {}
            legs = again.get("legs") or []
            again_leg = next((x for x in legs if x.get("role") == "MAIN"), legs[0] if legs else {})
        except Exception as exc:
            logger.warning(f"[Ironbeam] Order {order_id}: could not re-read the rejection ({exc})")
            return leg, order
        return (again_leg, again) if again_leg else (leg, order)

    def _book_fill(self, contract: IronbeamContract, side: str, filled: float) -> None:
        if self._known_positions is not None:
            signed = filled if side == "BUY" else -filled
            held = self._known_positions.get(contract.exch_sym, 0.0) + signed
            self._known_positions[contract.exch_sym] = held

    # ------------------------------------------------------------------
    # Account & positions
    # ------------------------------------------------------------------

    def _balance(self) -> dict:
        payload = self._request("GET", "/account/balances", params={"accountId": self._account_id})
        balances = payload.get("balances") or []
        if not balances:
            raise RuntimeError(f"Ironbeam returned no balance for account {self._account_id}")
        return next((b for b in balances if b.get("currencyCode") == "USD"), balances[0])

    def _log_margin_snapshot(self) -> None:
        """After a rejection, log the account's margin so a margin refusal is visible.

        Ironbeam often rejects for margin without a ``statusMsg``; this shows
        whether the account had room for the order. Never raises.
        """
        try:
            b = self._balance()
            summary = self.get_account_summary()
            margin_ow = (b.get("marginInfo") or {}).get("marginOW") or {}
            parts = [
                f"netLiq=${summary['net_liquidation']:,.0f}",
                f"initialMargin=${float(margin_ow.get('initialTotalMargin') or 0):,.0f}",
                f"maintMargin=${float(margin_ow.get('maintenanceTotalMargin') or 0):,.0f}",
                f"buyingPower=${summary['buying_power']:,.0f}",
            ]
            if margin_ow.get("marginError"):
                parts.append(f"marginError={margin_ow['marginError']}")
            hint = "  (no buying power left, likely a margin rejection)" if summary["buying_power"] <= 0 else ""
            logger.error(f"[Ironbeam] Account at rejection: {'  '.join(parts)}{hint}")
        except Exception as exc:
            logger.warning(f"[Ironbeam] Could not read margin after rejection: {exc}")

    def get_account_summary(self) -> dict:
        b = self._balance()
        net_liq = float(b.get("netLiquidity", b.get("totalEquity", 0.0)) or 0.0)
        available = float(b.get("netLiquidityAvailable", net_liq) or 0.0)
        margin = ((b.get("marginInfo") or {}).get("marginOW") or {}).get("initialTotalMargin")
        buying_power = available - float(margin) if margin is not None else float(
            b.get("cashBalanceAvailable", available) or 0.0
        )
        return {
            "net_liquidation": net_liq,
            "unrealized_pnl": float(b.get("openTradeEquity", 0.0) or 0.0),
            "realized_pnl": 0.0,
            "buying_power": buying_power,
        }

    def _read_positions(self, timeout: Optional[float] = None, network_retry: bool = True) -> list[dict]:
        payload = self._request(
            "GET",
            "/account/positions",
            params={"accountId": self._account_id, "consolidate": "true"},
            timeout=timeout,
            network_retry=network_retry,
        )
        return payload.get("positions") or []

    def _margin_in_use(self) -> float:
        info = self._balance().get("marginInfo") or {}
        return float((info.get("marginO") or {}).get("initialTotalMargin") or 0.0)

    def _confirmed_positions(self) -> list[dict]:
        """Read positions, refusing an empty list the account's margin contradicts.

        Reading "flat" while holding a position would make the strategy open a
        second one. An empty list is believed when our own records are flat or
        no margin is in use; otherwise it is read again, then raised as an error
        so the caller skips instead of trading.
        """
        rows = self._read_positions()
        if any(float(p.get("quantity") or 0) > 0 for p in rows):
            return rows
        if self._known_positions is not None and not any(self._known_positions.values()):
            return rows
        margin = self._margin_in_use()
        for _ in range(_EMPTY_POSITIONS_READS - 1):
            if margin <= 0:
                return rows
            self._sleep(_NETWORK_RETRY_WAIT_S)
            rows = self._read_positions()
            if any(float(p.get("quantity") or 0) > 0 for p in rows):
                return rows
            margin = self._margin_in_use()
        if margin <= 0:
            return rows
        raise IronbeamError(
            200, f"positions came back empty while ${margin:,.0f} of margin is in use, so they cannot be trusted"
        )

    def _net_positions(self) -> dict[str, dict]:
        rows = self._confirmed_positions()
        by_root: dict[str, list[tuple[str, float, float, float]]] = {}
        known: dict[str, float] = {}
        for p in rows:
            exch_sym = str(p.get("exchSym") or "")
            qty = float(p.get("quantity") or 0.0)
            if not exch_sym or qty <= 0:
                continue
            signed = -qty if str(p.get("side", "")).upper() == "SHORT" else qty
            known[exch_sym] = known.get(exch_sym, 0.0) + signed
            by_root.setdefault(root_of(exch_sym), []).append(
                (exch_sym, signed, float(p.get("price") or 0.0), float(p.get("unrealizedPL") or 0.0))
            )

        nets: dict[str, dict] = {}
        held: dict[str, IronbeamContract] = {}
        for root, lots in by_root.items():
            # Normally one contract per root. During a roll there can be two:
            # report the net, and trade/close the larger one.
            main = max(lots, key=lambda lot: abs(lot[1]))
            if len(lots) > 1:
                logger.warning(
                    f"[Ironbeam] Account holds {root} in {len(lots)} contracts "
                    f"({', '.join(lot[0] for lot in lots)}). Reporting the net and trading "
                    f"{main[0]}; close the others by hand."
                )
            nets[root] = {
                "qty": sum(lot[1] for lot in lots),
                "avg_price": main[2],
                "unreal_pnl": sum(lot[3] for lot in lots),
            }
            held[root] = IronbeamContract(exch_sym=main[0], root=root)
        self._held = held
        self._known_positions = known
        return nets

    def get_open_positions(self) -> list[dict]:
        return [
            {
                "symbol": root,
                "pos": net["qty"],
                "avg_cost": net["avg_price"],
                "unreal_pnl": net["unreal_pnl"],
            }
            for root, net in self._net_positions().items()
        ]

    def get_positions_for_sync(self) -> list[dict]:
        rows = []
        for root, net in self._net_positions().items():
            qty = net["qty"]
            if abs(qty) <= 1e-12:
                continue
            rows.append(
                {
                    "symbol": root,
                    "qty": qty,
                    "qty_available": abs(qty),
                    "side": "long" if qty > 0 else "short",
                    "avg_price": net["avg_price"],
                }
            )
        return rows


def _given_reason(leg: dict, order: dict) -> str:
    """The reason Ironbeam gave for a failed order, or ``""``."""
    for src in (leg, order):
        for key in ("statusMsg", "statusReason", "rejectReason", "reason", "text", "message"):
            if src.get(key):
                return str(src[key])
    return ""


def _reject_reason(leg: dict, order: dict) -> str:
    """Best-effort reason for a failed order; falls back to the raw order so it is never lost."""
    return _given_reason(leg, order) or f"no reason field; order={order or leg}"


def _result(
    order_id: str,
    contract: IronbeamContract,
    side: str,
    qty: float,
    avg: object,
    status: str,
    leg: dict,
) -> OrderResult:
    return OrderResult(
        id=order_id,
        symbol=contract.exch_sym,
        action=side,
        qty=float(qty),
        price=float(avg) if avg is not None else None,
        status=status,
        raw=leg,
    )
