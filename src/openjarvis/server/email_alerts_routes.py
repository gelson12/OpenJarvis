"""SSE endpoint that streams email-watcher match events to the LiveKit
worker so it can speak the alert mid-session (no waiting for the next
user turn).

Wire:
  email_watcher poller → in-memory queue → /v1/email_alerts/stream (SSE)
  → worker subscribes per-session → session.say("Sir — X just emailed you...")

The queue is process-local. With a single-container deploy that's fine —
the poller, the SSE endpoint, and the API server all live in the same
Python process. (If we ever split services, this becomes a Redis pub/sub.)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncGenerator, Dict, Optional

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)
router = APIRouter()


# In-process queue. Set on first use; cleared on each session drain.
# Bound size so a worker that's been disconnected doesn't blow memory.
_QUEUE: Optional["asyncio.Queue[Dict]"] = None
_QUEUE_MAX = 100


def _get_queue() -> "asyncio.Queue[Dict]":
    global _QUEUE
    if _QUEUE is None:
        _QUEUE = asyncio.Queue(maxsize=_QUEUE_MAX)
    return _QUEUE


def push_alert(match: Dict) -> None:
    """Called by the email_watcher poller's notify_cb. Safe to call from
    a non-async thread because we use put_nowait (drops oldest on overflow)."""
    try:
        q = _get_queue()
        try:
            q.put_nowait(match)
        except asyncio.QueueFull:
            # Drop oldest to make room — alerts decay if worker is offline
            try:
                q.get_nowait()
            except Exception:
                pass
            try:
                q.put_nowait(match)
            except Exception:
                pass
        logger.info(
            "openjarvis.email_alerts.queued sender=%s subject=%r",
            match.get("watch", {}).get("sender"),
            (match.get("message", {}).get("subject") or "")[:80],
        )
    except Exception as exc:
        logger.warning("email_alerts.push failed: %s", exc)


async def _alert_stream() -> AsyncGenerator[bytes, None]:
    """Yields SSE-formatted event lines as alerts arrive. Heartbeats every
    20s so the LiveKit worker's httpx client doesn't time out idle."""
    q = _get_queue()
    # Send an initial connect event so subscribers know we're alive
    yield b": connected\n\n"
    while True:
        try:
            match = await asyncio.wait_for(q.get(), timeout=20.0)
        except asyncio.TimeoutError:
            yield b": heartbeat\n\n"
            continue
        try:
            payload = json.dumps(match, ensure_ascii=False, default=str)
        except Exception as exc:
            logger.warning("email_alerts.serialize failed: %s", exc)
            continue
        yield f"event: email_alert\ndata: {payload}\n\n".encode("utf-8")


@router.get("/v1/email_alerts/stream")
async def email_alerts_stream() -> StreamingResponse:
    """SSE stream of email-watcher matches. Worker subscribes per-session."""
    return StreamingResponse(
        _alert_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx pass-through
            "Connection": "keep-alive",
        },
    )
