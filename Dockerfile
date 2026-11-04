# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# AgentForge — single deployable image
#
# Stage 1 builds the React/Vite dashboard. Stage 2 installs the Python
# backend, copies the built SPA into frontend/dist (served by FastAPI) and
# runs everything as an unprivileged user.
#
# Build:  docker build -t agentforge-backend:local .
# Run:    docker run --rm -p 8000:8000 --env-file .env agentforge-backend:local
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Stage 1 — frontend build (Node 24 / Alpine)
# ---------------------------------------------------------------------------
FROM node:24-alpine AS frontend-builder

WORKDIR /build/frontend

# Copy manifests first so the dependency layer is cached across source edits.
# `package*.json` matches package.json even before a package-lock.json exists.
COPY frontend/package*.json ./

# Reproducible install when a lockfile is present, fallback to npm install.
RUN if [ -f package-lock.json ]; then \
        npm ci --no-audit --no-fund; \
    else \
        npm install --no-audit --no-fund; \
    fi

COPY frontend/ ./

# Optional build-time API base URL. Leave empty to use same-origin `/api`
# (the normal deployment, where FastAPI serves the SPA).
ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}

# Vite emits the production bundle to frontend/dist.
RUN npm run build && test -d dist

# ---------------------------------------------------------------------------
# Stage 2 — Python 3.12 runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="AgentForge" \
      org.opencontainers.image.description="Autonomous multi-agent business automation platform" \
      org.opencontainers.image.source="https://github.com/example/agentforge" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONHASHSEED=0

WORKDIR /app

# Runtime system dependencies: none required. `psycopg[binary]` ships a
# self-contained libpq wheel; the healthcheck uses the Python stdlib instead
# of installing curl.

# Dependency layer (cached when requirements.txt is unchanged).
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

# Application code, demo dataset and knowledge corpus.
COPY app/ ./app/
COPY data/ ./data/

# Built SPA from stage 1 — FastAPI mounts this directory at /.
COPY --from=frontend-builder /build/frontend/dist ./frontend/dist

# Writable runtime locations + unprivileged user (no root at runtime).
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/artifacts /app/frontend \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

# Contract: GET /api/health -> {status, version, llm_provider, llm_model, database}
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"]

# Production server. The app must serve the SPA from frontend/dist and the
# REST/SSE API under /api (see docs/API_CONTRACT.md).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
