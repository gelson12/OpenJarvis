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
import time
import base64
import random
import asyncio
import logging

import httpx
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


# ── Elaboration (proactive Claude-CLI follow-up) ─────────────────────
# OpenJarvis's routes.py:745 already spawns a slow-track elaboration for
# every /v1/chat/completions request, voice included. The worker just
# needs to SURFACE it through Aura — saying the polite prompt with the
# agent's actual voice, listening for spoken yes/no via the existing STT
# path, and speaking the full Claude-CLI answer on accept. No browser
# banner is mounted on the Voice page (browser TTS + browser
# SpeechRecognition would fight LiveKit for mic + speakers).
_ELAB_DEADLINE_S = 30.0  # mirrors LISTEN_TIMEOUT_MS in ElaborationBanner

# Polite spoken openers — short, voice-friendly, mirror the chat banner.
_POLITE_PROMPTS = (
    "If I may, sir — would you like me to expand on that?",
    "There's a touch more on this if you'd care to hear it, sir.",
    "Shall I dig a little deeper, sir?",
    "Care for a fuller answer, sir?",
)

# Voice intent classification — Python port of voice_intents.ts so we
# can match the spoken yes/no without a round-trip to the frontend.
_AFFIRM_RE = re.compile(
    r"\b("
    r"yes|yeah|yep|yup|sure|ok(?:ay)?|please|"
    r"go ahead|go on|carry on|proceed|continue|"
    r"of course|certainly|absolutely|"
    r"elaborat\w*|expand|tell me more|do it|send it|fire (?:it|away)"
    r")\b",
    re.I,
)
_NEGATIVE_RE = re.compile(
    r"\b(no|nope|not now|not yet|skip|dismiss|later|nah|no thanks?|no thank you)\b",
    re.I,
)
_STOP_RE = re.compile(
    r"\b(stop|cancel|never ?mind|abort|forget it|quiet|shut up)\b",
    re.I,
)


def _classify_intent(text: str) -> str:
    """Return 'affirmative' | 'negative' | 'stop' | 'speech'.

    Stop beats negative beats affirmative — a "cancel" should never be
    misread as a yes even if the rest of the phrase rambles politely.
    """
    if not text:
        return "speech"
    t = text.strip().lower()
    if _STOP_RE.search(t):
        return "stop"
    if _NEGATIVE_RE.search(t):
        return "negative"
    if _AFFIRM_RE.search(t):
        return "affirmative"
    return "speech"


# ── Wake / sleep ─────────────────────────────────────────────────────
# The session auto-connects, so the worker is always listening. It stays
# DORMANT (silent, ignores speech) until it hears the wake phrase, and
# returns to dormant on the sleep phrase. Gated in on_user_turn_completed
# via `raise StopResponse()` (livekit-agents catches it → turn dropped).
# Wake match is deliberately forgiving — Deepgram routinely mis-hears
# "Jarvis" as "Travis / Jervis / Java's / Jarviss". In dormant mode a
# false positive is harmless (it just wakes), so we accept the common
# variants to make activation reliable.
_WAKE_RE = re.compile(
    r"\b(jarvis|jarviss|jarvi|jervis|travis|java'?s|charvis)\b", re.I
)
_SLEEP_RE = re.compile(
    r"\b(good\s?bye|go to sleep|sleep now|that'?s all for now|stand down)\b",
    re.I,
)


