# syntax=docker/dockerfile:1
# Build context is the REPOSITORY ROOT:  docker build -f apps/api/Dockerfile .
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Layer 1: third-party dependencies only (cached until pyproject/uv.lock change).
COPY pyproject.toml uv.lock README.md ./
COPY apps/api/pyproject.toml apps/api/pyproject.toml
COPY ml/pyproject.toml ml/pyproject.toml
COPY data-gen/pyproject.toml data-gen/pyproject.toml
RUN uv sync --frozen --no-dev --no-install-workspace --package pbip-api

# Layer 2: our code, installed non-editable so the runtime stage needs no sources.
COPY apps/api/src apps/api/src
COPY ml/src ml/src
COPY data-gen/src data-gen/src
RUN uv sync --frozen --no-dev --no-editable --package pbip-api

FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH"
# libgomp1: the OpenMP runtime LightGBM links against (fraud scorer, ADR-0019). Without it the
# model cannot be imported and the API serves the rules only.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --system app && useradd --system --gid app --home-dir /app --no-create-home app
WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app apps/api/alembic.ini apps/api/alembic.ini
COPY --chown=app:app apps/api/alembic apps/api/alembic
USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/api/health/live', timeout=2)"
# One uvicorn worker per container: scale horizontally with replicas (Prometheus metrics stay exact).
# --no-access-log: the app writes one structured access line (no query string); uvicorn's would duplicate it.
CMD ["uvicorn", "pbip_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]
