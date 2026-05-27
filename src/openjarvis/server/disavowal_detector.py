"""Disavowal detector — the FIRST half of the self-improvement loop.

THE FAILURE MODE WE'RE CLOSING
------------------------------
User asks Jarvis to "verify the last email I received today."
Tools (gmail_*, outlook_*) ARE auto-injected into the request.
The LLM, instead of calling outlook_list_messages, says:
  "I don't currently have a specific tool to query your calendar..."
  "I apologize, I don't have a direct 'get emails' function available..."
  "I have access to your Outlook account via OAuth, but I don't have..."

That's a DISAVOWAL — the LLM denying a capability it provably has.
Today's pipeline collected this as a low-confidence reflection but
nothing did anything with it. This module makes the disavowal *itself*
a structured event with the diagnostic data needed for auto-promotion.

THE OUTPUT
----------
On each turn, AFTER the LLM has replied, we check:
  1. Did the response text contain disavowal patterns?
  2. WAS the relevant tool actually injected (proving the LLM had it)?

If both true → append a structured event to
``~/.openjarvis/disavowals.jsonl``:

    {
      "ts": ..., "iso": "...",
      "user_text": "verify the last email I received today",
      "assistant_text": "I don't have a tool for...",
      "injected_tool_groups": ["outlook", "calendar"],
      "injected_tool_names": ["outlook_list_messages", ...],
      "inferred_category": "email",   // or "calendar", "watch", "unknown"
      "session_id": "...",
    }

The `learned_intents` module then reads this jsonl, clusters by
category + phrasing, and auto-promotes the most-common failing
phrasings to runtime patterns that bypass the LLM entirely.

Env: OPENJARVIS_DISAVOWAL_DETECTOR_ENABLED (default true).
"""

from __future__ import annotations

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


_LOCK = threading.Lock()


# Phrases the LLM emits when it disavows a tool it actually has. Collected
# from real production conversations today (2026-05-27).
_DISAVOWAL_PATTERNS: List[re.Pattern] = [
    re.compile(r"I\s+don'?t\s+(?:currently\s+|actually\s+)?have\s+a\s+(?:specific\s+|direct\s+)?tool", re.I),
    re.compile(r"I\s+don'?t\s+(?:currently\s+|actually\s+)?have\s+(?:a\s+)?(?:function|capability|specific\s+function)", re.I),
    re.compile(r"I\s+don'?t\s+(?:currently\s+|actually\s+)?have\s+access\s+to", re.I),
    re.compile(r"I\s+apolog(?:ise|ize)[^.]*don'?t\s+have", re.I),
    re.compile(r"this\s+is\s+a\s+limitation\s+(?:of|in)\s+(?:my|the)\s+(?:available\s+)?tool", re.I),
    re.compile(r"isn'?t\s+(?:currently\s+|actually\s+)?(?:available|exposed|in\s+my\s+toolset|accessible)", re.I),
    re.compile(r"functions?\s+(?:to|that\s+would)\s+(?:retrieve|check|query|search|monitor|access)[^.]*aren'?t\s+(?:currently\s+)?(?:available|exposed)", re.I),
    re.compile(r"I\s+(?:would\s+)?need\s+to[^.]*but\s+don'?t\s+have", re.I),
    re.compile(r"more\s+limited\s+than\s+I'?d?\s+need", re.I),
    re.compile(r"I\s+(?:can|could)\s+only\s+(?:interact|access)\s+(?:with|through)[^.]*explicitly\s+granted", re.I),
    re.compile(r"this\s+(?:seems|appears)\s+(?:to\s+be\s+)?(?:a\s+)?(?:gap|limitation)\s+in\s+(?:my|the)\s+(?:available\s+)?tool", re.I),
    re.compile(r"I\s+(?:don'?t|can'?t)\s+(?:retrieve|query|check)\s+your\s+(?:emails?|calendar|messages?|inbox)", re.I),
    # Past tense "I didn't actually call the tool"
    re.compile(r"I\s+(?:didn'?t|did\s+not)\s+(?:actually\s+)?(?:call|invoke|use)\s+(?:the|a)\s+(?:tool|function)", re.I),
]


