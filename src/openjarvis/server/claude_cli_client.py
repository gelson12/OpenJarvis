"""Client for the inspiring-cat Claude-CLI worker (submit-and-poll task API).

The worker exposes:
  POST /tasks              body {type: "claude_pro", payload: {prompt: str}} -> 201 {task_id, status}
  GET  /tasks/{task_id}    -> {id, type, status, result, error, ...}

`status` cycles through pending -> running -> done | failed.
Latency: 3-120s for Claude calls. No auth required at this writing.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


DEFAULT_BASE_URL = "https://inspiring-cat-production.up.railway.app"
DEFAULT_TASK_TYPE = "claude_pro"
DEFAULT_TIMEOUT_S = 180.0
DEFAULT_POLL_INTERVAL_S = 1.5


@dataclass
class TaskResult:
    task_id: str
    status: str  # pending | running | done | failed
    result: Optional[str]
    error: Optional[str]


def _base_url() -> str:
    return os.environ.get("CLI_WORKER_URL", DEFAULT_BASE_URL).rstrip("/")


def _auth_headers() -> dict[str, str]:
    """Build auth headers for inspiring-cat /tasks calls.

    Priority order (first match wins):
      1. INSPIRING_CAT_WEBHOOK_SECRET — shared secret for the worker.
      2. CLAUDE_SESSION_TOKEN — Claude.ai session cookie/token.
    Public /tasks endpoints work without auth at writing time, so an empty
    header dict is acceptable.
    """
    secret = os.environ.get("INSPIRING_CAT_WEBHOOK_SECRET")
    if secret:
        return {
            "Authorization": f"Bearer {secret}",
            "X-Webhook-Secret": secret,
        }
    token = os.environ.get("CLAUDE_SESSION_TOKEN")
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def _jarvis_context_note() -> str:
    """Build a system note describing OpenJarvis's already-active integrations.

    Inspiring-Cat (Claude-CLI) runs in a separate container with its own
    MCP server config — including some that target the wrong accounts
    (e.g. a `claude.ai n8n` MCP pointing at vistaltura.app.n8n.cloud while
    the user's actual self-hosted n8n is reached via OpenJarvis's
    N8N_BASE_URL on a completely different host).

    Without this note, Claude-CLI sees its own (misconfigured) MCPs and
    keeps telling the user "the n8n MCP needs auth" or pasting curl
    examples for the wrong server. Prepending the note tells Claude-CLI:
    OpenJarvis already has these integrations live — don't try to
    re-authenticate, don't paste curl examples, and don't reference any
    MCP-server endpoints; just elaborate on what the user actually
    asked, deferring to OpenJarvis's tool answers when relevant.
    """
    n8n_base = os.environ.get("N8N_BASE_URL", "").rstrip("/")
    bits: list[str] = [
        "[JARVIS-CONTEXT]",
        "You are providing a deeper elaboration to a question already "
        "being answered by OpenJarvis (the calling system). OpenJarvis "
        "has the following integrations LIVE and authenticated server-"
        "side via env-vars — do not suggest re-authenticating any of "
        "them, do not paste curl/bash examples for them, do not reference "
        "any MCP server endpoints (those target different accounts and "
        "are not what the user is using):",
    ]
    if n8n_base:
        bits.append(
            f"  - n8n: connected directly to {n8n_base} via N8N_API_KEY. "
            "Tools n8n_list_workflows / n8n_create_workflow / "
            "n8n_update_workflow / n8n_activate_workflow / "
            "n8n_execute_workflow / n8n_get_workflow / "
            "n8n_list_executions / n8n_list_credentials / "
            "n8n_get_credential / n8n_list_credential_types are callable "
            "by the OpenJarvis agent. The credentials API exposes "
            "metadata for SaaS services already auth'd inside n8n "
            "(Slack, Gmail OAuth, Stripe, Notion, etc.) — pair "
            "n8n_list_credentials with n8n_execute_workflow to USE those "
            "credentials without the user re-entering them. "
            "If the user mentions n8n, this is the only n8n that matters."
        )
    if os.environ.get("STRIPE_SECRET_KEY"):
        bits.append("  - stripe: live (revenue / charges / subscriptions / refunds)")
    if os.environ.get("PAYPAL_CLIENT_ID") and os.environ.get("PAYPAL_CLIENT_SECRET"):
        bits.append("  - paypal: live (transactions / subscriptions / refunds)")
    if os.environ.get("GOOGLE_REFRESH_TOKEN"):
        bits.append("  - google calendar: live (list / freebusy / create / update / delete)")
    if os.environ.get("GITHUB_PAT") or os.environ.get("GITHUB_TOKEN"):
        bits.append("  - github: live (repos / issues / PRs / actions)")
    if os.environ.get("OBSIDIAN_VAULT_URL"):
        bits.append("  - obsidian vault: live (read / write / search / backlinks)")
    if os.environ.get("CLOUDINARY_API_KEY"):
        bits.append("  - cloudinary: live")
    if os.environ.get("V0_API_KEY"):
        bits.append("  - v0: live")
    bits.append(
        "Your job is to give a deeper, more deliberate answer to the "
        "user's question — not to re-do the work OpenJarvis already did. "
        "If a tool call result isn't shown to you, assume the OpenJarvis "
        "agent will handle it. Focus on insight, edge cases, and follow-"
        "up suggestions, not on infrastructure plumbing."
    )
    return "\n".join(bits)


def _build_prompt(messages: list[dict[str, Any]]) -> str:
    """Flatten an OpenAI-style messages list into a single prompt string.

    inspiring-cat expects a string `prompt` in the payload, not a messages
    array — so we render the conversation as plain text with role tags.
    A JARVIS-CONTEXT block is prepended on every call so Claude-CLI
    knows which integrations are already live in OpenJarvis and stops
    referencing the wrong (out-of-band) MCP servers.
    """
    parts: list[str] = [_jarvis_context_note()]
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "") or ""
        if role == "system":
            parts.append(f"[SYSTEM]\n{content}")
        elif role == "assistant":
            parts.append(f"[ASSISTANT]\n{content}")
        else:
            parts.append(f"[USER]\n{content}")
    return "\n\n".join(parts)


async def submit(
    messages: list[dict[str, Any]],
    *,
    task_type: str = DEFAULT_TASK_TYPE,
    extra_payload: Optional[dict[str, Any]] = None,
) -> str:
    """Submit a Claude-CLI task; return the task_id immediately."""
    payload: dict[str, Any] = {"prompt": _build_prompt(messages)}
    if extra_payload:
        payload.update(extra_payload)
    body = {"type": task_type, "payload": payload}

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{_base_url()}/tasks",
            json=body,
            headers=_auth_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        task_id = data.get("task_id") or data.get("id")
        if not task_id:
            raise RuntimeError(f"inspiring-cat returned no task_id: {data!r}")
        logger.info("Claude-CLI task submitted: %s", task_id)
        return task_id


async def poll(task_id: str) -> TaskResult:
    """Single GET against /tasks/{id}."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{_base_url()}/tasks/{task_id}",
            headers=_auth_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        return TaskResult(
            task_id=data.get("id", task_id),
            status=data.get("status", "unknown"),
            result=data.get("result"),
            error=data.get("error"),
        )


async def await_completion(
    task_id: str,
    *,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> TaskResult:
    """Poll until status is done or failed, or until timeout_s elapses.

    Raises asyncio.TimeoutError on overall timeout. On failed status, returns
    the TaskResult so callers can inspect `error`.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    while True:
        try:
            tr = await poll(task_id)
        except httpx.HTTPError as exc:
            logger.warning("Poll error for %s: %s", task_id, exc)
            tr = TaskResult(task_id=task_id, status="pending", result=None, error=None)

        if tr.status in ("done", "failed"):
            return tr

        if asyncio.get_event_loop().time() >= deadline:
            raise asyncio.TimeoutError(
                f"Claude-CLI task {task_id} did not complete within {timeout_s}s"
            )
        await asyncio.sleep(poll_interval_s)


async def is_healthy() -> bool:
    """Quick health probe; safe to call from startup. Never raises."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{_base_url()}/health")
            if resp.status_code != 200:
                return False
            data = resp.json()
            return bool(data.get("claude_available"))
    except Exception:
        return False


__all__ = [
    "TaskResult",
    "submit",
    "poll",
    "await_completion",
    "is_healthy",
]
