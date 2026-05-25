"""Hermes selective routing — OpenJarvis voice worker → Hermes for multi-step turns.

When a voice turn looks multi-step (e.g. "first…then…", "summarize X and write Y"),
this router calls the Hermes gateway's /v1/chat/completions endpoint instead of
OpenJarvis's own backend. Multi-step queries benefit from Hermes's Tier 1+2 agentic
stack: task planner, post-turn reflector, skill distiller, tool sentinel, goal
tracker. Routine turns fall through to the normal OpenJarvis backend, which keeps
its fast response cadence and all its existing intent handlers.

Config (env vars on the OpenJarvis Railway service):
    OPENJARVIS_HERMES_ROUTE   "off" | "selective" | "always" (default "off")
    HERMES_URL                e.g. https://hermes-agent-production-027d.up.railway.app
    HERMES_API_KEY            bearer token (the API_SERVER_KEY value from the Hermes service)

If anything fails (network, 5xx, missing env), `call()` returns None so the worker
caller can fall through to the normal OpenJarvis backend path. Never raises.

Routing decision uses the same heuristic as agent/planner.py:should_plan in Hermes,
so the call only fires when Hermes's planner is likely to engage anyway. That
keeps the selective-routing latency tax aligned with where the agentic stack
actually helps.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


_MULTI_STEP_HINT = re.compile(
    r"\b(?:and then|after that|first[,. ]|then[,. ]|next[,. ]|finally|"
    r"step \d|step one|step two|once you|once that|once we|"
    r"in parallel|while you|in the meantime)\b",
    re.IGNORECASE,
)
_VERB_HINT = re.compile(
    r"\b(?:find|fetch|search|read|write|summarize|compare|list|generate|"
    r"create|build|deploy|run|test|analyze|extract|combine|merge|"
    r"refactor|debug|investigate|review|check|verify|email|send|post|"
    r"download|upload|push|pull|commit|publish|plan)\b",
    re.IGNORECASE,
)


def _mode() -> str:
    return os.environ.get("OPENJARVIS_HERMES_ROUTE", "off").strip().lower()


def enabled() -> bool:
    """True when env-gated AND the Hermes URL+key are configured."""
    if _mode() not in ("selective", "always"):
        return False
    if not os.environ.get("HERMES_URL", "").strip():
        return False
    return True


def should_route(text: str, *, min_chars: int = 120, min_verbs: int = 3) -> bool:
    """Decide whether a turn is worth routing to Hermes.

    `always` mode routes every turn (for debugging / opt-in heavy use).
    `selective` mode mirrors agent/planner.py:should_plan in Hermes.
    """
    if _mode() == "always":
        return True
    if _mode() != "selective":
        return False
    if not text or not isinstance(text, str):
        return False
    if _MULTI_STEP_HINT.search(text):
        return True
    if len(text) < min_chars:
        return False
    verbs = {m.group(0).lower() for m in _VERB_HINT.finditer(text)}
    return len(verbs) >= min_verbs


class HermesRouter:
    """Async client that calls Hermes /v1/chat/completions.

    Single shared httpx.AsyncClient with a generous timeout — Hermes's planner
    pipeline can take 5-10s on a 3-subtask decomposition. Failure → None.
    """

    def __init__(self, *, timeout_s: float = 60.0):
        self._timeout = timeout_s
        self._client: Optional[httpx.AsyncClient] = None
        self._base = os.environ.get("HERMES_URL", "").rstrip("/")
        self._key = os.environ.get("HERMES_API_KEY", "").strip()

    @property
    def is_configured(self) -> bool:
        return bool(self._base and self._key)

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def call(self, *, user_text: str, session_id: str) -> Optional[str]:
        """POST /v1/chat/completions to Hermes. Return assistant content or None."""
        if not self.is_configured or not user_text:
            return None
        if not user_text.strip():
            return None

        client = self._ensure_client()
        url = f"{self._base}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
            # Threading the LiveKit room name as the Hermes session id binds
            # vault recall + goals + reflection to this conversation across
            # sessions.  Stable per voice room.
            "X-Hermes-Session-Id": session_id or "openjarvis-voice",
            # Hint to Hermes that we already pre-filtered to a multi-step
            # candidate; planner can skip its own should_plan check.
            "X-Hermes-Source": "openjarvis-voice-selective",
        }
        body = {
            "model": "hermes-agent",
            "stream": False,
            "messages": [{"role": "user", "content": user_text}],
        }
        try:
            resp = await client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 — never let Hermes failure break a voice turn
            logger.warning("hermes_router.call failed (%s) — falling back to OpenJarvis backend", exc)
            return None

        # Standard OpenAI Chat Completions shape.
        try:
            content = payload["choices"][0]["message"]["content"]
        except Exception:
            logger.warning("hermes_router.call: unexpected response shape: %r", str(payload)[:200])
            return None

        if not isinstance(content, str) or not content.strip():
            return None

        # Surface metadata for log readers; Hermes adds `hermes_planner` when
        # the planner short-circuited the AIAgent path.
        meta = payload.get("hermes_planner")
        if meta:
            logger.info(
                "hermes_router.call planner=%s elapsed=%dms subtasks=%d session=%s",
                meta.get("success"),
                int(meta.get("elapsed_ms", 0)),
                int(meta.get("subtasks", 0)),
                session_id,
            )
        else:
            logger.info("hermes_router.call (no planner short-circuit) session=%s len=%d",
                         session_id, len(content))
        return content

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None
