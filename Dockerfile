# Stage 1: Build frontend SPA
FROM node:22-slim AS frontend

WORKDIR /app
COPY frontend/ ./frontend/
RUN cd frontend && npm ci --ignore-scripts 2>/dev/null || npm install
RUN cd frontend && npm run build

# Stage 2: Build Python package
FROM python:3.12-slim-bookworm AS builder

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ src/
COPY rust/ rust/

# Copy built frontend into the server static directory
COPY --from=frontend /app/src/openjarvis/server/static src/openjarvis/server/static/

RUN pip install --no-cache-dir uv && \
    uv pip install --system ".[server,memory-obsidian,inference-cloud,browser]"

# Stage 3: Runtime
FROM python:3.12-slim-bookworm

COPY --from=builder /usr/local /usr/local
COPY --from=builder /app /app
WORKDIR /app

# Install Chromium + the system libraries Playwright needs at runtime.
# Adds ~250 MB to the image but means browser_* tools work out-of-box —
# no separate playwright-server container required. Skip-on-failure so a
# transient apt issue doesn't block the whole deploy; the browser tools
# will surface a clear "Playwright not installed" error if Chromium is
# missing, which is recoverable on next rebuild.
RUN python -m playwright install chromium --with-deps 2>&1 | tail -20 || \
    echo "Playwright Chromium install failed; browser_* tools will be unavailable until next rebuild"

# Placeholder for future auth re-enablement; override at runtime if needed.
# All provider API keys (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.) are read
# directly from Railway's runtime environment — no defaults are set here so
# they are never stomped by empty Dockerfile values.
ENV OPENJARVIS_API_KEY=default-key-change-me

# Build-time provenance — exposed via GET /v1/version so we can verify
# what commit is actually live without digging through Railway logs.
# Railway sets RAILWAY_GIT_COMMIT_SHA automatically; we rename to the
# OPENJARVIS_* namespace so the endpoint can read either source.
ARG RAILWAY_GIT_COMMIT_SHA=unknown
ARG BUILD_TIME=unknown
ENV OPENJARVIS_GIT_COMMIT=${RAILWAY_GIT_COMMIT_SHA}
ENV OPENJARVIS_BUILD_TIME=${BUILD_TIME}

EXPOSE 8000

CMD exec jarvis serve --host 0.0.0.0 --port ${PORT:-8000}
