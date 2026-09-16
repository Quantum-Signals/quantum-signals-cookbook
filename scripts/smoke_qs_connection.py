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

        print("\n--- Signal Catalog ---")
        catalog = client.get_signal_catalog()
        subscriptions = client.get_subscriptions()
        subscribed_ids = {sub.signal_id for sub in subscriptions}
        print(f"  {len(catalog)} visible, {len(subscriptions)} subscribed")
        for signal_id, meta in sorted(catalog.items()):
            sym = meta.model_output_symbol or "?"
            sub = " *" if signal_id in subscribed_ids else ""
            retired = " [retired]" if meta.retired else ""
            print(f"  {signal_id}{sub}: {sym}{retired}")

        print("\n--- Streaming Signals (Ctrl-C to stop) ---")
        try:
            for signal in client.stream_signals():
                ts = signal.signal_time_utc
                sid = signal.signal_id or "?"

                if signal.is_null_signal():
                    print(
                        f"  {ts}  {signal.symbol:5s}  id={sid}  "
                        f"model={signal.model}  --  {signal.code_message}"
                    )
                    continue

                label = {0: "DOWN", 1: "STABLE", 2: "UP"}.get(signal.signal, f"?({signal.signal})")
                print(f"  {ts}  {signal.symbol:5s}  id={sid}  model={signal.model}  {label}")

                if signal.signal == 2:
                    print(f"    -> LONG signal for {signal.symbol}")
                elif signal.signal == 0:
                    print(f"    -> SHORT signal for {signal.symbol}")

        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
