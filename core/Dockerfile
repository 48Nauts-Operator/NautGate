# Multi-stage build: install deps with uv, then copy minimal runtime.
#
# Build context is the REPO ROOT (not core/), because the image must ship
# config/routing.yaml + config/pricing.yaml, which live outside core/. Without
# them a containerised run silently loses routing (model:auto can't resolve) and
# pricing (every cost_usd NULL). See docker-compose: `context: ..`.
FROM python:3.12-slim AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1

COPY core/pyproject.toml core/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY core/app/ ./app/
COPY core/bowden_pii/ ./bowden_pii/

FROM python:3.12-slim
WORKDIR /app

# pg_dump/psql so backups run inside the container via a direct DSN connection
# to the db service — no docker-exec, which can't work from inside a container
# (NAUTGATE-7). Debian trixie's own postgresql-client is v17, which dumps a PG16
# server fine (pg_dump must be >= the server major, and 17 >= 16).
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/app /app/app
COPY --from=builder /app/bowden_pii /app/bowden_pii

# Default routing + pricing tables, shipped so the image works standalone.
# main.py resolves these via parents[2], which is `/` inside the image (the
# core/ dir is flattened away), so pin the paths explicitly — main.py:70,79
# prefer the env vars over that fragile default. A user can still override the
# whole dir with a `./config:/app/config:ro` bind mount.
COPY config/ /app/config/
# pyproject is the single source of the version shown in the dashboard (health.py
# reads it when NAUTGATE_VERSION isn't set).
COPY core/pyproject.toml /app/pyproject.toml

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NAUTGATE_ROUTING_CONFIG_PATH=/app/config/routing.yaml \
    NAUTGATE_PRICING_CONFIG_PATH=/app/config/pricing.yaml

EXPOSE 8090

# /health is the one endpoint that answers without a database, so it's a clean
# liveness signal even while migrations run on first boot.
HEALTHCHECK --interval=15s --timeout=3s --retries=5 --start-period=20s \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:8090/health', timeout=2).status == 200 else 1)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
