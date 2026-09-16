# QuantumSignals Python Client — Quickstart

Get started with the QuantumSignals API using the official Python client (`quantumsignals-client` ≥ 0.9.0).

**Live trader integration:** this repo's bot consumes the signal stream automatically — see [README.md](README.md) for Docker deployment and [architecture.md](architecture.md) for how signals map to orders.

---

## Prerequisites

- Python 3.12+ (3.10+ supported by the client; this repo requires 3.12)
- A QuantumSignals API key from [quantumsignals.io](https://quantumsignals.io) or your administrator

## Installation

```bash
pip install quantumsignals-client
# or in this repo:
uv sync
```

---

## Step 1: Authenticate

The client uses API key authentication:

```python
from quantumsignals.client import Client

client = Client(api_key="qsk_your_api_key_here")
print("Client ready!")
```

For production, load the key from the environment (same variable the live trader uses):

```python
import os
from quantumsignals.client import Client

client = Client(api_key=os.environ["QUANTUMSIGNALS_API_KEY"])
```

---

## Step 2: Browse the Signal Catalog

Commerce signals are identified by a **signal_id** slug (e.g. `5min_sp500`). The catalog describes each signal's metadata, including the symbol name emitted on the stream.

```python
catalog = client.get_signal_catalog()
subscriptions = client.get_subscriptions()
subscribed_ids = {sub.signal_id for sub in subscriptions}

for signal_id, meta in catalog.items():
    sym = meta.model_output_symbol or "?"
    sub = "subscribed" if signal_id in subscribed_ids else "not subscribed"
    print(f"{signal_id}: {sym} ({sub})")
```

Use `model_output_symbol` to see which symbol names your signals emit — important for setting `TRADER_ALLOWED_SYMBOLS` in the live trader.

---

## Step 3: Stream Trading Signals

The live stream uses the v3 signals endpoint. By default you receive every signal your API key is entitled to; pass `signals=[...]` to filter by signal_id slug.

```python
for signal in client.stream_signals():
    print(f"{signal.symbol}: {signal.signal}")  # 0=down, 1=stable, 2=up

    if signal.signal == 2:
        print(f"  → BUY signal for {signal.symbol}")
    elif signal.signal == 0:
        print(f"  → SELL signal for {signal.symbol}")
```

Filter to specific signals:

```python
for signal in client.stream_signals(signals=["5min_sp500", "5min_nq"]):
    print(f"{signal.signal_id} {signal.symbol}: {signal.signal}")
```

The client handles SSE parsing, reconnection, and connection management.

Each signal object includes at minimum:

| Field | Description |
|---|---|
| `signal_id` | Commerce slug for this signal (e.g. `5min_sp500`) |
| `symbol` | Instrument name (see [Symbol names](#symbol-names) below) |
| `signal` | `0` = DOWN, `1` = STABLE, `2` = UP |
| `signal_time_utc` | UTC timestamp for the signal bar |
| `model` | Model that produced the signal |
| `is_null_signal()` | Returns `True` for heartbeat / non-actionable events |

---

## Pull Recent Signals

For one-off reads instead of a live stream:

```python
recent = client.get_recent_signals(signals=["5min_sp500"], last=10)
for signal in recent.signals:
    print(signal.signal_time_utc, signal.symbol, signal.signal)
```

---

## Symbol names

QuantumSignals may emit several alias formats for the same instrument. The live trader accepts all of these in `TRADER_ALLOWED_SYMBOLS` and maps them to tradable instruments:

| Format | Examples | Notes |
|---|---|---|
| Futures roots | `ES`, `NQ` | Common stream format |
| Full index names | `S&P500`, `Nasdaq100` | Also used in catalog metadata |
| ETF tickers | `SPY`, `QQQ` | Mapped by the `momentum` strategy on Alpaca |

Set `TRADER_ALLOWED_SYMBOLS` using the **QS signal symbol**, not necessarily the ETF ticker the bot trades on Alpaca (e.g. `S&P500,Nasdaq100`, not `SPY,QQQ`).

---

## Step 4: Fetch Backtest Results

Backtests are keyed by **signal_id**:

```python
signal_id = "5min_sp500"

metadata = client.get_backtest_metadata(signal_id=signal_id)
print(f"Data available: {metadata['min_date']} to {metadata['max_date']}")
print(f"Total rows: {metadata['row_count']}")

results = client.get_historical_predictions(
    signal_id=signal_id,
    format="json",
    page=1,
    page_size=100,
)

for row in results["data"]:
    print(f"Time: {row['signal_time_utc']}, Prediction: {row['prediction']}, Label: {row['label']}")

daily = client.get_daily_backtests(
    signal_id=signal_id,
    format="json",
)

for day in daily["data"]:
    print(f"Date: {day['accuracy_date']}, Accuracy: {day['accuracy_pct']:.1f}%")
```

---

## Complete Example

```python
from quantumsignals.client import Client, APIError

client = Client(api_key="qsk_your_api_key_here")

try:
    catalog = client.get_signal_catalog()
    subscriptions = client.get_subscriptions()
    print(f"Signal catalog: {len(catalog)} visible, {len(subscriptions)} subscribed")
    for signal_id, meta in catalog.items():
        print(f"  - {signal_id}: {meta.model_output_symbol}")

    print("\nStreaming signals (Ctrl+C to stop)...")
    for signal in client.stream_signals():
        print(f"{signal.signal_time_utc} | {signal.signal_id} | {signal.symbol}: {signal.signal}")

        if signal.is_null_signal():
            continue
        if signal.signal == 2:
            print(f"  → Executing BUY for {signal.symbol}")
        elif signal.signal == 0:
            print(f"  → Executing SELL for {signal.symbol}")

except APIError as e:
    print(f"API error: {e}")
except KeyboardInterrupt:
    print("\nStopped by user")
finally:
    client.close()
```

### Context manager (recommended)

```python
from quantumsignals.client import Client

with Client(api_key="qsk_your_api_key_here") as client:
    catalog = client.get_signal_catalog()
    print(f"Found {len(catalog)} signals")

    for signal in client.stream_signals():
        process_signal(signal)
# Client automatically closes here
```

---

## Advanced: Direct HTTP Integration

For non-Python integrations, use raw HTTP with `httpx`. The Python client is still recommended — it handles auth, SSE parsing, and errors.

### Signal catalog

```python
import httpx

API_KEY = "your-api-key-here"
BASE_URL = "https://api.quantumsignals.ai"

async def get_signal_catalog():
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{BASE_URL}/v3/commerce/catalog",
            headers={"x-api-key": API_KEY},
        )
        response.raise_for_status()
        return response.json()
```

### Streaming signals

```python
import httpx
import json

async def stream_signals():
    async with httpx.AsyncClient() as client:
        async with client.stream(
            "GET",
            f"{BASE_URL}/v3/signals",
            headers={"x-api-key": API_KEY},
            timeout=None,
        ) as response:
            response.raise_for_status()

            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    signal_data = line[5:].strip()
                    signal = json.loads(signal_data)
                    print(f"Signal: {signal}")
```

### Backtest endpoints

```python
async def get_historical_predictions(signal_id: str):
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{BASE_URL}/v3/backtest/historical_minute_predictions",
            params={
                "signal_id": signal_id,
                "format": "json",
                "page": 1,
                "page_size": 100,
            },
            headers={"x-api-key": API_KEY},
        )
        response.raise_for_status()
        return response.json()

predictions = await get_historical_predictions("5min_sp500")
```

---

## Smoke test in this repo

```bash
uv run python scripts/smoke_qs_connection.py
```

Validates your API key, prints the signal catalog, and streams live signals without placing any orders.
