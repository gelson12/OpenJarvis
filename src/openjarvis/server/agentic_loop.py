"""Round 20 Piece 3 — Plan-act-verify agentic loop.

The third architectural piece of Round 20. Phases A+B gave the LLM
better tool context (semantic ranking + self-introspection) and the
feedback loop (outcome logging + tool affinity). This piece adds the
RUNTIME verification layer:

  1. PLAN  — LLM proposes an action (call a tool, or answer directly)
  2. ACT   — server validates the proposed tool call against the
             registry; rejects hallucinated tool names BEFORE
             execution; dispatches via ToolRegistry to the actual
             Python implementation
  3. VERIFY — tool result appended as a `tool` message; loop re-enters
             the LLM with the result
  4. REFLECT — if the LLM narrates "I'll check X" without emitting a
             tool_call, re-prompt with "you said you would; emit the
             tool_call now"

The loop iterates up to N times (default 3) before forcing a final
response. Records every decision for Phase B's outcome logger.

Why this exists
---------------
The current chat path is one-shot: ``user message → LLM → response``.
The LLM can:
  * call a hallucinated tool name (``google_bridge``) → framework
    errors silently, LLM thinks it succeeded → user sees a wrong
    answer
  * narrate "I'll check your calendar" without emitting a tool call →
    no tool runs → user waits for a follow-up that never comes
  * disavow a capability it has → no recovery

This loop catches all three. Validation prevents hallucinated names.
Narration detection forces a retry. Disavowal detection (from Phase B)
gets logged so future turns get better tool suggestions.

Env flags
~~~~~~~~~
``OPENJARVIS_AGENTIC_LOOP_ENABLED`` (default ``false``)
    Master switch. Default OFF so this ships as an opt-in
    optimisation; can be flipped on per-deployment after observing
    Phase A+B for a session or two.
``OPENJARVIS_AGENTIC_LOOP_MAX_ITERATIONS`` (default ``3``)
    Hard cap on plan-act-verify cycles per turn to bound latency.

This module is INTENTIONALLY standalone. It doesn't import from
routes.py and routes.py only references it lazily inside a try/except
behind an env flag. That keeps the existing chat pipeline working
exactly as it does today; the loop is additive, not a refactor.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


def is_enabled() -> bool:
    return os.environ.get("OPENJARVIS_AGENTIC_LOOP_ENABLED", "false").lower() in (
        "1", "true", "yes", "on",
    )


def max_iterations() -> int:
    try:
        return max(1, min(10, int(
            os.environ.get("OPENJARVIS_AGENTIC_LOOP_MAX_ITERATIONS", "3"),
        )))
    except Exception:
        return 3


# ---------------------------------------------------------------------------
# Narration-without-action detector
# ---------------------------------------------------------------------------

# Phrases the LLM says when it INTENDS to call a tool but didn't emit
# a tool_call. If the response text matches this AND there are no
# tool_calls, the loop re-prompts the LLM to actually call.
_NARRATION_RE = re.compile(
    r"\b(?:"
    r"i'?ll\s+(?:check|look\s+up|search|fetch|retrieve|query|grab|"
    r"see|find|verify|confirm|look|pull|get)|"
    r"let\s+me\s+(?:check|look|search|fetch|retrieve|query|grab|"
    r"see|find|verify|confirm|pull|get)|"
    r"i'?m\s+going\s+to\s+(?:check|look|search|fetch|retrieve|query)|"
    r"give\s+me\s+(?:a\s+)?(?:moment|second|sec|minute)\s+(?:to|while\s+i)|"
    r"just\s+(?:a\s+)?moment\s+while\s+i|"
    r"hold\s+on\s+while\s+i|"
    r"one\s+(?:moment|second)\s+(?:sir|please)?\s*[,.]?\s*(?:i'?ll|let\s+me)"
    r")\b",
    re.IGNORECASE,
)


def looks_like_narration_without_action(
    response_text: str,
    tool_calls: Optional[List[Any]] = None,
) -> bool:
    """True when the LLM's text PROMISES an action but no tool_call
    was emitted. The signal: narration regex matches AND tool_calls
    is empty/None.
    """
    if tool_calls:
        return False  # actual tool_call present — not narration
    if not response_text:
        return False
    return bool(_NARRATION_RE.search(response_text))


# ---------------------------------------------------------------------------
# Tool-call validation (anti-hallucination)
# ---------------------------------------------------------------------------


def validate_tool_call(name: str) -> Tuple[bool, str]:
    """Verify the named tool exists in ToolRegistry.

    Returns ``(is_valid, reason)``. Used to reject hallucinated tool
    names like ``google_bridge`` BEFORE attempting to execute them.
    """
    if not name or not isinstance(name, str):
        return False, "tool name is empty"
    try:
        from openjarvis.core.registry import ToolRegistry
        if not ToolRegistry.contains(name):
            return False, (
                f"tool '{name}' does not exist in the registry. "
                "Call `introspect_tools` to find the correct tool name."
            )
        return True, ""
    except Exception as exc:
        return False, f"registry lookup failed: {exc}"


# ---------------------------------------------------------------------------
# Tool execution wrapper
# ---------------------------------------------------------------------------


@dataclass
class ToolCallOutcome:
    """One round of plan-act-verify: the call attempted + its result."""

    name: str
    arguments: Dict[str, Any]
    success: bool
    content: str
    latency_ms: int
    validation_error: Optional[str] = None


def execute_tool_call(name: str, arguments: Dict[str, Any]) -> ToolCallOutcome:
    """Dispatch one tool call via ToolRegistry. Validates name first;
    returns a structured outcome whether the call succeeded or failed.

    Never raises — the loop relies on a structured failure to drive
    the next iteration.
    """
    t0 = time.time()
    valid, reason = validate_tool_call(name)
    if not valid:
        logger.warning("agentic_loop.validate_rejected: %s (%s)", name, reason)
        return ToolCallOutcome(
            name=name,
            arguments=arguments,
            success=False,
            content=reason,
            latency_ms=int((time.time() - t0) * 1000),
            validation_error=reason,
        )
    try:
        from openjarvis.core.registry import ToolRegistry
        cls = ToolRegistry.get(name)
        inst = cls()
        result = inst.execute(**(arguments or {}))
        success = bool(getattr(result, "success", True))
        content = getattr(result, "content", None) or str(result)
        if not isinstance(content, str):
            try:
                content = json.dumps(content, ensure_ascii=False, default=str)
            except Exception:
                content = str(content)
        return ToolCallOutcome(
            name=name,
            arguments=arguments,
            success=success,
            content=content,
            latency_ms=int((time.time() - t0) * 1000),
        )
    except Exception as exc:
        logger.warning("agentic_loop.exec_failed: %s -> %s", name, exc)
        return ToolCallOutcome(
            name=name,
            arguments=arguments,
            success=False,
            content=f"Tool execution error: {exc}",
            latency_ms=int((time.time() - t0) * 1000),
        )


# ---------------------------------------------------------------------------
# OpenAI-format tool_calls extraction
# ---------------------------------------------------------------------------


def extract_tool_calls(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Pull ``tool_calls`` out of an OpenAI-format chat completion
    response. Returns a list of ``{id, name, arguments}`` dicts (with
    arguments parsed from JSON when possible). Empty list when none.

    Tolerant of partial / streamed / dict-shape variants since
    providers don't all follow the spec exactly.
    """
    if not isinstance(response, dict):
        return []
    choices = response.get("choices") or []
    if not choices:
        return []
    msg = choices[0].get("message") or {}
    raw = msg.get("tool_calls") or []
    if not raw:
        return []
    out: List[Dict[str, Any]] = []
    for tc in raw:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = fn.get("name") or tc.get("name") or ""
        raw_args = fn.get("arguments") or tc.get("arguments") or "{}"
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                args = {}
        elif isinstance(raw_args, dict):
            args = raw_args
        else:
            args = {}
        out.append({
            "id": tc.get("id") or f"call_{len(out)}",
            "name": name,
            "arguments": args,
        })
    return out


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