_CATEGORY_HINTS: Dict[str, List[str]] = {
    "email": ["email", "inbox", "mail", "message", "gmail", "outlook"],
    "calendar": ["calendar", "meeting", "event", "appointment", "schedule", "agenda"],
    "watch": ["notify", "alert", "let me know", "watch", "lookout", "monitor", "wait for", "trigger"],
}


def _enabled() -> bool:
    return os.environ.get("OPENJARVIS_DISAVOWAL_DETECTOR_ENABLED", "true").lower() in (
        "1", "true", "yes", "on",
    )


def _log_path() -> Path:
    base = os.environ.get("OPENJARVIS_HOME", "").strip()
    d = Path(base) if base else Path.home() / ".openjarvis"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d / "disavowals.jsonl"


def _looks_like_disavowal(assistant_text: str) -> bool:
    if not assistant_text or len(assistant_text) < 20:
        return False
    return any(p.search(assistant_text) for p in _DISAVOWAL_PATTERNS)


def _infer_category(user_text: str, assistant_text: str) -> str:
    blob = f"{user_text or ''} {assistant_text or ''}".lower()
    best_cat = "unknown"
    best_score = 0
    for cat, hints in _CATEGORY_HINTS.items():
        score = sum(1 for h in hints if h in blob)
        if score > best_score:
            best_cat = cat
            best_score = score
    return best_cat


def record_if_disavowal(
    *,
    user_text: str,
    assistant_text: str,
    injected_tool_groups: Optional[List[str]] = None,
    injected_tool_names: Optional[List[str]] = None,
    session_id: str = "",
) -> Optional[Dict[str, Any]]:
    """Inspect (user, assistant) pair. If the assistant disavowed a
    capability it should have, log a structured event and return it.
    Returns None when no disavowal is detected."""
    if not _enabled():
        return None
    if not _looks_like_disavowal(assistant_text):
        return None
    # Recording bar: if the caller told us tools were injected, great.
    # Otherwise rely on category inference from the user's text — a
    # disavowal of "email" tools when the user asked about email is
    # strong-enough evidence the LLM had the tools (auto-inject is
    # near-universal in production now).
    groups = injected_tool_groups or []
    category = _infer_category(user_text, assistant_text)
    if not groups and category == "unknown":
        return None  # nothing actionable to promote
    has_relevant_tools = bool(groups) and any(
        g not in ("always_on", "always_on_only") for g in groups
    )
    if groups and not has_relevant_tools and category == "unknown":
        return None  # only always_on injected, no domain match → skip
    event = {
        "ts": time.time(),
        "iso": datetime.now(timezone.utc).isoformat(),
        "user_text": (user_text or "").strip()[:600],
        "assistant_text": (assistant_text or "").strip()[:600],
        "injected_tool_groups": list(groups),
        "injected_tool_names": list(injected_tool_names or [])[:20],
        "inferred_category": category,
        "session_id": session_id,
    }
    try:
        with _LOCK:
            with open(_log_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.debug("disavowal_detector: write failed: %s", exc)
        return event

    logger.warning(
        "openjarvis.disavowal.detected category=%s user=%r groups=%s",
        category, event["user_text"][:80], groups,
    )
    return event


def read_recent(*, limit: int = 200, category: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read recent disavowals. Used by learned_intents to cluster + promote."""
    p = _log_path()
    if not p.exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if category and rec.get("inferred_category") != category:
                    continue
                out.append(rec)
    except Exception as exc:
        logger.debug("disavowal_detector: read failed: %s", exc)
        return []
    return out[-limit:]


def snapshot() -> Dict[str, Any]:
    p = _log_path()
    out: Dict[str, Any] = {"enabled": _enabled(), "log_path": str(p)}
    try:
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                total = sum(1 for _ in f)
            out["total_disavowals_logged"] = total
        else:
            out["total_disavowals_logged"] = 0
    except Exception as exc:
        out["error"] = str(exc)
    return out
