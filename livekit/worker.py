"""
OpenJarvis LiveKit Voice Worker

Bridges LiveKit voice sessions to OpenJarvis's /v1/chat/completions endpoint.
All intelligence (tools, memory, multi-LLM routing) lives in OpenJarvis.
This worker handles: VAD, STT (Deepgram), voice loop, TTS (Deepgram).

AUTH NOTE:
  OpenJarvis gates /v1/* behind HTTP Basic Auth whenever
  OPENJARVIS_BASIC_AUTH_USER + OPENJARVIS_BASIC_AUTH_PASSWORD are set on
  the OpenJarvis service (this is the production configuration). The
  LiveKit openai plugin would otherwise send 'Authorization: Bearer ...',
  which the Basic Auth gate rejects with 401. So we precompute and force
  a Basic auth header from env vars (no secrets hardcoded).

  If OpenJarvis is instead run with OPENJARVIS_AUTH_ENABLED=true (Bearer
  mode) and no Basic Auth, leave the BASIC_AUTH_* vars unset and set
  OPENJARVIS_API_KEY instead — the plugin's default Bearer auth is used.

Environment variables:
  LIVEKIT_URL                   - wss://your-server.livekit.cloud
  LIVEKIT_API_KEY               - LiveKit API key
  LIVEKIT_API_SECRET            - LiveKit API secret
  DEEPGRAM_API_KEY              - Deepgram API key (STT + TTS)
  OPENJARVIS_URL                - OpenJarvis base URL
                                  private: http://openjarvis.railway.internal:8000
                                  public : https://openjarvis-production-92cf.up.railway.app
  OPENJARVIS_BASIC_AUTH_USER    - Basic Auth user (match OpenJarvis service)
  OPENJARVIS_BASIC_AUTH_PASSWORD- Basic Auth password (match OpenJarvis service)
  OPENJARVIS_API_KEY            - Bearer key; used only if Basic Auth vars unset
  OPENJARVIS_MODEL              - model name OpenJarvis routes on. MUST be a
                                  name its MultiEngine accepts: a discovered
                                  model or a cloud-prefixed id (gpt-*, o1-*,
                                  o3-*, o4-*, claude-*, gemini-*, openrouter/*).
                                  Default: openrouter/auto (works with just
                                  OPENROUTER_API_KEY on the OpenJarvis service).
                                  Override e.g. openrouter/anthropic/claude-sonnet-4
"""

import os
import re
import json
import base64
import asyncio
import logging

from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import AgentSession, Agent, RoomInputOptions, StopResponse
from livekit.agents.utils.images import encode, EncodeOptions, ResizeOptions
from livekit.plugins import openai, deepgram, silero, noise_cancellation
from openai import AsyncOpenAI

load_dotenv()
logger = logging.getLogger(__name__)

AGENT_INSTRUCTION = """
You are Jarvis, a composed and dry-witted AI assistant with a warm British sensibility.

VOICE RULES (critical — this response is spoken aloud):
- Keep every reply to 1-2 sentences maximum.
- No markdown, no bullet points, no headers, no emojis.
- Use natural spoken phrasing, not written prose.
- When executing a task, confirm briefly then state what you did.

TONE:
- Calm, precise, slightly sardonic but never rude.
- Address the user as "sir" or by name when known.
- Treat requests with the seriousness they deserve, no more.

CAPABILITY:
- You have access to the full OpenJarvis tool suite (web search, email, calendar,
  GitHub, n8n workflows, file system, browser automation).
- Use tools when the user's intent requires external data or action.
- Never invent facts. If uncertain, say so in one sentence.
"""


def _openjarvis_auth_headers() -> dict:
    """Force the Authorization header OpenJarvis actually accepts.

    Production OpenJarvis runs the Basic Auth gate. The openai plugin's
    default 'Bearer <api_key>' would be rejected with 401, so when the
    Basic Auth env vars are present we override Authorization with a
    precomputed Basic token. Returns {} when Basic Auth is not configured
    (the plugin then falls back to its default Bearer auth).
    """
    user = os.environ.get("OPENJARVIS_BASIC_AUTH_USER", "")
    password = os.environ.get("OPENJARVIS_BASIC_AUTH_PASSWORD", "")
    if user and password:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        logger.info("OpenJarvis auth: HTTP Basic (user=%s)", user)
        return {"Authorization": f"Basic {token}"}
    logger.info("OpenJarvis auth: Bearer (OPENJARVIS_API_KEY)")
    return {}


