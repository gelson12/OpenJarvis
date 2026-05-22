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

import httpx
from dotenv import load_dotenv
from livekit import agents, rtc, api
from livekit.agents import AgentSession, Agent, RoomInputOptions, StopResponse
from livekit.agents.utils.images import encode, EncodeOptions, ResizeOptions
from livekit.plugins import openai, deepgram, silero, noise_cancellation
from openai import AsyncOpenAI

import search_tools

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
            min_speech_duration=0.02,
            min_silence_duration=0.25,
            activation_threshold=0.38,
            prefix_padding_duration=0.35,
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
    r"\b(jarvis|jarviss|jarvi|jervis|travis|java'?s|charvis)\b", re.I
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


# Lead / filler / command words stripped to recover the bare query from a
# spoken request like "can you play a video of cars on youtube".
_QUERY_NOISE = re.compile(
    r"\b(?:hey |ok |okay )?jarvis\b"
    r"|\b(?:can|could|would|will)\s+you\b|\bplease\b|\bfor me\b"
    r"|\bi\s+(?:want|need|wanna)(?:\s+to|\s+you\s+to)?\b"
    r"|\bi'?d\s+like(?:\s+to|\s+you\s+to)?\b"
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
    # Drop a dangling leading article left after the command word is gone
    # ("play a video of cars" -> "a cars" -> "cars").
    q = re.sub(r"^(?:a|an|the|some|my)\b\s*", "", q, flags=re.I)
    return q


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


class Assistant(Agent):
    def __init__(self, room: rtc.Room):
        super().__init__(instructions=AGENT_INSTRUCTION)
        self._room = room
        # Desktop control — lazy 2nd LiveKit connection to the PCs' bridge.
        self._desktop = DesktopBridge()
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
        """Send a UI command to the browser on the `jarvis-ui` topic."""
        try:
            await self._room.local_participant.publish_data(
                json.dumps(msg).encode("utf-8"),
                reliable=True,
                topic=_UI_TOPIC,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("jarvis-ui publish failed: %s", exc)

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
        """Show current news headlines on the JARVIS screen.

        Args:
            topic: Optional subject to focus the news on (e.g.
                "technology"). Leave empty for the top headlines.
        """
        articles = await search_tools.news_search(topic, limit=8)
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
        where = f" on {topic}" if topic else ""
        return f"Here are {len(articles)} headlines{where}, sir."

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

    async def handle_browser_event(self, msg: dict) -> None:
        """Apply a relayed interaction from the browser widget."""
        if self._browser is None:
            return
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
            # — strip the wake word and answer whatever followed it.
            self._just_woke = False
            rest = _WAKE_RE.sub("", text).strip(" ,.!?-")
            if rest:
                new_message.content = [rest]
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
                    # "Jarvis, what's the time" — answer what followed.
                    new_message.content = [rest]
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

        # Screen content — search / video / news / maps / live browser.
        # Handled by regex (worker @function_tools never fire on
        # OpenJarvis); we speak our own confirmation, so stop the turn.
        if await self._maybe_handle_content(text):
            raise StopResponse()

        # Desktop control — operate the user's Windows machines. Regex
        # fallback for when the routed LLM doesn't emit the desktop_control
        # tool call; we speak our own confirmation, so stop the turn.
        if await self._maybe_handle_desktop(text):
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
                # FAST PATH — skip the agent orchestrator; stream the
                # engine token-by-token. Cuts seconds off each voice turn.
                "X-OpenJarvis-Direct": "true",
            },
        ),
        tts=tts,
    )

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
    # Interim-transcript wake listener — wakes the agent the moment
    # Deepgram emits "Jarvis" in ANY transcript (interim OR final), not
    # only on a fully-endpointed turn. This is what makes waking reliable:
    # a short or mis-finalised "Jarvis" still has many partial transcripts
    # to match against. A false wake is harmless (dormant just flips on).
    @session.on("user_input_transcribed")
    def _on_user_transcript(ev) -> None:  # noqa: ANN001
        if assistant._awake:
            return
        text = getattr(ev, "transcript", "") or ""
        if text and _WAKE_RE.search(text):
            assistant._awake = True
            assistant._just_woke = True
            logger.info("wake: matched on transcript %r", text[:60])

    await session.start(
        room=ctx.room,
        agent=assistant,
        room_input_options=RoomInputOptions(
            noise_cancellation=_nc,
            # Required for the worker to receive the user's camera +
            # screen-share tracks (default off → no video reaches us).
            video_enabled=True,
        ),
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
