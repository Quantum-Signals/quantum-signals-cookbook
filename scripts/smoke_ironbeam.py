"""Read-only Ironbeam check: login, account, front months, quotes, balances, positions.

Places no orders. Reads IRONBEAM_* (and TRADER_ALLOWED_SYMBOLS) from .env.

    uv run python scripts/smoke_ironbeam.py
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

from qs_trader_broker_ironbeam import IronbeamBroker
from qs_trader_symbols import canonical_symbols, parse_allowed_symbols, trading_targets

load_dotenv()


def main() -> None:
    broker = IronbeamBroker()
    broker.connect()
    try:
        allowed = [s.strip() for s in os.environ.get("TRADER_ALLOWED_SYMBOLS", "").split(",") if s.strip()]
        targets = trading_targets(parse_allowed_symbols(allowed)) if allowed else canonical_symbols()

        print("\nContracts")
        for canon in targets:
            contract = broker.resolve_contract(canon)
            if contract is None:
                print(f"  {canon:5s} -> (none)")
                continue
            price = broker.get_last_price(contract)
            shown = f"{price:,.2f}" if price is not None else "no quote"
            print(f"  {canon:5s} -> {contract.exch_sym:16s} {shown}")

        print("\nStartup warnings")
        for w in broker.startup_entitlement_warnings(targets) or ["(none)"]:
            print(f"  {w}")

        print("\nAccount")
        for k, v in broker.get_account_summary().items():
            print(f"  {k:16s} {v:,.2f}")

        print("\nPositions")
        rows = broker.get_open_positions()
        for p in rows or [{"symbol": "(flat)", "pos": 0, "avg_cost": 0, "unreal_pnl": 0}]:
            print(f"  {p['symbol']:6s} {p['pos']:+g} @ {p['avg_cost']:,.2f}  uPnL={p['unreal_pnl']:,.2f}")
    finally:
        broker.disconnect()


if __name__ == "__main__":
    main()
