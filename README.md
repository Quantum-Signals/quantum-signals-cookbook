# QuantumSignals Live Trader

An algorithmic trading bot that streams real-time signals from [QuantumSignals](https://quantumsignals.ai) and executes trades automatically through a pluggable broker (**Alpaca** included).

**Developer documentation:** [docs/architecture.md](docs/architecture.md): module map, position-sizing logic, local development.

**Broker plugin architecture:** [docs/brokers.md](docs/brokers.md): architecture, implementing your own broker.

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) installed on your machine
- A **QuantumSignals** API key — get one at [quantumsignals.ai](https://quantumsignals.ai)
- For Alpaca: **Alpaca** [account](https://alpaca.markets) and API keys

---

## Quick Start

### 1. Create your `.env` file

Copy `.env.example` or create a new `.env` file in the project folder.

```env
# Required:
QUANTUMSIGNALS_API_KEY=your_qs_api_key_here

# Alpaca paper trading:
TRADER_BROKER=alpaca
ALPACA_API_KEY=your_alpaca_api_key_here
ALPACA_SECRET_KEY=your_alpaca_secret_key_here
ALPACA_PAPER=true

# Strategy:
TRADER_STRATEGY=momentum
TRADER_ALLOWED_SYMBOLS=ES,NQ

# Execution:
TRADER_EXECUTION_INTERVAL_MINUTES=5
TRADER_DAILY_START_CAPITAL=100000
```

### 2. Run with Docker Compose

```bash
docker compose up
```

The bot starts in the foreground, streams signals, and executes trades. Logs are saved to the `./logs` folder.

### 3. Check logs

```bash
# Follow live output
docker compose logs -f

# One log file per trading day per process run.
# Filename = session_<trading-day>_<process-start-time>.log
ls logs/
# session_2026-05-14_09-00-00.log
# session_2026-05-15_09-00-00.log
# session_2026-05-18_09-00-00.log   # Mon (Sat/Sun skipped)
# session_2026-05-18_14-30-15.log   # Same-day restart → new file

# Open today's logs (may be multiple if you restarted)
ls logs/session_$(date +%Y-%m-%d)_*.log
```

### 4. Stop with Docker Compose

```bash
docker compose stop
```

If `TRADER_CLOSE_ON_EXIT=true` (the default), the bot cancels working orders and closes **every position in
the account**, including ones it did not open, whenever it stops. See [Ending every day flat](#ending-every-day-flat).

---

## Configuration Reference

All settings are passed as environment variables in the `.env` file (or via Docker
`--env-file`). One CLI override is also supported:

```bash
uv run live-trader --execution-interval 10   # overrides TRADER_EXECUTION_INTERVAL_MINUTES
```

### Required

| Variable | Description |
|---|---|
| `QUANTUMSIGNALS_API_KEY` | Your QuantumSignals API key (always required, even with the simulated broker) |

Alpaca credentials are required only when `TRADER_BROKER=alpaca`:

| Variable | Description |
|---|---|
| `ALPACA_API_KEY` | Your Alpaca API key |
| `ALPACA_SECRET_KEY` | Your Alpaca secret key |

### Broker

| Variable | Default | Description |
|---|---|---|
| `TRADER_BROKER` | `simulated` | Broker to use. `simulated` = no real orders (no Alpaca connection). Set to `alpaca` (or any other installed broker) to trade for real |
| `ALPACA_PAPER` | `true` | Only applies when `TRADER_BROKER=alpaca`. `true` = Alpaca paper account, `false` = Alpaca live account (real money) |

### Strategy

| Variable | Default | Description |
|---|---|---|
| `TRADER_STRATEGY` | `momentum` | Trading strategy (`momentum` is the only option today) |
| `TRADER_ALLOWED_SYMBOLS` | *(all)* | Comma-separated filter; any alias works (`ES`, `SPY`, `S&P500`). Empty = allow all catalog symbols |
| `TRADER_EXECUTION_INTERVAL_MINUTES` | `5` | Minutes between executions per symbol (gated on signal timestamp) |

### Risk & Behaviour

| Variable | Default | Description |
|---|---|---|
| `TRADER_CLOSE_ON_EXIT` | `true` | Flatten all open positions when the bot stops |
| `TRADER_CLOSE_ON_START` | same as `TRADER_CLOSE_ON_EXIT` | Flatten positions left by an earlier session before trading |
| `TRADER_FOLLOW_SIGNAL_WINDOWS` | `true` | Trade each symbol only inside its signal's published entry window and close it at its exit time |
| `TRADER_US_MARKET_CALENDAR` | `true` | Skip US market holidays; end the session at 12:55 New York on half days |
| `TRADER_DAILY_START_CAPITAL` | `100000` | Splits this notional across active symbols for order sizing (used when `TRADER_SYMBOL_CAPITAL` is not set) |
| `TRADER_SYMBOL_CAPITAL` | *(empty)* | Per-symbol capital allocation (e.g. `ES:60000,NQ:40000`). Overrides equal split of `TRADER_DAILY_START_CAPITAL` |
| `TRADER_SIGNAL_QUEUE_MAXSIZE` | `0` | Max buffered signals (`0` = unlimited) |

#### Per-symbol capital allocation

By default, `TRADER_DAILY_START_CAPITAL` is split equally across all active symbols. Use `TRADER_SYMBOL_CAPITAL` for weighted allocation:

```env
# Equal split (default): $100k ÷ 2 = $50k per symbol
TRADER_ALLOWED_SYMBOLS=ES,NQ
TRADER_DAILY_START_CAPITAL=100000

# Weighted allocation: 60% ES, 40% NQ
TRADER_ALLOWED_SYMBOLS=ES,NQ
TRADER_SYMBOL_CAPITAL=ES:60000,NQ:40000
```

- Format: `SYMBOL:AMOUNT,SYMBOL:AMOUNT` (comma-separated pairs)
- Any alias works: `SPY:60000` is equivalent to `ES:60000`
- **Partial configuration:** symbols not listed get equal share of the **remaining** capital after explicit allocations (not `TRADER_DAILY_START_CAPITAL / n_symbols`)

**Example (partial configuration):**
```env
TRADER_DAILY_START_CAPITAL=150000
TRADER_ALLOWED_SYMBOLS=RTY,YM,ES
TRADER_SYMBOL_CAPITAL=RTY:100000  # Only RTY specified
```
- RTY → $100,000 (explicit)
- YM → $25,000 (remaining $50k ÷ 2)
- ES → $25,000 (remaining $50k ÷ 2)

**Warnings:** The bot logs warnings at startup if:
- A symbol in `TRADER_SYMBOL_CAPITAL` is not in `TRADER_ALLOWED_SYMBOLS` (allocation ignored)
- Explicit allocations exceed `TRADER_DAILY_START_CAPITAL` (unconfigured symbols get $0)

### Session window & multi-day mode

By default the bot runs **indefinitely** across multiple trading days. At the
end of every session it flattens positions, logs a daily summary, resets its
per-day counters, sleeps over the night/weekend, and resumes when the next
weekday's session start arrives. A cumulative run summary is printed when you
finally stop the bot (Ctrl-C).

#### Ending every day flat

With `TRADER_CLOSE_ON_EXIT=true`:

| What happens | What the bot does |
|---|---|
| Session end | Cancels working orders, closes everything and checks with the broker. Not flat: tries again every minute for 10 minutes, then every 15 minutes while idle |
| Ctrl-C, `docker compose stop`, `kill`, closed terminal, crash, lost signal stream | Closes everything, then exits. A second Ctrl-C does not interrupt it; five more force quit |
| Forced kill (`kill -9`), power cut | Nothing can run, so the **next start** closes what was left, before trading |
| Computer asleep at session end | Closes as soon as it wakes; signals received late are not traded |
| US holiday / half day | No session / ends at 12:55 New York, while the market is open |

Trading does not start until the broker confirms the account is flat, so a restart in the middle of a session
also closes that session's positions. If the bot still cannot close something, it logs `STILL OPEN at the broker`:
check for working orders, then close it by hand.

An order whose outcome is unknown (timeout, dropped connection) is sent again only if the broker can confirm that
no order is still working; otherwise it could fill twice and reverse the position.

Each trading day gets its **own log file** under `logs/`, named
`session_<YYYY-MM-DD>_<HH-MM-SS>.log` where the `HH-MM-SS` suffix is the
**process start time**. Overnight idle messages between two sessions land in
the *upcoming* day's file.

- One 4-day uninterrupted run → 4 log files (same start-time suffix).
- Ctrl-C and restart on the same day → that day gets **2 separate files**,
  one for each process run, with their respective start-time suffixes.

| Variable | Default | Description |
|---|---|---|
| `TRADER_SESSION_START_TIME` | `09:30` | Local time at which a new trading day resumes |
| `TRADER_SESSION_END_TIME`   | `15:55` | Local time at which positions are flattened and the daily summary is logged |
| `TRADER_SESSION_TIMEZONE`   | `America/New_York` | Timezone for the times above |
| `TRADER_RUN_DAYS`           | `0` | `0` = run indefinitely; `1` = exit after first EOD (legacy single-day behaviour); `N` = exit after N EODs |

#### Entry and exit times

Each signal publishes how it is traded in its backtest: an entry window and an exit time (for example gold
opens 09:30–09:50 and exits at 13:25 New York). The stream does not enforce them, so the bot does: it trades a
symbol only inside the entry window (end excluded), then holds, and closes it at the exit time, retrying every
minute until it is flat. The times are read from the QuantumSignals catalog at each session start and logged as
`[windows]`. If they cannot be read, the last known ones apply; without any, the bot trades the whole session as
if this were off, and tries to read them again every minute.

An entry window can be shorter than `TRADER_EXECUTION_INTERVAL_MINUTES`; the bot then takes at most one decision in
it, and a STABLE signal counts as that decision. Exit times need `TRADER_CLOSE_ON_EXIT=true`.

Weekends and US market holidays are skipped automatically, and half days end
early (turn this off with `TRADER_US_MARKET_CALENDAR=false`). One-off closures
are not detected.

---

## Strategy (`momentum`)

The bot consumes minute-level signals from QuantumSignals and places orders
through the configured broker. On **Alpaca**, futures signal symbols are mapped to
correlated ETFs (see [Supported Symbols](#supported-symbols)). Each broker maps QS
symbols via the shared catalog in `qs_trader_symbols`.

Incrementally builds a sized position on a fixed time grid (`TRADER_EXECUTION_INTERVAL_MINUTES`, default 5 min).

- **When it trades:** at most **one execution per symbol per interval**, gated on the signal's own timestamp (not wall-clock arrival time).
- **Order size:** each UP/DOWN slice is sized so daily notional ≈ `TRADER_DAILY_START_CAPITAL / n_active_symbols` (e.g. $100k across ES + NQ → ~$50k per symbol per day). Use `TRADER_SYMBOL_CAPITAL` for weighted allocation (e.g. `ES:60000,NQ:40000`).
- **Broker reconciliation:** every tick re-reads the broker position and plans close-then-open legs, so failed orders, partial fills, or manual trades self-heal on the next interval.
- **Fractional shares:** enabled on Alpaca (fractional longs; whole-share shorts with a reserve for sub-share intent). Whole-share brokers round each slice to whole shares.

---

## Supported Symbols

Set `TRADER_ALLOWED_SYMBOLS` using **any equivalent name** for an instrument —
futures ticker (`GC`), full QS name (`Gold`), or ETF (`GLD`). All forms map to
the same product and route to the broker (see startup **Symbol routing** log).

Catalog instruments include ES/NQ (→ SPY/QQQ on Alpaca), YM (→ DIA), RTY (→ IWM),
CL (→ USO), GC (→ GLD), SI (→ SLV), NG (→ UNG), and AAPL. See
`qs_trader_symbols.INSTRUMENTS` for the full alias list.

Alpaca supports **equities and ETFs only** — futures names are mapped to correlated
ETFs. Ironbeam trades the CME Group futures themselves, in the size you
name in `TRADER_ALLOWED_SYMBOLS` (`MES` trades micros, `ES` full size; the most-traded contract month is
resolved daily; see `ironbeam_symbol_map()`). See
[docs/brokers.md](docs/brokers.md).

---

## Paper Trading vs Live Trading

The bot has **two layers** of safety, applied in this order:

1. **`TRADER_BROKER`** — choose an installed broker. `simulated` (the default)
   needs no broker credentials and places no real orders — use it first to
   verify signal streaming and strategy logic locally. Any other value (e.g.
   `alpaca`) trades for real.
2. **Paper vs live account** — broker-specific switch for real (but fake-money)
   vs real-money execution.

### Alpaca (`TRADER_BROKER=alpaca`)

| `ALPACA_PAPER` | Result |
|---|---|
| `true` | Real orders to Alpaca **paper** account |
| `false` | Real orders to Alpaca **live** account — **real money** |

### Ironbeam (`TRADER_BROKER=ironbeam`)

REST API v3, CME Group futures (the size named in `TRADER_ALLOWED_SYMBOLS`) in **whole contracts**, long
and short. There is no fractional trading, so no dust positions.

**Credentials.** Set `IRONBEAM_API_KEY`, `IRONBEAM_USERNAME` (the trader id, e.g.
`DEMO…`) and `IRONBEAM_PASSWORD`. Leave `IRONBEAM_ACCOUNT_ID` empty unless the
login sees several accounts (startup then lists them). `IRONBEAM_BASE_URL`
defaults to `https://api.ironbeamapi.com`; set it only if the onboarding material
names another host.

**Demo vs live.** Live and simulated accounts are separate Ironbeam deployments
with separate keys: the key decides which one you trade, not a flag here (the
startup banner prints `LIVE (IRONBEAM)` for any real broker; the
`Connected to Ironbeam … account=` line shows which account it is).

**First check (read-only, places no orders):**

```bash
uv run python scripts/smoke_ironbeam.py
```

It logs in and prints the contract chosen for each allowed symbol, quotes,
balances and positions.

**Running without Docker.** `IRONBEAM_*` settings are read from the environment,
not from `.env` by the app, so pass the file to uv (Docker Compose already does):

```bash
uv run --env-file .env live-trader
```

**Contract month.** For each product the broker takes, among listed contracts
more than `IRONBEAM_ROLL_DAYS` from expiry, the one with the most open interest
(so silver trades December, not a thin November delivery month), cached per day.
While the account holds a contract, that one is traded until flat.

**Sizing.** Every order is at least one contract (≈ $35k for one MES, ≈ $350k
for one ES or GC, ≈ $200k for one SI at late-2026 prices), whatever `TRADER_DAILY_START_CAPITAL`
says. Repeated signals in one direction add a contract per interval per symbol,
and each contract ties up margin. On a small account use a long interval
(`TRADER_EXECUTION_INTERVAL_MINUTES=30`–`60`) and few symbols.
The contract size is the one you name in `TRADER_ALLOWED_SYMBOLS`: `MES`,
`MNQ`, `MYM`, `M2K`, `MCL`, `MGC` or `SIL` trade micros; `ES`, `NQ`, `YM`,
`RTY`, `CL`, `GC`, `SI` and any other name (`SPY`, `S&P500`, `Gold`, …) trade
full-size contracts, 10× larger (5× for silver), which only suit large
accounts. Natural gas has no micro here. An empty list trades every symbol full size. Naming both
sizes of one product (`ES,MES`) stops the bot at startup.

**Delayed prices.** Without a market-data subscription, the demo account's
quotes can be about 10 minutes old (seen on our demo account in November
2026). The prices the bot logs for its orders then differ from the fills, and
the unrealized PnL Ironbeam reports while positions are open can be off. Realized
PnL comes from the fills and is right.

**Order type.** Orders are market orders by default. With
`IRONBEAM_USE_MARKET_ORDERS=false` they are limit orders at the last price; one
not filled within `IRONBEAM_FILL_TIMEOUT_S` (10 s) stays working, so unfilled
orders can add up between signals until the next close cancels them.
`IRONBEAM_HTTP_TIMEOUT_S` (15 s) limits each request; placing an order always
waits at least 35 s for Ironbeam's answer.

**The whole account.** The bot treats the account as its own: at every close it
cancels **all** working orders, including stops you placed by hand, and closes
every position, including ones it did not open. Use a separate account for
manual trading.

**Order checks.** Ironbeam can reject an order without saying why, so the broker
logs read-only checks (set `IRONBEAM_DIAGNOSTICS=false` to turn them off; they
add about 1.5 s before an order and never block one). Search the log for:

| Log text | Shows |
|---|---|
| `CHECK` (startup) | account trading state, margin room, held contracts, working orders, expiry, thin books |
| `PRE-ORDER` | position, order book, margin before and after, and the expected outcome |
| `ORDER RESULT` | accepted or rejected, whether the expectation held, and the causes that fit |
| `AFTER REJECTION` | account state, all three margin tiers and the contract's session state |

The expectation uses a rule inferred from demo rejections, not a documented one:
maintenance margin after the order must stay under the start-of-day cash. It is
an estimate: on 2026-11-25 an order it expected to be rejected was accepted.
Nothing stops an order on the strength of it.

### Recommended progression

```env
# 1. Verify signals + strategy logic (no broker needed)
TRADER_BROKER=simulated

# 2. Paper trade with real broker fills
TRADER_BROKER=alpaca
ALPACA_PAPER=true

# 3. Live trading — only after paper results look correct
TRADER_BROKER=alpaca
ALPACA_PAPER=false

# Ironbeam: run smoke_ironbeam.py, then the bot on a demo key, before a live key
TRADER_BROKER=ironbeam
```
