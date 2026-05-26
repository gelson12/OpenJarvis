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
import uuid
import base64
import random
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv
from livekit import agents, rtc, api
from livekit.agents import (
    AgentSession,
    Agent,
    RoomInputOptions,
    StopResponse,
    function_tool,
)
from livekit.agents.utils.images import encode, EncodeOptions, ResizeOptions
from livekit.plugins import openai, deepgram, silero, noise_cancellation
from openai import AsyncOpenAI

# Gemini Live (primary low-latency path). Imported lazily-guarded so a
# missing google-genai install doesn't crash worker startup — the cascade
# path stays usable on its own.
try:
    from livekit.plugins.google.realtime import RealtimeModel as _GeminiRealtimeModel
    from google.genai import types as _genai_types
    _REALTIME_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001
    _GeminiRealtimeModel = None  # type: ignore[assignment]
    _genai_types = None  # type: ignore[assignment]
    _REALTIME_AVAILABLE = False
    logging.getLogger(__name__).warning(
        "livekit-plugins-google / google-genai unavailable (%s) — "
        "Gemini Live disabled, cascade only", _exc,
    )

import search_tools
from resource_ledger import ResourceLedger
from hermes_router import HermesRouter, enabled as _hermes_route_enabled, should_route as _hermes_should_route

# Accommodation booking — lazy import so a missing/broken accommodation
# module never crashes worker startup. See
# `brain/Accommodation Booking — Implementation Plan` in the user's vault.
try:
    from accommodation import (  # noqa: F401
        AccommodationService,
        Property as _AccommodationProperty,
        SearchQuery as _AccommodationSearchQuery,
    )
    _ACCOMMODATION_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001
    AccommodationService = None  # type: ignore[assignment]
    _AccommodationProperty = None  # type: ignore[assignment]
    _AccommodationSearchQuery = None  # type: ignore[assignment]
    _ACCOMMODATION_AVAILABLE = False
    logging.getLogger(__name__).warning(
        "accommodation module unavailable (%s) — hotel booking disabled", _exc,
    )

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

SCREEN:
- show_widget / hide_widget place or remove floating HUD panels (chat, clock,
  music, search, news, youtube, maps, apps, system) on the user's screen.
- web_search, search_youtube, show_news and show_map open result panels;
  open_browser opens a live, interactive browser the user can click and scroll.
- Call the matching tool when asked; keep the spoken reply to one sentence.

CLARIFICATION (CRITICAL — this is how you handle 'didn't catch that'):
The user's microphone is not always clean. When their message implies they
didn't understand or hear your prior reply, you MUST re-state your prior
reply more clearly — never say "I did not receive a query from you", never
claim the message is empty.

Treat ALL of these as a "please repeat" request, regardless of phrasing:
  "I didn't catch that", "say again", "come again", "pardon?", "huh?",
  "what?", "sorry?", "I beg your pardon", "one more time", "what was that",
  "could you repeat", "I missed that", "I don't follow", "speak up",
  "I didn't hear you", "what did you say", any short interjection that
  signals confusion, OR any other variation you can plausibly read as
  asking you to repeat.

When this happens:
  1. Re-state your previous response in one short sentence.
  2. If unclear what to repeat, ask: "Apologies sir, which part shall I
     repeat?" — NEVER say the user sent nothing.
  3. Don't apologise excessively. One "of course, sir" is enough.

If the user's message is genuinely empty or you cannot infer intent,
respond with a single graceful question, NOT a refusal.
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
    """Pre-load VAD model once per worker process to avoid cold-start delay.

    Tuned for a short single-word wake utterance ("Jarvis"): a low
    min_speech_duration so a ~0.4 s word registers, a short
    min_silence_duration so the turn endpoints fast, and a lower
    activation_threshold so a quietly-spoken wake word still triggers.
    """
    try:
        proc.userdata["vad"] = silero.VAD.load(
            # Tuned even tighter than before for sub-second wake-word
            # response. A short word like "Jarvis" is ~0.4s of audio; if
            # silence detection lags, the full turn is delayed by that
            # much before STT even returns the final transcript.
            min_speech_duration=0.02,
            min_silence_duration=0.20,
            activation_threshold=0.35,
            prefix_padding_duration=0.20,
        )
    except TypeError:
        # Older silero plugin without these kwargs — fall back to defaults.
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


# ── Gesture mode ─────────────────────────────────────────────────────
# Full-screen camera overlay blended into the HUD; hand gestures drive
# the widgets. Toggled by voice ("turn on/off gesture mode"); when
# turning on we force the camera publication on too so MediaPipe has
# frames.
# "Didn't catch that" / "say again" / "repeat" — meta-clarification phrases
# that ask Jarvis to re-state his prior response. Without intercepting,
# these land on the LLM as a fresh (empty-feeling) query and the model
# replies "I did not receive a query from you" — the worst possible UX.
_REPEAT_LAST_RE = re.compile(
    r"\b(?:"
    # "didn't catch/hear/get/understand/follow that" + contractions
    r"d(?:i|y|o)n[\'’]?t\s+(?:quite\s+|really\s+|just\s+)?"
    r"(?:catch|hear|get|understand|follow)\s+(?:that|what\s+you\s+said|you|it)?|"
    # "say that/it again", "say again"
    r"say\s+(?:that|it)?\s*again|"
    # "come again"
    r"come\s+again|"
    # "could/can/would you repeat", "repeat that/please/etc"
    r"(?:could|can|would|will|please)\s+you\s+(?:please\s+)?repeat|"
    r"repeat\s+(?:that|what|yourself|please|it|once\s+more)?|"
    # "one more time"
    r"one\s+more\s+time|"
    # "what did/was you/that say"
    r"what\s+(?:did|was)\s+(?:you|that)\s+(?:say|said)?|"
    # "i missed that/what you said"
    r"i\s+missed\s+(?:that|what\s+you\s+said|it)|"
    # "pardon?" / "I beg your pardon"
    r"(?:i\s+beg\s+your\s+)?pardon|"
    # "not quite sure/clear what you said/mean"
    r"not\s+quite\s+(?:sure|clear)\s+what\s+you\s+(?:said|mean)|"
    # "i don't follow/get that/understand"
    r"i\s+don[\'’]?t\s+(?:follow|get\s+that|understand)|"
    # "speak up" / "louder please"
    r"speak\s+up|louder\s+please|louder,?\s+sir"
    r")\b|"
    # Bare interjections at the END of the utterance (anchored)
    r"(?:^|\s)(?:huh|what|sorry|eh|hmm)\??\s*$",
    re.IGNORECASE,
)


def _last_assistant_from_ctx(turn_ctx: Any) -> str:
    """Pull the most recent assistant message text from a LiveKit chat
    context. Best-effort across livekit-agents versions — tries the
    common attribute / dict shapes and returns "" on miss."""
    if turn_ctx is None:
        return ""
    items = (
        getattr(turn_ctx, "items", None)
        or getattr(turn_ctx, "messages", None)
        or []
    )
    for item in reversed(list(items)):
        role = (
            getattr(item, "role", None)
            or (item.get("role") if isinstance(item, dict) else None)
        )
        if role != "assistant":
            continue
        content = (
            getattr(item, "text_content", None)
            or getattr(item, "content", None)
            or (item.get("content") if isinstance(item, dict) else None)
            or ""
        )
        if isinstance(content, list):
            # Some versions store content as a list of segments
            content = " ".join(
                str(c.get("text") if isinstance(c, dict) else c)
                for c in content if c
            )
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


_GESTURE_MODE_WORD = re.compile(r"\bgesture(?:\s+mode|\s+control)?\b", re.I)
_GESTURE_MODE_OFF = re.compile(
    r"\b(off|disable|stop|exit|leave|cancel|hide|turn it off)\b", re.I
)
_GESTURE_MODE_ON = re.compile(
    r"\b(on|enable|start|activate|enter|begin|turn it on)\b", re.I
)


def _gesture_mode_intent(text: str):
    """Return True (turn on), False (turn off), or None.

    "off" wins on conflict — "turn off gesture mode" is unambiguous.
    """
    if not text or not _GESTURE_MODE_WORD.search(text):
        return None
    if _GESTURE_MODE_OFF.search(text):
        return False
    if _GESTURE_MODE_ON.search(text):
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
        # Bound the vision call — a slow OpenRouter response must never
        # stall the voice turn (the await runs inside on_user_turn_completed).
        resp = await asyncio.wait_for(
            client.chat.completions.create(
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
                                    "frame in 1-2 concrete sentences. Focus "
                                    "on the main subject, any text, and "
                                    "notable details. No preamble."
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
            ),
            timeout=12.0,
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
    # Deepgram routinely mis-hears "Jarvis" as one of these. False wakes
    # are harmless (dormant just flips on; user can sleep again), so we
    # err on the side of recall. Every entry here came from a real
    # mis-transcription either reported by the user or seen in
    # production logs / community reports.
    r"\b("
    r"jarvis|jarviss|jarvi|jarves|jervis|jervi|jeevis|"
    r"travis|harvey|harvis|jarvey|charvis|carvis|"
    r"java'?s|javis|jarv|garvis|"
    r"drivers|driver|service|services|"
    r"jaris|jarus|jervice|jarvice"
    r")\b",
    re.I,
)
_SLEEP_RE = re.compile(
    r"\b(good\s?bye|go to sleep|sleep now|that'?s all for now|stand down)\b",
    re.I,
)


# ── Screen widgets (floating HUD panels) ─────────────────────────────
# The browser renders draggable, semi-transparent widget panels. The
# worker summons them by publishing a JSON command on the `jarvis-ui`
# data topic in the user's room.
_UI_TOPIC = "jarvis-ui"
_UI_OPEN_RE = re.compile(
    r"\b(open|show|bring up|display|pop up|put up|launch)\b", re.I
)
_UI_CLOSE_RE = re.compile(
    r"\b(close|hide|dismiss|get rid of|take down)\b", re.I
)
_WIDGET_WORDS: dict[str, str] = {
    "chat": r"chat|conversation|transcript|messages?",
    "music": r"music|spotify|player|songs?",
    "news": r"news|headlines",
    "youtube": r"youtube|videos?",
    "maps": r"maps?|directions|navigation",
    "search": r"search|google",
    "apps": r"apps?|services|programs?|launcher",
    "system": r"system|diagnostics",
    "clock": r"clock|chronometer",
}


def _widget_from_text(text: str) -> str | None:
    """Return the widget kind named in ``text``, or None."""
    for kind, pattern in _WIDGET_WORDS.items():
        if re.search(rf"\b(?:{pattern})\b", text, re.I):
            return kind
    return None


# ── Content intents (search / video / news / maps / live browser) ────
# OpenJarvis routes a worker's @function_tool calls to its own server-side
# tool registry, so worker-defined tools never actually fire. We instead
# detect these intents with a regex on the user's turn — the same approach
# the camera and widget handlers use — and call the matching method direct.
_BROWSER_RE = re.compile(
    r"\b(?:open|launch|start|bring up)\b[^.]*\bbrowser\b|\bopen chrome\b", re.I
)
_URL_RE = re.compile(
    r"\b(?:go to|browse to|navigate to|open)\s+"
    r"((?:https?://)?(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+[^\s,]*)", re.I
)
_YOUTUBE_RE = re.compile(r"\byoutube\b|\bvideos?\s+(?:of|about|for)\b", re.I)
_NEWS_RE = re.compile(r"\b(?:news|headlines)\b", re.I)
_MAP_RE = re.compile(r"\b(?:maps?|directions?)\b", re.I)
_WEBSEARCH_RE = re.compile(
    r"\b(?:search\s+(?:the\s+)?(?:web|internet|google)|google|look\s+up|"
    r"web\s+search|search\s+for)\b", re.I
)
# An explicit "put it on screen" verb. The bare nouns `news`/`maps` match
# far too eagerly ("any news on my project" should be answered, not turned
# into a news panel), so those two intents additionally require this verb.
_CONTENT_VERB = re.compile(
    r"\b(show|open|bring up|pull up|display|get me|give me|pop up|put up|"
    r"launch|see)\b", re.I
)

# Accommodation booking intent. Catches "find me a hotel", "book a place",
# "Airbnb in Lisbon", etc. See `brain/Accommodation Booking — Feasibility &
# Architecture` in the user's Obsidian vault.
_ACCOMMODATION_RE = re.compile(
    r"\b(hotel|airbnb|accommodation|vacation\s+rental|short\s+let|"
    r"book\s+(?:a\s+|me\s+)?(?:room|place|hotel|stay)|where\s+to\s+stay|"
    r"find\s+(?:me\s+)?(?:a\s+|an\s+)?(?:hotel|place|stay|room))\b", re.I
)
_ACCOMMODATION_BOOK_RE = re.compile(
    r"\b(?:book|reserve)\s+(?:the\s+|that\s+|it\b)", re.I
)
# Affirmative / negative cues for the confirm-before-book dialogue.
_ACCOMMODATION_YES_RE = re.compile(
    r"\b(?:yes|yeah|yep|sure|ok|okay|go\s+ahead|do\s+it|"
    r"book\s+it|confirm|please\s+do|sounds\s+good)\b", re.I
)
_ACCOMMODATION_NO_RE = re.compile(
    r"\b(?:no|nope|nah|cancel|never\s*mind|don'?t|stop|"
    r"actually\s+no|hold\s+on)\b", re.I
)
# Pending-book confirmation TTL — discard a forgotten quote after 90s so
# a stale state never books something the user didn't intend.
_ACCOMMODATION_PENDING_TTL_S = 90.0


# Lead / filler / command words stripped to recover the bare query from a
# spoken request like "can you play a video of cars on youtube".
_QUERY_NOISE = re.compile(
    r"\b(?:hey |ok |okay )?jarvis\b"
    r"|\b(?:can|could|would|will)\s+you\b|\bplease\b|\bfor me\b"
    # Polite request stems. The previous version only caught "I want to"
    # / "I'd like to" — it dropped "I would like to google Tom Cruise"
    # into the LLM as a junk search query ("I would like to Tom Cruise")
    # because "I would like to" was left in. Now covers like/love/want/
    # prefer in both contracted ("I'd") and uncontracted ("I would") form,
    # plus "let's" / "let me".
    r"|\bi\s+(?:want|need|wanna)(?:\s+to|\s+you\s+to)?\b"
    r"|\bi\s+would\s+(?:like|love|want|prefer)(?:\s+to|\s+you\s+to)?\b"
    r"|\bi'?d\s+(?:like|love|want|prefer)(?:\s+to|\s+you\s+to)?\b"
    r"|\blet'?s\b|\blet\s+me\b"
    r"|\b(?:search(?:\s+the\s+(?:web|internet))?(?:\s+for)?|google|look\s+up"
    r"|web\s+search(?:\s+for)?|find(?:\s+me)?|show(?:\s+me)?|bring\s+up"
    r"|pull\s+up|put\s+up|open|play|get\s+me|display|watch|see)\b"
    r"|\bon\s+(?:the\s+)?(?:web|internet)\b|\bonline\b",
    re.I,
)


def _clean_query(text: str) -> str:
    """Strip command / filler words to recover the bare search query."""
    q = " " + (text or "") + " "
    prev = None
    while prev != q:
        prev = q
        q = _QUERY_NOISE.sub(" ", q)
    q = re.sub(r"\s+", " ", q).strip(" ,.?!-\"'")
    # Drop a dangling leading article / filler pronoun left after the
    # command word is gone ("play a video of cars" -> "a cars" -> "cars";
    # "google me tom cruise" -> "me tom cruise" -> "tom cruise"). "me"
    # added because "google me X" / "search me X" are common spoken forms.
    q = re.sub(r"^(?:a|an|the|some|my|me|for\s+me)\b\s*", "", q, flags=re.I)
    return q


# ── Noisy-transcript detection ──────────────────────────────────────
# A content-intent topic ("news on X") that contains imperative fragments
# ("fix that", "also, the…") or stacked commas is almost certainly a
# garbled STT result, not what the user actually meant. We refuse to
# dispatch on it and ask a clarifying question instead.
_NOISY_TOPIC_RE = re.compile(
    r"\b(?:fix\s+(?:that|this|it)|do\s+that|also[,]?|by\s+the\s+way|"
    r"never\s*mind|nevermind|stop|wait|hold\s+on|cancel|forget\s+it|"
    r"hmm+|uh+|um+|like\s+i\s+said)\b",
    re.I,
)


def _topic_looks_noisy(arg: str) -> bool:
    """Heuristic: does this cleaned topic look like STT noise, not a query?

    Triggers when the topic:
      - is suspiciously long (> 8 words / > 60 chars after cleaning)
      - contains an imperative fragment (`fix that` / `also,` / filler)
      - has 2+ commas (multi-clause sentence stitched into the topic)
    """
    a = (arg or "").strip()
    if not a:
        return False  # empty is fine — handled as "top of category"
    if len(a) > 60:
        return True
    if len(a.split()) > 8:
        return True
    if a.count(",") >= 2:
        return True
    if _NOISY_TOPIC_RE.search(a):
        return True
    return False


def _content_intent(text: str):
    """Detect a HUD content intent. Returns ``(kind, arg)`` or ``None``.

    kind ∈ {browser, youtube, news, maps, web}; ``arg`` is the query/URL
    ('' is allowed for browser and news, where it is optional).
    """
    t = (text or "").strip()
    if not t:
        return None
    low = t.lower()

    url = _URL_RE.search(t)
    if _BROWSER_RE.search(low):
        return ("browser", url.group(1) if url else "")
    if url:
        return ("browser", url.group(1))

    # "google map(s)" is unambiguous — must route to maps, NOT websearch.
    # Without this, the bare "google" in _WEBSEARCH_RE swallows the phrase
    # and we search the web for "map" (zero useful results). Checked
    # before youtube/news/maps so "show me the google map" works without
    # needing the _CONTENT_VERB gate that protects the bare "map".
    if re.search(r"\bgoogle\s+maps?\b", low):
        q = re.sub(r"\b(?:google\s+)?maps?\b|\bnavigation\b", " ", t, flags=re.I)
        q = _clean_query(q)
        q = re.sub(r"^(?:of|to|for|the|on)\s+", "", q, flags=re.I).strip()
        return ("maps", q)

    if _YOUTUBE_RE.search(low):
        # The query can sit either side of "youtube" ("cars on youtube",
        # "youtube for cars"), so strip every youtube/video marker and the
        # command words wrapping it — what is left is the query itself.
        q = re.sub(r"\b(?:on|from|in|via|over\s+on)\s+youtube\b", " ", t, flags=re.I)
        q = re.sub(r"\byoutube\b", " ", q, flags=re.I)
        q = re.sub(r"\b(?:videos?|clips?|footage)\s+(?:of|about|for|on|with)\b",
                   " ", q, flags=re.I)
        q = re.sub(r"\b(?:videos?|clips?|footage)\b", " ", q, flags=re.I)
        return ("youtube", _clean_query(q))

    if _NEWS_RE.search(low) and (_CONTENT_VERB.search(low) or "headlines" in low):
        q = re.sub(r"\b(?:news|headlines)\b|\b(?:about|on|regarding)\b",
                   " ", t, flags=re.I)
        return ("news", _clean_query(q))

    if _MAP_RE.search(low) and (
        _CONTENT_VERB.search(low)
        or re.search(r"\b(?:directions?\s+to|map\s+of|navigate\s+to|where\s+is)\b",
                     low)
    ):
        q = re.sub(r"\b(?:google\s+)?maps?\b|\bnavigation\b", " ", t, flags=re.I)
        q = _clean_query(q)
        q = re.sub(r"^(?:of|to|for|the)\s+", "", q, flags=re.I).strip()
        return ("maps", q)

    if _WEBSEARCH_RE.search(low):
        return ("web", _clean_query(t))

    return None


# ── Bridge lifecycle (status + cold-start) ──────────────────────────
# Distinct from `_DESKTOP_HINT_RE` (which fires on file/app/volume verbs).
# These match utterances ABOUT the bridge itself — "are the bridges
# online?", "is the laptop bridge up?", "start the bridge on the rog".
#
# Two sub-intents are accepted:
#   STATUS  — "are/is X online/up/down/connected", "bridge status",
#             "which machines are online"
#   START   — "start/wake/launch/boot the bridge", "spin up the laptop"
#
# Conservative on purpose: the bare word "bridge" is far too generic
# ("Tower Bridge", "build a bridge to X"); we require an explicit
# connectivity verb (online/up/down/...) OR a start verb.
_BRIDGE_LIFECYCLE_STATUS_RE = re.compile(
    r"\b("
    r"(?:are|is)\s+(?:the\s+|my\s+|our\s+)?(?:bridges?|laptop|rog|pc|"
    r"desktop|computer|machine)s?(?:\s+(?:bridges?|connection))?\s+"
    r"(?:online|offline|up|down|connected|alive|running|reachable|live)|"
    r"(?:bridges?|connections?)\s+status|"
    r"(?:which|what)\s+(?:bridges?|machines?|computers?)\s+(?:are\s+)?"
    r"(?:online|connected|up|alive|reachable|live)"
    r")\b",
    re.I,
)
_BRIDGE_LIFECYCLE_START_RE = re.compile(
    r"\b(?:start|launch|wake|spin\s*up|bring\s+up|fire\s+up|boot|"
    r"kick\s+off|wake\s+up|switch\s+on|turn\s+on)\s+"
    r"(?:the\s+|my\s+|our\s+)?"
    r"(?:bridges?|desktop[\s\-]bridges?|run\.?\s*bat|"
    r"(?:laptop|rog|pc|desktop|computer|machine)(?:\s+bridges?)?)"
    r"\b",
    re.I,
)


def _bridge_lifecycle_target(text: str) -> str:
    """Pick the target machine for a bridge-lifecycle utterance.

    Returns one of the machine labels ('laptop' / 'rog' / 'pc' / 'desktop'
    / 'computer' / 'machine') if explicitly named, else 'all' for a
    broadcast question like "are the bridges online?". The labels mirror
    those the user actually says — `DesktopBridge.is_online()` collapses
    'pc'/'desktop'/'computer'/'machine'/'any'/'all' to "any online".
    """
    low = (text or "").lower()
    for label in ("laptop", "rog"):
        if re.search(rf"\b{label}\b", low):
            return label
    if re.search(r"\b(pc|desktop|computer|machine)\b", low):
        return "any"
    return "all"


# ── Desktop control (operate the user's Windows machines) ────────────
# A `desktop-bridge` process runs on each Windows machine (laptop, ROG),
# connects OUTBOUND to LiveKit, and sits in the JARVIS_CONTROL_ROOM. We
# join that same room as a second connection, publish a JSON command on
# `desktop-cmd`, and await the matching reply on `desktop-result`. No
# tunnel needed — the worker and the PCs are all outbound-only.
#
# This regex fallback fires the bridge directly on the user's turn — the
# reliable path, since the routed LLM tends to chat instead of emitting
# tool calls. The server-side `desktop_control` ToolRegistry tool is the
# smart, LLM-driven counterpart.
_CONTROL_ROOM = os.environ.get("JARVIS_CONTROL_ROOM", "jarvis-control")
_TOPIC_CMD = "desktop-cmd"
_TOPIC_RESULT = "desktop-result"

# The machine clause. Accept any natural preposition ("on/in/of/from my
# laptop", "of my pc") AND a bare "my laptop / my pc" — Deepgram and
# ordinary speech vary the wording, and the old `on`-only form silently
# dropped "list the files in my laptop" / "lower the volume of my pc".
_DESKTOP_MACHINE_RE = re.compile(
    r"\b(?:on|in|of|from|to|with|using|inside|across)\s+"
    r"(?:my\s+|the\s+|this\s+|our\s+)?"
    r"(laptop|rog|pc|desktop|computer|machine)\b"
    r"|\bmy\s+(laptop|rog|pc|computer|machine)\b",
    re.I,
)
_DESKTOP_OPEN_RE = re.compile(r"\b(open|launch|start)\b", re.I)

# Volume control words. `_VOL_DOWN`/`_VOL_UP` also cover the bare verbs
# ("make it quieter", "turn it up") so a direction is always resolvable.
_VOLUME_WORD = re.compile(
    r"\b(volume|sound|audio|mute|unmute|louder|quieter)\b", re.I
)
_VOL_DOWN = re.compile(
    r"\b(down|decrease|lower|reduce|quiet\w*|soft\w*|less)\b", re.I
)
_VOL_UP = re.compile(
    r"\b(up|increase|raise|boost|crank|loud\w*|higher|more)\b", re.I
)
_VOL_SET = re.compile(r"\b(?:to|at)\s+(\d{1,3})\b", re.I)

# Standalone volume intent — fires WITHOUT requiring an "on my laptop"
# clause, so "mute" / "lower the volume" go through the disambiguation
# router instead of falling into the content matcher. Conservative on
# purpose: the bare word "volume" alone must not trigger ("what's the
# volume of a sphere"); requires a verb or louder/quieter/mute.
_VOLUME_INTENT_RE = re.compile(
    r"\b("
    r"mute|unmute|"
    r"(?:turn|crank|bump|set|put)\s+(?:the\s+|down\s+|up\s+)?"
    r"(?:volume|sound|audio|it|that|music)|"
    r"(?:lower|raise|increase|decrease|reduce|boost|drop|bring\s+down|"
    r"bring\s+up)\s+(?:the\s+)?(?:volume|sound|audio|it|that|music)|"
    r"volume\s+(?:up|down|to|at)|"
    r"(?:louder|quieter|softer)"
    r")\b",
    re.I,
)

# ── Clarification primitive ──────────────────────────────────────────
# Generic "I asked a question, the next user turn is the answer" state.
# Used today by volume disambiguation; reused later by camera selection,
# app_open targeting, etc. Single-slot — a new clarification REPLACES any
# previous one so the state machine never gets stuck. 30s expiry.

_CANCEL_RE = re.compile(
    r"\b(never\s*mind|nevermind|cancel|forget\s+it|skip\s+it|"
    r"nothing|drop\s+it|stop)\b",
    re.I,
)

_ORDINAL_RE: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"\b(?:the\s+)?(?:first|1st|one|number\s*one)\b", re.I), 0),
    (re.compile(r"\b(?:the\s+)?(?:second|2nd|two|number\s*two)\b", re.I), 1),
    (re.compile(r"\b(?:the\s+)?(?:third|3rd|three|number\s*three)\b", re.I), 2),
]

