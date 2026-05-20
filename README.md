# QuantumSignals Live Trader

An algorithmic trading bot that streams real-time signals from [QuantumSignals](https://quantumsignals.io) and executes trades automatically through **Alpaca** (or Interactive Brokers).

**Developer documentation:** see [documentation.md](documentation.md) for architecture, module map, position-sizing logic, and local development.

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) installed on your machine
- A **QuantumSignals** API key — get one at [quantumsignals.io](https://quantumsignals.io)
- An **Alpaca** account and API keys — get them at [alpaca.markets](https://alpaca.markets)

---

## Quick Start

### 1. Create your `.env` file

Create a file named `.env` in any folder on your machine and fill in your keys:

```env
# ── Required ──────────────────────────────────────────────
QUANTUMSIGNALS_API_KEY=your_qs_api_key_here
ALPACA_API_KEY=your_alpaca_api_key_here
ALPACA_SECRET_KEY=your_alpaca_secret_key_here

# ── Safety ────────────────────────────────────────────────
# Set to "false" to trade real money. Starts as paper trading.
DRY_RUN=false
ALPACA_PAPER=true

# ── Strategy ──────────────────────────────────────────────
STRATEGY=momentum
ALLOWED_SYMBOLS=ES,NQ

# ── Execution ─────────────────────────────────────────────
EXECUTION_INTERVAL_MINUTES=5
DAILY_START_CAPITAL=100000
CLOSE_ON_EXIT=true
```

### 2. Run the bot

```bash
docker run -d \
  --name live-trader \
  --env-file .env \
  --restart unless-stopped \
  -v ./logs:/app/logs \
  quantumsignals/live-trader:latest
```

The bot starts immediately, streams signals, and executes trades. Logs are saved to the `./logs` folder.

### 3. Check logs

```bash
# Follow live output
docker logs -f live-trader

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

### 4. Stop the bot

```bash
docker stop live-trader
```

If `CLOSE_ON_EXIT=true` (the default), all open positions are automatically flattened on shutdown.

---

## Configuration Reference

All settings are passed as environment variables in the `.env` file.

### Required

| Variable | Description |
|---|---|
| `QUANTUMSIGNALS_API_KEY` | Your QuantumSignals API key |
| `ALPACA_API_KEY` | Your Alpaca API key |
| `ALPACA_SECRET_KEY` | Your Alpaca secret key |

### Broker

| Variable | Default | Description |
|---|---|---|
| `BROKER` | `alpaca` | Broker to use: `alpaca` or `ibkr` |
| `ALPACA_PAPER` | `true` | `true` = Alpaca paper account, `false` = live account |
| `DRY_RUN` | `true` | `true` = no orders placed at all (full simulation) |

### Strategy

| Variable | Default | Description |
|---|---|---|
| `STRATEGY` | `momentum` | Trading strategy: `momentum` or `signal_follower` |
| `ALLOWED_SYMBOLS` | *(all)* | Comma-separated symbol filter, e.g. `ES,NQ,SPY` |
| `EXECUTION_INTERVAL_MINUTES` | `5` | Minutes between momentum execution decisions |

### Risk & Behaviour

| Variable | Default | Description |
|---|---|---|
| `CLOSE_ON_EXIT` | `true` | Flatten all open positions when the bot stops |
| `DAILY_START_CAPITAL` | `100000` | Notional capital used for return % in the session summary |
| `SIGNAL_QUEUE_MAXSIZE` | `0` | Max buffered signals (`0` = unlimited) |

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
| `SESSION_START_TIME` | `09:30` | Local time at which a new trading day resumes |
| `SESSION_END_TIME`   | `15:55` | Local time at which positions are flattened and the daily summary is logged |
| `SESSION_TIMEZONE`   | `America/New_York` | Timezone for the times above |
| `RUN_DAYS`           | `0` | `0` = run indefinitely; `1` = exit after first EOD (legacy single-day behaviour); `N` = exit after N EODs |

Weekends are skipped automatically. US holidays are **not** detected — on a
holiday the bot wakes up normally but the QS stream simply produces no
actionable signals, so the day passes with an empty summary.

### Interactive Brokers (optional — only if `BROKER=ibkr`)

| Variable | Default | Description |
|---|---|---|
| `IB_HOST` | `host.docker.internal` | Host running IB Gateway / TWS |
| `IB_PORT` | `4002` | IB Gateway port (`4001` for TWS live, `4002` for paper) |
| `IB_CLIENT_ID` | `1` | IB API client ID |
| `IB_MARKET_DATA_TYPE` | `4` | `1` = live, `3` = delayed, `4` = delayed-frozen |

---

## Strategies

### `momentum` (default)

Accumulates QuantumSignals signals over a configurable interval (`EXECUTION_INTERVAL_MINUTES`) and adjusts the net position at each interval boundary. Futures symbols (ES, NQ) are automatically mapped to their ETF counterparts (SPY, QQQ) for execution on Alpaca.

### `signal_follower`

Follows intraday trading rules anchored to Eastern Time sessions. Uses a 5-minute decision grid, avoids trading outside market hours, and automatically flattens positions before market close.

---

## Supported Symbols

| Futures | ETFs / Stocks |
|---|---|
| ES, NQ, YM, RTY | SPY, QQQ, AAPL |
| MES, MNQ (micros) | |
| CL (crude oil), GC (gold) | |

---

## Paper Trading vs Live Trading

The bot has two independent safety switches:

| `DRY_RUN` | `ALPACA_PAPER` | Result |
|---|---|---|
| `true` | any | No orders sent anywhere — full local simulation |
| `false` | `true` | Real orders sent to Alpaca **paper** account |
| `false` | `false` | Real orders sent to Alpaca **live** account — real money |

Start with `DRY_RUN=true` to verify the bot connects and streams signals correctly before switching to paper or live trading.

---

## Using Docker Compose (alternative)

Create a `docker-compose.yml` alongside your `.env` file:

```yaml
services:
  trader:
    image: quantumsignals/live-trader:latest
    env_file: .env
    volumes:
      - ./logs:/app/logs
    restart: unless-stopped
```

Then run:

```bash
docker compose up -d       # start
docker compose logs -f     # follow logs
docker compose down        # stop
```

---

## Updating

Pull the latest image and restart:

```bash
docker pull quantumsignals/live-trader:latest
docker stop live-trader && docker rm live-trader
# re-run the docker run command from Quick Start
```

Or with Docker Compose:

```bash
docker compose pull && docker compose up -d
```
