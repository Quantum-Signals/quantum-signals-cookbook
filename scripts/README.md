# Scripts

Manual tools for smoke-testing and learning. These are **not** part of the
automated test suite (`tests/`). They require a real QS API key and/or a
running IB Gateway.

| Script | What it does | Requires |
|---|---|---|
| `smoke_qs_connection.py` | Validates your API key, prints the model catalog, and streams live signals | `QUANTUMSIGNALS_API_KEY` in `.env` |
| `smoke_multiday_supervisor.py` | Drives the multi-day supervisor offline with a fake clock and stubbed QS stream (no API key needed) | Nothing — fully offline |
| `tutorial_ibkr.py` | Step-by-step walkthrough of the IBKR + QS trading pipeline | IB Gateway running, `QUANTUMSIGNALS_API_KEY` in `.env` |

## Running

```bash
uv run python scripts/smoke_qs_connection.py
uv run python scripts/smoke_multiday_supervisor.py --days 3
uv run python scripts/tutorial_ibkr.py