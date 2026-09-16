"""QuantumSignals symbol catalog - canonical names, aliases, and broker helpers.

Every name the QS stream may emit for an instrument lives here once. Strategies
normalize incoming ``signal.symbol`` values to a *canonical* key (futures root,
e.g. ``ES``) for internal state. Each broker builds its own tradable map from
this catalog (Alpaca tickers, simulated contracts).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QSInstrument:
    """One QS instrument and the names that refer to it."""

    canonical: str
    """Preferred internal key (futures root, e.g. ``ES``)."""

    aliases: frozenset[str]
    """Every ``signal.symbol`` value QS may emit for this instrument."""

    alpaca_ticker: str | None = None
    """Tradable Alpaca equity/ETF; ``None`` when this broker cannot trade it."""

    position_symbols: frozenset[str] = frozenset()
    """Extra broker position keys (ETF tickers, micro roots) for sync lookups."""


def _inst(
    canonical: str,
    *aliases: str,
    alpaca_ticker: str | None = None,
    position_symbols: frozenset[str] | None = None,
) -> QSInstrument:
    all_names = frozenset({canonical, *aliases})
    pos = position_symbols or frozenset()
    extras: set[str] = set(pos)
    if alpaca_ticker:
        extras.add(alpaca_ticker)
    extras |= all_names
    return QSInstrument(
        canonical=canonical,
        aliases=all_names,
        alpaca_ticker=alpaca_ticker,
        position_symbols=frozenset(extras),
    )


# All instruments QuantumSignals models may publish. Keep aliases in sync with
# the QS stream (futures roots, full index names, ETF tickers, micro roots).
INSTRUMENTS: tuple[QSInstrument, ...] = (
    _inst(
        "ES",
        "S&P500",
        "SPY",
        "MES",
        alpaca_ticker="SPY",
    ),
    _inst(
        "NQ",
        "Nasdaq100",
        "QQQ",
        "MNQ",
        alpaca_ticker="QQQ",
    ),
    _inst(
        "YM",
        "Dow30",
        "Dow Jones",
        "DIA",
        "MYM",
        alpaca_ticker="DIA",
    ),
    _inst(
        "RTY",
        "Russell2000",
        "Russell 2000",
        "IWM",
        "M2K",
        alpaca_ticker="IWM",
    ),
    _inst(
        "CL",
        "Crude Oil",
        "USO",
        alpaca_ticker="USO",
    ),
    _inst(
        "GC",
        "Gold",
        "GLD",
        alpaca_ticker="GLD",
    ),
    _inst(
        "SI",
        "Silver",
        "SLV",
        alpaca_ticker="SLV",
    ),
    _inst(
        "NG",
        "Natural Gas",
        "Gas",
        "UNG",
        alpaca_ticker="UNG",
    ),
    _inst(
        "AAPL",
        alpaca_ticker="AAPL",
    ),
)

_ALIAS_TO_INSTRUMENT: dict[str, QSInstrument] = {}
_POSITION_TO_CANONICAL: dict[str, str] = {}
for _instrument in INSTRUMENTS:
    for _alias in _instrument.aliases:
        _ALIAS_TO_INSTRUMENT[_alias] = _instrument
    _POSITION_TO_CANONICAL[_instrument.canonical] = _instrument.canonical
    for _pos in _instrument.position_symbols:
        _POSITION_TO_CANONICAL[_pos] = _instrument.canonical


def canonicalize(qs_symbol: str) -> str | None:
    """Map any QS alias to the canonical instrument key, or ``None`` if unknown."""
    instrument = _ALIAS_TO_INSTRUMENT.get(qs_symbol)
    return instrument.canonical if instrument else None


def is_known_qs_symbol(qs_symbol: str) -> bool:
    """Return whether *qs_symbol* is a recognized QS name."""
    return qs_symbol in _ALIAS_TO_INSTRUMENT


def canonical_symbols() -> list[str]:
    """All canonical instrument keys (one per tradable QS product)."""
    return [i.canonical for i in INSTRUMENTS]


def aliases_for_canonical(canonical: str) -> frozenset[str]:
    """Every QS/broker alias for *canonical*, or empty if unknown."""
    for instrument in INSTRUMENTS:
        if instrument.canonical == canonical:
            return instrument.aliases
    return frozenset()


@dataclass(frozen=True)
class AllowedSymbolSet:
    """User ``TRADER_ALLOWED_SYMBOLS`` normalized for filtering and sizing.

    Accepts any alias the user might type (``ES``, ``SPY``, ``S&P500``) and
    collapses known names to one *canonical* key per instrument. Names outside
    the catalog are kept as *passthrough* broker symbols (e.g. a single-stock
    ticker).
    """

    canonical: frozenset[str]
    passthrough: frozenset[str]
    raw: frozenset[str]


def parse_allowed_symbols(names: list[str] | None) -> AllowedSymbolSet | None:
    """Normalize user input. ``None`` / empty → no filter (trade all catalog)."""
    if not names:
        return None
    canonical: set[str] = set()
    passthrough: set[str] = set()
    for name in names:
        canon = canonicalize(name)
        if canon:
            canonical.add(canon)
        else:
            passthrough.add(name)
    return AllowedSymbolSet(
        frozenset(canonical),
        frozenset(passthrough),
        frozenset(names),
    )


def is_symbol_allowed(qs_symbol: str, allowed: AllowedSymbolSet | None) -> bool:
    """Whether *qs_symbol* matches the user's allow-list (any alias form)."""
    if allowed is None:
        return canonicalize(qs_symbol) is not None
    canon = canonicalize(qs_symbol)
    if canon is not None and canon in allowed.canonical:
        return True
    return qs_symbol in allowed.passthrough