_BOTH_RE = re.compile(r"\b(both|all|everything|every\s*one)\b", re.I)


@dataclass
class PendingClarification:
    """One pending disambiguation question awaiting the user's answer.

    Stored as a single slot on `Assistant`. The resumer dict on the
    assistant maps `intent_kind` to the coroutine that finishes the
    deferred action once an option is picked.
    """
    intent_kind: str
    options: list[dict]
    original_args: dict
    prompt: str
    created_at: float
    expires_at: float


def _derive_match_words(process_name: str) -> list[str]:
    """Turn a process name into the words a user might say for it."""
    name = (process_name or "").strip().lower()
    base = re.sub(r"\.(exe|app|bin)$", "", name).strip()
    if not base:
        return []
    out = {base}
    if base in ("chrome", "msedge", "firefox", "opera", "brave"):
        out.add("browser")
    if base in ("vlc", "wmplayer", "mpc-hc", "mpv"):
        out.add("media player")
    if base == "wmplayer":
        out.add("windows media player")
    if base == "msedge":
        out.add("edge")
    if base == "spotify":
        out.add("music")
    return sorted(out)


# A generous net for "this turn MIGHT be a desktop request". Matching here
# only authorises the LLM router call — the router itself decides whether
# it's actually a desktop command. Cheap to be loose; the router rejects
# false positives with {"desktop": false}.
_DESKTOP_HINT_RE = re.compile(
    r"\b("
    r"laptop|rog|pc|computer|machine|workstation|"
    r"file|files|folder|folders|directory|document|documents|"
    r"download|downloads|"
    r"open|launch|start|run|execute|close|kill|terminate|quit|exit|"
    r"delete|remove|trash|move|copy|rename|create|make|new|"
    r"play|pause|skip|song|songs|music|video|videos|track|tune|"
    r"volume|sound|audio|mute|unmute|louder|quieter|"
    r"memory|ram|cpu|disk|storage|space|process|processes|task|tasks|"
    r"recycle|bin|clean|cleanup|clear|"
    r"app|apps|application|applications|program|programs|"
    r"notepad|chrome|firefox|edge|spotify|word|excel|outlook|"
    r"lock|shutdown|restart|reboot|sleep|"
    r"desktop|screen|wallpaper|"
    r"diagnose|debug|status|scan|"
    r"read|aloud|contents?|txt|pdf|docx?|xlsx?|csv|"
    r"share|attach|attachment"
    r")\b",
    re.I,
)

# The desktop bridge's command catalogue, in the exact shape the bridge
# accepts. Kept near the router so the prompt stays in sync with reality.
_BRIDGE_COMMANDS_GUIDE = """\
Available bridge commands (use `command` and `args`):

  open              {"target": "<app | file/folder path | url>"}
  list_dir          {"path": "<dir>"}              # ~ = home; env vars ok
  read_file         {"path": "<file>"}
  write_file        {"path": "<file>", "content": "<text>"}
  make_dir          {"path": "<dir>"}
  delete            {"path": "<file or dir>"}      # ALWAYS Recycle Bin
  empty_recycle_bin {}
  move              {"src": "<from>", "dst": "<to>"}
  copy              {"src": "<from>", "dst": "<to>"}
  search_files      {"path": "<root>", "pattern": "<name substring>",
                     "limit": 50}
  system_status     {}                              # CPU / RAM / disk
  list_processes    {"top": 10, "by": "memory" | "cpu"}
  close_app         {"name": "<process name, e.g. chrome>"}
  volume            {"action": "up"|"down"|"mute"|"unmute"|"set",
                     "level": 0-100 (for 'set')}
  audio_sessions    {}                              # list per-app audio sessions
  app_volume        {"process_name": "<name>",
                     "action": "up"|"down"|"mute"|"unmute"|"set",
                     "level": 0-100 (for 'set'),
                     "step": 0.0-1.0 (for up/down)}
  media_key         {"key": "play_pause"|"next"|"previous"|"stop"}
  play_media        {"query": "<song or video name>"}
  lock_workstation  {}
  shell             {"command": "<PowerShell, last-resort escape hatch>"}

Standard folders: ~\\Downloads, ~\\Documents, ~\\Desktop, ~\\Pictures,
~\\Music, ~\\Videos."""


_ROUTER_MODEL = os.environ.get(
    "OPENJARVIS_ROUTER_MODEL", "google/gemini-2.0-flash-001"
)
_router_client: AsyncOpenAI | None = None


def _get_router_client() -> AsyncOpenAI | None:
    """OpenRouter client for the intent router. Shares OPENROUTER_API_KEY
    with vision. Returns None when the key is unset — caller falls back."""
    global _router_client
    if _router_client is not None:
        return _router_client
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        logger.warning(
            "desktop router unavailable: OPENROUTER_API_KEY not set"
        )
        return None
    _router_client = AsyncOpenAI(
        base_url="https://openrouter.ai/api/v1", api_key=key
    )
    return _router_client


async def _route_desktop(text: str, machines_online: list[str]) -> dict | None:
    """Turn a free-form spoken request into a structured bridge command.

    Returns ``{"machine", "cmd", "args", "say"}`` on success, or None when
    the request isn't a desktop command / the LLM is unavailable. The
    router is given the live list of online bridges so it can pick a
    sensible default machine when the user doesn't name one.
    """
    client = _get_router_client()
    if client is None or not text:
        return None
    online = ", ".join(machines_online) if machines_online else "none right now"
    system = (
        "You translate ONE spoken request into ONE command for the user's "
        "Windows machines (laptop and rog). Output strict JSON only — no "
        "prose, no code fences.\n\n"
        "Schema. Either:\n"
        '  {"desktop": false}   — when the request is NOT about operating '
        "their computer.\n"
        "Or:\n"
        '  {"desktop": true, "machine": "laptop"|"rog"|"all", '
        '"command": "<name>", "args": {...}, '
        '"say": "<one short butler-voice sentence>"}\n\n'
        f"{_BRIDGE_COMMANDS_GUIDE}\n\n"
        "Rules:\n"
        "- Default machine: laptop. Bridges online right now: "
        + online + ".\n"
        "- Prefer a NAMED command. Use `shell` only when nothing else fits.\n"
        "- `delete` always sends to the Recycle Bin (never permanent).\n"
        "- For app names like Word/Excel/Notepad use `open` with target "
        "like 'notepad' or 'winword'. For URLs use `open` with the URL.\n"
        "- `say` is one short sentence, butler tone ('Right away, sir.', "
        "'Done, sir.', 'On it, sir.').\n"
        "- If the request is conversation, a question, or unclear, output "
        '{"desktop": false}.'
    )
    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=_ROUTER_MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ],
                temperature=0.0,
                max_tokens=400,
                response_format={"type": "json_object"},
            ),
            timeout=6.0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("desktop router LLM failed: %s", exc)
        return None
    raw = (resp.choices[0].message.content or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    try:
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "desktop router non-JSON: %s — %r", exc, raw[:200]
        )
        return None
    if not isinstance(data, dict) or not data.get("desktop"):
        return None
    cmd = (data.get("command") or "").strip()
    if not cmd:
        return None
    return {
        "machine": _norm_machine(str(data.get("machine") or "laptop")),
        "cmd": cmd,
        "args": data.get("args") or {},
        "say": (data.get("say") or "").strip(),
    }

# Spoken folder names → a path the desktop-bridge resolves via
# os.path.expanduser/expandvars. "list my downloads folder" -> "~\Downloads".
_KNOWN_DIRS = {
    "downloads": "~\\Downloads", "download": "~\\Downloads",
    "desktop": "~\\Desktop",
    "documents": "~\\Documents", "document": "~\\Documents",
    "pictures": "~\\Pictures", "picture": "~\\Pictures",
    "videos": "~\\Videos", "video": "~\\Videos",
    "music": "~\\Music",
    "home folder": "~", "home directory": "~", "user folder": "~",
}
# An explicit path: drive (C:\...), ~/..., %VAR%..., or a UNC \\share.
_EXPLICIT_PATH_RE = re.compile(
    r"([a-zA-Z]:\\[^\s,]*|[~%][^\s,]*|\\\\[^\s,]+)"
)


def _norm_machine(machine: str) -> str:
    """Map free-form machine words to a bridge label ('laptop'/'rog'/'all')."""
    m = (machine or "").strip().lower()
    if "rog" in m:
        return "rog"
    if m in ("all", "both", "every"):
        return "all"
    return "laptop"  # default — covers 'laptop', 'desktop', 'pc', '' etc.


def _desktop_path(text: str) -> str:
    """Best-effort file/folder path from a spoken request.

    Order: an explicit path, then a known Windows folder name, then the
    bare word before "folder"/"directory".
    """
    m = _EXPLICIT_PATH_RE.search(text)
    if m:
        return m.group(1)
    low = text.lower()
    for word, path in _KNOWN_DIRS.items():
        if re.search(rf"\b{re.escape(word)}\b", low):
            return path
    m = re.search(r"\b([\w.\-]+)\s+(?:folder|directory|dir)\b", text, re.I)
    if m:
        return m.group(1)
    return ""


def _desktop_intent(text: str):
    """Detect a desktop-control intent. Returns ``(machine, cmd, args)`` or None.

    Requires an explicit "on my laptop / on the rog" clause so ordinary
    conversation never reaches the user's machines by accident.
    """
    if not text:
        return None
    m = _DESKTOP_MACHINE_RE.search(text)
    if not m:
        return None
    # The regex has two machine groups (prepositional vs. bare "my X").
    machine = _norm_machine(m.group(1) or m.group(2))
    # Drop the machine clause so it isn't mistaken for a path/target.
    body = _DESKTOP_MACHINE_RE.sub(" ", text)
    low = body.lower()
    path = _desktop_path(body)

    # Volume — checked first; "decrease the volume" has no path/open verb.
    if _VOLUME_WORD.search(low):
        if re.search(r"\bunmute\b", low):
            return (machine, "volume", {"action": "unmute"})
        if re.search(r"\bmute\b", low):
            return (machine, "volume", {"action": "mute"})
        setm = _VOL_SET.search(low)
        if setm and re.search(r"\b(volume|sound|audio)\b", low):
            return (machine, "volume",
                    {"action": "set", "level": int(setm.group(1))})
        if _VOL_DOWN.search(low):
            return (machine, "volume", {"action": "down"})
        if _VOL_UP.search(low):
            return (machine, "volume", {"action": "up"})
        return None

    if re.search(r"\b(run|execute)\b", low) and "command" in low:
        cmd = re.sub(r"^.*?\bcommand\b[:\s]*", "", body, flags=re.I)
        cmd = cmd.strip(" ,.!?-\"'")
        return (machine, "shell", {"command": cmd}) if cmd else None
    if re.search(r"\bread\b", low) and re.search(r"\bfile\b|\.\w{1,5}\b", low):
        return (machine, "read_file", {"path": path}) if path else None
    # List a folder. A "strong" word (list/files/folder/…) stands alone;
    # a "weak" one (show/see/what's) still needs a path so chit-chat like
    # "what's up on my laptop" doesn't get treated as a directory listing.
    # When no folder is named ("all the files in my laptop"), default to
    # the user's home directory.
    strong_list = re.search(
        r"\b(list|files?|folder|directory|directories|contents?|browse|dir)\b",
        low,
    )
    weak_list = re.search(r"\b(show|see|view|what'?s)\b", low)
    if strong_list or (path and weak_list):
        return (machine, "list_dir", {"path": path or "~"})
    if _DESKTOP_OPEN_RE.search(low):
        target = re.sub(r"^.*?\b(?:open|launch|start)\b\s*", "", body,
                        flags=re.I)
        target = re.sub(r"\b(?:the|a|an|please|for me|up)\b", " ", target,
                        flags=re.I)
        target = re.sub(r"\s+", " ", target).strip(" ,.!?-\"'")
        return (machine, "open", {"target": target}) if target else None
    return None


def _desktop_reply(machine: str, cmd: str, args: dict, res: dict) -> str:
    """Format a desktop-bridge result into a one-line spoken confirmation."""
    if not isinstance(res, dict) or res.get("error"):
        err = res.get("error") if isinstance(res, dict) else "no response"
        return f"I couldn't do that on the {machine}, sir — {err}"
    if cmd == "open":
        return (
            f"Opened {res.get('opened', args.get('target', ''))} "
            f"on the {machine}, sir."
        )
    if cmd == "volume":
        action = args.get("action", "")
        if action == "mute":
            return f"Muted the {machine}, sir."
        if action == "unmute":
            return f"Unmuted the {machine}, sir."
        level = res.get("level")
        if level is not None:
            return f"Volume on the {machine} is now {level} percent, sir."
        return f"Volume on the {machine} adjusted, sir."
    if cmd == "list_dir":
        entries = res.get("entries", [])
        if not entries:
            return f"That folder on the {machine} is empty, sir."
        names = [e.get("name", "") for e in entries[:8]]
        extra = f", and {len(entries) - 8} more" if len(entries) > 8 else ""
        return (
            f"The {machine} folder holds {len(entries)} items, sir — "
            f"{', '.join(names)}{extra}."
        )
    if cmd == "read_file":
        content = (res.get("content") or "").strip()
        if not content:
            return f"That file on the {machine} is empty, sir."
        return f"Here is that file on the {machine}, sir: {content[:300]}"
    if cmd == "shell":
        out = (res.get("stdout") or "").strip()
        rc = res.get("returncode")
        tail = f" {out[:280]}" if out else ""
        return f"Done on the {machine}, sir — exit {rc}.{tail}"
    if cmd == "system_status":
        cpu = res.get("cpu_percent")
        ram_used = res.get("ram_used_gb")
        ram_total = res.get("ram_total_gb")
        ram_pct = res.get("ram_percent")
        disk_free = res.get("disk_free_gb")
        return (
            f"On the {machine}, sir: CPU at {cpu} percent, "
            f"RAM at {ram_pct} percent — {ram_used} of {ram_total} gigs "
            f"used — and {disk_free} gigs free on the system drive."
        )
    if cmd == "list_processes":
        top = res.get("top") or []
        if not top:
            return f"No processes to report on the {machine}, sir."
        head = ", ".join(
            f"{p.get('name','?')} {p.get('ram_mb',0):.0f} MB"
            for p in top[:5]
        )
        return f"Top on the {machine}, sir: {head}."
    if cmd == "close_app":
        n = res.get("closed", 0)
        name = args.get("name", "that app")
        if not n:
            return f"I don't see {name} running on the {machine}, sir."
        plural = "" if n == 1 else " instances"
        return f"Closed {n}{plural} of {name} on the {machine}, sir."
    if cmd == "delete":
        to = res.get("to", "")
        if to == "recycle_bin":
            return f"Sent it to the Recycle Bin on the {machine}, sir."
        if to == "permanent":
            return f"Permanently deleted on the {machine}, sir."
        return f"Deleted on the {machine}, sir."
    if cmd == "empty_recycle_bin":
        return f"Recycle Bin emptied on the {machine}, sir."
    if cmd == "search_files":
        matches = res.get("matches") or []
        if not matches:
            return f"No matches on the {machine}, sir."
        head = [os.path.basename(p) for p in matches[:5]]
        tail = (f", and {len(matches) - 5} more"
                if len(matches) > 5 else "")
        return (
            f"Found {len(matches)} on the {machine}, sir — "
            f"{', '.join(head)}{tail}."
        )
    if cmd in ("move", "copy"):
        verb = "Moved" if cmd == "move" else "Copied"
        return f"{verb} it on the {machine}, sir."
    if cmd == "make_dir":
        return f"Folder created on the {machine}, sir."
    if cmd == "write_file":
        return f"File written on the {machine}, sir."
    if cmd == "media_key":
        key = (args.get("key") or "").lower()
        labels = {
            "play_pause": "Toggled playback",
            "play": "Playing",
            "pause": "Paused",
            "next": "Skipping to the next track",
            "previous": "Going back a track",
            "prev": "Going back a track",
            "stop": "Stopped",
        }
        return f"{labels.get(key, 'Done')} on the {machine}, sir."
    if cmd == "play_media":
        if res.get("playing") == "youtube":
            return (
                f"I didn't find that on the {machine}, sir — opened a "
                "YouTube search instead."
            )
        playing = res.get("playing", "")
        if playing:
            return (
                f"Playing {os.path.basename(playing)} on the {machine}, sir."
            )
        return f"Playing it on the {machine}, sir."
    if cmd == "lock_workstation":
        return f"Locked the {machine}, sir."
    if cmd == "host_info":
        host = res.get("hostname", machine)
        return f"The {machine} reports as {host}, sir — online."
    return f"Done on the {machine}, sir."


# ── OpenCTI (intelligence / investigation layer) ─────────────────────
# Parallel pipeline to the desktop router above. Same shape: a broad
# keyword gate authorises an LLM router call → structured GraphQL
# operation → spoken result. Lives in the worker (not just the
# OpenJarvis backend tool) because the LLM-emitted tool calls aren't
# reliable on the routed model — proven by the desktop layer.

