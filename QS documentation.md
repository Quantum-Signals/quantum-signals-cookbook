QuantumSignals Python Client
The official Python client library for the QuantumSignals API. This client provides a simple, intuitive interface for authentication, streaming signals, managing API keys, and accessing backtest results.

Installation
Install the QuantumSignals Python client using pip or uv:

pip install quantumsignals-client

Copy
Quick Start
Get started in 30 seconds. Obtain your API key from your administrator or the QuantumSignals dashboard, then:

from quantumsignals.client import Client

# Create client with your API key
client = Client(api_key="qsk_your_api_key_here")

# Stream real-time trading signals
for signal in client.stream_signals():
    print(f"{signal.time} | {signal.symbol}: {signal.signal}")

Copy
Authentication
For production use, load the API key from environment variables:

import os
from quantumsignals.client import Client

api_key = os.getenv("QUANTUMSIGNALS_API_KEY")
if not api_key:
    raise ValueError("QUANTUMSIGNALS_API_KEY environment variable required")

client = Client(api_key=api_key)

Copy
Core Features
Model Catalog
Get information about available trading models:

# Get catalog as dict[model_id -> ModelDescription]
models = client.get_model_catalog()

for model_id, model in models.items():
    print(f"Model: {model_id}")
    print(f"  Trained on: {', '.join(model.trained_on_symbols)}")
    print(f"  Date range: {model.trained_date_range[0]} to {model.trained_date_range[1]}")
    print(f"  Hash: {model.hash}")

Copy
Signal Streaming
Stream real-time trading signals via Server-Sent Events:

# Stream indefinitely
for signal in client.stream_signals():
    print(f"Time: {signal.time}")
    print(f"Symbol: {signal.symbol}")
    print(f"Signal: {signal.signal}")  # 0 (down), 1 (stable), 2 (up)
    print(f"Model: {signal.model}")

    # Process signal
    if signal.signal == 2:
        execute_long(signal.symbol)
    elif signal.signal == 0:
        execute_short(signal.symbol)

Copy
Signal Data Model:

time: int - Unix timestamp
symbol: str - Trading symbol (e.g., "AAPL")
signal: int - Signal value: 0 (down/short), 1 (stable/hold), 2 (up/long)
model: str - Model identifier
Backtest Operations
Access historical backtest data to evaluate model performance. Backtest data includes predictions, actual outcomes, and performance metrics for each model-symbol combination.

How Backtests Work
Backtests are identified by a model_family (e.g., "Pythia") and a symbol (e.g., "ES"). Each backtest contains:

Metadata - Information about the backtest configuration, date ranges, and status
Historical Predictions - Time-series data of model predictions vs actual outcomes with PnL calculations
Daily Summaries - Aggregated performance metrics for each trading day
Get Backtest Metadata
Retrieve metadata about a backtest, including its configuration and date ranges:

from quantumsignals.client import BacktestMetadata

# Get metadata for a specific model-symbol combination
metadata: BacktestMetadata = client.get_backtest_metadata(
    model_family="Pythia",
    symbol="ES"
)

print(f"Model Family: {metadata.model_family}")
print(f"Symbol: {metadata.symbol}")
print(f"Backtest range: {metadata.backtest_from_date} to {metadata.backtest_to_date}")
print(f"Training range: {metadata.training_from_date} to {metadata.training_to_date}")
print(f"Status: {metadata.status}")

Copy
Get Historical Predictions
Retrieve historical prediction data with pagination support and multiple export formats:

# Get predictions with pagination (JSON format)
predictions = client.get_historical_predictions(
    model_family="Pythia",
    symbol="ES",
    format="json",
    page=1,
    page_size=1000
)

# Process paginated results
print(f"Page: {predictions['page']} of {predictions['total_pages']}")
print(f"Total records: {predictions['total']}")

for row in predictions['data']:
    print(f"{row['date_time']}: pred={row['prediction']}, actual={row['label']}, pnl={row['cumulative_pnl']}")

# Optional: Filter by date range
filtered_predictions = client.get_historical_predictions(
    model_family="Pythia",
    symbol="ES",
    format="json",
    from_date="2024-01-01",
    to_date="2024-12-31",
    page=1,
    page_size=1000
)

Copy
Bulk Export Formats

For large datasets, export to CSV or Parquet for efficient offline analysis:

# Export to CSV (returns bytes)
csv_data = client.get_historical_predictions(
    model_family="Pythia",
    symbol="ES",
    format="csv"
)
with open("backtest_predictions.csv", "wb") as f:
    f.write(csv_data)

# Export to Parquet (returns bytes, efficient for large datasets)
parquet_data = client.get_historical_predictions(
    model_family="Pythia",
    symbol="ES",
    format="parquet"
)
with open("backtest_predictions.parquet", "wb") as f:
    f.write(parquet_data)

