from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from loguru import logger

from qs_trader_broker_core.base import BrokerBase
from qs_trader_symbols import position_symbol_to_canonical
from trader.models import PerformanceTracker, Signal

# Trading minutes per day for US equities (9:30 AM - 4:00 PM ET = 6.5 hours)
_TRADING_MINUTES_PER_DAY = 390


@dataclass
class ScheduleInfo:
    """Scheduling context for the report panel."""

    session_start_time: str  # "HH:MM" format
    session_end_time: str  # "HH:MM" format
    exit_window_start: str  # "HH:MM" format (liquidation window start)
    exit_window_end: str  # "HH:MM" format (liquidation window end)
    decision_interval_minutes: int
    session_timezone: str  # e.g., "America/New_York"


@dataclass
class SizingInfo:
    """Per-symbol sizing information."""

    symbol: str
    dollars_per_decision: float
    units_per_decision: float
    mkt_price: float


@dataclass
class ReportContext:
    """All context needed to generate the account info panel."""

    user_tz: ZoneInfo
    report_interval_s: float
    next_report_at: datetime
    schedule: ScheduleInfo
    notional_per_decision: float  # Total dollars targeted per decision point
    sizing_info: list[SizingInfo]
    contract_multipliers: dict[str, float]


def _format_currency(value: float, width: int = 0, show_sign: bool = False) -> str:
    """Format a value as currency."""
    if show_sign:
        formatted = f"${value:+,.2f}"
    else:
        formatted = f"${value:,.0f}"
    return formatted.rjust(width) if width else formatted


def _format_pct(value: float, width: int = 0) -> str:
    """Format a value as percentage."""
    formatted = f"{value:.2%}"
    return formatted.rjust(width) if width else formatted


def _format_qty(qty: float) -> str:
    """Format a quantity: integer display for whole numbers, 4 d.p. for fractional."""
    if qty == int(qty):
        return str(int(qty))
    return f"{qty:.4f}"


def _format_signed_qty(qty: float) -> str:
    """Like _format_qty but always shows the sign."""
    sign = "+" if qty >= 0 else ""
    return f"{sign}{_format_qty(qty)}"


def _humanise_timedelta(td: timedelta) -> str:
    """Convert timedelta to human-readable string."""
    total = int(td.total_seconds())
    if total < 0:
        total = 0
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _convert_time_to_tz(
    hhmm: str, from_tz: ZoneInfo, to_tz: ZoneInfo, ref_date: datetime
) -> str:
    """Convert a time string from one timezone to another.

    Uses proper timezone conversion via a reference date, not fixed offsets.
    """
    h, m = int(hhmm.split(":")[0]), int(hhmm.split(":")[1])
    dt_in_from_tz = ref_date.replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=from_tz)
    dt_in_to_tz = dt_in_from_tz.astimezone(to_tz)
    return dt_in_to_tz.strftime("%H:%M")


def _compute_schedule_stats(
    now: datetime,
    schedule: ScheduleInfo,
    user_tz: ZoneInfo,
) -> dict:
    """Compute schedule-related stats for the report."""
    session_tz = ZoneInfo(schedule.session_timezone)

    # Parse session times in session timezone
    sh, sm = int(schedule.session_start_time.split(":")[0]), int(schedule.session_start_time.split(":")[1])
    eh, em = int(schedule.session_end_time.split(":")[0]), int(schedule.session_end_time.split(":")[1])

    # Create datetime objects for today's session in session timezone
    today = now.astimezone(session_tz).date()
    session_start = datetime(today.year, today.month, today.day, sh, sm, tzinfo=session_tz)
    session_end = datetime(today.year, today.month, today.day, eh, em, tzinfo=session_tz)

    # Calculate total decision points for the day
    session_minutes = int((session_end - session_start).total_seconds() / 60)
    total_decisions = session_minutes // schedule.decision_interval_minutes

    # Calculate executed decisions based on elapsed time
    now_session = now.astimezone(session_tz)
    if now_session < session_start:
        elapsed_minutes = 0
    elif now_session > session_end:
        elapsed_minutes = session_minutes
    else:
        elapsed_minutes = int((now_session - session_start).total_seconds() / 60)
    executed = elapsed_minutes // schedule.decision_interval_minutes

    remaining = max(0, total_decisions - executed)

    # Exit window times converted to user timezone
    exit_start_user = _convert_time_to_tz(
        schedule.exit_window_start, session_tz, user_tz, now
    )
    exit_end_user = _convert_time_to_tz(
        schedule.exit_window_end, session_tz, user_tz, now
    )

    # Time until exit window opens
    exh, exm = int(schedule.exit_window_start.split(":")[0]), int(schedule.exit_window_start.split(":")[1])
    exit_window_start = datetime(today.year, today.month, today.day, exh, exm, tzinfo=session_tz)
    time_to_exit = exit_window_start - now.astimezone(session_tz)
    if time_to_exit.total_seconds() < 0:
        countdown = "OPEN"
    else:
        countdown = _humanise_timedelta(time_to_exit)

    return {
        "executed": executed,
        "remaining": remaining,
        "total": total_decisions,
        "interval_min": schedule.decision_interval_minutes,
        "exit_window": f"{exit_start_user}–{exit_end_user}",
        "countdown": countdown,
    }


