# QuantumSignals Python Client — Quickstart

Get started with the QuantumSignals API using the official Python client (`quantumsignals-client` ≥ 0.6.0).

**Live trader integration:** this repo's bot consumes the signal stream automatically — see [README.md](README.md) for Docker deployment and [documentation.md](documentation.md) for how signals map to orders.

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

## Step 2: Fetch Available Models

```python
models = client.get_model_catalog()

for model_id, model in models.items():
    print(f"Model: {model_id}")
    print(f"  Symbols: {', '.join(model.trained_on_symbols)}")
    print(f"  Training period: {model.trained_date_range[0]} to {model.trained_date_range[1]}")
```

Use `trained_on_symbols` to see which symbol names your models emit — important for setting `ALLOWED_SYMBOLS` in the live trader.

---

## Step 3: Stream Trading Signals

```python
for signal in client.stream_signals():
    print(f"{signal.symbol}: {signal.signal}")  # 0=down, 1=stable, 2=up

    if signal.signal == 2:
        print(f"  → BUY signal for {signal.symbol}")
    elif signal.signal == 0:
        print(f"  → SELL signal for {signal.symbol}")
```

The client handles SSE parsing, reconnection, and connection management.

Each signal object includes at minimum:

| Field | Description |
|---|---|
| `symbol` | Instrument name (see [Symbol names](#symbol-names) below) |
| `signal` | `0` = DOWN, `1` = STABLE, `2` = UP |
| `signal_time_utc` | UTC timestamp for the signal bar |
| `model` | Model that produced the signal |
| `is_null_signal()` | Returns `True` for heartbeat / non-actionable events |

---

## Symbol names

QuantumSignals has used several symbol formats across API versions. The live trader accepts all of these in `ALLOWED_SYMBOLS` and maps them to tradable instruments:

| Format | Examples | Notes |
|---|---|---|
| Legacy futures tickers | `ES`, `NQ` | Original stream format |
| Full index names | `S&P500`, `Nasdaq100` | Client ≥ 0.5.1; preferred in backtest APIs |
| ETF tickers | `SPY`, `QQQ` | Client ≥ 0.6.0 / v3 endpoint; handled by `momentum` strategy |

When querying backtests, either form usually works:

```python
symbol = "S&P500"   # preferred
symbol = "ES"       # legacy alias
```

Set `ALLOWED_SYMBOLS` using the **QS signal name**, not the ETF ticker the bot trades on Alpaca (e.g. `S&P500,Nasdaq100`, not `SPY,QQQ`).

---

## Step 4: Fetch Backtest Results

```python
model_family = "Pythia-bB8ES"
symbol = "S&P500"

metadata = client.get_backtest_metadata(
    model_family=model_family,
    symbol=symbol,
)
print(f"Data available: {metadata['min_date']} to {metadata['max_date']}")
print(f"Total rows: {metadata['row_count']}")

results = client.get_historical_predictions(
    model_family=model_family,
    symbol=symbol,
    format="json",
    page=1,
    page_size=100,
)

for row in results["data"]:
    print(f"Time: {row['signal_time_utc']}, Prediction: {row['prediction']}, Label: {row['label']}")

daily = client.get_daily_backtests(
    model_family=model_family,
    symbol=symbol,
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
    print("Available models:")
    models = client.get_model_catalog()
    for model_id, model in models.items():
        print(f"  - {model_id}: {', '.join(model.trained_on_symbols)}")

    print("\nStreaming signals (Ctrl+C to stop)...")
    for signal in client.stream_signals():
        print(f"{signal.signal_time_utc} | {signal.symbol}: {signal.signal}")

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
    models = client.get_model_catalog()
    print(f"Found {len(models)} models")

    for signal in client.stream_signals():
        process_signal(signal)
# Client automatically closes here
```

---

## Advanced: Direct HTTP Integration

For non-Python integrations, use raw HTTP with `httpx`. The Python client is still recommended — it handles auth, SSE parsing, and errors.

### Getting models

```python
import httpx

API_KEY = "your-api-key-here"
BASE_URL = "https://api.quantumsignals.ai"

async def get_models():
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{BASE_URL}/v2/models",
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
            f"{BASE_URL}/v2/signals",
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
async def get_historical_predictions(model_family: str, symbol: str):
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{BASE_URL}/v2/backtest/historical_minute_predictions",
            params={
                "model_family": model_family,
                "symbol": symbol,
                "format": "json",
                "page": 1,
                "page_size": 100,
            },
            headers={"x-api-key": API_KEY},
        )
        response.raise_for_status()
        return response.json()

# Usage — either symbol form works
predictions = await get_historical_predictions("Pythia-bB8ES", "S&P500")
predictions = await get_historical_predictions("Pythia-bB8ES", "ES")
```

---

## Smoke test in this repo

```bash
uv run python scripts/test_qs_connection.py
```

Validates your API key and prints the live signal stream without placing any orders.
