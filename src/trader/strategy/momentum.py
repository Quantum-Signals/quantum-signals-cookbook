from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from requests.exceptions import ReadTimeout, ConnectionError
from urllib3.exceptions import ProtocolError
from trader.config import Settings
from qs_trader_broker_core.base import BrokerBase, normalize_position_side
from qs_trader_symbols import (
    canonicalize,
    is_symbol_allowed,
    parse_allowed_symbols,
    position_symbol_to_canonical,
    trading_targets,
)
from trader.models import PerformanceTracker, Signal, sig_label
from trader.strategy.base import StrategyBase

_TRADING_MINUTES_PER_DAY = 390  # 6.5 hours for US equities

# Positions at or below this size are treated as flat when deciding whether an
# open leg may follow a close leg. Alpaca often retains sub-lot dust after
# fractional unwinds; anything at or below this is closeable but not blocking.
_DUST_QTY = 1e-4
# Broker can still report positions below _DUST_QTY (e.g. 1e-8 short); those
# must still be closed before opening the opposite side.
_BROKER_EPS = 1e-9

def _compute_order_qty(
    interval_minutes: int,
    price: float,
    target_daily_notional: float,
    use_fractional: bool = False,
    multiplier: float = 1.0,
) -> float:
    """Return the per-execution order size so a symbol's daily notional ≈ target_daily_notional.

    With a 5-min interval there are 78 executions/day.  At price $545 (SPY)
    and a $50k per-symbol budget:
      fractional:   qty = 50_000 / (78 × 545) ≈ 1.1776 shares
      whole-share:  qty = round(1.1776) = 1 share

    Doubling the interval halves executions/day and doubles qty, keeping
    daily notional constant.  *multiplier* is the futures point value
    (MES=5, MNQ=2); equities stay 1.
    """
    executions_per_day = _TRADING_MINUTES_PER_DAY // interval_minutes
    point_value = price * (multiplier if multiplier > 0 else 1.0)
    if executions_per_day <= 0 or point_value <= 0:
        return 0.0001 if use_fractional else 1.0
    raw = target_daily_notional / (executions_per_day * point_value)
    if use_fractional:
        return round(raw, 4)  # Alpaca accepts up to 9 d.p.; 4 is plenty
    return float(max(1, round(raw)))


@dataclass(frozen=True)
class _Leg:
    """One order needed to move the broker position toward the target."""

    action: str  # "BUY" | "SELL"
    qty: float  # unsigned
    intent: str  # "close" | "open"


