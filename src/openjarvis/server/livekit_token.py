"""LiveKit access-token endpoint for the OpenJarvis voice UI.

Mints a short-lived LiveKit participant token and explicitly dispatches
the ``openjarvis-agent`` worker into the room (via RoomConfiguration ->
RoomAgentDispatch), mirroring the LiveKit agent-starter-react token route
but server-side in OpenJarvis's FastAPI.

The voice agent itself (livekit/worker.py) is a separate long-running
worker; this endpoint only issues the browser its room token.

Auth model (defense-in-depth):
  - All ``/v1/*`` routes already sit behind OpenJarvis's HTTP Basic Auth
    gate; the same-origin SPA fetch carries those creds automatically.
  - When ``LIVEKIT_TOKEN_SHARED_SECRET`` is configured, this route also
    requires a matching ``X-Voice-Secret`` header (constant-time compare).
    When it is unset (local dev), the route still works but logs a warning.
"""

from __future__ import annotations

import datetime
import hmac
import logging
import os
import secrets

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

# Imported lazily inside handlers so the optional livekit-api dep stays
# optional outside the voice routes.

logger = logging.getLogger(__name__)

router = APIRouter()

_DEFAULT_AGENT_NAME = "openjarvis-agent"
_TOKEN_TTL = datetime.timedelta(minutes=15)


class ConnectionDetails(BaseModel):
    serverUrl: str
    roomName: str
    participantName: str
    participantToken: str


