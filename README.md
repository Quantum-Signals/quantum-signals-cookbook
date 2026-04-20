# Live Trader

Algorithmic trading bot that streams real-time signals from [QuantumSignals](https://quantumsignals.io) and executes trades through Interactive Brokers (or a built-in simulated broker for paper testing).

## Features

- **Real-time signal streaming** — consumes QuantumSignals SSE feed in a background thread with automatic reconnection and backoff.
- **Two strategies out of the box**
  - `momentum` — interval-based net position adjustments, maps futures signals (ES → SPY, NQ → QQQ).
  - `signal_follower` — intraday session rules with ET time windows, 5-minute decision grid, and automatic flattening near market close.
- **Dual broker support** — live trading via IB Gateway / TWS, or a fully simulated gateway for dry-run testing.
- **Session lifecycle** — position sync on startup, optional flatten-on-exit, and a PnL session summary on shutdown.
- **Structured logging** — console + daily rotating log files via Loguru.

## Prerequisites

- **Python 3.12+**
- A **QuantumSignals** account and API key
- **IB Gateway** or **TWS** running locally (only required for live trading)

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/<your-org>/live-trader.git
cd live-trader
```

### 2. Install dependencies

The project uses [uv](https://docs.astral.sh/uv/) for dependency management. A `uv.lock` is included.

```bash
# With uv (recommended)
uv sync

# Or with pip (editable install)
pip install -e .
```

### 3. Configure environment variables

Create a `.env` file in the project root:

```env
QUANTUMSIGNALS_API_KEY=your_api_key_here

# Broker mode — set to "false" for live IB trading
DRY_RUN=false

# IB Gateway connection (ignored when DRY_RUN=true)
IB_HOST=127.0.0.1
IB_PORT=4002
IB_CLIENT_ID=1

# Strategy: "momentum": execute every execution interval or "signal_follower": execute every minute
STRATEGY=momentum 
```

See the full configuration reference below for all available variables.

### 4. Run the bot

```bash
# Via the installed console script, default execution interval is 5
live-trader 

# Override the momentum execution interval (minutes)
live-trader --execution-interval 10
```

Press `Ctrl-C` to gracefully shut down. The bot will flatten open positions (if `CLOSE_ON_EXIT` is enabled) and print a session summary.

## Configuration Reference

All settings are loaded from environment variables and/or a `.env` file.

| Variable | Default | Description |
|---|---|---|
| `QUANTUMSIGNALS_API_KEY` | *(required)* | Your QuantumSignals API key |
| `DRY_RUN` | `true` | `true` uses the simulated gateway; `false` connects to IB |
| `IB_HOST` | `127.0.0.1` | IB Gateway / TWS host |
| `IB_PORT` | `4002` | IB Gateway port (`4001` for TWS live, `4002` for TWS paper / IB Gateway) |
| `IB_CLIENT_ID` | `1` | IB API client ID |
| `IB_MARKET_DATA_TYPE` | `4` | `1` = live, `3` = delayed, `4` = delayed-frozen |
| `STRATEGY` | `momentum` | `momentum` or `signal_follower` |
| `ALLOWED_SYMBOLS` | *(empty — all)* | Comma-separated symbol filter, e.g. `ES,NQ,SPY` |
| `CLOSE_ON_EXIT` | `true` | Flatten all positions on shutdown |
| `DAILY_START_CAPITAL` | `100000` | Notional capital for return % display |
| `SIGNAL_QUEUE_MAXSIZE` | `0` | Max queued signals (`0` = unlimited) |
| `EXECUTION_INTERVAL_MINUTES` | `5` | Minutes between momentum execution decisions |

## Project Structure

```
live-trader/
├── main.py                     # Entry point
├── pyproject.toml              # Project metadata and dependencies
├── src/trader/
│   ├── app.py                  # Main bot loop, logging, shutdown
│   ├── config.py               # Settings dataclass, env/CLI loading
│   ├── models.py               # Signal enum, Position, PerformanceTracker
│   ├── streaming.py            # QS SSE background thread with retries
│   ├── reporting.py            # Periodic account and PnL logging
│   ├── gateways/
│   │   ├── base.py             # Abstract broker interface
│   │   ├── ibkr.py             # Interactive Brokers gateway (ib-async)
│   │   └── simulated.py        # In-memory simulated broker
│   └── strategy/
│       ├── base.py             # Strategy interface
│       ├── momentum.py         # Interval-based momentum strategy
│       └── signal_follower.py  # Intraday session-aware strategy
├── tests/                      # Unit tests
└── scripts/
    └── test_qs_connection.py   # Standalone QS connectivity check
```

## Strategies

### Momentum

Accumulates signals over a configurable interval and adjusts net position accordingly. Futures symbols (ES, NQ) are mapped to their ETF counterparts (SPY, QQQ) for order execution.

### Signal Follower

Follows intraday trading rules anchored to Eastern Time sessions. Uses a 5-minute decision grid, avoids trading outside configured windows, and automatically flattens positions before market close.

## Supported Symbols

The default symbol map covers major futures and ETFs:

| Futures | Stocks / ETFs |
|---|---|
| ES, NQ, YM, RTY | SPY, QQQ, AAPL |
| MES, MNQ (micros) | |
| CL (crude oil), GC (gold) | |

Position sizes and contract multipliers are pre-configured in `src/trader/config.py` and can be adjusted there.

## Testing

```bash
# Run the full test suite
pytest

# Verify QuantumSignals connectivity (requires API key in env)
python scripts/test_qs_connection.py
```

## License

See [LICENSE](LICENSE) for details.
