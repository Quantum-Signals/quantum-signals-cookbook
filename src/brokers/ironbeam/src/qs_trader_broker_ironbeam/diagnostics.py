"""Read-only checks and logs that explain why Ironbeam accepts or rejects an order.

Nothing here places, changes or blocks an order, and nothing raises: a check
that fails is logged and trading carries on. Every read uses a short timeout,
so a slow Ironbeam delays an order by about one timeout.

Why it exists: Ironbeam rejected silver orders without a reason while the
account looked well funded. All 17 silver orders that day fit one rule: accepted
when the maintenance margin *after* the order stayed under the cash the day
started with ($50,000), rejected above it. ``netLiquidity`` did not predict it.
That rule is inferred, not documented, so each order logs what it predicts and
whether the prediction held.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from loguru import logger

if TYPE_CHECKING:
    from .broker import IronbeamBroker, IronbeamContract

TIMEOUT_S = 5.0
# Resting size this close to the best price counts as available to a market order.
NEAR_PCT = 0.005
THIN_CONTRACTS = 10
# Older than a long weekend: the account was not processed, or was restored.
STALE_START_OF_DAY_DAYS = 4
_TIERS = ("marginO", "marginOW", "marginOWI")


def _num(value: Any) -> Optional[float]:
    return float(value) if value is not None else None


def _usd(value: Optional[float]) -> str:
    return "?" if value is None else f"${value:,.0f}"


def _tier(balance: dict, name: str) -> dict:
    return (balance.get("marginInfo") or {}).get(name) or {}


def _utc(ms: Any) -> Optional[datetime]:
    return datetime.fromtimestamp(float(ms) / 1000, timezone.utc) if ms else None


def near_size(levels: list[dict]) -> tuple[Optional[float], int]:
    """Best price and the contracts resting within ``NEAR_PCT`` of it."""
    priced = [x for x in levels if x.get("price") is not None]
    if not priced:
        return None, 0
    best = float(priced[0]["price"])
    size = sum(
        int(x.get("size") or 0) + int(x.get("impliedSize") or 0)
        for x in priced
        if abs(float(x["price"]) - best) <= abs(best) * NEAR_PCT
    )
    return best, size


def margin_after(
    margin_now: float, per_contract: float, position: float, side: str, quantity: int
) -> tuple[float, int]:
    """Estimated margin once the order fills, and how many contracts it opens.

    Contracts that reduce the position free margin and the rest add it. Offsets
    between products (short gold against long silver) are ignored, so the
    estimate can run high.
    """
    signed = quantity if side == "BUY" else -quantity
    closing = int(min(quantity, abs(position))) if position * signed < 0 else 0
    opening = quantity - closing
    return max(0.0, margin_now + (opening - closing) * per_contract), opening


@dataclass
class OrderCheck:
    """What was known just before an order was sent."""

    symbol: str
    side: str
    quantity: int
    position: Optional[float] = None
    opening: Optional[int] = None
    best: Optional[float] = None
    near: Optional[int] = None
    margin_now: Optional[float] = None
    margin_after: Optional[float] = None
    start_cash: Optional[float] = None
    trading_state: str = ""

    @property
    def predicted(self) -> Optional[str]:
        """``"accept"`` / ``"reject"`` under the margin rule, ``None`` when unknown."""
        if self.opening == 0:
            return "accept"
        if self.margin_after is None or self.start_cash is None:
            return None
        return "reject" if self.margin_after > self.start_cash else "accept"

    def causes(self) -> list[str]:
        """Explanations for a rejection that fit what was known before the order."""
        out: list[str] = []
        if self.trading_state not in ("", "ENABLED") and self.opening != 0:
            out.append(f"account trading state is {self.trading_state}")
        if self.predicted == "reject":
            out.append(
                f"margin: about {_usd(self.margin_after)} maintenance after the order is above the "
                f"{_usd(self.start_cash)} start-of-day cash"
            )
        if self.near is not None and self.near < self.quantity:
            out.append(f"thin book: only {self.near} contract(s) within {NEAR_PCT:.1%} of the best price")
        return out


@dataclass
class Diagnostics:
    broker: "IronbeamBroker"
    start_cash: Optional[float] = None
    trading_state: str = ""
    # Contracts held at startup, checked alongside the ones the bot will trade.
    held_symbols: list[str] = field(default_factory=list)
    _start_cash_day: object = None
    _margins: dict[str, tuple[object, float]] = field(default_factory=dict)
    _rejections: dict[str, int] = field(default_factory=dict)

    # ------------------------------------------------------------------ reads

    def _get(self, path: str, **params: str) -> dict:
        return self.broker._request("GET", path, params=params, timeout=TIMEOUT_S, network_retry=False)

    def _balance(self, kind: Optional[str] = None) -> dict:
        params = {"accountId": self.broker._account_id}
        if kind:
            params["type"] = kind
        rows = self._get("/account/balances", **params).get("balances") or []
        return next((b for b in rows if b.get("currencyCode") == "USD"), rows[0] if rows else {})

    def _refresh_start_cash(self) -> dict:
        today = datetime.fromtimestamp(self.broker._clock()).date()
        if self._start_cash_day == today and self.start_cash is not None:
            return {}
        start = self._balance("START_OF_DAY")
        self.start_cash = _num(start.get("cashBalance"))
        self._start_cash_day = today
        return start

    def _refresh_state(self) -> dict:
        risks = self._get("/risk/riskInfo", accountId=self.broker._account_id).get("risks") or []
        risk = risks[0] if risks else {}
        self.trading_state = str(risk.get("tradingState") or "")
        return risk

    def _maint_per_contract(self, symbol: str, side: str) -> Optional[float]:
        today = datetime.fromtimestamp(self.broker._clock()).date()
        key = f"{symbol}:{side}"
        cached = self._margins.get(key)
        if cached is not None and cached[0] == today:
            return cached[1]
        rows = self._get("/reference/securityMargin", symbols=symbol).get("securityMarginAndValues") or []
        row = next((r for r in rows if r.get("exchSym") == symbol), {})
        value = _num(row.get("maintMarginLong" if side == "BUY" else "maintMarginShort"))
        if value is not None:
            self._margins[key] = (today, value)
        return value

    # ---------------------------------------------------------------- startup

    def account_check(self) -> None:
        """Log the account's state, margin room and open positions. Never raises."""
        for name, check in (
            ("account state", self._check_state),
            ("margin", self._check_margin),
            ("positions", self._check_positions),
            ("working orders", self._check_working_orders),
        ):
            try:
                check()
            except Exception as exc:
                logger.warning(f"[Ironbeam] CHECK {name}: could not read ({type(exc).__name__}: {exc})")

    def _check_state(self) -> None:
        risk = self._refresh_state()
        logger.info(
            f"[Ironbeam] CHECK account state: {self.trading_state or 'unknown'}  "
            f"liquidationValue={risk.get('liquidationValue')}  "
            f"startNetLiq={_usd(_num(risk.get('startNetLiquidity')))}  "
            f"liquidationEvents={risk.get('liquidationEvents') or 'none'}"
        )
        if self.trading_state not in ("", "ENABLED"):
            logger.error(
                f"[Ironbeam] CHECK account state is {self.trading_state}: Ironbeam will refuse orders "
                "that add risk"
            )

    def _check_margin(self) -> None:
        now = self._balance()
        start = self._refresh_start_cash()
        tier = _tier(now, "marginO")
        maint, initial = _num(tier.get("maintenanceTotalMargin")), _num(tier.get("initialTotalMargin"))
        net_liq = _num(now.get("netLiquidity"))
        room = self.start_cash - maint if self.start_cash is not None and maint is not None else None
        logger.info(
            f"[Ironbeam] CHECK margin: cash={_usd(_num(now.get('cashBalance')))}  "
            f"startOfDayCash={_usd(self.start_cash)}  netLiq={_usd(net_liq)}  "
            f"initial={_usd(initial)}  maint={_usd(maint)}  "
            f"room before rejections (startOfDayCash - maint, inferred rule)={_usd(room)}"
        )
        if room is not None and room <= 0:
            logger.warning("[Ironbeam] CHECK margin: no room left, orders that add risk will likely be rejected")
        if net_liq is not None and maint is not None and net_liq < maint:
            logger.warning(
                f"[Ironbeam] CHECK margin: net liquidity {_usd(net_liq)} is below the {_usd(maint)} "
                "maintenance margin of the open positions"
            )
        for name in _TIERS:
            error = _tier(now, name).get("marginError")
            if error and error != "NONE":
                logger.warning(f"[Ironbeam] CHECK margin: {name}.marginError={error}")
        as_of = _utc(_tier(start, "marginO").get("asOfTime"))
        if as_of is not None:
            age_days = (self.broker._clock() - as_of.timestamp()) / 86_400
            logger.info(f"[Ironbeam] CHECK start-of-day balance is dated {as_of:%Y-%m-%d}")
            if age_days > STALE_START_OF_DAY_DAYS:
                logger.warning(
                    f"[Ironbeam] CHECK start-of-day balance is {age_days:.0f} days old: Ironbeam may have "
                    "restored an older copy of this account. Compare the positions with the platform."
                )

    def _check_positions(self) -> None:
        rows = self.broker._read_positions(timeout=TIMEOUT_S, network_retry=False)
        held: dict[str, list[float]] = {}
        dates: dict[str, set[str]] = {}
        for p in rows:
            symbol, quantity = str(p.get("exchSym") or ""), float(p.get("quantity") or 0)
            if not symbol or quantity <= 0:
                continue
            signed = -quantity if str(p.get("side", "")).upper() == "SHORT" else quantity
            total = held.setdefault(symbol, [0.0, 0.0])
            total[0] += signed
            total[1] += float(p.get("unrealizedPL") or 0)
            dates.setdefault(symbol, set()).add(str(p.get("dateOpened") or "?"))
        self.held_symbols = list(held)
        if not held:
            logger.info("[Ironbeam] CHECK positions: none reported")
        for symbol, (quantity, pnl) in held.items():
            logger.info(
                f"[Ironbeam] CHECK position {symbol} {quantity:+g}  uPnL={_usd(pnl)}  "
                f"opened {', '.join(sorted(dates[symbol]))}"
            )

    def _check_working_orders(self) -> None:
        orders = self._get(
            "/order/updates", accountId=self.broker._account_id, orderState="ACTIVE"
        ).get("orders") or []
        if not orders:
            logger.info("[Ironbeam] CHECK working orders: none")
        for order in orders:
            for leg in order.get("legs") or []:
                logger.warning(
                    f"[Ironbeam] CHECK working order {order.get('orderId')}: {leg.get('side')} "
                    f"{leg.get('quantity')} {leg.get('exchSym')} {leg.get('status')}"
                )

    def contract_check(self, symbols: list[str]) -> list[str]:
        """Log each contract's state, book, margin and expiry; return the warnings."""
        warnings: list[str] = []
        symbols = list(dict.fromkeys([*self.held_symbols, *symbols]))[:10]
        if not symbols:
            return warnings
        data: dict[str, dict[str, dict]] = {}
        for name, path, key in (
            ("status", "/reference/securityStatus", "securityStatuses"),
            ("margin", "/reference/securityMargin", "securityMarginAndValues"),
            ("depth", "/market/depth", "depths"),
            ("definition", "/reference/securityDefinitions", "securityDefinitions"),
        ):
            try:
                rows = self._get(path, symbols=",".join(symbols)).get(key) or []
                data[name] = {str(r.get("exchSym")): r for r in rows}
            except Exception as exc:
                logger.warning(f"[Ironbeam] CHECK contract {name}: could not read ({type(exc).__name__}: {exc})")
        now = datetime.fromtimestamp(self.broker._clock(), timezone.utc)
        for symbol in symbols:
            status = data.get("status", {}).get(symbol, {}).get("status", "unknown")
            margin = data.get("margin", {}).get(symbol, {})
            expiry = _utc(data.get("definition", {}).get(symbol, {}).get("expirationTime"))
            book = data.get("depth", {}).get(symbol, {})
            bid, bid_size = near_size(book.get("bids") or [])
            ask, ask_size = near_size(book.get("asks") or [])
            logger.info(
                f"[Ironbeam] CHECK contract {symbol}: {status}  bid={bid} x{bid_size}  ask={ask} x{ask_size} "
                f"(contracts within {NEAR_PCT:.1%})  maint={_usd(_num(margin.get('maintMarginLong')))}/contract  "
                f"expires={f'{expiry:%Y-%m-%d %H:%M} UTC' if expiry else '?'}"
            )
            if expiry is not None:
                days = (expiry - now).total_seconds() / 86_400
                if days <= self.broker._settings.roll_days:
                    warnings.append(f"{symbol} expires in {days:.1f} day(s) ({expiry:%Y-%m-%d %H:%M} UTC)")
            if "depth" in data and min(bid_size, ask_size) < THIN_CONTRACTS:
                warnings.append(
                    f"{symbol} is thin: {bid_size} bid / {ask_size} offered near the best price, so market "
                    "orders can fill far away or be rejected"
                )
        return warnings

    # ----------------------------------------------------------------- orders

    def before_order(self, contract: "IronbeamContract", side: str, quantity: int) -> OrderCheck:
        """Log margin room and the book before an order. Never raises, never blocks."""
        check = OrderCheck(contract.exch_sym, side, quantity, trading_state=self.trading_state)
        known = self.broker._known_positions
        if known is not None:
            check.position = known.get(contract.exch_sym, 0.0)
        cash = net_liq = None
        try:  # stops at the first failed read, so a slow Ironbeam costs one timeout
            book = self._get("/market/depth", symbols=contract.exch_sym).get("depths") or []
            row = next((d for d in book if d.get("exchSym") == contract.exch_sym), {})
            check.best, check.near = near_size(row.get("asks" if side == "BUY" else "bids") or [])
            balance = self._balance()
            cash, net_liq = _num(balance.get("cashBalance")), _num(balance.get("netLiquidity"))
            check.margin_now = _num(_tier(balance, "marginO").get("maintenanceTotalMargin"))
            self._refresh_start_cash()
            check.start_cash = self.start_cash
            per_contract = self._maint_per_contract(contract.exch_sym, side)
            if check.position is not None and check.margin_now is not None and per_contract is not None:
                check.margin_after, check.opening = margin_after(
                    check.margin_now, per_contract, check.position, side, quantity
                )
        except Exception as exc:
            logger.warning(f"[Ironbeam] PRE-ORDER check incomplete ({type(exc).__name__}: {exc})")
        position = "?" if check.position is None else f"{check.position:+g}"
        logger.info(
            f"[Ironbeam] PRE-ORDER {side} {quantity} {contract.exch_sym}  position={position}  "
            f"best={check.best} x{check.near}  maint {_usd(check.margin_now)} → about {_usd(check.margin_after)}  "
            f"startOfDayCash={_usd(check.start_cash)} cash={_usd(cash)} netLiq={_usd(net_liq)}  "
            f"expect={check.predicted or 'unknown'}"
        )
        return check

    def after_order(self, check: OrderCheck, order_id: str, accepted: bool, avg: Any = None, reason: str = "") -> None:
        """Log how the order ended against the prediction. Never raises."""
        try:
            actual = "accept" if accepted else "reject"
            match = "unknown" if check.predicted is None else ("yes" if check.predicted == actual else "NO")
            if accepted:
                self._rejections.pop(check.symbol, None)
                slip = ""
                if avg is not None and check.best is not None:
                    slip = f"  vs best before order {check.best} ({float(avg) - check.best:+g})"
                logger.info(
                    f"[Ironbeam] ORDER RESULT {order_id} {check.side} {check.quantity} {check.symbol} accepted"
                    f"{f'  avg fill {avg}' if avg is not None else ''}{slip}  prediction correct={match}"
                )
                return
            count = self._rejections[check.symbol] = self._rejections.get(check.symbol, 0) + 1
            causes = [f"Ironbeam's reason: {reason}"] if reason else []
            causes += check.causes() + self._causes_now(check.symbol)
            logger.error(
                f"[Ironbeam] ORDER RESULT {order_id} {check.side} {check.quantity} {check.symbol} REJECTED  "
                f"(rejection {count} in a row for this contract)  prediction correct={match}  causes that fit: "
                f"{'; '.join(causes) or 'none, send this order id to Ironbeam support'}"
            )
        except Exception as exc:
            logger.warning(f"[Ironbeam] Could not log the result of order {order_id}: {exc}")

    def _causes_now(self, symbol: str) -> list[str]:
        """Account state, margin tiers and contract state read right after a rejection."""
        causes: list[str] = []
        try:
            before = self.trading_state
            self._refresh_state()
            logger.error(f"[Ironbeam] AFTER REJECTION account state={self.trading_state or 'unknown'}")
            if self.trading_state not in ("", "ENABLED") and self.trading_state != before:
                causes.append(f"account trading state is now {self.trading_state}")
        except Exception as exc:
            logger.warning(f"[Ironbeam] AFTER REJECTION account state unreadable: {exc}")
        try:
            balance = self._balance()
            parts = []
            for name in _TIERS:
                tier = _tier(balance, name)
                error = tier.get("marginError")
                parts.append(
                    f"{name}: initial={_usd(_num(tier.get('initialTotalMargin')))} "
                    f"maint={_usd(_num(tier.get('maintenanceTotalMargin')))} error={error}"
                )
                if error and error != "NONE":
                    causes.append(f"{name}.marginError={error}")
            logger.error(f"[Ironbeam] AFTER REJECTION {'  |  '.join(parts)}")
        except Exception as exc:
            logger.warning(f"[Ironbeam] AFTER REJECTION margin unreadable: {exc}")
        try:
            rows = self._get("/reference/securityStatus", symbols=symbol).get("securityStatuses") or []
            status = str((rows[0] if rows else {}).get("status") or "unknown")
            logger.error(f"[Ironbeam] AFTER REJECTION {symbol} session status={status}")
            if status not in ("OPEN", "unknown", "UNKNOWN"):
                causes.append(f"{symbol} session is {status}")
        except Exception as exc:
            logger.warning(f"[Ironbeam] AFTER REJECTION contract status unreadable: {exc}")
        return causes