def prewarm(proc: agents.JobProcess):
    """Pre-load VAD model once per worker process to avoid cold-start delay."""
    proc.userdata["vad"] = silero.VAD.load()


# Structured command channel to the browser UI. The frontend listens via
# @livekit/components-react useDataChannel(UI_COMMAND_TOPIC). We send a
# structured JSON command, NOT raw transcription text, so the UI never has
# to parse speech.
UI_COMMAND_TOPIC = "ui-command"

_CAM_WORD = re.compile(r"\b(camera|cam|webcam|video)\b", re.I)
_CAM_OFF = re.compile(
    r"\b(off|disable|stop|close|kill|hide|turn it off|shut)\b", re.I
)
_CAM_ON = re.compile(
    r"\b(on|enable|start|open|show|turn it on|activate)\b", re.I
)


def _camera_intent(text: str):
    """Return True (turn on), False (turn off), or None (not a camera cmd).

    Server-side intent parse on the user's turn — the agreed approach.
    'off' wins if both polarities somehow appear ("turn the camera off,
    not on").
    """
    if not text or not _CAM_WORD.search(text):
        return None
    if _CAM_OFF.search(text):
        return False
    if _CAM_ON.search(text):
        return True
    return None


# ── Camera vision (Task 3) ───────────────────────────────────────────
# On a vision phrase ("what do you see", "look at this", …) the worker
# samples ONE frame from the user's camera track, asks an OpenRouter
# vision model to describe it, and injects that description into the
# user's turn so OpenJarvis (text-only) answers as if Jarvis can see.
_VISION_RE = re.compile(
    r"\b("
    r"what do you see|what can you see|can you see|do you see|"
    r"look at (this|that|me|here|the)|take a look|"
    r"see (this|that)|describe (what|this|the|it)|"
    r"what(’s| is|'s)? (this|that|in front|on the screen)|"
    r"what am i (holding|showing|wearing|pointing)|"
    r"use your eyes|through (the|your) camera|with your camera|read (this|the)"
    r")\b",
    re.I,
)

_VISION_MODEL = os.environ.get(
    "OPENJARVIS_VISION_MODEL", "google/gemini-2.0-flash-001"
)
_vision_client: AsyncOpenAI | None = None


def _is_vision_intent(text: str) -> bool:
    return bool(text) and bool(_VISION_RE.search(text))


def _get_vision_client() -> AsyncOpenAI | None:
    """Lazily build the OpenRouter client; None if no key in env."""
    global _vision_client
    if _vision_client is not None:
        return _vision_client
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        logger.warning(
            "camera vision requested but OPENROUTER_API_KEY is not set"
        )
        return None
    _vision_client = AsyncOpenAI(
        base_url="https://openrouter.ai/api/v1", api_key=key
    )
    return _vision_client


async def _describe_frame(frame: rtc.VideoFrame) -> str | None:
    """Return a 1-2 sentence description of a camera frame, or None."""
    client = _get_vision_client()
    if client is None:
        return None
    try:
        jpeg = encode(
            frame,
            EncodeOptions(
                format="JPEG",
                resize_options=ResizeOptions(
                    width=1024, height=1024, strategy="scale_aspect_fit"
                ),
            ),
        )
        b64 = base64.b64encode(jpeg).decode()
        resp = await client.chat.completions.create(
            model=_VISION_MODEL,
            max_tokens=150,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Describe what is visible in this webcam "
                                "frame in 1-2 concrete sentences. Focus on "
                                "the main subject, any text, and notable "
                                "details. No preamble."
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{b64}"
                            },
                        },
                    ],
                }
            ],
        )
        desc = (resp.choices[0].message.content or "").strip()
        return desc or None
    except Exception as exc:  # noqa: BLE001
        logger.error("vision describe failed: %s", exc)
        return None


