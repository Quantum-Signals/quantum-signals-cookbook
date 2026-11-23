"""Read-only Ironbeam check: login, account, front months, quotes, balances, positions.

Places no orders. Reads IRONBEAM_* (and TRADER_ALLOWED_SYMBOLS) from .env.
Each section runs on its own: if one fails (e.g. Ironbeam times out), the
error is printed and the remaining sections still run. Exits non-zero when
any section failed.

    uv run python scripts/smoke_ironbeam.py
"""

from __future__ import annotations

import os
import sys
from typing import Callable

from dotenv import load_dotenv

from qs_trader_broker_ironbeam import IronbeamBroker
from qs_trader_symbols import canonical_symbols, parse_allowed_symbols, trading_targets

load_dotenv()


def _section(title: str, body: Callable[[], None], failures: list[str]) -> None:
    print(f"\n{title}")
    try:
        body()
    except Exception as exc:  # report and keep going
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        failures.append(title)


def main() -> int:
    broker = IronbeamBroker()
    broker.connect()
    failures: list[str] = []
    try:
        allowed = [s.strip() for s in os.environ.get("TRADER_ALLOWED_SYMBOLS", "").split(",") if s.strip()]
        targets = trading_targets(parse_allowed_symbols(allowed)) if allowed else canonical_symbols()

        def positions() -> None:
            rows = broker.get_open_positions()
            for p in rows or [{"symbol": "(flat)", "pos": 0, "avg_cost": 0, "unreal_pnl": 0}]:
                print(f"  {p['symbol']:6s} {p['pos']:+g} @ {p['avg_cost']:,.2f}  uPnL={p['unreal_pnl']:,.2f}")

        def account() -> None:
            for k, v in broker.get_account_summary().items():
                print(f"  {k:16s} {v:,.2f}")

        def contracts() -> None:
            for canon in targets:
                try:
                    contract = broker.resolve_contract(canon)
                    if contract is None:
                        print(f"  {canon:5s} -> (none)")
                        continue
                    price = broker.get_last_price(contract)
                    shown = f"{price:,.2f}" if price is not None else "no quote"
                    print(f"  {canon:5s} -> {contract.exch_sym:16s} {shown}")
                except Exception as exc:
                    print(f"  {canon:5s} -> FAILED: {type(exc).__name__}: {exc}")
                    failures.append(f"Contracts ({canon})")

        def warnings() -> None:
            for w in broker.startup_entitlement_warnings(targets) or ["(none)"]:
                print(f"  {w}")

        # Positions first: the broker prefers a contract you already hold, so
        # the Contracts section then shows what the bot would actually trade.
        _section("Positions", positions, failures)
        _section("Account", account, failures)
        _section("Contracts", contracts, failures)
        _section("Startup warnings", warnings, failures)
    finally:
        broker.disconnect()

    if failures:
        print(f"\n{len(failures)} section(s) failed: {', '.join(failures)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
