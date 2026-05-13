FROM python:3.12-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Install dependencies first (layer caching)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy source
COPY src/ src/
COPY main.py ./

# Install the project itself
RUN uv sync --frozen --no-dev

# Create logs directory
RUN mkdir -p logs

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["uv", "run", "live-trader"]
