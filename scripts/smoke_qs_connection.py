"""Quick test script to verify QuantumSignals API connectivity and signal streaming."""

import os
from dotenv import load_dotenv
from quantumsignals.client import Client

load_dotenv()

API_KEY = os.environ["QUANTUMSIGNALS_API_KEY"]


def main() -> None:
    with Client(api_key=API_KEY) as client:
        print("=" * 60)
        print("  QuantumSignals API Test")
        print("=" * 60)

        print("\n--- Model Catalog ---")
        models = client.get_model_catalog()
        for model_id, model in models.items():
            symbols = ", ".join(model.trained_on_symbols)
            date_range = f"{model.trained_date_range[0]} to {model.trained_date_range[1]}"
            print(f"  {model_id}: {symbols}  (trained {date_range})")

        print("\n--- Streaming Signals (Ctrl-C to stop) ---")
        try:
            for signal in client.stream_signals():
                ts = signal.signal_time_utc

                if signal.is_null_signal():
                    print(f"  {ts}  {signal.symbol:5s}  model={signal.model}  --  {signal.code_message}")
                    continue

                label = {0: "DOWN", 1: "STABLE", 2: "UP"}.get(signal.signal, f"?({signal.signal})")
                print(f"  {ts}  {signal.symbol:5s}  model={signal.model}  {label}")

                if signal.signal == 2:
                    print(f"    -> LONG signal for {signal.symbol}")
                elif signal.signal == 0:
                    print(f"    -> SHORT signal for {signal.symbol}")

        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
