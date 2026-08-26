# syntax=docker/dockerfile:1.7

# ---------------------------------------------------------------------------
# Stage 1: build the frontend
# ---------------------------------------------------------------------------
FROM node:22-alpine AS frontend

WORKDIR /build

# Copy the manifests first so the dependency layer is cached across code edits.
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund

COPY frontend/ ./
# vite.config.ts writes to ../backend/app/static, so give it somewhere to land.
RUN mkdir -p /backend/app && npm run build && ls -la /build/../backend/app/static

# ---------------------------------------------------------------------------
# Stage 2: python dependencies
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS deps

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /wheels
COPY backend/requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 3: runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="AmiSearch" \
      org.opencontainers.image.description="Self-hosted price tracking and restock alerts for AmiAmi." \
      org.opencontainers.image.source="https://github.com/bejak1999/ami_search" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    PORT=8080

# curl_cffi needs libcurl's TLS stack at runtime; tini reaps the worker threads
# cleanly on stop so TrueNAS restarts are not slow.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tini curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=deps /wheels /wheels
COPY backend/requirements.txt .
RUN pip install --no-index --find-links=/wheels -r requirements.txt && rm -rf /wheels

COPY backend/app ./app
COPY --from=frontend /backend/app/static ./app/static
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Run unprivileged. TrueNAS bind mounts usually want a specific uid/gid, so
# these are overridable at build time.
ARG PUID=1000
ARG PGID=1000
RUN groupadd -g "${PGID}" amisearch 2>/dev/null || true \
    && useradd -u "${PUID}" -g "${PGID}" -m -s /usr/sbin/nologin amisearch 2>/dev/null || true \
    && mkdir -p /data && chown -R "${PUID}:${PGID}" /data /app

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

USER amisearch
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