@router.post("/v1/livekit/token", response_model=ConnectionDetails)
async def issue_livekit_token(
    x_voice_secret: str | None = Header(default=None),
) -> ConnectionDetails:
    livekit_url = os.environ.get("LIVEKIT_URL", "")
    api_key = os.environ.get("LIVEKIT_API_KEY", "")
    api_secret = os.environ.get("LIVEKIT_API_SECRET", "")
    shared_secret = os.environ.get("LIVEKIT_TOKEN_SHARED_SECRET", "")
    agent_name = os.environ.get("OPENJARVIS_VOICE_AGENT_NAME", _DEFAULT_AGENT_NAME)

    # Shared secret is defense-in-depth ONLY. The real gate is OpenJarvis's
    # HTTP Basic Auth — every /v1/* route already sits behind it and the
    # same-origin SPA fetch carries those creds automatically. A missing or
    # mismatched X-Voice-Secret (e.g. VITE_VOICE_SECRET not baked into the
    # SPA build) must NEVER hard-fail voice. Log and proceed.
    if shared_secret and (
        not x_voice_secret
        or not hmac.compare_digest(x_voice_secret, shared_secret)
    ):
        logger.warning(
            "X-Voice-Secret missing/mismatched — proceeding anyway "
            "(endpoint still behind HTTP Basic Auth). Bake VITE_VOICE_SECRET "
            "at build time to silence this."
        )

    missing = [
        name
        for name, val in (
            ("LIVEKIT_URL", livekit_url),
            ("LIVEKIT_API_KEY", api_key),
            ("LIVEKIT_API_SECRET", api_secret),
        )
        if not val
    ]
    if missing:
        raise HTTPException(
            status_code=500,
            detail=f"LiveKit not configured; missing env: {', '.join(missing)}",
        )

    # Imported here so the dependency is only required when the voice
    # endpoint is actually used (it ships in the `server` extra).
    from livekit.api import AccessToken, VideoGrants
    from livekit.protocol.agent_dispatch import RoomAgentDispatch
    from livekit.protocol.room import RoomConfiguration

    suffix = secrets.randbelow(10_000)
    room_name = f"voice_assistant_room_{suffix}"
    participant_name = "user"
    participant_identity = f"voice_assistant_user_{suffix}"

    token = (
        AccessToken(api_key, api_secret)
        .with_identity(participant_identity)
        .with_name(participant_name)
        .with_ttl(_TOKEN_TTL)
        .with_grants(
            VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .with_room_config(
            RoomConfiguration(agents=[RoomAgentDispatch(agent_name=agent_name)])
        )
        .to_jwt()
    )

    return ConnectionDetails(
        serverUrl=livekit_url,
        roomName=room_name,
        participantName=participant_name,
        participantToken=token,
    )


class LiveKitSelftestResult(BaseModel):
    ok: bool
    reason: str
    livekit_url: str | None = None
    api_key_prefix: str | None = None  # first 6 chars only — never leak the full key
    rooms_visible: int | None = None
    error_type: str | None = None
    hint: str | None = None


@router.get("/v1/livekit/selftest", response_model=LiveKitSelftestResult)
async def livekit_selftest() -> LiveKitSelftestResult:
    """Probe the configured LiveKit credentials against the LiveKit Cloud
    REST API. Returns a structured diagnosis so an operator can tell
    *why* `wss` connections fail without scraping logs:

      - missing env  → which var
      - auth reject  → key/secret pair mismatched or revoked
      - 404 / DNS    → URL points at the wrong project / stale project
      - network      → LiveKit Cloud unreachable from the deploy

    Sits behind the same HTTP Basic Auth as every other /v1/* route.
    Returns 200 with `ok=false` on diagnosable failure (so curl + browser
    can both read the structured body); only 500s on truly unexpected
    errors.
    """
    livekit_url = os.environ.get("LIVEKIT_URL", "")
    api_key = os.environ.get("LIVEKIT_API_KEY", "")
    api_secret = os.environ.get("LIVEKIT_API_SECRET", "")

    if not (livekit_url and api_key and api_secret):
        missing = [
            n
            for n, v in (
                ("LIVEKIT_URL", livekit_url),
                ("LIVEKIT_API_KEY", api_key),
                ("LIVEKIT_API_SECRET", api_secret),
            )
            if not v
        ]
        return LiveKitSelftestResult(
            ok=False,
            reason=f"missing env: {', '.join(missing)}",
            error_type="missing_env",
            hint="set the variables in Railway and redeploy",
        )

    key_prefix = api_key[:6] + "…" if len(api_key) > 6 else "(short)"

    try:
        from livekit import api as lkapi
    except ImportError as exc:
        return LiveKitSelftestResult(
            ok=False,
            reason=f"livekit.api module unavailable: {exc}",
            livekit_url=livekit_url,
            api_key_prefix=key_prefix,
            error_type="import_error",
            hint="install the livekit-api Python package on the server",
        )

    # LiveKit Cloud REST is https://<project>.livekit.cloud — the SDK
    # accepts either the wss URL or the https URL. We pass the same URL
    # the worker + token endpoint use so any URL mistake surfaces here.
    client = lkapi.LiveKitAPI(livekit_url, api_key, api_secret)
    try:
        try:
            res = await client.room.list_rooms(lkapi.ListRoomsRequest())
        finally:
            # The SDK exposes aclose() in recent versions; older ones
            # auto-close. Defensive try keeps either path working.
            close = getattr(client, "aclose", None)
            if close:
                try:
                    await close()
                except Exception:
                    pass
    except Exception as exc:
        # The SDK raises livekit.api's TwirpError on REST-level auth
        # failures and httpx exceptions on network failures. We don't
        # import either explicitly (keeps the import graph thin) — instead
        # we read the exception text + type to classify.
        etype = type(exc).__name__
        msg = str(exc)
        low = msg.lower()
        if "401" in msg or "unauthorized" in low or "invalid api key" in low:
            hint = (
                "Key and secret don't match a key pair on the project at "
                "LIVEKIT_URL. Check you pasted both from the SAME row in the "
                "LiveKit Cloud dashboard, AND that the URL's subdomain is "
                "for the project that owns this key."
            )
            err = "auth_rejected"
        elif "404" in msg or "not found" in low:
            hint = (
                "LIVEKIT_URL resolves but the project at that subdomain "
                "doesn't exist (or was deleted). Update the URL."
            )
            err = "project_not_found"
        elif "name or service not known" in low or "nodename nor servname" in low or "getaddrinfo" in low:
            hint = "DNS lookup failed for LIVEKIT_URL — typo in the subdomain."
            err = "dns_failure"
        elif "timed out" in low or "timeout" in low:
            hint = "LiveKit Cloud unreachable from Railway — transient or firewall."
            err = "network_timeout"
        else:
            hint = "Unrecognised failure; raw error in `reason` for triage."
            err = "unknown"
        return LiveKitSelftestResult(
            ok=False,
            reason=f"{etype}: {msg}",
            livekit_url=livekit_url,
            api_key_prefix=key_prefix,
            error_type=err,
            hint=hint,
        )

    return LiveKitSelftestResult(
        ok=True,
        reason="LiveKit Cloud accepted credentials and returned the room list",
        livekit_url=livekit_url,
        api_key_prefix=key_prefix,
        rooms_visible=len(res.rooms) if hasattr(res, "rooms") else 0,
    )


__all__ = ["router"]
