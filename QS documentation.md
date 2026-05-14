Quickstart Guide¶
Get started with the QuantumSignals API in 5 minutes using the official Python client.

Prerequisites¶
Python 3.10+
Installation¶
Install the QuantumSignals Python client using pip or uv:


pip install quantumsignals-client
Step 1: Authenticate¶
The QuantumSignals client uses API key authentication. Obtain your API key from your administrator or the QuantumSignals dashboard:


from quantumsignals.client import Client

# Create client with your API key
client = Client(api_key="qsk_your_api_key_here")

print("Client ready!")
For production use, load the API key from environment variables:


import os
from quantumsignals.client import Client

client = Client(api_key=os.getenv("QUANTUMSIGNALS_API_KEY"))
Step 2: Fetch Available Models¶
Get the catalog of available trading models:


# Get model catalog
models = client.get_model_catalog()

# Display available models
for model_id, model in models.items():
    print(f"Model: {model_id}")
    print(f"  Symbols: {', '.join(model.trained_on_symbols)}")
    print(f"  Training period: {model.trained_date_range[0]} to {model.trained_date_range[1]}")
Step 3: Stream Trading Signals¶
Stream real-time trading signals using a simple iterator pattern:


# Stream signals (runs indefinitely)
for signal in client.stream_signals():
    print(f"{signal.symbol}: {signal.signal}")  # 0 (down/sell), 1 (stable/hold), 2 (up/buy)

    # Process signal
    if signal.signal == 2:
        print(f"  → BUY signal for {signal.symbol}")
    elif signal.signal == 0:
        print(f"  → SELL signal for {signal.symbol}")
The client handles all the complexity of Server-Sent Events (SSE) parsing and connection management.

Step 4: Fetch Backtest Results¶
Access historical backtest results by model family and symbol:


# Specify the model family and symbol to query
model_family = "Pythia-bB8ES"  # Your model family name
symbol = "S&P500"  # Trading symbol, can also use the raw symbol "ES"

# Get metadata to see what data is available
metadata = client.get_backtest_metadata(
    model_family=model_family,
    symbol=symbol
)
print(f"Data available: {metadata['min_date']} to {metadata['max_date']}")
print(f"Total rows: {metadata['row_count']}")

# Get historical minute predictions (paginated)
results = client.get_historical_predictions(
    model_family=model_family,
    symbol=symbol,
    format="json",
    page=1,
    page_size=100
)

for row in results['data']:
    print(f"Time: {row['signal_time_utc']}, Prediction: {row['prediction']}, Label: {row['label']}")

# Get daily accuracy summaries
daily = client.get_daily_backtests(
    model_family=model_family,
    symbol=symbol,
    format="json"
)

for day in daily['data']:
    print(f"Date: {day['accuracy_date']}, Accuracy: {day['accuracy_pct']:.1f}%")
Complete Example¶
Here's a complete example:


from quantumsignals.client import Client, APIError

# Create client with your API key (obtain from your administrator or the QuantumSignals dashboard)
client = Client(api_key="qsk_your_api_key_here")

try:
    # 1. Get available models
    print("Available models:")
    models = client.get_model_catalog()
    for model_id, model in models.items():
        print(f"  - {model_id}: {', '.join(model.trained_on_symbols)}")

    # 2. Stream and process signals
    print("\nStreaming signals (Ctrl+C to stop)...")
    for signal in client.stream_signals():
        print(f"{signal.signal_time_utc} | {signal.symbol}: {signal.signal}")

        # Execute trading logic based on signal
        if signal.signal == 2:
            print(f"  → Executing BUY for {signal.symbol}")
            # execute_buy(signal.symbol)
        elif signal.signal == 0:
            print(f"  → Executing SELL for {signal.symbol}")
            # execute_sell(signal.symbol)

except APIError as e:
    print(f"API error: {e}")
except KeyboardInterrupt:
    print("\nStopped by user")
finally:
    client.close()
Best Practice: Use a context manager to ensure proper cleanup:


from quantumsignals.client import Client

with Client(api_key="qsk_your_api_key_here") as client:
    models = client.get_model_catalog()
    print(f"Found {len(models)} models")

    for signal in client.stream_signals():
        process_signal(signal)
# Client automatically closes here
Next Steps¶
Python Client Documentation - Complete guide to the Python client
Authentication Guide - Detailed security best practices
Streaming API Reference - Advanced SSE usage patterns
API Reference - Complete endpoint documentation
Advanced: Direct HTTP Integration¶
For users who cannot use the Python client (e.g., integrating from other languages or custom tooling), here are examples using raw HTTP calls with httpx.

Getting Models (httpx)¶

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

models = await get_models()
Streaming Signals (httpx)¶

import httpx
import json

async def stream_signals():
    async with httpx.AsyncClient() as client:
        async with client.stream(
            "GET",
            f"{BASE_URL}/v2/signals",
            headers={"x-api-key": API_KEY},
            timeout=None,  # No timeout for streaming
        ) as response:
            response.raise_for_status()

            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    signal_data = line[5:].strip()
                    signal = json.loads(signal_data)
                    print(f"Signal: {signal}")

await stream_signals()
Getting Backtest Results (httpx)¶

import httpx

async def get_historical_predictions(model_family: str, symbol: str):
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{BASE_URL}/v2/backtest/historical_minute_predictions",
            params={
                "model_family": model_family,
                "symbol": symbol,
                "format": "json",
                "page": 1,
                "page_size": 100
            },
            headers={"x-api-key": API_KEY},
        )
        response.raise_for_status()
        return response.json()

async def get_daily_backtests(model_family: str, symbol: str):
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{BASE_URL}/v2/backtest/daily_backtests",
            params={
                "model_family": model_family,
                "symbol": symbol,
                "format": "json"
            },
            headers={"x-api-key": API_KEY},
        )
        response.raise_for_status()
        return response.json()

# Usage
predictions = await get_historical_predictions("Pythia-bB8ES", "ES")
daily = await get_daily_backtests("Pythia-bB8ES", "ES")
Note: The Python client is the recommended approach for Python users as it handles authentication, connection management, error handling, and SSE parsing automatically.