class Assistant(Agent):
    def __init__(self, room: rtc.Room):
        super().__init__(instructions=AGENT_INSTRUCTION)
        self._room = room
        # Wake/sleep: dormant on connect, woken by "Hey Jarvis".
        self._awake = False
        # Most-recent camera frame from the user's video track (or None).
        self._latest_frame: rtc.VideoFrame | None = None
        self._seen_frame = False
        self._video_tasks: set[asyncio.Task] = set()
        self._wire_video(room)
        # Elaboration state — set by the SSE listener, cleared on the
        # next user turn (yes/no) or when the deadline passes.
        self._pending_elab_id: str | None = None
        self._pending_elab_deadline: float = 0.0
        self._elab_task: asyncio.Task | None = None
        self._oj_base: str = ""
        self._oj_auth: dict = {}

    # ── Camera track capture ─────────────────────────────────────────
    def _wire_video(self, room: rtc.Room) -> None:
        """Keep only the latest frame from any subscribed camera track."""

        async def _consume(track: rtc.VideoTrack) -> None:
            stream = rtc.VideoStream(track)
            logger.info("video: subscribed to a video track")
            try:
                async for ev in stream:
                    self._latest_frame = ev.frame
                    if not self._seen_frame:
                        self._seen_frame = True
                        logger.info("video: first frame received")
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

    # ── Elaboration bridge (proactive Claude-CLI follow-up) ──────────
    def start_elaboration_listener(
        self, openjarvis_url: str, auth_headers: dict
    ) -> None:
        """Subscribe to OpenJarvis's elaborations SSE in the background.

        Idempotent — repeated calls leave the existing task running.
        """
        self._oj_base = openjarvis_url.rstrip("/")
        self._oj_auth = dict(auth_headers)
        if self._elab_task is not None and not self._elab_task.done():
            return
        self._elab_task = asyncio.create_task(self._consume_elab_stream())

    async def _consume_elab_stream(self) -> None:
        """Long-lived SSE consumer with exponential backoff (cap 30 s).

        Parses MCP-style server-sent events (event: + data:) and routes
        them to _handle_elab_event.
        """
        url = f"{self._oj_base}/v1/elaborations/stream"
        headers = {**self._oj_auth, "Accept": "text/event-stream"}
        backoff = 1.0
        while True:
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("GET", url, headers=headers) as r:
                        r.raise_for_status()
                        backoff = 1.0
                        event_name: str | None = None
                        data_lines: list[str] = []
                        async for line in r.aiter_lines():
                            if line == "":
                                if event_name and data_lines:
                                    payload = "\n".join(data_lines)
                                    await self._handle_elab_event(
                                        event_name, payload
                                    )
                                event_name = None
                                data_lines = []
                                continue
                            if line.startswith(":"):
                                continue  # SSE comment / heartbeat
                            if line.startswith("event:"):
                                event_name = line[6:].strip()
                            elif line.startswith("data:"):
                                data_lines.append(line[5:].lstrip())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "elaboration SSE disconnected (%s); reconnecting in %.0fs",
                    exc,
                    backoff,
                )
                await asyncio.sleep(min(backoff, 30.0))
                backoff = min(backoff * 2.0, 30.0)

    async def _handle_elab_event(self, event: str, raw: str) -> None:
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            return

        if event == "proposed":
            elab_id = data.get("id")
            if not elab_id:
                return
            # Don't surface proactive prompts while dormant — just
            # dismiss them so they don't linger server-side.
            if not self._awake:
                asyncio.create_task(self._elab_dismiss(elab_id))
                return
            # If a previous elaboration is still pending, dismiss it so
            # we never queue overlapping spoken prompts.
            if self._pending_elab_id and self._pending_elab_id != elab_id:
                old = self._pending_elab_id
                asyncio.create_task(self._elab_dismiss(old))
            self._pending_elab_id = elab_id
            self._pending_elab_deadline = time.time() + _ELAB_DEADLINE_S
            polite = random.choice(_POLITE_PROMPTS)
            try:
                await self.session.say(polite)
            except Exception as exc:  # noqa: BLE001
                logger.warning("session.say polite prompt failed: %s", exc)
            return

        if event == "accepted_full":
            answer = (
                data.get("claude_answer") or data.get("answer") or ""
            ).strip()
            if not answer:
                return
            # Never speak a raw internal status/log line — the Claude-CLI
            # backend sometimes returns e.g.
            # "[cli_worker: CLI auth recovery in progress …]" instead of a
            # real elaboration. Drop those silently.
            low = answer.lower()
            if answer.startswith("[") or any(
                marker in low
                for marker in (
                    "cli_worker",
                    "auth recovery",
                    "claude_pro task",
                    "skipping",
                    "traceback",
                )
            ):
                logger.warning("elaboration answer looked like a log line — dropped")
                if self._pending_elab_id == data.get("id"):
                    self._pending_elab_id = None
                return
            try:
                await self.session.say(answer)
            except Exception as exc:  # noqa: BLE001
                logger.warning("session.say elaboration answer failed: %s", exc)
            if self._pending_elab_id == data.get("id"):
                self._pending_elab_id = None
            return

        if event in ("dismissed", "discarded", "failed"):
            if self._pending_elab_id == data.get("id"):
                self._pending_elab_id = None
            return

    async def _elab_post(self, path: str) -> None:
        url = f"{self._oj_base}{path}"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(url, headers=self._oj_auth)
        except Exception as exc:  # noqa: BLE001
            logger.warning("elaboration POST %s failed: %s", path, exc)

    async def _elab_accept(self, elab_id: str) -> None:
        await self._elab_post(f"/v1/elaborations/{elab_id}/accept")

    async def _elab_dismiss(self, elab_id: str) -> None:
        await self._elab_post(f"/v1/elaborations/{elab_id}/dismiss")

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        """Intercept commands before they reach the LLM.

        Order matters:
        1. Pending elaboration yes/no — short-circuit (don't treat "yes"
           as a new question; clean up stale prompts past the deadline).
        2. Camera on/off — structured data-channel command + confirmation.
        3. Vision intent — inject a frame description so OpenJarvis can
           answer as if it can see.
        """
        text = getattr(new_message, "text_content", "") or ""

        # ── Wake / sleep gate (runs before everything else) ──────────
        # The session is always connected; stay dormant until "Hey
        # Jarvis", return to dormant on "goodbye Jarvis".
        if not self._awake:
            if _WAKE_RE.search(text):
                self._awake = True
                rest = _WAKE_RE.sub("", text).strip(" ,.!?-")
                if len(rest.split()) >= 2:
                    # "Hey Jarvis, what's the time" — answer the question.
                    new_message.content = [rest]
                    text = rest
                else:
                    await self.session.say("Yes, sir?")
                    raise StopResponse()
            else:
                raise StopResponse()  # dormant — ignore non-wake speech
        else:
            if _SLEEP_RE.search(text):
                self._awake = False
                await self.session.say("Goodbye, sir.")
                raise StopResponse()

        # 0) Pending elaboration yes/no — must run BEFORE camera/vision
        # so "yes" doesn't accidentally trigger something else.
        if self._pending_elab_id:
            if time.time() < self._pending_elab_deadline:
                intent = _classify_intent(text)
                if intent == "affirmative":
                    elab_id = self._pending_elab_id
                    self._pending_elab_id = None
                    await self._elab_accept(elab_id)
                    raise StopResponse()
                if intent in ("negative", "stop"):
                    elab_id = self._pending_elab_id
                    self._pending_elab_id = None
                    await self._elab_dismiss(elab_id)
                    raise StopResponse()
                # Else: user is continuing the conversation — leave the
                # elaboration pending until the deadline; don't speak the
                # polite prompt again.
            else:
                # Deadline elapsed before any yes/no — silently dismiss.
                elab_id = self._pending_elab_id
                self._pending_elab_id = None
                asyncio.create_task(self._elab_dismiss(elab_id))

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

    # STT with wake-word boosting. "Jarvis" is an uncommon proper noun
    # Deepgram routinely mis-hears (Travis/Jervis/service…), which is why
    # waking took several tries. We boost it via Deepgram's keyword API —
    # trying the nova-3 (`keyterms`) and nova-2 (`keywords`) forms in
    # turn, falling back to a plain STT so the worker never fails to
    # start on an API mismatch.
    stt = None
    for _kw in (
        {"keyterms": ["Jarvis"]},
        {"keywords": [("Jarvis", 5.0)]},
        {},
    ):
        try:
            stt = deepgram.STT(**_kw)
            logger.info("STT: Deepgram initialized (boost=%s)", bool(_kw))
            break
        except TypeError:
            continue  # kwarg not supported in this plugin version
        except Exception as exc:
            logger.error("Deepgram STT init failed: %s", exc)
            raise RuntimeError("STT provider unavailable") from exc
    if stt is None:
        raise RuntimeError("STT provider unavailable")

    # TTS — male, Jarvis-like voice. Deepgram Aura 'orion' is a calm,
    # composed male voice; override via OPENJARVIS_TTS_VOICE.
    tts_voice = os.environ.get("OPENJARVIS_TTS_VOICE", "aura-orion-en")
    try:
        tts = deepgram.TTS(model=tts_voice)
        logger.info("TTS: Deepgram Aura initialized (voice=%s)", tts_voice)
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

    assistant = Assistant(ctx.room)
    await session.start(
        room=ctx.room,
        agent=assistant,
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVC(),
            # Required for the worker to receive the user's camera +
            # screen-share tracks (default off → no video reaches us).
            video_enabled=True,
        ),
    )
    # Proactive elaboration is OFF by default. The OpenJarvis dual-track
    # elaboration fires on every turn (not the "3+ coherent topics" the
    # user expects) and its Claude-CLI backend currently leaks raw status
    # lines (e.g. "[cli_worker: CLI auth recovery in progress …]") as the
    # spoken answer. Re-enable only with OPENJARVIS_VOICE_ELABORATION=1
    # once that backend is reliable and topic-gated.
    if os.environ.get("OPENJARVIS_VOICE_ELABORATION", "").lower() in (
        "1",
        "true",
        "yes",
    ):
        assistant.start_elaboration_listener(
            openjarvis_url, _openjarvis_auth_headers()
        )
    else:
        logger.info("voice elaboration disabled (set OPENJARVIS_VOICE_ELABORATION=1 to enable)")


if __name__ == "__main__":
    agents.cli.run_app(
        agents.WorkerOptions(
            agent_name="openjarvis-agent",
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
        )
    )
