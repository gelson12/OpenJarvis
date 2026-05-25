"""Goal tracker as a registered BaseTool (OpenJarvis tool-registry adapter).

The actual storage + system-prompt-injection logic lives in
``openjarvis.tools.goal_tracker``.  This module just wraps it as a single
``BaseTool`` with an action enum so it surfaces in the model's tool list
when ``OPENJARVIS_GOALS_ENABLED=true``.

Pattern mirrors `memory_manage.py` for consistency with the existing tool
library — one registered tool, one ``execute`` dispatch on an ``action``
parameter.
"""

from __future__ import annotations

import json
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec
from openjarvis.tools import goal_tracker as _gt


@ToolRegistry.register("goal")
class GoalTool(BaseTool):
    """Manage persistent user goals that span sessions.

    Goals stored in ``~/.openjarvis/goals.json``.  Active goals are
    auto-injected into every system prompt by ``SystemPromptBuilder`` so
    the agent stays aligned with what the user is trying to accomplish
    over days/weeks.
    """

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="goal",
            description=(
                "Manage persistent user goals that survive across sessions. "
                "INVOKE THIS TOOL (do not write code like goal.list() — call "
                "the function with action=... arguments). "
                "Use ACTIONS: 'add' to record a new goal, 'list' to retrieve "
                "current goals, 'complete' to mark done, 'archive' to pause/"
                "abandon, 'progress' to append a progress note. "
                "Use whenever the user expresses lasting intent (\"my goal "
                "is...\", \"I want to ship...\", \"by Friday I need\") — "
                "DON'T use for transient single-turn requests. "
                "Examples of correct invocation: "
                "call goal with action='add', text='ship the agentic upgrade'; "
                "call goal with action='list', status='active'; "
                "call goal with action='complete', id='abc12345'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "list", "complete", "archive", "progress"],
                        "description": "Goal action to perform.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Goal text (for add).",
                    },
                    "id": {
                        "type": "string",
                        "description": "Goal id (for complete / archive / progress).",
                    },
                    "note": {
                        "type": "string",
                        "description": "Progress note (for progress).",
                    },
                    "due": {
                        "type": "string",
                        "description": "Optional ISO date YYYY-MM-DD (for add).",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["low", "normal", "high"],
                        "description": "Priority (for add). Default normal.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["active", "done", "archived", "all"],
                        "description": "Status filter (for list). Default active.",
                    },
                },
                "required": ["action"],
            },
            category="goal",
        )

    def execute(self, **params: Any) -> ToolResult:
        if not _gt._enabled():  # noqa: SLF001 — env gate check
            return ToolResult(
                tool_name=self.spec.name,
                success=False,
                content="Goal tracking is disabled. Set OPENJARVIS_GOALS_ENABLED=true to enable.",
            )

        action = (params.get("action") or "").lower()
        try:
            if action == "add":
                result = _gt.goal_add(
                    params.get("text", ""),
                    params.get("due"),
                    params.get("priority"),
                )
            elif action == "list":
                result = _gt.goal_list(
                    params.get("status", "active"),
                    int(params.get("limit", 20)),
                )
            elif action == "complete":
                result = _gt.goal_complete(params.get("id", ""))
            elif action == "archive":
                result = _gt.goal_archive(params.get("id", ""))
            elif action == "progress":
                result = _gt.goal_progress(params.get("id", ""), params.get("note", ""))
            else:
                return ToolResult(
                    tool_name=self.spec.name,
                    success=False,
                    content=f"Unknown action: {action}. Use add/list/complete/archive/progress.",
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                tool_name=self.spec.name,
                success=False,
                content=f"goal_tracker {action} failed: {exc}",
            )

        return ToolResult(
            tool_name=self.spec.name,
            success=bool(result.get("ok", True)) and "error" not in result,
            content=json.dumps(result, ensure_ascii=False),
        )
