# TMGM integration — conversation context

Working notes from the TMGM / QuantumSignals live-trader discussion (Sep–Oct 2026). Not product docs; use this to resume the work.

## What we are integrating

The live trader is a Linux/Docker Python process (`TRADER_BROKER=…`). Brokers are plugins under `src/brokers/`: depend on `qs-trader-broker-core` + `qs-trader-symbols`, implement `BrokerBase`, register a `qs_trader_brokers` entry point. No core `if broker == …` branch.

TMGM is **not** like Alpaca (REST) or Rithmic (WebSocket). Jack (TMGM) confirmed: **no direct broker API**. Partners connect **into MetaTrader**. ForexVPS is affiliated optional hosting, not a special SDK.

Split of ownership:

| Who | What they own |
|-----|----------------|
| MetaQuotes | MT5 terminal, protocol, official Python package |
| TMGM | Account, symbols, spreads, leverage, servers |

`TmgmBroker` would call the **local MetaTrader 5 Python API** (terminal already logged into TMGM), not `api.tmgm.com`:

- `initialize(login, password, server)`
- `symbol_info_tick`
- `order_send`
- `account_info`
- `positions_get`

On real Windows that is `MetaTrader5`. Official package is **MT5-only** (no MT4 equivalent).

## Architecture decision

**Alex:** try **Wine + MT5 on Linux** first (avoid a Windows VPS).

Wine only runs the Windows MT5 installer. You still need TMGM’s terminal **and** a trading login. Linux Python cannot import official `MetaTrader5` directly; you need a bridge (`pymt5linux` / `mt5linux`) to Wine-side Python talking to the terminal.

**Yianni (later):** do **not** do Wine for now — Alex is busy. Use **ForexVPS (Windows)** as Jack described for partners.

That is a reasonable deferral: same `TmgmBroker` shape, simpler path (official `MetaTrader5`, no Wine bridge). Wine can come back later.

ForexVPS is free only with TMGM equity/volume thresholds; otherwise paid. Any Windows VM works.

Do **not** bake Wine (or MT5) into the current `live-trader` Docker image until a spike works.

## QS symbols vs TMGM

The bot only trades the QuantumSignals catalog. TMGM FX/crypto extra book does not matter until QS emits those names.

Likely map (confirm on **your** MT5 Market Watch; suffixes vary by server):

| QS canonical (aliases) | Likely TMGM symbol |
|------------------------|--------------------|
| ES (`S&P500`, `SPY`, `MES`) | US500 |
| NQ (`Nasdaq100`, `QQQ`, `MNQ`) | NAS100 |
| YM (`Dow30`, `DIA`, `MYM`) | US30 |
| RTY (`Russell2000`, `IWM`, `M2K`) | US2000 |
| CL (`Crude Oil`, `USO`) | XTIUSD or USOIL |
| GC (`Gold`, `GLD`) | XAUUSD |
| SI (`Silver`, `SLV`) | XAGUSD |
| AAPL | AAPL.NAS |
| NG (`Natural Gas`, `UNG`) | probably unmapped |

Alpaca already maps YM→DIA and RTY→IWM (ETFs, not futures). Empty `TRADER_ALLOWED_SYMBOLS` = full catalog; `.env.example` `ES,NQ` would exclude YM/RTY.

TMGM sizing is **lots** (often 0.01 / 0.1), not shares. Prefer a **netting** account so momentum’s net position model works. CFD contract sizes ≠ `config.py` futures multipliers.

## Accounts (do not commit passwords)

Portal **Client ID** (e.g. `11827580`) and portal email/password are **not** MT logins.

**Live (do not fund for testing)**

- Created via “Try Demo Account” but provisioned as **live**
- Login `301482533`, MT5 Edge, server `TradeMaxGlobal-Live2`, $0
- Portal showed Live Accounts only; no Demo tab on that profile at first

**Demo**

- Later created successfully; **Type is MT5**
- TMGM **WebTrader is MT4-only** — MT5 demo login will **fail** there. That is expected. Ignore WebTrader.
- Use desktop **MetaTrader 5** (ForexVPS) with demo login + **demo** server (not `TradeMaxGlobal-Live2`) + trading password.

Jack said demo needs no special access: create in the portal. In practice “Try Demo” can funnel into a live profile; WebTrader “Demo” is a marketing redirect.

## Emails / people

- **Jack** — TMGM. No API; MT + optional ForexVPS; demo in portal.
- **Yianni** — introduced Arij; asked to document accounts and keep him + Alexander informed. Chose ForexVPS over Wine for now.
- **Alex** — preferred Wine first; then preoccupied.

## In-repo work (not started)

When MT5 is logged into the **demo** on a Windows VPS:

1. Spike script: `initialize`, `account_info`, ticks on mapped symbols, one tiny demo order + flatten.
2. Package `qs-trader-broker-tmgm`: `TmgmSettings` (`TMGM_*`), `TmgmBroker`, entry point `tmgm`.
3. Catalog: `tmgm_symbol` + `tmgm_symbol_map()`.
4. Root `pyproject.toml` workspace dep + `uv sync`. `TRADER_BROKER=tmgm`.
5. Tests with a fake MT client (CI will not run MT5).
6. Confirm exact Market Watch names and min lots.

Do not implement against the live $0 account.

## Immediate next step

Stand up ForexVPS (or any Windows box) → install TMGM MT5 → log in with the **MT5 demo** → run the Python spike. Then add the broker plugin.

## Docker note (unrelated, this thread)

`docker compose up` failed when a leftover `src/brokers/rithmic` directory existed without `pyproject.toml` (uv workspace glob `src/brokers/*`). Removing that empty dir fixed the build on master (Rithmic is a separate branch).
