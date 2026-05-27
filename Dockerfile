# Stage 1: Build frontend SPA
FROM node:22-slim AS frontend

WORKDIR /app
COPY frontend/ ./frontend/
RUN cd frontend && npm ci --ignore-scripts 2>/dev/null || npm install

# Shared-secret for the LiveKit token endpoint, baked into the SPA at
# build time so the browser can send the X-Voice-Secret header. Railway
# passes the service variable of the same name as a build arg. Empty by
# default (gate then relies solely on OpenJarvis HTTP Basic Auth).
ARG VITE_VOICE_SECRET=""
ENV VITE_VOICE_SECRET=${VITE_VOICE_SECRET}

RUN cd frontend && npm run build

# Stage 2: Build Python package
FROM python:3.12-slim-bookworm AS builder

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ src/
COPY rust/ rust/
# LiveKit voice worker is co-located in this image (single-service
# deployment): its code + deps must be present alongside the API server.
COPY livekit/ livekit/
# Accommodation booking module — vendored in-repo (see brain/Accommodation
# Booking — Implementation Plan in the vault). Lazy-imported by the worker,
# so a missing folder degrades gracefully to "Accommodation isn't configured".
COPY accommodation/ accommodation/

# Copy built frontend into the server static directory
COPY --from=frontend /app/src/openjarvis/server/static src/openjarvis/server/static/

RUN pip install --no-cache-dir uv && \
    uv pip install --system ".[server,memory-obsidian,inference-cloud,browser]" && \
    uv pip install --system -r livekit/requirements.txt

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

# Pre-download the Silero VAD model so the voice worker doesn't stall on
# first job fetching it. Non-fatal: it will lazy-download if this skips.
RUN python -c "from livekit.plugins import silero; silero.VAD.load()" 2>/dev/null || \
    echo "Silero VAD prefetch skipped; worker will download it on first job"

# Pre-download all livekit-plugin model files (turn-detector ONNX weights
# in particular — without this the inference subprocess crash-loops with
# "Could not find file 'model_q8.onnx'"). This is the canonical pattern
# LiveKit recommends in the error message itself. Non-fatal: worker code
# falls back to VAD-only endpointing if turn-detector init fails at runtime.
RUN python -m livekit.agents download-files 2>&1 | tail -10 || \
    echo "livekit download-files failed; turn-detector will fall back to VAD endpointing"

# Belt-and-suspenders CRLF defence. Even with .gitattributes enforcing LF
# on every commit, a Windows-side checkout / a `railway up` upload from a
# misconfigured working tree CAN slip CRLF into the container. Bash on
# Python 3.12 slim refuses CRLF and crashes the launcher with
# "$'\r': command not found / set: pipefail: invalid option / exit 127".
# This strip runs once at image-build time so no script needs to know
# about it; it's idempotent on already-LF files.
RUN find /app -maxdepth 4 \( -name '*.sh' -o -name 'entrypoint*' -o -name 'start*' \) \
    -type f -print0 | xargs -0 -r sed -i 's/\r$//' \
    && chmod +x /app/livekit/start.sh /app/entrypoint.sh 2>/dev/null || true

EXPOSE 8080

# Single container runs BOTH the OpenJarvis API server and the LiveKit
# voice worker (see livekit/start.sh). No separate worker service needed.
CMD ["bash", "/app/livekit/start.sh"]
