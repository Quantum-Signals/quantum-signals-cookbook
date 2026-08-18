# QuantumSignals Live Trader

An algorithmic trading bot that streams real-time signals from [QuantumSignals](https://quantumsignals.io) and executes trades automatically through **Alpaca** or **Interactive Brokers**.

**Developer documentation:** [documentation.md](documentation.md) — architecture, module map, position-sizing logic, local development.

**QuantumSignals API client:** [QS documentation.md](QS%20documentation.md) — signal streaming, symbol names, backtests.

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) installed on your machine
- A **QuantumSignals** API key — get one at [quantumsignals.io](https://quantumsignals.io)
- An **Alpaca** account and API keys (only when `DRY_RUN=false` and `BROKER=alpaca`) — [alpaca.markets](https://alpaca.markets)

---

## Quick Start

### 1. Create your `.env` file

Copy `.env.example` or create a file named `.env` in any folder on your machine:

```env
# ── Required ──────────────────────────────────────────────
QUANTUMSIGNALS_API_KEY=your_qs_api_key_here

# ── Safety (start here — no broker credentials needed) ───
DRY_RUN=true

# ── Paper trading (step 2 — uncomment when ready) ─────────
# DRY_RUN=false
# BROKER=alpaca
# ALPACA_API_KEY=your_alpaca_api_key_here
# ALPACA_SECRET_KEY=your_alpaca_secret_key_here
# ALPACA_PAPER=true

# ── Strategy ──────────────────────────────────────────────
STRATEGY=momentum
ALLOWED_SYMBOLS=ES,NQ          # or S&P500,Nasdaq100 (QS client ≥ 0.6.0)

# ── Execution ─────────────────────────────────────────────
EXECUTION_INTERVAL_MINUTES=5
DAILY_START_CAPITAL=100000
CLOSE_ON_EXIT=true
```

See [Paper Trading vs Live Trading](#paper-trading-vs-live-trading) for the recommended progression from simulation → paper → live.

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

All settings are passed as environment variables in the `.env` file (or via Docker
`--env-file`). One CLI override is also supported:

```bash
uv run live-trader --execution-interval 10   # overrides EXECUTION_INTERVAL_MINUTES
```

### Required

| Variable | Description |
|---|---|
| `QUANTUMSIGNALS_API_KEY` | Your QuantumSignals API key (always required, even in `DRY_RUN`) |

Alpaca credentials are required only when `DRY_RUN=false` **and** `BROKER=alpaca`:

| Variable | Description |
|---|---|
| `ALPACA_API_KEY` | Your Alpaca API key |
| `ALPACA_SECRET_KEY` | Your Alpaca secret key |

### Broker

| Variable | Default | Description |
|---|---|---|
| `DRY_RUN` | `true` | `true` = simulated broker — no real orders, no Alpaca/IB connection needed for trading |
| `BROKER` | `alpaca` | Broker to use: `alpaca` or `ibkr` (ignored when `DRY_RUN=true`) |
| `ALPACA_PAPER` | `true` | `true` = Alpaca paper account, `false` = Alpaca live account (real money) |

### Strategy

| Variable | Default | Description |
|---|---|---|
| `STRATEGY` | `momentum` | Trading strategy: `momentum` or `signal_follower` |
| `ALLOWED_SYMBOLS` | *(all)* | Comma-separated QS symbol filter, e.g. `ES,NQ` or `S&P500,Nasdaq100`. Empty = allow all mapped symbols |
| `EXECUTION_INTERVAL_MINUTES` | `5` | **momentum:** minutes between executions per symbol (gated on signal timestamp). **signal_follower:** N-minute decision grid |

### Risk & Behaviour

| Variable | Default | Description |
|---|---|---|
| `CLOSE_ON_EXIT` | `true` | Flatten all open positions when the bot stops |
| `DAILY_START_CAPITAL` | `100000` | **momentum:** splits this notional across active symbols for order sizing. **signal_follower:** used only for return % in log summaries |
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
| `IB_HOST` | `127.0.0.1` | Host running IB Gateway / TWS. Use `host.docker.internal` in Docker on Mac/Windows |
| `IB_PORT` | `4002` | IB Gateway port (`4001` for TWS live, `4002` for paper) |
| `IB_CLIENT_ID` | `1` | IB API client ID |
| `IB_MARKET_DATA_TYPE` | `4` | `1` = live, `3` = delayed, `4` = delayed-frozen |

---

## Strategies

Both strategies consume minute-level signals from QuantumSignals and place orders
through the configured broker. On **Alpaca**, futures signal symbols are mapped to
correlated ETFs (see [Supported Symbols](#supported-symbols)). On **IBKR**, native
futures can be traded directly when listed in the symbol map.

### `momentum` (default)

Incrementally builds a sized position on a fixed time grid (`EXECUTION_INTERVAL_MINUTES`, default 5 min).

- **When it trades:** at most **one execution per symbol per interval**, gated on the signal's own timestamp (not wall-clock arrival time).
- **Order size:** each UP/DOWN slice is sized so daily notional ≈ `DAILY_START_CAPITAL / n_active_symbols` (e.g. $100k across ES + NQ → ~$50k per symbol per day).
- **Broker reconciliation:** every tick re-reads the broker position and plans close-then-open legs, so failed orders, partial fills, or manual trades self-heal on the next interval.
- **Fractional shares:** enabled on Alpaca (fractional longs; whole-share shorts with a reserve for sub-share intent). IBKR uses whole shares.
- **Signal symbols:** `ES`, `NQ`, `S&P500`, `Nasdaq100`, `SPY`, `QQQ` (QS client ≥ 0.6.0 may emit full names or ETF tickers directly).

### `signal_follower`

Discrete intraday strategy that trades **exactly 1 share** per actionable signal minute on the mapped ETF.

- **Decision grid:** on every N-minute boundary (`EXECUTION_INTERVAL_MINUTES`), the latest signal sets an *active decision* — UP → go long, DOWN → go short, STABLE → pause.
- **Execution:** every signal minute inside the trading window with an active decision places a 1-share BUY or SELL.
- **ET session windows (hardcoded):**
  - **Pre-market** (before 9:45 AM): flatten any stale broker positions
  - **Trading** (9:45 AM – 3:30 PM): execute active decisions
  - **Close** (3:55 – 4:00 PM): flatten all positions (also driven by `tick()` even without new signals)
- **Signal symbols:** `ES`, `NQ`, `S&P500`, `Nasdaq100` → traded as `SPY` / `QQQ` on Alpaca.
- **`DAILY_START_CAPITAL`:** does not affect order size — only the return % shown in day summaries.

| Aspect | `momentum` | `signal_follower` |
|---|---|---|
| Order size | Notional / interval / price | 1 share |
| Trade frequency | Once per interval per symbol | Every signal minute with active decision |
| Session logic | App supervisor (`SESSION_*`) | Hardcoded ET windows + app EOD |
| Best for | Sized, interval-based exposure | Simple 1-share-per-minute following |

---

## Supported Symbols

QuantumSignals may emit signals under **legacy futures tickers** (`ES`, `NQ`) or
**full names** (`S&P500`, `Nasdaq100`). Client ≥ 0.6.0 may also emit ETF tickers
(`SPY`, `QQQ`) directly — handled by `momentum` only.

### Signal → execution mapping

| QS signal symbol | Alpaca (ETF) | IBKR (native) |
|---|---|---|
| `ES`, `S&P500` | SPY | ES futures |
| `NQ`, `Nasdaq100` | QQQ | NQ futures |
| `SPY` | SPY | SPY stock |
| `QQQ` | QQQ | QQQ stock |

Set `ALLOWED_SYMBOLS` using the **QS signal name** (e.g. `S&P500,Nasdaq100` or
`ES,NQ`), not the ETF ticker.

### IBKR-only symbols (no QS mapping — futures/stocks in symbol map)

These can be traded on IBKR when signals exist and the symbol is allowed, but are
not mapped to ETFs for Alpaca:

| Category | Symbols |
|---|---|
| Index futures | YM, RTY, MES, MNQ |
| Commodity futures | CL (crude oil), GC (gold) |
| Stocks | AAPL |

Alpaca supports **equities and ETFs only** — any futures entry in the symbol map is
rejected at order time.

---

## Paper Trading vs Live Trading

The bot has **three layers** of safety, applied in this order:

1. **`DRY_RUN`** — simulated gateway (`SimulatedGateway`). No broker credentials
   needed for order placement. Use this first to verify signal streaming and strategy
   logic locally.
2. **`BROKER`** — choose Alpaca (REST, ETFs) or IBKR (futures + equities via
   Gateway/TWS). Ignored when `DRY_RUN=true`.
3. **Paper vs live account** — broker-specific switch for real (but fake-money)
   vs real-money execution.

### Alpaca (`BROKER=alpaca`)

| `DRY_RUN` | `ALPACA_PAPER` | Result |
|---|---|---|
| `true` | any | Simulated — no Alpaca connection for orders |
| `false` | `true` | Real orders to Alpaca **paper** account |
| `false` | `false` | Real orders to Alpaca **live** account — **real money** |

### Interactive Brokers (`BROKER=ibkr`)

| `DRY_RUN` | `IB_PORT` | Result |
|---|---|---|
| `true` | any | Simulated — no IB connection for orders |
| `false` | `4002` (default) | Real orders via IB Gateway **paper** |
| `false` | `4001` | Real orders via IB Gateway/TWS **live** — **real money** |

On Docker Desktop (Mac/Windows), set `IB_HOST=host.docker.internal` so the container
can reach IB Gateway on your host. On Linux, use your machine's LAN IP or
`network_mode: host`.

### Recommended progression

```env
# 1. Verify signals + strategy logic (no broker needed)
DRY_RUN=true

# 2. Paper trade with real broker fills
DRY_RUN=false
BROKER=alpaca
ALPACA_PAPER=true

# 3. Live trading — only after paper results look correct
DRY_RUN=false
BROKER=alpaca
ALPACA_PAPER=false
```

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