_CTI_HINT_RE = re.compile(
    r"\b("
    r"opencti|cti|"
    r"intel|intelligence|threat|threats|"
    r"observable|observables|indicator|indicators|"
    r"incident|incidents|investigate|investigation|"
    r"adversary|actor|ioc|iocs|stix|"
    r"kill\s?chain|campaign|malware|phishing|breach|"
    r"suspicious|"
    r"log\s+(?:the\s+|that\s+|this\s+)?(?:domain|ip|url|hash|email|file)|"
    r"indicators\s+of\s+compromise|"
    # Lifecycle words — "activate / wake / boot / spin up / stand down /
    # power down Global Eyes" etc. route into the CTI router for the
    # spinup/spindown commands.
    r"global\s*eyes|"
    r"(?:activate|wake|boot|fire\s?up|spin\s?up|stand\s?down|"
    r"power\s?down|shut\s?down|kill)\s+(?:the\s+)?"
    r"(?:global\s*eyes|intel|intelligence|cti|opencti)"
    r")\b",
    re.I,
)

_CTI_COMMANDS_GUIDE = """\
Available OpenCTI commands (use `command` and `args`):

  cti_search        {"query": "<term>", "limit": 10}
                    Search across STIX objects (entities, observables,
                    indicators, incidents, threat actors, malware,
                    reports, intrusion sets).

  cti_add_observable {"value": "<observable value>",
                      "observable_type": "domain"|"ip"|"ipv6"|"url"
                                       |"email"|"md5"|"sha1"|"sha256"
                                       |"hash"|"file"|"user-agent"
                                       |"mutex"|"registry-key"}
                    Log a new cyber observable in the knowledge graph.

  cti_create_incident {"name": "<incident name>",
                       "description": "<free text, optional>"}

  cti_link          {"from_id": "<stix id>", "to_id": "<stix id>",
                     "relationship": "related-to"|"indicates"
                                   |"attributed-to"|"uses"|"targets"
                                   |"mitigates"|"based-on"}

  cti_summary       {"hours": 24}     # rollup of new objects in window
  cti_list_indicators {"limit": 10}
  cti_enrich        {"value": "<observable value>",
                     "observable_type": "domain"|"ip"|"ipv6"|"url"
                                       |"email"|"md5"|"sha1"|"sha256"
                                       |"hash"|"file"|"user-agent"
                                       |"mutex"|"registry-key"}
                    Add the observable AND wait ~30s for enrichment
                    connectors (VirusTotal, AbuseIPDB, Shodan, etc.)
                    to fire, then report the findings. Use for:
                    "enrich X", "look up X", "scan X", "check X
                    against threat intel", "is X malicious".
  cti_open_panel    {"dashboard": "<slug>", "path": "<optional URL path>"}
                    Mount the on-screen Intelligence panel; optional
                    deep-link path like "/dashboard/threats".

  cti_spinup        {}
                    Boot the OpenCTI stack on Railway (~3 min cold start).
                    Use for: "activate / wake / boot / spin up / fire up /
                    start Global Eyes / intel / OpenCTI".
  cti_spindown      {}
                    Tear down the OpenCTI stack to stop the Railway bill.
                    Use for: "stand down / power down / shut down / stop /
                    kill / put away Global Eyes / intel / OpenCTI"."""


_CTI_OPEN_RE = re.compile(
    r"\b(open|show|bring up|pop up|put up|launch|display)\b[^.]*"
    r"\b(?:intel|intelligence|cti|opencti|threat\s+panel|threats?\s+dashboard)\b",
    re.I,
)


async def _route_cti(text: str) -> dict | None:
    """LLM-routed OpenCTI intent. Returns structured command or None.

    Mirrors `_route_desktop()` — same OpenRouter client, same JSON mode,
    same temperature. Separate router prompt because OpenCTI's vocabulary
    (STIX, observables, indicators, kill chain) is wildly different from
    desktop ops and conflating them would muddle both.
    """
    client = _get_router_client()
    if client is None or not text:
        return None
    system = (
        "You translate ONE spoken request into ONE command for the user's "
        "self-hosted OpenCTI intelligence platform. Output strict JSON "
        "only — no prose, no code fences.\n\n"
        "Schema. Either:\n"
        '  {"cti": false}    — when the request is NOT about threat '
        "intelligence / OpenCTI / investigations.\n"
        "Or:\n"
        '  {"cti": true, "command": "<name>", "args": {...}, '
        '"say": "<one short butler-voice sentence>"}\n\n'
        f"{_CTI_COMMANDS_GUIDE}\n\n"
        "Rules:\n"
        "- For 'log foo.com as a suspicious domain' → cti_add_observable "
        '{"value": "foo.com", "observable_type": "domain"}.\n'
        "- For 'open the intel panel' / 'show the threats dashboard' → "
        "cti_open_panel (optionally with a dashboard slug).\n"
        "- For 'what came in today' / 'today's threats' → cti_summary.\n"
        "- `say` is one short sentence, butler tone "
        "('Logged, sir.', 'On it, sir.', 'Searching the intel, sir.').\n"
        '- If the request isn\'t about intel work, output {"cti": false}.'
    )
    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=_ROUTER_MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ],
                temperature=0.0,
                max_tokens=400,
                response_format={"type": "json_object"},
            ),
            timeout=6.0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("cti router LLM failed: %s", exc)
        return None
    raw = (resp.choices[0].message.content or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    try:
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cti router non-JSON: %s — %r", exc, raw[:200])
        return None
    if not isinstance(data, dict) or not data.get("cti"):
        return None
    cmd = (data.get("command") or "").strip()
    if not cmd:
        return None
    return {
        "cmd": cmd,
        "args": data.get("args") or {},
        "say": (data.get("say") or "").strip(),
    }


# OpenCTI observable type vocabulary — same map the server tool uses, kept
# duplicated here so the worker can run without importing the backend.
_OBSERVABLE_TYPE_MAP: dict[str, str] = {
    "domain": "Domain-Name", "domain-name": "Domain-Name",
    "hostname": "Hostname",
    "ip": "IPv4-Addr", "ipv4": "IPv4-Addr",
    "ipv6": "IPv6-Addr",
    "url": "Url",
    "email": "Email-Addr", "email-addr": "Email-Addr",
    "md5": "StixFile", "sha1": "StixFile", "sha256": "StixFile",
    "hash": "StixFile", "file": "StixFile",
    "user-agent": "User-Agent",
    "mutex": "Mutex",
    "registry-key": "Windows-Registry-Key",
}


class OpenCTIClient:
    """Async GraphQL client for a Railway-hosted OpenCTI.

    Path Y (chosen 2026-05-24): OpenCTI lives on Railway with a public
    URL; the worker talks to it via direct httpx. The bridge `http_proxy`
    handler is no longer in the CTI hot path (still useful for any future
    localhost service). The class still accepts a DesktopBridge ref for
    forward-compat with a possible Path Y+Z hybrid, but doesn't use it.

    Env:
      OPENCTI_URL      full Railway URL, e.g.
                       'https://opencti-production-xx.up.railway.app'
      OPENCTI_TOKEN    admin / API token
    """

    def __init__(self, bridge: "DesktopBridge") -> None:
        self._bridge = bridge  # reserved for future hybrid mode
        self._base_url = os.environ.get("OPENCTI_URL", "").rstrip("/")
        self._token = os.environ.get("OPENCTI_TOKEN", "")
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        return self._base_url

    async def _ensure(self) -> httpx.AsyncClient:
        async with self._lock:
            if self._client is not None:
                return self._client
            if not (self._base_url and self._token):
                raise RuntimeError(
                    "OpenCTI unavailable — OPENCTI_URL / OPENCTI_TOKEN "
                    "are not set on the worker"
                )
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
                # Generous connect timeout — a service warming up after
                # cti_spinup may not accept connections for ~30 s; we
                # want the auto-spin-up flow's poll-loop to surface that
                # as a connect-refused, not a longer hang.
                timeout=httpx.Timeout(30.0, connect=8.0),
            )
            logger.info("opencti: client initialised for %s", self._base_url)
            return self._client

    async def _gql(self, query: str, variables: dict | None = None) -> dict:
        client = await self._ensure()
        resp = await client.post(
            "/graphql",
            json={"query": query, "variables": variables or {}},
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data and data["errors"]:
            raise RuntimeError(f"opencti errors: {data['errors'][:2]}")
        return data.get("data") or {}

    # ── Operations ───────────────────────────────────────────────

    async def search(self, query: str, limit: int = 10) -> dict:
        gql = """
        query Search($search: String, $count: Int) {
          stixCoreObjects(search: $search, first: $count) {
            edges {
              node {
                id
                entity_type
                representative { main secondary }
              }
            }
          }
        }
        """
        data = await self._gql(gql, {"search": query, "count": limit})
        edges = (data.get("stixCoreObjects") or {}).get("edges") or []
        return {
            "matches": [
                {
                    "id": (e.get("node") or {}).get("id"),
                    "type": (e.get("node") or {}).get("entity_type"),
                    "name": (
                        ((e.get("node") or {}).get("representative") or {})
                        .get("main")
                        or ""
                    ),
                }
                for e in edges
            ],
            "query": query,
        }

    async def add_observable(self, value: str, raw_type: str) -> dict:
        obs_type = _OBSERVABLE_TYPE_MAP.get(
            (raw_type or "").strip().lower(), raw_type or ""
        )
        if not obs_type:
            raise RuntimeError(f"unknown observable type '{raw_type}'")
        type_to_key: dict[str, str] = {
            "Domain-Name": "DomainName", "Hostname": "Hostname",
            "IPv4-Addr": "IPv4Addr", "IPv6-Addr": "IPv6Addr",
            "Url": "Url", "Email-Addr": "EmailAddr",
            "StixFile": "StixFile", "User-Agent": "UserAgent",
            "Mutex": "Mutex",
            "Windows-Registry-Key": "WindowsRegistryKey",
        }
        key = type_to_key.get(obs_type, obs_type.replace("-", ""))
        if obs_type == "StixFile":
            inner: dict = {
                "name": value,
                "hashes": [{"algorithm": "Unknown", "hash": value}],
            }
        elif obs_type == "Windows-Registry-Key":
            inner = {"attribute_key": value}
        else:
            inner = {"value": value}
        gql = """
        mutation AddObs($input: StixCyberObservableAddInput!) {
          stixCyberObservableAdd(input: $input) {
            id
            observable_value
            entity_type
          }
        }
        """
        data = await self._gql(
            gql, {"input": {"type": obs_type, key: inner}}
        )
        obs = data.get("stixCyberObservableAdd") or {}
        return {
            "id": obs.get("id"),
            "value": obs.get("observable_value") or value,
            "type": obs.get("entity_type") or obs_type,
        }

    async def create_incident(self, name: str, description: str = "") -> dict:
        gql = """
        mutation IncAdd($input: IncidentAddInput!) {
          incidentAdd(input: $input) {
            id
            name
          }
        }
        """
        data = await self._gql(
            gql, {"input": {"name": name, "description": description}}
        )
        inc = data.get("incidentAdd") or {}
        return {"id": inc.get("id"), "name": inc.get("name")}

    async def link(self, from_id: str, to_id: str, rel: str = "related-to") -> dict:
        gql = """
        mutation RelAdd($input: StixCoreRelationshipAddInput!) {
          stixCoreRelationshipAdd(input: $input) {
            id
            relationship_type
          }
        }
        """
        data = await self._gql(
            gql,
            {
                "input": {
                    "fromId": from_id,
                    "toId": to_id,
                    "relationship_type": rel,
                }
            },
        )
        r = data.get("stixCoreRelationshipAdd") or {}
        return {
            "id": r.get("id"),
            "relationship": r.get("relationship_type") or rel,
        }

    async def summary(self, hours: int = 24) -> dict:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).isoformat()
        gql = """
        query Summary($filters: FilterGroup) {
          stixCoreObjects(
            filters: $filters,
            first: 8,
            orderBy: created_at,
            orderMode: desc
          ) {
            pageInfo { globalCount }
            edges {
              node {
                entity_type
                representative { main }
              }
            }
          }
        }
        """
        filters = {
            "mode": "and",
            "filters": [
                {
                    "key": "created_at",
                    "operator": "gt",
                    "values": [cutoff],
                }
            ],
            "filterGroups": [],
        }
        data = await self._gql(gql, {"filters": filters})
        block = data.get("stixCoreObjects") or {}
        total = (block.get("pageInfo") or {}).get("globalCount", 0)
        edges = block.get("edges") or []
        return {
            "window_hours": hours,
            "total": total,
            "recent": [
                {
                    "type": (e.get("node") or {}).get("entity_type"),
                    "name": (
                        ((e.get("node") or {}).get("representative") or {})
                        .get("main", "")
                    ),
                }
                for e in edges
            ],
        }

    async def list_indicators(self, limit: int = 10) -> dict:
        gql = """
        query Inds($count: Int) {
          indicators(
            first: $count, orderBy: created_at, orderMode: desc
          ) {
            edges {
              node {
                id
                name
                x_opencti_score
              }
            }
          }
        }
        """
        data = await self._gql(gql, {"count": limit})
        edges = (data.get("indicators") or {}).get("edges") or []
        return {
            "indicators": [
                {
                    "id": (e.get("node") or {}).get("id"),
                    "name": (e.get("node") or {}).get("name"),
                    "score": (e.get("node") or {}).get("x_opencti_score"),
                }
                for e in edges
            ],
        }

    async def get_observable(self, obs_id: str) -> dict:
        """Fetch the live state of one observable — its score, the
        external references that enrichment connectors have attached,
        and any labels (verdicts) they wrote back."""
        gql = """
        query GetObs($id: String!) {
          stixCyberObservable(id: $id) {
            id
            observable_value
            entity_type
            x_opencti_score
            externalReferences {
              edges {
                node {
                  source_name
                  url
                  description
                }
              }
            }
            objectLabel {
              value
              color
            }
          }
        }
        """
        data = await self._gql(gql, {"id": obs_id})
        obs = data.get("stixCyberObservable") or {}
        refs = (obs.get("externalReferences") or {}).get("edges") or []
        labels = obs.get("objectLabel") or []
        return {
            "id": obs.get("id"),
            "value": obs.get("observable_value"),
            "type": obs.get("entity_type"),
            "score": obs.get("x_opencti_score"),
            "refs": [
                {
                    "source": (e.get("node") or {}).get("source_name"),
                    "url": (e.get("node") or {}).get("url"),
                    "description": (
                        (e.get("node") or {}).get("description") or ""
                    )[:200],
                }
                for e in refs
            ],
            "labels": [
                {"value": lab.get("value"), "color": lab.get("color")}
                for lab in labels
            ],
        }

    async def enrich(
        self, value: str, obs_type: str, deadline_s: float = 30.0
    ) -> dict:
        """Add the observable, then poll for enrichment connector output.

        Enrichment connectors (VirusTotal, AbuseIPDB, Shodan
        InternetDB) fire automatically when an observable is added.
        They write findings back as external references, labels, and
        an aggregate x_opencti_score. We poll get_observable every 5s
        up to ``deadline_s`` and return the first state that has ANY
        enrichment signal (refs OR labels OR a non-null score) — or
        the bare observable if nothing fired in time.
        """
        added = await self.add_observable(value, obs_type)
        obs_id = added.get("id")
        if not obs_id:
            return {"error": "could not create observable", **added}
        end = time.time() + deadline_s
        attempts = 0
        last: dict = {}
        while time.time() < end:
            attempts += 1
            try:
                last = await self.get_observable(obs_id)
            except Exception:  # noqa: BLE001
                pass
            if last.get("refs") or last.get("labels") or last.get("score"):
                last["enrichment_attempts"] = attempts
                return last
            await asyncio.sleep(5.0)
        # No enrichment signal in the window — still return what we have.
        if not last:
            last = added
        last["enrichment_attempts"] = attempts
        last["timed_out"] = True
        return last


def _cti_reply(cmd: str, args: dict, res: dict) -> str:
    """Format an OpenCTI result into a single butler sentence."""
    if not isinstance(res, dict) or res.get("error"):
        err = res.get("error") if isinstance(res, dict) else "no response"
        return f"OpenCTI hiccup, sir — {err}"
    if cmd == "cti_search":
        matches = res.get("matches") or []
        if not matches:
            q = args.get("query", "that")
            return f"Nothing in the intel for '{q}', sir."
        head = ", ".join(
            f"{m.get('name','?')} ({m.get('type','')})"
            for m in matches[:5]
        )
        return f"{len(matches)} hits, sir — {head}."
    if cmd == "cti_add_observable":
        return (
            f"Logged {args.get('value', res.get('value', 'it'))} as a "
            f"{args.get('observable_type', res.get('type', 'observable'))}, sir."
        )
    if cmd == "cti_create_incident":
        return (
            f"Incident '{args.get('name', res.get('name', 'unnamed'))}' "
            "is on the books, sir."
        )
    if cmd == "cti_link":
        rel = args.get("relationship") or res.get("relationship") or "related-to"
        return f"Linked, sir — {rel}."
    if cmd == "cti_summary":
        total = res.get("total", 0)
        hours = res.get("window_hours", args.get("hours", 24))
        if not total:
            return f"Nothing new in the last {hours} hours, sir."
        recent = res.get("recent") or []
        names = ", ".join(
            r.get("name", r.get("type", "?")) for r in recent[:3]
        )
        return (
            f"{total} new objects in the last {hours} hours, sir — "
            f"latest: {names}."
        )
    if cmd == "cti_list_indicators":
        items = res.get("indicators") or []
        if not items:
            return "No recent indicators, sir."
        return (
            f"{len(items)} recent indicators, sir — "
            + ", ".join(i.get("name", "?") for i in items[:4])
            + "."
        )
    if cmd == "cti_enrich":
        value = args.get("value") or res.get("value") or "that"
        refs = res.get("refs") or []
        labels = res.get("labels") or []
        score = res.get("score")
        if not refs and not labels and score is None:
            if res.get("timed_out"):
                return (
                    f"Logged {value}, sir, but no enrichment came back "
                    "in the window — the connectors may be cold."
                )
            return f"Logged {value}, sir. No findings yet."
        bits: list[str] = []
        if score is not None:
            bits.append(f"score {score}")
        # Surface up to 2 distinct sources in the spoken reply.
        sources = []
        for r in refs:
            s = r.get("source") or ""
            if s and s not in sources:
                sources.append(s)
            if len(sources) >= 3:
                break
        if sources:
            bits.append("flagged by " + ", ".join(sources))
        if labels:
            label_words = [lab.get("value", "") for lab in labels[:3]]
            label_words = [w for w in label_words if w]
            if label_words:
                bits.append("labels " + " / ".join(label_words))
        summary = "; ".join(bits) if bits else "no clear verdict"
        return f"{value} — {summary}, sir."
    if cmd == "cti_open_panel":
        return "Intelligence panel is up, sir."
    if cmd == "cti_spinup":
        return (
            "On it, sir — Global Eyes coming online, give me about three "
            "minutes."
        )
    if cmd == "cti_spindown":
        return "Powering down Global Eyes, sir."
    return "Done, sir."


# ── OpenCTI lifecycle (Path Y — Railway on-demand via GitHub Actions) ──
# Voice "activate Global Eyes" fires this workflow with action=start;
# "stand down Global Eyes" or the idle watchdog fires action=stop. The
# workflow itself just runs the existing railway_schedule.py against the
# OpenCTI service list (see Jarvis/.github/workflows/opencti-lifecycle.yml).

# How long the idle watchdog waits after the last successful CTI op
# before tearing down the Railway services. 180 s = 3 minutes (user-set).
_CTI_IDLE_SECONDS = 180


def _is_offline_error(exc: BaseException) -> bool:
    """Heuristic: does this exception look like the OpenCTI platform is
    not currently reachable? Used by the auto-spin-up path to decide
    whether to attempt a transparent recovery instead of speaking the
    error verbatim."""
    if isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
            httpx.PoolTimeout,
        ),
    ):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        # 502 / 503 / 504 from Railway's edge while a container boots.
        return exc.response.status_code in (502, 503, 504)
    msg = str(exc).lower()
    return any(
        tok in msg
        for tok in (
            "connect", "connection refused", "no route",
            "name or service not known", "temporarily unavailable",
            "502", "503", "504",
        )
    )


