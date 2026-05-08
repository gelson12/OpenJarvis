"""V0 (vercel.com/v0) tools — generate, iterate, deploy websites.

V0 is Vercel's AI website builder. The agent calls these tools to:
  - Generate a new site/UI from a prompt (v0_design_create)
  - Iterate on an existing design with follow-up prompts (v0_design_iterate)
  - Extract the live preview / deploy URL from a V0 response (helper)

V0's response contains the generated code + a hosted preview URL the
user can visit immediately. We extract the URL and surface it cleanly
so the agent can pass it back to the user without parsing raw JSON.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.integrations.v0 import V0Client, V0UnavailableError, get_default_client
from openjarvis.tools._stubs import BaseTool, ToolSpec


# V0 commonly embeds preview URLs as either v0.dev/chat/<id> links or
# vusercontent.net deploy URLs in the assistant content. Match both so
# we can hand the user a one-click URL.
_PREVIEW_URL_RE = re.compile(
    r"https?://(?:v0\.dev/chat/|[a-z0-9-]+\.vusercontent\.net/?|vercel\.app/?)\S*",
    re.IGNORECASE,
)


def _extract_assistant_text(payload: Any) -> str:
    """Pull the assistant message text from V0's OpenAI-compatible response."""
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    # Some providers return content as a list of typed parts
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                out.append(part["text"])
        return "\n".join(out)
    return ""


def _extract_preview_urls(text: str) -> list[str]:
    """Pull all V0/Vercel preview URLs from the assistant text."""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _PREVIEW_URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;)\"'")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _format_v0_response(payload: Any) -> dict[str, Any]:
    """Turn V0's raw OpenAI-shaped response into the cleaner shape the
    agent should send back to the user: assistant text, extracted
    preview URLs, and the latest assistant message ready to feed back
    into v0_design_iterate."""
    text = _extract_assistant_text(payload)
    return {
        "assistant_message": text,
        "preview_urls": _extract_preview_urls(text),
        "raw": payload,
    }


def _ok(name: str, payload: Any) -> ToolResult:
    if not isinstance(payload, str):
        try:
            payload = json.dumps(payload, default=str, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            payload = str(payload)
    return ToolResult(tool_name=name, content=payload or "(no content)", success=True)


def _err(name: str, exc: Exception) -> ToolResult:
    return ToolResult(tool_name=name, content=f"V0 error: {exc}", success=False)


class _V0Base(BaseTool):
    is_local = False

    def __init__(self, client: Optional[V0Client] = None) -> None:
        self._client = client or get_default_client()


# ---------------------------------------------------------------------------
# v0_design_create — generate a new website / UI from a prompt
# ---------------------------------------------------------------------------


@ToolRegistry.register("v0_design_create")
class V0DesignCreateTool(_V0Base):
    """Generate a new website / UI from a natural-language prompt."""

    tool_id = "v0_design_create"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="v0_design_create",
            description=(
                "Generate a new website / UI from a prompt using Vercel V0. "
                "Best for 'build me a landing page that ...' / 'design a "
                "dashboard for ...' style asks. Returns the assistant "
                "message + the live preview URL the user can visit "
                "immediately. To refine the result without starting over, "
                "call v0_design_iterate with the prior assistant_message "
                "and a new follow-up prompt."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "Detailed description of what to build. Specify "
                            "layout, components, color/style, framework (V0 "
                            "defaults to Next.js + shadcn/ui + Tailwind)."
                        ),
                    },
                    "system": {
                        "type": "string",
                        "description": (
                            "Optional system message to steer style / tone."
                        ),
                    },
                    "model": {
                        "type": "string",
                        "description": "V0 model id (v0-1.5-md default).",
                    },
                },
                "required": ["prompt"],
            },
            category="dev",
            requires_confirmation=True,
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            payload = self._client.chat_create(
                params["prompt"],
                model=params.get("model", "v0-1.5-md"),
                system=params.get("system"),
            )
        except V0UnavailableError as exc:
            return _err(self.spec.name, exc)
        return _ok(self.spec.name, _format_v0_response(payload))


# Keep the legacy v0_chat_create name as an alias so any existing
# call sites or learned tool-call patterns still work. Both classes
# resolve to the same V0 chat completion under the hood.
@ToolRegistry.register("v0_chat_create")
class V0ChatCreateTool(V0DesignCreateTool):
    tool_id = "v0_chat_create"


# ---------------------------------------------------------------------------
# v0_design_iterate — refine an existing design with a follow-up prompt
# ---------------------------------------------------------------------------


@ToolRegistry.register("v0_design_iterate")
class V0DesignIterateTool(_V0Base):
    """Iterate on an existing V0 design with a follow-up prompt.

    The agent passes:
      - prior_prompt: the original prompt that built the design
      - prior_response: the assistant_message from the previous turn
      - follow_up: the refinement ("make the hero section larger", etc.)

    V0 picks up the previous design and applies the change, returning a
    new preview URL that supersedes the old one.
    """

    tool_id = "v0_design_iterate"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="v0_design_iterate",
            description=(
                "Refine a V0-generated design without starting over. "
                "Pass the prior prompt + prior response + a follow-up "
                "instruction. Use this for 'make it darker', 'add a "
                "pricing section', 'switch to dark mode', etc. — "
                "anything that builds on the existing design."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "prior_prompt": {
                        "type": "string",
                        "description": (
                            "The original v0_design_create prompt the "
                            "design was built from."
                        ),
                    },
                    "prior_response": {
                        "type": "string",
                        "description": (
                            "The assistant_message from the previous V0 "
                            "turn (returned by v0_design_create or a "
                            "previous v0_design_iterate call)."
                        ),
                    },
                    "follow_up": {
                        "type": "string",
                        "description": "The refinement / new instruction.",
                    },
                    "model": {
                        "type": "string",
                        "description": "V0 model id (v0-1.5-md default).",
                    },
                },
                "required": ["prior_prompt", "prior_response", "follow_up"],
            },
            category="dev",
            requires_confirmation=True,
        )

    def execute(self, **params: Any) -> ToolResult:
        messages = [
            {"role": "user", "content": params["prior_prompt"]},
            {"role": "assistant", "content": params["prior_response"]},
            {"role": "user", "content": params["follow_up"]},
        ]
        try:
            payload = self._client.chat(
                messages, model=params.get("model", "v0-1.5-md"),
            )
        except V0UnavailableError as exc:
            return _err(self.spec.name, exc)
        return _ok(self.spec.name, _format_v0_response(payload))


# ---------------------------------------------------------------------------
# v0_extract_preview_url — utility for parsing existing V0 outputs
# ---------------------------------------------------------------------------


@ToolRegistry.register("v0_extract_preview_url")
class V0ExtractPreviewUrlTool(BaseTool):
    """Pull the preview URL(s) out of an arbitrary V0 response text."""

    tool_id = "v0_extract_preview_url"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="v0_extract_preview_url",
            description=(
                "Given a V0 assistant message (or any text containing "
                "v0.dev / vusercontent.net / vercel.app links), extract "
                "all preview URLs. Use when the user pastes prior V0 "
                "output and you need to surface the deploy URL."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                },
                "required": ["text"],
            },
            category="dev",
        )

    def execute(self, **params: Any) -> ToolResult:
        text = params.get("text") or ""
        urls = _extract_preview_urls(text)
        return _ok(
            self.spec.name,
            {"preview_urls": urls, "count": len(urls)},
        )


__all__ = [
    "V0DesignCreateTool",
    "V0ChatCreateTool",  # legacy alias
    "V0DesignIterateTool",
    "V0ExtractPreviewUrlTool",
]
