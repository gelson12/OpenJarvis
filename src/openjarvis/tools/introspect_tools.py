"""Round 20 Piece 2 — Self-introspection meta-tool.

A tool that lets the LLM ask its own registry: "what tools do I have
for X?" Returns a ranked list of relevant tools with their callable
signatures so the LLM can pick one to invoke next.

Why this exists
---------------
The LLM has ~140 tools in its registry but only sees ~5-10 per turn
(the keyword-filtered relevance subset). When the user asks something
the keyword filter misses, the LLM has two failure modes today:

1. Disavow ("I don't have a tool to do X") — even when it does.
2. Hallucinate a tool name (the ``google_bridge`` bug, Round 18).

This tool removes both failure modes by giving the LLM a way to
INTROSPECT before either disavowing or guessing. When uncertain,
``introspect_tools(query="check the user's google calendar")``
returns the top-N matching tools with descriptions + their parameter
schemas — the LLM picks one and calls it directly.

The system-prompt addendum in routes.py tells the LLM:
  "When unsure if you have a capability, call ``introspect_tools``
   FIRST instead of disavowing or inventing a tool name."

Env flag
~~~~~~~~
``OPENJARVIS_INTROSPECT_TOOL_ENABLED`` (default ``true``).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.environ.get("OPENJARVIS_INTROSPECT_TOOL_ENABLED", "true").lower() in (
        "1", "true", "yes", "on",
    )


@ToolRegistry.register("introspect_tools")
class IntrospectToolsTool(BaseTool):
    """Query the agent's own tool registry by semantic similarity.

    Returns the top-N tools matching a natural-language query along
    with their callable signatures. The LLM should call this when
    uncertain whether a given capability exists rather than guessing
    or disavowing.
    """

    tool_id = "introspect_tools"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="introspect_tools",
            description=(
                "Search YOUR OWN tool registry for tools matching a "
                "natural-language description. Use this BEFORE saying "
                "'I don't have a tool for that' — your registry has "
                "~140 tools but only ~5-10 are surfaced per turn by "
                "the relevance filter; this tool searches ALL of them. "
                "Returns ranked matches with names, descriptions, and "
                "callable parameter schemas so you can pick one and "
                "call it next. Example queries: 'check google calendar', "
                "'send an email', 'create an n8n workflow', 'list files "
                "on the user's laptop'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Natural-language description of the "
                            "capability you're looking for. Be specific "
                            "(e.g. 'check Google Calendar for upcoming "
                            "meetings' beats 'calendar')."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Max results to return (default 10, "
                            "capped at 20)."
                        ),
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
            category="introspection",
            cost_estimate=0.0,
            latency_estimate=0.1,
        )

    def execute(self, **params: Any) -> ToolResult:
        if not _enabled():
            return ToolResult(
                tool_name=self.spec.name,
                success=False,
                content="introspect_tools is disabled via env flag.",
            )
        query = (params.get("query") or "").strip()
        if not query:
            return ToolResult(
                tool_name=self.spec.name,
                success=False,
                content="Query is required and cannot be empty.",
            )
        try:
            limit = int(params.get("limit", 10))
        except Exception:
            limit = 10
        limit = max(1, min(20, limit))

        try:
            from openjarvis.server.tool_router import rank_tools_for_query
            ranked = rank_tools_for_query(query, top_k=limit)
        except Exception as exc:
            logger.warning("introspect_tools: ranker failed (%s)", exc)
            return ToolResult(
                tool_name=self.spec.name,
                success=False,
                content=(
                    f"Tool router unavailable: {exc}. The keyword-based "
                    "filter is still active — see your existing toolset."
                ),
            )

        if not ranked:
            return ToolResult(
                tool_name=self.spec.name,
                success=True,
                content=(
                    f"No tools in the registry matched '{query}' above "
                    "the relevance threshold. The capability may "
                    "genuinely not exist — say so honestly. Do NOT "
                    "invent a tool name."
                ),
            )

        # Enrich each result with the actual parameter schema so the LLM
        # has everything it needs to call the tool on the next turn.
        enriched = []
        for r in ranked:
            entry: dict = {
                "name": r["name"],
                "score": r["score"],
                "description": r["description"],
            }
            try:
                cls = ToolRegistry.get(r["name"])
                inst = cls()
                spec = inst.spec
                entry["parameters"] = spec.parameters
                entry["category"] = spec.category
                if spec.requires_confirmation:
                    entry["requires_confirmation"] = True
            except Exception as exc:
                logger.debug(
                    "introspect_tools: param fetch failed for %s (%s)",
                    r["name"], exc,
                )
            enriched.append(entry)

        body = {
            "query": query,
            "count": len(enriched),
            "tools": enriched,
            "instruction": (
                "Pick the tool that best matches the user's intent and "
                "call it on your next turn. If none look right, say so "
                "honestly — do NOT invent a tool name."
            ),
        }
        return ToolResult(
            tool_name=self.spec.name,
            success=True,
            content=json.dumps(body, ensure_ascii=False, indent=2),
        )
