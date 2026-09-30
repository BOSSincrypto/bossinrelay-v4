# syntax=docker/dockerfile:1
# Многостадийная сборка: итоговый образ на python:3.12-slim, цель < 400MB.
ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS builder
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt .
RUN pip install --prefix=/install --no-cache-dir -r requirements.txt

FROM python:${PYTHON_VERSION}-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000
WORKDIR /srv
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -r -u 10001 relay
COPY --from=builder /install /usr/local
COPY app/ ./app/
RUN mkdir -p /srv/data && chown -R relay:relay /srv
USER relay
EXPOSE 8000
# /healthz появится вместе с ядром; пока живём на /login (200 без авторизации).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -sf "http://127.0.0.1:${PORT:-8000}/healthz" || curl -sf "http://127.0.0.1:${PORT:-8000}/login" || exit 1
# Один воркер — обязательно при 512MB RAM (см. README).
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --loop uvloop --http httptools --limit-concurrency 64 --timeout-keep-alive 5"]
