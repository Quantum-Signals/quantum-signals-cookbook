FROM python:3.12-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Copy the workspace (root + broker packages under src/brokers/*) and install.
# The workspace members must be present before `uv sync` so it can resolve and
# build them, so we copy source before syncing (single layer).
COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
COPY main.py ./

RUN uv sync --frozen --no-dev

# Create logs directory
RUN mkdir -p logs

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["uv", "run", "live-trader"]