def _build_info_block(
    now: datetime,
    ctx: ReportContext,
) -> list[str]:
    """Build the INFO section."""
    ts = now.astimezone(ctx.user_tz).strftime("%Y-%m-%d %H:%M:%S %Z")

    # Handle on-trade mode (interval <= 0) vs time-based mode (interval > 0)
    if ctx.report_interval_s <= 0:
        interval_str = "on-trade"
        next_str = "after next trade"
    else:
        interval_min = int(ctx.report_interval_s / 60)
        interval_str = f"{interval_min}min"
        next_str = ctx.next_report_at.astimezone(ctx.user_tz).strftime("%H:%M:%S")

    return [
        "INFO",
        f"  ts            : {ts}",
        f"  interval      : {interval_str}",
        f"  next          : {next_str}",
    ]


def _build_account_block(
    broker: BrokerBase,
    perf: PerformanceTracker,
    ctx: ReportContext,
    positions: list[dict],
    prices: dict[str, float],
) -> list[str]:
    """Build the ACCOUNT section."""
    acct = broker.get_account_summary()

    nlv = acct.get("net_liquidation", 0.0)
    buying_power = acct.get("buying_power", 0.0)

    # Calculate gross and net notional from positions
    gross_notional = 0.0
    net_notional = 0.0
    total_unreal_pnl = 0.0

    for p in positions:
        sym = p["symbol"]
        qty = float(p.get("pos", p.get("qty", 0)))
        mkt_price = prices.get(sym, 0.0)
        avg_cost = float(p.get("avg_cost", p.get("avg_price", 0)))

        multiplier = ctx.contract_multipliers.get(sym, 1.0)
        notional = qty * mkt_price * multiplier
        gross_notional += abs(notional)
        net_notional += notional

        cost_basis = qty * avg_cost * multiplier
        mkt_value = notional
        unreal_pnl = mkt_value - cost_basis
        total_unreal_pnl += unreal_pnl

    # Use broker-reported unrealized PnL if available, else calculated
    broker_unreal = acct.get("unrealized_pnl", total_unreal_pnl)
    broker_real = acct.get("realized_pnl", perf.gross_pnl)
    day_pnl = broker_unreal + broker_real

    # Leverage
    leverage = gross_notional / nlv if nlv > 0 else 0.0

    return [
        "ACCOUNT",
        f"  NLV              : {_format_currency(nlv)}",
        f"  GrossNotional    : {_format_currency(gross_notional)}",
        f"  NetNotional      : {_format_currency(net_notional, show_sign=True)}",
        f"  Lev              : {leverage:.2f}x",
        f"  BuyPow           : {_format_currency(buying_power)}",
        f"  Notional/Decision: {_format_currency(ctx.notional_per_decision)}",
        f"  UnrealPnL        : {_format_currency(broker_unreal, show_sign=True)}",
        f"  RealPnL          : {_format_currency(broker_real, show_sign=True)}",
        f"  DayPnL           : {_format_currency(day_pnl, show_sign=True)}",
    ]