class MomentumStrategy(StrategyBase):
    """
    Incremental momentum strategy that decides and executes at a
    user-configurable interval (default 5 minutes).

    At each interval the latest signal adjusts the net position by
    *qty* units sized so each symbol's daily notional ≈ $50 000
    (≈ $100 000 total across ES→SPY and NQ→QQQ):

        UP     → BUY  qty  (net position += qty)
        DOWN   → SELL qty  (net position -= qty)
        STABLE → hold      (no trade, timer still resets)

    Execution reconciles to a target rather than trading blind deltas.
    Every execution:

      1. fetches the broker's actual position (the only source of truth),
      2. computes the feasible target = broker position + reserve + slice,
         snapped to a whole share on the short side (Alpaca forbids
         fractional shorts, code 42210000),
      3. derives at most two legs (close the opposite side, then open) via
         ``_plan_legs``, and
      4. places them; if a leg fails, the remainder of the slice is dropped
         and the next execution re-plans from broker truth.

    Because every tick re-reads the broker, state drift (failed orders,
    manual trades, partial fills, restarts) self-heals instead of compounding.

    The per-symbol ``_position_reserve`` carries the fractional remainder
    between the ideal (continuous) position and the whole-share position the
    broker can hold on the short side; it drains naturally as the ideal
    crosses integer boundaries.  Invariant: ideal = broker + reserve, with
    reserve = 0 while the ideal is non-negative.
    """

    def __init__(
        self,
        broker: BrokerBase,
        perf: PerformanceTracker,
        settings: Settings,
    ) -> None:
        self.broker = broker
        self.perf = perf
        self._settings = settings

        self._interval_minutes = settings.execution_interval_minutes
        self._interval_s = self._interval_minutes * 60
        # Broker capability, not a broker name: any broker that declares
        # supports_fractional gets fractional sizing (see BrokerBase).
        self._use_fractional = broker.supports_fractional

        self._allowed = parse_allowed_symbols(settings.allowed_symbols)

        active_symbols = trading_targets(self._allowed)
        n_symbols = max(len(active_symbols), 1)
        self._target_daily_notional_per_symbol = settings.daily_start_capital / n_symbols

        # Last known broker position / cost basis, refreshed from the broker
        # on every execution and by sync_positions().  Used for STABLE logging
        # and as a PnL fallback — never as the basis for order sizing.
        self._net_qty: dict[str, float] = {}
        self._total_cost: dict[str, float] = {}
        self._last_exec_time: dict[str, float] = {}
        self._position_reserve: dict[str, float] = {}
        self._last_sync_time: float = 0.0

        execs_per_day = _TRADING_MINUTES_PER_DAY // self._interval_minutes
        logger.info(
            f"MomentumStrategy: interval={self._interval_minutes}min  "
            f"execs/day={execs_per_day}  "
            f"symbols={n_symbols}  "
            f"target_per_symbol=${self._target_daily_notional_per_symbol:,.0f}  "
            f"total_target=${settings.daily_start_capital:,.0f}  "
            f"fractional={'yes' if self._use_fractional else 'no (whole-share)'}"
        )

    # ------------------------------------------------------------------
    # Multi-day lifecycle
    # ------------------------------------------------------------------
    def reset_for_new_day(self) -> None:
        """Drop yesterday's net-position / cost-basis / interval timers.

        Called by the supervisor after EOD flattening so a fresh session
        starts from a clean slate.
        """
        self._net_qty.clear()
        self._total_cost.clear()
        self._last_exec_time.clear()
        self._position_reserve.clear()
        logger.debug("MomentumStrategy: per-day state cleared")

    # ------------------------------------------------------------------
    # Position sync
    # ------------------------------------------------------------------
    def sync_positions(self) -> None:
        """Reconcile the bot's position state with what the broker actually holds.

        Clears any stale entries first so positions that have disappeared at the
        broker (e.g. EOD orders that filled overnight, manual closes) no longer
        linger in strategy state. Cooldown timers in ``_last_exec_time`` are
        preserved.

        State is only cleared after a successful broker fetch so a network
        failure cannot leave the strategy thinking it is flat while the broker
        still holds positions (July 2026).
        """
        now = time.perf_counter()
        if now - self._last_sync_time < 5.0:
            logger.debug("Skipping sync — already synced within last 5 seconds")
            return
        self._last_sync_time = now

        pos_list = []
        for attempt in range(3):
            try:
                pos_list = self.broker.get_positions_for_sync()
                break
            except (ReadTimeout, ConnectionError, ProtocolError) as e:
                if attempt < 2:
                    logger.warning(f"Network error during sync_positions. Retrying {attempt + 1}/3...")
                    time.sleep(2)
                else:
                    logger.error("Failed to sync positions after 3 attempts. Aborting.")
                    raise e

        self._net_qty.clear()
        self._total_cost.clear()
        self._position_reserve.clear()

        for p in pos_list:
            etf_sym = p["symbol"]
            # Internal state is keyed by QS signal symbol ("ES"/"NQ"), not the
            # ETF ticker ("SPY"/"QQQ").  Map back so handle_signal() finds it.
            sig_sym = position_symbol_to_canonical(etf_sym) or etf_sym
            qty = float(p["qty"])
            avg_price = p["avg_price"]
            if qty == 0:
                continue
            if abs(qty) <= _DUST_QTY and qty < 0:
                logger.info(
                    f"  Ignoring dust SHORT {_fmt_qty(abs(qty))}x {etf_sym} "
                    f"@ {avg_price:.2f} — treating as flat"
                )
                continue
            self._net_qty[sig_sym] = qty
            self._total_cost[sig_sym] = qty * avg_price
            label = "LONG" if qty > 0 else "SHORT"
            logger.info(f"  Synced {label} {_fmt_qty(abs(qty))}x {etf_sym} @ {avg_price:.2f} from broker")
        if pos_list:
            logger.info(f"Synced {len(pos_list)} existing position(s) from broker")
        else:
            logger.info("No existing positions at broker")

    # ------------------------------------------------------------------
    # Signal handler — gated to one execution per N minutes per symbol
    # ------------------------------------------------------------------
    def handle_signal(self, signal: object) -> None:
        sym = signal.symbol
        sig = signal.signal
        ts = signal.signal_time_utc

        if signal.is_null_signal():
            logger.debug(f"Signal  {sym}  model={signal.model}  -- {signal.code_message}")
            return

        if not is_symbol_allowed(sym, self._allowed):
            logger.debug(f"  -> {sym} not in ALLOWED_SYMBOLS — skipping")
            return

        canon = canonicalize(sym)
        if canon is None:
            # Passthrough symbol (not in catalog) — broker resolves directly.
            canon = sym

        self.perf.signal_counts[sig] += 1
        logger.info(f"Signal  {sym}  {sig_label(sig)}  model={signal.model}  ts={ts}")

        # Gate on the signal's own timestamp rather than wall-clock time.
        # S&P500 and Nasdaq100 signals for the same QS bar share an identical
        # signal_time_utc, so both always evaluate the cooldown against the
        # same reference — regardless of when their SSE events arrived or how
        # long the previous symbol's order took to fill.
        # Floor to whole seconds: QS timestamps can carry sub-second fractions
        # that aren't visible in the log but cause the 5-min boundary to be
        # missed by <1 s, making the effective interval 6 min instead of 5 min.
        signal_ts = math.floor(_signal_ts_epoch(ts))
        elapsed = signal_ts - self._last_exec_time.get(canon, 0.0)
        if elapsed < self._interval_s:
            remaining = self._interval_s - elapsed
            logger.debug(
                f"  -> Next execution for {canon} in {remaining:.0f}s — skipping"
            )
            return

        if sig == Signal.STABLE:
            net = self._net_qty.get(canon, 0.0)
            logger.info(f"  -> STABLE — holding {canon} (net: {_fmt_signed_qty(net)})")
            self._last_exec_time[canon] = signal_ts
            return

        self._execute(canon, sym, sig, signal_ts)

    # ------------------------------------------------------------------
    # Execution — reconcile the broker position to the target
    # ------------------------------------------------------------------
    def _execute(self, canon: str, qs_symbol: str, sig: int, signal_ts: float) -> None:
        """Move the broker position for *canon* toward this tick's target.

        ``signal_ts`` is the QS signal's UTC epoch (seconds since 1970) and is
        stored in ``_last_exec_time`` so the next cooldown check compares
        signal timestamps — not wall-clock arrival times.
        """
        contract = self.broker.resolve_contract(qs_symbol)
        if contract is None:
            logger.warning(
                f"  -> No tradable contract for {qs_symbol} — skipping "
                f"(check symbol map / contract month / IB permissions)"
            )
            return

        pos_sym = _position_symbol(contract, qs_symbol)
        label = _instrument_label(contract, qs_symbol)

        price = self._fetch_price(label, contract)
        if price is None:
            logger.warning(f"  -> No market data for {label} — skipping")
            return

        slice_qty = _compute_order_qty(
            self._interval_minutes, price,
            self._target_daily_notional_per_symbol,
            self._use_fractional,
            self._settings.contract_multipliers.get(pos_sym, 1.0),
        )
        signed_slice = slice_qty if sig == Signal.UP else -slice_qty

        self._last_exec_time[canon] = signal_ts

        # The broker is the only source of truth for the current position.
        current, avail_close, avg_entry = _broker_state(self.broker, pos_sym)

        known = self._net_qty.get(canon, 0.0)
        if abs(current - known) > _DUST_QTY:
            logger.warning(
                f"  -> {label} drift: strategy believed "
                f"{_fmt_signed_qty(known)}, broker holds {_fmt_signed_qty(current)} "
                f"— planning from broker"
            )

        reserve = self._position_reserve.get(canon, 0.0)
        ideal = current + reserve + signed_slice
        target, new_reserve = _snap_target(ideal, self._use_fractional)
        self._position_reserve[canon] = new_reserve

        legs = _plan_legs(current, target, avail_close, self._use_fractional)

        if not legs:
            direction = "short" if signed_slice < 0 else "long"
            logger.info(
                f"  -> No order for {label}: accumulating {direction} intent "
                f"(reserve={_fmt_signed_qty(new_reserve)}, "
                f"net stays at {_fmt_signed_qty(current)})"
            )
            self._settle(canon, price, current, avg_entry, current)
            return

        new_net = current
        prev_intent: Optional[str] = None
        for leg in legs:
            if leg.intent == "open" and prev_intent == "close":
                if not _wait_opposite_cleared(self.broker, pos_sym, leg.action):
                    logger.warning(
                        f"  -> Broker still shows the opposite position for "
                        f"{pos_sym} after close — skipping open leg"
                    )
                    break
            trade = self.broker.place_order(
                contract, leg.action, leg.qty, price, position_intent=leg.intent,
            )
            if trade is None:
                logger.warning(
                    f"  -> {leg.action} {_fmt_qty(leg.qty)}x {label} failed — "
                    f"dropping the rest of this slice; next interval re-plans "
                    f"from the broker position"
                )
                break
            signed_qty = leg.qty if leg.action == "BUY" else -leg.qty
            multiplier = self._settings.contract_multipliers.get(pos_sym, 1.0)
            logger.info(
                f"  -> {leg.action} {_fmt_qty(leg.qty)}x {label} @ {price:.2f}  "
                f"notional=${leg.qty * price * multiplier:,.2f}  "
                f"({_leg_label(leg)})  [signal={canon}]  "
                f"(net: {_fmt_signed_qty(new_net)} → {_fmt_signed_qty(new_net + signed_qty)})"
            )
            new_net += signed_qty
            prev_intent = leg.intent

        self._settle(canon, price, current, avg_entry, new_net)

        if abs(new_reserve) > 1e-9:
            logger.info(f"  -> (reserve: {_fmt_signed_qty(new_reserve)})")

    def _fetch_price(self, trade_sym: str, contract: object) -> Optional[float]:
        """Fetch the last price, retrying on dropped/stale connections."""
        for attempt in range(3):
            try:
                return self.broker.get_last_price(contract)
            except (ConnectionError, ProtocolError):
                if attempt < 2:
                    logger.warning(
                        f"  -> Connection dropped fetching {trade_sym} price. "
                        f"Retrying {attempt + 1}/3..."
                    )
                    time.sleep(2)
                else:
                    logger.error(
                        f"  -> Failed to fetch price for {trade_sym} after 3 attempts."
                    )
        return None

    def _settle(
        self,
        sym: str,
        price: float,
        prev_net: float,
        avg_entry: Optional[float],
        new_net: float,
    ) -> None:
        """Update PnL and cached state after this tick's fills.

        ``prev_net`` is the broker position before the tick's orders and
        ``new_net`` the position implied by the fills that succeeded.
        """
        goes_flat = abs(prev_net) > _DUST_QTY and abs(new_net) <= _DUST_QTY
        crosses_zero = (
            (prev_net > _DUST_QTY and new_net < -_DUST_QTY)
            or (prev_net < -_DUST_QTY and new_net > _DUST_QTY)
        )

        if goes_flat or crosses_zero:
            entry = avg_entry
            if entry is None:
                known = self._net_qty.get(sym, 0.0)
                cost = self._total_cost.get(sym, 0.0)
                entry = cost / known if abs(known) > _BROKER_EPS else price
            pnl = (price - entry) * prev_net
            direction = Signal.UP if prev_net > 0 else Signal.DOWN
            self.perf.record_close(sym, pnl, direction)
            logger.info(
                f"  -> Closed {'LONG' if prev_net > 0 else 'SHORT'} "
                f"{_fmt_qty(abs(prev_net))}x {sym}  est.PnL={pnl:+.2f}"
            )
            logger.info(f"  -> {self.perf.summary()}")

        self._net_qty[sym] = new_net
        if abs(new_net) <= _BROKER_EPS:
            self._total_cost[sym] = 0.0
        elif goes_flat or crosses_zero:
            self._total_cost[sym] = new_net * price
        else:
            entry = avg_entry if avg_entry is not None else price
            self._total_cost[sym] = prev_net * entry + (new_net - prev_net) * price