class GitHubDispatchClient:
    """Fires a workflow_dispatch on the OpenCTI lifecycle workflow.

    Env (set on the worker service):
      GITHUB_DISPATCH_TOKEN     PAT with `repo` + `workflow` scopes
      GITHUB_DISPATCH_OWNER     GitHub user / org (default 'gelson12')
      GITHUB_DISPATCH_REPO      Repo name      (default 'friday_jarvis2')
      GITHUB_DISPATCH_WORKFLOW  Workflow file  (default 'opencti-lifecycle.yml')
      GITHUB_DISPATCH_REF       Branch to run against (default 'main')
    """

    def __init__(self) -> None:
        self._token = os.environ.get("GITHUB_DISPATCH_TOKEN", "")
        self._owner = os.environ.get("GITHUB_DISPATCH_OWNER", "gelson12")
        self._repo = os.environ.get(
            "GITHUB_DISPATCH_REPO", "friday_jarvis2"
        )
        self._workflow = os.environ.get(
            "GITHUB_DISPATCH_WORKFLOW", "opencti-lifecycle.yml"
        )
        self._ref = os.environ.get("GITHUB_DISPATCH_REF", "main")
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> httpx.AsyncClient | None:
        async with self._lock:
            if self._client is not None:
                return self._client
            if not self._token:
                logger.warning(
                    "gh dispatch: GITHUB_DISPATCH_TOKEN not set; OpenCTI "
                    "lifecycle commands will fail until you add it"
                )
                return None
            self._client = httpx.AsyncClient(
                base_url="https://api.github.com",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self._token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=httpx.Timeout(15.0, connect=10.0),
            )
            logger.info(
                "gh dispatch: ready (%s/%s :: %s on %s)",
                self._owner, self._repo, self._workflow, self._ref,
            )
            return self._client

    async def dispatch(self, action: str) -> tuple[bool, str]:
        """Fire `action` (start|stop) → (ok, message)."""
        if action not in ("start", "stop"):
            return False, f"invalid action '{action}'"
        client = await self._ensure()
        if client is None:
            return False, "GITHUB_DISPATCH_TOKEN is not set on the worker"
        try:
            resp = await client.post(
                f"/repos/{self._owner}/{self._repo}/actions/workflows/"
                f"{self._workflow}/dispatches",
                json={"ref": self._ref, "inputs": {"action": action}},
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"gh dispatch network error: {exc}"
        if resp.status_code == 204:
            return True, ""
        return False, (
            f"gh dispatch returned {resp.status_code}: "
            f"{resp.text[:200]}"
        )


class DesktopBridge:
    """Lazy LiveKit connection to the desktop-bridge control room.

    Also a presence index: the bridge processes join the same room with
    identity ``desktop-bridge-<machine>``, so the worker can know exactly
    which user PCs are reachable at any moment without polling.
    """

    def __init__(self) -> None:
        self._room: rtc.Room | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _machine_from_identity(identity: str) -> str | None:
        """Pull the machine label out of a ``desktop-bridge-<m>`` identity."""
        if not identity or not identity.startswith("desktop-bridge-"):
            return None
        return identity[len("desktop-bridge-"):].strip().lower() or None

    def online_machines(self) -> list[str]:
        """Snapshot of the machines whose desktop-bridge is currently in
        the control room. Returns ``[]`` if the worker hasn't joined yet
        (so callers can fall back to "unknown" wording)."""
        if self._room is None:
            return []
        machines: set[str] = set()
        for p in self._room.remote_participants.values():
            m = self._machine_from_identity(getattr(p, "identity", "") or "")
            if m:
                machines.add(m)
        return sorted(machines)

    def is_online(self, machine: str) -> bool:
        m = (machine or "").strip().lower()
        if m in ("", "all", "any"):
            return bool(self.online_machines())
        return m in set(self.online_machines())

    async def _ensure(self) -> rtc.Room | None:
        async with self._lock:
            if self._room is not None:
                return self._room
            url = os.environ.get("LIVEKIT_URL", "")
            key = os.environ.get("LIVEKIT_API_KEY", "")
            secret = os.environ.get("LIVEKIT_API_SECRET", "")
            if not (url and key and secret):
                logger.warning("desktop bridge: LIVEKIT_* env not set")
                return None
            token = (
                api.AccessToken(key, secret)
                .with_identity(f"openjarvis-worker-ctl-{uuid.uuid4().hex[:8]}")
                .with_grants(
                    api.VideoGrants(
                        room_join=True,
                        room=_CONTROL_ROOM,
                        can_publish=True,
                        can_subscribe=True,
                        can_publish_data=True,
                    )
                )
                .to_jwt()
            )
            room = rtc.Room()

            @room.on("data_received")
            def _on_data(packet: rtc.DataPacket) -> None:  # noqa: ANN001
                if packet.topic != _TOPIC_RESULT:
                    return
                try:
                    msg = json.loads(bytes(packet.data).decode("utf-8"))
                except Exception:  # noqa: BLE001
                    return
                fut = self._pending.pop(msg.get("id", ""), None)
                if fut and not fut.done():
                    fut.set_result(msg)

            await room.connect(url, token)
            self._room = room
            logger.info("desktop bridge: connected to '%s'", _CONTROL_ROOM)
            return room

    async def send(
        self, target: str, cmd: str, args: dict, timeout: float = 30.0
    ) -> dict:
        """Send a command to a machine's bridge; return its result dict."""
        room = await self._ensure()
        if room is None:
            return {"error": "desktop bridge unavailable (LIVEKIT_* unset)"}
        cmd_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[cmd_id] = fut
        payload = json.dumps(
            {"id": cmd_id, "target": target, "cmd": cmd, "args": args}
        ).encode("utf-8")
        try:
            await room.local_participant.publish_data(
                payload, reliable=True, topic=_TOPIC_CMD
            )
            msg = await asyncio.wait_for(fut, timeout)
            return msg.get("result", {})
        except asyncio.TimeoutError:
            return {
                "error": f"no response from the '{target}' machine — is its "
                f"desktop-bridge running?"
            }
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}
        finally:
            self._pending.pop(cmd_id, None)


# ── APK build (rebuild mobile-bridge APK via VS-Code-inspiring-cat) ─
# Voice command fires the same /tasks shell pipeline used to produce
# the first APK manually: curl|bash a build script from the
# friday_jarvis2 repo, run it on the inspiring-cat container, publish
# the APK as a GitHub release. ~10-15 min end-to-end.
_APK_BUILD_RE = re.compile(
    r"\b("
    r"(?:build|rebuild|compile|make|create)\s+"
    r"(?:me\s+|us\s+)?(?:the\s+|a\s+|an\s+)?"
    r"(?:mobile[\s-]?bridge|android(?:\s+app)?|apk|phone\s+app)"
    r")\b",
    re.I,
)
# Dedicated mobile-bridge phrasings shortcut around the "which repo?"
# follow-up — they always mean the fixed mobile-bridge module inside
# gelson12/friday_jarvis2.
_APK_MOBILE_BRIDGE_RE = re.compile(
    r"\bmobile[\s-]?bridge\b|\bphone\s+app\b", re.I,
)
# "from <owner>/<repo>" or "from github.com/<owner>/<repo>" — captures
# the repo when the user names one. Owner constrained to GitHub's
# username rules (no dots, no leading dash).
_APK_REPO_RE = re.compile(
    r"\b(?:from|on|out\s+of|using)\s+"
    r"(?:https?://)?(?:github\.com/)?"
    r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}[A-Za-z0-9])?)"
    r"\s*[/ ]\s*"
    r"([A-Za-z0-9][A-Za-z0-9._-]{0,99})",
    re.I,
)
# Parsed verbatim from a follow-up answer to "which repo, sir?".
# Looser than the leading-"from" version so the user can just say
# "gelson12 slash weather-app" or "github.com/foo/bar".
_APK_REPO_BARE_RE = re.compile(
    r"(?:https?://)?(?:github\.com/)?"
    r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}[A-Za-z0-9])?)"
    r"\s*(?:/|\bslash\b|\s)\s*"
    r"([A-Za-z0-9][A-Za-z0-9._-]{0,99})",
    re.I,
)
INSPIRING_CAT_URL = os.environ.get(
    "INSPIRING_CAT_URL",
    "https://inspiring-cat-production.up.railway.app",
).rstrip("/")
MOBILE_BRIDGE_REPO = os.environ.get(
    "MOBILE_BRIDGE_REPO", "gelson12/friday_jarvis2",
)
MOBILE_BRIDGE_BUILD_SCRIPT_URL = os.environ.get(
    "MOBILE_BRIDGE_BUILD_SCRIPT_URL",
    f"https://raw.githubusercontent.com/{MOBILE_BRIDGE_REPO}"
    "/main/scripts/build-mobile-bridge-apk.sh",
)
# Where all voice-built APKs are released: single repo we control,
# single cleanup workflow, single PAT to manage. Source repo can be
# anything; releases always land here.
APK_RELEASE_REPO = os.environ.get(
    "APK_RELEASE_REPO", MOBILE_BRIDGE_REPO,
)