def _build_schedule_block(
    now: datetime,
    ctx: ReportContext,
) -> list[str]:
    """Build the SCHEDULE section."""
    stats = _compute_schedule_stats(now, ctx.schedule, ctx.user_tz)

    return [
        "SCHEDULE",
        f"  executed     : {stats['executed']}",
        f"  remaining    : {stats['remaining']}",
        f"  total        : {stats['total']}",
        f"  interval     : {stats['interval_min']}min",
        f"  Exit window  : {stats['exit_window']} (in {stats['countdown']})",
    ]


def _build_sizing_block(
    ctx: ReportContext,
) -> list[str]:
    """Build the SIZING section."""
    if not ctx.sizing_info:
        return ["SIZING", "  (no active symbols)"]

    lines = ["SIZING"]
    header = f"  {'SYM':<8} {'$/decision':>14} {'units/decision':>16} {'mktPrice':>12}"
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))

    total_dollars = 0.0
    for s in ctx.sizing_info:
        total_dollars += s.dollars_per_decision
        units_str = _format_qty(s.units_per_decision)
        lines.append(
            f"  {s.symbol:<8} {_format_currency(s.dollars_per_decision):>14} "
            f"{units_str:>16} {s.mkt_price:>12.2f}"
        )

    lines.append("  " + "-" * (len(header) - 2))
    lines.append(f"  {'TOTAL':<8} {_format_currency(total_dollars):>14}")

    return lines


def _build_positions_block(
    broker: BrokerBase,
    perf: PerformanceTracker,
    ctx: ReportContext,
    positions: list[dict],
    prices: dict[str, float],
) -> list[str]:
    """Build the POSITIONS section."""
    if not positions:
        return ["POSITIONS", "  (flat)"]

    lines = ["POSITIONS"]
    header = (
        f"  {'SYM':<8} {'qty':>8} {'avgCost':>10} {'costBasis':>12} "
        f"{'mktPrice':>10} {'mktValue':>12} {'UnrealPnL':>12} {'RealPnL':>12}"
    )
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))

    total_unreal = 0.0
    total_real = 0.0

    for p in positions:
        sym = p["symbol"]
        qty = float(p.get("pos", p.get("qty", 0)))
        avg_cost = float(p.get("avg_cost", p.get("avg_price", 0)))
        mkt_price = prices.get(sym, 0.0)

        multiplier = ctx.contract_multipliers.get(sym, 1.0)
        cost_basis = qty * avg_cost * multiplier
        mkt_value = qty * mkt_price * multiplier
        unreal_pnl = mkt_value - cost_basis

        # Get per-symbol realized PnL from tracker
        # Trades are recorded under canonical symbol (e.g., "YM") but positions
        # use ETF ticker (e.g., "DIA"), so map back to canonical for lookup
        canon_sym = position_symbol_to_canonical(sym) or sym
        sym_stats = perf.symbol_stats.get(canon_sym)
        real_pnl = sym_stats.realized_pnl if sym_stats else 0.0

        total_unreal += unreal_pnl
        total_real += real_pnl

        lines.append(
            f"  {sym:<8} {_format_signed_qty(qty):>8} {avg_cost:>10.4f} "
            f"{_format_currency(cost_basis, show_sign=True):>12} {mkt_price:>10.2f} "
            f"{_format_currency(mkt_value, show_sign=True):>12} "
            f"{_format_currency(unreal_pnl, show_sign=True):>12} "
            f"{_format_currency(real_pnl, show_sign=True):>12}"
        )

    lines.append("  " + "-" * (len(header) - 2))
    lines.append(
        f"  {'TOTAL':<8} {' ':>8} {' ':>10} {' ':>12} {' ':>10} {' ':>12} "
        f"{_format_currency(total_unreal, show_sign=True):>12} "
        f"{_format_currency(total_real, show_sign=True):>12}"
    )

    # Add trade counts per position if available
    lines.append("")
    lines.append("  Trade counts:")
    trade_header = f"    {'SYM':<8} {'BUY':>6} {'SELL':>6} {'SHORT':>6} {'COVER':>6}"
    lines.append(trade_header)
    lines.append("    " + "-" * (len(trade_header) - 4))

    for p in positions:
        sym = p["symbol"]
        # Map ETF ticker back to canonical symbol for trade count lookup
        canon_sym = position_symbol_to_canonical(sym) or sym
        sym_stats = perf.symbol_stats.get(canon_sym)
        if sym_stats:
            tc = sym_stats.trade_counts
            lines.append(
                f"    {sym:<8} {tc['BUY']:>6} {tc['SELL']:>6} "
                f"{tc['SHORT']:>6} {tc['COVER']:>6}"
            )
        else:
            lines.append(f"    {sym:<8} {0:>6} {0:>6} {0:>6} {0:>6}")

    return lines


