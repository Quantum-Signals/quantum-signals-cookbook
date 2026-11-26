# Brokers — plugin architecture

How the trader talks to brokers, and how to add your own. The app core knows
nothing about specific brokers: each broker is a **separate package** that
registers itself via an entry point and is discovered at runtime.

## Packages

Brokers live in their own uv workspace packages under `src/brokers/`, each
owning its broker SDK:

| Package | Module | Provides | SDK dep |
|---------|--------|----------|---------|
| `qs-trader-symbols` | `qs_trader_symbols` | QS symbol catalog, alias normalization, broker map helpers | — |
| `qs-trader-broker-core` | `qs_trader_broker_core` | `BrokerBase`, `OrderResult`, `SimulatedBroker` | — |
| `qs-trader-broker-alpaca` | `qs_trader_broker_alpaca` | `AlpacaBroker`, `AlpacaSettings` (`ALPACA_*`) | `alpaca-py` |
| `qs-trader-broker-ironbeam` | `qs_trader_broker_ironbeam` | `IronbeamBroker`, `IronbeamSettings` (`IRONBEAM_*`) | `requests` |

## Ironbeam: futures in whole contracts

`IronbeamBroker` trades CME Group futures (the size named in `TRADER_ALLOWED_SYMBOLS`: `MES` trades the `MES` contract
month, e.g. `XCME:MES.Z26`, and `ES` the full-size `ES`) in whole contracts, long
and short. It sets `supports_fractional = False`, so the strategy rounds every
slice to at least one contract: no reserve and no dust. Notional is
`qty × price × contract multiplier` (MES = $5 per point), with the multiplier
taken from `contract_multipliers` keyed by the position symbol (the futures root).

## Discovery & selection

Brokers are **discovered via entry points**, not a hardcoded list. Each package
registers under the `qs_trader_brokers` group in its own `pyproject.toml`:

```toml
[project.entry-points.qs_trader_brokers]
alpaca = "qs_trader_broker_alpaca:AlpacaBroker"
```

`create_broker(settings)` (in the app's
[`brokers/__init__.py`](../src/trader/brokers/__init__.py)) builds the
name→class registry at runtime with `importlib.metadata.entry_points` and picks
one:

```
TRADER_BROKER=simulated   → simulated broker (SimulatedBroker, in core; the default)
TRADER_BROKER=alpaca      → AlpacaBroker      (REST, ETFs, fractional longs)
TRADER_BROKER=ironbeam    → IronbeamBroker    (REST v3, CME Group futures)
```

Installing a package that registers a broker under this group makes it
selectable via `TRADER_BROKER=<name>` with **no core change** — an external
`uv add my-broker-package` works the same way. An unknown `TRADER_BROKER` raises
a `ValueError` listing the installed brokers. (Reinstall — `uv sync` — after
editing entry points so the metadata is rewritten.)

## Dependency graph (a DAG — no import cycle)

Broker packages depend on `qs-trader-broker-core` (and transitively
`qs-trader-symbols`), **never on the app**. Each broker owns and self-loads its
own configuration from namespaced env vars via `pydantic-settings`, so
`create_broker()` constructs it with no arguments and the app passes it no
config. The app's `Settings` only decides *which* broker to run
(`TRADER_BROKER`).

```
trader (app) ──→ qs-trader-symbols
     │                ↑
     ├──→ qs-trader-broker-core ──→ qs-trader-symbols
     ├──→ qs-trader-broker-alpaca ──→ qs-trader-broker-core
     └──→ qs-trader-broker-ironbeam ──→ qs-trader-broker-core
```

## The `BrokerBase` interface

A broker subclasses `BrokerBase` and implements these methods (see
[`base.py`](../src/brokers/core/src/qs_trader_broker_core/base.py) for the full
contracts and `None` conventions):

| Method | Purpose |
|--------|---------|
| `connect()` / `disconnect()` | Lifecycle |
| `resolve_contract(qs_symbol) -> contract \| None` | QS symbol → opaque broker contract (`None` = not tradable, skip) |
| `get_last_price(contract) -> float \| None` | Latest price |
| `place_order(contract, action, qty, price=None, position_intent=None) -> OrderResult \| None` | Submit; `None` = failed. Set `last_order_not_placed = True` when the order was definitely refused: only then is a failed close retried |
| `cancel_working_orders() -> bool` | Optional. Cancel all working orders; `True` only if the broker confirms none is left. Without it, a close with an unknown outcome is never resent |
| `get_account_summary() -> dict` | NLV, PnL, buying power |
| `get_open_positions()` / `get_positions_for_sync() -> list[dict]` | Positions (the latter with signed qty) |

`contract` is opaque — whatever the broker needs (a ticker string, a
broker-native contract object, a dataclass). The strategy layer never inspects
it; it only threads it back through `get_last_price` / `place_order`. `place_order` always
returns an `OrderResult` (with the fill price resolved by the broker), so callers
never touch broker-native objects.

## Symbol maps

QuantumSignals symbol names (canonical keys and every stream alias) live in
one catalog: `qs_trader_symbols`. Each broker builds its tradable map from
that catalog:

| Map | Location | Purpose |
|-----|----------|---------|
| `INSTRUMENTS` / `canonicalize()` | `qs_trader_symbols` | QS alias → canonical key (`ES`, `NQ`, …) |
| `AlpacaBroker.DEFAULT_SYMBOL_MAP` | built via `alpaca_symbol_map()` | QS alias → tradable ETF/stock ticker |
| `IronbeamBroker.DEFAULT_SYMBOL_MAP` | built via `ironbeam_symbol_map()` | QS alias → futures root (`ES`, or `MES` when `TRADER_ALLOWED_SYMBOLS` names `MES`); the contract month is resolved at runtime. AAPL is omitted |
| `SimulatedBroker.DEFAULT_SYMBOL_MAP` | built via `simulated_symbol_map()` | Dry-run contract shapes; `.DEFAULT_PRICES` seeds quotes |

Each broker accepts an optional `symbol_map` (and the simulator, `sim_prices`)
constructor override for tests; the app never passes one, so the broker's own
default is used at runtime. (Contract multipliers for EOD PnL are broker-neutral
and stay in the app's `config.py`.)

## Create your own broker

A broker is its own package (in-tree under `src/brokers/`, or external):

1. **Create a package** that depends on `qs-trader-broker-core` and
   `qs-trader-symbols`. In-tree, add a directory under `src/brokers/`; the
   workspace glob picks it up.
2. **Define settings** — a `pydantic-settings` `BaseSettings` with a namespaced
   `env_prefix` (e.g. `MYBROKER_`). Do **not** import `trader` — that keeps the
   package graph a DAG.
3. **Subclass `BrokerBase`** — implement the abstract methods and build
   `DEFAULT_SYMBOL_MAP` from `qs_trader_symbols` (or a custom map). Make
   `__init__(self, settings=None)` self-load its settings
   (`settings or MyBrokerSettings()`) so `create_broker()` can build it with no
   arguments.
4. **Register the entry point** in the package's own `pyproject.toml`:
   ```toml
   [project.entry-points.qs_trader_brokers]
   myname = "my_module:MyBroker"
   ```
5. **`uv sync`** to write the entry-point metadata, then select it with
   `TRADER_BROKER=myname` and configure it via `MYBROKER_*` env vars. No change
   to `create_broker` is needed.