def trading_targets(allowed: AllowedSymbolSet | None) -> list[str]:
    """Keys used for sizing and startup checks (canonical + passthrough)."""
    if allowed is None:
        return canonical_symbols()
    return sorted(allowed.canonical) + sorted(allowed.passthrough)


def active_canonical_symbols(allowed: set[str] | frozenset[str] | list[str] | None) -> list[str]:
    """Canonical keys that pass *allowed* (QS names). Empty *allowed* → all known."""
    parsed = parse_allowed_symbols(list(allowed) if allowed else None)
    if parsed is None:
        return canonical_symbols()
    return sorted(parsed.canonical)


def instrument_for_canonical(canonical: str) -> QSInstrument | None:
    for instrument in INSTRUMENTS:
        if instrument.canonical == canonical:
            return instrument
    return None


def format_alias_list(canonical: str, *, limit: int = 8) -> str:
    """Comma-separated aliases for log messages."""
    aliases = sorted(aliases_for_canonical(canonical) or {canonical})
    if len(aliases) > limit:
        extra = len(aliases) - limit
        return ", ".join(aliases[:limit]) + f", … (+{extra} more)"
    return ", ".join(aliases)


def position_symbol_to_canonical(broker_symbol: str) -> str | None:
    """Map a broker position key (ETF ticker, micro root, …) back to canonical."""
    return _POSITION_TO_CANONICAL.get(broker_symbol)


def alpaca_symbol_map() -> dict[str, str]:
    """Build Alpaca ``DEFAULT_SYMBOL_MAP`` (QS alias → tradable ticker)."""
    mapping: dict[str, str] = {}
    for instrument in INSTRUMENTS:
        if not instrument.alpaca_ticker:
            continue
        for alias in instrument.aliases:
            mapping[alias] = instrument.alpaca_ticker
    return mapping


def simulated_symbol_map() -> dict[str, dict[str, str]]:
    """Build SimulatedBroker ``DEFAULT_SYMBOL_MAP`` (QS alias → contract spec)."""
    _EXCHANGE: dict[str, str] = {
        "ES": "CME",
        "NQ": "CME",
        "YM": "CBOT",
        "RTY": "CME",
        "MES": "CME",
        "MNQ": "CME",
        "MYM": "CBOT",
        "M2K": "CME",
        "CL": "NYMEX",
        "GC": "COMEX",
        "AAPL": "SMART",
        "SPY": "SMART",
        "QQQ": "SMART",
        "DIA": "SMART",
        "IWM": "SMART",
        "USO": "SMART",
        "GLD": "SMART",
        "SLV": "SMART",
        "UNG": "SMART",
        "NG": "NYMEX",
        "SI": "COMEX",
    }

    def _spec(symbol: str, sec_type: str) -> dict[str, str]:
        return {
            "symbol": symbol,
            "secType": sec_type,
            "exchange": _EXCHANGE.get(symbol, "SIM"),
            "currency": "USD",
        }

    mapping: dict[str, dict[str, str]] = {}
    for instrument in INSTRUMENTS:
        if instrument.alpaca_ticker:
            trade, sec = instrument.alpaca_ticker, "STK"
        else:
            trade, sec = instrument.canonical, "STK"
        spec = _spec(trade, sec)
        for alias in instrument.aliases:
            mapping[alias] = spec
    return mapping