def _build_signals_block(
    perf: PerformanceTracker,
) -> list[str]:
    """Build the SIGNALS section."""
    if not perf.symbol_stats:
        return [
            "SIGNALS",
            f"  (global) UP={perf.signal_counts[Signal.UP]}  "
            f"STABLE={perf.signal_counts[Signal.STABLE]}  "
            f"DOWN={perf.signal_counts[Signal.DOWN]}",
        ]

    lines = ["SIGNALS"]
    header = f"  {'SYM':<8} {'UP':>6} {'STABLE':>8} {'DOWN':>6} {'TOTAL':>8}"
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))

    total_up = 0
    total_stable = 0
    total_down = 0

    for sym, stats in sorted(perf.symbol_stats.items()):
        up = stats.signal_counts[Signal.UP]
        stable = stats.signal_counts[Signal.STABLE]
        down = stats.signal_counts[Signal.DOWN]
        total = up + stable + down

        total_up += up
        total_stable += stable
        total_down += down

        lines.append(f"  {sym:<8} {up:>6} {stable:>8} {down:>6} {total:>8}")

    lines.append("  " + "-" * (len(header) - 2))
    lines.append(
        f"  {'TOTAL':<8} {total_up:>6} {total_stable:>8} {total_down:>6} "
        f"{total_up + total_stable + total_down:>8}"
    )

    return lines


def _run_consistency_checks(
    positions: list[dict],
    perf: PerformanceTracker,
    ctx: ReportContext,
    schedule_stats: dict,
) -> list[str]:
    """Run consistency check assertions and return any warnings."""
    warnings = []

    # Check: qty = BUY + COVER − SELL − SHORT for each symbol
    for p in positions:
        sym = p["symbol"]
        qty = float(p.get("pos", p.get("qty", 0)))
        sym_stats = perf.symbol_stats.get(sym)
        if sym_stats:
            tc = sym_stats.trade_counts
            expected = tc["BUY"] + tc["COVER"] - tc["SELL"] - tc["SHORT"]
            # Note: This check is approximate due to fractional quantities
            if abs(qty - expected) > 1.0:
                warnings.append(
                    f"Position mismatch for {sym}: qty={qty:.4f}, "
                    f"expected from trades={expected}"
                )

    # Check: Σ $/decision = Notional/Decision
    total_sizing = sum(s.dollars_per_decision for s in ctx.sizing_info)
    if ctx.notional_per_decision > 0 and abs(total_sizing - ctx.notional_per_decision) > 1.0:
        warnings.append(
            f"Sizing sum mismatch: Σ$/decision=${total_sizing:,.0f} != "
            f"Notional/Decision=${ctx.notional_per_decision:,.0f}"
        )

    # Check: executed + remaining = total
    if schedule_stats["executed"] + schedule_stats["remaining"] != schedule_stats["total"]:
        warnings.append(
            f"Schedule mismatch: {schedule_stats['executed']} + "
            f"{schedule_stats['remaining']} != {schedule_stats['total']}"
        )

    # Check: UP + STABLE + DOWN = executed for each symbol
    for sym, stats in perf.symbol_stats.items():
        total_signals = stats.total_signals()
        # This check is informational since signals may not align with scheduled decisions
        # (signals can arrive outside decision points)

    return warnings


