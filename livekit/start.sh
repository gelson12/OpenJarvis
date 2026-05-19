#!/usr/bin/env bash
# Single-container launcher: runs the OpenJarvis API server AND the LiveKit
# voice worker side-by-side in one Railway service. If either process
# exits, the script exits non-zero so Railway's restart policy restarts
# the whole container (acceptable for a single-user deployment).
set -uo pipefail

# The worker calls OpenJarvis over loopback by default — no public
# round-trip, no second service. An explicit OPENJARVIS_URL still wins.
export OPENJARVIS_URL="${OPENJARVIS_URL:-http://127.0.0.1:${PORT:-8080}}"

echo "[start] launching LiveKit voice worker (agent: openjarvis-agent)"
python /app/livekit/worker.py start &
WORKER_PID=$!

echo "[start] launching OpenJarvis API server on :${PORT:-8080}"
jarvis serve --host 0.0.0.0 --port "${PORT:-8080}" &
SERVE_PID=$!

# Exit as soon as EITHER process exits; kill the survivor; non-zero exit
# triggers Railway's container restart policy.
wait -n "$WORKER_PID" "$SERVE_PID"
EXIT=$?
echo "[start] a process exited (code ${EXIT}); shutting down the container"
kill "$WORKER_PID" "$SERVE_PID" 2>/dev/null || true
exit "$EXIT"