# ------------------------------------------------------------------
# Reconciliation core
# ------------------------------------------------------------------

def _snap_target(ideal: float, use_fractional: bool) -> tuple[float, float]:
    """Snap the ideal (continuous) position to what the broker can hold.

    Returns ``(target, reserve)`` with ``ideal == target + reserve``.
    Fractional longs are held exactly on Alpaca; shorts snap to the nearest
    whole share (fractional shorts are rejected, code 42210000).  Integer
    brokers round everything and carry no reserve.
    """
    if use_fractional:
        if ideal >= -_BROKER_EPS:
            return max(0.0, ideal), 0.0
        target = float(round(ideal))
        return target, ideal - target
    return float(round(ideal)), 0.0


def _plan_legs(
    current: float,
    target: float,
    avail_close: float,
    use_fractional: bool,
) -> list[_Leg]:
    """Orders that move a broker position from *current* to *target*.

    Pure function of the broker state; returns at most two legs — close the
    opposite side, then open — because Alpaca requires separate
    close/open orders when a position crosses through flat.

    - Close legs are capped to ``avail_close`` (shares not tied up in other
      orders) and floored so they never exceed broker holdings; sub-lot dust
      is still closed via a 9-d.p. fallback.
    - An open leg is emitted only when the opposite side is fully cleared
      (within ``_DUST_QTY``); otherwise the close leg makes partial progress
      and the next tick re-plans.
    - Short opens are whole shares (targets are pre-snapped by
      ``_snap_target``).
    """
    delta = target - current
    if abs(delta) <= _BROKER_EPS:
        return []

    legs: list[_Leg] = []
    if delta < 0:  # selling
        sell_total = -delta
        cur_long = max(current, 0.0)
        closeable = min(cur_long, avail_close)
        close_qty = _floor_leg(min(sell_total, closeable), use_fractional)
        if close_qty > _BROKER_EPS:
            legs.append(_Leg("SELL", close_qty, "close"))
        if target < -_DUST_QTY:
            if cur_long - close_qty > _DUST_QTY:
                return legs  # long not cleared — no short open this tick
            open_qty = -target - max(-current, 0.0)
            if use_fractional:
                open_qty = float(round(open_qty))  # whole-share shorts only
            if open_qty > _BROKER_EPS:
                legs.append(_Leg("SELL", open_qty, "open"))
    else:  # buying
        buy_total = delta
        cur_short = max(-current, 0.0)
        closeable = min(cur_short, avail_close)
        close_qty = _floor_leg(min(buy_total, closeable), use_fractional)
        if close_qty > _BROKER_EPS:
            legs.append(_Leg("BUY", close_qty, "close"))
        if target > _DUST_QTY:
            if cur_short - close_qty > _DUST_QTY:
                return legs  # short not cleared — no long open this tick
            open_qty = target - max(current, 0.0)
            if not use_fractional:
                open_qty = float(round(open_qty))
            if open_qty > _DUST_QTY:
                legs.append(_Leg("BUY", open_qty, "open"))
    return legs