class Assistant(Agent):
    def __init__(self, room: rtc.Room):
        super().__init__(instructions=AGENT_INSTRUCTION)
        self._room = room
        # Most-recent camera frame from the user's video track (or None).
        self._latest_frame: rtc.VideoFrame | None = None
        self._video_tasks: set[asyncio.Task] = set()
        self._wire_video(room)

    # ── Camera track capture ─────────────────────────────────────────
    def _wire_video(self, room: rtc.Room) -> None:
        """Keep only the latest frame from any subscribed camera track."""

        async def _consume(track: rtc.VideoTrack) -> None:
            stream = rtc.VideoStream(track)
            try:
                async for ev in stream:
                    self._latest_frame = ev.frame
            finally:
                await stream.aclose()

        @room.on("track_subscribed")
        def _on_sub(track, pub, participant):  # noqa: ANN001
            if track.kind == rtc.TrackKind.KIND_VIDEO:
                t = asyncio.create_task(_consume(track))
                self._video_tasks.add(t)
                t.add_done_callback(self._video_tasks.discard)

        # The audio AgentSession won't auto-subscribe video, so opt in
        # explicitly whenever a camera track is (or becomes) published.
        @room.on("track_published")
        def _on_pub(pub, participant):  # noqa: ANN001
            if pub.kind == rtc.TrackKind.KIND_VIDEO:
                pub.set_subscribed(True)

        for participant in room.remote_participants.values():
            for pub in participant.track_publications.values():
                if pub.kind == rtc.TrackKind.KIND_VIDEO:
                    pub.set_subscribed(True)
                    if pub.track is not None:
                        t = asyncio.create_task(_consume(pub.track))
                        self._video_tasks.add(t)
                        t.add_done_callback(self._video_tasks.discard)

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        """Intercept camera on/off before it reaches the LLM.

        Sends a structured data-channel command to the browser and speaks
        a short confirmation, then short-circuits the turn (no LLM round
        trip) so "turn the camera on" never becomes a chat message.
        """
        text = getattr(new_message, "text_content", "") or ""

        # 1) Camera on/off — structured command, short-circuit the turn.
        want = _camera_intent(text)
        if want is not None:
            try:
                await self._room.local_participant.publish_data(
                    json.dumps({"type": "camera", "enabled": want}).encode(),
                    reliable=True,
                    topic=UI_COMMAND_TOPIC,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("camera command publish failed: %s", exc)
                return
            await self.session.say(
                "Camera on, sir." if want else "Camera off, sir."
            )
            raise StopResponse()

        # 2) Vision — sample a frame and inject a description so the
        #    normal OpenJarvis text turn answers as if Jarvis can see.
        if _is_vision_intent(text):
            frame = self._latest_frame
            if frame is None:
                new_message.content.append(
                    "\n\n[Camera vision: the camera is off or no frame is "
                    "available — ask the user to turn the camera on.]"
                )
                return
            desc = await _describe_frame(frame)
            if desc:
                new_message.content.append(
                    f"\n\n[Camera vision — what the user's camera shows "
                    f"right now: {desc}]"
                )
            else:
                new_message.content.append(
                    "\n\n[Camera vision: unable to analyse the camera "
                    "frame just now.]"
                )


async def entrypoint(ctx: agents.JobContext):
    await ctx.connect()

    openjarvis_url = os.environ.get(
        "OPENJARVIS_URL", "http://localhost:8000"
    ).rstrip("/")
    openjarvis_key = os.environ.get("OPENJARVIS_API_KEY", "basic-auth")
    model_name = os.environ.get("OPENJARVIS_MODEL", "openrouter/auto")

    try:
        stt = deepgram.STT()
        logger.info("STT: Deepgram initialized")
    except Exception as exc:
        logger.error("Deepgram STT init failed: %s", exc)
        raise RuntimeError("STT provider unavailable") from exc

    try:
        tts = deepgram.TTS()
        logger.info("TTS: Deepgram Aura initialized")
    except Exception as exc:
        logger.error("Deepgram TTS init failed: %s", exc)
        raise RuntimeError("TTS provider unavailable") from exc

    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=stt,
        llm=openai.LLM(
            model=model_name,
            base_url=f"{openjarvis_url}/v1",
            api_key=openjarvis_key,
            extra_headers={
                **_openjarvis_auth_headers(),
                # Tell OpenJarvis to emit a STRICT OpenAI SSE stream
                # (only chat chunks + [DONE]) — the LiveKit LLM client
                # crashes on OpenJarvis's custom `event:` UI events.
                "X-OpenJarvis-Stream": "openai",
            },
        ),
        tts=tts,
    )

    await session.start(
        room=ctx.room,
        agent=Assistant(ctx.room),
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVC()
        ),
    )


if __name__ == "__main__":
    agents.cli.run_app(
        agents.WorkerOptions(
            agent_name="openjarvis-agent",
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
        )
    )