@dataclass
class AgenticLoopTrace:
    """Full record of a plan-act-verify session for one user turn."""

    iterations: int = 0
    tool_calls: List[ToolCallOutcome] = field(default_factory=list)
    narration_retries: int = 0
    final_response_text: str = ""
    halted_by: str = "complete"  # complete | max_iterations | error

    def to_dict(self) -> Dict[str, Any]:
        return {
            "iterations": self.iterations,
            "tool_calls": [
                {
                    "name": tc.name,
                    "success": tc.success,
                    "latency_ms": tc.latency_ms,
                    "validation_error": tc.validation_error,
                }
                for tc in self.tool_calls
            ],
            "narration_retries": self.narration_retries,
            "halted_by": self.halted_by,
            "final_response_preview": (self.final_response_text or "")[:200],
        }


def run_loop(
    *,
    messages: List[Dict[str, Any]],
    llm_call: Callable[[List[Dict[str, Any]]], Dict[str, Any]],
    max_iter: Optional[int] = None,
) -> Tuple[str, AgenticLoopTrace]:
    """Run the plan-act-verify loop.

    Parameters
    ----------
    messages : list of OpenAI-format message dicts
        The conversation so far. Will be EXTENDED with tool results
        and assistant turns as the loop iterates.
    llm_call : callable
        ``llm_call(messages) -> response_dict``. The caller wraps the
        actual engine / provider. Must return an OpenAI-format chat
        completion (``{"choices": [{"message": {...}}]}``).
    max_iter : int, optional
        Cap on iterations. Defaults to ``OPENJARVIS_AGENTIC_LOOP_MAX_ITERATIONS``.

    Returns
    -------
    (final_text, trace)
        ``final_text`` is the LLM's final assistant message (what gets
        spoken to the user). ``trace`` is the structured record for
        the outcome logger.
    """
    if not is_enabled():
        # Loop is disabled — caller should fall back to one-shot.
        raise RuntimeError(
            "agentic_loop is disabled "
            "(OPENJARVIS_AGENTIC_LOOP_ENABLED is not set). Caller "
            "should not invoke run_loop in this mode."
        )

    cap = max_iter if max_iter is not None else max_iterations()
    trace = AgenticLoopTrace()
    working_messages = list(messages)
    last_response_text = ""

    for it in range(cap):
        trace.iterations = it + 1
        try:
            response = llm_call(working_messages)
        except Exception as exc:
            logger.warning("agentic_loop: llm_call raised %s", exc)
            trace.halted_by = "error"
            return (last_response_text or f"(error: {exc})"), trace

        msg = (response.get("choices") or [{}])[0].get("message") or {}
        response_text = msg.get("content") or ""
        tool_calls = extract_tool_calls(response)

        # Branch 1: tool_calls present — execute, append, loop again
        if tool_calls:
            # Append the assistant turn with tool_calls (per OpenAI spec)
            working_messages.append({
                "role": "assistant",
                "content": response_text or None,
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(
                                tc["arguments"], ensure_ascii=False,
                            ),
                        },
                    }
                    for tc in tool_calls
                ],
            })
            for tc in tool_calls:
                outcome = execute_tool_call(tc["name"], tc["arguments"])
                trace.tool_calls.append(outcome)
                working_messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": tc["name"],
                    "content": outcome.content,
                })
            last_response_text = response_text
            continue  # next iteration: LLM sees the tool results

        # Branch 2: narration without action — re-prompt
        if looks_like_narration_without_action(response_text, tool_calls):
            trace.narration_retries += 1
            logger.info(
                "agentic_loop.narration_retry: %r", response_text[:120],
            )
            working_messages.append({
                "role": "assistant",
                "content": response_text,
            })
            working_messages.append({
                "role": "user",
                "content": (
                    "You said you would do that but didn't actually emit "
                    "a tool_call. Please call the appropriate tool NOW "
                    "(use `introspect_tools` if uncertain which tool to "
                    "call). Do NOT narrate — call the tool."
                ),
            })
            last_response_text = response_text
            continue

        # Branch 3: clean final answer — return it
        trace.final_response_text = response_text
        trace.halted_by = "complete"
        return response_text, trace

    # Loop cap reached without a clean final answer
    trace.halted_by = "max_iterations"
    trace.final_response_text = last_response_text
    return last_response_text, trace


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def snapshot() -> Dict[str, Any]:
    """Diagnostic block for /v1/_debug/agentic."""
    return {
        "enabled": is_enabled(),
        "max_iterations": max_iterations(),
        "version": "round-20-phase-c",
        "narration_re_pattern_present": True,
    }