def _floor_leg(qty: float, use_fractional: bool) -> float:
    """Floor a close-leg size so it never exceeds broker holdings.

    Fractional brokers floor at 4 d.p.; sub-0.0001 dust (e.g. 9.9994e-05)
    would floor to 0, so fall back to 9 d.p. to still close it.
    """
    if qty <= _BROKER_EPS:
        return 0.0
    if not use_fractional:
        return float(math.floor(qty + 1e-12))
    floored = _floor_qty(qty, 4)
    if floored <= _BROKER_EPS:
        return _floor_qty(qty, 9)
    return floored


def _position_symbol(contract: object, fallback: str) -> str:
    """Symbol used in broker position dicts for *contract*.

    Alpaca contracts are ticker strings. Simulated contracts use ``.symbol``.
    Other brokers may expose ``position_key`` or ``root``.
    """
    if isinstance(contract, str):
        return contract
    key = getattr(contract, "position_key", None) or getattr(contract, "root", None)
    if key:
        return str(key)
    symbol = getattr(contract, "symbol", None)
    return str(symbol) if symbol else fallback


def _instrument_label(contract: object, fallback: str) -> str:
    """Name to print in logs: the instrument actually sent to the broker.

    Alpaca contracts are ticker strings (``SPY``). Simulated contracts use
    ``.symbol``. Other brokers may expose ``trading_symbol``.
    """
    if isinstance(contract, str):
        return contract
    trading = getattr(contract, "trading_symbol", None)
    if trading:
        return str(trading)
    symbol = getattr(contract, "symbol", None)
    return str(symbol) if symbol else fallback


