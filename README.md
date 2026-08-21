# QuantumSignals Live Trader

An algorithmic trading bot that streams real-time signals from [QuantumSignals](https://quantumsignals.io) and executes trades automatically through a pluggable broker (**Alpaca** included).

**Developer documentation:** [docs/architecture.md](docs/architecture.md): module map, position-sizing logic, local development.

**Broker plugin architecture:** [docs/brokers.md](docs/brokers.md): architecture, implementing your own broker.

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) installed on your machine
- A **QuantumSignals** API key — get one at [quantumsignals.io](https://quantumsignals.io)
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
docker compose up -d
```

The bot starts in the background, streams signals, and executes trades. Logs are saved to the `./logs` folder.

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

If `TRADER_CLOSE_ON_EXIT=true` (the default), all open positions are automatically flattened on shutdown.

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
| `TRADER_BROKER` | `simulated` | Broker to use. `simulated` = no real orders, no Alpaca/IB connection needed. Set to `alpaca` (or any other installed broker) to trade for real |
| `ALPACA_PAPER` | `true` | Only applies when `TRADER_BROKER=alpaca`. `true` = Alpaca paper account, `false` = Alpaca live account (real money) |

### Strategy

| Variable | Default | Description |
|---|---|---|
| `TRADER_STRATEGY` | `momentum` | Trading strategy: `momentum` or `signal_follower` |
| `TRADER_ALLOWED_SYMBOLS` | *(all)* | Comma-separated QS symbol filter, e.g. `ES,NQ` or `S&P500,Nasdaq100`. Empty = allow all mapped symbols |
| `TRADER_EXECUTION_INTERVAL_MINUTES` | `5` | **momentum:** minutes between executions per symbol (gated on signal timestamp). **signal_follower:** N-minute decision grid |

### Risk & Behaviour

| Variable | Default | Description |
|---|---|---|
| `TRADER_CLOSE_ON_EXIT` | `true` | Flatten all open positions when the bot stops |
| `TRADER_DAILY_START_CAPITAL` | `100000` | **momentum:** splits this notional across active symbols for order sizing. **signal_follower:** used only for return % in log summaries |
| `TRADER_SIGNAL_QUEUE_MAXSIZE` | `0` | Max buffered signals (`0` = unlimited) |

### Session window & multi-day mode

By default the bot runs **indefinitely** across multiple trading days. At the
end of every session it flattens positions, logs a daily summary, resets its
per-day counters, sleeps over the night/weekend, and resumes when the next
weekday's session start arrives. A cumulative run summary is printed when you
finally stop the bot (Ctrl-C).

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

Weekends are skipped automatically. US holidays are **not** detected — on a
holiday the bot wakes up normally but the QS stream simply produces no
actionable signals, so the day passes with an empty summary.

---

## Strategies

Both strategies consume minute-level signals from QuantumSignals and place orders
through the configured broker. On **Alpaca**, futures signal symbols are mapped to
correlated ETFs (see [Supported Symbols](#supported-symbols)). Each broker maps QS
symbols to whatever it trades via its own symbol map.

### `momentum` (default)

Incrementally builds a sized position on a fixed time grid (`TRADER_EXECUTION_INTERVAL_MINUTES`, default 5 min).

- **When it trades:** at most **one execution per symbol per interval**, gated on the signal's own timestamp (not wall-clock arrival time).
- **Order size:** each UP/DOWN slice is sized so daily notional ≈ `TRADER_DAILY_START_CAPITAL / n_active_symbols` (e.g. $100k across ES + NQ → ~$50k per symbol per day).
- **Broker reconciliation:** every tick re-reads the broker position and plans close-then-open legs, so failed orders, partial fills, or manual trades self-heal on the next interval.
- **Fractional shares:** enabled on Alpaca (fractional longs; whole-share shorts with a reserve for sub-share intent). Whole-share brokers round each slice to whole shares.
- **Signal symbols:** `ES`, `NQ`, `S&P500`, `Nasdaq100`, `SPY`, `QQQ` (QS client ≥ 0.6.0 may emit full names or ETF tickers directly).

### `signal_follower`

Discrete intraday strategy that trades **exactly 1 share** per actionable signal minute on the mapped ETF.

- **Decision grid:** on every N-minute boundary (`TRADER_EXECUTION_INTERVAL_MINUTES`), the latest signal sets an *active decision* — UP → go long, DOWN → go short, STABLE → pause.
- **Execution:** every signal minute inside the trading window with an active decision places a 1-share BUY or SELL.
- **ET session windows (hardcoded):**
  - **Pre-market** (before 9:45 AM): flatten any stale broker positions
  - **Trading** (9:45 AM – 3:30 PM): execute active decisions
  - **Close** (3:55 – 4:00 PM): flatten all positions (also driven by `tick()` even without new signals)
- **Signal symbols:** `ES`, `NQ`, `S&P500`, `Nasdaq100` → traded as `SPY` / `QQQ` on Alpaca.
- **`TRADER_DAILY_START_CAPITAL`:** does not affect order size — only the return % shown in day summaries.

| Aspect | `momentum` | `signal_follower` |
|---|---|---|
| Order size | Notional / interval / price | 1 share |
| Trade frequency | Once per interval per symbol | Every signal minute with active decision |
| Session logic | App supervisor (`SESSION_*`) | Hardcoded ET windows + app EOD |
| Best for | Sized, interval-based exposure | Simple 1-share-per-minute following |

---

## Supported Symbols

Set `TRADER_ALLOWED_SYMBOLS` using the **QS signal name** (e.g. `S&P500,Nasdaq100` or
`ES,NQ`), not the ETF ticker.

Alpaca supports **equities and ETFs only** — futures signals are mapped to
correlated ETFs, and any non-equity entry in the symbol map is rejected at order
time. Other brokers define their own mappings; see
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
```