class Assistant(Agent):
    def __init__(self, room: rtc.Room):
        super().__init__(instructions=AGENT_INSTRUCTION)
        self._room = room
        # Desktop control — lazy 2nd LiveKit connection to the PCs' bridge.
        self._desktop = DesktopBridge()
        # OpenCTI — Railway-hosted, reached via direct httpx (Path Y).
        # Lifecycle (spinup/spindown) goes through GitHub Actions
        # workflow_dispatch on the opencti-lifecycle workflow.
        self._cti = OpenCTIClient(self._desktop)
        self._gh = GitHubDispatchClient()
        # Lifecycle state — see _cti_spinup, _cti_idle_watch.
        self._cti_up: bool = False
        self._cti_last_active: float = 0.0
        self._cti_idle_task: asyncio.Task | None = None
        # Last assistant reply (for "repeat that" / "I didn't catch that"
        # clarification handling). Updated by `_remember_say` whenever WE
        # speak; also pulled from turn_ctx as a fallback when the framework
        # spoke (e.g. an LLM-produced reply that bypassed session.say).
        self._last_assistant_text: str = ""
        # Wake/sleep: dormant on connect, woken by "Hey Jarvis".
        self._awake = False
        # Set by the interim-transcript wake listener: the next turn is
        # the one that woke us, so strip the wake word from it.
        self._just_woke = False
        # First-wake greeting includes desktop-bridge presence; cleared
        # after the first wake so subsequent ones just say "Yes, sir?".
        self._announced_status = False
        # Most-recent camera frame from the user's video track (or None).
        self._latest_frame: rtc.VideoFrame | None = None
        self._seen_frame = False
        self._video_tasks: set[asyncio.Task] = set()
        # Live remote-browser widget
        self._browser = None
        self._browser_task: asyncio.Task | None = None
        self._wire_video(room)
        # Elaboration state — set by the SSE listener, cleared on the
        # next user turn (yes/no) or when the deadline passes.
        self._pending_elab_id: str | None = None
        self._pending_elab_deadline: float = 0.0
        self._elab_task: asyncio.Task | None = None
        self._oj_base: str = ""
        self._oj_auth: dict = {}
        # HUD widget inventory pushed from the frontend on jarvis-ui-state.
        # Stays [] until the first publish lands. `_open_widgets_at == 0.0`
        # means "no state ever received" — close-widget priority falls back
        # to permissive mode in that gap.
        self._open_widgets: list[dict] = []
        self._open_widgets_at: float = 0.0
        # Playback state per widget id, reported from the frontend on
        # `widget_playback`. Absent means unknown (treated as
        # possibly-playing). Explicit 'paused'/'stopped'/'ended' wins.
        self._widget_playback_states: dict[str, str] = {}
        # Widget kinds we auto-muted at the top of the current user turn
        # so we can auto-unmute on the next one.
        self._auto_muted_kinds: set[str] = set()
        self._auto_mute_warning_spoken: bool = False
        self._auto_mute_safety_task: asyncio.Task | None = None
        # Unified inventory of open resources (widgets, browser, etc.)
        # with idle-detection + spoken warning before auto-close.
        self._ledger = ResourceLedger()
        # Generic clarification slot for ambiguous intents.
        self._pending_clarification: PendingClarification | None = None
        self._clarification_resumers: dict[str, callable] = {}
        self._clarification_resumers["volume"] = self._resume_volume
        self._clarification_resumers["content"] = self._resume_content
        # APK build is its own one-shot follow-up — a free-form repo
        # answer ("gelson12 slash weather-app") doesn't fit the
        # option-picker shape PendingClarification uses. 30s TTL is
        # enforced inside _maybe_handle_apk_repo_followup.
        self._apk_awaiting_repo: bool = False
        self._apk_awaiting_repo_at: float = 0.0
        self._apk_build_active: bool = False
        # Desktop master volumes we auto-muted at the start of this turn.
        # Mirrors `_auto_muted_kinds` but for OS-level mute on each PC, so
        # we can unmute exactly what we muted at end-of-turn.
        self._auto_muted_machines: set[str] = set()
        # Cache of "is any audio session active on this machine?" so we
        # don't re-enumerate on every turn. (mono time, busy-set).
        self._desktop_audio_busy_cache: tuple[float, set[str]] | None = None
        # Hermes selective router — multi-step queries go to Hermes for the
        # full Tier 1+2 agentic stack (planner, reflector, distiller, goals,
        # tool-sentinel).  Routine turns still hit the OpenJarvis backend.
        # Env-gated by OPENJARVIS_HERMES_ROUTE; off by default.
        self._hermes_router: HermesRouter = HermesRouter()

        # Accommodation booking. Lazy-init: build once on first use so a
        # missing LITEAPI_KEY doesn't crash worker startup. Last-search
        # results are cached for the "book the X" follow-up turn.
        self._accommodation = None  # type: ignore[assignment]
        self._accommodation_init_attempted = False
        self._accommodation_last_results: list = []
        self._accommodation_pending_book: dict | None = None

    def _has_widget(self, kind: str) -> bool:
        """True when a panel of `kind` is currently visible on the HUD."""
        if not kind:
            return False
        target = kind.lower()
        return any(
            (w.get("kind") or "").lower() == target for w in self._open_widgets
        )

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

    # ── Screen widget tools (floating HUD panels) ───────────────────
    async def _publish_ui(self, msg: dict) -> None:
        """Send a UI command to the browser on the `jarvis-ui` topic.

        Also maintains an OPTIMISTIC mirror of `_open_widgets` for every
        `open_widget` / `close_widget` / `close_all` we publish — so the
        worker never has to wait for the frontend's `widget_state` echo
        to know what it just opened. The frontend echo, when it arrives,
        replaces this mirror with the authoritative state.

        Why this is here: the user reported asking to mute a clearly-
        visible YouTube widget and getting "nothing is currently playing"
        because the inventory hadn't synced yet. Optimistic tracking
        closes that race entirely.
        """
        try:
            await self._room.local_participant.publish_data(
                json.dumps(msg).encode("utf-8"),
                reliable=True,
                topic=_UI_TOPIC,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("jarvis-ui publish failed: %s", exc)

        # Local optimistic mirror.
        try:
            mtype = msg.get("type")
            if mtype == "open_widget":
                kind = (msg.get("kind") or "").strip()
                if kind:
                    # Singleton per kind on the frontend side — replace
                    # any prior entry of the same kind.
                    self._open_widgets = [
                        w for w in self._open_widgets
                        if (w.get("kind") or "").lower() != kind.lower()
                    ]
                    self._open_widgets.append({
                        "kind": kind,
                        "id": f"local-{kind}-{uuid.uuid4().hex[:8]}",
                        "title": msg.get("title") or kind,
                        "optimistic": True,
                    })
                    # Don't claim a fresh widget_state sync (that's the
                    # frontend's job) — but stamp non-zero so close-widget
                    # priority knows we have at least optimistic visibility.
                    if self._open_widgets_at == 0.0:
                        self._open_widgets_at = time.monotonic()
            elif mtype == "close_widget":
                kind = (msg.get("kind") or "").strip().lower()
                if kind:
                    self._open_widgets = [
                        w for w in self._open_widgets
                        if (w.get("kind") or "").lower() != kind
                    ]
            elif mtype == "close_all":
                self._open_widgets = []
        except Exception as exc:  # noqa: BLE001
            logger.debug("optimistic widget mirror update failed: %s", exc)

    async def _maybe_handle_widget(self, text: str) -> None:
        """Open/close screen widgets when the user asks.

        Best-effort fallback so panels still work even if the LLM
        doesn't emit a show_widget tool call. Safe to double-fire —
        widgets are singletons on the browser side.
        """
        if not text:
            return
        opening = bool(_UI_OPEN_RE.search(text))
        closing = bool(_UI_CLOSE_RE.search(text))
        if not (opening or closing):
            return
        if closing and re.search(
            r"\b(all|everything|every (widget|panel)|the (widgets|panels))\b",
            text,
            re.I,
        ):
            await self._publish_ui({"type": "close_all"})
            return
        kind = _widget_from_text(text)
        if kind is None:
            return
        if closing and not opening:
            await self._publish_ui({"type": "close_widget", "kind": kind})
        else:
            await self._publish_ui({"type": "open_widget", "kind": kind})

    # ── Close-widget priority ────────────────────────────────────────
    async def _maybe_handle_close_widget(self, text: str) -> bool:
        """Close a HUD panel that the user just named.

        Runs BEFORE _maybe_handle_content so "close the YouTube" closes
        the panel instead of triggering a YouTube search for the word
        "Close." Guarded by the live widget inventory: only intercepts
        when the named widget is actually visible.
        """
        if not text:
            return False
        if not _UI_CLOSE_RE.search(text):
            return False
        if _UI_OPEN_RE.search(text):
            return False
        if re.search(
            r"\b(all|everything|every (widget|panel)|the (widgets|panels))\b",
            text,
            re.I,
        ):
            await self._publish_ui({"type": "close_all"})
            try:
                await self.session.say("Closing all panels, sir.")
            except Exception:  # noqa: BLE001
                pass
            return True
        kind = _widget_from_text(text)
        if kind is None:
            return False
        known_state = self._open_widgets_at > 0.0
        if known_state and not self._has_widget(kind):
            # We KNOW (from a real widget_state sync) that this panel
            # isn't on screen. Tell the user honestly instead of leaving
            # it to the LLM to fabricate "I don't have access" — that
            # was the exact failure mode in the user-reported regression
            # ("close the news window" → "I don't have access").
            try:
                await self.session.say(
                    f"The {kind} panel doesn't seem to be open, sir."
                )
            except Exception:  # noqa: BLE001
                pass
            return True
        # State unknown OR widget is open — fire the close. close_widget
        # is idempotent on the frontend, so an unknown-state close is a
        # safe no-op when nothing's actually there.
        await self._publish_ui({"type": "close_widget", "kind": kind})
        try:
            await self.session.say(f"Closing the {kind} panel, sir.")
        except Exception:  # noqa: BLE001
            pass
        return True

    # ── Bridge lifecycle (status + cold-start) ───────────────────────
    async def _maybe_handle_bridge_lifecycle(self, text: str) -> bool:
        """Answer "are the bridges online?" / "start the bridge on X".

        Runs BEFORE `_maybe_handle_desktop` so "is the laptop online" gets
        a presence answer instead of being routed as a desktop COMMAND.
        Cold-start case is honest about its limits: if the named bridge
        is offline, the worker has no way to start its process from the
        cloud (the bridge is the path), so we tell the user that — no
        fake confirmations.

        Returns True when the turn was handled (caller stops the turn).
        """
        if not text:
            return False
        is_status = bool(_BRIDGE_LIFECYCLE_STATUS_RE.search(text))
        is_start = bool(_BRIDGE_LIFECYCLE_START_RE.search(text))
        if not (is_status or is_start):
            return False

        # Refresh the control-room connection so presence is fresh.
        try:
            await self._desktop._ensure()
        except Exception as exc:  # noqa: BLE001
            logger.warning("bridge-lifecycle: control-room connect failed: %s", exc)

        online = self._desktop.online_machines()
        target = _bridge_lifecycle_target(text)

        # STATUS sub-intent — name what's online and what isn't.
        if is_status and not is_start:
            if target in ("all", "any"):
                if not online:
                    msg = (
                        "No bridges are online right now, sir. The "
                        "scheduled task on the PC should bring it back "
                        "at next logon, or start `run.bat` manually."
                    )
                elif len(online) == 1:
                    msg = f"Only the {online[0]} bridge is online, sir."
                else:
                    msg = (
                        " and ".join(m.capitalize() for m in online)
                        + " are both online, sir."
                    )
            else:
                if self._desktop.is_online(target):
                    msg = f"The {target} bridge is online, sir."
                else:
                    others = [m for m in online if m != target]
                    if others:
                        msg = (
                            f"The {target} bridge is offline, sir — "
                            f"{', '.join(others)} is still up."
                        )
                    else:
                        msg = (
                            f"The {target} bridge is offline, sir, "
                            "and nothing else is online either."
                        )
            try:
                await self.session.say(msg)
            except Exception:  # noqa: BLE001
                pass
            return True

        # START sub-intent — cold-start the bridge process on a PC.
        # The hard constraint: if the bridge is offline, the cloud worker
        # has NO channel to that PC (the bridge IS the channel). So all
        # we can do is:
        #   - confirm if it's already online (idempotent ack)
        #   - explain honestly if it isn't, and point at the boot-start
        #     task that should bring it back without intervention.
        # Wake-on-LAN via mobile-bridge is the planned escape hatch
        # (Phase 0D, deferred); when it lands, this branch will fire it.
        if target in ("all", "any"):
            if not online:
                msg = (
                    "I can't reach any of your PCs right now, sir — the "
                    "bridges aren't online, and the worker has no "
                    "channel to wake them from the cloud. Wake one "
                    "manually, or rely on the boot-start task at next "
                    "logon."
                )
            else:
                msg = (
                    " and ".join(m.capitalize() for m in online)
                    + " are already online, sir."
                )
            try:
                await self.session.say(msg)
            except Exception:  # noqa: BLE001
                pass
            return True

        if self._desktop.is_online(target):
            try:
                await self.session.say(
                    f"The {target} bridge is already online, sir."
                )
            except Exception:  # noqa: BLE001
                pass
            return True

        # Specific machine, offline. Honest refusal.
        try:
            await self.session.say(
                f"I can't reach the {target}, sir — the bridge isn't "
                "online, and from the cloud I have no channel to wake "
                "it. The scheduled task should restart it at next "
                "logon; otherwise launch `run.bat` on the machine."
            )
        except Exception:  # noqa: BLE001
            pass
        return True

    # ── Clarification resolver ───────────────────────────────────────
    async def _maybe_resume_clarification(self, text: str) -> bool:
        """If a clarification is pending, try to resolve `text` to an option."""
        pc = self._pending_clarification
        if pc is None:
            return False
        now = time.monotonic()
        if now >= pc.expires_at:
            logger.info("clarification expired: %s", pc.intent_kind)
            self._pending_clarification = None
            return False
        if not text:
            return False
        if _CANCEL_RE.search(text):
            self._pending_clarification = None
            try:
                await self.session.say("As you wish, sir.")
            except Exception:  # noqa: BLE001
                pass
            return True
        # Free-text clarifications (no option matching). Used by the
        # content router when it gates a noisy topic — the next turn IS
        # the answer, in full sentence form. We pass `text` straight to
        # the resumer, which re-extracts the topic.
        if pc.intent_kind == "content":
            captured = pc
            self._pending_clarification = None
            resumer = self._clarification_resumers.get("content")
            if resumer is not None:
                try:
                    await resumer(text, captured, None)
                except Exception as exc:  # noqa: BLE001
                    logger.error("content resumer failed: %s", exc)
            return True
        low = text.lower()
        chosen: list[dict] = []
        if pc.intent_kind == "volume" and _BOTH_RE.search(text):
            chosen = list(pc.options)
        if not chosen:
            for opt in pc.options:
                words = opt.get("match_words") or []
                for w in words:
                    if re.search(rf"\b{re.escape(w)}\b", low, re.I):
                        chosen = [opt]
                        break
                if chosen:
                    break
        if not chosen:
            for pattern, idx in _ORDINAL_RE:
                if pattern.search(text) and idx < len(pc.options):
                    chosen = [pc.options[idx]]
                    break
        if not chosen:
            logger.info(
                "clarification resume miss for %s: %r", pc.intent_kind, text[:120]
            )
            self._pending_clarification = None
            return False
        resumer = self._clarification_resumers.get(pc.intent_kind)
        if resumer is None:
            logger.warning(
                "no resumer registered for clarification kind %s", pc.intent_kind
            )
            self._pending_clarification = None
            return False
        captured = pc
        self._pending_clarification = None
        try:
            for opt in chosen:
                await resumer(text, captured, opt)
        except Exception as exc:  # noqa: BLE001
            logger.error("clarification resumer for %s failed: %s",
                         captured.intent_kind, exc)
        return True

    # ── Volume disambiguation ────────────────────────────────────────
    async def _maybe_handle_volume(self, text: str) -> bool:
        """Volume command with HUD/desktop disambiguation."""
        if not text or not _VOLUME_INTENT_RE.search(text):
            return False

        low = text.lower()

        # Refusal of the desktop-machine bail when the user clearly named
        # a HUD audio widget. Without this, "unmute the news from the
        # YouTube window" got mis-routed to the desktop handler ("from"
        # used to be a hard exit), which then said "I can't reach the
        # laptop" because no machine was online — the worst kind of
        # wrong answer (volume IS handled, just on the HUD side).
        hud_widget_kw = bool(re.search(
            r"\b("
            r"youtube|the\s+video|video\s+(?:panel|widget|window)|"
            r"music\s+(?:panel|widget|window)|"
            r"browser\s+(?:panel|widget|window)|"
            r"news\s+(?:panel|widget|window)|"
            r"(?:the\s+)?(?:panel|widget|window)"
            r")\b",
            low,
        ))
        if _DESKTOP_MACHINE_RE.search(text) and not hud_widget_kw:
            return False
        if re.search(r"\bunmute\b", low):
            action_args: dict = {"action": "unmute"}
        elif re.search(r"\bmute\b", low):
            action_args = {"action": "mute"}
        elif _VOL_SET.search(low) and re.search(
            r"\b(volume|sound|audio)\b", low
        ):
            level = int(_VOL_SET.search(low).group(1))
            action_args = {"action": "set", "level": max(0, min(100, level))}
        elif _VOL_DOWN.search(low):
            action_args = {"action": "down"}
        elif _VOL_UP.search(low):
            action_args = {"action": "up"}
        else:
            return False

        wants_master = bool(
            re.search(r"\b(master|all\s+sound|everything)\b", low)
        )

        candidates: list[dict] = []

        # HUD audio widgets (visible panels that can produce sound).
        # `news` is included because show_news opens a companion YouTube
        # widget by default — when the user says "mute the news" they mean
        # the audio coming from that companion, not the silent headlines.
        audio_widget_kinds = {"youtube", "music", "browser", "news"}
        # `mute` is a PREVENTIVE action — apply it to anything that
        # could produce sound, even if paused right now. Volume up/down/
        # set still respect playback state (no point boosting silence).
        is_mute_action = action_args.get("action") in ("mute", "unmute")
        for w in self._open_widgets:
            wkind = (w.get("kind") or "").lower()
            if wkind not in audio_widget_kinds:
                continue
            # Respect explicit playback state from the widget. Absent =
            # unknown = treated as possibly-playing (the prior default).
            wid = w.get("id") or ""
            pstate = self._widget_playback_states.get(wid)
            if not is_mute_action and pstate in ("paused", "stopped", "ended"):
                continue
            title = (w.get("title") or wkind).strip() or wkind
            match_words = {wkind, title.lower()}
            if wkind == "youtube":
                match_words.update({"youtube", "video", "the video", "panel"})
                # The companion YouTube widget that `show_news` opens has
                # title prefixed "News Video — …". If we see that, also
                # match on news/headlines so "mute the news" routes here.
                if title.lower().startswith("news video"):
                    match_words.update({"news", "headlines", "the news"})
            elif wkind == "music":
                match_words.update({"music", "the music", "panel"})
            elif wkind == "browser":
                match_words.update({"browser", "the browser", "panel"})
            elif wkind == "news":
                # The headlines panel itself is silent; the audio comes
                # from the companion YouTube widget (matched separately).
                # But the user says "mute the news" so we still want this
                # candidate to appear in the disambiguation list.
                match_words.update({"news", "headlines", "the news", "panel"})
            candidates.append({
                "label": title if wkind != "youtube" else "YouTube",
                "target_kind": "widget",
                "widget_kind": wkind,
                "match_words": sorted(match_words),
            })

        # Per-app audio sessions on each online bridge (parallel).
        try:
            await self._desktop._ensure()
        except Exception as exc:  # noqa: BLE001
            logger.warning("control-room connect failed: %s", exc)
        machines = self._desktop.online_machines()
        if machines:
            async def _enum(m: str) -> tuple[str, dict]:
                try:
                    res = await self._desktop.send(
                        m, "audio_sessions", {}, timeout=4.0
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("audio_sessions on %s failed: %s", m, exc)
                    res = {"sessions": []}
                return m, res

            try:
                results = await asyncio.gather(
                    *(_enum(m) for m in machines), return_exceptions=False
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("audio enumeration failed: %s", exc)
                results = []

            for machine, res in results:
                for s in (res.get("sessions") or []):
                    if not s.get("is_active"):
                        continue
                    proc = (s.get("process_name") or "").strip()
                    if not proc:
                        continue
                    label = (s.get("display_name") or proc).strip() or proc
                    match_words = set(_derive_match_words(proc))
                    match_words.add(label.lower())
                    candidates.append({
                        "label": label,
                        "target_kind": "session",
                        "machine": machine,
                        "process_name": proc,
                        "match_words": sorted(w for w in match_words if w),
                    })

        if wants_master and machines:
            for machine in machines:
                candidates.append({
                    "label": f"the {machine} master volume",
                    "target_kind": "master",
                    "machine": machine,
                    "match_words": ["master", "everything", "all"],
                })

        if not candidates:
            try:
                await self.session.say("Nothing is currently playing, sir.")
            except Exception:  # noqa: BLE001
                pass
            return True

        if len(candidates) == 1:
            await self._dispatch_volume(candidates[0], action_args)
            return True

        shown = candidates[:3]
        labels = [c["label"] for c in shown]
        if len(candidates) > 3:
            tail = f", {labels[-1]}, or another one"
            prompt = ", ".join(labels[:-1]) + tail + ", sir?"
        elif len(labels) == 3:
            prompt = f"{labels[0]}, {labels[1]}, or {labels[2]}, sir?"
        else:
            prompt = f"{labels[0]} or {labels[1]}, sir?"
        self._pending_clarification = PendingClarification(
            intent_kind="volume",
            options=candidates,
            original_args=action_args,
            prompt=prompt,
            created_at=time.monotonic(),
            expires_at=time.monotonic() + 30.0,
        )
        try:
            await self.session.say(prompt)
        except Exception:  # noqa: BLE001
            pass
        return True

    async def _dispatch_volume(self, option: dict, action_args: dict) -> None:
        """Apply a parsed volume action to one resolved option."""
        target_kind = option.get("target_kind")
        action = action_args.get("action") or ""
        if target_kind == "widget":
            msg: dict = {
                "type": "widget_volume",
                "kind": option["widget_kind"],
                "action": action if action in ("mute", "unmute", "set") else "set",
            }
            if action == "set":
                msg["level"] = int(action_args.get("level") or 50)
            elif action == "up":
                msg["level"] = 100
            elif action == "down":
                msg["level"] = 25
            await self._publish_ui(msg)
        elif target_kind == "session":
            await self._desktop.send(
                option["machine"], "app_volume",
                {"process_name": option["process_name"], **action_args},
                timeout=8.0,
            )
        elif target_kind == "master":
            await self._desktop.send(
                option["machine"], "volume", action_args, timeout=8.0,
            )
        else:
            logger.warning("dispatch_volume: unknown target_kind %r", target_kind)
            return

        verb_map = {
            "mute": "Muting",
            "unmute": "Unmuting",
            "up": "Turning up",
            "down": "Lowering",
            "set": "Setting",
        }
        verb = verb_map.get(action, "Adjusting")
        try:
            await self.session.say(f"{verb} {option['label']}, sir.")
        except Exception:  # noqa: BLE001
            pass

    async def _resume_volume(
        self, text: str, pc: PendingClarification, option: dict
    ) -> None:
        """Finish a deferred volume action once an option is chosen."""
        await self._dispatch_volume(option, pc.original_args)

    # ── Content-intent clarification (free-text resumer) ──────────────
    async def _ask_content_clarification(
        self, kind: str, arg: str, original_text: str
    ) -> bool:
        """Park a noisy content intent and ask the user to rephrase.

        Returns True so the caller stops the turn; the next user utterance
        is resolved by `_resume_content` and re-dispatched cleanly.
        """
        category = {
            "news": "the news", "youtube": "a video",
            "web": "the web", "maps": "the map", "browser": "the browser",
        }.get(kind, "that")
        if arg.strip():
            prompt = (
                f"I didn't quite catch that, sir. What would you like "
                f"on {category}?"
            )
        else:
            prompt = f"What would you like on {category}, sir?"
        self._pending_clarification = PendingClarification(
            intent_kind="content",
            options=[],
            original_args={
                "kind": kind, "original_arg": arg, "raw": original_text,
            },
            prompt=prompt,
            created_at=time.monotonic(),
            expires_at=time.monotonic() + 30.0,
        )
        try:
            await self.session.say(prompt)
        except Exception:  # noqa: BLE001
            pass
        return True

    async def _resume_content(
        self, text: str, pc: PendingClarification, option: dict | None
    ) -> None:
        """Re-dispatch a deferred content intent with the user's clarified topic.

        `text` is the user's free-form reply (a topic phrase). We strip
        filler and feed it back into the original kind's flow. If the
        clarified topic STILL looks noisy, we give up rather than loop —
        the user can always re-ask.
        """
        kind = pc.original_args.get("kind") or ""
        new_topic = _clean_query(text or "")
        # Drop a leading "it's", "the news on", "about" — common in replies.
        new_topic = re.sub(
            r"^(?:it'?s\s+|the\s+|on\s+|about\s+|regarding\s+|news\s+on\s+|"
            r"video\s+(?:of|about|on)\s+)+",
            "", new_topic, flags=re.I,
        ).strip(" ,.?!-\"'")
        if not new_topic and kind not in ("news", "browser"):
            try:
                await self.session.say(
                    "Still didn't catch a topic, sir — try again when you're ready."
                )
            except Exception:  # noqa: BLE001
                pass
            return
        if _topic_looks_noisy(new_topic):
            try:
                await self.session.say(
                    "I'm still not sure I follow, sir. Let's pick this up again."
                )
            except Exception:  # noqa: BLE001
                pass
            return
        dispatch = {
            "web": lambda: self.web_search(new_topic),
            "youtube": lambda: self.search_youtube(new_topic),
            "news": lambda: self.show_news(new_topic),
            "maps": lambda: self.show_map(new_topic),
            "browser": lambda: self.open_browser(
                new_topic or "https://www.google.com"
            ),
        }
        fn = dispatch.get(kind)
        if fn is None:
            return
        try:
            reply = await asyncio.wait_for(fn(), timeout=20.0)
        except asyncio.TimeoutError:
            reply = "That's taking too long, sir — try again in a moment."
        except Exception as exc:  # noqa: BLE001
            logger.error("content resume '%s' failed: %s", kind, exc)
            reply = "I couldn't complete that just now, sir."
        try:
            await self.session.say(reply)
        except Exception:  # noqa: BLE001
            pass

    async def _maybe_handle_music(self, text: str) -> bool:
        """Handle "play music" / "play some music" / "music please" with
        explicit disambiguation. Without this handler, the request falls
        through to the LLM which says "I cannot play music" — useless.

        Logic:
          - If a laptop bridge is online and a music app (Spotify, etc.) is
            already running there, ask: "On your laptop or via YouTube?"
          - Otherwise offer the YouTube fallback: "I can search YouTube for
            the music of your choice — what would you like?"
          - If the user already specified an artist/song in the same turn
            ("play Pink Floyd"), open the YouTube widget directly.
        """
        if not text:
            return False
        low = text.lower()
        # Trigger: a play verb + music noun. "play me a song" / "music
        # please" / "could you play music".
        play_verb = re.search(r"\b(play|put on|throw on|queue|start)\b", low)
        music_noun = re.search(
            r"\b(music|song|songs|track|tune|tunes|playlist|album|"
            r"some\s+music|bit\s+of\s+music)\b", low,
        )
        if not (play_verb and music_noun):
            return False
        # If the user explicitly said "on my laptop/pc/rog", let the
        # desktop router handle it (Spotify on the bridge).
        if _DESKTOP_MACHINE_RE.search(text):
            return False
        # Extract any artist/song after "play"
        m = re.search(
            r"\bplay\s+(?:me\s+|some\s+)?(.+?)(?:\s+please|\s*\?|\s*$)", low,
        )
        specific = ""
        if m:
            specific = m.group(1).strip()
            # Strip the music-noun if it's the whole match ("play music")
            specific = re.sub(
                r"\b(music|song|songs|track|tune|tunes|playlist|album|"
                r"some\s+music|bit\s+of\s+music)\b",
                "", specific, flags=re.I,
            ).strip()
        if specific and len(specific) >= 3:
            # User named something specific — open YouTube widget directly
            try:
                await self.search_youtube(specific)
            except Exception as exc:  # noqa: BLE001
                logger.error("music search_youtube failed: %s", exc)
                try:
                    await self.session.say(
                        "I couldn't reach YouTube just now, sir."
                    )
                except Exception:  # noqa: BLE001
                    pass
            return True
        # No specific track — disambiguate based on laptop presence
        try:
            await self._desktop._ensure()
        except Exception:  # noqa: BLE001
            pass
        machines = []
        try:
            machines = self._desktop.online_machines() or []
        except Exception:  # noqa: BLE001
            pass
        if machines:
            reply = (
                f"Would you like the music on your {machines[0]}, sir, or "
                "would you prefer I open YouTube here on the HUD?"
            )
        else:
            reply = (
                "Your laptop bridge isn't connected, sir. Shall I open "
                "YouTube here and search for some music of your choice? "
                "Just tell me an artist or genre."
            )
        try:
            await self.session.say(reply)
        except Exception:  # noqa: BLE001
            pass
        return True

    async def _maybe_handle_content(self, text: str) -> bool:
        """Handle search / video / news / maps / browser intents by regex.

        Returns True when an intent was handled — the caller then stops
        the turn, since we speak our own confirmation. This mirrors the
        web_search / search_youtube / show_news / show_map / open_browser
        methods, which never fire as LLM tools on an OpenJarvis backend.
        """
        intent = _content_intent(text)
        if intent is None:
            return False
        kind, arg = intent

        # Coherence gate — refuse to dispatch on a garbled topic. The
        # user just saw their news search return Elton John because the
        # transcript was junk ("a little bit of delay, fix that, also,
        # , the"); a conscious AGI asks instead of guessing.
        # Empty arg is allowed for news/browser (= top of category).
        needs_topic = kind in ("youtube", "web", "maps")
        topic_required_but_missing = needs_topic and not arg.strip()
        if topic_required_but_missing or _topic_looks_noisy(arg):
            return await self._ask_content_clarification(kind, arg, text)

        dispatch = {
            "web": lambda: self.web_search(arg),
            "youtube": lambda: self.search_youtube(arg),
            "news": lambda: self.show_news(arg),
            "maps": lambda: self.show_map(arg),
            "browser": lambda: self.open_browser(
                arg or "https://www.google.com"
            ),
        }
        if kind not in dispatch:
            return False
        try:
            # Bound the search/browser call so a hung provider can never
            # freeze the whole voice turn.
            reply = await asyncio.wait_for(dispatch[kind](), timeout=20.0)
        except asyncio.TimeoutError:
            logger.error("content intent '%s' timed out", kind)
            reply = "That's taking too long, sir — try again in a moment."
        except Exception as exc:  # noqa: BLE001
            logger.error("content intent '%s' failed: %s", kind, exc)
            reply = "I couldn't complete that just now, sir."
        try:
            await self.session.say(reply)
        except Exception:  # noqa: BLE001
            pass
        return True

    # ── Accommodation booking ────────────────────────────────────────
    def _accommodation_service(self):
        """Lazy-init the accommodation service. Returns None when LITEAPI_KEY
        (or any other configured provider env) is missing — callers speak a
        graceful "not configured" reply."""
        if self._accommodation is not None:
            return self._accommodation
        if self._accommodation_init_attempted or not _ACCOMMODATION_AVAILABLE:
            return None
        self._accommodation_init_attempted = True
        try:
            self._accommodation = AccommodationService.from_env()
        except Exception as exc:  # noqa: BLE001
            logger.warning("accommodation init failed: %s", exc)
            self._accommodation = None
        return self._accommodation

    async def _handle_accommodation_search(self, text: str) -> bool:
        service = self._accommodation_service()
        if service is None:
            try:
                await self.session.say("Accommodation isn't configured, sir.")
            except Exception:  # noqa: BLE001
                pass
            return True
        location = _accommodation_nlu.parse_location(text)
        if not location:
            try:
                await self.session.say(
                    "Where would you like to stay, sir? Tell me a city or area."
                )
            except Exception:  # noqa: BLE001
                pass
            return True
        check_in, check_out = _accommodation_nlu.parse_dates(text)
        guests = _accommodation_nlu.parse_guests(text)
        preferred = _accommodation_nlu.parse_provider_preference(text)
        query = _AccommodationSearchQuery(
            location=location,
            check_in=check_in,
            check_out=check_out,
            guests=guests,
            currency=os.environ.get("ACCOMMODATION_DEFAULT_CURRENCY", "GBP"),
            preferred_providers=preferred,
        )
        try:
            properties = await asyncio.wait_for(service.search(query, limit=12), timeout=20.0)
        except asyncio.TimeoutError:
            try:
                await self.session.say("The search took too long, sir — try again in a moment.")
            except Exception:  # noqa: BLE001
                pass
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("accommodation search failed: %s", exc)
            try:
                await self.session.say("I couldn't reach the booking system just now, sir.")
            except Exception:  # noqa: BLE001
                pass
            return True
        self._accommodation_last_results = properties
        # Surface the carousel on the HUD.
        widget_payload = {
            "query": location,
            "check_in": check_in.isoformat(),
            "check_out": check_out.isoformat(),
            "properties": [
                {
                    "provider_id": p.provider_id,
                    "external_id": p.external_id,
                    "name": p.name,
                    "price_total": p.price_total,
                    "price_currency": p.price_currency,
                    "rating": p.rating,
                    "review_count": p.review_count,
                    "address": p.address,
                    "images": p.images[:3],
                    "lat": p.lat,
                    "lng": p.lng,
                }
                for p in properties
            ],
        }
        await self._publish_ui({
            "type": "open_widget",
            "kind": "accommodation",
            "title": f"Stays in {location}",
            "payload": widget_payload,
        })
        if not properties:
            try:
                await self.session.say(
                    f"I couldn't find any properties in {location} for those dates, sir."
                )
            except Exception:  # noqa: BLE001
                pass
            return True
        cheapest = properties[0]
        nights = (check_out - check_in).days
        try:
            await self.session.say(
                f"Found {len(properties)} properties in {location}, sir. "
                f"The {cheapest.name} is cheapest at {cheapest.price_total:.0f} "
                f"{cheapest.price_currency} total for {nights} nights. "
                f"Say 'book the {cheapest.name.split()[0]}' to reserve."
            )
        except Exception:  # noqa: BLE001
            pass
        return True

    async def _handle_accommodation_book_start(self, text: str) -> bool:
        """Phase 2: locks in a quote and asks the user to confirm. The actual
        booking only fires once the user replies "yes" — handled in
        `_maybe_resume_accommodation_book` on the next turn."""
        service = self._accommodation_service()
        if service is None or not self._accommodation_last_results:
            try:
                await self.session.say(
                    "I don't have any properties to book, sir — search for somewhere first."
                )
            except Exception:  # noqa: BLE001
                pass
            return True
        # Match the spoken name against last results: longest distinct token wins.
        text_lower = text.lower()
        target = None
        best_overlap = 0
        for prop in self._accommodation_last_results:
            tokens = [t for t in prop.name.lower().split() if len(t) > 3]
            overlap = sum(1 for tok in tokens if tok in text_lower)
            if overlap > best_overlap:
                best_overlap = overlap
                target = prop
        if target is None:
            target = self._accommodation_last_results[0]
        first_name = os.environ.get("ACCOMMODATION_GUEST_FIRST_NAME", "").strip()
        last_name = os.environ.get("ACCOMMODATION_GUEST_LAST_NAME", "").strip()
        email = os.environ.get("ACCOMMODATION_GUEST_EMAIL", "").strip()
        if not (first_name and last_name and email):
            try:
                await self.session.say(
                    "Booking isn't fully set up, sir — your guest details are missing. "
                    "Set the ACCOMMODATION_GUEST_* env vars on the worker."
                )
            except Exception:  # noqa: BLE001
                pass
            return True
        # Apify Airbnb is read-only — there's no quote to lock. Skip straight
        # to the redirect-confirmation prompt.
        is_redirect = target.extras.get("is_redirect_provider", False) if hasattr(target, "extras") else False
        if is_redirect:
            self._accommodation_pending_book = {
                "target": target,
                "quote": None,
                "is_redirect": True,
                "created_at": time.time(),
            }
            try:
                await self.session.say(
                    f"That's an Airbnb listing — I can open it on your phone so you "
                    f"finish the booking on Airbnb itself. Shall I send the link, sir?"
                )
            except Exception:  # noqa: BLE001
                pass
            return True
        try:
            quote = await asyncio.wait_for(service.quote(target), timeout=15.0)
        except Exception as exc:  # noqa: BLE001
            logger.error("accommodation quote failed: %s", exc)
            try:
                await self.session.say("I couldn't lock in the price just now, sir.")
            except Exception:  # noqa: BLE001
                pass
            return True
        self._accommodation_pending_book = {
            "target": target,
            "quote": quote,
            "is_redirect": False,
            "created_at": time.time(),
        }
        nights_text = ""
        try:
            # Best-effort: pull nights from the property if exposed by the search
            # cache; otherwise just speak the total.
            pass
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.session.say(
                f"Locked in {quote.price_total:.0f} {quote.price_currency} total at "
                f"the {target.name}. {quote.cancellation_policy[:120]} "
                f"Confirm, sir?"
            )
        except Exception:  # noqa: BLE001
            pass
        return True

    async def _finalize_pending_book(self) -> bool:
        """User said yes — execute the booking with the stored quote."""
        pending = self._accommodation_pending_book
        if not pending:
            return False
        self._accommodation_pending_book = None
        service = self._accommodation_service()
        if service is None:
            return True
        target = pending["target"]
        is_redirect = pending.get("is_redirect", False)
        quote = pending.get("quote")
        # For redirect providers (Apify Airbnb), the book_token IS the listing URL.
        book_token = (quote.book_token if quote else target.book_token)
        request = _AccommodationBookingRequest(
            quote_id=(quote.quote_id if quote else target.book_token),
            book_token=book_token,
            guest_first_name=os.environ.get("ACCOMMODATION_GUEST_FIRST_NAME", "").strip(),
            guest_last_name=os.environ.get("ACCOMMODATION_GUEST_LAST_NAME", "").strip(),
            guest_email=os.environ.get("ACCOMMODATION_GUEST_EMAIL", "").strip(),
        )
        try:
            result = await asyncio.wait_for(
                service.book(
                    request,
                    property_name=target.name,
                    provider_id=target.provider_id,
                ),
                timeout=25.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("accommodation book failed: %s", exc)
            try:
                await self.session.say("The booking failed, sir. Please try again.")
            except Exception:  # noqa: BLE001
                pass
            return True
        if not result.success or not result.checkout_url:
            try:
                await self.session.say(
                    "The provider didn't return a payment link, sir — booking aborted."
                )
            except Exception:  # noqa: BLE001
                pass
            return True
        if not service.telegram.configured:
            await self._publish_ui({
                "type": "open_widget",
                "kind": "accommodation",
                "title": "Complete on Airbnb" if is_redirect else "Complete payment",
                "payload": {
                    "query": target.name,
                    "checkout_url": result.checkout_url,
                    "price_total": result.price_total,
                    "price_currency": result.price_currency,
                },
            })
        try:
            if is_redirect:
                await self.session.say(
                    f"Sent the Airbnb listing to your phone, sir. "
                    f"Tap it to complete the booking on Airbnb."
                )
            else:
                await self.session.say(
                    f"Booking link sent to your phone, sir. "
                    f"Total {result.price_total:.0f} {result.price_currency}. "
                    f"Tap to complete payment securely."
                )
        except Exception:  # noqa: BLE001
            pass
        return True

    async def _maybe_resume_accommodation_book(self, text: str) -> bool:
        """Yes/no resume for the pending booking confirmation. Runs BEFORE
        the content router so a bare "yes" doesn't fall through to search."""
        pending = self._accommodation_pending_book
        if not pending:
            return False
        # TTL: a forgotten quote shouldn't book on a much-later "yes".
        if time.time() - pending["created_at"] > _ACCOMMODATION_PENDING_TTL_S:
            self._accommodation_pending_book = None
            return False
        if _ACCOMMODATION_NO_RE.search(text or ""):
            self._accommodation_pending_book = None
            try:
                await self.session.say("Cancelled, sir.")
            except Exception:  # noqa: BLE001
                pass
            return True
        if _ACCOMMODATION_YES_RE.search(text or ""):
            return await self._finalize_pending_book()
        # User said something else — leave pending in place, let TTL expire.
        return False

    async def _maybe_handle_accommodation(self, text: str) -> bool:
        """Top-level dispatch for accommodation intents. Cascade-only — when
        OPENJARVIS_VOICE_PROVIDER=realtime, regex handlers are skipped, so
        this never fires (documented gap)."""
        if not _ACCOMMODATION_RE.search(text or ""):
            return False
        if _ACCOMMODATION_BOOK_RE.search(text) and self._accommodation_last_results:
            return await self._handle_accommodation_book_start(text)
        return await self._handle_accommodation_search(text)

    async def _wake_greeting(self) -> str:
        """Greeting spoken when the user wakes Jarvis with no follow-up.

        On the FIRST wake of a session we name the desktop bridges that
        are currently online, so the user immediately knows which of
        their machines Jarvis can drive (rather than discovering it the
        hard way mid-command).
        """
        if self._announced_status:
            return "Yes, sir?"
        self._announced_status = True
        # Eagerly connect to the control room so presence is accurate.
        try:
            await self._desktop._ensure()
        except Exception as exc:  # noqa: BLE001
            logger.warning("control-room connect failed: %s", exc)
        online = self._desktop.online_machines()
        if not online:
            return (
                "At your service, sir. Note — no desktop bridges are "
                "online; start desktop-bridge\\run.bat on the laptop or "
                "the ROG if you need me to operate them."
            )
        if len(online) == 1:
            return (
                f"At your service, sir. Your {online[0]} is online "
                "and ready."
            )
        return (
            "At your service, sir. "
            + " and ".join(online).capitalize()
            + " are both online."
        )

    async def _maybe_handle_cti(self, text: str) -> bool:
        """Operate OpenCTI via the LLM intent router.

        Mirrors `_maybe_handle_desktop`: broad keyword gate → LLM router
        → OpenCTI GraphQL call → spoken result. Returns True when the
        turn was handled (caller stops the turn).
        """
        if not text or not _CTI_HINT_RE.search(text):
            return False

        # Fast path — "open the intel panel" doesn't need an LLM call,
        # just mount the widget. Cheaper and zero-latency.
        if _CTI_OPEN_RE.search(text):
            await self._open_cti_panel()
            try:
                await self.session.say("Intelligence panel is up, sir.")
            except Exception:  # noqa: BLE001
                pass
            return True

        routed = await _route_cti(text)
        if routed is None:
            return False
        cmd = routed["cmd"]
        args = routed["args"] or {}
        say_hint = routed.get("say", "")

        # Quick acknowledgement for ops that might take a moment.
        info_cmds = {
            "cti_search", "cti_summary", "cti_list_indicators", "cti_enrich"
        }
        if say_hint and cmd not in info_cmds:
            try:
                await self.session.say(say_hint)
            except Exception:  # noqa: BLE001
                pass

        # Lifecycle commands — no auto-spinup wrapper (they ARE the
        # lifecycle). Spinup also spawns the idle watchdog.
        if cmd == "cti_spinup":
            await self._cti_spinup()
            return True
        if cmd == "cti_spindown":
            await self._cti_spindown()
            return True

        # Dispatch — wrapped so auto-spinup can recover from "offline"
        # exceptions transparently on the user's first request.
        async def _run() -> dict:
            if cmd == "cti_search":
                return await self._cti.search(
                    str(args.get("query", "")),
                    int(args.get("limit") or 10),
                )
            if cmd == "cti_add_observable":
                return await self._cti.add_observable(
                    str(args.get("value", "")),
                    str(args.get("observable_type", "")),
                )
            if cmd == "cti_create_incident":
                return await self._cti.create_incident(
                    str(args.get("name", "")),
                    str(args.get("description", "")),
                )
            if cmd == "cti_link":
                return await self._cti.link(
                    str(args.get("from_id", "")),
                    str(args.get("to_id", "")),
                    str(args.get("relationship") or "related-to"),
                )
            if cmd == "cti_summary":
                return await self._cti.summary(
                    int(args.get("hours") or 24)
                )
            if cmd == "cti_list_indicators":
                return await self._cti.list_indicators(
                    int(args.get("limit") or 10)
                )
            if cmd == "cti_enrich":
                return await self._cti.enrich(
                    str(args.get("value", "")),
                    str(args.get("observable_type", "")),
                )
            if cmd == "cti_open_panel":
                await self._open_cti_panel(
                    dashboard=args.get("dashboard"),
                    path=args.get("path"),
                )
                return {"opened": True}
            return {"error": f"unknown cti command '{cmd}'"}

        res: dict
        try:
            res = await _run()
        except Exception as exc:  # noqa: BLE001
            # Auto-spinup: detect "service is offline" exceptions and
            # bring OpenCTI up, then retry the original op once.
            if _is_offline_error(exc):
                logger.info(
                    "cti %s hit offline error (%s) — auto-spinup",
                    cmd, exc,
                )
                try:
                    await self.session.say(
                        "Bringing Global Eyes online first, sir — one moment."
                    )
                except Exception:  # noqa: BLE001
                    pass
                up = await self._cti_spinup(quiet=True)
                if up:
                    try:
                        res = await _run()
                    except Exception as exc2:  # noqa: BLE001
                        logger.error(
                            "cti %s retry after auto-spinup failed: %s",
                            cmd, exc2,
                        )
                        res = {"error": f"retry after spinup failed: {exc2}"}
                else:
                    res = {
                        "error": "auto-spinup did not complete in time"
                    }
            else:
                logger.error("cti %s failed: %s", cmd, exc)
                res = {"error": str(exc)}

        # Stamp activity on success so the idle watchdog starts the
        # clock from the most recent real use.
        if isinstance(res, dict) and not res.get("error"):
            self._cti_last_active = time.time()

        try:
            await self.session.say(_cti_reply(cmd, args, res))
        except Exception:  # noqa: BLE001
            pass
        return True

    async def _open_cti_panel(
        self, dashboard: str | None = None, path: str | None = None
    ) -> None:
        """Publish an `open_widget` for the cti panel on the jarvis-ui topic."""
        payload: dict = {}
        cti_url = os.environ.get("OPENCTI_URL", "").rstrip("/")
        if cti_url:
            payload["url"] = cti_url
        if path:
            payload["path"] = path
        if dashboard:
            payload["dashboard"] = dashboard
        await self._publish_ui(
            {
                "type": "open_widget",
                "kind": "cti",
                "title": "Intelligence",
                "payload": payload,
            }
        )

    # ── CTI lifecycle (Path Y on-demand) ────────────────────────────

    async def _cti_spinup(self, quiet: bool = False) -> bool:
        """Trigger the OpenCTI lifecycle workflow with action=start, then
        poll until healthy. ``quiet`` skips the ack speech (used by the
        auto-spinup path which already spoke its own line).

        Returns True when the platform answered a GraphQL ping within
        the deadline, False otherwise.
        """
        ok, why = await self._gh.dispatch("start")
        if not ok:
            try:
                await self.session.say(
                    f"I couldn't kick off the spin-up, sir — {why}"
                )
            except Exception:  # noqa: BLE001
                pass
            return False

        if not quiet:
            try:
                await self.session.say(
                    "On it, sir — Global Eyes coming online, give me "
                    "about three minutes."
                )
            except Exception:  # noqa: BLE001
                pass

        healthy = await self._wait_until_cti_healthy()
        if healthy:
            self._cti_up = True
            self._cti_last_active = time.time()
            # Spawn the idle watchdog if it isn't already running.
            if (
                self._cti_idle_task is None
                or self._cti_idle_task.done()
            ):
                self._cti_idle_task = asyncio.create_task(
                    self._cti_idle_watch()
                )
            try:
                if not quiet:
                    await self.session.say("Global Eyes are online, sir.")
                await self._open_cti_panel()
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                await self.session.say(
                    "Global Eyes didn't come up in time, sir — check the "
                    "Railway dashboard."
                )
            except Exception:  # noqa: BLE001
                pass
        return healthy

    async def _cti_spindown(self) -> None:
        """Trigger the OpenCTI lifecycle workflow with action=stop and
        clean up local state. Idempotent."""
        # Mark down immediately so the watchdog loop exits this tick.
        self._cti_up = False
        if self._cti_idle_task is not None:
            self._cti_idle_task.cancel()
            self._cti_idle_task = None
        ok, why = await self._gh.dispatch("stop")
        if not ok:
            try:
                await self.session.say(
                    f"Couldn't fire the spin-down, sir — {why}"
                )
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            await self._publish_ui(
                {"type": "close_widget", "kind": "cti"}
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.session.say("Powering down Global Eyes, sir.")
        except Exception:  # noqa: BLE001
            pass

    async def _wait_until_cti_healthy(
        self, deadline_s: float = 420.0
    ) -> bool:
        """Poll OpenCTI's GraphQL ping every 10 s up to ``deadline_s``.
        Returns True on the first success, False on deadline."""
        end = time.time() + deadline_s
        attempts = 0
        while time.time() < end:
            attempts += 1
            try:
                # Tiny GraphQL ping — succeeds the moment the platform
                # is serving requests and the admin token is accepted.
                await self._cti._gql("{ me { name } }")
                logger.info(
                    "cti healthy after %d probes (%.1fs)",
                    attempts, deadline_s - (end - time.time()),
                )
                return True
            except Exception as exc:  # noqa: BLE001
                logger.debug("cti probe %d not yet healthy: %s", attempts, exc)
            await asyncio.sleep(10.0)
        return False

    async def _cti_idle_watch(self) -> None:
        """Background watchdog: if no CTI activity for `_CTI_IDLE_SECONDS`
        seconds while the platform is up, announce + spin it down."""
        try:
            while self._cti_up:
                await asyncio.sleep(30.0)
                if not self._cti_up:
                    return
                idle = time.time() - self._cti_last_active
                if idle >= _CTI_IDLE_SECONDS:
                    minutes = max(1, int(_CTI_IDLE_SECONDS // 60))
                    word = "minute" if minutes == 1 else "minutes"
                    try:
                        await self.session.say(
                            f"Global Eyes has been idle for over "
                            f"{minutes} {word}, sir — powering down to "
                            "save computational resources."
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    await self._cti_spindown()
                    return
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.error("cti idle watch crashed: %s", exc)

    async def _maybe_handle_desktop(self, text: str) -> bool:
        """Operate the user's Windows machines via the LLM intent router.

        Architecture:
          1. Broad keyword gate — cheap test for "could be desktop". This
             filters out 90%+ of turns so we don't spend an LLM call.
          2. LLM router — converts free-form speech into a structured
             bridge command. Handles arbitrary phrasings ('in my pc',
             'increase memory', 'play that Pink Floyd track'…).
          3. Regex fallback — kept for when OPENROUTER_API_KEY is unset or
             the router times out, so the feature still works.
          4. Presence check — if the target machine's bridge isn't in the
             control room we say so honestly, never pretend.

        Returns True when handled (caller raises StopResponse).
        """
        if not text or not _DESKTOP_HINT_RE.search(text):
            return False

        # Eagerly join the control room so presence is known before we
        # decide what to say. Safe to call repeatedly — `_ensure` is locked.
        try:
            await self._desktop._ensure()
        except Exception as exc:  # noqa: BLE001
            logger.warning("control-room connect failed: %s", exc)

        online = self._desktop.online_machines()

        routed = await _route_desktop(text, online)
        say_hint = ""
        if routed is not None:
            machine, cmd, args = routed["machine"], routed["cmd"], routed["args"]
            say_hint = routed.get("say", "")
        else:
            intent = _desktop_intent(text)
            if intent is None:
                return False
            machine, cmd, args = intent

        # Honest offline message — never pretend we have access we don't.
        if machine != "all" and not self._desktop.is_online(machine):
            msg = (
                f"Your {machine}'s desktop-bridge isn't connected, sir — "
                "start desktop-bridge\\run.bat on that machine."
            )
            if online:
                msg += f" Online right now: {', '.join(online)}."
            try:
                await self.session.say(msg)
            except Exception:  # noqa: BLE001
                pass
            return True

        # Speak the router's quick acknowledgement immediately so the user
        # hears progress while a long op (search, system_status) runs.
        # Skip for chatty info commands — _desktop_reply produces the
        # better, data-rich reply for those.
        info_cmds = {
            "list_dir", "read_file", "system_status", "list_processes",
            "search_files", "host_info",
        }
        if say_hint and cmd not in info_cmds:
            try:
                await self.session.say(say_hint)
            except Exception:  # noqa: BLE001
                pass

        timeout = 70.0 if cmd == "shell" else 30.0
        try:
            res = await self._desktop.send(
                machine, cmd, args, timeout=timeout
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("desktop send failed: %s", exc)
            res = {"error": str(exc)}

        reply = _desktop_reply(machine, cmd, args, res)
        try:
            await self.session.say(reply)
        except Exception:  # noqa: BLE001
            pass
        return True

    async def _maybe_handle_apk_build(self, text: str) -> bool:
        """Voice-triggered Android APK build via inspiring-cat.

        Two flavours share this handler:

        1. Dedicated mobile-bridge ("rebuild the mobile bridge" /
           "compile the phone app") → always builds the mobile-bridge
           module in gelson12/friday_jarvis2; legacy tag prefix
           `mobile-bridge-v0.1.0-` (the auto-delete workflow already
           catches it).

        2. Generic ("build the apk from <owner>/<repo>") → clones the
           named GitHub repo, builds the root Android project, releases
           under `voice-apk-<owner>-<repo>-<timestamp>` on
           gelson12/friday_jarvis2 (single release host = single
           cleanup workflow). If the user didn't name a repo, we set a
           one-shot follow-up flag and ask "which repo, sir?" — the
           next turn is consumed by _maybe_handle_apk_repo_followup.

        Guarded so a double-trigger doesn't fire two builds in parallel.
        """
        if not text or not _APK_BUILD_RE.search(text):
            return False
        if getattr(self, "_apk_build_active", False):
            try:
                await self.session.say(
                    "A build is already running, sir — I'll let you "
                    "know when it's done."
                )
            except Exception:  # noqa: BLE001
                pass
            return True

        # Dedicated mobile-bridge shortcut — never asks for a repo.
        if _APK_MOBILE_BRIDGE_RE.search(text):
            await self._launch_apk_build(
                source_repo=MOBILE_BRIDGE_REPO,
                module_dir="mobile-bridge",
                tag_prefix="mobile-bridge-v0.1.0-",
                speak_name="mobile-bridge APK",
            )
            return True

        # Generic flow — try to extract a github owner/repo from the
        # utterance ("build the apk from gelson12/weather-app").
        m = _APK_REPO_RE.search(text)
        if m:
            owner, repo = m.group(1), m.group(2)
            await self._launch_generic_apk_build(owner, repo)
            return True

        # No repo named — ask back and consume the next user turn.
        self._apk_awaiting_repo = True
        self._apk_awaiting_repo_at = time.monotonic()
        try:
            await self.session.say(
                "Which repo, sir? I need owner slash name — for example, "
                "gelson12 slash weather-app."
            )
        except Exception:  # noqa: BLE001
            pass
        return True

    async def _maybe_handle_apk_repo_followup(self, text: str) -> bool:
        """One-shot follow-up consumer for "which repo, sir?".

        Runs BEFORE every other handler so a free-form "owner/repo"
        reply doesn't get routed into the content matcher. 30s TTL;
        cancellable with "never mind".
        """
        if not getattr(self, "_apk_awaiting_repo", False):
            return False
        if time.monotonic() - getattr(self, "_apk_awaiting_repo_at", 0.0) > 30:
            self._apk_awaiting_repo = False
            return False
        if not text:
            return False
        if _CANCEL_RE.search(text):
            self._apk_awaiting_repo = False
            try:
                await self.session.say("As you wish, sir.")
            except Exception:  # noqa: BLE001
                pass
            return True
        m = _APK_REPO_BARE_RE.search(text)
        if not m:
            logger.info("apk repo follow-up miss: %r", text[:120])
            try:
                await self.session.say(
                    "I didn't catch the repo, sir. Try again with "
                    "owner slash name."
                )
            except Exception:  # noqa: BLE001
                pass
            return True
        self._apk_awaiting_repo = False
        owner, repo = m.group(1), m.group(2)
        await self._launch_generic_apk_build(owner, repo)
        return True

    async def _launch_generic_apk_build(self, owner: str, repo: str) -> None:
        """Speak the ack + spawn the build worker for an arbitrary repo."""
        slug = re.sub(r"[^a-z0-9]+", "-", f"{owner}-{repo}".lower()).strip("-")
        tag_prefix = f"voice-apk-{slug}-"
        await self._launch_apk_build(
            source_repo=f"{owner}/{repo}",
            module_dir="",
            tag_prefix=tag_prefix,
            speak_name=f"APK from {owner} slash {repo}",
        )

    async def _launch_apk_build(
        self,
        *,
        source_repo: str,
        module_dir: str,
        tag_prefix: str,
        speak_name: str,
    ) -> None:
        """Set the active flag, speak the ack, and spawn the worker."""
        self._apk_build_active = True
        try:
            await self.session.say(
                f"Building the {speak_name} on inspiring-cat, sir — "
                "this takes about 10 to 15 minutes. I'll announce the "
                "download link when it's ready."
            )
        except Exception:  # noqa: BLE001
            pass
        asyncio.create_task(self._apk_build_worker(
            source_repo=source_repo,
            module_dir=module_dir,
            tag_prefix=tag_prefix,
        ))

    async def _latest_apk_tag(self, tag_prefix: str) -> str | None:
        """Return the latest release tag on APK_RELEASE_REPO matching prefix."""
        url = f"https://api.github.com/repos/{APK_RELEASE_REPO}/releases"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    url, headers={"Accept": "application/vnd.github+json"}
                )
                resp.raise_for_status()
                for rel in resp.json():
                    tag = rel.get("tag_name") or ""
                    if tag.startswith(tag_prefix):
                        return tag
        except Exception as exc:  # noqa: BLE001
            logger.warning("apk-tag fetch failed: %s", exc)
        return None

    async def _apk_build_worker(
        self,
        *,
        source_repo: str,
        module_dir: str,
        tag_prefix: str,
    ) -> None:
        """Background task: submit build, poll for release, speak URL."""
        try:
            before = await self._latest_apk_tag(tag_prefix)
            logger.info(
                "apk build: starting source=%s module=%r prefix=%s before=%s",
                source_repo, module_dir, tag_prefix, before,
            )

            owner, _, name = source_repo.partition("/")
            source_url = f"https://github.com/{source_repo}.git"
            # The build script reads these env vars. APK_MODULE_DIR is
            # empty for repos whose Android project is at the root.
            env_exports = (
                f"export SOURCE_REPO_URL='{source_url}' "
                f"REPO_OWNER='{owner}' REPO_NAME='{name}' "
                f"APK_MODULE_DIR='{module_dir}' "
                f"TAG_PREFIX='{tag_prefix}' "
                f"RELEASE_REPO='{APK_RELEASE_REPO}';"
            )
            cmd = (
                f"nohup bash -c '{env_exports} curl -fsSL "
                f"{MOBILE_BRIDGE_BUILD_SCRIPT_URL} | bash "
                f"> /tmp/mb-build.log 2>&1' "
                f"> /tmp/mb-launcher.log 2>&1 < /dev/null & "
                f"disown $!; echo \"launched pid=$!\""
            )
            payload = {
                "type": "shell",
                "payload": {"command": cmd, "cwd": "/workspace"},
            }
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.post(
                    f"{INSPIRING_CAT_URL}/tasks", json=payload
                )
                r.raise_for_status()
                launch_resp = r.json()
            logger.info("apk build launcher accepted: %s", launch_resp)

            deadline = time.monotonic() + 25 * 60
            new_tag: str | None = None
            while time.monotonic() < deadline:
                await asyncio.sleep(30)
                tag = await self._latest_apk_tag(tag_prefix)
                if tag and tag != before:
                    new_tag = tag
                    break

            if not new_tag:
                try:
                    await self.session.say(
                        "The APK build didn't finish within 25 minutes, "
                        "sir — check the inspiring-cat log."
                    )
                except Exception:  # noqa: BLE001
                    pass
                return

            release_url = (
                f"https://github.com/{APK_RELEASE_REPO}"
                f"/releases/tag/{new_tag}"
            )
            try:
                short = new_tag[len(tag_prefix):] or "build"
                await self.session.say(
                    f"Your APK is ready, sir — {short}. "
                    "The release page is on the panel. "
                    "It will be deleted in 24 hours."
                )
            except Exception:  # noqa: BLE001
                pass
            try:
                await self._publish_ui({
                    "type": "open_widget",
                    "kind": "site",
                    "title": f"APK: {new_tag}",
                    "payload": {
                        "url": release_url,
                        "prompt": f"APK build from {source_repo}",
                    },
                })
            except Exception as exc:  # noqa: BLE001
                logger.warning("apk site widget open failed: %s", exc)

        except Exception as exc:  # noqa: BLE001
            logger.error("apk build worker failed: %s", exc)
            try:
                await self.session.say(
                    "The APK build hit an error, sir — "
                    "check the inspiring-cat logs."
                )
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._apk_build_active = False

    async def show_widget(self, widget: str, title: str = "") -> str:
        """Display a floating widget panel on the user's JARVIS screen.

        Use this when the user asks to open, show, or bring up a panel.

        Args:
            widget: Which panel — one of "chat", "clock", "music",
                "search", "news", "youtube", "maps", "apps", "system".
            title: Optional custom header text for the panel.
        """
        kind = (widget or "").strip().lower()
        valid = {
            "chat", "clock", "music", "search", "news",
            "youtube", "maps", "browser", "apps", "system",
        }
        if kind not in valid:
            return (
                f"There is no '{widget}' widget. Available panels: "
                + ", ".join(sorted(valid))
            )
        msg: dict = {"type": "open_widget", "kind": kind}
        if title:
            msg["title"] = title
        await self._publish_ui(msg)
        return f"Displayed the {kind} widget on screen."

    async def hide_widget(self, widget: str = "") -> str:
        """Close a floating widget on the user's JARVIS screen.

        Args:
            widget: Which panel to close. Pass "all" (or leave it
                empty) to clear every widget from the screen.
        """
        kind = (widget or "").strip().lower()
        if kind in ("", "all", "everything"):
            await self._publish_ui({"type": "close_all"})
            return "Cleared all widgets from the screen."
        await self._publish_ui({"type": "close_widget", "kind": kind})
        return f"Closed the {kind} widget."

    # ── Search & content tools (web, video, news, maps) ─────────────
    async def web_search(self, query: str) -> str:
        """Search the web and show the results on the JARVIS screen.

        Use this when the user asks to search for, google, or look up
        something on the web.

        Args:
            query: What to search the web for.
        """
        results = await search_tools.web_search(query, limit=6)
        await self._publish_ui(
            {
                "type": "open_widget",
                "kind": "search",
                "title": f"Search — {query}",
                "payload": {"query": query, "results": results},
            }
        )
        if not results:
            return f"I couldn't find anything for '{query}', sir."
        return f"I found {len(results)} results for '{query}', now on screen."

    async def search_youtube(self, query: str) -> str:
        """Search YouTube and show the videos on the JARVIS screen.

        Args:
            query: What videos to search for.
        """
        videos = await search_tools.youtube_search(query, limit=8)
        await self._publish_ui(
            {
                "type": "open_widget",
                "kind": "youtube",
                "title": f"YouTube — {query}",
                "payload": {"query": query, "videos": videos},
            }
        )
        if not videos:
            return f"No videos found for '{query}', sir."
        return (
            f"I found {len(videos)} videos for '{query}' — "
            "the first is ready to play."
        )

    async def show_news(self, topic: str = "") -> str:
        """Show current news headlines on the JARVIS screen, and (when a
        related video can be derived from the actual headlines) open a
        news-themed YouTube panel alongside.

        Args:
            topic: Optional subject to focus the news on (e.g.
                "technology"). Leave empty for the top headlines.

        Why this is shaped the way it is:
          Previously this fetched YouTube with the raw user `topic` (or
          "breaking news today" on empty), which routinely returned
          unrelated content — a noisy STT turn would search YouTube for
          "delay, fix that" and Elton John's catalogue would appear next
          to the news panel. We now (a) skip YouTube entirely if there
          are no real headlines, and (b) derive the YouTube query from
          the top headline's TITLE, so the companion video is actually
          on-topic.
        """
        topic = (topic or "").strip()
        articles = await search_tools.news_search(topic, limit=8)

        # News widget up front so the user has something to read while
        # we (maybe) fetch a related video.
        await self._publish_ui(
            {
                "type": "open_widget",
                "kind": "news",
                "title": f"News — {topic}" if topic else "Top Headlines",
                "payload": {"query": topic, "articles": articles},
            }
        )

        if not articles:
            return "I couldn't reach the news feed just now, sir."

        # Derive a SAFE YouTube query from the top headline. Strip
        # source suffix (" - Reuters"), pipe-separated subtitle, and
        # truncate to the first 8 meaningful words — real headlines
        # routinely run 80-130 chars, which is far too specific for
        # YouTube (returns 0 results) AND used to trip our noise
        # heuristic (intended for spoken topics, not headline strings)
        # so we fell off the companion-video path entirely.
        top = articles[0] if articles else {}
        top_title = (top.get("title") or "").strip()
        top_source = (top.get("source") or "").strip()
        video_query = re.sub(r"\s+-\s+[A-Z][A-Za-z0-9 .&'-]+$", "", top_title)
        video_query = re.sub(r"\s+\|.*$", "", video_query).strip()
        # Drop punctuation that YouTube treats as nothing useful.
        video_query_clean = re.sub(r"[\"'`]", "", video_query)
        # First N words is the right granularity for YouTube — the
        # core noun phrase is almost always within the first 6-8 words,
        # and over-specifying just kills recall.
        video_query_short = " ".join(video_query_clean.split()[:8])

        # We deliberately DO NOT apply `_topic_looks_noisy` here —
        # headlines are inherently long and that heuristic would
        # reject every legitimate one. Just gate on having SOMETHING
        # substantive to search (>= 12 chars).
        if len(video_query_short) >= 12:
            try:
                videos = await asyncio.wait_for(
                    search_tools.youtube_search(video_query_short, limit=6),
                    timeout=6.0,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("news-companion youtube fetch failed: %s", exc)
                videos = []
            if videos:
                # Open the companion YouTube widget immediately, in
                # parallel with the headlines panel. The previous 4-second
                # sleep was a UX delay the user explicitly asked to remove.
                try:
                    await self._publish_ui(
                        {
                            "type": "open_widget",
                            "kind": "youtube",
                            "title": f"News Video — {video_query_short[:50]}",
                            "payload": {
                                "query": video_query_short,
                                "videos": videos,
                            },
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "news-companion video open failed: %s", exc
                    )

        where = f" on {topic}" if topic else ""
        if top_title:
            lead = f"Top headline{where}: {top_title}"
            lead += f", from {top_source}." if top_source else "."
        else:
            lead = f"Here are the latest headlines{where}, sir."
        return lead

    async def show_map(self, place: str) -> str:
        """Show a place, address, or directions on a map on the JARVIS
        screen.

        Args:
            place: A place or address (e.g. "Tower Bridge, London"), or
                a directions query (e.g. "London to Oxford").
        """
        await self._publish_ui(
            {
                "type": "open_widget",
                "kind": "maps",
                "title": f"Maps — {place}",
                "payload": {"query": place},
            }
        )
        return f"Showing {place} on the map, sir."

    # ── Live remote-browser widget ──────────────────────────────────
    async def _publish_browser(self, msg: dict) -> None:
        """Publish a frame chunk on the `jarvis-browser` data topic."""
        try:
            await self._room.local_participant.publish_data(
                json.dumps(msg).encode("utf-8"),
                reliable=True,
                topic="jarvis-browser",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("jarvis-browser publish failed: %s", exc)

    async def _push_browser_frame(self) -> None:
        """Screenshot the live page and stream it in ~12 KB chunks."""
        if self._browser is None:
            return
        img = await self._browser.screenshot()
        if img is None:
            return
        b64 = base64.b64encode(img).decode()
        frame_id = uuid.uuid4().hex[:8]
        size = 12000
        total = max(1, (len(b64) + size - 1) // size)
        for seq in range(total):
            msg: dict = {
                "t": "frame",
                "id": frame_id,
                "seq": seq,
                "total": total,
                "data": b64[seq * size : (seq + 1) * size],
            }
            if seq == 0:
                msg["url"] = self._browser.url
            await self._publish_browser(msg)

    async def _browser_stream_loop(self) -> None:
        """Refresh the streamed frame while the browser widget is open."""
        try:
            await asyncio.sleep(0.4)  # let the widget mount + subscribe
            while self._browser is not None:
                await self._push_browser_frame()
                await asyncio.sleep(1.5)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.error("browser stream loop failed: %s", exc)

    async def _stop_browser(self) -> None:
        """Cancel streaming and close the Playwright page, if any."""
        task, self._browser_task = self._browser_task, None
        if task is not None:
            task.cancel()
        browser, self._browser = self._browser, None
        if browser is not None:
            await browser.close()
        # Remove from the ledger (idempotent if a ledger close already
        # triggered us — re-entry into close() finds nothing and exits).
        try:
            await self._ledger.close("browser", "main")
        except Exception:  # noqa: BLE001
            pass

    async def handle_browser_event(self, msg: dict) -> None:
        """Apply a relayed interaction from the browser widget."""
        if self._browser is None:
            return
        # Any interaction counts as activity for the idle watcher.
        await self._ledger.touch("browser", "main")
        action = msg.get("action")
        if action == "click":
            await self._browser.click(
                float(msg.get("x", 0.0)), float(msg.get("y", 0.0))
            )
        elif action == "scroll":
            await self._browser.scroll(float(msg.get("dy", 0.0)))
        elif action == "navigate":
            await self._browser.navigate(str(msg.get("url", "")))
        elif action == "back":
            await self._browser.back()
        elif action == "reload":
            await self._browser.reload()
        elif action == "key":
            await self._browser.key(str(msg.get("key", "")))
        elif action == "close":
            await self._stop_browser()
            return
        else:
            return
        await self._push_browser_frame()

    async def open_browser(self, url: str = "https://www.google.com") -> str:
        """Open a live, interactive web browser on the JARVIS screen.

        It is a real Chromium page — the user can click links, scroll,
        type, and enter a new address in the widget itself.

        Args:
            url: The page to open. Defaults to Google.
        """
        from browser_view import BrowserSession

        await self._stop_browser()
        browser = BrowserSession()
        try:
            await browser.open(url)
        except Exception as exc:  # noqa: BLE001
            return f"I couldn't open the browser, sir: {exc}"
        self._browser = browser
        # Register with the ledger so "what's open?" lists it and the
        # idle watcher will close it after 10 minutes of no interaction.
        await self._ledger.open(
            "browser",
            "main",
            f"the browser ({url})",
            idle_threshold_s=600.0,
            on_close=self._stop_browser,
        )
        await self._publish_ui(
            {
                "type": "open_widget",
                "kind": "browser",
                "title": "Browser",
                "payload": {"loading": True},
            }
        )
        self._browser_task = asyncio.create_task(self._browser_stream_loop())
        return f"The browser is open, sir — loading {url}."

    # ── Cross-talk auto-mute ─────────────────────────────────────────
    async def _check_desktop_audio_busy(self) -> set[str]:
        """Which online desktop machines have any active audio session?

        Bounded enumeration (1.5 s per machine, in parallel), cached for
        20 s so we don't pay this on every turn. Returns the SET of
        machine names that are currently producing sound — these are the
        ones we'll mute pre-emptively when the user speaks.
        """
        cache = self._desktop_audio_busy_cache
        now = time.monotonic()
        if cache is not None and now - cache[0] < 20.0:
            return set(cache[1])
        try:
            await self._desktop._ensure()
        except Exception:  # noqa: BLE001
            return set()
        machines = self._desktop.online_machines()
        if not machines:
            self._desktop_audio_busy_cache = (now, set())
            return set()

        async def _busy(m: str) -> str | None:
            try:
                res = await self._desktop.send(
                    m, "audio_sessions", {}, timeout=1.5
                )
            except Exception:  # noqa: BLE001
                return None
            for s in (res.get("sessions") or []):
                if s.get("is_active"):
                    return m
            return None

        try:
            results = await asyncio.gather(
                *(_busy(m) for m in machines), return_exceptions=False
            )
        except Exception:  # noqa: BLE001
            results = []
        busy = {m for m in results if m}
        self._desktop_audio_busy_cache = (now, busy)
        return busy

    async def _maybe_auto_unmute(self) -> None:
        """Un-mute anything we auto-muted at the start of the previous turn."""
        if not self._auto_muted_kinds and not self._auto_muted_machines:
            return
        kinds = sorted(self._auto_muted_kinds)
        machines = sorted(self._auto_muted_machines)
        self._auto_muted_kinds.clear()
        self._auto_muted_machines.clear()
        task = self._auto_mute_safety_task
        if task is not None:
            self._auto_mute_safety_task = None
            task.cancel()
        for kind in kinds:
            try:
                await self._room.local_participant.publish_data(
                    json.dumps({
                        "type": "widget_volume",
                        "kind": kind,
                        "action": "unmute",
                    }).encode(),
                    reliable=True,
                    topic="jarvis-ui",
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("auto-unmute widget publish failed: %s", exc)
        # Unmute the desktop masters we muted on this turn.
        for m in machines:
            try:
                await self._desktop.send(
                    m, "volume", {"action": "unmute"}, timeout=3.0,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("auto-unmute desktop send failed: %s", exc)

    async def _maybe_auto_mute(self) -> None:
        """Mute any actively-playing source so the user can be heard.

        Covers two distinct audio surfaces:
          1. HUD widgets currently playing (YouTube panel, music panel,
             browser widget) — muted via the `widget_volume` data topic.
          2. Per-machine OS master volume on each online desktop bridge
             that has at least one active audio session (Spotify, Chrome
             tab outside our HUD, anything making sound).

        Spoken warning fires once per session — silent on subsequent
        turns. Both surfaces are restored by `_maybe_auto_unmute` on the
        next user turn, or by the 60 s safety task if the user falls
        silent.
        """
        if self._auto_muted_kinds or self._auto_muted_machines:
            return  # already muted this turn cycle

        # ── Surface 1: HUD widgets ──────────────────────────────────
        playing_ids = {
            wid for wid, st in self._widget_playback_states.items()
            if st == "playing"
        }
        kinds: set[str] = set()
        for w in self._open_widgets:
            if (w.get("id") or "") in playing_ids:
                k = (w.get("kind") or "").lower()
                if k:
                    kinds.add(k)

        # ── Surface 2: external (PC master) ─────────────────────────
        busy_machines = await self._check_desktop_audio_busy()

        if not kinds and not busy_machines:
            return

        # Mute HUD widgets.
        for kind in sorted(kinds):
            try:
                await self._room.local_participant.publish_data(
                    json.dumps({
                        "type": "widget_volume",
                        "kind": kind,
                        "action": "mute",
                        "reason": "user_speaking",
                    }).encode(),
                    reliable=True,
                    topic="jarvis-ui",
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("auto-mute widget publish failed: %s", exc)

        # Mute desktop masters in parallel.
        async def _mute_machine(m: str) -> None:
            try:
                await self._desktop.send(
                    m, "volume", {"action": "mute"}, timeout=3.0,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("auto-mute desktop send failed: %s", exc)

        if busy_machines:
            await asyncio.gather(
                *(_mute_machine(m) for m in sorted(busy_machines)),
                return_exceptions=False,
            )

        self._auto_muted_kinds = kinds
        self._auto_muted_machines = busy_machines

        # 60 s safety unmute: if the user falls silent and never starts
        # another turn, don't strand audio muted forever.
        if self._auto_mute_safety_task is None:
            async def _safety() -> None:
                try:
                    await asyncio.sleep(60.0)
                except asyncio.CancelledError:
                    return
                await self._maybe_auto_unmute()
            self._auto_mute_safety_task = asyncio.create_task(_safety())

        if not self._auto_mute_warning_spoken:
            self._auto_mute_warning_spoken = True
            try:
                await self.session.say(
                    "I'll mute that while you speak, sir."
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("auto-mute warning failed: %s", exc)

    # ── Resource-ledger voice tools ──────────────────────────────────
    @function_tool
    async def list_open(self) -> str:
        """List everything currently open — widgets, the live browser,
        any tracked resource — along with how long each has been idle.

        Use when the user asks 'what's open?' / 'what do I have open' /
        'list everything open'.
        """
        items = await self._ledger.list_open()
        if not items:
            return "Nothing is open, sir."
        now = time.monotonic()
        parts: list[str] = []
        for r in sorted(items, key=lambda x: x.opened_at):
            idle = now - r.last_activity_at
            mins = int(idle // 60)
            if mins < 1:
                idle_str = "just now"
            elif mins == 1:
                idle_str = "1 minute idle"
            else:
                idle_str = f"{mins} minutes idle"
            parts.append(f"{r.label} ({idle_str})")
        if len(parts) == 1:
            return f"You have {parts[0]} open, sir."
        return f"Open: {', '.join(parts[:-1])}, and {parts[-1]}, sir."

    @function_tool
    async def close_idle(self) -> str:
        """Close every tracked resource that is currently past its idle
        threshold — no warning, no grace. The background watcher
        otherwise warns and waits 10 seconds before closing on its own.
        """
        items = await self._ledger.list_open()
        now = time.monotonic()
        closed: list[str] = []
        for r in items:
            if r.idle_threshold_s is None:
                continue
            if (now - r.last_activity_at) < r.idle_threshold_s:
                continue
            if await self._ledger.close(r.kind, r.id):
                closed.append(r.label)
        if not closed:
            return "Nothing is idle, sir."
        if len(closed) == 1:
            return f"Closed {closed[0]}, sir."
        return f"Closed {', '.join(closed[:-1])}, and {closed[-1]}, sir."

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        """Intercept commands before the LLM — and never die silently.

        Any unexpected error speaks a short apology instead of dropping
        the turn with no audio at all (a cause of 'Jarvis went quiet').
        ``StopResponse`` is the normal control-flow signal, so it is
        re-raised untouched.
        """
        try:
            await self._handle_turn(turn_ctx, new_message)
        except StopResponse:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("turn handling failed: %s", exc)
            try:
                await self.session.say(
                    "Apologies, sir — something went wrong there."
                )
            except Exception:  # noqa: BLE001
                pass
            raise StopResponse()

    async def _handle_turn(self, turn_ctx, new_message) -> None:
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
        if self._just_woke:
            # The interim-transcript listener already woke us mid-utterance
            # — answer whatever followed the wake word.
            self._just_woke = False
            rest = _WAKE_RE.sub("", text).strip(" ,.!?-")
            if rest:
                # Update local `text` so downstream regex handlers see the
                # clean query. Deliberately DO NOT mutate
                # new_message.content here — livekit-agents fires
                # preemptive LLM generation on the transcribed text before
                # on_user_turn_completed completes, and mutating the
                # message invalidates the speculative result, adding
                # whole-RTT latency to every wake turn. Modern LLMs cope
                # with a leading "Hey Jarvis," prefix fine.
                text = rest
            else:
                await self.session.say(await self._wake_greeting())
                raise StopResponse()
        elif not self._awake:
            # Interim listener missed it — last-chance check on the final
            # transcript.
            if _WAKE_RE.search(text):
                self._awake = True
                rest = _WAKE_RE.sub("", text).strip(" ,.!?-")
                if rest:
                    # Same as above — update local text but don't mutate
                    # new_message.content; preserves preemptive generation.
                    text = rest
                else:
                    await self.session.say(await self._wake_greeting())
                    raise StopResponse()
            else:
                raise StopResponse()  # dormant — ignore non-wake speech
        else:
            if _SLEEP_RE.search(text):
                self._awake = False
                await self.session.say("Goodbye, sir.")
                raise StopResponse()

        # ── Repeat-last clarification (BEFORE Hermes + all intents) ───────
        # If the user signals they didn't catch the prior reply, re-speak
        # what we just said. Regex covers the common phrasings (~90% of
        # cases) instantly without an LLM call. For phrasings the regex
        # misses, the AGENT_INSTRUCTION system prompt now explicitly tells
        # the LLM how to handle them gracefully — so the long tail still
        # gets handled, just via the (slightly slower) LLM path.
        if _REPEAT_LAST_RE.search(text):
            last = self._last_assistant_text.strip()
            if not last:
                last = _last_assistant_from_ctx(turn_ctx)
            if last:
                # Cap length so very long prior replies don't sound silly
                snippet = last if len(last) <= 280 else (last[:270] + "…")
                try:
                    await self.session.say(f"Of course, sir — {snippet}")
                    self._last_assistant_text = snippet
                except Exception:  # noqa: BLE001
                    pass
            else:
                try:
                    await self.session.say(
                        "Apologies sir, I don't have a prior reply to repeat. "
                        "What would you like me to do?"
                    )
                except Exception:  # noqa: BLE001
                    pass
            raise StopResponse()

        # Hermes selective routing — multi-step queries go to Hermes for the
        # full Tier 1+2 agentic stack (planner decomposes into subtasks,
        # reflector critiques after, goals injected from vault, etc.).
        # Routine turns fall through to the normal OpenJarvis backend below,
        # which keeps its fast cadence and existing intent handlers intact.
        # Env-gated by OPENJARVIS_HERMES_ROUTE=selective.  Failure to reach
        # Hermes → fall through silently (no degradation).
        if _hermes_route_enabled() and _hermes_should_route(text):
            answer = await self._hermes_router.call(
                user_text=text,
                session_id=getattr(self._room, "name", "") or "openjarvis-voice",
            )
            if answer:
                await self.session.say(answer)
                raise StopResponse()
            # else: silent fall-through to OpenJarvis backend path.

        # Pending clarification — resolve before anything else. If we
        # asked a disambiguation question last turn, this turn is the
        # answer; resolves to a target and dispatches the deferred action.
        if await self._maybe_resume_clarification(text):
            raise StopResponse()

        # APK build repo follow-up — consume "owner/repo" answer when
        # we just asked "which repo, sir?". Runs immediately after the
        # generic clarification slot so a free-form reply never falls
        # through to the content matcher.
        if await self._maybe_handle_apk_repo_followup(text):
            raise StopResponse()

        # Camera on/off — highly specific, runs FIRST among voice intents
        # so phrases like "turn on the camera" never get swallowed by the
        # desktop hint gate (which contains "open|launch|start|run").
        _early_cam = _camera_intent(text)
        if _early_cam is not None:
            try:
                await self._room.local_participant.publish_data(
                    json.dumps({"type": "camera", "enabled": _early_cam}).encode(),
                    reliable=True,
                    topic=UI_COMMAND_TOPIC,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("camera command publish failed: %s", exc)
            try:
                await self.session.say(
                    "Camera on, sir." if _early_cam else "Camera off, sir."
                )
            except Exception:  # noqa: BLE001
                pass
            raise StopResponse()

        # Gesture mode — same rationale as camera. Must precede desktop +
        # content dispatchers so "gesture mode" / "turn on gesture mode"
        # never hits the desktop router (which produces the misleading
        # "your laptop's desktop-bridge isn't connected" error).
        _early_gm = _gesture_mode_intent(text)
        if _early_gm is not None:
            try:
                if _early_gm:
                    await self._room.local_participant.publish_data(
                        json.dumps({"type": "camera", "enabled": True}).encode(),
                        reliable=True,
                        topic=UI_COMMAND_TOPIC,
                    )
                await self._room.local_participant.publish_data(
                    json.dumps({"type": "gesture_mode", "enabled": _early_gm}).encode(),
                    reliable=True,
                    topic=UI_COMMAND_TOPIC,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("gesture-mode command publish failed: %s", exc)
            try:
                await self.session.say(
                    "Gesture mode on, sir." if _early_gm else "Gesture mode off, sir."
                )
            except Exception:  # noqa: BLE001
                pass
            raise StopResponse()

        # Volume — disambiguates across HUD widgets and desktop apps.
        # Runs BEFORE content + desktop so "mute" never falls into the
        # YouTube content matcher and never needs "on my laptop".
        if await self._maybe_handle_volume(text):
            raise StopResponse()

        # Close-widget priority — beats content router when a panel is
        # actually open ("close the YouTube" → close panel, NOT search
        # YouTube for "Close"). Guarded by live widget inventory.
        if await self._maybe_handle_close_widget(text):
            raise StopResponse()

        # Bridge lifecycle — "are the bridges online?" / "start the
        # bridge on the laptop". Runs BEFORE the desktop router so
        # "is the laptop online" gets a presence answer, not a command.
        if await self._maybe_handle_bridge_lifecycle(text):
            raise StopResponse()

        # Accommodation booking — yes/no resume for a pending confirmation.
        # Runs BEFORE content so a bare "yes" after "confirm the Marriott?"
        # doesn't get swallowed by a search/show handler.
        if await self._maybe_resume_accommodation_book(text):
            raise StopResponse()

        # Music intent — disambiguate laptop vs YouTube. Runs BEFORE
        # content so "play some music" doesn't get misrouted to the
        # generic websearch matcher.
        if await self._maybe_handle_music(text):
            raise StopResponse()

        # Screen content — search / video / news / maps / live browser.
        # Handled by regex (worker @function_tools never fire on
        # OpenJarvis); we speak our own confirmation, so stop the turn.
        if await self._maybe_handle_content(text):
            raise StopResponse()

        # Accommodation booking — search / book hotels via LiteAPI (Phase 1).
        # Apify Airbnb (Phase 2) plugs in as a read-only provider. Runs
        # AFTER content so generic "search/show" verbs still find the right
        # widget; BEFORE the LLM fall-through so booking intent never turns
        # into a chat reply. PCI-safe by design.
        if await self._maybe_handle_accommodation(text):
            raise StopResponse()

        # Intelligence — OpenCTI graph operations. Runs BEFORE desktop so
        # "log foo.com as suspicious" doesn't get mistaken for a file op.
        if await self._maybe_handle_cti(text):
            raise StopResponse()

        # Desktop control — operate the user's Windows machines. Regex
        # fallback for when the routed LLM doesn't emit the desktop_control
        # tool call; we speak our own confirmation, so stop the turn.
        if await self._maybe_handle_desktop(text):
            raise StopResponse()

        # APK build (voice-triggered rebuild of mobile-bridge). Fires
        # the same /tasks shell pipeline used to produce the first APK;
        # runs ~10-15 min on inspiring-cat and publishes the result as
        # a GitHub release.
        if await self._maybe_handle_apk_build(text):
            raise StopResponse()

        # Screen widgets — open/close panels on request (non-blocking).
        await self._maybe_handle_widget(text)

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

        # ── Cross-talk auto-mute ─────────────────────────────────────
        # Top of every real turn: release any prior auto-mute, then mute
        # what's currently playing so Jarvis can be heard. Cheap no-op
        # when nothing is playing or already-muted state is current.
        await self._maybe_auto_unmute()
        await self._maybe_auto_mute()

        # Camera + gesture mode are now handled at the top of _handle_turn
        # (above the desktop/content dispatchers) so phrases like "play"
        # / "open" / "video" don't get swallowed by _DESKTOP_HINT_RE.

        # 2) Vision — sample a frame and inject a description so the
        #    normal OpenJarvis text turn answers as if Jarvis can see.
        if _is_vision_intent(text):
            frame = self._latest_frame
            if frame is None and self._video_tasks:
                # A video track is subscribed but its first frame may not
                # have landed yet (e.g. camera was just turned on). Wait
                # briefly rather than wrongly reporting "the camera is off".
                for _ in range(15):
                    await asyncio.sleep(0.1)
                    if self._latest_frame is not None:
                        break
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


# ── Voice provider selection ──────────────────────────────────────────
# Default: cascade (Deepgram STT → OpenJarvis → Aura TTS, ~1 s TTFA).
# Reason it's no longer `auto`: Gemini Live answers FROM AUDIO directly,
# bypassing the worker's regex intent handlers — so "show me the news"
# got a hallucinated "Displaying the latest news headlines" with NO
# widget actually opened, because `_maybe_handle_content` never fired
# before Gemini started speaking. Cascade gives the worker first crack
# at every turn (intent handlers fire BEFORE the LLM), at the cost of
# ~700 ms more latency.
#
# OPENJARVIS_VOICE_PROVIDER overrides:
#   - `cascade`  (default) — Deepgram + OpenJarvis + Aura
#   - `realtime`           — Gemini Live (low-latency, but intent handlers
#                            fire too late; widgets won't open)
#   - `auto`               — try Live, fall back to cascade on failure
#                            (PRE-FIX behaviour; opt back in if you know
#                            you don't need widget intents on this session)

def _voice_provider_pref() -> str:
    pref = os.environ.get("OPENJARVIS_VOICE_PROVIDER", "cascade").strip().lower()
    if pref in ("auto", "realtime", "cascade"):
        return pref
    return "cascade"


def _realtime_enabled() -> bool:
    if not _REALTIME_AVAILABLE:
        return False
    pref = _voice_provider_pref()
    if pref == "cascade":
        return False
    # `realtime` forces it on (even without a key, so we surface the auth
    # error rather than silently falling back); `auto` requires a key.
    if pref == "realtime":
        return True
    return bool(
        os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    )


def _build_realtime_session(ctx: agents.JobContext) -> AgentSession:
    """AgentSession driven by Gemini Live (audio in + audio out, no STT/TTS).

    Voice id defaults to Charon (deep male — closest free analogue to the
    Deepgram Aura 'aura-orion-en' butler tone). Audio transcription on
    both ends is REQUIRED so `user_input_transcribed` events still fire —
    every regex intent handler + the interim-wake listener depend on
    transcript text.
    """
    model_id = os.environ.get(
        "OPENJARVIS_REALTIME_MODEL",
        "gemini-2.5-flash-native-audio-preview-12-2025",
    )
    voice = os.environ.get("OPENJARVIS_REALTIME_VOICE", "Charon")
    api_key = (
        os.environ.get("GOOGLE_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or None
    )
    rt_llm = _GeminiRealtimeModel(
        model=model_id,
        api_key=api_key,
        voice=voice,
        modalities=[_genai_types.Modality.AUDIO],
        input_audio_transcription=_genai_types.AudioTranscriptionConfig(),
        output_audio_transcription=_genai_types.AudioTranscriptionConfig(),
        temperature=0.7,
    )
    return AgentSession(
        vad=ctx.proc.userdata["vad"],
        llm=rt_llm,
    )


def _build_cascade_session(ctx: agents.JobContext) -> AgentSession:
    """The original Deepgram STT → OpenJarvis LLM proxy → Deepgram Aura
    TTS pipeline. Used as primary when OPENJARVIS_VOICE_PROVIDER=cascade
    or as fallback when Gemini Live fails to start."""
    openjarvis_url = os.environ.get(
        "OPENJARVIS_URL", "http://localhost:8000"
    ).rstrip("/")
    openjarvis_key = os.environ.get("OPENJARVIS_API_KEY", "basic-auth")
    model_name = os.environ.get("OPENJARVIS_MODEL", "openrouter/auto")

    # STT with wake-word boosting. "Jarvis" is an uncommon proper noun
    # Deepgram routinely mis-hears (Travis/Jervis/service…). We boost it
    # via Deepgram's keyword API — trying the nova-3 (`keyterms`) and
    # nova-2 (`keywords`) forms in turn, falling back to a plain STT so
    # the worker never fails to start on an API mismatch.
    if not os.environ.get("DEEPGRAM_API_KEY"):
        raise RuntimeError(
            "DEEPGRAM_API_KEY is not set — Deepgram STT/TTS cannot start"
        )
    stt = None
    for _label, _kw in (
        ("nova-3 keyterms", {"keyterms": ["Jarvis"]}),
        ("nova-2 keywords", {"keywords": [("Jarvis", 5.0)]}),
        ("no", {}),
    ):
        try:
            stt = deepgram.STT(**_kw)
            logger.info(
                "STT: Deepgram initialized (%s wake-word boost)", _label
            )
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

    return AgentSession(
        # NOTE: preemptive_generation left at its default (True). The
        # wake-gate fix in commit 1e6dcc2 stops mutating
        # new_message.content, so the framework's speculative mid-turn
        # LLM call is no longer invalidated on wake turns and actually
        # saves a round-trip.
        vad=ctx.proc.userdata["vad"],
        stt=stt,
        llm=openai.LLM(
            model=model_name,
            base_url=f"{openjarvis_url}/v1",
            api_key=openjarvis_key,
            # 4s timeout (down from 8s). chat_completions consistently
            # responds in 1-2s for trivial/simple turns on the post-Round-5
            # backend; the prior 8s allowed 4 retries (~32s of dead air)
            # when the backend stalled. 4s + the framework's 2 retries =
            # max ~14s, which is the absolute ceiling for a voice turn.
            timeout=4.0,
            extra_headers={
                **_openjarvis_auth_headers(),
                "X-OpenJarvis-Stream": "openai",
                "X-OpenJarvis-Direct": "true",
            },
        ),
        tts=tts,
    )


def _attach_wake_listener(session: AgentSession, assistant: "Assistant") -> None:
    """Interim-transcript wake listener — wakes the agent the moment the
    transcriber emits "Jarvis" in ANY transcript (interim OR final), not
    only on a fully-endpointed turn. Works identically on the cascade
    (Deepgram) and the realtime (Gemini Live) paths — both emit
    `user_input_transcribed` with `.transcript` + `.is_final`.

    Doubles as the pre-emptive auto-mute trigger: any interim transcript
    while the agent is awake fires `_maybe_auto_mute` immediately, so
    background audio (HUD widgets, PC music) is silenced WITHIN ~200 ms
    of the user opening their mouth — well before the turn endpoints.
    """

    @session.on("user_input_transcribed")
    def _on_user_transcript(ev) -> None:  # noqa: ANN001
        text = getattr(ev, "transcript", "") or ""
        if not assistant._awake:
            if text and _WAKE_RE.search(text):
                assistant._awake = True
                assistant._just_woke = True
                logger.info("wake: matched on transcript %r", text[:60])
                # Wake-on-music: kick off the mute task right away so the
                # background audio is down by the time TTS replies.
                try:
                    asyncio.create_task(assistant._maybe_auto_mute())
                except Exception:  # noqa: BLE001
                    pass
            return
        # Already awake — any speech is a candidate user turn. Fire the
        # mute task PRE-EMPTIVELY on the first interim transcript so we
        # don't wait for the endpoint to mute background audio. Cheap
        # no-op when nothing is playing (cached).
        if text:
            try:
                asyncio.create_task(assistant._maybe_auto_mute())
            except Exception:  # noqa: BLE001
                pass


async def entrypoint(ctx: agents.JobContext):
    await ctx.connect()

    openjarvis_url = os.environ.get(
        "OPENJARVIS_URL", "http://localhost:8000"
    ).rstrip("/")

    assistant = Assistant(ctx.room)

    # BVC noise cancellation can over-suppress quiet speech; allow opting
    # out with OPENJARVIS_NOISE_CANCEL=0 if user turns are being missed.
    _nc = None
    if os.environ.get("OPENJARVIS_NOISE_CANCEL", "1").lower() not in (
        "0",
        "false",
        "no",
    ):
        _nc = noise_cancellation.BVC()

    room_input_options = RoomInputOptions(
        noise_cancellation=_nc,
        # Required for the worker to receive the user's camera +
        # screen-share tracks (default off → no video reaches us).
        video_enabled=True,
    )

    session: AgentSession | None = None
    used_provider = "cascade"

    if _realtime_enabled():
        try:
            session = _build_realtime_session(ctx)
            _attach_wake_listener(session, assistant)
            # Bound the Live handshake. Typical connect is < 2 s; an 8 s
            # cap kills hangs (bad key, region trouble) fast so the
            # cascade fallback can take over within one session lifetime.
            await asyncio.wait_for(
                session.start(
                    room=ctx.room,
                    agent=assistant,
                    room_input_options=room_input_options,
                ),
                timeout=8.0,
            )
            used_provider = "realtime"
            logger.info(
                "voice: Gemini Live ACTIVE (model=%s, voice=%s)",
                os.environ.get(
                    "OPENJARVIS_REALTIME_MODEL",
                    "gemini-2.5-flash-native-audio-preview-12-2025",
                ),
                os.environ.get("OPENJARVIS_REALTIME_VOICE", "Charon"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "voice: Gemini Live failed (%s) — falling back to cascade",
                exc,
            )
            if session is not None:
                try:
                    await session.aclose()
                except Exception:  # noqa: BLE001
                    pass
            session = None

    if session is None:
        session = _build_cascade_session(ctx)
        _attach_wake_listener(session, assistant)
        await session.start(
            room=ctx.room,
            agent=assistant,
            room_input_options=room_input_options,
        )
        used_provider = "cascade"
        logger.info(
            "voice: cascade ACTIVE (Deepgram → OpenJarvis → Aura)"
        )

    # Relay live-browser interactions (click / scroll / key / navigate)
    # from the browser widget back to the worker's Playwright page.
    @ctx.room.on("data_received")
    def _on_browser_data(packet: rtc.DataPacket) -> None:
        if packet.topic != "jarvis-browser":
            return
        try:
            msg = json.loads(bytes(packet.data).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return
        asyncio.create_task(assistant.handle_browser_event(msg))

    # Reverse channel: the frontend pushes its open-widget inventory on
    # `jarvis-ui-state` so the worker can answer "is the YouTube panel
    # actually open?" — required by close-widget priority and volume
    # disambiguation.
    @ctx.room.on("data_received")
    def _on_ui_state(packet: rtc.DataPacket) -> None:
        if packet.topic != "jarvis-ui-state":
            return
        try:
            msg = json.loads(bytes(packet.data).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return
        mtype = msg.get("type")
        if mtype == "widget_state":
            raw_open = msg.get("open")
            open_list: list = raw_open if isinstance(raw_open, list) else []
            prior_ids: set[str] = {
                w.get("id") for w in assistant._open_widgets
                if isinstance(w, dict) and isinstance(w.get("id"), str)
            }
            new_ids: set[str] = {
                w.get("id") for w in open_list
                if isinstance(w, dict) and isinstance(w.get("id"), str)
            }
            assistant._open_widgets = open_list
            assistant._open_widgets_at = time.monotonic()
            # Diff into the resource ledger so "what's open?" is accurate.
            for w in open_list:
                if not isinstance(w, dict):
                    continue
                wid = w.get("id")
                if not isinstance(wid, str) or wid in prior_ids:
                    continue
                label = w.get("title") or w.get("kind") or "widget"
                asyncio.create_task(
                    assistant._ledger.open("widget", wid, str(label))
                )
            for wid in prior_ids - new_ids:
                asyncio.create_task(
                    assistant._ledger.close("widget", wid)
                )
        elif mtype == "widget_playback":
            wid = msg.get("id")
            state = msg.get("state")
            if isinstance(wid, str) and state in (
                "playing", "paused", "stopped", "ended"
            ):
                if state == "playing":
                    assistant._widget_playback_states[wid] = "playing"
                else:
                    assistant._widget_playback_states.pop(wid, None)
    # Start the unified resource-ledger idle watcher (browser auto-closes
    # after 10 min of no interaction; widgets carry no threshold; CTI
    # keeps its own watchdog for the moment).
    await assistant._ledger.start_idle_watcher(session)

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