def _broker_position(broker: BrokerBase, etf_symbol: str) -> dict | None:
    """Return the broker position dict for *etf_symbol*, or None if flat."""
    for attempt in range(3):
        try:
            for p in broker.get_positions_for_sync():
                if p["symbol"] == etf_symbol:
                    return p
            return None
        except (ReadTimeout, ConnectionError, ProtocolError):
            if attempt < 2:
                logger.warning(
                    f"Network error fetching broker positions for {etf_symbol}. "
                    f"Retrying {attempt + 1}/3..."
                )
                time.sleep(2)
            else:
                logger.error(
                    f"Failed to fetch positions for {etf_symbol} after 3 attempts."
                )
                raise


def _broker_state(broker: BrokerBase, etf_symbol: str) -> tuple[float, float, Optional[float]]:
    """Return ``(signed_qty, qty_available, avg_entry_price)`` from the broker.

    ``qty_available`` is unsigned and capped by what the broker reports as
    free to close (shares not tied up in other open orders).
    """
    pos = _broker_position(broker, etf_symbol)
    if pos is None:
        return 0.0, 0.0, None
    qty = float(pos["qty"])
    side = normalize_position_side(pos.get("side"), qty=qty)
    signed = -abs(qty) if side == "short" else abs(qty)
    avail = abs(float(pos.get("qty_available") or 0.0)) or abs(qty)
    avg_raw = pos.get("avg_price")
    avg = float(avg_raw) if avg_raw else None
    return signed, avail, avg


