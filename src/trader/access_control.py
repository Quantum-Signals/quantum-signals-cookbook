"""Startup entitlement checks — QS signal access and broker tradability.

Runs once when the process starts (after broker connect), not on each trade.
Emits ``logger.warning`` for mismatches so operators can fix config before
the session generates silent no-fills.
"""

from __future__ import annotations

from typing import Iterable

from loguru import logger
from quantumsignals.client import APIError, Client as QSClient
from quantumsignals.client.models import SignalMetadata, Subscription

from qs_trader_broker_core.base import BrokerBase
from qs_trader_symbols import (
    AllowedSymbolSet,
    aliases_for_canonical,
    canonicalize,
    format_alias_list,
    instrument_for_canonical,
    parse_allowed_symbols,
    trading_targets,
)
from trader.config import Settings


def _broker_target_label(broker: BrokerBase, qs_symbol: str) -> str:
    contract = broker.resolve_contract(qs_symbol)
    if contract is None:
        return "(not tradable on this broker)"
    if isinstance(contract, str):
        return contract
    trading = getattr(contract, "trading_symbol", None)
    if trading:
        root = getattr(contract, "root", "")
        return f"{trading} ({root})" if root else str(trading)
    symbol = getattr(contract, "symbol", None)
    return str(symbol) if symbol else repr(contract)


def log_symbol_routing(
    settings_broker: str, broker: BrokerBase, allowed: AllowedSymbolSet | None
) -> None:
    """Log how user-facing symbol names route to the connected broker."""
    targets = trading_targets(allowed)
    logger.info(
        f"Symbol routing for broker={settings_broker} "
        f"({len(targets)} target(s)):"
    )
    for key in targets:
        inst = instrument_for_canonical(key)
        if inst is not None:
            sample = inst.canonical
            aliases = format_alias_list(key)
            target = _broker_target_label(broker, sample)
            logger.info(f"  [{key}] user may say: {aliases}  →  {target}")
        else:
            target = _broker_target_label(broker, key)
            logger.info(f"  [{key}] (direct)  →  {target}")


def target_canonical_symbols(settings: Settings) -> list[str]:
    """Instrument keys the trader will attempt to follow (canonical + passthrough)."""
    return trading_targets(parse_allowed_symbols(settings.allowed_symbols))


def _canonical_set(names: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for name in names:
        canon = canonicalize(name)
        if canon:
            out.add(canon)
    return out


def _symbols_from_metadata(meta: SignalMetadata) -> set[str]:
    names: set[str] = set()
    if meta.model_output_symbol:
        names.add(meta.model_output_symbol)
    if meta.trading_symbols:
        for values in meta.trading_symbols.values():
            names.update(values)
    return names


def _subscribed_canonical(
    subscriptions: list[Subscription],
    catalog: dict[str, SignalMetadata],
) -> set[str]:
    subscribed: set[str] = set()
    for sub in subscriptions:
        meta = catalog.get(sub.signal_id)
        if meta is None:
            continue
        subscribed |= _canonical_set(_symbols_from_metadata(meta))
    return subscribed


def _catalog_canonical(catalog: dict[str, SignalMetadata]) -> set[str]:
    out: set[str] = set()
    for meta in catalog.values():
        out |= _canonical_set(_symbols_from_metadata(meta))
    return out


def qs_entitlement_warnings(settings: Settings, targets: list[str]) -> list[str]:
    """Check QuantumSignals commerce subscriptions and signal catalog for *targets*."""
    if not targets:
        return ["No target symbols configured (check TRADER_ALLOWED_SYMBOLS)"]

    warnings: list[str] = []
    try:
        with QSClient(api_key=settings.qs_api_key) as qs:
            try:
                catalog = qs.get_signal_catalog()
                subscriptions = qs.get_subscriptions()
            except APIError as exc:
                warnings.append(
                    f"Could not load QS signal catalog/subscriptions ({exc}) — "
                    "entitlement check incomplete"
                )
                return warnings

            subscribed_canonical = _subscribed_canonical(subscriptions, catalog)
            catalog_canonical = _catalog_canonical(catalog)
            missing_meta_ids = [
                sub.signal_id for sub in subscriptions if sub.signal_id not in catalog
            ]

            for canon in targets:
                alias_hint = ", ".join(sorted(aliases_for_canonical(canon) or {canon})[:4])
                in_sub = canon in subscribed_canonical
                in_catalog = canon in catalog_canonical

                if in_sub:
                    continue
                if in_catalog:
                    warnings.append(
                        f"{canon} ({alias_hint}) — in signal catalog but no "
                        "active commerce subscription; stream will omit this symbol"
                    )
                elif missing_meta_ids:
                    warnings.append(
                        f"{canon} ({alias_hint}) — subscribed but symbol metadata "
                        "missing from catalog; verify signal_id entitlements"
                    )
                else:
                    warnings.append(
                        f"{canon} ({alias_hint}) — no active subscription and "
                        "not in signal catalog; live stream will not include this symbol"
                    )

    except Exception as exc:
        warnings.append(f"QS entitlement check failed: {exc}")

    return warnings


def run_startup_access_checks(settings: Settings, broker: BrokerBase) -> None:
    """Log entitlement warnings for QS and the connected broker (startup only)."""
    allowed = parse_allowed_symbols(settings.allowed_symbols)
    targets = target_canonical_symbols(settings)

    log_symbol_routing(settings.broker, broker, allowed)

    if allowed and allowed.passthrough:
        for name in sorted(allowed.passthrough):
            logger.warning(
                f"[access] {name} is not in the QS symbol catalog — "
                "only direct broker trading applies (no QS signal alias match)"
            )

    logger.info(
        f"Access control: checking entitlements for {len(targets)} symbol(s): "
        f"{', '.join(targets) if targets else '(none)'}"
    )

    qs_targets = [t for t in targets if canonicalize(t) is not None]
    qs_warnings = qs_entitlement_warnings(settings, qs_targets)
    broker_warnings = broker.startup_entitlement_warnings(targets)

    if not qs_warnings and not broker_warnings:
        logger.info("Access control: no entitlement issues detected")
        return

    for msg in qs_warnings:
        logger.warning(f"[access/QS] {msg}")
    for msg in broker_warnings:
        logger.warning(f"[access/broker] {msg}")