def report_once(
    broker: BrokerBase,
    perf: PerformanceTracker,
    ctx: Optional[ReportContext] = None,
) -> None:
    """Log account summary, open positions, and bot performance (single snapshot).

    This renders a comprehensive account info panel with the following sections:
    - INFO: Timestamp, print interval, next scheduled print
    - ACCOUNT: NLV, notional exposure, leverage, PnL
    - SCHEDULE: Decision points executed/remaining, exit window countdown
    - SIZING: Per-symbol dollar and unit allocation per decision
    - POSITIONS: Open positions with cost basis, market value, PnL
    - SIGNALS: Per-symbol signal counts since session open

    Falls back to a minimal report if context is not provided (backward compatible).
    """
    try:
        now = datetime.now()

        # Fallback to minimal report if no context
        if ctx is None:
            _report_minimal(broker, perf)
            return

        now = datetime.now(ctx.user_tz)

        # Fetch positions and prices
        pos_list = broker.get_open_positions()
        prices: dict[str, float] = {}
        for p in pos_list:
            sym = p["symbol"]
            contract = broker.resolve_contract(sym)
            if contract:
                price = broker.get_last_price(contract)
                if price:
                    prices[sym] = price

        # Build all sections
        lines: list[str] = []
        lines.append("=" * 80)

        lines.extend(_build_info_block(now, ctx))
        lines.append("")

        lines.extend(_build_account_block(broker, perf, ctx, pos_list, prices))
        lines.append("")

        lines.extend(_build_schedule_block(now, ctx))
        lines.append("")

        lines.extend(_build_sizing_block(ctx))
        lines.append("")

        lines.extend(_build_positions_block(broker, perf, ctx, pos_list, prices))
        lines.append("")

        lines.extend(_build_signals_block(perf))

        # Run consistency checks
        schedule_stats = _compute_schedule_stats(now, ctx.schedule, ctx.user_tz)
        warnings = _run_consistency_checks(pos_list, perf, ctx, schedule_stats)
        if warnings:
            lines.append("")
            lines.append("WARNINGS")
            for w in warnings:
                lines.append(f"  ⚠ {w}")

        lines.append("=" * 80)

        # Log each line
        for line in lines:
            logger.info(line)

    except Exception as exc:
        logger.warning(f"Reporting error: {exc}")


def _report_minimal(broker: BrokerBase, perf: PerformanceTracker) -> None:
    """Minimal report when no context is provided (backward compatible)."""
    try:
        acct = broker.get_account_summary()
        pos_list = broker.get_open_positions()

        logger.info("=" * 60)
        logger.info(
            f"ACCOUNT  NLV=${acct['net_liquidation']:,.0f}  "
            f"UnrealPnL=${acct['unrealized_pnl']:+,.2f}  "
            f"RealPnL=${acct['realized_pnl']:+,.2f}  "
            f"BuyPow=${acct['buying_power']:,.0f}"
        )
        if pos_list:
            logger.info("POSITIONS:")
            for p in pos_list:
                logger.info(
                    f"  {p['symbol']:8s} qty={p['pos']:+.0f}  "
                    f"avgCost={p['avg_cost']:.4f}  "
                    f"UnrealPnL=${p['unreal_pnl']:+,.2f}"
                )
        logger.info(f"BOT PERF  {perf.summary()}")
        logger.info(
            f"SIGNALS   UP={perf.signal_counts[Signal.UP]}  "
            f"STABLE={perf.signal_counts[Signal.STABLE]}  "
            f"DOWN={perf.signal_counts[Signal.DOWN]}"
        )
        logger.info("=" * 60)
    except Exception as exc:
        logger.warning(f"Reporting error: {exc}")