def _wait_opposite_cleared(
    broker: BrokerBase,
    etf_symbol: str,
    open_action: str,
    timeout: float = 3.0,
    poll: float = 1.0,
) -> bool:
    """Poll until the side blocking *open_action* is gone (post-close settle lag).

    A BUY open needs the short cleared; a SELL open needs the long cleared.
    Uses exponential backoff to reduce API calls while catching quick settles.
    """

    def _cleared() -> bool:
        current, _, _ = _broker_state(broker, etf_symbol)
        if open_action == "BUY":
            return current >= -_BROKER_EPS
        return current <= _BROKER_EPS

    if _cleared():
        return True
    deadline = time.perf_counter() + timeout
    wait = poll
    while time.perf_counter() < deadline:
        time.sleep(wait)
        if _cleared():
            return True
        wait = min(wait * 1.5, 2.0)
    return False


# ------------------------------------------------------------------
# Formatting / misc helpers
# ------------------------------------------------------------------

def _signal_ts_epoch(signal_time_utc: object) -> float:
    """Return *signal_time_utc* as a UTC epoch float (seconds since 1970-01-01).

    Handles the three forms the QS client may produce:
      - a tz-aware/naive ``datetime`` object
      - an ``int`` or ``float`` already in epoch seconds
      - an ISO-8601 string (with or without trailing "Z")
    """
    if isinstance(signal_time_utc, datetime):
        dt = (
            signal_time_utc
            if signal_time_utc.tzinfo
            else signal_time_utc.replace(tzinfo=timezone.utc)
        )
        return dt.timestamp()
    if isinstance(signal_time_utc, (int, float)):
        return float(signal_time_utc)
    raw = str(signal_time_utc).replace("Z", "+00:00")
    return datetime.fromisoformat(raw).timestamp()


def _floor_qty(qty: float, decimals: int = 4) -> float:
    """Round down so order size never exceeds broker holdings (float-safe)."""
    if qty <= 0:
        return 0.0
    factor = 10**decimals
    return math.floor(qty * factor + 1e-12) / factor


def _leg_label(leg: _Leg) -> str:
    if leg.intent == "close":
        return "cover" if leg.action == "BUY" else "close"
    return "long" if leg.action == "BUY" else "short"


def _fmt_qty(qty: float) -> str:
    """Format a quantity: integer display for whole numbers, 4 d.p. for fractional."""
    if qty == int(qty):
        return str(int(qty))
    return f"{qty:.4f}"


def _fmt_signed_qty(qty: float) -> str:
    """Like _fmt_qty but always shows the sign (for net position displays)."""
    sign = "+" if qty >= 0 else ""
    return f"{sign}{_fmt_qty(qty)}"


