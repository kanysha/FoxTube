# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 AS base

COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH" \
    RUFF_CACHE_DIR=/tmp/ruff-cache \
    MYPY_CACHE_DIR=/tmp/mypy-cache
RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 foxtube \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin foxtube \
    && mkdir -p /var/lib/foxtube/media \
    && chown 1000:1000 /var/lib/foxtube/media
WORKDIR /app

FROM base AS runtime
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
USER 1000:1000
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--no-access-log", "--no-server-header", "--no-proxy-headers"]

FROM runtime AS development
USER root
RUN apt-get update \
    && apt-get install --no-install-recommends -y shellcheck \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --group dev --no-install-project
COPY tests ./tests
COPY scripts ./scripts
COPY deploy ./deploy
USER 1000:1000