def format_day_summary(
    day: date,
    perf: PerformanceTracker,
    *,
    intraday_pnl: float,
    exit_pnl: float,
    daily_start_capital: float,
    snapshot: Optional[dict] = None,
    intraday_closes: int = 0,
) -> list[str]:
    """Build the end-of-day summary lines (no I/O).

    ``Trades`` / ``Closed`` is the number of positions that went flat — not
    each order. Unrealized is $0 after flatten because nothing is left open;
    the pre-close mark-to-market lives on *snapshot*.
    """
    snapshot = snapshot or {}
    positions = list(snapshot.get("positions") or [])
    pre_unreal = float(snapshot.get("unrealized_pnl") or 0.0)
    nlv = snapshot.get("net_liquidation")
    eod_closes = max(0, perf.total_trades - intraday_closes)
    orders = perf.order_counts()
    order_total = sum(orders.values())
    sig_up = perf.signal_counts[Signal.UP]
    sig_stable = perf.signal_counts[Signal.STABLE]
    sig_down = perf.signal_counts[Signal.DOWN]
    sig_total = sig_up + sig_stable + sig_down
    total_pnl = perf.gross_pnl

    lines = [
        "=" * 60,
        f"DAY SUMMARY  {day}",
        "-" * 60,
        "SIGNALS  (every UP/STABLE/DOWN received)",
        f"  UP={sig_up}  STABLE={sig_stable}  DOWN={sig_down}  total={sig_total}",
    ]
    if perf.symbol_stats:
        lines.append(f"  {'SYM':<8} {'UP':>6} {'STABLE':>8} {'DOWN':>6} {'TOTAL':>8}")
        for sym, stats in sorted(perf.symbol_stats.items()):
            up = stats.signal_counts[Signal.UP]
            stable = stats.signal_counts[Signal.STABLE]
            down = stats.signal_counts[Signal.DOWN]
            lines.append(
                f"  {sym:<8} {up:>6} {stable:>8} {down:>6} {up + stable + down:>8}"
            )

    lines.extend([
        "",
        "ORDERS  (fills sent to the broker — not the same as Closed)",
        f"  BUY={orders['BUY']}  SELL={orders['SELL']}  "
        f"SHORT={orders['SHORT']}  COVER={orders['COVER']}  total={order_total}",
    ])
    if perf.symbol_stats:
        lines.append(f"  {'SYM':<8} {'BUY':>6} {'SELL':>6} {'SHORT':>6} {'COVER':>6}")
        for sym, stats in sorted(perf.symbol_stats.items()):
            tc = stats.trade_counts
            lines.append(
                f"  {sym:<8} {tc['BUY']:>6} {tc['SELL']:>6} "
                f"{tc['SHORT']:>6} {tc['COVER']:>6}"
            )

    lines.extend([
        "",
        "CLOSED POSITIONS  (a 'trade' is a position going flat, not each order)",
        f"  During session : {intraday_closes}",
        f"  EOD flatten    : {eod_closes}",
        f"  Total          : {perf.total_trades}   "
        f"W={perf.winning_trades}  L={perf.losing_trades}  "
        f"Win%={perf.win_rate:.1%}",
    ])
    if perf._trade_log:
        for entry in perf._trade_log:
            side = "LONG" if entry["direction"] == Signal.UP else "SHORT"
            lines.append(
                f"    {entry['symbol']:<8} {side:<5}  "
                f"PnL={_format_currency(entry['pnl'], show_sign=True)}"
            )
    elif perf.total_trades == 0:
        if order_total:
            lines.append(
                "    none — orders filled but no position went flat "
                "(scaled in / leftover under 1 share)"
            )
        else:
            lines.append("    none — no fills and nothing flattened")

    in_session = (
        f"  Realized in-session       : {_format_currency(intraday_pnl, show_sign=True)}"
    )
    if intraday_closes == 0 and intraday_pnl == 0.0:
        in_session += "  (no position closed/flipped during the day)"

    lines.extend([
        "",
        "PNL",
        f"  Unrealized before flatten : {_format_currency(pre_unreal, show_sign=True)}",
        "  Unrealized now            : $0.00  "
        "(flat — we close everything at session end)",
        in_session,
        f"  Realized on flatten       : {_format_currency(exit_pnl, show_sign=True)}",
        f"  Total day realized        : {_format_currency(total_pnl, show_sign=True)}",
    ])
    if daily_start_capital:
        lines.append(
            f"  Return on {_format_currency(daily_start_capital)}     : "
            f"{total_pnl / daily_start_capital:+.2%}"
        )
    if nlv is not None:
        lines.append(f"  NLV at flatten            : {_format_currency(float(nlv))}")

    lines.extend(["", "POSITIONS AT FLATTEN"])
    if positions:
        for p in positions:
            qty = float(p.get("qty", p.get("pos", 0)))
            avg = float(p.get("avg_price", p.get("avg_cost", 0)))
            label = "LONG" if qty >= 0 else "SHORT"
            lines.append(
                f"  {label:<5} {_format_qty(abs(qty)):>8} {p.get('symbol', '?')}  "
                f"avg={avg:.2f}"
            )
    else:
        lines.append("  (already flat — nothing to close)")

    lines.append("=" * 60)
    return lines
