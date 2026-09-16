# Scripts

Manual tools for smoke-testing and learning. These are **not** part of the
automated test suite (`tests/`). `smoke_qs_connection.py` needs a real QS API
key; `smoke_multiday_supervisor.py` is fully offline.

| Script | What it does | Requires |
|---|---|---|
| `smoke_qs_connection.py` | Validates your API key, prints the signal catalog, and streams live signals | `QUANTUMSIGNALS_API_KEY` in `.env` |
| `smoke_multiday_supervisor.py` | Drives the multi-day supervisor offline with a fake clock and stubbed QS stream (no API key needed) | Nothing — fully offline |

## Running

```bash
uv run python scripts/smoke_qs_connection.py
uv run python scripts/smoke_multiday_supervisor.py --days 3
```
