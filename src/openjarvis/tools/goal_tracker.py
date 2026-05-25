"""Persistent goal tracker (Round 2.2 of OpenJarvis-native agentic upgrade).

User-stated goals persist across sessions to ``~/.openjarvis/goals.json``.
Every turn pulls the active goals and injects them into the system prompt
via ``SystemPromptBuilder`` so the agent stays aligned with what the user
is actually trying to accomplish over days/weeks, not just the current turn.

Tools exposed to the model:
    goal_add(text, due?, priority?)       — create a goal
    goal_list(status?)                    — list goals (default: active only)
    goal_complete(id)                     — mark a goal done
    goal_archive(id)                      — archive a paused/abandoned goal
    goal_progress(id, note)               — append a progress note

Storage format (single JSON file with all goals, structured for round-trip):
    {
      "goals": [
        {"id": "abc12345", "text": "...", "status": "active|done|archived",
         "priority": "low|normal|high", "created_at": "...", "due": "...|null",
         "notes": [{"ts": "...", "text": "..."}]}, ...
      ]
    }

Env gate: OPENJARVIS_GOALS_ENABLED (default false; opt-in after deploy).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool schemas (OpenAI function-calling shape)
# ---------------------------------------------------------------------------

GOAL_ADD_SCHEMA = {
    "name": "goal_add",
    "description": (
        "Record a persistent goal for the user that spans multiple turns or "
        "days. Use when the user expresses lasting intent (\"I want to X\", "
        "\"goal: X\", \"by Friday I need\"). Don't use for transient single-turn requests."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The goal, one short sentence."},
            "due": {"type": "string", "description": "Optional ISO date YYYY-MM-DD."},
            "priority": {"type": "string", "enum": ["low", "normal", "high"]},
        },
        "required": ["text"],
    },
}

GOAL_LIST_SCHEMA = {
    "name": "goal_list",
    "description": "List goals (default: active only).",
    "parameters": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["active", "done", "archived", "all"]},
            "limit": {"type": "integer"},
        },
    },
}

GOAL_COMPLETE_SCHEMA = {
    "name": "goal_complete",
    "description": "Mark a goal as done. Only when the user has explicitly accomplished it.",
    "parameters": {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    },
}

GOAL_ARCHIVE_SCHEMA = {
    "name": "goal_archive",
    "description": "Archive a goal (paused or abandoned).",
    "parameters": {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    },
}

GOAL_PROGRESS_SCHEMA = {
    "name": "goal_progress",
    "description": "Append a progress note to an active goal.",
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "note": {"type": "string"},
        },
        "required": ["id", "note"],
    },
}


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _goals_path() -> Path:
    base = os.environ.get("OPENJARVIS_HOME", "").strip()
    if base:
        d = Path(base)
    else:
        d = Path.home() / ".openjarvis"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d / "goals.json"


_FILE_LOCK = threading.Lock()


def _load() -> Dict[str, Any]:
    path = _goals_path()
    if not path.exists():
        return {"goals": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("goal_tracker: load failed (%s) — starting fresh", exc)
        return {"goals": []}


def _save(data: Dict[str, Any]) -> None:
    path = _goals_path()
    try:
        with _FILE_LOCK:
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("goal_tracker: save failed: %s", exc)
    # Invalidate the system-prompt-block snapshot so the next turn picks
    # up the new/changed goals immediately (instead of waiting for the
    # 60-second TTL to expire).
    try:
        with _SNAPSHOT_LOCK:
            _SNAPSHOT["ts"] = 0.0
            _SNAPSHOT["active"] = []
    except Exception:
        pass


def _make_id(text: str, ts: float) -> str:
    raw = f"{text.strip()[:120]}::{int(ts)}".encode("utf-8", errors="replace")
    return hashlib.sha1(raw).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def _enabled() -> bool:
    return os.environ.get("OPENJARVIS_GOALS_ENABLED", "false").lower() in ("1", "true", "yes", "on")


def goal_add(text: str, due: Optional[str] = None,
             priority: Optional[str] = None) -> Dict[str, Any]:
    if not text or not text.strip():
        return {"error": "text required"}
    now = time.time()
    gid = _make_id(text, now)
    goal = {
        "id": gid,
        "text": text.strip()[:300],
        "status": "active",
        "priority": (priority or "normal").lower(),
        "due": (due or "").strip() or None,
        "created_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "notes": [],
    }
    data = _load()
    data["goals"].append(goal)
    _save(data)
    logger.info("openjarvis.goal_tracker.add id=%s text=%r", gid, text[:80])
    return {"ok": True, "goal": goal}


def goal_list(status: str = "active", limit: int = 20) -> Dict[str, Any]:
    data = _load()
    goals = data.get("goals", [])
    if status and status != "all":
        goals = [g for g in goals if g.get("status") == status]
    return {"goals": goals[:limit]}


def goal_complete(id: str) -> Dict[str, Any]:
    return _set_status(id, "done")


def goal_archive(id: str) -> Dict[str, Any]:
    return _set_status(id, "archived")


def _set_status(gid: str, status: str) -> Dict[str, Any]:
    if not gid:
        return {"error": "id required"}
    data = _load()
    for g in data.get("goals", []):
        if g.get("id") == gid:
            g["status"] = status
            g["updated_at"] = datetime.now(timezone.utc).isoformat()
            _save(data)
            logger.info("openjarvis.goal_tracker.status id=%s status=%s", gid, status)
            return {"ok": True, "goal": g}
    return {"error": f"goal {gid} not found"}


def goal_progress(id: str, note: str) -> Dict[str, Any]:
    if not id or not note:
        return {"error": "id and note required"}
    data = _load()
    for g in data.get("goals", []):
        if g.get("id") == id:
            notes = g.get("notes") or []
            notes.append({"ts": datetime.now(timezone.utc).isoformat(), "text": note.strip()[:280]})
            g["notes"] = notes[-20:]
            _save(data)
            logger.info("openjarvis.goal_tracker.update id=%s note=%r", id, note[:60])
            return {"ok": True, "goal": g}
    return {"error": f"goal {id} not found"}


def dispatch(tool_name: str, args: Dict[str, Any]) -> str:
    """Single entry point for tool registry dispatch. Returns JSON string."""
    try:
        if tool_name == "goal_add":
            result = goal_add(args.get("text", ""), args.get("due"), args.get("priority"))
        elif tool_name == "goal_list":
            result = goal_list(args.get("status", "active"), int(args.get("limit", 20)))
        elif tool_name == "goal_complete":
            result = goal_complete(args.get("id", ""))
        elif tool_name == "goal_archive":
            result = goal_archive(args.get("id", ""))
        elif tool_name == "goal_progress":
            result = goal_progress(args.get("id", ""), args.get("note", ""))
        else:
            result = {"error": f"unknown goal tool {tool_name}"}
    except Exception as exc:
        logger.warning("goal_tracker.dispatch %s failed: %s", tool_name, exc)
        result = {"error": str(exc)[:200]}
    return json.dumps(result, ensure_ascii=False)


def tool_schemas() -> List[Dict[str, Any]]:
    if not _enabled():
        return []
    return [GOAL_ADD_SCHEMA, GOAL_LIST_SCHEMA, GOAL_COMPLETE_SCHEMA,
            GOAL_ARCHIVE_SCHEMA, GOAL_PROGRESS_SCHEMA]


# ---------------------------------------------------------------------------
# System-prompt block — called by SystemPromptBuilder
# ---------------------------------------------------------------------------

_SNAPSHOT_LOCK = threading.Lock()
_SNAPSHOT: Dict[str, Any] = {"ts": 0.0, "active": []}
_SNAPSHOT_TTL = 60.0  # 1 min — goals change rarely; cheap to refresh


def active_goals_block(max_goals: int = 5) -> str:
    """Short markdown listing active goals — injected into system prompt
    every turn so the agent stays aligned across sessions."""
    if not _enabled():
        return ""
    now = time.time()
    with _SNAPSHOT_LOCK:
        if (now - _SNAPSHOT["ts"]) > _SNAPSHOT_TTL:
            try:
                goals = _load().get("goals", [])
            except Exception:
                goals = []
            active = [g for g in goals if g.get("status") == "active"]
            prio_order = {"high": 0, "normal": 1, "low": 2}
            active.sort(key=lambda g: (
                prio_order.get(g.get("priority", "normal"), 1),
                g.get("due") or "9999-12-31",
            ))
            _SNAPSHOT["ts"] = now
            _SNAPSHOT["active"] = active
        active = _SNAPSHOT["active"][:max_goals]

    if not active:
        return ""

    lines = ["# Active Goals", ""]
    for g in active:
        line = f"- [{g['id']}] ({g.get('priority', 'normal')}) {g['text']}"
        if g.get("due"):
            line += f"  _due {g['due']}_"
        lines.append(line)
    lines.append("")
    lines.append(
        "Keep responses aligned with these goals. If a response moves one forward, "
        "use goal_progress. If a goal is accomplished, use goal_complete."
    )
    return "\n".join(lines)
