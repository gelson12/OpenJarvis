"""
OpenJarvis LiveKit Voice Worker

Bridges LiveKit voice sessions to OpenJarvis's /v1/chat/completions endpoint.
All intelligence (tools, memory, multi-LLM routing) lives in OpenJarvis.
This worker handles: VAD, STT (Deepgram), voice loop, TTS (Deepgram).

Environment variables required:
  LIVEKIT_URL          - wss://your-server.livekit.cloud
  LIVEKIT_API_KEY      - LiveKit API key
  LIVEKIT_API_SECRET   - LiveKit API secret
  DEEPGRAM_API_KEY     - Deepgram API key (for STT + TTS)
  OPENJARVIS_URL       - URL of the OpenJarvis service
  OPENJARVIS_API_KEY   - API key matching OPENJARVIS_API_KEY on that service
"""

import os
import logging
from dotenv import load_dotenv
from livekit import agents
from livekit.agents import AgentSession, Agent, RoomInputOptions
from livekit.plugins import openai, deepgram, silero, noise_cancellation

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


def prewarm(proc: agents.JobProcess):
    """Pre-load VAD model once per worker process to avoid cold-start delay."""
    proc.userdata["vad"] = silero.VAD.load()


class Assistant(Agent):
    def __init__(self):
        super().__init__(instructions=AGENT_INSTRUCTION)


async def entrypoint(ctx: agents.JobContext):
    await ctx.connect()

    openjarvis_url = os.environ.get("OPENJARVIS_URL", "http://localhost:8000")
    openjarvis_key = os.environ.get("OPENJARVIS_API_KEY", "default-key-change-me")

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
            model="openjarvis",
            base_url=f"{openjarvis_url}/v1",
            api_key=openjarvis_key,
        ),
        tts=tts,
    )

    await session.start(
        room=ctx.room,
        agent=Assistant(),
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVC()
        ),
    )


if __name__ == "__main__":
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
        )
    )
