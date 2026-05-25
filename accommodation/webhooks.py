"""
Webhook receiver for provider booking-completion callbacks.

Designed as a FastAPI sub-app that mounts onto an existing FastAPI server
(OpenJarvis's routes.py is FastAPI; mount with
`app.mount("/accommodation", webhook_app)`).

For fj2 which doesn't run a FastAPI server in the worker, this module is
unused in Phase 1 — fj2 polls the provider booking endpoint on demand via
`service.quote()` (LiteAPI exposes a GET /v3.0/bookings/<id> shape that can
verify confirmation status).

FastAPI is an optional import here so the rest of the module stays usable
in environments without it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Any, Awaitable, Callable

_log = logging.getLogger("accommodation.webhooks")


def _verify_liteapi_signature(body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def create_webhook_app(
    on_booking_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
):
    """Build a FastAPI app exposing `/v1/webhook/liteapi`.

    `on_booking_event` is awaited with the parsed JSON payload on every valid
    webhook. The caller wires it to LiveKit data-channel publishing so the
    voice agent that originated the booking gets notified.

    Raises ImportError if FastAPI isn't available — that's intentional; this
    helper is opt-in and we don't want it adding a hard dep to the module.
    """
    from fastapi import FastAPI, Header, HTTPException, Request

    app = FastAPI(title="accommodation-webhooks")

    @app.post("/v1/webhook/liteapi")
    async def liteapi_webhook(
        request: Request,
        x_liteapi_signature: str = Header(default=""),
    ) -> dict[str, str]:
        secret = os.environ.get("LITEAPI_WEBHOOK_SECRET", "").strip()
        body = await request.body()
        if secret and not _verify_liteapi_signature(body, x_liteapi_signature, secret):
            _log.warning("liteapi webhook: signature mismatch")
            raise HTTPException(status_code=401, detail="bad signature")
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid json")
        if on_booking_event is not None:
            try:
                await on_booking_event(payload)
            except Exception:  # noqa: BLE001
                _log.exception("on_booking_event raised")
        return {"status": "ok"}

    return app
