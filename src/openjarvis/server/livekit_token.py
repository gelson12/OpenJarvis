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

    if shared_secret:
        if not x_voice_secret or not hmac.compare_digest(
            x_voice_secret, shared_secret
        ):
            raise HTTPException(status_code=403, detail="Invalid voice secret")
    else:
        logger.warning(
            "LIVEKIT_TOKEN_SHARED_SECRET not set — /v1/livekit/token is "
            "gated only by HTTP Basic Auth. Set it in production."
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


__all__ = ["router"]