# Load Parquet with pandas for analysis
import pandas as pd
df = pd.read_parquet("backtest_predictions.parquet")
print(df.head())

Copy
Prediction Data Fields:

date_time: str - ISO timestamp (e.g., "2025-10-01T09:30:00")
prediction: int - Model prediction: 0 (down), 1 (stable), 2 (up)
label: int - Actual outcome: 0 (down), 1 (stable), 2 (up)
cumulative_pnl: float - Cumulative profit/loss at this point
Get Daily Backtest Summaries
Get aggregated daily performance metrics for quick analysis:

# Get daily summaries with pagination
daily_backtests = client.get_daily_backtests(
    model_family="Pythia",
    symbol="ES",
    format="json",
    page=1,
    page_size=100
)

for day in daily_backtests['data']:
    print(f"Date: {day['date']}")
    print(f"  Accuracy: {day['accuracy']:.2%}")
    print(f"  Daily PnL: ${day['daily_pnl']:,.2f}")
    print(f"  Cumulative PnL: ${day['cumulative_pnl']:,.2f}")
    print(f"  Total predictions: {day['total_predictions']}")

# Filter by date range
recent_backtests = client.get_daily_backtests(
    model_family="Pythia",
    symbol="ES",
    format="json",
    from_date="2024-12-01",
    to_date="2024-12-31",
    page=1,
    page_size=100
)

# Export daily summaries to CSV or Parquet
csv_summaries = client.get_daily_backtests(
    model_family="Pythia",
    symbol="ES",
    format="csv"
)
with open("daily_summaries.csv", "wb") as f:
    f.write(csv_summaries)

Copy
Daily Summary Data Fields:

date: str - Trading date (e.g., "2024-12-15")
accuracy: float - Prediction accuracy for the day (0.0 to 1.0)
precision: float - Precision metric for the day
recall: float - Recall metric for the day
f1_score: float - F1 score for the day
daily_pnl: float - Profit/loss for the day
cumulative_pnl: float - Cumulative profit/loss up to this day
total_predictions: int - Number of predictions made that day
Best Practices
Use Context Managers
Properly close HTTP connections using context managers:

from signalq.client import Client

with Client(api_key="qsk_your_api_key_here") as client:
    models = client.get_model_catalog()
    # Client automatically closes on exit

Copy
Handle Exceptions
The client provides specific exceptions for error handling:

from signalq.client import Client, APIKeyError, APIError

client = Client(api_key="qsk_your_api_key_here")

try:
    models = client.get_model_catalog()
except APIKeyError as e:
    print(f"API key issue: {e}")
except APIError as e:
    print(f"API request failed: {e}")
    print(f"Status code: {e.status_code}")
    print(f"Response: {e.response_body}")

Copy
Production Signal Streaming
For production signal streaming with error recovery:

from signalq.client import Client, APIError
import time

def stream_signals_with_retry(api_key: str, max_retries=3):
    """Stream signals with automatic retry on failure."""
    client = Client(api_key=api_key)
    retry_count = 0

    while retry_count < max_retries:
        try:
            for signal in client.stream_signals():
                # Process signal
                process_signal(signal)
                retry_count = 0  # Reset on successful signal

        except APIError as e:
            retry_count += 1
            wait_time = 2 ** retry_count  # Exponential backoff
            print(f"Stream error: {e}. Retrying in {wait_time}s...")
            time.sleep(wait_time)
        except KeyboardInterrupt:
            print("Stopping stream...")
            break
        finally:
            client.close()

Copy
Complete Example
Here's a complete example:

from signalq.client import Client, APIError
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def main():
    """Main trading bot using QuantumSignals client."""

    # Initialize client with your API key (obtain from your administrator or the QuantumSignals dashboard)
    with Client(api_key="qsk_your_api_key_here") as client:
        # Get available models
        logger.info("Fetching model catalog...")
        models = client.get_model_catalog()
        logger.info(f"Available models: {list(models.keys())}")

        # Stream and process signals
        logger.info("Starting signal stream...")
        try:
            for signal in client.stream_signals():
                logger.info(
                    f"Signal received: {signal.symbol} -> {signal.signal} "
                    f"(model: {signal.model}, time: {signal.time})"
                )

                # Execute trading logic
                if signal.signal == 2:
                    logger.info(f"LONG signal for{signal.symbol}")
                    # execute_long(signal.symbol)
                elif signal.signal == 0:
                    logger.info(f"SHORT signal for{signal.symbol}")
                    # execute_short(signal.symbol)

        except APIError as e:
            logger.error(f"API error: {e}")
            raise
        except KeyboardInterrupt:
            logger.info("Shutting down...")

if __name__ == "__main__":
    main()